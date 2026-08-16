"""The bounded agent loop -- generation, once the model can act.

`app/rag/pipeline.py` is a straight line: contextualise, retrieve once, generate
once. That shape cannot answer a question with two halves. Ask "how does the
propulsion budget compare with the comms budget?" and one embedding of one
question retrieves whichever half is closer in vector space, then the model
answers confidently about that half. Reranking does not help, because the missing
text was never a candidate.

PRD open item 7 specifies a *score-triggered* rewrite loop as the Stage 2 answer.
It would not help here either: both retrievals score fine, they are each simply
half an answer. Letting the model call the retriever solves both cases at once --
it rewrites because it decided to, and it searches twice because it noticed there
were two things to look up. That is why this supersedes item 7 rather than
sitting beside it, and it is also why CLAUDE.md's calibration measurement matters:
on `3.1-lesson-gist.md` on-topic questions scored 0.61-0.67 and off-topic ones
0.49-0.58, overlapping bands with no separation, so a threshold sitting in that
distribution fires late on bad retrievals and early on good ones. A model reading
the retrieved text can tell "this is about the wrong thing" far more reliably.

**Retrieval still runs before this loop and is not replaced by the tool.** The
first search is unconditional in every real turn, so making the model ask for it
would waste a round trip; `RETRIEVE` and its `SCORE_CHECK` keep working unchanged;
and an agent that calls no tools produces exactly today's answer from exactly
today's context. The tool is for the *second* search onward.

------------------------------------------------------------------
THE CIRCULAR IMPORT, AND HOW IT IS BROKEN.

`pipeline` imports this module at the top (it needs `ContextLedger` and
`run_agent_loop` for its generation branch). This module needs two things from
`pipeline` -- `format_context`, so there is one renderer rather than two, and
`USER_TEMPLATE`, so the human turn keeps its shape -- and importing it back at
module scope would deadlock the first import of either.

The import is therefore made **inside the two functions that need it**
(`ContextLedger.format_context` and `_human_turn`). Both run long after both
modules are loaded, and the cost is one dict lookup in `sys.modules` per call.

The alternative -- moving `format_context` and `USER_TEMPLATE` into a third
module -- was rejected because it would put the prompt's shape somewhere neither
the chain nor the loop lives, and the whole point of delegating is that the
prompt the model sees does not change form mid-turn.
------------------------------------------------------------------
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from langchain_core.documents import Document
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import BaseTool

from app.db.models import Agent
from app.rag import events
from app.rag.refusal import detect_gap
from app.rag.retriever import (
    META_CHUNK_ID,
    META_CHUNK_INDEX,
    META_FILENAME,
    RERANK_SCORE_KEY,
    Retrieval,
)
from app.tools.corpus import SEARCH_CORPUS
from app.tools.registry import ToolArtifact, ToolContext, ToolOutcome, build_tools

log = logging.getLogger("uvicorn.error")

# Appended to the persona prompt, never replacing it.
#
# `SystemMessage(content=...)` is used rather than the `("system", ...)` tuple
# form throughout this project precisely so that a brace in user-editable persona
# text is not parsed as a template variable (see `pipeline.answer_question`). The
# guidance is concatenated after the persona for the same reason: string
# concatenation cannot introduce a template variable that the invoke below would
# then demand a value for.
#
# The cost line ("only when they earn their cost") is not politeness. A search is
# an embedding call plus a Pinecone query plus, when reranking is on, a Cohere
# call -- CLAUDE.md measures those at 365 ms + 394 ms + ~830 ms, so roughly 1.6 s
# per tool call. Against a persona turn that is already the slowest thing in the
# product, a model that does not know a tool is expensive will call it every turn.
#
# The last sentence exists because a model asked for a chart will happily
# *describe* the chart it did not make.
#
# **The last paragraph is the load-bearing one, and it exists because the first
# version of this text did not work.** Measured 2026-08-16, `agentic_check.py`
# S3: given one chunk about batteries and a question that also asked about data
# storage, Gemma answered the half it could and wrote "The provided text does not
# contain information regarding the onboard storage" for the other -- with
# `search_corpus` bound and unused, on a corpus that plainly contained the
# answer.
#
# That is not a weak model. It is two instructions competing, and the wrong one
# winning for a structural reason. Every system prompt in this project is
# refusal-first by design (`personas.py` states the grounding rule before it
# establishes voice, and DEFAULT_SYSTEM_PROMPT does the same) because that is
# what makes the non-agentic pipeline trustworthy -- CLAUDE.md's measurement
# that "refusal comes from the prompt, not the threshold" is the whole reason
# the product can be trusted to say "I don't know". A model drilled to treat a
# gap in its context as a cue to DECLINE will do exactly that when handed a tool
# for filling gaps, because declining was made the stronger, earlier rule.
#
# The fix is not to weaken grounding -- that would trade a hallucination-free
# system for a tool-happy one. It is to SEQUENCE the two rules: searching comes
# before concluding, and refusal is a conclusion. It goes last because it is what
# the model reads last, and the earlier draft's final line ("say so plainly when
# the context does not cover something") was reinforcing precisely the behaviour
# that made the tool unreachable.
TOOL_GUIDANCE = """

You have tools. Use them only when they earn their cost.

- search_corpus: run another search when the question has more than one part, \
when the context above does not cover something you were asked, or when a term \
in the question does not appear in the context. Search for the missing thing, \
not the whole question again.
- run_python: write and run Python when the user wants a chart, a slide deck, a \
table or a file. Put the numbers in the code as literals -- you have no \
filesystem and no network.

Rules that do not change: answer only from the context, cite with [n] markers, \
and never claim to have made a file unless run_python returned one. A tool \
result is context too, and is cited the same way.

One rule DOES change, and it is the important one. "The context does not cover \
this" is now something you conclude AFTER searching, never instead of \
searching. If any part of the question is missing from the context above, call \
search_corpus for that part before you answer. Only say the corpus does not \
cover something once a search has come back without it."""

# Sent as a user turn when `detect_gap` fires, immediately before a forced
# `search_corpus` call. It names the phrase that triggered it so the model is
# answering a specific observation about its own last message rather than a
# generic scold, and it repeats the one instruction that matters -- search for
# the MISSING part, not the whole question, which is otherwise the reflex.
GAP_NUDGE = (
    "Your last answer said \"{marker}\". Before that stands, search the corpus "
    "for the part you said was missing -- just that part, not the whole "
    "question. If the search comes back without it, say so and that will be the "
    "right answer."
)

# How many consecutive steps may end with *every* tool call having failed before
# the loop stops trying and forces an answer.
#
# One failure must never stop the loop -- a model that wrote bad Python reading
# its own traceback and fixing it on the next step is the single most valuable
# behaviour a code interpreter has, and it is lost the moment a failure is
# treated as terminal. Two in a row is different: nothing has succeeded, the
# model is not converging, and every further step spends a full generation round
# trip plus a tool call to produce the same error again. The user still gets an
# answer, because the forced final invoke below runs either way; what changes is
# `stopped_reason`, which is how the trace distinguishes "it ran out of budget
# while making progress" from "it ran out of ideas".
MAX_CONSECUTIVE_FAILED_STEPS = 2


# --------------------------------------------------------------------------
# The ledger -- why citations survive a mid-turn retrieval
# --------------------------------------------------------------------------


@dataclass
class LedgerEntry:
    """One chunk in the turn's context, with the marker it was given."""

    # 1-based, assigned once, never reassigned. Position IS the marker:
    # `ask.run_turn` builds `AskOut.citations` by enumerating
    # `AnswerResult.documents` from 1, so an entry's marker and its index in
    # `ContextLedger.documents` must agree or every citation in the answer points
    # one source to the left.
    marker: int
    document: Document
    # The dedupe key, not necessarily a `chunks.id`. See `_dedupe_key`.
    key: str
    similarity: float | None = None
    rerank: float | None = None
    # "initial" | "tool". Diagnostic rather than functional: it is what makes a
    # trace answer "did the model's own search actually add anything?" without
    # re-deriving it from marker arithmetic.
    source: str = "initial"


def _dedupe_key(doc: Document) -> str:
    """The identity of a retrieved chunk, for deduplication.

    `chunk_id` is the real answer -- it is written into every vector's metadata
    at upsert and is the only join key back to Postgres. Two fallbacks exist
    because a vector without one is not a broken vector: it predates the current
    metadata scheme, `ask._chunk_uuid` already drops it from the citation list
    rather than failing the turn, and it can still ground an answer perfectly
    well. What it must not do is appear twice under two markers when the model
    searches for the same thing twice, so it is deduped on the next most stable
    thing available and finally on a hash of its own text.

    The prefixes keep the three namespaces apart. Without them a filename that
    happened to look like a UUID could collide with a real chunk id.
    """
    raw = doc.metadata.get(META_CHUNK_ID)
    if raw:
        return f"chunk:{raw}"

    filename = doc.metadata.get(META_FILENAME)
    index = doc.metadata.get(META_CHUNK_INDEX)
    if filename is not None and index is not None:
        return f"loc:{filename}#{index}"

    # sha1 rather than `hash()`: the builtin is salted per interpreter run, and a
    # key that changes between processes is a key that silently stops deduping.
    digest = hashlib.sha1(doc.page_content.encode("utf-8", "replace")).hexdigest()
    return f"text:{digest}"


class ContextLedger:
    """Ordered, deduped by chunk id. Owns the `[n]` numbering for the whole turn.

    Citations are `[n]` markers resolved against `AskOut.citations`, which
    `run_turn` builds 1-based from `AnswerResult.documents`. Once a tool can add
    documents in the middle of a turn, three things have to hold or the markers
    lie:

    1. A chunk retrieved twice keeps **one** marker, not two.
    2. A marker shown to the model inside a tool result is the **same** marker
       the user sees in the citation list.
    3. Order is stable, because position is the marker.

    All three are properties of one object owning the numbering, which is why
    this exists rather than each tool numbering its own results.
    """

    def __init__(self) -> None:
        self._entries: list[LedgerEntry] = []
        # key -> marker. The marker, not the entry, because every lookup this
        # class does wants the number.
        self._markers: dict[str, int] = {}

    # ---------------------------------------------------------------- build

    @classmethod
    def seed(cls, retrieval: Retrieval) -> ContextLedger:
        """Start a turn from the unconditional first retrieval."""
        ledger = cls()
        ledger._absorb(retrieval, source="initial")
        return ledger

    def merge(self, retrieval: Retrieval) -> list[int]:
        """Add anything new; return the markers of everything in `retrieval`.

        **Existing entries get their markers returned too, and that is the
        load-bearing detail.** When the model searches for something it already
        has, the tool result says "these are chunks [2] and [5]" rather than
        presenting them as new -- so the model can see it is going in circles and
        stop, and the marker the user will eventually see stays correct. Hiding
        already-held chunks produces a loop that searches three times for the
        same thing and a citation list that disagrees with the answer.

        The returned list is in `retrieval.documents` order -- relevance order
        for this search -- so it is deliberately not sorted or monotonic. The
        caller renders it as the model should read it, best match first.
        """
        return self._absorb(retrieval, source="tool")

    def _absorb(self, retrieval: Retrieval, *, source: str) -> list[int]:
        # Similarity comes from `scored`, which is the PRE-rerank candidate list,
        # while `documents` is the post-rerank final set. They are different
        # lengths and different orders once reranking has run, so the two are
        # joined through the dedupe key rather than by index -- exactly the
        # warning `Retrieval.similarity_scores` carries.
        similarity_by_key: dict[str, float] = {}
        for doc, score in retrieval.scored:
            similarity_by_key.setdefault(_dedupe_key(doc), float(score))

        markers: list[int] = []
        for doc in retrieval.documents:
            key = _dedupe_key(doc)
            existing = self._markers.get(key)
            if existing is not None:
                markers.append(existing)
                continue

            raw_rerank = doc.metadata.get(RERANK_SCORE_KEY)
            entry = LedgerEntry(
                marker=len(self._entries) + 1,
                document=doc,
                key=key,
                similarity=similarity_by_key.get(key),
                rerank=float(raw_rerank) if raw_rerank is not None else None,
                source=source,
            )
            self._entries.append(entry)
            self._markers[key] = entry.marker
            markers.append(entry.marker)

        return markers

    # ----------------------------------------------------------------- read

    @property
    def entries(self) -> list[LedgerEntry]:
        """Marker order. A copy, so a caller cannot renumber the turn."""
        return list(self._entries)

    @property
    def documents(self) -> list[Document]:
        """Marker order == list order. This is what `AnswerResult.documents` becomes."""
        return [entry.document for entry in self._entries]

    def marker_for(self, doc: Document) -> int | None:
        """The marker already assigned to `doc`, or None if it is not held."""
        return self._markers.get(_dedupe_key(doc))

    def __len__(self) -> int:
        return len(self._entries)

    def format_context(self) -> str:
        """The context block, numbered with the markers this ledger assigned.

        **Delegates to `pipeline.format_context`, so there is one renderer.** Two
        renderers would drift, and the drift would be invisible: the prompt would
        simply change shape halfway through a turn, and the only symptom would be
        a model citing slightly worse. The import is local -- see the module
        docstring for why.
        """
        from app.rag import pipeline

        return pipeline.format_context(
            self.documents, markers=[entry.marker for entry in self._entries]
        )


# --------------------------------------------------------------------------
# What one tool call produced
# --------------------------------------------------------------------------


@dataclass
class ToolInvocation:
    """One tool call, as the trace will record it.

    `pipeline.py` never touches the database and `TraceRecorder` is constructed
    in `ask.run_turn`, so the loop cannot write its own trace events. It
    accumulates them as plain data here instead and `run_turn` turns them into
    rows -- the same split that keeps every write of a turn inside one
    transaction.

    `detail` is spread into the `TOOL_RESULT` payload; `args` goes through
    `trace._jsonable`, which coerces anything unserialisable to `str` and never
    raises, so a 4 KB Python program in `args["code"]` lands in the payload
    intact. That is exactly what the trace panel should show.
    """

    step: int
    call_id: str
    tool: str
    args: dict[str, Any]
    ok: bool
    # One line, safe to render in a UI without further processing.
    summary: str
    detail: dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0
    error: str | None = None


@dataclass
class LoopResult:
    """Everything one bounded loop produced."""

    text: str
    tool_calls: list[ToolInvocation] = field(default_factory=list)
    artifacts: list[ToolArtifact] = field(default_factory=list)
    # Steps in which at least one tool actually ran. A model that answered
    # immediately reports 0, which is what makes "tools were on and it chose not
    # to use them" distinguishable from "tools were off".
    steps: int = 0
    # Time inside tools, and summed model time. Separate because the trace has to
    # ADD UP to the turn rather than overlap: `contextualize_ms + retrieval_ms +
    # tool_ms + generation_ms` is the whole turn, and it only is if a tool call's
    # seconds are never also counted as generation.
    tool_ms: int = 0
    generation_ms: int = 0
    # "max_steps" | "tool_error" | None
    stopped_reason: str | None = None


# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------


def _human_turn(ledger: ContextLedger, question: str) -> HumanMessage:
    """The single context-bearing human message, rebuilt from the ledger.

    `USER_TEMPLATE` is a `str.format` template that this project owns; values
    substituted into it are not re-parsed, so braces inside a retrieved chunk or
    inside the question are already safe. Local import: see the module docstring.
    """
    from app.rag.pipeline import USER_TEMPLATE

    return HumanMessage(
        content=USER_TEMPLATE.format(
            context=ledger.format_context(), question=question
        )
    )


def _unknown_tool_message(name: str, available: list[str]) -> str:
    """What the model is told when it calls a tool that does not exist.

    Names the real tools rather than only refusing, because the next attempt is
    then usually correct and the alternative costs a whole step.
    """
    return (
        f"There is no tool named {name!r}. "
        f"The tools available are: {', '.join(available)}. "
        "Call one of those, or answer from the context you already have."
    )


async def _execute(
    call: dict[str, Any],
    tools_by_name: dict[str, BaseTool],
    *,
    step: int,
    fallback_id: str,
) -> tuple[ToolInvocation, ToolMessage]:
    """Run one tool call. **Never raises.**

    A tool that raises would end the turn, and ending the turn is the one thing
    that must not happen: a model handed its own traceback fixes its code on the
    next step surprisingly often, and a model told "there is no tool called
    `search`" calls `search_corpus` instead. Both of those are worth more than a
    clean stack trace in a log nobody is reading.

    So everything -- an unknown tool, pydantic rejecting the arguments, the
    sandbox failing to start, an unexpected exception inside a tool -- comes back
    as `ok=False` plus a `ToolMessage` the model can read and correct from.
    """
    name = str(call.get("name") or "")
    raw_args = call.get("args")
    args = raw_args if isinstance(raw_args, dict) else {"__raw__": raw_args}
    call_id = str(call.get("id") or fallback_id)
    started = time.perf_counter()

    def elapsed() -> int:
        return int((time.perf_counter() - started) * 1000)

    def failed(error: str) -> tuple[ToolInvocation, ToolMessage]:
        invocation = ToolInvocation(
            step=step,
            call_id=call_id,
            tool=name or "unknown",
            args=args,
            ok=False,
            summary=error.splitlines()[0][:200] if error else "tool call failed",
            detail={},
            duration_ms=elapsed(),
            error=error,
        )
        return invocation, ToolMessage(
            content=error, tool_call_id=call_id, status="error"
        )

    tool = tools_by_name.get(name)
    if tool is None:
        # Registry order, not sorted: `build_tools` puts search first because
        # some models weight the first name they read, and a model that just
        # guessed a tool name is exactly the one being nudged.
        return failed(_unknown_tool_message(name, list(tools_by_name)))

    try:
        # Invoked with the tool-call dict rather than with bare arguments, so
        # langchain builds the `ToolMessage` and stamps `tool_call_id` itself.
        # `id` is normalised first: a provider that omits it would otherwise
        # produce a ToolMessage with a null id, and the NEXT request would carry
        # an assistant tool call with no matching result -- which most providers
        # reject outright, turning one odd response into a dead turn.
        message = await tool.ainvoke({**call, "id": call_id, "type": "tool_call"})
    except Exception as exc:  # noqa: BLE001 - see the docstring
        # Pydantic's own error text is what goes to the model here, and it is
        # usually enough: it names the missing or mistyped field.
        log.warning("Tool %s failed on step %d: %s", name, step, exc)
        return failed(f"{type(exc).__name__}: {exc}")

    # Every tool in the registry is built with
    # `response_format="content_and_artifact"`, so the second half of its return
    # value rides on `ToolMessage.artifact` -- the string goes to the model, the
    # structured outcome comes back here for the trace. A tool that returned
    # something else is a programming error rather than a turn failure, so it is
    # absorbed into a plausible outcome rather than raised on.
    outcome = message.artifact
    if not isinstance(outcome, ToolOutcome):
        outcome = ToolOutcome(ok=True, summary=str(message.content)[:200])

    if not outcome.ok:
        # A tool that reports failure without raising -- the sandbox path, where
        # a `SandboxResult(ok=False)` is a normal return carrying a traceback the
        # model is meant to read. It becomes a TOOL_ERROR row all the same.
        message.status = "error"

    invocation = ToolInvocation(
        step=step,
        call_id=call_id,
        tool=name,
        args=args,
        ok=outcome.ok,
        summary=outcome.summary,
        detail=dict(outcome.detail),
        duration_ms=elapsed(),
        error=outcome.error,
    )
    return invocation, message


def _invalid_call_message(
    call: dict[str, Any], *, step: int, fallback_id: str
) -> tuple[ToolInvocation, ToolMessage]:
    """Handle a tool call the provider could not parse into arguments.

    langchain puts these on `AIMessage.invalid_tool_calls` rather than on
    `tool_calls`, and they are easy to drop by accident -- but langchain-openai
    serialises BOTH lists back into the next request's `tool_calls` field, so an
    invalid call with no answering `ToolMessage` leaves the conversation
    malformed and the next invoke fails. Answering it costs nothing and keeps
    the loop alive.
    """
    call_id = str(call.get("id") or fallback_id)
    name = str(call.get("name") or "unknown")
    error = (
        f"Your call to {name} could not be parsed: {call.get('error') or 'malformed arguments'}. "
        "Send the arguments again as plain JSON."
    )
    invocation = ToolInvocation(
        step=step,
        call_id=call_id,
        tool=name,
        args={"__raw__": call.get("args")},
        ok=False,
        summary="malformed tool call",
        detail={},
        duration_ms=0,
        error=error,
    )
    return invocation, ToolMessage(content=error, tool_call_id=call_id, status="error")


async def _emit_tool_outcome(emit: events.Emit, invocation: ToolInvocation) -> None:
    """One `tool_result` or one `tool_error`, from a finished `ToolInvocation`.

    Two event names rather than one with an `ok` flag, mirroring the split
    `TOOL_RESULT` / `TOOL_ERROR` already makes in the trace and for the same
    reason: a failed tool call is not a failed turn, and a client that renders
    the two identically hides the single most interesting thing the loop does.

    **Neither frame is terminal.** A tool failure comes back to the model as a
    `ToolMessage` and never as an exception (`_execute` never raises), so the loop
    continues and the turn still ends in `done`. A client treating `tool_error` as
    the end of the stream would stop reading a turn that was about to succeed.
    """
    await emit(
        events.TOOL_RESULT if invocation.ok else events.TOOL_ERROR,
        {
            "step": invocation.step,
            "tool": invocation.tool,
            "ok": invocation.ok,
            "duration_ms": invocation.duration_ms,
            # Already one line and already safe to render -- for a failure
            # `_execute` sets it to the error's first line, truncated. The frame
            # deliberately does not carry `detail` or `args`: a 4 KB Python
            # program belongs in the trace row, which is durable, not on a wire
            # whose whole purpose is to arrive quickly.
            "summary": invocation.summary,
        },
    )


def _call_id(call: dict[str, Any], fallback: str) -> str:
    """The id a tool call will be answered under.

    Duplicated from `_execute` rather than returned by it, because the SSE
    `tool_call` frame has to go out *before* the call runs -- a tool event that
    only appears once the tool has finished is not progress, it is a receipt, and
    the whole reason these frames exist is the 1.6 s of silence a search costs.
    The two must agree: a frame carrying one id and a `ToolMessage` carrying
    another would make a client unable to pair a result with its call.
    """
    return str(call.get("id") or fallback)


async def _astream_message(
    runnable,
    messages: list[BaseMessage],
    emit: events.Emit,
    *,
    stream_tokens: bool,
) -> AIMessage:
    """One streamed model call, accumulated back into a single message.

    **The accumulated `AIMessageChunk` is what the rest of the loop then reads**
    -- `.tool_calls`, `.text`, and the object itself appended into `messages` and
    re-serialised into the next request. All three were verified against this
    route before this function was written, because "chunk merging is documented
    and probably fine" is exactly the reasoning CLAUDE.md records this project
    losing to three times. Measured 2026-08-16, `google/gemma-4-31b-it` through
    OpenRouter with `search_corpus` bound, `require_parameters` on and `top_k` in
    `extra_body`:

        forced tool call, streamed   5 chunks -> .tool_calls carried name, an
                                     args dict and an id, .text == ""
        that merged chunk fed back   18 chunks -> "The solar arrays deliver
        into a second request        12.4 kW at end of life."
        tool_choice="none"           18 chunks, text as normal

    A separate finding from the same probe, worth recording because it looks like
    a streaming bug and is not: `tool_choice="search_corpus"` is honoured only
    intermittently on this route -- 1 of 3 under `ainvoke` and 1 of 3 under
    `astream` on identical messages. That is provider variance in the same family
    as `tool_choice="any"` being ignored outright (CLAUDE.md, T4), it predates
    streaming, and the loop already handles it: the gap branch falls through and
    accepts the answer when the forced call produces nothing to execute.

    **A tool step emits no tokens, and the step's nature is settled on its first
    meaningful chunk** rather than at the end. Waiting for the end would defeat
    streaming; guessing would put a tool call's stray prose on screen as if it
    were the answer. The first chunk that carries either `tool_call_chunks` or
    non-empty text decides it, which costs nothing measurable -- in the probe the
    decision landed on chunk 1 every time.

    The residual case, stated rather than hidden: a model that emitted text and
    *then* a tool call would have had that text streamed already. `done.result`
    is authoritative and the client replaces its draft with it, so the turn
    self-heals; Gemma produced `text == ""` on every tool-call response measured
    above, so this has not been observed here.
    """
    accumulated: AIMessageChunk | None = None
    # None -> undecided, "text" -> streaming, "tool" -> suppressed for this call.
    mode: str | None = None
    # Every text piece emitted so far, so the markup sentinel can be found across
    # a chunk boundary. A special token is several characters and arrives split.
    emitted_text: list[str] = []

    async for chunk in runnable.astream(messages):
        accumulated = chunk if accumulated is None else accumulated + chunk

        if not stream_tokens:
            continue

        if mode is None:
            if getattr(chunk, "tool_call_chunks", None):
                mode = "tool"
                continue
            piece = getattr(chunk, "text", "") or ""
            if not piece:
                # Role-only or metadata-only opening chunk. Decides nothing.
                continue
            mode = "text"
            if _emit_until_markup(piece, emitted_text):
                await emit(events.TOKEN, {"text": piece})
            emitted_text.append(piece)
            continue

        if mode == "tool":
            continue

        piece = getattr(chunk, "text", "") or ""
        if piece:
            # **Stop emitting the moment leaked markup appears, and never resume.**
            # `_message_text` strips it from the stored answer, but that is too
            # late for a stream: the tokens are already on the user's screen, and
            # a correction that arrives after the read is not a correction. See
            # `_strip_leaked_tool_markup` for the measurement.
            if _emit_until_markup(piece, emitted_text):
                await emit(events.TOKEN, {"text": piece})
            emitted_text.append(piece)

    if accumulated is None:
        # A stream that yielded nothing at all. An empty AIMessage keeps every
        # caller's attribute access working -- `.tool_calls` is [], `.text` is ""
        # -- so the loop takes its normal-exit branch and `_message_text` logs the
        # empty answer, which is the same handling a non-streamed empty response
        # already gets.
        log.warning("Streamed model call yielded no chunks")
        return AIMessage(content="")
    return accumulated


async def run_agent_loop(
    *,
    agent: Agent,
    question: str,
    ledger: ContextLedger,
    system_prompt: str,
    model: BaseChatModel,
    max_steps: int,
    emit: events.Emit | None = None,
    follow_up: Sequence[BaseMessage] | None = None,
    force_first_tool: str | None = None,
) -> LoopResult:
    """Generate an answer, letting the model call tools first.

    `model` is the already-configured generation model -- `pipeline` builds it
    through `get_chat_model`, which goes through `llm.build_chat_model`, which is
    the only place a chat model is constructed in this project. **Nothing here
    adds a parameter to that request beyond `tools` and `tool_choice`**, and that
    restraint is load-bearing rather than tidy: `openrouter_require_parameters`
    is on, so every parameter in the request must be one some provider
    advertises, and 14 of the 19 endpoints serving `google/gemma-4-31b-it`
    advertise both `tools` and the `top_k` the request already carries in
    `extra_body`. That intersection is the routing headroom. A third parameter
    could empty it, and the failure is a `404 No endpoints found that can handle
    the requested parameters` -- a 404 on a model that plainly exists, which
    CLAUDE.md records this project hitting three separate times.

    `parallel_tool_calls` in particular is never sent: `build_chat_model` sets
    `disabled_params={"parallel_tool_calls": None}` precisely because
    langchain-openai binds it unasked and OpenRouter advertises no such
    parameter. Verified on the bound model -- `bind_tools([...])` puts exactly
    one key, `tools`, into the request.

    An OpenRouter 404 on the bound call **propagates**. It is a configuration
    fault rather than a turn fault, and swallowing it would hide exactly the
    failure mode above.

    `emit` is the SSE channel, and `None` is the whole of the off switch.
    **Streaming adds no parameter to the request** -- `stream` is a transport
    flag OpenRouter's routing filter does not consult (it appears in zero of the
    19 endpoints' `supported_parameters` and streaming works anyway, verified on
    this repo), so the 14-of-19 `tools` INTERSECT `top_k` headroom quoted above is
    unchanged by it. `ChatOpenAI(stream_usage=True)` would break that, because it
    injects an unadvertised `stream_options` key that has never been probed; it is
    never set here.

    With `emit is None` every model call below is the identical `.ainvoke(...)`
    line it was before streaming existed rather than a similar one, which is what
    keeps scenario S1 of `scripts/agentic_check.py` -- and every number already
    recorded in EVAL.md -- true structurally instead of by care.

    **`follow_up` and `force_first_tool` are the REDRAFT seam, and both default
    to None so nothing changes without them.** The self-check
    (`app/rag/selfcheck.py`) needs to re-run generation over the ledger it
    already has, carrying the draft it is rejecting and the critic's complaint
    about it, and it needs the option of making the model search before it
    rewrites. That is this loop's job in every particular -- bounded steps,
    `ToolMessage` observations, a forced final answer, the same streaming -- so
    the alternative was a second, thinner loop in `pipeline.py` that would drift
    from this one on the first change to either. Two arguments buy the reuse:

    * `follow_up` is appended after the context turn, so the ledger rebuild at
      `messages[1]` below still lands on the right message.
    * `force_first_tool` makes only the FIRST step's runnable name a tool,
      through the same `bind_tools(tools, tool_choice=...)` the gap trigger uses.
      A NAMED tool, never `"any"`, which is silently ignored on this route.
      Subsequent steps go back to `bound`, so the budget is not spent forcing.

    Neither adds a parameter to the request: `tool_choice` is already present on
    every call this loop makes, so the 14-of-19 routing headroom measured for
    `google/gemma-4-31b-it` is untouched (T5).
    """
    ctx = ToolContext(agent=agent, ledger=ledger)
    tools = build_tools(ctx)
    tools_by_name = {tool.name: tool for tool in tools}
    bound = model.bind_tools(tools)
    # Built once, and only when asked for. `bound` itself is the object every
    # pre-existing caller uses on every step, which is what keeps the default
    # path the identical call it always was.
    first_bound = (
        bound
        if force_first_tool is None
        else model.bind_tools(tools, tool_choice=force_first_tool)
    )

    messages: list[BaseMessage] = [
        SystemMessage(content=system_prompt + TOOL_GUIDANCE),
        _human_turn(ledger, question),
    ]
    if follow_up:
        messages.extend(follow_up)

    invocations: list[ToolInvocation] = []
    generation_ms = 0
    tool_ms = 0
    steps = 0
    consecutive_failed_steps = 0
    gap_search_used = False
    # Whether a `search_corpus` call has already run and returned this turn.
    # Read only by the gap trigger, to answer the question that trigger is
    # actually asking -- see the comment there.
    corpus_searched = False
    stopped_reason: str | None = None

    # The step whose generation produced the text currently on the client's
    # screen. Read only by the gap branch, to decide whether a retraction has
    # anything to retract.
    streamed_text_step: int | None = None

    for step in range(1, max(max_steps, 0) + 1):
        t0 = time.perf_counter()
        # `bound` on every step unless a caller forced the first one -- see
        # `force_first_tool`. With it unset this expression IS `bound`.
        runnable = first_bound if step == 1 else bound
        if emit is None:
            ai = await runnable.ainvoke(messages)
            generation_ms += int((time.perf_counter() - t0) * 1000)
        else:
            # **The per-step call is streamed, not only the two structurally
            # final ones.** The common tool-enabled turn takes the normal exit on
            # step 1 with no tool calls -- that is what `agentic_check.py` S2
            # asserts -- so streaming only the classic path and the forced final
            # call would ship a feature that emits zero tokens on the majority of
            # turns while the connection opens and nothing throws. That is the
            # error-shaped pass `new features/loop.md` T2 exists to forbid: the
            # test "did the stream open?" stays green while the thing wanted did
            # not happen. The price is `answer_reset` below, bounded to at most
            # once per turn by `gap_search_used`.
            await events.phase(
                emit, events.PHASE_GENERATE, events.STARTED, step=step
            )
            ai = await _astream_message(runnable, messages, emit, stream_tokens=True)
            elapsed = int((time.perf_counter() - t0) * 1000)
            generation_ms += elapsed
            # `.text` directly rather than `_message_text`, which logs a warning
            # on an empty answer -- and an empty `.text` is the NORMAL shape of a
            # tool step (measured: every tool-call response on this route came
            # back with `text == ""`), so routing it through the logger would
            # print a warning on the loop's most ordinary path.
            streamed_text_step = step if (getattr(ai, "text", "") or "") else None
            await events.phase(
                emit,
                events.PHASE_GENERATE,
                events.FINISHED,
                duration_ms=elapsed,
                step=step,
            )
        messages.append(ai)

        calls = list(getattr(ai, "tool_calls", None) or [])
        invalid = list(getattr(ai, "invalid_tool_calls", None) or [])

        if not calls and not invalid:
            # ---------------------------------------------------------------
            # The gap trigger
            # ---------------------------------------------------------------
            # Before accepting an answer that admits it does not know something,
            # make the model look once.
            #
            # This exists because prompting alone demonstrably does not work.
            # Measured 2026-08-16 against `google/gemma-4-31b-it` with one chunk
            # of context and a two-part question, `search_corpus` bound:
            #
            #   tool_choice="auto"             -> no tool call, answered half,
            #                                     declared the other half missing
            #   bare prompt, no refusal rule   -> no tool call
            #   "You MUST call search_corpus"  -> no tool call
            #   tool_choice="search_corpus"    -> called it, correctly
            #
            # So the plumbing is fine and the model simply will not INITIATE a
            # search on its own judgement. It calls `run_python` readily, because
            # "draw me a chart" is an instruction; noticing a gap and deciding to
            # go looking is a judgement, and this model does not make it. Note in
            # particular that `tool_choice="any"` was ignored too -- only a NAMED
            # tool forces a call on this route, which is its own trap, because a
            # dropped "required" is indistinguishable from a model that chose not
            # to call.
            #
            # Hence a trigger rather than more prompt. `detect_gap` is the same
            # marker list `queries.refused` uses, applied without the position
            # rules (see `app/rag/refusal.py` for why those two questions differ)
            # -- if the text says something is missing, that is the signal.
            #
            # This is PRD open item 7's score-triggered loop, with a trigger that
            # works. The specified one compares `top_score` against
            # `score_threshold`, and CLAUDE.md's measurement kills it: on-topic
            # questions scored 0.61-0.67 and off-topic 0.49-0.58, so 0.5 sits
            # inside the overlap. The model's own statement that it lacks
            # something is a far better signal than a threshold in the noise --
            # it is read off the text rather than off a distribution.
            #
            # Fired at most ONCE per turn (`gap_search_used`), and only while a
            # step remains, so the worst case is a single extra retrieval on a
            # turn that was going to refuse anyway. That cost buys a strictly
            # better refusal too: "I searched and it is not there" is a stronger
            # claim than "it was not in the chunk I happened to be given".
            #
            # -------------------------------------------------------------
            # `not corpus_searched` -- added 2026-08-16 with the DeepSeek swap
            # -------------------------------------------------------------
            # The table above is a fact about Gemma, not about the world, and
            # `settings.generation_model` now points at
            # `deepseek/deepseek-v4-flash-0731`, which self-initiated a search in
            # 6/6 trials on the same probe. So the premise this branch was written
            # under -- "no tool calls this step" implies "never searched" -- is
            # false for the default model. Without this gate, the ordinary shape of
            # a CORRECT refusal on the new model is:
            #
            #   step 1  model searches, finds nothing
            #   step 2  model answers "the corpus does not cover X"
            #   ...     detect_gap fires, and forces the SAME search again
            #
            # That is a guaranteed wasted retrieval on every correct refusal, plus
            # a nudge inviting the model to re-answer a question it had already
            # answered correctly.
            #
            # Restating it as `loop.md` T2 does makes the gate obvious rather than
            # clever. The outcome this trigger wants is **"the model searched
            # before it declined"**, never "an admission appeared in the text". If
            # a search has already run, the outcome HAS occurred and there is
            # nothing to trigger. The old condition was a proxy that was exact only
            # while the model never searched.
            #
            # Residual risk, stated rather than hidden: a turn could search for
            # topic A and then admit a gap about topic B, and this gate suppresses
            # the second search. Accepted, on the measurement that this model emits
            # 1.50-2.00 calls per step and covers both halves of a two-part
            # question in one step (probe 2, 8/8 steps). Revisit it if a scenario
            # ever shows a real single-topic search followed by a different gap.
            #
            # The branch is NOT dead code on the default model: `tools_enabled`
            # agents whose `generation_model` names Gemma still reach it, which is
            # exactly the configuration S13 pins.
            gap = detect_gap(_message_text(ai))
            if gap and not gap_search_used and not corpus_searched and step < max_steps:
                gap_search_used = True
                steps = step
                ctx.step = step
                messages.append(
                    HumanMessage(content=GAP_NUDGE.format(marker=gap))
                )
                t1 = time.perf_counter()
                # A NAMED tool, not "any". "any" is silently ignored here.
                forced_runnable = model.bind_tools(tools, tool_choice=SEARCH_CORPUS)
                if emit is None:
                    forced = await forced_runnable.ainvoke(messages)
                else:
                    # Streamed for transport symmetry, with tokens suppressed:
                    # this call exists to produce a tool call, and any prose it
                    # emits alongside one is about to be discarded with the rest
                    # of the step. `stream_tokens=False` keeps that text off the
                    # screen instead of showing it and retracting it a moment
                    # later, which is the failure mode `answer_reset` is already
                    # the minimum acceptable amount of.
                    forced = await _astream_message(
                        forced_runnable, messages, emit, stream_tokens=False
                    )
                generation_ms += int((time.perf_counter() - t1) * 1000)
                messages.append(forced)
                forced_calls = list(getattr(forced, "tool_calls", None) or [])

                # **The retraction goes out HERE: after the forced call is known
                # to have produced something, and before that something runs.**
                #
                # Both halves of that are load-bearing and they pull in opposite
                # directions. It cannot fire when the gap is merely detected,
                # because `tool_choice=SEARCH_CORPUS` is honoured only
                # intermittently on this route (measured 1 of 3 under both
                # `ainvoke` and `astream`) -- and when the forced call comes back
                # empty the loop falls through below and the answer the user is
                # reading STANDS. Wiping the screen for a turn whose text was
                # about to be kept is the worse error of the two.
                #
                # And it cannot wait until the `continue`, because the search sits
                # in between: the reader would watch a complete-looking answer,
                # then "Searched the corpus", and only then be told the answer had
                # been withdrawn -- the events in the wrong causal order, which
                # reads as the search having caused nothing.
                #
                # Suppressed when this step streamed no text. There is no draft to
                # withdraw, and a client told to clear an empty buffer would render
                # the explanatory copy for a wipe the user never saw.
                if forced_calls and emit is not None and streamed_text_step == step:
                    await emit(
                        events.ANSWER_RESET,
                        {"reason": "gap_detected", "marker": gap},
                    )
                    streamed_text_step = None

                for position, call in enumerate(forced_calls):
                    if emit is not None:
                        await emit(
                            events.TOOL_CALL,
                            {
                                "step": step,
                                "tool": str(call.get("name") or "unknown"),
                                "call_id": _call_id(call, f"gap_{step}_{position}"),
                                # Set here and null on a model-chosen call, so a
                                # reader does not credit the model with a decision
                                # the code made. Same distinction the TOOL_CALL
                                # trace payload draws, for the same reason.
                                "trigger": "gap_detected",
                            },
                        )
                    invocation, message = await _execute(
                        call,
                        tools_by_name,
                        step=step,
                        fallback_id=f"gap_{step}_{position}",
                    )
                    if emit is not None:
                        await _emit_tool_outcome(emit, invocation)
                    # Marked so the trace can tell a search the model chose from
                    # one the loop insisted on. Without it, a reader would credit
                    # the model with a decision the code made.
                    invocation.args = {**invocation.args, "trigger": "gap_detected"}
                    invocation.detail = {**invocation.detail, "trigger": "gap_detected"}
                    tool_ms += invocation.duration_ms
                    invocations.append(invocation)
                    messages.append(message)
                if forced_calls:
                    messages[1] = _human_turn(ledger, question)
                    continue
                # The forced call produced nothing to execute. Fall through and
                # accept the answer rather than spending another step.

            # The normal exit, and the common one. An agent with tools on that
            # had no reason to search returns here on step 1 with the same answer
            # the classic path would have produced from the same context.
            return LoopResult(
                text=_message_text(ai),
                tool_calls=invocations,
                artifacts=ctx.artifacts,
                steps=steps,
                tool_ms=tool_ms,
                generation_ms=generation_ms,
                stopped_reason=None,
            )

        steps = step
        # Tools stamp the artifacts they produce with the step that produced
        # them, and a closure has no other way to know which step it is running
        # in. Set before execution, never read after.
        ctx.step = step

        step_ok = 0
        # **Sequential, never `asyncio.gather`.** langchain-openai is not asking
        # for parallel calls at all (see `disabled_params` above), so a batch of
        # more than one is already unusual; and `run_python` spawns a subprocess
        # on a deployment with a single uvicorn worker, where two at once is two
        # interpreters' worth of memory competing for one core. Sequential is
        # both the correct shape and the safe one.
        for position, call in enumerate(calls):
            if emit is not None:
                # Before `_execute`, deliberately. The durable TOOL_CALL row is
                # still written after the turn by `ask.run_turn` -- the frame and
                # the row are two renderings of one `ToolInvocation` and only the
                # row is durable -- but a frame that waited for the row's timing
                # would arrive after the 1.6 s of silence it exists to explain.
                await emit(
                    events.TOOL_CALL,
                    {
                        "step": step,
                        "tool": str(call.get("name") or "unknown"),
                        "call_id": _call_id(call, f"call_{step}_{position}"),
                        # Null: the model chose this one.
                        "trigger": None,
                    },
                )
            invocation, message = await _execute(
                call,
                tools_by_name,
                step=step,
                fallback_id=f"call_{step}_{position}",
            )
            if emit is not None:
                await _emit_tool_outcome(emit, invocation)
            tool_ms += invocation.duration_ms
            invocations.append(invocation)
            messages.append(message)
            step_ok += 1 if invocation.ok else 0
            # A search that returned zero results still counts as searched, and
            # deliberately: `search_corpus` reports `ok=True` with "0 results
            # above the retrieval floor" precisely because finding nothing is an
            # answer. That is the case the gap trigger must NOT then re-run.
            if invocation.tool == SEARCH_CORPUS and invocation.ok:
                corpus_searched = True

        for position, call in enumerate(invalid):
            invocation, message = _invalid_call_message(
                call, step=step, fallback_id=f"invalid_{step}_{position}"
            )
            if emit is not None:
                # A malformed call is still a call the model made, and it is
                # answered with a `ToolMessage` like any other failure -- so it
                # gets the same pair of frames rather than being silently absent
                # from a timeline that then appears to skip a step.
                await emit(
                    events.TOOL_CALL,
                    {
                        "step": step,
                        "tool": invocation.tool,
                        "call_id": invocation.call_id,
                        "trigger": None,
                    },
                )
                await _emit_tool_outcome(emit, invocation)
            invocations.append(invocation)
            messages.append(message)

        # A search may have added context. **Rebuild the human turn rather than
        # appending a second one**: two context blocks in one conversation means
        # one of them is stale, and the model has no way to tell which. Rebuilding
        # keeps exactly one, always reflecting the ledger.
        messages[1] = _human_turn(ledger, question)

        if step_ok:
            consecutive_failed_steps = 0
        else:
            consecutive_failed_steps += 1
            if consecutive_failed_steps >= MAX_CONSECUTIVE_FAILED_STEPS:
                stopped_reason = "tool_error"
                break
    else:
        # The budget was spent with the model still wanting to call tools.
        stopped_reason = "max_steps"

    # Budget spent, or the model stopped converging. Force an answer.
    #
    # **`tool_choice="none"` with the tools still bound, not a bare model.**
    # `tool_choice` is a parameter OpenRouter routes on; dropping `tools`
    # entirely on the last call would change the routing constraint mid-turn and
    # risk a different provider answering than the one that made the calls. One
    # parameter set for the whole turn is the property worth keeping.
    t0 = time.perf_counter()
    final_runnable = model.bind_tools(tools, tool_choice="none")
    if emit is None:
        final = await final_runnable.ainvoke(messages)
        generation_ms += int((time.perf_counter() - t0) * 1000)
    else:
        # `steps + 1`: this generation is not one of the tool steps, it is the
        # one after the last of them, and numbering it `steps` would put two
        # `generate` pairs on the wire under one step number for two different
        # model calls.
        final_step = steps + 1
        await events.phase(
            emit, events.PHASE_GENERATE, events.STARTED, step=final_step
        )
        final = await _astream_message(
            final_runnable, messages, emit, stream_tokens=True
        )
        elapsed = int((time.perf_counter() - t0) * 1000)
        generation_ms += elapsed
        await events.phase(
            emit,
            events.PHASE_GENERATE,
            events.FINISHED,
            duration_ms=elapsed,
            step=final_step,
        )

    return LoopResult(
        text=_message_text(final),
        tool_calls=invocations,
        artifacts=ctx.artifacts,
        steps=steps,
        tool_ms=tool_ms,
        generation_ms=generation_ms,
        stopped_reason=stopped_reason,
    )


# A model's own tool-call markup, arriving in the CONTENT channel instead of in
# `tool_calls`, where the user then reads it.
#
# Measured 2026-08-16 in the browser, `deepseek/deepseek-v4-flash-0731`, on a turn
# that spent its whole step budget. When the budget runs out the loop re-invokes
# with `tool_choice="none"` (see the forced final answer below) -- and this model
# still WANTED to search, so it expressed that the only way left to it:
#
#     Let me try searching for the thermal rejection budget document ...
#     <|DSML|tool_calls> <|DSML|invoke name="search_corpus"> ...
#
# (with U+FF5C FULLWIDTH VERTICAL LINE, not ASCII `|` -- DeepSeek's special-token
# delimiter, which is exactly why it survives every provider-side parser that
# looks for the ASCII form.)
#
# **Nothing raised. The turn succeeded. The user got machinery in their answer.**
# `new features/loop.md` T2 in a place nobody had looked: the assertion "did the
# turn produce an answer" was true, and `agentic_check.py` S6 asserts precisely
# that -- `bool(out.answer)` -- so the suite stayed green through it.
#
# Truncating from the first sentinel rather than excising a matched block is
# deliberate. The markup is a *continuation* the model never finished, so there is
# no reliable closing token to match, and anything after the sentinel is machinery
# by construction. Prose before it is kept, because it usually reads as a normal
# closing sentence.
#
# Scoped to the sentinel rather than to `<` so that a legitimate answer discussing
# HTML or XML is untouched: U+FF5C does not appear in English prose, and does not
# appear in this project's corpora at all.
_LEAKED_TOOL_MARKUP = re.compile("<｜")


def _emit_until_markup(piece: str, emitted: list[str]) -> bool:
    """Whether this streamed piece may still go to the screen.

    False once leaked markup has appeared, and false for every piece after it --
    a stream cannot be un-read, so the gate latches rather than filtering.

    **Checks the JOIN of everything so far, not the piece.** `<｜` is two
    characters and a stream splits wherever the provider's buffer happened to
    end, so a sentinel arriving as `"<"` then `"｜"` is invisible to any per-chunk
    test. That is the same class of bug as `sentences()` in `refusal.py` refusing
    to split on a lone newline: generated text is chunked by the transport, and a
    matcher that assumes the transport's boundaries are meaningful will miss on a
    schedule nobody can reproduce.

    The cost of the join is one string build per token on a path that is already
    doing an HTTP write per token.

    Residual, stated rather than hidden: if a chunk ends exactly on the `<`, that
    one character has already been written and cannot be recalled -- only the
    `｜` and everything after it is suppressed. Holding a one-character tail back
    on every token would fix it and would delay every legitimate answer's last
    character behind the next chunk. A stray `<` is the cheaper defect.
    """
    if _LEAKED_TOOL_MARKUP.search("".join(emitted)):
        return False
    return not _LEAKED_TOOL_MARKUP.search("".join(emitted) + piece)


def _strip_leaked_tool_markup(text: str) -> str:
    """Text up to the first leaked special-token sentinel.

    Returns the input unchanged when there is none, which is every turn on a
    model whose markup the provider parses properly -- including every Gemma turn
    this project ever took.
    """
    match = _LEAKED_TOOL_MARKUP.search(text)
    if match is None:
        return text
    log.warning(
        "Stripped leaked tool-call markup from a model answer (%d chars removed).",
        len(text) - match.start(),
    )
    return text[: match.start()].rstrip()


def _message_text(message: AIMessage | BaseMessage) -> str:
    """The answer text, however the provider shaped the content.

    `.text` is a property in langchain-core 1.x (it was a method in 0.x, and
    calling it still works through a deprecation shim -- do not). The fallback
    exists because a provider returning structured content blocks would give an
    empty string here, and an empty answer is worth logging rather than
    returning silently.

    Leaked tool-call markup is stripped here -- see `_strip_leaked_tool_markup`.
    This is the single place the loop reads text off a message, which is why the
    strip belongs here rather than at the three call sites.
    """
    text = _strip_leaked_tool_markup(getattr(message, "text", "") or "")
    if not text:
        log.warning(
            "Agent loop produced no text; content type was %s",
            type(getattr(message, "content", None)).__name__,
        )
    return text
