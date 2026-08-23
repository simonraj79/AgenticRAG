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

**SSE streaming lives in `app/api/stream.py`, and this route is unchanged by
it.** PRD section 2.2 specifies token-by-token transport; the two `/ask/stream`
routes over there provide it, and they run `run_turn` below rather than a second
copy of it. The old note here said streaming and durable recording pull in
opposite directions -- the row is only complete once the last token has arrived
-- and that is still exactly right, which is why the resolution was a transport
on the last model call and nothing else: `run_turn` gained one optional `emit`
parameter, and with it unset every branch from here down is the line it already
was. The recording half never learned that streaming exists.

Two routes rather than a `?stream=` flag on this one, for the same reason there
are already two ask endpoints over one engine: a flag would force
`response_model=AskOut` off this handler, deleting the validation that makes the
terminal `done` payload byte-identical to this route's body.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi import Query as QueryParam  # `Query` is the SQLAlchemy model below.
from langchain_core.documents import Document as LCDocument
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
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
from app.db.session import SessionLocal
from app.db.specialists import BY_SLUG, roster
from app.metering import store as metering_store
from app.metering.context import collect_usage, meter_as
from app.rag import events
from app.rag.pipeline import HISTORY_TURNS, ChatTurn, answer_question
from app.rag.refusal import detect_refusal
from app.rag.retriever import META_CHUNK_ID, RERANK_SCORE_KEY
from app.rag.route import parse_mentions
from app.rag.selfcheck import SIGNAL_PHANTOM, markers_in
from app.rag.trace import (
    DELEGATE,
    GENERATE,
    REFUSE,
    RERANK,
    RETRIEVE,
    REWRITE,
    ROUTE,
    SCORE_CHECK,
    SELF_CHECK,
    TOOL_CALL,
    TOOL_ERROR,
    TOOL_RESULT,
    TraceRecorder,
)

router = APIRouter(prefix="/api", tags=["ask"])

# **This module used `log` before it had one.** `log.exception(...)` at the
# artefact-storage failure path (section 8, "Could not store a tool artefact")
# has been a bare `NameError` waiting on that branch since it was written: the
# name resolves nowhere, so a bucket hiccup -- the one thing that handler exists
# to survive -- would have killed the whole turn instead of dropping one
# attachment. Found while wiring the failure-path seam below, which needs a
# logger of its own for exactly the same reason: a swallow that cannot say what
# it swallowed is a silence, not a guard.
log = logging.getLogger(__name__)

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

    `rewritten_question` is null when the rewrite produced nothing, and a string
    when it did -- **even if that string equals `question`.** Null and unchanged
    are different facts (`pipeline.contextualize_question` explains why), and
    since 2026-08-16 the rewriter runs on every turn, so a null here now means
    "the rewrite failed or was switched off" rather than "this was a first turn".

    `rewritten_changed` is what a client should actually render on. Once every
    turn carries a rewrite, a banner gated on `rewritten_question` being truthy
    fires on every message in every thread, usually quoting a sentence a word
    away from the one the user just typed -- which spends the single most useful
    explanatory affordance in the product on saying nothing.

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
    # Null on a turn where the rewriter was not asked at all; False when it read
    # the question and left it alone. Three states, because a client that
    # collapses the first two renders "Searched for" over an unrewritten turn.
    rewritten_changed: bool | None = None
    citations: list[CitationOut]
    tool_steps: int = 0
    # Round-TRIPS, where `tool_steps` counts ROUNDS. They were the same number
    # until 2026-08-16 and are not any more.
    #
    # `google/gemma-4-31b-it` emitted at most one call per step, so a client could
    # honestly render `tool_steps` as "searched twice". Measured on
    # `deepseek/deepseek-v4-flash-0731`, 8/8 steps emitted TWO `search_corpus`
    # calls, several of them near-duplicates -- so a turn that ran two retrievals
    # reported "searched once", understating the work by half.
    #
    # That matters here more than it would elsewhere: the product exists to make
    # the pipeline inspectable, and `max_tool_steps` is a slider a workshop
    # attendee is invited to tune. A student watching the step budget had no way
    # to see that three steps can be six retrievals.
    #
    # Defaults to 0 and is absent from every GENERATE payload written before this
    # landed, which `conversations.py` handles the same way it already handles
    # `tool_steps` -- see the backfill note there.
    tool_calls: int = 0
    handouts: list[HandoutOut] = Field(default_factory=list)

    # Which teaching voice answered, and how that was decided. All four default
    # to the classic value, so an agent with `specialists IS NULL` serialises
    # exactly as it did before this feature -- the same property `tool_steps` and
    # `handouts` above carry, one feature later.
    #
    # `specialist` is the primary and `specialists` is every voice that answered;
    # a two-mention turn has both, and the message chip renders the first while
    # the section headings show the rest.
    specialist: str | None = None
    specialists: list[str] = Field(default_factory=list)
    # "router" | "mention" | "fallback". The client shows the pill differently
    # for a mention (the user chose) than for a route (the agent chose), and
    # `fallback` means neither happened -- the router failed and the agent's own
    # prompt answered.
    route_trigger: str | None = None
    # Null on every turn where the free pre-check did not fire, which is almost
    # all of them. "ungrounded" is the amber chip: the check found unsupported
    # claims and there was no budget left to redraft, so the answer stands and
    # says so rather than being edited.
    self_check_verdict: str | None = None


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


def _delegated_sections(answer: str, slugs: Sequence[str]) -> list[tuple[str, str]]:
    """(slug, body) for each `## heading` section, in the order they appear.

    **Reconstructed from the answer rather than carried out of the pipeline**,
    and that is a deliberate limit rather than an omission. `AnswerResult` names
    eleven routing fields and a twelfth carrying per-section text would put the
    whole answer in the object twice; the headings are written by
    `pipeline.answer_question` from `Specialist.heading`, so the split is
    deterministic against a constant this module can read.

    What that buys is a DELEGATE payload with real `answer_chars` and `markers`
    per section -- the difference between "two specialists were asked" and "two
    specialists each said something, citing these passages". A section the model
    returned empty is dropped by the pipeline, so it simply has no heading here
    and no row, which is the honest rendering.

    Split on the STORED answer, after `normalise_citation_markers`, so `markers`
    is the set a reader can actually click.
    """
    found: list[tuple[int, str, int]] = []
    for slug in slugs:
        specialist = BY_SLUG.get(slug)
        if specialist is None:
            continue
        needle = f"## {specialist.heading}"
        start = answer.find(needle)
        if start < 0:
            continue
        found.append((start, slug, start + len(needle)))

    # Position order, not mention order: they agree today, and sorting is what
    # makes the "everything up to the next heading" slice below correct even if
    # they ever stop agreeing.
    found.sort()
    sections: list[tuple[str, str]] = []
    for index, (_, slug, body_start) in enumerate(found):
        end = found[index + 1][0] if index + 1 < len(found) else len(answer)
        sections.append((slug, answer[body_start:end].strip()))
    return sections


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
# A TURN THAT RAISES USED TO DISCARD THE SPEND IT HAD ALREADY PAID FOR, and the
# loss was silent in the strongest sense this repository has a name for.
#
# Measured shape of one ordinary turn (CLAUDE.md, 2026-08-20, a two-document
# agent): a rewrite, THREE embeddings, THREE reranks and TWO generation calls --
# nine billable calls, $0.00049354. Every one of those records is buffered by
# `collect_usage` and was drained in exactly one place, `_run_turn`'s
# `persist_quietly` call, which only `db.add()`s; the commit that makes those
# rows real is fifteen lines further on. So any raise between the `queries`
# flush and that commit threw the whole buffer away, and a raise AT the commit
# rolled back rows that had already been added.
#
# There was no log line either, which is what makes this `loop.md` T2 rather
# than an ordinary leak. `LoggingSink.record` (`meter.py:124-136`) logs only
# when NO collection is active -- it returns the moment `emit_record` accepts
# the record into the turn's bucket. A turn with a bucket open therefore
# produced neither a row nor a line. The spend was not unattributed; it was
# ABSENT. There is no error anywhere to trigger on, only an outcome that did
# not happen.
#
# The failure this makes visible is the one an operator most needs to see, and
# CLAUDE.md records this project hitting it three separate times: an OpenRouter
# `404 No endpoints found` storm, where every turn fails and the console reads
# as a quiet week.
#
# Nor could the admin console reveal the hole on its own. Coverage there is
# `distinct(api_usage.query_id) / count(queries)`, and on a failed turn both
# sides disappear together -- the `queries` row rolls back with the usage rows,
# so the ratio stays flat while the money leaves. A measurement can only report
# a denominator it still has.
#
# `scripts/metering_check.py` cases 13, 13b, 13c, 13d, 14a-14e, 15, 16, 16b, 17a
# and 17b pin the fix; D1/D2/D3 execute it against the real schema and D4 proves
# the harness left nothing behind. The ones added after the first review found
# them unpinned: 14d (rows added, then a FAILING commit), 14e (an empty buffer
# opens no connection), 16 (the receipt is written where the commit is) and
# 17a/17b (a cancellation is contained, and re-armed).


@dataclass
class _TurnReceipt:
    """What the turn managed to make DURABLE. Two facts, two writers.

    `_run_turn` writes both -- `rows_written` where it persists, `committed`
    where its single commit returns -- and `_persist_orphaned_usage` reads them.
    Nothing else touches it.

    **Two fields rather than one boolean, because there are FOUR states and a
    boolean reaches only two.** `store.persist_quietly` returns `(None, None)`
    both when it had nothing to write and when it raised and was swallowed, so
    "the commit returned" alone would mark a buffer durable that was never
    written -- reproducing the original hole THROUGH the fix:

        committed  rows_written  means                                seam writes
        False      None          the turn raised at or before persist N rows
        True       N             the ordinary successful turn         zero
        True       None          persist raised, then commit returned N rows
        False      N             persist added rows, then the COMMIT   N rows
                                 RAISED and rolled them back

    The third row is the one no plain boolean can reach; the fourth is the case
    the feature was built for (PLAN section 3.5's headline: rows added, then
    discarded by a failing commit) and an earlier version of this table omitted
    it while calling itself exhaustive. `metering_check.py` case 14c drives the
    first three in one case and 14d drives the fourth -- separately, because 14d
    is the only one of the four that goes red when `durable` drops `committed`.
    """

    rows_written: int | None = None
    committed: bool = False

    @property
    def durable(self) -> bool:
        """Both facts, or neither counts. Dropping either is silent, differently.

        Drop `committed` and the seam declines to write on a FAILING commit --
        the one failure it was built for, and the fourth row above. Drop
        `rows_written` and a swallowed persist followed by a good commit marks
        the buffer durable having written nothing.

        Both halves are pinned, and they needed different cases: 14c goes red
        without `rows_written`, 14d goes red without `committed`. That
        asymmetry is why the fourth state is driven separately rather than
        folded into 14c, which stays green under the `committed` mutation.
        """
        return self.committed and bool(self.rows_written)


async def _persist_orphaned_usage(records: list, receipt: _TurnReceipt) -> int:
    """A failed turn's spend, written from a SECOND session. Returns rows written.

    **The guard lives here, in the seam, not only at the call site**, and it is
    the whole feature. A `finally` that fires on the success path too records
    every normal turn's spend twice, and this repository has had that exact bug
    once already: `collect_usage`'s docstring in `app/metering/context.py` is
    the record of the eval-job / `run_turn` double-count -- roughly double the
    real spend, no error anywhere, and both totals plausible, which is what
    would have let it survive. `metering_check.py` case 13 is passed PERFECTLY by a `finally`
    that always persists and cannot see this at all; case 14c is what does.

    **It returns without opening a session on the durable path**, and that is a
    different claim from "writes no rows" -- 14c asserts `sessions_opened == 0`
    as well as `rows == 0`. A seam that opened a connection and added nothing
    would still pay the cost below on EVERY successful turn, which is the one
    path where the extra connection is never justified.

    **Why a second session at all.** The turn's session is being rolled back --
    that is the premise -- so writing through it writes nothing. The four
    precedents in this repository do the same thing for the same reason
    (`rag/jobs.py:230`, `eval/jobs.py:299`, `handouts/jobs.py:947`,
    `api/eval.py:1086`).

    **Their connection-cost reasoning does NOT transfer, and copying the fourth
    precedent unread is the trap.** All four close their own `async with
    SessionLocal()` BEFORE their `finally` runs, so their second session is
    never concurrent with their first. `run_turn`'s callers hold the turn
    session open across this call -- the FastAPI dependency in
    `ask.py`/`conversations.py`, and `stream.py`'s own `async with`. Against
    `pool_size=5, max_overflow=5` on a single uvicorn worker, with the 404 storm
    above being precisely the moment every turn wants the extra connection at
    once. No harness can prove a pool property, so the mitigation IS this code:
    the function guards on `records` and awaits nothing else, so the second
    connection is opened only when there is something to write and is held for
    one insert and one commit. `metering_check.py` case 14e executes that guard
    with an EMPTY buffer, on both non-durable receipts -- a turn that died before
    its first model call is the ordinary shape during the 404 storm this is
    written for. Every case before 14e passed three records, so the clause was
    never run by anything.

    **`query_id` is deliberately not passed, and that omission is measured
    rather than argued.** The `queries` row was flushed inside the transaction
    that just died, so naming its id from a surviving session is a
    referential-integrity violation -- `metering_check.py --db` case D2 executes
    that insert and watches the database refuse it. The column is nullable
    precisely so an unattributed row is legal: losing the attribution must never
    mean losing the cost. No `meter_as` site in the repo sets
    `UsageRecord.query_id` (case 15 asserts that too), so `store.to_row` leaves
    the column NULL for free.

    **Nothing escapes this function, and `except Exception` was not enough to
    say so.** It runs in a `finally` outside `_run_turn`'s commit handler, whose
    `except` deletes staged storage keys and then RE-RAISES the original error.
    An exception thrown from here would replace that error with an accounting
    one, and the caller would be told the meter broke rather than what actually
    killed the turn. Same asymmetry as `UsageMeter.on_llm_end`: if the
    accounting is broken, the accounting is what should fail.

    The claim was FALSE for one exception and it is the one that matters:
    `asyncio.CancelledError` derives from BaseException, not Exception (verified
    on this venv, Python 3.12.10), so a uvicorn shutdown or `--reload` landing
    inside the seam's single DB round trip replaced a real
    `404 No endpoints found` with `CancelledError` -- measured by driving the
    seam with a session factory that raises it. Hence `except BaseException`.

    **A swallowed cancellation is a different bug, so it is RE-ARMED rather than
    eaten.** `asyncio.current_task().cancelling()` is non-zero only when a
    cancel was genuinely requested (Python 3.11+), which separates "the server
    is going away" from a driver raising `CancelledError` spuriously; in the
    first case the cancel is requested again on the way out, so it is delivered
    at the caller's next await instead of here, where it would be the accounting
    replacing the turn's own error. `metering_check.py` case 17a asserts the
    containment and 17b asserts the re-arm -- separately, because a build that
    swallowed cancellation entirely would pass 17a alone.
    """
    if not records or receipt.durable:
        return 0
    try:
        async with SessionLocal() as meter_db:
            metering_store.persist(meter_db, records)
            await meter_db.commit()
        # INFO, not DEBUG. This line is the only surviving evidence that money
        # was spent on a turn that produced nothing else -- no answer, no
        # `queries` row, no trace -- so it is what an operator greps for when
        # the console shows a quiet week and the OpenRouter bill does not.
        log.info(
            "recovered %s usage record(s) from a turn that did not commit; "
            "written unattributed (query_id NULL)",
            len(records),
        )
        return len(records)
    except BaseException as exc:  # noqa: BLE001 -- accounting never replaces the real error
        log.warning(
            "could not persist the usage of a failed turn; %s record(s) lost",
            len(records),
            exc_info=True,
        )
        # See the docstring: contained here so the turn's own error survives,
        # re-requested so a real shutdown is not silently declined. Anything
        # else BaseException-shaped (KeyboardInterrupt, SystemExit) is swallowed
        # too and that is the same trade, taken deliberately: this frame owes
        # the caller the error that killed the turn, and it holds one insert and
        # one commit.
        if isinstance(exc, asyncio.CancelledError):
            task = asyncio.current_task()
            # `Task.cancelling()` is 3.11+; this venv is 3.12.10 and nothing in
            # the repo pins a floor, so it is read defensively. An
            # AttributeError raised HERE, inside the handler, would escape and
            # do the exact thing this handler exists to prevent. Absent, the
            # conservative reading is "no cancel was requested": a swallowed
            # cancellation delays a shutdown by one turn, where cancelling a
            # task nobody cancelled kills a healthy answer.
            cancelling = getattr(task, "cancelling", None)
            if task is not None and callable(cancelling) and cancelling():
                task.cancel()
        return 0


async def run_turn(
    db: AsyncSession,
    *,
    agent: Agent,
    user: User,
    session: Session | None,
    conversation: Conversation,
    question: str,
    rewrite: bool | None = None,
    emit: events.Emit | None = None,
) -> AskOut:
    """`_run_turn`, with every model call it makes attributed and buffered.

    **A wrapper rather than a `with` block inside the engine**, because the
    engine has several returns and a `finally` that must not grow a second job.
    This shape also makes the off switch total: with metering disabled the two
    context managers are still entered, the sink still finds an empty collection,
    and nothing is written -- but `_run_turn` itself is untouched either way, so
    "the classic path is byte-identical" stays a structural claim rather than a
    careful one.

    The scope names `generation` as the kind. Subsystems that make a DIFFERENT
    kind of call open their own nested `meter_as` -- the rewriter, the router and
    the critic each do -- and those inherit the user and agent set here, which is
    the entire reason `meter_as` merges rather than replaces.

    `query_id` is deliberately NOT set here: the row does not exist yet.
    `store.persist` stamps it, and `app/metering/store.py` says why.

    **The `finally` is the fifth of five `collect_usage()` sites to grow one**,
    and it was the last because this is the only one whose happy path already
    persisted -- which made the hole look like it was already plugged.
    `metering_check.py` case 13 derives that denominator from the source rather
    than from a list of filenames, so a sixth site added without a `finally`
    goes red on the day it lands.

    `records` is bound to an empty list BEFORE the `with`, so the `finally` has
    something to read even if `collect_usage()` or `meter_as()` raises on entry.
    The name is rebound to the bucket by the `with`, and the bucket outlives the
    block: `collect_usage`'s own `finally` resets a ContextVar, it does not
    empty the list.
    """
    records: list = []
    receipt = _TurnReceipt()
    try:
        with collect_usage() as records, meter_as(
            user_id=user.id, agent_id=agent.id, call_kind="generation"
        ):
            return await _run_turn(
                db,
                agent=agent,
                user=user,
                session=session,
                conversation=conversation,
                question=question,
                rewrite=rewrite,
                emit=emit,
                usage_records=records,
                receipt=receipt,
            )
    finally:
        # Reads the receipt and does nothing on an ordinary turn. See
        # `_persist_orphaned_usage` for why the guard is in the seam and not
        # here: a `finally` that always persists passes case 13 perfectly and
        # doubles the console.
        await _persist_orphaned_usage(records, receipt)


async def _run_turn(
    db: AsyncSession,
    *,
    agent: Agent,
    user: User,
    session: Session | None,
    conversation: Conversation,
    question: str,
    rewrite: bool | None = None,
    emit: events.Emit | None = None,
    usage_records: list | None = None,
    receipt: _TurnReceipt | None = None,
) -> AskOut:
    """Answer one question inside one thread, and record everything about it.

    `rewrite` is threaded straight through to `pipeline.answer_question` and
    `None` means "use `settings.rewrite_every_turn`". The one caller that passes
    it is `app/eval/jobs.py`, which needs its golden questions embedded verbatim
    or every EVAL.md baseline stops being comparable -- see the note there.

    Commits. The caller hands over a `conversation` that is already in the
    session (flushed, so it has an id) and gets back a completed turn; every row
    this function writes lands in that one commit or none of them do.

    **The agent is a parameter, never derived from the request.** Both callers
    resolve it through an ownership check first -- `OwnedAgent` here,
    `conversations._conversation_agent` there -- and this function does no
    checking of its own precisely so that there is no second, weaker place for
    the rule to live. `agent.namespace` is what scopes retrieval, and PRD
    section 3.2 requires that scoping to be structural.

    **`emit` streams progress; it changes nothing about what is recorded.** With
    it set, this function emits exactly one frame of its own -- `start`, below,
    the moment `query.id` exists -- and hands the rest to `answer_question`.
    Every write, the single commit, and the order of both are untouched, because
    the durable half of a turn is the half nobody watches in a browser and is
    therefore the half that must not acquire a second code path. Scenario S1 of
    `scripts/agentic_check.py` calls this function directly with no `emit` and
    asserts the resulting trace rows; that assertion holds structurally, since
    with `emit is None` every branch below and beneath is the line it already was.
    """
    started = time.perf_counter()

    # Two assignments below write to this and nothing else reads it here, so an
    # absent one is a receipt nobody collects rather than a branch. That keeps
    # the two writes unconditional, which matters: a receipt updated inside an
    # `if` is a receipt that can be silently skipped, and the whole guard in
    # `_persist_orphaned_usage` keys on those two fields being honest.
    receipt = receipt or _TurnReceipt()

    # History BEFORE the row for this turn exists. `Query.created_at` is a
    # server-side now() assigned at flush, so a placeholder added first would be
    # picked up by the query below as the newest "prior" turn -- handing the
    # rewriter the very question it is meant to be resolving, with a null answer,
    # and pushing a genuinely useful turn out past the cap.
    history = await _recent_history(db, conversation.id)

    # ------------------------------------------------------------------
    # 0. `@mentions`, parsed HERE and not in the pipeline
    # ------------------------------------------------------------------
    # **The raw text is stored and the stripped text is what runs.** Those are
    # deliberately two strings. `queries.question` has to show what the user
    # typed, or the thread renders a message they did not send; and `@feynman`
    # means nothing in vector space, reaching a rewriter documented to mangle
    # terms it does not recognise. Parsing inside `answer_question` would mean
    # this function stored one string while the pipeline ran another, with
    # nothing forcing them to be the same one.
    #
    # `roster(agent.specialists)` is empty for a non-orchestrator, and
    # `parse_mentions` then returns the text untouched -- so `@risk` in an
    # ordinary question stays literal text on every agent, and on an orchestrator
    # only slugs and aliases in ITS OWN roster match. There is no path here
    # through which retrieved text could name a specialist: the roster comes off
    # the agent row, and an unmatched token is left alone rather than guessed at.
    mention = parse_mentions(question, roster(agent.specialists))

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
        # RAW, including any `@mention`. See the parse above.
        question=question,
    )
    db.add(query)
    await db.flush()

    # The first frame, and the only one this function emits itself. It goes out
    # here rather than at the end because `query.id` exists from this line and
    # `conversation.id` was flushed by the caller -- so a streaming client learns
    # the identity of the turn it is watching BEFORE the pipeline runs, instead
    # of at `done` the way the JSON route forces. That is what lets a new thread
    # be promoted in the sidebar immediately, and what gives a stopped turn a key
    # to keep its partial text under.
    if emit is not None:
        await emit(
            events.START,
            {"query_id": str(query.id), "conversation_id": str(conversation.id)},
        )

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
    result = await answer_question(
        agent,
        # STRIPPED. `result.question` carries this same string back, which is
        # what everything below compares the rewrite against -- comparing
        # against the raw text would report every mentioned turn as "rewritten"
        # merely because the sigil is gone.
        mention.question,
        history=history,
        rewrite=rewrite,
        emit=emit,
        mention_specialists=mention.specialists,
    )

    # ------------------------------------------------------------------
    # 1b. ROUTE -- which voice answered, and who decided
    # ------------------------------------------------------------------
    # Recorded on every orchestrator turn, including the one where a mention made
    # the choice and no model ran. `trigger` is what separates the three, and
    # they are three genuinely different facts: the router chose, the user chose,
    # or the router failed and the agent's own prompt answered. The last is
    # indistinguishable from the first by reading the answer, and needs the
    # opposite response.
    #
    # ONE row even for a two-mention turn -- `specialists` is a list beside
    # `specialist` -- because that is one decision with two outcomes, and two
    # rows would claim the router ran twice.
    #
    # First, before REWRITE, because routing reads the RAW question: the signal
    # it goes on ("explain", "quiz me") is a property of how the user asked, and
    # normalising a question into a search query is exactly what flattens it.
    if result.route_trigger is not None:
        trace.record(
            ROUTE,
            payload={
                "trigger": result.route_trigger,
                "specialist": result.specialist,
                "specialists": result.specialists,
                "why": result.route_why,
                # Null on a mention: no model was asked. Naming one anyway would
                # credit a decision the user made to a call that never happened.
                "model": (
                    settings.decision_model
                    if result.route_trigger == "router"
                    else None
                ),
                # What the agent could have chosen from, so a trace read months
                # later does not depend on the roster still being what it was.
                "roster": list(agent.specialists or []),
                "failed": result.route_failed,
            },
            duration_ms=result.route_ms,
        )

    # ------------------------------------------------------------------
    # 2. REWRITE -- contextualisation, if it fired
    # ------------------------------------------------------------------
    # PRD section 4.3 lists REWRITE as an event type; this is its first producer.
    #
    # **Recorded on `rewrite_attempted`, not on `rewritten_question is not
    # None`.** Those were the same condition until first turns started being
    # rewritten; now they differ on exactly the turn worth seeing, the one where
    # the rewriter was called and came back with nothing. A rewriter that read
    # six turns of history and concluded the question already stood on its own
    # made a decision too, and a trace showing nothing for it is
    # indistinguishable from a turn where it was never called. `changed` carries
    # the difference for anyone who only wants the interesting ones.
    #
    # This payload is also the only durable home for the rewrite. There is no
    # `queries.rewritten_question` column, so `conversations._load_messages`
    # reads `after` back out of here to fill `MessageOut.rewritten_question` --
    # which is why the key names are part of the contract, not implementation
    # details.
    if result.rewrite_attempted:
        rewritten = result.rewritten_question
        trace.record(
            REWRITE,
            payload={
                # Was hardcoded "conversation_history", which mislabels every
                # first-turn rewrite. Still distinguishes this machinery from
                # the score-triggered rewrite PRD 3.5 specifies for Stage 2 --
                # that will write REWRITE rows too, and only the trigger says
                # which one fired.
                "trigger": "conversation_history" if history else "first_turn",
                # `result.question` and not the raw `question`: this field is
                # what the rewriter was HANDED, and on a mentioned turn that is
                # the stripped text. Identical to `question` on every turn
                # without a mention, which is every turn on a non-orchestrator.
                "before": result.question,
                "after": rewritten,
                "changed": rewritten is not None and rewritten != result.question,
                # **The new key, and the reason it exists.** With the rewriter on
                # every turn a failure is no longer "it did not run" -- it is a
                # silently worse search on a turn that expected help.
                # `contextualize_question` swallows every exception by design, so
                # without this the degradation is invisible in the trace as well
                # as in the logs.
                "failed": rewritten is None,
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
    # **That guarantee was FREE and is now bought.** It held because bytes and
    # row were one write: rolling back un-wrote both because there was only one
    # thing to un-write. An R2 put is not in the transaction, so the pair is
    # re-established by ordering instead -- object first, row second, and a
    # best-effort delete on the way out if the turn never commits. The keys are
    # collected in `staged_keys` for exactly that.
    #
    # The bytes are ALSO still written to `content`, keeping
    # `storage_route=postgres` a real rollback until a later change set drops the
    # column. The sandbox has already refused anything over 5 MB per file, so the
    # ceiling is enforced before a row is ever built.
    handout_rows: list[Handout] = []
    staged_keys: list[str] = []
    for produced in result.artifacts:
        artifact = produced.artifact
        handout_id = uuid.uuid4()

        # Generated here rather than left to the column default, because the key
        # must exist before the object does and the object must exist before the
        # row is added. Three things in a fixed order, from one id.
        storage_key: str | None = None
        if storage.enabled():
            storage_key = storage.handout_key(agent.id, handout_id, artifact.mime_type)
            try:
                await asyncio.to_thread(
                    storage.put_object, storage_key, artifact.content, artifact.mime_type
                )
                staged_keys.append(storage_key)
            except storage.StorageError:
                # The turn does NOT fail because a file could not be stored. The
                # answer is written, the tool ran, and the user is owed both --
                # losing a whole grounded answer to a bucket hiccup would be a
                # far worse outcome than losing the attachment. The row is
                # skipped rather than written keyless, so nothing claims to be a
                # downloadable handout that is not one.
                log.exception(
                    "Could not store a tool artefact for conversation %s; "
                    "the answer is kept and the file is dropped",
                    conversation.id,
                )
                continue

        handout_rows.append(
            Handout(
                id=handout_id,
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
                storage_key=storage_key,
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
            # Rounds and round-trips diverged when the generation model started
            # emitting several calls per step -- see `AskOut.tool_calls`. Recorded
            # separately rather than derived, because `conversations.py` rebuilds
            # a turn's summary from this payload alone and counting TOOL_CALL rows
            # there would make the two views disagree on old turns.
            "tool_calls": len(result.tool_calls),
            "stopped_reason": result.stopped_reason,
            "tool_ms": result.tool_ms,
            "handouts": len(handout_rows),
            # **The only schema change streaming makes anywhere, and it is one
            # optional key in a JSONB payload.** No migration (`event_type` is
            # String(32), `payload` is JSONB) and no new `EVENT_TYPES` member --
            # SSE frames are transport, not decisions, and a STREAM event type
            # would put the wire protocol in the decision log permanently.
            #
            # It is worth recording all the same, because chunk accumulation is a
            # genuinely different reconstruction path for the answer string: a
            # future disagreement between a streamed answer and a non-streamed one
            # is answerable from the row rather than by guessing which route wrote
            # it. Absent rather than `false` on the non-streaming path, so a
            # GENERATE payload written by the classic route is byte-identical to
            # every one already in the database.
            **({"streamed": True} if emit is not None else {}),
        },
        # Generation alone, now that the pipeline times its own phases. The
        # RETRIEVE and REWRITE events above carry the rest, so the three add up
        # to the turn instead of overlapping.
        duration_ms=result.generation_ms,
    )

    # ------------------------------------------------------------------
    # 7b. DELEGATE -- one row per section, when more than one voice answered
    # ------------------------------------------------------------------
    # GENERATE above summarises the whole generation stage; these detail it. A
    # single-specialist turn writes none: there is nothing to distinguish from
    # the answer itself, and a DELEGATE row per turn would make "did work get
    # handed to a second voice?" unanswerable without arithmetic.
    #
    # `markers` is the real assertion here. Two sections drafted against ONE
    # ledger means `[3]` refers to the same chunk in both, and that shared
    # numbering is the only reason concatenating them without a synthesiser call
    # is safe. A trace that recorded only `answer_chars` could not show it.
    if len(result.specialists) > 1:
        for slug, body in _delegated_sections(answer, result.specialists):
            trace.record(
                DELEGATE,
                payload={
                    "specialist": slug,
                    # The only producer today. `"tool"` is reserved for
                    # `consult_specialist`, which is Phase 4 and may never be
                    # built -- it ships only if these traces show turns that
                    # wanted a second lens.
                    "source": "mention",
                    # Null for the same reason: a mention carries no task. The
                    # key is present so a `consult_specialist` row and a mention
                    # row have one shape.
                    "task": None,
                    "answer_chars": len(body),
                    "markers": sorted(markers_in(body)),
                },
            )

    # ------------------------------------------------------------------
    # 7c. SELF_CHECK -- only when the free pre-check fired
    # ------------------------------------------------------------------
    # Absent from the overwhelming majority of turns, and that absence is the
    # design rather than a gap: the pre-check is a set operation against the
    # ledger's own marker range, so a grounded answer costs no model call, no
    # row and no event.
    #
    # `acted` is the field that stops two very different outcomes rendering
    # identically -- "we checked and it was fine" against "we checked, it was
    # not, and the budget was spent". CLAUDE.md records the cost of collapsing
    # that distinction once already, where `METRIC_TIMEOUT_S` made a hang and a
    # rate limit print the same string and they needed opposite fixes.
    if result.self_check_signal is not None:
        trace.record(
            SELF_CHECK,
            payload={
                "signal": result.self_check_signal,
                "verdict": result.self_check_verdict,
                "unsupported": result.self_check_unsupported,
                # Not carried on `AnswerResult`, which the plan pins to eleven
                # named fields. The signal name already says a citation was
                # invented; this key stays so a `consult_specialist`-era payload
                # and this one have one shape.
                "suggested_query": None,
                # Recomputed, and ONLY when the draft survived. `acted` true
                # means the text below is the redraft, so its markers are the
                # corrected ones and reporting them here would say the phantom
                # was never there. Null is the honest value in that case.
                "phantom_markers": (
                    sorted(markers_in(answer) - {c.marker for c in citations})
                    if result.self_check_signal == SIGNAL_PHANTOM
                    and not result.self_check_acted
                    else None
                ),
                "ledger_size": len(result.documents),
                "acted": result.self_check_acted,
            },
            duration_ms=result.self_check_ms,
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

    # ------------------------------------------------------------------
    # 9b. What the turn cost
    # ------------------------------------------------------------------
    # Every model call this turn made -- generation, the rewrite, the route, the
    # critic, and each step of the agent loop -- buffered by the sink and written
    # here as one batch, INSIDE the commit below. That is what makes
    # `queries.prompt_tokens` a trustworthy cache of the `api_usage` sum rather
    # than a second source of truth that can drift: either both land or neither
    # does.
    #
    # `api_usage` is authoritative and the two columns here are the cache. The
    # admin console reads `api_usage`; these exist for the per-turn UI and for
    # cheap sorting, and they have been NULL on every row written before this
    # feature -- which the console renders as "not measured", never as zero.
    #
    # **`persist` inside a `try` here, rather than `persist_quietly`, and the
    # duplication is deliberate.** The failure-path seam has to know whether
    # this write ADDED ROWS, and `persist_quietly`'s return value cannot say:
    # it answers `(None, None)` when there was nothing to write, when it raised
    # and swallowed, AND on a perfectly good write of records that carry no
    # token counts -- a rerank-only turn is exactly that shape. Reading a token
    # sum as a row count would mark such a turn's buffer durable when it was
    # not, or not durable when it was, and the second of those is the
    # double-count `collect_usage`'s docstring in `context.py` records. So the
    # count is taken from the records handed over (`persist` writes one row per
    # record and filters none), and the swallow is repeated here rather than
    # inferred.
    #
    # `new features/15-failure-paths/02-failed-turn-metering.md` section 6(b)
    # names the alternative and PREFERS it: widen `store.persist_quietly`'s
    # return so the count comes from the writer. That edit belongs to
    # `app/metering/store.py`, which this change set does not own, so what ships
    # here is the route 6(b) argues against -- disclosed, not chosen. If it is
    # ever made, collapse these six lines back to one call: cases 14a/14b/14c/14d
    # drive the seam through an explicit receipt and are independent of which
    # side counts, which is why the guard's behaviour is pinned either way.
    #
    # **The live cost of the deviation, stated so it is not rediscovered:
    # `store.persist_quietly` now has ZERO call sites** (`grep -rn
    # persist_quietly backend/app` finds only its definition and these comments)
    # and its swallow lives here instead. The log line below therefore does NOT
    # repeat store.py's -- one message, one call site, so an operator grepping a
    # warning lands on the code that emitted it. `metering_check.py` case 13c
    # asserts the two strings stay distinct, and case 13's drain names are
    # derived from store.py rather than restated, so deleting the dead function
    # is a one-line change over there that costs this harness nothing.
    usage_records = usage_records or []
    try:
        query.prompt_tokens, query.completion_tokens = metering_store.persist(
            db, usage_records, query_id=query.id
        )
        receipt.rows_written = len(usage_records)
    except Exception:  # noqa: BLE001 -- accounting never costs the user an answer
        # Same asymmetry as `persist_quietly`, which this replaces: this runs
        # inside the caller's transaction, so a raise here would roll back the
        # ANSWER. `receipt.rows_written` stays None, which is the third receipt
        # state -- the seam will write the buffer from its own session even
        # though the commit below is about to succeed.
        log.warning(
            "could not persist this turn's usage inside its transaction; "
            "the failure-path seam will write it unattributed",
            exc_info=True,
        )

    # The single commit. Query, chunks, trace, handouts and conversation land
    # together or not at all.
    #
    # The `except` is the other half of section 6b's ordering, and it is the only
    # thing standing between a failed commit and a permanently unreachable
    # object. The key was derived from a `handout_id` that exists nowhere except
    # in a transaction about to be discarded, so nothing -- no row, no listing,
    # no future request -- could ever name that object again. `delete_quietly`
    # cannot raise, so the original error is what propagates, which is the one
    # the caller needs to see.
    try:
        await db.commit()
        # IMMEDIATELY after the commit RETURNS, and the position is the control.
        # Move this one line above `await db.commit()` and a failing commit
        # marks the buffer durable, so the seam declines to rewrite rows that
        # just rolled back -- the exact failure this feature was built for, with
        # nothing raising.
        #
        # **`metering_check.py` case 16 holds this line in place, and the note
        # that used to sit here said no automated case could.** That was wrong,
        # and measured wrong: deleting this line left cases 13/13b/14a/14b/14c/15
        # and `admin_check.py` 5/5b/5c/5d ALL GREEN while every successful turn
        # wrote its usage twice -- once attributed inside the transaction, once
        # unattributed from the seam, because `durable` is `committed and
        # rows_written` and the first half was now never set. It is true that
        # both positions are syntactically fine and that every offline case
        # constructs its own receipt; what does not follow is that nothing can
        # read the source. 16 is ~15 lines of AST over this `try` body: find the
        # `await db.commit()`, assert the next statement is this assignment.
        receipt.committed = True
    except Exception:
        for key in staged_keys:
            storage.delete_quietly(key)
        raise

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
        # None when the rewriter was never asked, so the client can tell that
        # apart from "asked, and it changed nothing". The banner renders on True
        # alone -- see `AskOut`.
        rewritten_changed=(
            (
                result.rewritten_question is not None
                # Against the STRIPPED question, which is what the rewriter saw.
                # See the REWRITE payload above.
                and result.rewritten_question != result.question
            )
            if result.rewrite_attempted
            else None
        ),
        citations=citations,
        tool_steps=result.tool_steps,
        tool_calls=len(result.tool_calls),
        specialist=result.specialist,
        specialists=result.specialists,
        route_trigger=result.route_trigger,
        self_check_verdict=result.self_check_verdict,
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
