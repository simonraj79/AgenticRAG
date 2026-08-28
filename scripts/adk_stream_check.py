"""Layer 1 harness for the ADK runtime's STREAMING path. No DB, no network, no model.

WHY THIS IS A DELIVERABLE AND NOT A SIDE EFFECT.

The streaming half of `app/rag/agent_loop.py` has **no harness case at all**.
`agent_loop_check` runs entirely with `emit=None`, so chunk accumulation, the
per-step token suppression on a tool step, the empty-stream fallback, the per-step
generate pairs and the `answer_reset` ordering are, today, unasserted in either
runtime. Only the two pure functions `_emit_until_markup` and
`_strip_leaked_tool_markup` are pinned, by `refusal_check` 27-33.

That is precisely the gap the DSML leak came through: every harness green, and the
model's own `<|DSML|tool_calls>` markup on the user's screen. The assertion that
would have caught it is about frames, and nothing was emitting frames into a test.

ADK adds a hazard the langchain runtime does not have. In SSE mode the text
arrives TWICE -- once as `partial=True` fragments, and once as a `partial=False`
aggregate carrying the whole string. Emit both and the answer renders twice; meter
both and the bill doubles; run the gap detector on a fragment and a half-written
`"I don't kn"` trips a marker.

Cases (`new features/18-adk-runtime/PLAN.md` section 5):
  S1  a tool step emits ZERO token frames
  S2  one generate started/finished pair per step, in order
  S4  an empty stream -> empty answer, normal exit, no exception
  S5  a sentinel SPLIT ACROSS two fragments is caught, and the latch holds
  S7  no empty token frames
  S8  the partial=False aggregate is NOT re-emitted as tokens
  S9  emit=None -> zero frames and a LoopResult equal to the streamed one

    backend/.venv/Scripts/python.exe scripts/adk_stream_check.py

ASCII in print(), exit 1 on any failure -- `new features/06-test-plan.md`.
"""

from __future__ import annotations

import asyncio
import sys
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
from app.rag import events  # noqa: E402

# The real sentinel, imported rather than retyped. U+FF5C is invisible in a diff
# and a hand-typed ASCII pipe would make this file pass while testing nothing.
from app.rag.textguard import _LEAKED_TOOL_MARKUP  # noqa: E402

failures: list[str] = []
checks = 0


def check(label: str, ok: bool, detail: str = "", *, always: bool = False) -> None:
    global checks
    checks += 1
    show = detail if (detail and (always or not ok)) else ""
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" -- {show}" if show else ""))
    if not ok:
        failures.append(label)


class StreamingScriptedLlm(BaseLlm):
    """Replays turns, honouring `stream` the way `OpenRouterAdkLlm` does.

    Each scripted turn is `(fragments, tool_calls)`. Streaming yields one
    `partial=True` response per fragment and then ONE `partial=False` aggregate
    carrying the join -- which is the exact shape the real adapter produces and
    the shape every plugin gates on.
    """

    model: str = "scripted/stream"
    script: list = []
    calls: list = []
    streamed: list = []

    async def generate_content_async(self, llm_request: LlmRequest, stream: bool = False):
        index = len(self.calls)
        self.calls.append(index)
        self.streamed.append(stream)
        fragments, tool_calls = (
            self.script[index] if index < len(self.script) else ([], [])
        )

        if stream:
            for fragment in fragments:
                yield LlmResponse(
                    content=types.Content(role="model", parts=[types.Part(text=fragment)]),
                    partial=True,
                )

        parts: list[types.Part] = []
        joined = "".join(fragments)
        if joined:
            parts.append(types.Part(text=joined))
        for position, (name, args) in enumerate(tool_calls):
            parts.append(
                types.Part(
                    function_call=types.FunctionCall(
                        id=f"s{index}_{position}", name=name, args=args
                    )
                )
            )
        yield LlmResponse(
            content=types.Content(role="model", parts=parts or [types.Part(text="")]),
            partial=False,
        )


class _Args(BaseModel):
    query: str = Field(description="q")


class _PyArgs(BaseModel):
    code: str = Field(description="c")
    purpose: str = Field(description="p")


def stub_tools(_ctx):
    async def _search(query: str):
        return "chunk text", registry.ToolOutcome(ok=True, summary="ok", detail={})

    async def _run_python(code: str, purpose: str):
        return "ok", registry.ToolOutcome(ok=True, summary="ran", detail={})

    return [
        StructuredTool.from_function(
            coroutine=_search, name="search_corpus", description="s",
            args_schema=_Args, response_format="content_and_artifact",
        ),
        StructuredTool.from_function(
            coroutine=_run_python, name="run_python", description="p",
            args_schema=_PyArgs, response_format="content_and_artifact",
        ),
    ]


async def drive(script, *, streaming: bool = True, max_steps: int = 3):
    """Run one turn, capturing every emitted frame."""
    from app.rag.agent_loop import ContextLedger
    from app.adk.loop import run_agent_loop_adk

    frames: list[tuple[str, dict]] = []

    async def emit(name: str, payload: dict) -> None:
        frames.append((name, dict(payload)))

    original = registry.build_tools
    registry.build_tools = stub_tools
    try:
        model = StreamingScriptedLlm(script=list(script), calls=[], streamed=[])
        result = await run_agent_loop_adk(
            agent=SimpleNamespace(id="a", namespace="n", generation_model=None),
            question="What is X?",
            ledger=ContextLedger(),
            system_prompt="Ground every claim.",
            model=model,
            max_steps=max_steps,
            emit=emit if streaming else None,
        )
        return result, frames, model
    finally:
        registry.build_tools = original


def tokens(frames) -> list[str]:
    return [payload["text"] for name, payload in frames if name == events.TOKEN]


async def main() -> int:
    print("adk_stream_check -- the streaming path, which had no harness in either runtime")
    print("")

    # --- S8 / S7 ---------------------------------------------------------
    print("S7/S8 -- the aggregate must not be re-emitted as tokens")
    result, frames, model = await drive([(["Hello ", "world", "!"], [])])
    emitted = tokens(frames)
    check("S8 fragments were streamed", emitted == ["Hello ", "world", "!"], str(emitted))
    check(
        "S8 the concatenation equals the answer EXACTLY, not twice",
        "".join(emitted) == result.text,
        f"joined={''.join(emitted)!r} answer={result.text!r}",
        always=True,
    )
    check(
        "S8 the answer is not doubled",
        result.text == "Hello world!",
        repr(result.text),
    )
    check("S7 no empty token frames", all(t for t in emitted), str(emitted))
    check("S8 the model was asked to STREAM", model.streamed and model.streamed[0] is True,
          str(model.streamed))
    print("")

    # --- S1 --------------------------------------------------------------
    print("S1 -- a tool step emits zero token frames")
    result, frames, model = await drive(
        [
            ([], [("search_corpus", {"query": "x"})]),   # tool step: no text at all
            (["The ", "answer ", "[1]."], []),
        ]
    )
    emitted = tokens(frames)
    check(
        "S1 only the ANSWER step produced tokens",
        "".join(emitted) == "The answer [1].",
        str(emitted),
    )
    check("S1 the tool did run", len(result.tool_calls) == 1, str(len(result.tool_calls)))
    tool_frames = [n for n, _ in frames if n == events.TOOL_CALL]
    check("S1 a tool_call frame was emitted", tool_frames == [events.TOOL_CALL], str(tool_frames))
    print("")

    # --- S2 --------------------------------------------------------------
    print("S2 -- generate phases pair up, started before finished")
    phases = [
        (p["name"], p["status"], p.get("step"))
        for n, p in frames
        if n == events.PHASE and p.get("name") == events.PHASE_GENERATE
    ]
    started = [p for p in phases if p[1] == events.STARTED]
    finished = [p for p in phases if p[1] == events.FINISHED]
    check("S2 at least one generate pair", bool(started), str(phases))
    check("S2 every started has a finished", len(started) == len(finished),
          f"started={len(started)} finished={len(finished)}", always=True)
    check(
        "S2 the first frame is a started, not a finished",
        bool(phases) and phases[0][1] == events.STARTED,
        str(phases[:2]),
    )
    check(
        "S2 a finished frame carries a duration",
        all(
            p.get("duration_ms") is not None
            for n, p in frames
            if n == events.PHASE
            and p.get("name") == events.PHASE_GENERATE
            and p.get("status") == events.FINISHED
        ),
    )
    print("")

    # --- S5 --------------------------------------------------------------
    print("S5 -- a sentinel split across two fragments")
    # `<` and U+FF5C arrive in SEPARATE fragments. Any per-fragment matcher misses
    # this, which is the whole reason `_emit_until_markup` checks the JOIN.
    sentinel = _LEAKED_TOOL_MARKUP.pattern           # "<｜", imported not retyped
    result, frames, model = await drive(
        [([
            "The array generates 4.2 kW [1]. ",
            sentinel[0],                              # "<"
            sentinel[1] + "DSML" + sentinel[1] + "tool_calls",   # the rest
            " invoke name=search_corpus",
         ], [])]
    )
    emitted = tokens(frames)
    joined = "".join(emitted)
    check(
        "S5 the prose before the sentinel was emitted",
        joined.startswith("The array generates 4.2 kW [1]."),
        repr(joined[:50]),
    )
    check(
        "S5 nothing after the split sentinel reached the stream",
        "DSML" not in joined and "tool_calls" not in joined,
        repr(joined),
        always=True,
    )
    check(
        "S5 the latch HELD -- the trailing fragment was suppressed too",
        "invoke name" not in joined,
        repr(joined[-40:]),
    )
    check(
        "S5 and the STORED answer is stripped as well",
        _LEAKED_TOOL_MARKUP.search(result.text) is None,
        repr(result.text[-40:]),
    )
    print("")

    # --- S4 --------------------------------------------------------------
    print("S4 -- an empty stream exits normally")
    result, frames, model = await drive([([], [])])
    check("S4 no exception, a LoopResult came back", result is not None)
    check("S4 the answer is empty rather than absent", result.text == "", repr(result.text))
    check("S4 stopped_reason is None", result.stopped_reason is None, str(result.stopped_reason))
    check("S4 no token frames", tokens(frames) == [], str(tokens(frames)))
    print("")

    # --- S9 --------------------------------------------------------------
    print("S9 -- emit=None is the whole of the off switch")
    script = [([], [("search_corpus", {"query": "x"})]), (["Answer ", "[1]."], [])]
    streamed_result, streamed_frames, streamed_model = await drive(script, streaming=True)
    silent_result, silent_frames, silent_model = await drive(script, streaming=False)

    check("S9 emit=None produced zero frames", silent_frames == [], str(len(silent_frames)))
    check(
        "S9 emit=None did NOT ask the model to stream",
        silent_model.streamed and not any(silent_model.streamed),
        str(silent_model.streamed),
        always=True,
    )
    check(
        "S9 the streamed run DID ask the model to stream",
        any(streamed_model.streamed),
        str(streamed_model.streamed),
    )
    for field in ("text", "steps", "stopped_reason"):
        check(
            f"S9 LoopResult.{field} identical with and without streaming",
            getattr(streamed_result, field) == getattr(silent_result, field),
            f"streamed={getattr(streamed_result, field)!r} "
            f"silent={getattr(silent_result, field)!r}",
        )
    check(
        "S9 the same tools ran either way",
        [i.tool for i in streamed_result.tool_calls] == [i.tool for i in silent_result.tool_calls],
        f"{[i.tool for i in streamed_result.tool_calls]} vs "
        f"{[i.tool for i in silent_result.tool_calls]}",
    )
    print("")

    if failures:
        print(f"FAILED {len(failures)} of {checks}:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"all {checks} adk stream checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
