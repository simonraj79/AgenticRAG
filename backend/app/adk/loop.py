"""`run_agent_loop_adk` -- the ADK twin of `run_agent_loop`.

**IDENTICAL signature, IDENTICAL return type.** That is the whole safety argument
of this change set: `pipeline.answer_question`, `ask.run_turn`, the four trace row
types, `api_usage`, the eval job and the Ragas scorecard all sit above this
function and read a `LoopResult`. Nothing above the seam is edited, so nothing
above the seam can regress -- structurally, not carefully.

------------------------------------------------------------------
WHY THERE ARE TWO RUNNER INVOCATIONS AND NOT ONE.

ADK's `Runner` owns its own loop: model -> tools -> model -> ... until a response
carries no function call. That covers the ordinary turn completely, and the step
budget is enforced from `before_model_callback`.

The gap trigger cannot live inside it. It fires *after* the model has produced a
final answer, and its whole mechanism is to re-ask with a NAMED tool forced --
which is a new request, not a continuation. So the shape is:

    invocation 1   the ordinary bounded turn
    (gap detected, four gates pass)
    invocation 2   GAP_NUDGE as a new user turn, tools narrowed to search_corpus,
                   tool_config=ANY. If the model declines anyway, the trigger
                   synthesises the call and ADK dispatches it.

Both invocations share ONE session, so invocation 2 sees the whole history --
including the draft it is about to retract, which is what lets the model narrow
its query to the missing part rather than re-asking the whole question.
------------------------------------------------------------------

The session service is in-memory and created per turn, then discarded. ADK's
`DatabaseSessionService` exists and is deliberately not used: this project already
persists conversation history in `conversations`/`messages` and reconstructs a
turn's messages from there, so a second durable session store would be a second
source of truth for the same fact.

This function writes nothing to the database. It accumulates plain data, exactly
as `run_agent_loop` does, and `ask.run_turn` turns it into rows in one transaction.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.models.base_llm import BaseLlm
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.adk.context import AdkTurnContext
from app.adk.model import OpenRouterAdkLlm
from app.adk.plugins import build_callbacks
from app.adk.tools import build_adk_tools
from app.config import settings
from app.rag import events
from app.rag.textguard import _emit_until_markup

log = logging.getLogger("uvicorn.error")

_APP_NAME = "groundwork"

# A generous ceiling that is NOT the step budget. `RunConfig.max_llm_calls` raises
# `LlmCallsLimitExceededError` out of `run_async` mid-iteration, which would turn a
# routine budget exhaustion into a 500 -- so the real budget is enforced in
# `before_model_callback` and this stays a runaway-loop net. Any occurrence is a
# hard bug signal, never a normal turn.
_RUNAWAY_CEILING = 32


async def _drive(
    *,
    runner: Runner,
    session_id: str,
    message: types.Content,
    ctx: AdkTurnContext,
    model: OpenRouterAdkLlm,
    emit: events.Emit | None,
) -> str:
    """One runner invocation. Returns the final answer text.

    Owns the streaming translation, and the two halves of it are the two things
    `agent_loop.py`'s streaming path had no harness for:

    - **Only `partial=True` events become TOKEN frames.** ADK delivers the text
      twice -- once as fragments, once as a `partial=False` aggregate carrying the
      whole string. Emitting both renders the answer twice.
    - **The markup latch spans fragments.** `_emit_until_markup` checks the JOIN of
      everything emitted so far, because `<` and U+FF5C arrive in separate chunks
      often enough to matter and a stream cannot be un-read.
    """
    answer = ""
    emitted: list[str] = []
    step_open: int | None = None
    started = time.perf_counter()
    # Snapshot, so this invocation is charged only its OWN model time. `getattr`
    # rather than an attribute access because a harness stub is a bare `BaseLlm`
    # and must not be required to implement our bookkeeping to be drivable.
    model_ms_before = getattr(model, "total_call_ms", 0)

    run_config = RunConfig(
        streaming_mode=StreamingMode.SSE if emit is not None else StreamingMode.NONE,
        max_llm_calls=_RUNAWAY_CEILING,
    )

    async for event in runner.run_async(
        user_id="turn",
        session_id=session_id,
        new_message=message,
        run_config=run_config,
    ):
        parts = event.content.parts if event.content and event.content.parts else []
        partial = bool(getattr(event, "partial", False))

        # A generate phase per step, opened lazily on the first event that belongs
        # to a new step. The langchain runtime emits these around each model call;
        # here the step number is only knowable once the budget callback has
        # incremented it.
        if emit is not None and ctx.step != step_open and ctx.step > 0:
            if step_open is not None:
                await events.phase(
                    emit,
                    events.PHASE_GENERATE,
                    events.FINISHED,
                    duration_ms=int((time.perf_counter() - started) * 1000),
                    step=step_open,
                )
            await events.phase(emit, events.PHASE_GENERATE, events.STARTED, step=ctx.step)
            step_open = ctx.step
            started = time.perf_counter()

        for part in parts:
            if part.function_call and emit is not None:
                await emit(
                    events.TOOL_CALL,
                    {
                        "step": ctx.step,
                        "tool": str(part.function_call.name or "unknown"),
                        "call_id": part.function_call.id or f"adk_{ctx.step}",
                        # Set on a forced call and null on a model-chosen one, so
                        # a reader does not credit the model with a decision the
                        # code made.
                        "trigger": "gap_detected" if ctx.gap_fired and ctx.forcing else None,
                    },
                )
            if not part.text:
                continue
            if partial:
                if emit is not None and _emit_until_markup(part.text, emitted):
                    await emit(events.TOKEN, {"text": part.text})
                emitted.append(part.text)
            else:
                # The aggregate. Authoritative for the STORED answer and never
                # re-emitted as tokens.
                answer = part.text

    if emit is not None and step_open is not None:
        await events.phase(
            emit,
            events.PHASE_GENERATE,
            events.FINISHED,
            duration_ms=int((time.perf_counter() - started) * 1000),
            step=step_open,
        )

    ctx.generation_ms += getattr(model, "total_call_ms", 0) - model_ms_before
    if emitted and not answer:
        # Streamed fragments arrived but no aggregate did. Prefer the text the
        # user actually read over an empty answer.
        answer = "".join(emitted)
    return answer


async def run_agent_loop_adk(
    *,
    agent: Any,
    question: str,
    ledger: Any,
    system_prompt: str,
    model: Any = None,
    max_steps: int,
    emit: events.Emit | None = None,
    follow_up: Sequence[Any] | None = None,
    force_first_tool: str | None = None,
) -> Any:
    """Generate an answer, letting the model call tools first. ADK edition.

    `model` is accepted and IGNORED when it is a langchain chat model, because the
    two runtimes construct their model differently and `pipeline` builds a
    langchain one unconditionally. The signature keeps it so the two callables
    remain drop-in interchangeable -- `app/rag/runtime.LoopCallable` is that
    contract, and a signature that diverged would make the selector a lie.

    `force_first_tool` is honoured: `pipeline`'s self-check redraft passes
    `SEARCH_CORPUS` when the critic suggested a query, and dropping it would
    silently turn a forced search into an optional one.
    """
    from app.rag.agent_loop import GAP_NUDGE, TOOL_GUIDANCE, LoopResult, _human_turn

    ctx = AdkTurnContext(agent=agent, ledger=ledger)
    tools = build_adk_tools(ctx)

    # **Any `BaseLlm` is accepted, not only ours, and the narrower check was a
    # real bug caught by `adk_loop_check` case 3b.** Testing
    # `isinstance(model, OpenRouterAdkLlm)` silently DISCARDED a scripted stub and
    # built a live model in its place -- so an offline harness made a billed
    # OpenRouter call and returned a fluent answer. Nothing raised. A case
    # asserting only "an answer was produced" would have printed green forever
    # while testing nothing and spending money.
    #
    # The langchain chat model `pipeline` passes is NOT a `BaseLlm`, so it still
    # falls through to the builder below, which is the intended behaviour.
    adk_model = model if isinstance(model, BaseLlm) else None
    if adk_model is None:
        from app.adk.model import build_adk_model

        adk_model = build_adk_model(getattr(agent, "generation_model", None) or None)

    callbacks = build_callbacks(
        ctx, max_steps=max_steps, question=question, search_query=question
    )
    llm_agent = LlmAgent(
        name="groundwork_agent",
        model=adk_model,
        # **`system_prompt + TOOL_GUIDANCE`, exactly as `agent_loop.py:862` does,
        # and omitting it was a real defect caught by `adk_parity_check`.**
        #
        # The first version passed the persona alone. The turn still looked
        # excellent -- three searches, a correct refusal, the persona's pedagogy
        # intact -- and the only visible symptom was that the model cited
        # `[power-subsystem.md#2.0]` instead of `[1]`, because TOOL_GUIDANCE is
        # what asks for the `[n]` form. `ask.normalise_citation_markers` resolves
        # both, so even that produced no error.
        #
        # What was actually missing is far worse than a citation style.
        # CLAUDE.md's S16 measurement: `GENERATION_REASONING` is false, and that
        # is only affordable because TOOL_GUIDANCE's FINAL PARAGRAPH survives --
        # the two are redundant with each other, either alone holds tool use at
        # 6/6, and removing BOTH drops it to 2/6 with nothing raising. This
        # runtime had removed one of them silently.
        #
        # APPENDED, never prepended: every persona opens with "GROUNDING COMES
        # FIRST. It outranks every instruction below", and a preamble would bury
        # that sentence under a second one. A specialist prompt REPLACES the
        # persona upstream in `pipeline`, so nothing is stacked here beyond this.
        instruction=system_prompt + TOOL_GUIDANCE,
        tools=tools,
        # Sub-agent transfer is off. There are no sub-agents, and leaving the
        # machinery enabled hands the model a `transfer_to_agent` tool it can call
        # -- a tool whose failure mode is a turn that produces nothing.
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
        **callbacks,
    )

    session_service = InMemorySessionService()
    runner = Runner(
        app_name=_APP_NAME,
        agent=llm_agent,
        session_service=session_service,
    )
    session = await session_service.create_session(app_name=_APP_NAME, user_id="turn")

    if force_first_tool:
        # The self-check redraft's forced search. Same mechanism the gap trigger
        # uses on its second invocation.
        ctx.forcing = True

    opening = _human_turn(ledger, question) if ledger is not None else None
    opening_text = (
        opening.content
        if opening is not None and isinstance(opening.content, str)
        else question
    )
    parts = [types.Part(text=opening_text)]
    for message in follow_up or []:
        text = getattr(message, "content", "") or getattr(message, "text", "")
        if isinstance(text, str) and text.strip():
            parts.append(types.Part(text=text))

    answer = await _drive(
        runner=runner,
        session_id=session.id,
        message=types.Content(role="user", parts=parts),
        ctx=ctx,
        model=adk_model,
        emit=emit,
    )

    # ---------------------------------------------------------------- gap
    if ctx.gap_fired and ctx.forcing:
        # **The retraction goes out here: after the gap is known and before the
        # search runs.** It cannot wait until the search returns, or the reader
        # watches a complete-looking answer, then "Searched the corpus", and only
        # then learns the answer was withdrawn -- the events in the wrong causal
        # order. Suppressed when nothing was streamed, because a client told to
        # clear an empty buffer renders the copy for a wipe the user never saw.
        if emit is not None and answer:
            await emit(
                events.ANSWER_RESET,
                {"reason": "gap_detected", "marker": ctx.gap_marker},
            )
        forced_answer = await _drive(
            runner=runner,
            session_id=session.id,
            message=types.Content(
                role="user",
                parts=[types.Part(text=GAP_NUDGE.format(marker=ctx.gap_marker))],
            ),
            ctx=ctx,
            model=adk_model,
            emit=emit,
        )
        if forced_answer:
            answer = forced_answer
    ctx.forcing = False
    ctx.gap_phase = False

    # `tool_steps` is derived from what actually RAN, never from the model-call
    # counter. `LoopResult.steps` is documented as "steps in which at least one
    # tool actually ran" -- it is the field that tells "tools were on and the
    # model chose not to use them" from "tools were off", and crediting a step to
    # a forced call the model declined would collapse exactly that distinction.
    ctx.tool_steps = len({inv.step for inv in ctx.invocations})

    return LoopResult(
        text=answer,
        tool_calls=ctx.invocations,
        artifacts=ctx.drain_artifacts(),
        steps=ctx.tool_steps,
        tool_ms=ctx.tool_ms,
        generation_ms=ctx.generation_ms,
        stopped_reason=ctx.stopped_reason,
    )
