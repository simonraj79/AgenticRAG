"""Ask, query history, and the trace timeline.

Three routes, one idea: **a turn is not finished when the answer is returned, it
is finished when the record of it is durable.** The workshop's version prints an
answer and forgets it. Everything that makes this build worth deploying --
citations in the UI, the Stage 2 before/after demo, the `contexts` Ragas scores
in Stage 3 -- is downstream of rows written here, and none of it can be
reconstructed afterwards from an answer string.

So one question produces four kinds of write, and they land in a single
transaction:

    queries        one row: what was asked, what came back, how long, refused?
    query_chunks   what was actually in the prompt, with both scores
    trace_events   why the pipeline did what it did, in order
    (agents)       untouched -- config is read here, never written

The transaction boundary is deliberate. A `queries` row whose `query_chunks` are
missing is not a partial record, it is a misleading one: Stage 3 would read it
as "this question retrieved nothing" and score context recall at zero against a
turn that actually retrieved fine. Rolling the whole turn back costs a lost
debugging trail; committing half of it costs a wrong measurement, and a wrong
measurement is the only failure mode Stage 3 exists to prevent.

**Not implemented here: SSE streaming.** PRD section 2.2 specifies token-by-token
transport and this route returns plain JSON. Streaming and durable recording pull
in opposite directions -- the row is only complete once the last token has
arrived -- so the shape that works is to stream tokens and write the rows in the
same handler after the stream closes. That is a change to this route's response
type, not to anything below it. Deferred, not designed around.
"""

from __future__ import annotations

import re
import time
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi import Query as QueryParam  # `Query` is the SQLAlchemy model below.
from langchain_core.documents import Document as LCDocument
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.api.deps import CurrentUser, DbSession, OwnedAgent
from app.auth.deps import optional_session
from app.config import settings
from app.db.models import Agent, Chunk, Document, Query, QueryChunk, Session, TraceEvent
from app.rag.pipeline import answer_question
from app.rag.retriever import META_CHUNK_ID, search_with_scores
from app.rag.trace import (
    GENERATE,
    REFUSE,
    RERANK,
    RETRIEVE,
    SCORE_CHECK,
    TraceRecorder,
)

router = APIRouter(prefix="/api", tags=["ask"])

# `queries.session_id` wants the session row's id, and only `optional_session`
# returns the row rather than the user. Optional rather than required: the
# ownership check has already happened via OwnedAgent, so a resolvable session
# is a bonus (it tells you which browser asked), not a precondition.
OptionalSession = Annotated[Session | None, Depends(optional_session)]

# Where CohereRerank writes its score. Verified against langchain_cohere/rerank.py
# (`doc_copy.metadata["relevance_score"] = res["relevance_score"]`), which also
# deepcopies the base metadata -- so `chunk_id` survives the rerank and a
# reranked document still joins back to its Postgres row.
RERANK_SCORE_KEY = "relevance_score"

# Enough of the chunk to recognise it in a citation list without shipping the
# whole corpus to the browser on every answer.
PREVIEW_CHARS = 240

# How much of an answer may go by before a refusal phrase stops counting as a
# refusal.
#
# A model that declines says so before it says anything else. A model that
# answers and *then* notes a gap -- "...the lesson covers X, Y and Z. The context
# does not say when the method was first published." -- is qualifying an answer,
# not refusing, and counting that turn as a refusal corrupts the one metric
# Stage 3 uses to check that grounding works.
#
# Position alone does not separate the two, because a real answer's first
# sentence can run past any fixed character window. What separates them is how
# much substance precedes the phrase: scanning stops once the model has produced
# ~200 characters of actual content, so a marker in the opening (however
# long-winded the refusal that follows) counts and a marker after two sentences
# of real answer does not. The budget is charged per sentence and only after that
# sentence has been checked, so a leading apology barely dents it and a single
# long opening sentence is still examined in full.
REFUSAL_LEAD_CHARS = 200

# How much preamble may precede a CAVEAT_MARKERS phrase and still count. Room
# for an apology or a hedge ("I'm sorry.", "Let me check the context.") and
# nothing more.
REFUSAL_PREAMBLE_CHARS = 40

# Substring markers, lowercased and whitespace-normalised before matching, in
# two tiers -- because the phrases split cleanly into two kinds and treating
# them alike is what produces false positives.
#
# REFUSAL_MARKERS are phrasings a model reaches for only when declining. They
# count anywhere in the lead.
#
# CAVEAT_MARKERS are ordinary qualifications. "The material does not include a
# glossary" is a perfectly good sentence inside a real answer, and a flat
# substring test over the opening would score that turn as a refusal. They count
# only when said before the model has answered anything -- a refusal leads with
# the reason it is refusing; an answer earns its caveats first.
#
# This is still a heuristic over natural language and it is still fallible in
# both directions. It is spelled out rather than inferred because
# `queries.refused` is a Stage 3 success metric (PRD section 4.4: a correct
# refusal is a correct outcome), and `golden_questions.expected_behaviour =
# 'refuse'` is what will actually calibrate these lists -- ten questions whose
# right answer is "I don't know" will show which phrasings Gemma really reaches
# for and which entries here never fire once.
REFUSAL_MARKERS = (
    "does not contain",
    "doesn't contain",
    "do not contain",
    "don't contain",
    "no information",
    "not enough information",
    "insufficient information",
    "no relevant information",
    "cannot answer",
    "can't answer",
    "cannot be answered",
    "unable to answer",
    "i do not know",
    "i don't know",
    "not found in the",
    "not covered in the",
    "outside the scope",
)

CAVEAT_MARKERS = (
    "context does not",
    "does not mention",
    "doesn't mention",
    "does not provide",
    "doesn't provide",
    "does not include",
    "does not specify",
    "does not appear in",
)


# --------------------------------------------------------------------------
# Wire shapes
# --------------------------------------------------------------------------

class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)


class CitationOut(BaseModel):
    """One retrieved chunk, as the UI cites it.

    Both scores are present and both can be null, which is the point rather than
    sloppiness: `similarity_score` is Pinecone's cosine score from the first-pass
    search and `rerank_score` is Cohere's, so a Stage 1 answer has the first and
    not the second. Showing the pair side by side IS the reranking demo.
    """

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    filename: str
    chunk_index: int
    rank: int
    similarity_score: float | None = None
    rerank_score: float | None = None
    text_preview: str


class AskOut(BaseModel):
    query_id: uuid.UUID
    answer: str
    refused: bool
    latency_ms: int
    model_used: str
    citations: list[CitationOut]


class QueryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    question: str
    answer: str | None = None
    refused: bool
    latency_ms: int | None = None
    model_used: str | None = None
    created_at: datetime


class TraceEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    step_index: int
    event_type: str
    payload: dict | None = None
    score: float | None = None
    duration_ms: int | None = None
    created_at: datetime


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _chunk_uuid(doc: LCDocument) -> uuid.UUID | None:
    """The Postgres `chunks.id` behind a retrieved vector, or None.

    `chunk_id` is written into every vector's metadata at upsert and is the only
    join key between the two stores. A vector without a parseable one is either
    hand-written or predates the current metadata scheme; it can still ground an
    answer perfectly well, so it is dropped from the citation list rather than
    being allowed to fail the request. The alternative -- a 500 on a question
    that was answered correctly -- trades a missing footnote for a broken route.
    """
    raw = doc.metadata.get(META_CHUNK_ID)
    if not raw:
        return None
    try:
        return uuid.UUID(str(raw))
    except ValueError:
        return None


def _sentences(text: str) -> list[str]:
    """Split into sentences, for the refusal scan only.

    Splits on a terminator followed by whitespace, and on blank lines. It does
    NOT split on a lone newline, which is the case that matters: generated text
    soft-wraps mid-sentence, so treating every newline as a boundary would cut
    "does not\\ncontain" into two fragments and the marker would match neither.
    """
    return [s for s in re.split(r"(?<=[.!?])\s+|\n{2,}", text) if s.strip()]


def _detect_refusal(answer: str) -> str | None:
    """The matched refusal phrase, or None. See REFUSAL_MARKERS.

    **Refusal is detected from the answer, never from the score.** CLAUDE.md
    records the measurement that settles this: on `3.1-lesson-gist.md`,
    on-topic questions scored 0.61-0.67 and off-topic ones 0.49-0.58 -- an
    overlapping band, not a separation -- and the plainly off-topic "What is the
    refund policy for this course?" scored **0.5765**, comfortably *above* the
    0.5 threshold. It was refused anyway, correctly, because the system prompt
    forbids answering outside the context. The threshold governs *rewriting*;
    the prompt governs *refusing*. Wiring `refused` to the threshold would have
    marked that turn as answered.

    Sentence by sentence until the content budget runs out -- see
    REFUSAL_LEAD_CHARS for why a fixed character window is not enough, and
    CAVEAT_MARKERS for why the markers are in two tiers. Whitespace is normalised
    inside each sentence because generated text wraps, and "does not  contain" is
    the same refusal as "does not contain".
    """
    consumed = 0
    for sentence in _sentences(answer):
        lowered = " ".join(sentence.lower().split())
        for marker in REFUSAL_MARKERS:
            if marker in lowered:
                return marker
        if consumed <= REFUSAL_PREAMBLE_CHARS:
            for marker in CAVEAT_MARKERS:
                if marker in lowered:
                    return marker
        # Charged after the checks, so the sentence that blows the budget is
        # still examined. A single long opening sentence that refuses is a
        # refusal.
        consumed += len(lowered)
        if consumed >= REFUSAL_LEAD_CHARS:
            return None
    return None


async def _chunk_rows(
    db: AsyncSession, agent: Agent, chunk_ids: list[uuid.UUID]
) -> dict[uuid.UUID, tuple[int, uuid.UUID, str]]:
    """chunk_id -> (chunk_index, document_id, filename), for chunks this agent owns.

    The `Document.agent_id` predicate is not redundant with the namespace scoping
    that already happened. It does two jobs.

    It is the second half of the tenancy boundary: `app/api/deps.py` proves the
    caller owns the agent and `rag/retriever.py` proves the query only reached
    that agent's namespace, and this proves the rows we are about to write
    `query_chunks` against belong to the same agent. Three independent checks on
    the one boundary whose failure is silent.

    It also quietly drops orphaned vectors -- the failure `rag/delete.py`'s
    docstring describes, where rows went and vectors stayed. Those vectors still
    match queries, so they can appear in `result.documents`; inserting a
    `query_chunks` row for one would violate the foreign key and fail the whole
    turn at commit, with an IntegrityError naming a chunk id that no longer
    exists anywhere. Left-joining them away costs a citation and saves the answer.
    """
    if not chunk_ids:
        return {}

    rows = await db.execute(
        select(Chunk.id, Chunk.chunk_index, Chunk.document_id, Document.filename)
        .join(Document, Document.id == Chunk.document_id)
        .where(Chunk.id.in_(chunk_ids), Document.agent_id == agent.id)
    )
    return {r.id: (r.chunk_index, r.document_id, r.filename) for r in rows}


# --------------------------------------------------------------------------
# POST /api/agents/{agent_id}/ask
# --------------------------------------------------------------------------

@router.post("/agents/{agent_id}/ask", response_model=AskOut)
async def ask(
    body: AskRequest,
    agent: OwnedAgent,
    user: CurrentUser,
    db: DbSession,
    session: OptionalSession,
) -> AskOut:
    """Answer one question against one agent's corpus, and record the turn.

    Stage 1 only: retrieve, optionally rerank, generate. There is no rewrite loop
    yet, which is why SCORE_CHECK below records a comparison and then does
    nothing with it.
    """
    question = body.question.strip()
    if not question:
        # `min_length=1` catches the empty string; this catches "   ".
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="question is required",
        )

    started = time.perf_counter()

    # The `queries` row is created FIRST, as a placeholder, and flushed. Two
    # reasons, both about ordering rather than about durability.
    #
    # `trace_events.query_id` is a NOT NULL foreign key, so the recorder needs a
    # real id before it can record step 0 -- and step 0 happens before there is
    # an answer to store. SQLAlchemy's unit of work would sort the inserts
    # correctly on its own at commit, but only because it knows the FK; making
    # the order explicit means a trace written by a future code path that does
    # not go through this function still lands after its query.
    #
    # `flush()` is not `commit()`. Nothing is visible to another transaction
    # yet, and a failure anywhere below rolls this placeholder back with
    # everything else.
    query = Query(
        id=uuid.uuid4(),
        user_id=user.id,
        agent_id=agent.id,
        # Which browser asked. Nullable, and null is not an error: the cookie may
        # have expired between the ownership check and here, and losing the
        # session attribution is not a reason to refuse an authenticated request.
        session_id=session.id if session is not None else None,
        question=question,
    )
    db.add(query)
    await db.flush()

    trace = TraceRecorder(db, query.id)

    # ------------------------------------------------------------------
    # 1. Scored retrieval
    # ------------------------------------------------------------------
    # Retrieval runs twice per question and that is a known, temporary cost.
    # `answer_question` returns Documents but not their Pinecone scores, and
    # this route needs them twice over: `query_chunks.similarity_score` is a
    # per-chunk score, and SCORE_CHECK needs the top one. The right fix is for
    # the pipeline to return scores, and Stage 2 forces it anyway -- the rewrite
    # loop branches on the top score, so the check has to live inside the
    # pipeline rather than beside it. Until then this route pays for the trace
    # rather than fabricating it.
    #
    # The price is bounded and measured. CLAUDE.md's latency breakdown: embed
    # 365 ms, Pinecone k=20 394 ms, generation 13.2 s -- 89% of the turn. A
    # second embed-and-search is roughly 5% on top, spent to make citations and
    # the Trace view truthful.
    #
    # `run_in_threadpool` because `search_with_scores` is synchronous --
    # `similarity_search_with_score` blocks on a network call. Awaiting it
    # directly would block the event loop for the length of an index query, and
    # the Render starter plan runs a single uvicorn worker, so every other
    # in-flight request would queue behind it. FastAPI does this automatically
    # for `def` handlers; an `async def` that calls blocking code has to ask.
    t0 = time.perf_counter()
    scored: list[tuple[LCDocument, float]] = await run_in_threadpool(
        search_with_scores, agent, question
    )
    retrieve_ms = int((time.perf_counter() - t0) * 1000)

    similarity_by_chunk: dict[uuid.UUID, float] = {}
    ranked_before: list[str] = []
    for doc, score in scored:
        cid = _chunk_uuid(doc)
        if cid is None:
            continue
        # setdefault, not assignment: if the same chunk somehow comes back twice
        # the better-ranked occurrence is the one that counts.
        similarity_by_chunk.setdefault(cid, float(score))
        ranked_before.append(str(cid))

    # Pinecone's cosine metric is higher-is-closer, so this is the raw score with
    # no inversion. See `retriever.search_with_scores` -- "fixing" it by
    # subtracting from 1 silently inverts the Stage 2 rewrite trigger.
    top_score = float(scored[0][1]) if scored else None

    trace.record(
        RETRIEVE,
        payload={
            "k": agent.retrieve_k,
            "returned": len(scored),
            "top_score": top_score,
            # The pre-rerank ordering. Half of the RERANK event's before/after
            # pair, and on its own the answer to "did the right chunk even come
            # back, or did the reranker never get a chance?"
            "chunk_ids": ranked_before,
            "scores": [round(float(s), 6) for _, s in scored],
        },
        score=top_score,
        duration_ms=retrieve_ms,
    )

    # ------------------------------------------------------------------
    # 2. Score check -- OBSERVABILITY ONLY
    # ------------------------------------------------------------------
    # `score_threshold` governs REWRITING (Stage 2). It does not govern refusing,
    # and it is not a safety control. This project has already measured the
    # difference: the off-topic question "What is the refund policy for this
    # course?" scored 0.5765 against a 0.5 threshold -- above it, so Stage 2
    # would not have rewritten -- and was refused correctly anyway, by the system
    # prompt. On-topic questions scored 0.61-0.67 and off-topic ones 0.49-0.58,
    # which is an overlap, not a separation. Treating this number as the gate on
    # whether to answer would let that refund question through.
    #
    # It is recorded because Stage 3 exists to turn 0.5 into a measured number,
    # and that measurement needs the comparison logged on every turn including
    # the ones where nothing happened as a result.
    below_threshold = top_score is not None and top_score < agent.score_threshold
    trace.record(
        SCORE_CHECK,
        payload={
            "top_score": top_score,
            "threshold": agent.score_threshold,
            "below_threshold": below_threshold,
            "max_rewrites": agent.max_rewrites,
            "governs": "rewrite",
            "action": "none -- Stage 1 has no rewrite loop",
        },
        score=top_score,
    )

    # ------------------------------------------------------------------
    # 3. Generate
    # ------------------------------------------------------------------
    t0 = time.perf_counter()
    result = await answer_question(agent, question)
    generate_ms = int((time.perf_counter() - t0) * 1000)

    final_ids = [cid for cid in (_chunk_uuid(d) for d in result.documents) if cid]

    # RERANK is recorded before GENERATE even though both are learned at the same
    # instant: `answer_question` is one opaque call and the reranker fires inside
    # it. `step_index` reflects the order the pipeline ran, not the order this
    # function found out.
    if result.reranked:
        ranked_after = [str(cid) for cid in final_ids]
        # The chunks reranking pulled up from outside the cut a similarity-only
        # retriever would have made. This list IS the Stage 2 demo: empty means
        # Cohere agreed with Pinecone and the rerank changed nothing.
        survivors = set(ranked_before[: agent.rerank_top_n])
        trace.record(
            RERANK,
            payload={
                "model": settings.rerank_model,
                "top_n": agent.rerank_top_n,
                "before": ranked_before,
                "after": ranked_after,
                "rerank_scores": [
                    d.metadata.get(RERANK_SCORE_KEY) for d in result.documents
                ],
                "promoted": [c for c in ranked_after if c not in survivors],
            },
            # No duration, deliberately. The Cohere call happens inside
            # `answer_question` and this route cannot time it separately; a
            # figure derived by subtraction would be a guess wearing a
            # measurement's clothes. CLAUDE.md puts the rerank hop at ~830 ms,
            # Singapore to US, if a rough number is wanted.
            duration_ms=None,
        )

    trace.record(
        GENERATE,
        payload={
            "model": result.model,
            "context_chunks": len(result.documents),
            "context_chars": sum(len(d.page_content) for d in result.documents),
            "answer_chars": len(result.answer),
            # This duration is the whole `answer_question` call, which retrieves
            # before it generates. Labelled rather than silently attributed:
            # generation is ~89% of it (CLAUDE.md), so the number is close, and
            # a reader who needs the split can subtract the RETRIEVE event.
            "duration_includes_retrieval": True,
            "pipeline_latency_ms": result.latency_ms,
        },
        duration_ms=generate_ms,
    )

    # ------------------------------------------------------------------
    # 4. Refusal, from the answer text
    # ------------------------------------------------------------------
    marker = _detect_refusal(result.answer)
    refused = marker is not None
    if refused:
        trace.record(
            REFUSE,
            payload={
                "matched_phrase": marker,
                "detected_in": "answer_text",
                # The pair below is the whole point of recording this event.
                # A refusal with `above_threshold: true` is the refund-policy
                # case: retrieval looked fine by the number and the prompt
                # declined anyway. That combination is evidence the threshold is
                # not the safety control, and it is only visible if both values
                # are on the row.
                "mechanism": "system_prompt",
                "top_score": top_score,
                "threshold": agent.score_threshold,
                "above_threshold": (
                    None if top_score is None else top_score >= agent.score_threshold
                ),
            },
            score=top_score,
        )

    # ------------------------------------------------------------------
    # 5. query_chunks -- citations now, Ragas `contexts` in Stage 3
    # ------------------------------------------------------------------
    # What goes in this table is the FINAL context set: the chunks that were
    # actually in the prompt, not all 20 candidates. That choice is load-bearing
    # for Stage 3. Ragas' context precision and context recall score the contexts
    # the model was given -- feed them the pre-rerank 20 and precision collapses
    # by construction, measuring the retriever's recall rather than the agent's
    # answer, and the scorecard silently reports the wrong system.
    #
    # The discarded candidates are not lost: the full pre-rerank ordering lives
    # in the RETRIEVE and RERANK trace payloads above. `query_chunks` records
    # what was used; `trace_events` records what happened. Neither is a superset
    # of the other and they are not interchangeable.
    known = await _chunk_rows(db, agent, final_ids)

    citations: list[CitationOut] = []
    for position, doc in enumerate(result.documents, start=1):
        # 1-based. `rank` is a position in an ordering ("rank 1" is the best
        # match), unlike `chunk_index` and `step_index`, which are 0-based
        # offsets. Enumeration walks `result.documents` rather than the rows we
        # matched, so a skipped chunk leaves a gap instead of renumbering -- the
        # rank stays the position the chunk held in the model's context.
        cid = _chunk_uuid(doc)
        row = known.get(cid) if cid is not None else None
        if cid is None or row is None:
            continue
        chunk_index, document_id, filename = row

        rerank_raw = doc.metadata.get(RERANK_SCORE_KEY)
        rerank_score = float(rerank_raw) if rerank_raw is not None else None
        similarity_score = similarity_by_chunk.get(cid)

        db.add(
            QueryChunk(
                id=uuid.uuid4(),
                query_id=query.id,
                chunk_id=cid,
                rank=position,
                similarity_score=similarity_score,
                rerank_score=rerank_score,
            )
        )
        citations.append(
            CitationOut(
                chunk_id=cid,
                document_id=document_id,
                filename=filename,
                chunk_index=chunk_index,
                rank=position,
                similarity_score=similarity_score,
                rerank_score=rerank_score,
                text_preview=doc.page_content[:PREVIEW_CHARS],
            )
        )

    # ------------------------------------------------------------------
    # 6. Close the turn
    # ------------------------------------------------------------------
    # Wall time for the whole route, not `result.latency_ms`. That figure covers
    # the pipeline call; this one covers what the user waited for, including the
    # extra scored retrieval above. Storing the smaller number would make the
    # duplicate search invisible in exactly the place someone would look for it.
    latency_ms = int((time.perf_counter() - started) * 1000)

    query.answer = result.answer
    query.model_used = result.model
    query.latency_ms = latency_ms
    query.refused = refused

    # The single commit. Query, chunks and trace land together or not at all.
    await db.commit()

    return AskOut(
        query_id=query.id,
        answer=result.answer,
        refused=refused,
        latency_ms=latency_ms,
        model_used=result.model,
        citations=citations,
    )


# --------------------------------------------------------------------------
# GET /api/agents/{agent_id}/queries
# --------------------------------------------------------------------------

@router.get("/agents/{agent_id}/queries", response_model=list[QueryOut])
async def list_queries(
    agent: OwnedAgent,
    db: DbSession,
    limit: Annotated[int, QueryParam(ge=1, le=200)] = 50,
) -> list[Query]:
    """Recent questions for one agent, newest first.

    Scoped on `agent_id` alone, not on `agent_id AND user_id`. `OwnedAgent` has
    already established that this caller owns this agent, and `queries.agent_id`
    is the tenancy key -- adding a user predicate would look like defence in
    depth while actually encoding a second, weaker rule that would have to be
    kept in step with the first when sharing arrives.

    Capped rather than paginated. A history list is a sidebar, and the ask route
    writes one row per question, so an agent used heavily for an afternoon has
    hundreds of rows and an uncapped query would eventually ship all of them on
    every page load.
    """
    rows = await db.scalars(
        select(Query)
        .where(Query.agent_id == agent.id)
        # `created_at` is a server-side now(), so several questions asked inside
        # the same transaction share a timestamp exactly. The id tie-break is
        # arbitrary but stable, which is what a list being repainted needs -- a
        # non-deterministic order makes rows appear to shuffle between renders.
        .order_by(Query.created_at.desc(), Query.id.desc())
        .limit(limit)
    )
    return list(rows.all())


# --------------------------------------------------------------------------
# GET /api/trace/{query_id}
# --------------------------------------------------------------------------

@router.get("/trace/{query_id}", response_model=list[TraceEventOut])
async def get_trace(
    query_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> list[TraceEvent]:
    """The decision timeline for one query.

    **The ownership check is written out by hand here, and it is the only place
    in this module where that is true.** Every other route takes `OwnedAgent`,
    which binds to an `agent_id` path parameter and does the check in a
    dependency. This route is keyed on a query id and is deliberately not nested
    under an agent -- `/api/trace/{query_id}` is what the pinned contract says --
    so there is no `agent_id` in the path for `owned_agent` to bind to and
    nothing for it to load. FastAPI would not fail; the dependency simply would
    not resolve.

    So the check is manual, and it goes through `queries.user_id` because that is
    the column recording who asked. Going via `queries.agent_id` -> `agents` ->
    `owner_user_id` reaches the same answer today and would be the wrong hook
    later: a shared agent (PRD section 4.2 reserves `visibility` for exactly
    that) would make every viewer of the agent a reader of everyone's questions.
    A trace belongs to the person who asked, not to the corpus.

    404, not the 403 `owned_agent` returns, and the difference is intentional.
    An agent id is a handle the UI holds across sessions, so a wrong owner there
    is usually a stale handle after an account switch and a distinct status says
    so. A query id is only ever obtained from a list this same caller just
    fetched, so there is no stale-handle case to diagnose -- and collapsing
    "not yours" into "not found" gives nothing away.
    """
    query = await db.get(Query, query_id)
    if query is None or query.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Query not found",
        )

    rows = await db.scalars(
        select(TraceEvent)
        .where(TraceEvent.query_id == query_id)
        # `step_index`, never `created_at`. Several events inside one turn land
        # in the same millisecond and share a server-side now(), so a timestamp
        # sort would scramble the pipeline order this view exists to show.
        .order_by(TraceEvent.step_index.asc())
    )
    return list(rows.all())
