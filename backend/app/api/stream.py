"""SSE transport for the answer path -- the same turn, narrated while it runs.

PRD section 2.2. Two routes, `POST /api/agents/{agent_id}/ask/stream` and
`POST /api/conversations/{conversation_id}/ask/stream`, mirroring the two JSON
ask endpoints and running the SAME `ask.run_turn`. Nothing about retrieval,
reranking, the tool loop or recording moves; streaming is a transport on the last
model call, and `app/rag/events.py` carries the vocabulary both halves share.

**Two new paths, not a flag and not `Accept:` negotiation.** `app/api/ask.py`
already states the rule this follows -- two ask endpoints, one `run_turn`,
because "two copies of that body would drift, and the half that drifts is always
the recording half". Streaming is a third transport, not a third engine. A
`?stream=true` would have to strip `response_model=AskOut` off the JSON handler,
deleting the Pydantic validation the terminal `done` payload depends on for being
byte-identical to that route; `Accept:` negotiation would make that guarantee
depend on a header a proxy may rewrite. New paths mean the existing handlers are
not edited at all, which is a structural guarantee rather than a careful one.

------------------------------------------------------------------
THE SESSION, AND WHY IT IS NOT `DbSession`.

**Do not put `db: DbSession` in either handler below.** Since FastAPI 0.106 the
exit half of a `yield` dependency runs BEFORE a `StreamingResponse`'s body is
consumed, so a session taken from the request is already closed by the time the
first frame is produced. The failure surfaces as a closed-connection or
wrong-loop error deep inside SQLAlchemy, naming neither the dependency nor this
module -- verbatim the trap CLAUDE.md records for `BackgroundTasks`, arriving
through a different door.

So the turn runs in a task that opens its own session (`_run_turn_streamed`), and
the corollary `app/rag/jobs.py` and `app/handouts/jobs.py` both state applies
here too: **pass ids, never ORM objects.** `owned_agent` returns an `Agent` bound
to the request session; carrying it across is the same bug wearing the shape of
an argument. Ownership is proved in the request phase, then only ids cross.

This is a rule, not a structural guarantee -- nothing stops the next person
typing `db: DbSession` into a signature below, and the traceback will name
SQLAlchemy. It is why this paragraph is as long as it is.
------------------------------------------------------------------
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.ask import (
    AskRequest,
    OptionalSession,
    clean_question,
    derive_conversation_title,
    run_turn,
)
from app.api.conversations import OwnedConversation, _conversation_agent
from app.api.deps import CurrentUser, DbSession, OwnedAgent
from app.db.models import Agent, Conversation, Session, User
from app.db.session import SessionLocal
from app.rag import events

log = logging.getLogger("uvicorn.error")

router = APIRouter(prefix="/api", tags=["ask"])

# How long the drain loop waits on an empty queue before sending a comment frame.
#
# **The heartbeat is not optional.** The silent window before the first token is
# real: contextualisation ~4 s on a follow-up, embed 365 ms, Pinecone 394 ms,
# Cohere ~830 ms, plus ~1.6 s per tool search and 1-3 s per `run_python`. A tool
# turn can be quiet for 5-8 s, and the `phase` events do not reliably cover all of
# it. Ten seconds is comfortably inside any intermediary's idle timeout and cheap
# enough that an idle stream costs six bytes a minute.
HEARTBEAT_S = 10.0

# Strong references to in-flight turns. **This set is what makes a disconnect
# safe.** `asyncio` holds only a weak reference to a running task, so a task
# nobody is awaiting can be garbage-collected mid-await and simply stop -- which
# on this path would mean a generation that was paid for, a `queries` row that
# was flushed, and no commit. See `_frames` for the whole disconnect story.
_IN_FLIGHT: set[asyncio.Task] = set()


# --------------------------------------------------------------------------
# Framing
# --------------------------------------------------------------------------

def _frame(event: str, seq: int, payload: dict[str, Any]) -> str:
    """One SSE frame: an `event:` line, one `data:` line, a blank line.

    The `event:` name always equals the payload's `type`, so a frame is
    self-describing whether a parser dispatches on the header or on the body.

    One `data:` line is always enough because `json.dumps` escapes every newline
    inside a string, so the encoded body cannot contain a raw `\\n` -- which is
    the character that would otherwise split a frame in half. A client that joins
    multiple `data:` lines with `\\n` is still correct against this; it simply
    never has more than one.

    **No `id:` line, ever.** An `id:` invites `Last-Event-ID` resumption, and a
    turn that writes rows and bills tokens is not replayable. `seq` gives a client
    what it actually needs -- the ability to notice a dropped or reordered frame
    -- without inviting the browser to re-run the turn.
    """
    body = json.dumps(
        {"type": event, "seq": seq, **payload},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"event: {event}\ndata: {body}\n\n"


# The comment frame. A client's parser must ignore it and must NOT count it as an
# event or advance its `seq` expectation.
_PING = ": ping\n\n"

# Set on the response, and every one of them earns its place.
#
# `no-transform` is the one that is easy to drop and expensive to lose: a
# compressing intermediary buffers in order to compress, which turns a stream
# into one late blob -- a failure that renders perfectly and produces no error.
# `X-Accel-Buffering: no` says the same thing to nginx specifically. Note that no
# `GZipMiddleware` is installed in `app/main.py`; adding one later would break
# streaming silently, and there is a comment there saying so.
_SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


# --------------------------------------------------------------------------
# The turn, in a task that owns its own session
# --------------------------------------------------------------------------

async def _resolve(
    db: AsyncSession, *, agent_id: uuid.UUID, user_id: uuid.UUID
) -> tuple[Agent, User]:
    """Re-load the agent and user inside the streaming session, and re-prove the pair.

    Ownership was already established in the request phase by `OwnedAgent` or by
    `conversations._conversation_agent`, so this is the third independent check on
    one boundary -- and `app/api/deps.py` explains why that is the right number
    for this particular boundary: a wrong namespace is not an error, it is a
    successful cross-tenant read with no exception and no log line. The rows are
    being fetched anyway; the comparison is free.
    """
    agent = await db.get(Agent, agent_id)
    user = await db.get(User, user_id)
    if agent is None or user is None or agent.owner_user_id != user.id:
        # Only reachable if the agent or the account was deleted between the
        # dependency resolving and this task starting.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found"
        )
    return agent, user


async def _run_turn_streamed(
    queue: asyncio.Queue,
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: uuid.UUID | None,
    conversation_id: uuid.UUID | None,
    question: str,
) -> None:
    """One turn, start to durable, writing frames onto `queue`.

    Opens its own session -- see this module's docstring for why it must -- and
    puts a `None` sentinel on the queue in a `finally`, so the drain loop always
    terminates even if this raises somewhere unexpected.

    `conversation_id is None` means a new thread. The `Conversation` is created
    HERE, inside this session, so it lands in the same transaction as the turn
    rather than being committed early by the request session and left orphaned if
    generation fails -- the "row nobody asked for" that
    `conversations.create_conversation` argues against. That is also why the
    client learns `conversation_id` from the `start` frame: by the time this
    function knows it, the response has long since begun.
    """
    async def emit(name: str, payload: dict[str, Any]) -> None:
        """The `events.Emit` the pipeline is handed. A queue put, and nothing else.

        Deliberately not a `TraceRecorder` and deliberately touching no session:
        `new features/loop.md` S3 -- the pipeline never reaches the database, the
        route records at the boundary -- is what keeps a tool addable without
        either module learning the other's concerns, and a callback that could
        write rows would end that in one line.
        """
        await queue.put((name, payload))

    try:
        async with SessionLocal() as db:
            agent, user = await _resolve(db, agent_id=agent_id, user_id=user_id)
            session_row = (
                await db.get(Session, session_id) if session_id is not None else None
            )

            if conversation_id is None:
                conversation = Conversation(
                    id=uuid.uuid4(),
                    agent_id=agent.id,
                    user_id=user.id,
                    title=derive_conversation_title(question),
                )
                db.add(conversation)
                await db.flush()
            else:
                conversation = await db.get(Conversation, conversation_id)
                if (
                    conversation is None
                    or conversation.user_id != user.id
                    or conversation.agent_id != agent.id
                    or conversation.is_archived
                ):
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="Conversation not found",
                    )

            # `run_turn` emits `start` itself, the moment `query.id` exists.
            result = await run_turn(
                db,
                agent=agent,
                user=user,
                session=session_row,
                conversation=conversation,
                question=question,
                emit=emit,
            )

        # After the commit and after the handout refresh, both of which happened
        # inside `run_turn`. **`done` wraps the AskOut rather than being it**: every
        # other frame carries `type` and `seq`, and making the terminal frame the one
        # structurally different shape would hand a parser two grammars -- while
        # putting `type`/`seq` INTO `AskOut` would change what the JSON route
        # returns, which is forbidden. `model_dump(mode="json")` on the same
        # validated model the JSON route returns is what makes the two bodies
        # identical rather than merely similar.
        await emit(events.DONE, {"result": result.model_dump(mode="json")})

    except HTTPException as exc:
        # Reachable only for the two rows re-checked above, i.e. deletion between
        # the request phase and here. Everything a client normally gets wrong --
        # auth, ownership, a blank question -- was already answered with an
        # ordinary HTTP status before a single byte went out.
        await emit(events.ERROR, {"status": exc.status_code, "detail": str(exc.detail)})
    except Exception as exc:  # noqa: BLE001 - the terminal frame is the report
        # **A failure AFTER the 200 and the headers are on the wire cannot be an
        # HTTP status any more**, so it has to be a frame. The case that matters
        # is an OpenRouter `404 No endpoints found that can handle the requested
        # parameters`, which CLAUDE.md records this project hitting three times
        # and which the agent loop deliberately lets propagate: a stream that
        # merely stops is indistinguishable from a dropped connection, and the
        # one thing a configuration fault must not do is look like a network
        # blip.
        log.exception("Streaming turn failed: %s", exc)
        await emit(
            events.ERROR,
            {
                "status": status.HTTP_500_INTERNAL_SERVER_ERROR,
                "detail": f"{type(exc).__name__}: {exc}",
            },
        )
    finally:
        await queue.put(None)


# --------------------------------------------------------------------------
# The drain loop
# --------------------------------------------------------------------------

async def _frames(
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: uuid.UUID | None,
    conversation_id: uuid.UUID | None,
    question: str,
) -> AsyncIterator[str]:
    """Drain the turn's frames onto the wire, numbering them as they leave.

    **`seq` is assigned here, at the single point every frame passes through.**
    The queue is FIFO, so dequeue order is emission order, and one counter in one
    place is what makes "starts at 0, increments by exactly 1" true rather than
    hoped for -- the producing modules never see a sequence number at all.

    The queue is unbounded on purpose. A bounded one would make `emit` block once
    a disconnected client stopped draining, which would stall the turn forever --
    the opposite of what the disconnect rule below wants. Unbounded costs the
    frames of one answer, which is tens of kilobytes of small dicts.

    ------------------------------------------------------------------
    WHAT HAPPENS WHEN THE CLIENT DISCONNECTS: **the turn runs to completion and
    commits.** Nothing is cancelled.

    That is the honest behaviour rather than the convenient one, and it is this
    module's whole reason for being shaped the way it is. `app/api/ask.py` opens
    with the claim the product rests on -- "a turn is not finished when the answer
    is returned, it is finished when the record of it is durable". A user closing
    the tab at 40 seconds has already paid for the generation; discarding the
    `queries` row, its trace, its `query_chunks` and its handouts would destroy
    the Stage 3 evidence for a turn that actually happened, silently, and would do
    it more often than the JSON route ever could because a 45-second stream is
    exactly the thing people close.

    The mechanism is two facts, not one. **The session belongs to the task**, not
    to this generator, so nothing this generator does on the way out can close a
    connection the turn is still using -- an `async with SessionLocal()` here
    would tear the session down the moment Starlette called `aclose()`, which is
    the subtle version of the very trap the module docstring warns about.
    And **`_IN_FLIGHT` holds a strong reference**, because that, not shielding, is
    what stops a task nobody awaits from being collected mid-flight.

    Deliberately NOT `await asyncio.shield(task)` in the `finally`. On the normal
    path the task is already done -- the sentinel that broke the loop is put after
    `run_turn` returns -- so the await would be a no-op. On the disconnect path
    the `finally` runs inside `aclose()`, where awaiting a 40-second turn either
    blocks the response teardown or raises `RuntimeError: async generator ignored
    GeneratorExit`. The two facts above deliver the guarantee shielding was there
    to deliver, without either failure.

    The cost, stated plainly: an abandoned turn keeps a pool connection for its
    full 6-45 s with nobody reading the result, against `pool_size=5 +
    max_overflow=5` (`app/db/session.py`). That cap is pre-existing; streaming
    raises the disconnect rate and so makes it more reachable.

    The `done` frame is written after the commit. If nobody is listening the put
    lands in a queue nobody drains, and the record already exists. That is the
    only correct order.
    ------------------------------------------------------------------
    """
    queue: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(
        _run_turn_streamed(
            queue,
            agent_id=agent_id,
            user_id=user_id,
            session_id=session_id,
            conversation_id=conversation_id,
            question=question,
        )
    )
    _IN_FLIGHT.add(task)
    task.add_done_callback(_IN_FLIGHT.discard)

    seq = 0
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_S)
            except TimeoutError:
                # A comment frame. Costs six bytes, and is the difference between
                # an intermediary seeing an idle connection and seeing a live one
                # during the 5-8 s a tool turn can spend without a phase event.
                yield _PING
                continue

            if item is None:
                break

            name, payload = item
            yield _frame(name, seq, payload)
            seq += 1
    finally:
        if not task.done():
            # Reached when the reader went away mid-turn. Nothing to do -- and
            # doing nothing is the point. See the docstring above.
            log.info(
                "SSE reader disconnected after %d frames; the turn continues and "
                "will still commit.",
                seq,
            )


def _stream(
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: uuid.UUID | None,
    conversation_id: uuid.UUID | None,
    question: str,
) -> StreamingResponse:
    """Wrap the drain loop in a correctly-headed SSE response.

    Plain `StreamingResponse`, not `sse-starlette`. **That package is installed
    but is not a declared dependency** -- it is in `requirements.txt` only
    transitively via `mcp`/`langchain-mcp-adapters`, and importing it by name
    would make it a real dependency that has to be promoted to `requirements.in`,
    which is exactly the argument that file already makes for `python-multipart`,
    `langchain-openai` and `pandas`. What it would buy is five header literals and
    a keepalive; the keepalive is the four lines above. Against that,
    `EventSourceResponse` runs the body inside its own task group, so the
    disconnect design in `_frames` would have to be re-verified against those
    semantics rather than against plain Starlette's.

    If the heartbeat ever proves fiddly, promoting `sse-starlette` is one line in
    `requirements.in` plus a comment -- a cheap fallback, not a rewrite.
    """
    return StreamingResponse(
        _frames(
            agent_id=agent_id,
            user_id=user_id,
            session_id=session_id,
            conversation_id=conversation_id,
            question=question,
        ),
        media_type="text/event-stream; charset=utf-8",
        headers=_SSE_HEADERS,
    )


# --------------------------------------------------------------------------
# POST /api/agents/{agent_id}/ask/stream
# --------------------------------------------------------------------------

@router.post("/agents/{agent_id}/ask/stream")
async def ask_stream(
    body: AskRequest,
    agent: OwnedAgent,
    user: CurrentUser,
    db: DbSession,
    session: OptionalSession,
) -> StreamingResponse:
    """Ask one question over SSE, starting a new thread for it.

    The streaming twin of `ask.ask`, which is not edited by this feature. Note
    that unlike that route, the `Conversation` is NOT created here -- it is
    created inside the streaming session so it shares the turn's transaction, and
    the client learns its id from the `start` frame.

    `db` is present only so the dependencies above can resolve; **nothing below
    this line may use it**, and nothing does -- see the module docstring for the
    FastAPI 0.106 teardown ordering that makes touching it a bug whose traceback
    names SQLAlchemy.

    No `response_model`. The response is an event stream, and the terminal frame's
    `result` is validated by `AskOut` where it is built -- inside `run_turn` --
    which is the same model and the same validation the JSON route returns.
    """
    question = clean_question(body.question)
    # Ids only. An ORM object belongs to the session that loaded it.
    return _stream(
        agent_id=agent.id,
        user_id=user.id,
        session_id=session.id if session is not None else None,
        conversation_id=None,
        question=question,
    )


# --------------------------------------------------------------------------
# POST /api/conversations/{conversation_id}/ask/stream
# --------------------------------------------------------------------------

@router.post("/conversations/{conversation_id}/ask/stream")
async def ask_in_conversation_stream(
    body: AskRequest,
    conversation: OwnedConversation,
    user: CurrentUser,
    db: DbSession,
    session: OptionalSession,
) -> StreamingResponse:
    """Ask a follow-up over SSE, inside an existing thread.

    The streaming twin of `conversations.ask_in_conversation`, resolving the agent
    exactly as that route does: from the conversation row, never from the request.
    `_conversation_agent` re-proves it against the caller, and `_run_turn_streamed`
    then re-proves the whole chain a third time inside its own session -- read
    `app/api/conversations.py`'s docstring before shortening any of that.

    As above: `db` exists for the dependencies and must not be used here.
    """
    question = clean_question(body.question)
    agent = await _conversation_agent(db, conversation, user)

    return _stream(
        agent_id=agent.id,
        user_id=user.id,
        session_id=session.id if session is not None else None,
        conversation_id=conversation.id,
        question=question,
    )
