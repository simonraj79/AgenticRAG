"""Layer 1 harness for `app/adk/loop.py`. No DB, no network, no model.

The sibling of `scripts/agent_loop_check.py`, and it exists for the same reason
that file does: the loop is the most decision-heavy function in the runtime, and
every assertion about it that has to go through `agentic_check.py` costs a live
turn against a database, a Pinecone namespace and two providers. The questions
nobody can afford are the ones that go unasked.

Everything is scripted: a `BaseLlm` that replays a list of responses, stub
langchain tools patched in over `app.tools.registry.build_tools`, and an agent
that is a `SimpleNamespace`. `run_agent_loop_adk` reads nothing off `agent` except
to hand it to the tool context, so the whole loop is hermetic.

Cases (ids follow `new features/18-adk-runtime/PLAN.md` section 5):
  3b  no tool call -> immediate return, stopped_reason is None, steps == 0
  10  ToolInvocation.content is the string the model READ, clipped by the SETTING
  14  two calls in one step do not overlap in time (the ledger marker race)
  15  budget exhausted -> stopped_reason "max_steps" AND an answer is still produced
  16  the gap trigger fires at most once per turn
  17  the gap trigger is suppressed when a search already ran (corpus_searched)
  18  a tool that RAISES does not abort the invocation
  1b  the model declines the forced call -> a synthetic call is dispatched anyway
  20  a normal turn's tool call is not stamped with a gap trigger

    backend/.venv/Scripts/python.exe scripts/adk_loop_check.py

ASCII in print(), exit 1 on any failure -- `new features/06-test-plan.md`.
"""

from __future__ import annotations

import asyncio
import sys
import time
import warnings
from pathlib import Path
from types import SimpleNamespace

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from google.adk.models.base_llm import BaseLlm  # noqa: E402
from google.adk.models.llm_request import LlmRequest  # noqa: E402
from google.adk.models.llm_response import LlmResponse  # noqa: E402
from google.genai import types  # noqa: E402
from langchain_core.tools import StructuredTool  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

import app.tools.registry as registry  # noqa: E402
from app.config import settings  # noqa: E402

failures: list[str] = []
checks = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" -- {detail}" if detail else ""))
    if not ok:
        failures.append(label)


# ---------------------------------------------------------------- the model
class ScriptedLlm(BaseLlm):
    """Replays a list of `(text, [(tool, args)])` turns and records requests."""

    model: str = "scripted/test"
    script: list = []
    calls: list = []
    requests: list = []

    async def generate_content_async(self, llm_request: LlmRequest, stream: bool = False):
        self.requests.append(llm_request)
        index = len(self.calls)
        self.calls.append(index)
        text, tool_calls = self.script[index] if index < len(self.script) else ("done.", [])
        parts: list[types.Part] = []
        if text:
            parts.append(types.Part(text=text))
        for position, (name, args) in enumerate(tool_calls):
            parts.append(
                types.Part(
                    function_call=types.FunctionCall(
                        id=f"c{index}_{position}", name=name, args=args
                    )
                )
            )
        yield LlmResponse(content=types.Content(role="model", parts=parts))


# ---------------------------------------------------------------- the tools
TIMELINE: list[tuple[str, float]] = []


class _Args(BaseModel):
    query: str = Field(description="q")


class _PyArgs(BaseModel):
    code: str = Field(description="c")
    purpose: str = Field(description="p")


def make_stub_tools(*, payload: str = "R" * 50, raises: bool = False, ok: bool = True):
    """Stub langchain tools with the real `content_and_artifact` contract."""

    async def _search(query: str):
        TIMELINE.append(("enter", time.perf_counter()))
        await asyncio.sleep(0.02)
        TIMELINE.append(("exit", time.perf_counter()))
        if raises:
            raise RuntimeError("stub tool exploded")
        return payload, registry.ToolOutcome(
            ok=ok, summary=f'stub for "{query}"', detail={"returned": 1, "query": query}
        )

    async def _run_python(code: str, purpose: str):
        return "ok", registry.ToolOutcome(ok=True, summary="ran", detail={"purpose": purpose})

    def build(_ctx):
        return [
            StructuredTool.from_function(
                coroutine=_search,
                name="search_corpus",
                description="search",
                args_schema=_Args,
                response_format="content_and_artifact",
            ),
            StructuredTool.from_function(
                coroutine=_run_python,
                name="run_python",
                description="python",
                args_schema=_PyArgs,
                response_format="content_and_artifact",
            ),
        ]

    return build


async def drive(script, *, max_steps=3, payload="R" * 50, raises=False, ok=True, question="What is X?"):
    """Run one turn against a scripted model, with the tool registry patched."""
    from app.rag.agent_loop import ContextLedger
    from app.adk.loop import run_agent_loop_adk

    TIMELINE.clear()
    original = registry.build_tools
    registry.build_tools = make_stub_tools(payload=payload, raises=raises, ok=ok)
    try:
        model = ScriptedLlm(script=list(script), calls=[], requests=[])
        agent = SimpleNamespace(id="a1", namespace="agent_a1", generation_model=None)
        result = await run_agent_loop_adk(
            agent=agent,
            question=question,
            ledger=ContextLedger(),
            system_prompt="Ground every claim.",
            model=model,
            max_steps=max_steps,
        )
        return result, model
    finally:
        registry.build_tools = original


# ---------------------------------------------------------------- cases
async def main() -> int:
    print("adk_loop_check -- the ADK runtime, offline")
    print("")

    # --- 3b -------------------------------------------------------------
    print("3b -- no tool call ends the turn immediately")
    result, model = await drive([("The answer is 42 [1].", [])])
    check("3b answer returned", result.text == "The answer is 42 [1].", repr(result.text))
    check("3b stopped_reason is None", result.stopped_reason is None, str(result.stopped_reason))
    check("3b steps == 0 (no tool ran)", result.steps == 0, str(result.steps))
    check("3b exactly one model call", len(model.calls) == 1, str(len(model.calls)))
    check("3b no tool invocations", result.tool_calls == [], str(result.tool_calls))
    print("")

    # --- 20 / 10 --------------------------------------------------------
    print("20/10 -- a model-chosen tool call")
    result, model = await drive(
        [("", [("search_corpus", {"query": "power budget"})]), ("Found it [1].", [])]
    )
    check("20 tool ran", len(result.tool_calls) == 1, str(len(result.tool_calls)))
    check("20 steps == 1", result.steps == 1, str(result.steps))
    inv = result.tool_calls[0]
    check("20 NOT stamped as gap-triggered", "trigger" not in inv.args, str(inv.args))
    check("10 content is what the model read", inv.content == "R" * 50, f"{len(inv.content)} chars")
    check("10 content differs from summary", inv.content != inv.summary)
    check("20 final answer is the second turn", result.text == "Found it [1].", repr(result.text))
    check("20 tool_ms recorded", result.tool_ms > 0, f"{result.tool_ms}ms")

    # content clipping follows the SETTING, not a literal
    limit = settings.trajectory_max_tool_content_chars
    result, _ = await drive(
        [("", [("search_corpus", {"query": "q"})]), ("done", [])], payload="X" * (limit + 500)
    )
    check(
        "10 content clipped to the configured limit",
        len(result.tool_calls[0].content) == limit,
        f"{len(result.tool_calls[0].content)} vs {limit}",
    )
    print("")

    # --- 14 -------------------------------------------------------------
    print("14 -- two calls in one step do not overlap")
    result, _ = await drive(
        [
            (
                "",
                [
                    ("search_corpus", {"query": "a"}),
                    ("search_corpus", {"query": "b"}),
                ],
            ),
            ("Both [1][2].", []),
        ]
    )
    check("14 both calls ran", len(result.tool_calls) == 2, str(len(result.tool_calls)))
    overlapped = False
    spans = [(TIMELINE[i][1], TIMELINE[i + 1][1]) for i in range(0, len(TIMELINE) - 1, 2)]
    for i in range(len(spans) - 1):
        if spans[i][1] > spans[i + 1][0]:
            overlapped = True
    check("14 dispatch was sequential (no overlap)", not overlapped, f"spans={len(spans)}")
    print("")

    # --- 15 -------------------------------------------------------------
    print("15 -- budget exhaustion still answers")
    result, model = await drive(
        [
            ("", [("search_corpus", {"query": "1"})]),
            ("", [("search_corpus", {"query": "2"})]),
            ("", [("search_corpus", {"query": "3"})]),
            ("Forced answer [1].", []),
        ],
        max_steps=2,
    )
    check("15 stopped_reason is max_steps", result.stopped_reason == "max_steps", str(result.stopped_reason))
    check("15 an answer was still produced", bool(result.text), repr(result.text))
    # The exhausted call must keep `tools` populated with tool_choice none.
    last = model.requests[-1]
    fcc = last.config.tool_config.function_calling_config if last.config.tool_config else None
    check(
        "15 exhausted call sets mode NONE",
        fcc is not None and fcc.mode == types.FunctionCallingConfigMode.NONE,
        str(fcc.mode if fcc else None),
    )
    check(
        "15 exhausted call KEEPS tools populated (routing constraint)",
        bool(last.config.tools),
        f"tools={len(last.config.tools or [])}",
    )
    print("")

    # --- 16 / 17 --------------------------------------------------------
    print("16/17 -- the gap trigger")
    # A gap admission with NO prior search: the trigger must fire.
    result, model = await drive(
        [
            ("The provided text does not mention the vendor.", []),
            ("", [("search_corpus", {"query": "vendor"})]),
            ("The vendor is Acme [1].", []),
        ]
    )
    check("16 trigger fired (a search ran)", len(result.tool_calls) == 1, str(len(result.tool_calls)))
    check(
        "16 the forced call is stamped gap_detected",
        result.tool_calls[0].args.get("trigger") == "gap_detected",
        str(result.tool_calls[0].args),
    )
    check(
        "16 detail carries the trigger too",
        result.tool_calls[0].detail.get("trigger") == "gap_detected",
        str(result.tool_calls[0].detail),
    )
    check(
        "16 assistant_text is the RETRACTED draft",
        "does not mention" in result.tool_calls[0].assistant_text,
        repr(result.tool_calls[0].assistant_text[:60]),
    )
    check("16 the answer is the redraft", result.text == "The vendor is Acme [1].", repr(result.text))

    # Fires at most once -- asserted against the CALLBACK, not the loop.
    #
    # **Mutation testing forced this rewrite twice, and the second finding is the
    # honest one.** The first version scripted three gap admissions end-to-end and
    # passed with `gap_fired` deleted, because `corpus_searched` suppressed the
    # second force instead. The second version made the search FAIL so
    # `corpus_searched` stayed false -- and it STILL passed, because
    # `run_agent_loop_adk` enters its gap block exactly once per turn, so the
    # once-per-turn property is guaranteed by the LOOP STRUCTURE and the gate is
    # defence in depth behind it.
    #
    # That is worth stating rather than papering over: an end-to-end case cannot
    # distinguish "the gate works" from "the loop made the gate unreachable", so
    # it cannot be the assertion. Driving the callback directly can, and does --
    # deleting `ctx.gap_fired` from the condition turns 16c red.
    from app.adk.context import AdkTurnContext
    from app.adk.plugins import make_gap_trigger

    probe = AdkTurnContext(agent=SimpleNamespace(id="p", namespace="n"), ledger=None)
    trigger = make_gap_trigger(probe, max_steps=5, search_query="q")

    def gap_response():
        return LlmResponse(
            content=types.Content(
                role="model",
                parts=[types.Part(text="The provided text does not mention the vendor.")],
            )
        )

    probe.step = 1
    trigger(callback_context=None, llm_response=gap_response())
    first_fired = probe.forcing
    # Simulate the forced call having been made and the phase closing, exactly as
    # the loop does, then present a SECOND gap admission.
    probe.forcing = False
    probe.step = 2
    trigger(callback_context=None, llm_response=gap_response())
    second_fired = probe.forcing

    check("16c the trigger fires on the first gap admission", first_fired is True)
    check(
        "16c the trigger does NOT fire on a second admission (gap_fired gate)",
        second_fired is False,
        f"forcing={second_fired}",
    )

    # And the end-to-end outcome, which is what the product actually promises.
    result, model = await drive(
        [
            ("The provided text does not mention the vendor.", []),
            ("", [("search_corpus", {"query": "vendor"})]),
            ("The provided text does not mention it either.", []),
            ("The provided text still does not mention it.", []),
        ],
        ok=False,
        max_steps=5,
    )
    searches = [i for i in result.tool_calls if i.tool == "search_corpus"]
    check(
        "16 at most ONE gap-triggered search per turn, even when it FAILED",
        len([i for i in searches if i.args.get("trigger") == "gap_detected"]) <= 1,
        f"searches={len(searches)}",
    )
    check(
        "16 a failed search does NOT set corpus_searched",
        all(i.ok is False for i in searches),
        str([i.ok for i in searches]),
    )

    # 17: a search already ran, so a later gap admission must NOT force another.
    result, model = await drive(
        [
            ("", [("search_corpus", {"query": "vendor"})]),
            ("The provided text does not mention the vendor.", []),
        ]
    )
    check(
        "17 suppressed when corpus_searched (exactly one search)",
        len([i for i in result.tool_calls if i.tool == "search_corpus"]) == 1,
        str(len(result.tool_calls)),
    )
    check(
        "17 the correct refusal stands as the answer",
        "does not mention" in result.text,
        repr(result.text[:60]),
    )
    print("")

    # --- 1b -------------------------------------------------------------
    print("1b -- the model declines the forced call; ADK dispatches it anyway")
    result, model = await drive(
        [
            ("The provided text does not mention the vendor.", []),
            ("I still will not search.", []),   # declines the forced call
            ("Answer after the synthetic search [1].", []),
        ]
    )
    searches = [i for i in result.tool_calls if i.tool == "search_corpus"]
    check(
        "1b a synthetic search was dispatched despite the refusal",
        len(searches) == 1,
        f"searches={len(searches)}",
    )
    check(
        "1b the synthetic call is stamped gap_detected",
        bool(searches) and searches[0].args.get("trigger") == "gap_detected",
        str(searches[0].args if searches else None),
    )
    print("")

    # --- 18 -------------------------------------------------------------
    print("18 -- a raising tool does not abort the invocation")
    result, model = await drive(
        [("", [("search_corpus", {"query": "boom"})]), ("Recovered [1].", [])],
        raises=True,
    )
    check("18 the invocation completed", bool(result.text), repr(result.text))
    check("18 the failure was recorded", len(result.tool_calls) == 1, str(len(result.tool_calls)))
    check("18 recorded as not-ok", result.tool_calls[0].ok is False, str(result.tool_calls[0].ok))
    check(
        "18 the model could READ the error",
        "exploded" in (result.tool_calls[0].content or ""),
        repr((result.tool_calls[0].content or "")[:60]),
    )
    print("")

    if failures:
        print(f"FAILED {len(failures)} of {checks}:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"all {checks} adk loop checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
