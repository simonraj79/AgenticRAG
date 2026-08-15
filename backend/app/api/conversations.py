"""Chat threads: list, create, read, rename, remove, and ask into.

A conversation is the only thing in this API reached by an id that is **not**
nested under the agent that owns it. `/api/conversations/{id}` is what the pinned
contract says, and that single fact drives most of what is unusual in this
module.

**The tenancy check is written by hand, and it is the highest-risk code here.**
`app/api/deps.py` explains why an agent's namespace is the boundary that fails
silently: a wrong namespace is not an error, it is a successful cross-tenant read
that returns another user's documents with no exception and no log line. Every
other route resolves that boundary through `OwnedAgent`, which binds to an
`agent_id` path parameter -- and there is no `agent_id` in these paths for it to
bind to. So the chain is rebuilt here, in one dependency rather than in six
handlers, and it runs in exactly one direction:

    conversation_id -> conversations row -> user_id == session user   (or 404)
                                         -> agent_id -> agents row
                                                     -> owner_user_id
                                                        == session user (or 404)
                                                     -> agent.namespace

**The agent is derived from the conversation. It is never accepted from the
client**, in a body, a query parameter or a header -- and neither is a namespace,
which `rag/retriever.py` makes structurally impossible by taking an `Agent`
object rather than a string. The second check, on `agents.owner_user_id`, is
redundant today: a conversation's `agent_id` was copied from an agent the caller
owned. It is kept because "redundant" is a claim about the code as it stands, and
the failure it guards against is the one nobody would see.

**Deleting a thread archives it.** See `delete_conversation` -- the short version
is that `queries.conversation_id` cascades, so a real DELETE would take this
thread's `trace_events` and `query_chunks` with it, and those are the rows Stage
3 scores and the audit trail rests on. Every read path in this module treats an
archived thread as gone, so the API behaves exactly as a hard delete would.

The ask route reuses `app/api/ask.py`'s engine unchanged. The two ask endpoints
differ in how they get a conversation, not in what a turn is.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.ask import (
    PREVIEW_CHARS,
    AskOut,
    AskRequest,
    CitationOut,
    OptionalSession,
    clean_question,
    run_turn,
)
from app.api.deps import CurrentUser, DbSession, OwnedAgent
from app.db.models import (
    Agent,
    Chunk,
    Conversation,
    Document,
    Query,
    QueryChunk,
    TraceEvent,
    User,
)
from app.rag.trace import REWRITE

router = APIRouter(prefix="/api", tags=["conversations"])

# Stripped and length-checked at the edge. `conversations.title` is String(200),
# so an over-long title is a 422 here rather than a DataError from the driver,
# and a title of pure whitespace collapses to "" and fails `min_length` instead
# of becoming a thread with a blank name in the sidebar.
ConversationTitle = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]


# --------------------------------------------------------------------------
# The tenancy boundary, by hand
# --------------------------------------------------------------------------

async def owned_conversation(
    conversation_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> Conversation:
    """Load a conversation the caller owns, or 404.

    A dependency rather than six copies of the same four lines, for the reason
    `app/api/deps.py` gives about `owned_agent`: a check copied into every
    handler is a check missing from the next one somebody adds, and the one it
    goes missing from will be the ask route, where the mistake retrieves from
    another tenant's corpus and returns their material as an answer.

    Ownership is `conversations.user_id`, not a hop through the agent. A thread
    is the record of what one person asked, so a shared agent (PRD section 4.2
    reserves `visibility` for that) must not turn every viewer of the corpus into
    a reader of everyone's chat history. Same rule, same reasoning, as
    `ask.get_trace`.

    404 rather than the 403 `owned_agent` returns. An agent id is a handle the
    UI holds across sessions, so a wrong owner there usually means a stale handle
    after an account switch and deserves a distinct status. A conversation id is
    only ever obtained from a list this same caller just fetched -- there is no
    stale-handle case to diagnose, and collapsing "not yours" into "not found"
    gives nothing away.

    An archived thread is treated as absent, which is what makes the soft delete
    in `delete_conversation` indistinguishable from a real one through this API.
    """
    conversation = await db.get(Conversation, conversation_id)
    if (
        conversation is None
        or conversation.user_id != user.id
        or conversation.is_archived
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found",
        )
    return conversation


OwnedConversation = Annotated[Conversation, Depends(owned_conversation)]


async def _conversation_agent(
    db: AsyncSession, conversation: Conversation, user: User
) -> Agent:
    """The agent this thread talks to, re-proved against the caller.

    **The only source of `agent_id` is the conversation row.** Accepting one from
    the request -- even as an optimisation to save this lookup -- would let a
    caller who owns thread A point a question at agent B's namespace, and the
    result would be a perfectly normal-looking answer built from somebody else's
    corpus. There is no parameter here through which that could happen.

    The owner check duplicates work `owned_conversation` has effectively already
    done, since a conversation's `agent_id` came from an agent the same user
    owned at creation time. It stays because the cost is a primary-key lookup
    that this request needs anyway, and the thing it protects -- `agent.namespace`,
    which is about to scope a Pinecone query -- has no other guard between here
    and the index.
    """
    agent = await db.get(Agent, conversation.agent_id)
    if agent is None or agent.owner_user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found",
        )
    return agent


# --------------------------------------------------------------------------
# Wire shapes
# --------------------------------------------------------------------------

class ConversationOut(BaseModel):
    """One thread, as the chat sidebar lists it."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    agent_id: uuid.UUID
    # Null only in the window between an explicit create with no title and the
    # thread's first question, which names it. The sidebar must render that.
    title: str | None = None
    # Not a column: an aggregate over `queries`, supplied by the route. It
    # carries a default so `model_validate` against the ORM object succeeds, and
    # `from_row` is the only sanctioned way to fill it -- a response built any
    # other way silently reports an empty thread.
    message_count: int = 0
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, conversation: Conversation, message_count: int) -> ConversationOut:
        return cls.model_validate(conversation).model_copy(
            update={"message_count": message_count}
        )


class MessageOut(BaseModel):
    """One completed turn, replayed from the database.

    `answer` and `latency_ms` are nullable because `queries.answer` and
    `queries.latency_ms` are: a row exists from the moment the question is
    accepted, and a turn whose generation failed keeps its question. `AskOut`
    types both non-null because it is only ever built from a turn that finished.

    `rewritten_question` is read back out of the REWRITE trace event, not off a
    column -- there is no `queries.rewritten_question`. Null means
    contextualisation did not run at all.
    """

    query_id: uuid.UUID
    question: str
    answer: str | None = None
    refused: bool
    latency_ms: int | None = None
    model_used: str | None = None
    rewritten_question: str | None = None
    created_at: datetime
    citations: list[CitationOut]


class ConversationDetail(ConversationOut):
    messages: list[MessageOut]


class ConversationCreate(BaseModel):
    """Body for POST /api/agents/{agent_id}/conversations.

    `extra="forbid"` because the only field is optional: a client that misspells
    it would otherwise get a 201 and an untitled thread, and then debug why its
    title never appeared.
    """

    model_config = ConfigDict(extra="forbid")

    # Omitted means "name it after the first question" -- see
    # `ask.derive_conversation_title`, which `run_turn` applies to any thread
    # still untitled when a turn lands.
    title: ConversationTitle | None = None


class ConversationPatch(BaseModel):
    """Body for PATCH /api/conversations/{id}. Rename only.

    Required rather than optional. This is not a general partial update -- there
    is exactly one editable field, and a PATCH that omits it is a request with no
    content, which is far more likely to be a client bug than an intention.
    Clearing a title back to null is deliberately not offered: an untitled thread
    is a transient state before the first question, not something to return to.
    """

    model_config = ConfigDict(extra="forbid")

    title: ConversationTitle


# --------------------------------------------------------------------------
# Reading a thread back
# --------------------------------------------------------------------------

async def _load_messages(db: AsyncSession, conversation_id: uuid.UUID) -> list[MessageOut]:
    """Every turn in one thread, oldest first, with its citations.

    Three statements, not one per message. The obvious shape -- load the queries,
    then walk them asking for citations -- is an N+1 that grows with the length of
    the conversation, and on an async session each of those follow-ups is a
    separate awaited round trip rather than the cheap attribute access it looks
    like in the source.

    **`marker` comes back as `query_chunks.rank`.** There is no marker column,
    and there does not need to be: `run_turn` writes the rank as the position the
    chunk held in the model's context, which is exactly what the answer's `[n]`
    refers to. The two fields are written from one integer and read from one
    column, which is what stops a replayed message from attributing a claim to a
    different source than the live answer did.

    Citations can legitimately come back empty for a turn that had them. CLAUDE.md
    records the cascade: `query_chunks.chunk_id` is ON DELETE CASCADE to `chunks`,
    which cascades from `documents`, so deleting a source file empties the stored
    evidence of every past answer that cited it. The answer text keeps its
    markers; `normalise_citation_markers` ran against a citation list that existed
    at the time. Nothing here can repair that, and pretending otherwise by hiding
    the markers would only make the loss invisible.
    """
    rows = (
        await db.scalars(
            select(Query)
            .where(Query.conversation_id == conversation_id)
            # Oldest first: a transcript reads downwards. `ix_queries_conversation
            # _created` returns it already ordered. The id tie-break is for the
            # server-side now() collisions described in `ask.list_queries`.
            .order_by(Query.created_at.asc(), Query.id.asc())
        )
    ).all()
    if not rows:
        return []

    query_ids = [row.id for row in rows]

    cited = await db.execute(
        select(
            QueryChunk.query_id,
            QueryChunk.chunk_id,
            QueryChunk.rank,
            QueryChunk.similarity_score,
            QueryChunk.rerank_score,
            Chunk.chunk_index,
            Chunk.document_id,
            Chunk.text,
            Document.filename,
        )
        .join(Chunk, Chunk.id == QueryChunk.chunk_id)
        .join(Document, Document.id == Chunk.document_id)
        .where(QueryChunk.query_id.in_(query_ids))
        .order_by(QueryChunk.query_id, QueryChunk.rank)
    )
    # No `Document.agent_id` predicate, unlike `ask._chunk_rows`, and the
    # asymmetry is deliberate. There it guards an INSERT: an unmatched row would
    # violate a foreign key and fail the turn. Here it could only ever hide a
    # citation that was already written and already proved, turning a tenancy
    # belt-and-braces into a silent data-loss bug on a read path.
    citations: dict[uuid.UUID, list[CitationOut]] = {}
    for row in cited:
        citations.setdefault(row.query_id, []).append(
            CitationOut(
                marker=row.rank,
                chunk_id=row.chunk_id,
                document_id=row.document_id,
                filename=row.filename,
                chunk_index=row.chunk_index,
                rank=row.rank,
                similarity_score=row.similarity_score,
                rerank_score=row.rerank_score,
                # Truncated on read rather than stored truncated: `chunks.text`
                # is the source of truth (it is what a re-embed rebuilds from),
                # and the preview length is a UI decision that should be
                # changeable without a migration.
                text_preview=row.text[:PREVIEW_CHARS],
            )
        )

    rewrites: dict[uuid.UUID, str] = {}
    events = await db.execute(
        select(TraceEvent.query_id, TraceEvent.payload)
        .where(TraceEvent.query_id.in_(query_ids), TraceEvent.event_type == REWRITE)
        # First REWRITE per query wins. One turn produces one today; Stage 2's
        # score-triggered loop will add more, and the contract's
        # `rewritten_question` is the string that was searched, so the earliest
        # is the wrong one to keep once that lands. Flagged rather than guessed
        # at -- see this module's notes.
        .order_by(TraceEvent.query_id, TraceEvent.step_index)
    )
    for row in events:
        after = (row.payload or {}).get("after")
        if isinstance(after, str) and after:
            rewrites.setdefault(row.query_id, after)

    return [
        MessageOut(
            query_id=row.id,
            question=row.question,
            answer=row.answer,
            refused=row.refused,
            latency_ms=row.latency_ms,
            model_used=row.model_used,
            rewritten_question=rewrites.get(row.id),
            created_at=row.created_at,
            citations=citations.get(row.id, []),
        )
        for row in rows
    ]


async def _message_count(db: AsyncSession, conversation_id: uuid.UUID) -> int:
    total = await db.scalar(
        select(func.count())
        .select_from(Query)
        .where(Query.conversation_id == conversation_id)
    )
    return int(total or 0)


# --------------------------------------------------------------------------
# Threads under an agent
# --------------------------------------------------------------------------

@router.get("/agents/{agent_id}/conversations", response_model=list[ConversationOut])
async def list_conversations(agent: OwnedAgent, db: DbSession) -> list[ConversationOut]:
    """This agent's threads, most recently active first.

    **The order is part of the contract, not a preference.** The chat view opens
    `rows[0]` on arrival, so any other ordering silently opens the wrong thread
    -- and `updated_at` is only meaningful because `run_turn` writes the
    conversation row on every turn (`models._updated_at` explains why nothing
    does that automatically). If threads stop reordering, look there first.

    Scoped on `agent_id` alone, not on `agent_id AND user_id`, matching
    `ask.list_queries`. `OwnedAgent` has already established that this caller
    owns this agent; a second, weaker predicate alongside it would have to be
    kept in step with the first when sharing arrives.

    One statement, one round trip. Listing threads and then counting messages per
    thread is an N+1 that grows with the sidebar. `func.count(Query.id)` rather
    than `count(*)` because the join is an OUTER join -- a brand-new thread must
    still appear, and on that row `count(*)` would count the single all-NULL join
    row and report one message.
    """
    rows = await db.execute(
        select(Conversation, func.count(Query.id))
        .outerjoin(Query, Query.conversation_id == Conversation.id)
        .where(
            Conversation.agent_id == agent.id,
            Conversation.is_archived.is_(False),
        )
        .group_by(Conversation.id)
        .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
    )
    return [
        ConversationOut.from_row(conversation, count) for conversation, count in rows.all()
    ]


@router.post(
    "/agents/{agent_id}/conversations",
    response_model=ConversationOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_conversation(
    body: ConversationCreate, agent: OwnedAgent, user: CurrentUser, db: DbSession
) -> ConversationOut:
    """Start an empty thread.

    In the contract, and deliberately not on the UI's critical path: the chat
    view creates threads implicitly by asking (`POST /api/agents/{id}/ask`),
    because an empty conversation is a row nobody asked for and a sidebar entry
    with nothing in it. This exists for a client that wants to name a thread
    before typing into it, and for anything scripted.

    `user.id` rather than `agent.owner_user_id`. They are the same person today;
    they stop being the same person the moment PRD 4.2's sharing lands, and
    `conversations.user_id` records who is talking, not who owns the corpus.
    """
    conversation = Conversation(
        id=uuid.uuid4(),
        agent_id=agent.id,
        user_id=user.id,
        # Null is fine and is the normal case: `run_turn` names an untitled
        # thread after its first question.
        title=body.title,
    )
    db.add(conversation)
    await db.commit()

    # `created_at` and `updated_at` are server defaults, so their values come
    # from Postgres rather than from Python. SQLAlchemy usually fetches them via
    # RETURNING on the INSERT, but when it does not the attributes are left
    # pending a lazy load -- and a lazy load on an async session raises
    # MissingGreenlet at the moment the serialiser touches them, which reads as a
    # driver fault rather than a missing refresh.
    await db.refresh(conversation)

    # 0 by construction, not by query: a thread created a millisecond ago has no
    # turns in it.
    return ConversationOut.from_row(conversation, 0)


# --------------------------------------------------------------------------
# One thread
# --------------------------------------------------------------------------

@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(
    conversation: OwnedConversation, db: DbSession
) -> ConversationDetail:
    """A whole thread: metadata plus every turn, with citations.

    Uncapped, unlike `ask.list_queries`. A transcript that silently omits its
    middle is worse than a slow one -- the follow-up the user is reading depends
    on the turn above it -- and the practical ceiling is a person typing, not a
    batch job. If a thread ever grows large enough for this to hurt, the fix is
    windowed paging in the chat view rather than a cap here that would make the
    history-aware rewriter and the visible transcript disagree.
    """
    messages = await _load_messages(db, conversation.id)
    detail = ConversationOut.from_row(conversation, len(messages))
    return ConversationDetail(**detail.model_dump(), messages=messages)


@router.patch("/conversations/{conversation_id}", response_model=ConversationOut)
async def rename_conversation(
    body: ConversationPatch, conversation: OwnedConversation, db: DbSession
) -> ConversationOut:
    """Rename a thread.

    A rename counts as activity: `title` is a mapped column, so writing it fires
    the `onupdate` on `updated_at` and the thread rises to the top of the
    sidebar. That is the intended behaviour -- the user just touched it -- but it
    is a side effect of the column definition rather than of anything visible
    here, so it is worth naming.

    `db.refresh` after the commit because `updated_at` was written by
    `onupdate=func.now()`, a server-side expression: the attribute holds that
    expression until it is refetched, and reading it on an async session without
    a refresh raises MissingGreenlet from inside the serialiser.
    """
    conversation.title = body.title
    await db.commit()
    await db.refresh(conversation)

    return ConversationOut.from_row(
        conversation, await _message_count(db, conversation.id)
    )


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(
    conversation: OwnedConversation, db: DbSession
) -> dict[str, bool]:
    """Remove a thread from the user's view.

    **This archives rather than destroys, and the distinction is invisible
    through the API.** `owned_conversation` treats an archived row as absent, so
    every route in this module -- read, rename, ask, and a second delete -- 404s
    afterwards exactly as it would on a deleted row.

    The reason is the cascade. `queries.conversation_id` is ON DELETE CASCADE,
    and `trace_events` and `query_chunks` cascade from `queries`, so a real
    DELETE here removes the decision timeline and the stored `contexts` for every
    turn in the thread. Those are the rows Stage 3's Ragas scores are computed
    from and the ones the "full audit trail" argument for building this rather
    than buying it rests on. CLAUDE.md already records the same failure arriving
    by a different door -- deleting a document empties the contexts of every past
    query that cited it -- and calls out that a scorecard which keeps its scores
    and loses its evidence is worse than one that loses both, because the numbers
    still render and nothing signals that they are no longer reproducible.
    `models.Conversation.is_archived` exists for precisely this, and says so.

    So: a UI affordance as casual as "delete chat" does not get to destroy
    measurement data. A purge -- for a genuine erasure request, where destroying
    the evidence is the point -- is a separate, deliberate operation, and it does
    not belong on this verb.
    """
    conversation.is_archived = True
    await db.commit()
    return {"ok": True}


# --------------------------------------------------------------------------
# POST /api/conversations/{conversation_id}/ask
# --------------------------------------------------------------------------

@router.post("/conversations/{conversation_id}/ask", response_model=AskOut)
async def ask_in_conversation(
    body: AskRequest,
    conversation: OwnedConversation,
    user: CurrentUser,
    db: DbSession,
    session: OptionalSession,
) -> AskOut:
    """Ask a follow-up inside an existing thread.

    The only difference from `ask.ask` is where the conversation comes from, and
    therefore how much history reaches the rewriter -- `run_turn` loads the
    thread's recent turns and hands them to `pipeline.answer_question`, which
    resolves "what is its power budget?" into a question that can actually be
    embedded. Everything after that is byte-identical between the two routes
    because it is the same function.

    The agent is derived from the conversation and re-checked against the caller
    before `run_turn` sees it. Read `_conversation_agent` before changing the
    order of these two lines.
    """
    question = clean_question(body.question)
    agent = await _conversation_agent(db, conversation, user)

    return await run_turn(
        db,
        agent=agent,
        user=user,
        session=session,
        conversation=conversation,
        question=question,
    )
