"""Ask, query history, and the trace timeline.

Three routes and one engine, all built on one idea: **a turn is not finished when
the answer is returned, it is finished when the record of it is durable.** The
workshop's version prints an answer and forgets it. Everything that makes this
build worth deploying -- citations in the UI, the Stage 2 before/after demo, the
`contexts` Ragas scores in Stage 3, and now a chat thread that can be reopened --
is downstream of rows written here, and none of it can be reconstructed
afterwards from an answer string.

So one question produces five kinds of write, and they land in a single
transaction:

    queries        one row: what was asked, what came back, how long, refused?
    query_chunks   what was actually in the prompt, with both scores
    trace_events   why the pipeline did what it did, in order
    conversations  the thread's title and its last-activity timestamp
    (agents)       untouched -- config is read here, never written

The transaction boundary is deliberate. A `queries` row whose `query_chunks` are
missing is not a partial record, it is a misleading one: Stage 3 would read it
as "this question retrieved nothing" and score context recall at zero against a
turn that actually retrieved fine. Rolling the whole turn back costs a lost
debugging trail; committing half of it costs a wrong measurement, and a wrong
measurement is the only failure mode Stage 3 exists to prevent.

**`run_turn` lives here, and `app/api/conversations.py` imports it.** Both the
one-shot route below and the conversation-scoped one over there are the same
turn -- retrieve, generate, record -- differing only in which thread the row
lands in and how much history reaches the rewriter. Two copies of that body
would drift, and the half that drifts is always the recording half, because it
is the half nobody watches in a browser. The dependency runs one way: this
module knows nothing about conversation routes.

**Every ask now belongs to a thread.** The one-shot route creates one implicitly
rather than writing a `queries` row with a null `conversation_id`. The column
stays nullable because rows from before this change exist and mean "a single
question asked outside any thread" (see `models.Conversation`), but nothing
written from today on adds to that population -- a question with no thread is a
question the chat view cannot show.

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
from collections.abc import Sequence
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi import Query as QueryParam  # `Query` is the SQLAlchemy model below.
from langchain_core.documents import Document as LCDocument
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, DbSession, OwnedAgent
from app.auth.deps import optional_session
from app.config import settings
from app.db.models import (
    Agent,
    Chunk,
    Conversation,
    Document,
    Handout,
    Query,
    QueryChunk,
    Session,
    TraceEvent,
    User,
)
from app.api.handouts import HandoutOut
from app.rag.pipeline import HISTORY_TURNS, ChatTurn, answer_question
from app.rag.refusal import detect_refusal
from app.rag.retriever import META_CHUNK_ID, RERANK_SCORE_KEY
from app.rag.trace import (
    GENERATE,
    REFUSE,
    RERANK,
    RETRIEVE,
    REWRITE,
    SCORE_CHECK,
    TOOL_CALL,
    TOOL_ERROR,
    TOOL_RESULT,
    TraceRecorder,
)

router = APIRouter(prefix="/api", tags=["ask"])

# `queries.session_id` wants the session row's id, and only `optional_session`
# returns the row rather than the user. Optional rather than required: the
# ownership check has already happened via OwnedAgent, so a resolvable session
# is a bonus (it tells you which browser asked), not a precondition.
OptionalSession = Annotated[Session | None, Depends(optional_session)]

# Enough of the chunk to recognise it in a citation list without shipping the
# whole corpus to the browser on every answer.
PREVIEW_CHARS = 240

# How long an auto-derived conversation title may be. `conversations.title` is
# String(200), so this is a legibility limit rather than a storage one: the chat
# sidebar shows one line, and a 200-character "title" is an ellipsis with a few
# words in front of it.
TITLE_MAX_CHARS = 80

# Refusal markers and the two detection functions now live in
# `app/rag/refusal.py`. They moved when the agent loop needed the same
# phrases: `agent_loop` cannot import from `app.api`, and a second copy of a
# list that has already been wrong three times is the one outcome worth
# ruling out structurally. That module's docstring explains why there are two
# functions and why only one of them may write `queries.refused`.


# Anything in square brackets on one line, captured so the contents can be
# judged. Newlines are excluded so an unbalanced `[` cannot swallow a paragraph
# looking for its partner.
_BRACKET_RE = re.compile(r"\[([^\[\]\n]{1,300})\]")

# Splits `[1, 2]` and `[a.md; b.md]` into their parts. Tried only after the
# whole bracket has failed to resolve, so a filename containing a comma is still
# matched intact.
_MARKER_SEPARATORS = re.compile(r"[,;]")


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

    **`marker` and `rank` always carry the same integer, and both fields stay.**
    `rank` is a position in the retrieval ordering; `marker` is the number the
    answer text puts in brackets. They coincide because the answer's `[n]` has to
    point at the nth chunk of the context the model was actually handed -- but
    they are different facts, and a UI resolving a chip must read `marker`. The
    day the two diverge (a citation dropped, a set reordered for display) reading
    `rank` would silently attribute a claim to the wrong source, which is worse
    than showing no source at all because it still looks like provenance.

    There is no `marker` column. It is `query_chunks.rank` on the way back in --
    see `conversations._load_messages` -- which is exactly why the two must not
    be allowed to drift apart.
    """

    marker: int
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    filename: str
    chunk_index: int
    rank: int
    similarity_score: float | None = None
    rerank_score: float | None = None
    text_preview: str


class AskOut(BaseModel):
    """One completed turn, as the asker sees it.

    `conversation_id` is load-bearing rather than informational: the chat UI
    never calls the explicit create route, so every thread comes into existence
    through an ask and this field is the only way the client learns its id.

    `rewritten_question` is null when contextualisation did not run, and a string
    when it did -- **even if that string equals `question`.** Null and unchanged
    are different facts (`pipeline.contextualize_question` explains why), and a
    client showing "searched for: ..." must be able to tell "the model read this
    and left it alone" from "the model was never asked".

    `tool_steps` and `handouts` both default to empty, so an agent with tools off
    serialises exactly as it did before the loop existed. That is not tidiness:
    it is what lets an agent whose scorecard is already recorded in EVAL.md stay
    reproducible, and it is asserted by scenario S1 of `scripts/agentic_check.py`.

    `handouts` carries the files THIS turn produced, so the panel can show them
    without waiting for its poll. It is not the agent's full list -- that is
    `GET /api/agents/{id}/handouts`.
    """

    query_id: uuid.UUID
    conversation_id: uuid.UUID
    answer: str
    refused: bool
    latency_ms: int
    model_used: str
    rewritten_question: str | None = None
    citations: list[CitationOut]
    tool_steps: int = 0
    handouts: list[HandoutOut] = Field(default_factory=list)


class QueryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    question: str
    answer: str | None = None
    refused: bool
    latency_ms: int | None = None
    model_used: str | None = None
    # Null for the one-shot rows written before threads existed. Readers must
    # handle it -- see `models.Query.conversation_id`.
    conversation_id: uuid.UUID | None = None
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

def clean_question(raw: str) -> str:
    """The question as it will be stored, or a 422.

    `min_length=1` on the model catches the empty string; this catches "   ",
    which is a different request with the same outcome and would otherwise reach
    the embedder as a blank query.
    """
    question = raw.strip()
    if not question:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="question is required",
        )
    return question


def derive_conversation_title(question: str) -> str:
    """Name a thread after the question that started it.

    Deliberately no model call. A title is a handle in a sidebar, not a summary:
    the first question is what the user will recognise the thread by, it is
    already the thing they typed, and spending a generation round trip -- the
    part of a turn CLAUDE.md measures at 13.2 s, 89% of the total -- to
    paraphrase it would make starting a conversation the slowest thing in the app
    in exchange for a slightly tidier string.

    Whitespace is collapsed first because a pasted question arrives with newlines
    in it and a sidebar renders one line either way; truncation then breaks on a
    word boundary, but only if that boundary is past the halfway mark, so a
    single very long word is cut rather than reduced to an ellipsis.
    """
    collapsed = " ".join(question.split())
    if len(collapsed) <= TITLE_MAX_CHARS:
        return collapsed

    cut = collapsed[:TITLE_MAX_CHARS]
    boundary = cut.rfind(" ")
    if boundary >= TITLE_MAX_CHARS // 2:
        cut = cut[:boundary]
    # ASCII "...", not U+2026: this string is echoed by scripts that print to a
    # Windows console, where the codepage mangles non-ASCII (see CLAUDE.md).
    return cut.rstrip(" ,;:.-") + "..."


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


def _handout_kind(mime_type: str) -> str:
    """Map a produced file's MIME type onto the panel's four kinds.

    `handouts.kind` drives how a row renders -- a chart gets a thumbnail, a sheet
    gets its markdown inline -- so it is a presentation choice, not a second copy
    of the MIME type. Keeping it as a small explicit mapping rather than a suffix
    split means a `.svg` chart and a `.png` chart land in the same bucket, which
    is what a reader of the panel expects.

    The fallback is `"file"` rather than a guess. A kind the panel does not
    recognise renders as a plain downloadable row, which is a worse experience
    than a thumbnail and a much better one than a mislabelled thumbnail that
    fails to load.
    """
    if mime_type in ("image/png", "image/svg+xml"):
        return "chart"
    if mime_type.endswith("presentationml.presentation"):
        return "deck"
    if mime_type in ("text/csv", "application/json"):
        return "table"
    if mime_type in ("text/markdown", "text/plain"):
        return "sheet"
    return "file"


def normalise_citation_markers(answer: str, citations: Sequence[CitationOut]) -> str:
    """Rewrite the answer's brackets into `[n]` markers that resolve.

    **A dangling citation is worse than no citation.** A chip rendered from a
    marker with nothing behind it still looks like provenance -- the reader takes
    the claim as sourced, clicks, and gets nothing, having already believed it.
    An unmarked sentence at least presents itself as unsourced. So a bracket that
    cannot be resolved to a citation in this turn's list is removed from the text
    rather than shipped for the UI to fail on.

    Two input shapes are handled, because the generator's own prompt decides
    which arrives and that prompt lives in `app/rag/pipeline.py`:

    * `[3]` -- a number. Kept when a citation carries that marker, deleted when
      not. This is what a numbered context block produces.
    * `[3.1-lesson-gist.md]` -- a filename. `DEFAULT_SYSTEM_PROMPT` asks for
      exactly this today ("cite the source filename in brackets"), and
      `format_context` tags every chunk with its filename, so it is what Gemma
      actually emits. Mapped to the best-ranked citation from that file.

    Handling both is not indecision. The API contract promises `[n]` markers that
    index into `citations` by `marker`, and this function is the only place that
    promise can be kept from -- so it has to hold whether or not the prompt is
    ever changed to number its context blocks, and it must keep holding on the
    turn after such a change while a conversation still contains older answers.

    Anything else in brackets is left exactly as written. Markdown links,
    `[sic]`, code samples and checklists are not citations, and a function that
    deletes text it merely failed to understand is a worse bug than the one it
    was written to prevent.
    """
    valid = {c.marker for c in citations}

    # Best-ranked citation per source file, and per file stem so that a model
    # dropping the extension still resolves. `setdefault` because `citations` is
    # already in marker order: the first entry for a filename is its best-ranked
    # chunk, which is the one a claim attributed to "that file" should open.
    by_name: dict[str, int] = {}
    for citation in citations:
        name = citation.filename.strip().lower()
        by_name.setdefault(name, citation.marker)
        by_name.setdefault(name.rsplit(".", 1)[0], citation.marker)

    def resolve(token: str) -> int | None:
        token = token.strip()
        if not token:
            return None
        if token.isdigit():
            number = int(token)
            return number if number in valid else None
        return by_name.get(token.lower())

    def rewrite(match: re.Match[str]) -> str:
        raw = match.group(1)

        # `[label](url)` is a markdown link. Rewriting its label would break the
        # link and deleting it would leave a bare URL in parentheses.
        tail = match.end()
        if tail < len(answer) and answer[tail] == "(":
            return match.group(0)

        # The whole bracket first, so a filename containing a separator is not
        # torn in half by the split below.
        whole = resolve(raw)
        if whole is not None:
            return f"[{whole}]"

        tokens = [t for t in _MARKER_SEPARATORS.split(raw) if t.strip()]
        markers = [m for m in (resolve(t) for t in tokens) if m is not None]
        if markers:
            # `dict.fromkeys` dedupes while keeping the order the model chose --
            # "[2, 2, 5]" becomes "[2][5]", not "[2][2][5]".
            return "".join(f"[{m}]" for m in dict.fromkeys(markers))

        # Nothing resolved. Delete it only if it was unmistakably meant as a
        # citation -- every token a bare number. Anything else is prose.
        if tokens and all(t.strip().isdigit() for t in tokens):
            return ""
        return match.group(0)

    cleaned = _BRACKET_RE.sub(rewrite, answer)
    # Deleting a marker leaves the space that preceded it stranded, and a
    # sentence ending " ." reads as a typo in the answer rather than as a
    # citation that was withdrawn.
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"[ \t]+([.,;:!?])", r"\1", cleaned)
    return cleaned.strip()


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


async def _recent_history(db: AsyncSession, conversation_id: uuid.UUID) -> list[ChatTurn]:
    """The thread's last few turns, oldest first, for the rewriter.

    Capped in SQL rather than in Python. `pipeline.contextualize_question` slices
    to `HISTORY_TURNS` itself, so passing the whole thread would be correct --
    and would also drag every question and answer of a fifty-turn conversation
    across the wire on every follow-up, to discard all but six. The cap belongs
    where the rows are read.

    Newest-first with a LIMIT and then reversed, because "the last six" is not
    expressible as an ascending order. The `id` tie-break matters for the same
    reason it does in `list_queries`: `created_at` is a server-side now() and
    two turns can share one.

    Answers come back as `Query.answer`, which is nullable -- a turn that failed
    mid-generation still has a question worth resolving pronouns against, and
    `ChatTurn` types the answer optional for exactly that.
    """
    rows = await db.execute(
        select(Query.question, Query.answer)
        .where(Query.conversation_id == conversation_id)
        .order_by(Query.created_at.desc(), Query.id.desc())
        .limit(HISTORY_TURNS)
    )
    return [(r.question, r.answer) for r in reversed(rows.all())]


# --------------------------------------------------------------------------
# The turn engine
# --------------------------------------------------------------------------

async def run_turn(
    db: AsyncSession,
    *,
    agent: Agent,
    user: User,
    session: Session | None,
    conversation: Conversation,
    question: str,
) -> AskOut:
    """Answer one question inside one thread, and record everything about it.

    Commits. The caller hands over a `conversation` that is already in the
    session (flushed, so it has an id) and gets back a completed turn; every row
    this function writes lands in that one commit or none of them do.

    **The agent is a parameter, never derived from the request.** Both callers
    resolve it through an ownership check first -- `OwnedAgent` here,
    `conversations._conversation_agent` there -- and this function does no
    checking of its own precisely so that there is no second, weaker place for
    the rule to live. `agent.namespace` is what scopes retrieval, and PRD
    section 3.2 requires that scoping to be structural.
    """
    started = time.perf_counter()

    # History BEFORE the row for this turn exists. `Query.created_at` is a
    # server-side now() assigned at flush, so a placeholder added first would be
    # picked up by the query below as the newest "prior" turn -- handing the
    # rewriter the very question it is meant to be resolving, with a null answer,
    # and pushing a genuinely useful turn out past the cap.
    history = await _recent_history(db, conversation.id)

    # The `queries` row is created next, as a placeholder, and flushed. Two
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
        conversation_id=conversation.id,
        question=question,
    )
    db.add(query)
    await db.flush()

    trace = TraceRecorder(db, query.id)

    # ------------------------------------------------------------------
    # 1. The pipeline, once
    # ------------------------------------------------------------------
    # One call, and it returns its own evidence. This route used to run a second
    # scored retrieval alongside `answer_question` purely to recover the Pinecone
    # scores that a `BaseRetriever` has nowhere to put -- CLAUDE.md prices that
    # duplicate at embed 365 ms + Pinecone k=20 394 ms per question. `aretrieve`
    # keeps the scores and `AnswerResult` carries them out, so the trace below is
    # built from what actually happened rather than from a second search that
    # merely resembled it.
    #
    # That difference is not only cost. The old pair could disagree: two searches
    # against a namespace being written to concurrently can return different
    # candidates, and the RETRIEVE event would then describe a retrieval that
    # never fed the answer it sits next to.
    #
    # Not `run_in_threadpool`. `answer_question` is genuinely async down to an
    # asyncio Pinecone client, so the event loop stays free -- which matters
    # because Render runs a single uvicorn worker and a blocked loop queues every
    # other in-flight request behind a 13-second generation.
    result = await answer_question(agent, question, history=history)

    # ------------------------------------------------------------------
    # 2. REWRITE -- contextualisation, if it fired
    # ------------------------------------------------------------------
    # PRD section 4.3 lists REWRITE as an event type; this is its first producer.
    #
    # Recorded on `is not None`, NOT on the string having changed. A rewriter
    # that read six turns of history and concluded the question already stood on
    # its own made a decision, and a trace that shows nothing for it is
    # indistinguishable from a first turn where the rewriter was never called.
    # `changed` carries the difference for anyone who only wants the interesting
    # ones.
    #
    # This payload is also the only durable home for the rewrite. There is no
    # `queries.rewritten_question` column, so `conversations._load_messages`
    # reads `after` back out of here to fill `MessageOut.rewritten_question` --
    # which is why the key name is part of the contract, not an implementation
    # detail.
    if result.rewritten_question is not None:
        trace.record(
            REWRITE,
            payload={
                # Distinguishes this from the score-triggered rewrite PRD 3.5
                # specifies for Stage 2. Both will write REWRITE rows; only the
                # trigger says which machinery fired.
                "trigger": "conversation_history",
                "before": question,
                "after": result.rewritten_question,
                "changed": result.rewritten_question != question,
                "history_turns": len(history),
                "model": settings.decision_model,
            },
            duration_ms=result.contextualize_ms,
        )

    # ------------------------------------------------------------------
    # 3. RETRIEVE
    # ------------------------------------------------------------------
    similarity_by_chunk: dict[uuid.UUID, float] = {}
    ranked_before: list[str] = []
    for doc, score in result.scored:
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
    top_score = result.top_score

    trace.record(
        RETRIEVE,
        payload={
            "k": agent.retrieve_k,
            "returned": len(result.scored),
            "top_score": top_score,
            # What was embedded, which is the rewrite when there was one. A
            # RETRIEVE event showing the raw question next to scores produced by
            # a different string would misattribute every follow-up.
            "query": result.search_query,
            # The pre-rerank ordering. Half of the RERANK event's before/after
            # pair, and on its own the answer to "did the right chunk even come
            # back, or did the reranker never get a chance?"
            "chunk_ids": ranked_before,
            "scores": [round(float(s), 6) for _, s in result.scored],
        },
        score=top_score,
        duration_ms=result.retrieval_ms,
    )

    # ------------------------------------------------------------------
    # 4. Score check -- OBSERVABILITY ONLY
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

    # What this number DOES depends on whether the agent has tools, and saying so
    # is not decoration. A trace string asserting a feature does not exist, after
    # it does, is a documentation bug sitting in the most-read part of the
    # product -- and this particular string said "not built yet" for long enough
    # that it would be believed.
    #
    # Neither branch makes the threshold a branch in the code. With tools off,
    # nothing consumes it. With tools on, the model is shown the retrieved text
    # and decides for itself whether to search again, which is a strictly better
    # judge than this number: on-topic questions measured 0.61-0.67 here and
    # off-topic ones 0.49-0.58, so 0.5 sits INSIDE the overlap rather than
    # between the two populations.
    tools_on = result.tool_steps > 0 or (
        settings.agent_tools_enabled and bool(agent.tools_enabled)
    )
    trace.record(
        SCORE_CHECK,
        payload={
            "top_score": top_score,
            "threshold": agent.score_threshold,
            "below_threshold": below_threshold,
            "max_rewrites": agent.max_rewrites,
            "governs": "rewrite",
            "action": (
                "advisory -- the agent loop decides whether to search again"
                if tools_on
                else "none -- tools are off for this agent"
            ),
        },
        score=top_score,
    )

    # ------------------------------------------------------------------
    # 5. RERANK
    # ------------------------------------------------------------------
    final_ids = [cid for cid in (_chunk_uuid(d) for d in result.documents) if cid]

    # `result.reranked` reports whether reranking ACTUALLY ran, not whether it
    # was enabled -- an empty retrieval skips it, and a RERANK event whose before
    # and after lists are both empty describes nothing.
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
                "rerank_scores": result.rerank_scores,
                "promoted": [c for c in ranked_after if c not in survivors],
            },
            # No duration, deliberately. The Cohere call happens inside
            # `answer_question` and this route cannot time it separately; a
            # figure derived by subtraction would be a guess wearing a
            # measurement's clothes. CLAUDE.md puts the rerank hop at ~830 ms,
            # Singapore to US, if a rough number is wanted.
            duration_ms=None,
        )

    # ------------------------------------------------------------------
    # 5b. The tool loop, if one ran
    # ------------------------------------------------------------------
    # `pipeline.py` does not know this session exists and `TraceRecorder` is not
    # reachable from it, so the loop accumulates its activity as plain data on
    # `AnswerResult` and it becomes rows here. That is the same split the rest of
    # this function relies on -- the pipeline decides, the route records -- and
    # keeping it means a tool can be added without either module learning about
    # the other's concerns.
    #
    # Position matters. These sit after RETRIEVE and before GENERATE because that
    # is the order they happened in: the first retrieval seeded the context, the
    # tools amended it, and generation read the result. `step_index` is a plain
    # counter on the recorder, so inserting here needs no renumbering.
    #
    # TOOL_CALL is recorded for a FAILED call too, and separately from its error.
    # The arguments the model chose are the decision worth keeping whether or not
    # the call worked -- a program that raised is exactly the one someone reading
    # the trace wants to see.
    for invocation in result.tool_calls:
        trace.record(
            TOOL_CALL,
            payload={
                "step": invocation.step,
                "tool": invocation.tool,
                "call_id": invocation.call_id,
                # `_jsonable` stringifies anything it cannot serialise and never
                # raises, so a 4 KB Python program in `args["code"]` survives
                # intact -- which is what the Trace view renders on its own.
                "args": invocation.args,
            },
        )
        if invocation.ok:
            trace.record(
                TOOL_RESULT,
                payload={
                    "step": invocation.step,
                    "tool": invocation.tool,
                    "call_id": invocation.call_id,
                    "ok": True,
                    "summary": invocation.summary,
                    **invocation.detail,
                },
                duration_ms=invocation.duration_ms,
            )
        else:
            trace.record(
                TOOL_ERROR,
                payload={
                    "step": invocation.step,
                    "tool": invocation.tool,
                    "call_id": invocation.call_id,
                    "ok": False,
                    "error": invocation.error,
                    **invocation.detail,
                },
                duration_ms=invocation.duration_ms,
            )

    # ------------------------------------------------------------------
    # 6. query_chunks -- citations now, Ragas `contexts` in Stage 3
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
        # rank stays the position the chunk held in the model's context, which is
        # also what makes it usable as the citation `marker`. A gap is not a
        # defect: the answer's `[2]` for a chunk that vanished resolves to
        # nothing and is stripped by `normalise_citation_markers`, which is the
        # correct outcome. Renumbering would instead point `[2]` at a different
        # source and look perfectly fine doing it.
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
                marker=position,
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

    # The stored answer is the normalised one, not the raw generation. The text
    # and its citation list are read back together by the chat view, so an answer
    # persisted with markers that no longer resolve would render broken chips
    # forever after -- and unlike the live response, there is nothing left to
    # re-derive them from.
    answer = normalise_citation_markers(result.answer, citations)

    # ------------------------------------------------------------------
    # 6b. Handouts produced by the tool loop
    # ------------------------------------------------------------------
    # Written into THIS transaction, alongside the query and the trace. If the
    # turn rolls back so do its handouts, which is the only correct outcome: an
    # artefact attributed to an answer that was never stored is orphaned, and it
    # would be orphaned holding megabytes.
    #
    # Bytes go in Postgres rather than object storage. No bucket is provisioned
    # (PRD open item 10) and `handouts.content` is `deferred()`, so a list query
    # never pulls them. The sandbox has already refused anything over 5 MB per
    # file, so the ceiling is enforced before a row is ever built.
    handout_rows: list[Handout] = []
    for produced in result.artifacts:
        artifact = produced.artifact
        handout_rows.append(
            Handout(
                id=uuid.uuid4(),
                agent_id=agent.id,
                conversation_id=conversation.id,
                query_id=query.id,
                created_by_user_id=user.id,
                kind=_handout_kind(artifact.mime_type),
                # The model's own statement of what it was making. Asking for it
                # is why `run_python` takes a `purpose` argument at all -- a tool
                # that has to say what it is for writes better code, and the
                # answer doubles as the title a human reads in the panel.
                title=produced.title[:200] or "Generated file",
                filename=artifact.filename[:255],
                mime_type=artifact.mime_type[:128],
                byte_size=artifact.byte_size,
                content=artifact.content,
                # Stored and shown. For a product whose whole purpose is making a
                # pipeline inspectable, hiding the step that produced the output
                # would be the one place it stopped doing that -- and it is the
                # fastest way for a user to see why a chart is wrong.
                source_code=produced.source_code,
                origin="tool",
                status="ready",
                meta={"tool": "run_python", "step": produced.step},
            )
        )
    for row in handout_rows:
        db.add(row)

    # ------------------------------------------------------------------
    # 7. GENERATE
    # ------------------------------------------------------------------
    trace.record(
        GENERATE,
        payload={
            "model": result.model,
            "context_chunks": len(result.documents),
            "context_chars": sum(len(d.page_content) for d in result.documents),
            "answer_chars": len(answer),
            "citations": len(citations),
            "pipeline_latency_ms": result.latency_ms,
            # How much of the turn the model drove itself. `stopped_reason` is
            # null on a normal exit and "max_steps" when the budget ran out --
            # the second is the one worth seeing, because it means the answer was
            # forced rather than finished.
            "tool_steps": result.tool_steps,
            "stopped_reason": result.stopped_reason,
            "tool_ms": result.tool_ms,
            "handouts": len(handout_rows),
        },
        # Generation alone, now that the pipeline times its own phases. The
        # RETRIEVE and REWRITE events above carry the rest, so the three add up
        # to the turn instead of overlapping.
        duration_ms=result.generation_ms,
    )

    # ------------------------------------------------------------------
    # 8. Refusal, from the answer text
    # ------------------------------------------------------------------
    # Scanned on the normalised text, because that is what is stored and what a
    # reader will later compare against `refused`. Marker rewriting only touches
    # brackets, so it cannot create or destroy a refusal phrase.
    marker = detect_refusal(answer)
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
    # 9. Close the turn
    # ------------------------------------------------------------------
    # Wall time for the whole engine, not `result.latency_ms`. That figure covers
    # the pipeline call; this one covers what the user waited for, including the
    # history read and the citation join.
    latency_ms = int((time.perf_counter() - started) * 1000)

    query.answer = answer
    query.model_used = result.model
    query.latency_ms = latency_ms
    query.refused = refused

    # THE CONVERSATION ROW MUST BE WRITTEN, or the chat list never reorders.
    # `conversations.updated_at` is SQLAlchemy `onupdate`, not a database trigger
    # (see `models._updated_at`): inserting a `queries` row that points at this
    # conversation does not touch it. The sidebar sorts on `updated_at DESC`, so
    # without this assignment a thread would sink to the bottom of the list the
    # moment a newer thread was created and stay there however heavily it was
    # used.
    #
    # `func.now()` rather than a Python timestamp, to keep every timestamp in the
    # schema on the database's clock -- `created_at` is a server default, and a
    # thread whose `updated_at` predates its own `created_at` because two
    # machines disagree is a confusing row to debug. The cost is that the
    # attribute holds a SQL expression until it is refetched, so **do not read
    # `conversation.updated_at` after this without `db.refresh`**: on an async
    # session the implicit reload raises MissingGreenlet.
    if conversation.title is None:
        conversation.title = derive_conversation_title(question)
    conversation.updated_at = func.now()

    # The single commit. Query, chunks, trace, handouts and conversation land
    # together or not at all.
    await db.commit()

    # `handouts.created_at` is a server_default, and `expire_on_commit=False`
    # does not conjure a value that was never sent -- reading it during Pydantic
    # serialisation raises MissingGreenlet from inside the validator, which is a
    # traceback that names neither this line nor the column. Refresh only the
    # rows about to be serialised, and only when there are any; the common turn
    # produces none and pays nothing.
    for row in handout_rows:
        await db.refresh(row)

    return AskOut(
        query_id=query.id,
        conversation_id=conversation.id,
        answer=answer,
        refused=refused,
        latency_ms=latency_ms,
        model_used=result.model,
        rewritten_question=result.rewritten_question,
        citations=citations,
        tool_steps=result.tool_steps,
        # Built from the pending objects rather than re-read after the commit.
        # `expire_on_commit=False` keeps them readable, and every field
        # `HandoutOut` wants was set in Python -- `created_at` is the one
        # server_default here, so it is the one that needs the refresh below.
        handouts=[HandoutOut.model_validate(row) for row in handout_rows],
    )


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
    """Ask one question, starting a new thread for it.

    This is the "New chat" path, and it is the only route the chat UI uses to
    create a conversation -- `POST /api/agents/{id}/conversations` exists in the
    contract and is exercised by nothing, because a thread with no turns in it is
    a row the user never asked for. Hence `AskOut.conversation_id`: the client
    learns the id of the thread it just started from the answer to its first
    question, and every follow-up goes to
    `POST /api/conversations/{id}/ask` instead.

    No history is loaded, because there is none by construction -- the
    conversation is one flush old. `run_turn` reads it anyway rather than being
    told to skip it, so there is exactly one code path that decides what the
    rewriter sees.
    """
    question = clean_question(body.question)

    # Explicit id rather than waiting on the flush default: `queries.
    # conversation_id` needs it, and reading it off an unflushed instance would
    # be None. Titled here rather than in `run_turn` only so the row is complete
    # from its first INSERT.
    conversation = Conversation(
        id=uuid.uuid4(),
        agent_id=agent.id,
        user_id=user.id,
        title=derive_conversation_title(question),
    )
    db.add(conversation)
    await db.flush()

    return await run_turn(
        db,
        agent=agent,
        user=user,
        session=session,
        conversation=conversation,
        question=question,
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

    Flat and thread-agnostic: this is the query log, not the chat view. Rows
    carry `conversation_id` so a caller can group them, and it is null for the
    one-shot rows written before threads existed.

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

    **The ownership check is written out by hand here**, as it is for a
    conversation in `app/api/conversations.py` and for the same reason. Every
    agent-scoped route takes `OwnedAgent`, which binds to an `agent_id` path
    parameter and does the check in a dependency. This route is keyed on a query
    id and is deliberately not nested under an agent -- `/api/trace/{query_id}`
    is what the pinned contract says -- so there is no `agent_id` in the path for
    `owned_agent` to bind to and nothing for it to load. FastAPI would not fail;
    the dependency simply would not resolve.

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

    The chat view calls this once per message, on demand, so a twenty-turn thread
    can issue twenty of these. That is why the check is a single `db.get` on the
    primary key rather than a join.
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
