"""The four mechanisms that exist because a model will not do the right thing alone.

These are AGENT callbacks (`LlmAgent(before_model_callback=[...])`) rather than
`BasePlugin`s. Both work; agent callbacks are positional, accept lists, and keep
the whole interception layer readable in one file. A plugin would additionally
register on the `Runner`, which is a second place the order could be changed.

**ORDER MATTERS AND IS DECLARED IN `build_callbacks` AT THE BOTTOM.**

  before_model:  step_budget  ->  context_block
  after_model:   text_guard   ->  gap_trigger

`step_budget` runs first because `context_block` must not rebuild a context turn
for a call the budget has already decided is the final one. `text_guard` runs
before `gap_trigger` because the trigger reads the answer text, and it must read
the text a USER would see -- running `detect_gap` over unstripped DSML markup
would let the model's own machinery decide whether a search happens.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from app.adk.context import AdkTurnContext
from app.rag import events
from app.rag.refusal import detect_gap
from app.rag.textguard import _strip_leaked_tool_markup
from app.tools.corpus import SEARCH_CORPUS

log = logging.getLogger("uvicorn.error")


def _response_text(response: LlmResponse) -> str:
    if not response.content or not response.content.parts:
        return ""
    return "".join(part.text for part in response.content.parts if part.text)


def _function_calls(response: LlmResponse) -> list[types.FunctionCall]:
    if not response.content or not response.content.parts:
        return []
    return [part.function_call for part in response.content.parts if part.function_call]


# --------------------------------------------------------------------------
# before_model
# --------------------------------------------------------------------------


def make_step_budget(ctx: AdkTurnContext, *, max_steps: int):
    """Count model invocations and close the loop when the budget is spent.

    **On exhaustion this sets `tool_config` to NONE and LEAVES `config.tools`
    POPULATED.** The obvious ADK move -- `llm_request.config.tools = []` -- is a
    silent behaviour change on the most delicate wire property in the project:
    `tools` is a parameter OpenRouter ROUTES on, so dropping it mid-turn changes
    the routing constraint and risks a different provider answering than the one
    that made the calls. The langchain runtime binds tools with
    `tool_choice="none"` for exactly this reason, and `adk_model_check` A8 asserts
    the pair rather than trusting the comment.

    `RunConfig.max_llm_calls` is deliberately NOT the budget: it raises
    `LlmCallsLimitExceededError` out of `run_async` mid-iteration, turning a
    routine budget exhaustion into a 500. It stays at a generous default as a
    runaway net, and any occurrence of it is a hard bug signal.
    """

    def step_budget(*, callback_context: Any, llm_request: LlmRequest) -> LlmResponse | None:
        ctx.step += 1

        if ctx.forcing:
            # The gap-forced call. Narrow to the single named tool for THIS call
            # only. On this route `tool_choice="any"` is accepted and silently
            # ignored -- only a NAMED tool forces a call -- and `app/adk/model.py`
            # maps a single `allowed_function_names` entry to exactly that.
            declarations = [
                d
                for tool in (llm_request.config.tools or [])
                for d in (tool.function_declarations or [])
                if d.name == SEARCH_CORPUS
            ]
            if declarations:
                llm_request.config.tools = [types.Tool(function_declarations=declarations)]
            llm_request.config.tool_config = types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(
                    mode=types.FunctionCallingConfigMode.ANY,
                    allowed_function_names=[SEARCH_CORPUS],
                )
            )
            return None

        if ctx.step > max_steps:
            ctx.stopped_reason = ctx.stopped_reason or "max_steps"
            llm_request.config.tool_config = types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(
                    mode=types.FunctionCallingConfigMode.NONE
                )
            )
        return None

    return step_budget


def make_context_block(ctx: AdkTurnContext, *, question: str):
    """Rebuild the single context-bearing user turn from the ledger.

    The langchain runtime does `messages[1] = _human_turn(ledger, question)` after
    every tool step, so exactly one context block ever exists. ADK keeps the
    original user message in the session and appends tool results after it, so
    without this the model would read the context as it stood BEFORE its own
    searches -- it would search, receive markers, and then be shown a context that
    does not contain them.

    **Rebuilt, never appended to.** Two context blocks in one conversation means
    one of them is stale, and the model has no way to tell which.
    """

    def context_block(*, callback_context: Any, llm_request: LlmRequest) -> LlmResponse | None:
        if ctx.ledger is None:
            return None
        from app.rag.agent_loop import _human_turn

        rebuilt = _human_turn(ctx.ledger, question)
        text = rebuilt.content if isinstance(rebuilt.content, str) else str(rebuilt.content)

        for content in llm_request.contents or []:
            if content.role != "user":
                continue
            parts = content.parts or []
            # The first user content carrying TEXT is the context turn. A user
            # content carrying a `function_response` is a tool result and must be
            # left alone -- overwriting one would delete the search the model just
            # ran, which is the exact opposite of what this callback is for.
            if any(p.function_response for p in parts):
                continue
            if not any(p.text for p in parts):
                continue
            content.parts = [types.Part(text=text)]
            break
        return None

    return context_block


# --------------------------------------------------------------------------
# after_model
# --------------------------------------------------------------------------


def make_text_guard(ctx: AdkTurnContext):
    """Keep the model's own tool-call markup off the user's screen.

    Deliberately does NOT gate on `llm_response.partial`. The sentinel is two
    characters (U+FF5C follows `<`) and a stream splits wherever the provider's
    buffer happened to end, so a per-fragment matcher misses it on a schedule
    nobody can reproduce. `app/adk/events.py` owns the streaming latch; this owns
    the STORED text.
    """

    def text_guard(*, callback_context: Any, llm_response: LlmResponse) -> LlmResponse | None:
        if llm_response.partial:
            return None
        if not llm_response.content or not llm_response.content.parts:
            return None
        changed = False
        parts: list[types.Part] = []
        for part in llm_response.content.parts:
            if part.text:
                cleaned = _strip_leaked_tool_markup(part.text)
                if cleaned != part.text:
                    changed = True
                parts.append(types.Part(text=cleaned) if cleaned else types.Part(text=""))
            else:
                parts.append(part)
        if not changed:
            return None
        llm_response.content.parts = parts
        return llm_response

    return text_guard


def make_gap_trigger(ctx: AdkTurnContext, *, max_steps: int, search_query: str):
    """Make the model look once before accepting an answer that admits a gap.

    ------------------------------------------------------------------
    THIS IS THE MOST EXPENSIVE FINDING IN THE PROJECT, AND EVERY PLAUSIBLE
    FIX EXCEPT THIS ONE IS A PROMPT EDIT.

    Measured against `google/gemma-4-31b-it` with `search_corpus` bound: full
    persona prompt -> no tool call; bare prompt with no grounding rule -> no tool
    call; "You MUST call search_corpus" -> no tool call; `tool_choice="any"` -> no
    tool call; a NAMED tool -> called it, correctly.

    The cause is structural rather than a weak model: every system prompt here is
    refusal-first by design, and a model drilled to treat a gap in its context as
    a cue to DECLINE will do exactly that when handed a tool for FILLING gaps.
    Weakening the grounding rule would trade a hallucination-free system for a
    tool-happy one.
    ------------------------------------------------------------------

    Four gates, all of which must hold:
      1. `detect_gap` matched the answer text
      2. the trigger has not already fired this turn (`gap_fired`)
      3. no search has run this turn (`corpus_searched`)
      4. a step remains

    Gate 3 is the one added with the DeepSeek swap and it is not an optimisation.
    The new default model self-initiates a search 6/6, so "no tool calls this
    step" no longer implies "never searched", and without the gate the ordinary
    shape of a CORRECT refusal earns a guaranteed wasted retrieval plus a nudge
    inviting the model to re-answer a question it had already answered correctly.
    Stated as an outcome rather than a proxy: the trigger wants *"the model
    searched before it declined"*, and a search having run means that has already
    happened.

    **The synthetic fallback is what ADK buys that langchain could not.**
    `tool_choice=<named tool>` is honoured on this route roughly 1 time in 3
    (measured, under both `ainvoke` and `astream`), and the langchain runtime can
    only fall through and accept the answer when the forced call comes back empty.
    Here, returning an `LlmResponse` carrying a synthetic `function_call` makes ADK
    dispatch the search ITSELF, without asking the provider anything -- so a fired
    trigger always executes exactly one search, deterministically, at the cost of
    zero further model calls.

    That is a genuine behaviour change and it is recorded rather than glossed:
    EVAL baselines were all measured under the intermittent behaviour, so a
    cross-runtime scorecard comparison needs a re-baseline first.
    """

    def gap_trigger(*, callback_context: Any, llm_response: LlmResponse) -> LlmResponse | None:
        if llm_response.partial:
            # A fragment is not an answer. Running the detector over one would let
            # a half-written `"I don't kn"` trip a marker.
            return None
        if _function_calls(llm_response):
            # **The force WORKED. Clear it here.**
            # Leaving `forcing` set was a real bug caught by `adk_loop_check` 16:
            # the model made the forced call, the tool ran, and then the NEXT
            # response -- the ordinary answer -- was read as "declined the forced
            # call" and earned a second, synthetic search. Two retrievals, one
            # gap, and an answer built from a script that had run off its end.
            ctx.forcing = False
            return None

        text = _response_text(llm_response)

        if ctx.forcing:
            # The forced call came back with no tool call -- the ~2-in-3 case.
            # Synthesise one. `ctx.forcing` is cleared here so the model's NEXT
            # invocation is a normal answering call rather than another forced
            # one, which is what stops this being a loop.
            ctx.forcing = False
            ctx.gap_synthetic = True
            log.info(
                "Gap trigger: model declined the forced call; dispatching a "
                "synthetic search for %r.",
                search_query,
            )
            return LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part(
                            function_call=types.FunctionCall(
                                id=f"gap-{uuid.uuid4().hex[:12]}",
                                name=SEARCH_CORPUS,
                                args={"query": search_query},
                            )
                        )
                    ],
                ),
            )

        gap = detect_gap(text)
        if not gap or ctx.gap_fired or ctx.corpus_searched or ctx.step >= max_steps:
            return None

        ctx.gap_fired = True
        ctx.forcing = True
        ctx.gap_phase = True
        # The retracted draft, not the forced call's own preamble. This is the
        # text that ADMITTED the gap and is the reason the search happens at all;
        # a trajectory that dropped it would show a search with no motive.
        ctx.gap_draft_text = text
        ctx.gap_marker = gap
        log.info("Gap trigger fired on marker %r at step %d.", gap, ctx.step)

        # Returning None lets this answer stand as the invocation's result; the
        # LOOP sees `gap_fired` and runs a second, forced invocation. Doing it
        # there rather than here keeps the model composing its own narrowed query
        # -- which is what GAP_NUDGE ("search for the MISSING part, not the whole
        # question") exists to shape, and which a synthetic call cannot do.
        return None

    return gap_trigger


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------


def build_callbacks(
    ctx: AdkTurnContext, *, max_steps: int, question: str, search_query: str
) -> dict[str, list]:
    """The callbacks, in the one order that is correct. See the module docstring."""
    return {
        "before_model_callback": [
            make_step_budget(ctx, max_steps=max_steps),
            make_context_block(ctx, question=question),
        ],
        "after_model_callback": [
            make_text_guard(ctx),
            make_gap_trigger(ctx, max_steps=max_steps, search_query=search_query),
        ],
    }
