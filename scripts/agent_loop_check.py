"""Layer 1 harness for `app/rag/agent_loop.py`. No DB, no network, no model.

WHY THIS FILE EXISTS.

`run_agent_loop` is the most decision-heavy function in the project and, until
2026-08-23, **no harness anywhere drove it**. `grep run_agent_loop scripts/`
returned nothing; `grep tool_steps scripts/*.py` returned only config lines. Every
assertion about the loop was made through `agentic_check.py`, which needs a
database, a Pinecone namespace and two providers -- so the cheapest question about
the loop cost a live turn, and the questions nobody could afford went unasked.

The specific thing it pins, and why it is worth a file:

`LoopResult.steps` is documented at agent_loop.py:413-416 as **"steps in which at
least one tool actually ran"**, and the docstring says exactly what that number is
for -- it is what makes *"tools were on and the model chose not to use them"*
distinguishable from *"tools were off"*. Those are different facts about an agent
and they are the first thing anyone asks when a turn shows no searches. That
distinction was live in this repo on 2026-08-23: a production turn reported
`tool_steps: 0` and the only way to tell which of the two had happened was to read
`agents.tools_enabled` out of the database by hand.

The gap trigger broke the invariant. `steps = step` was assigned the moment the
trigger fired -- BEFORE the forced `tool_choice=search_corpus` invoke was known to
have produced anything. When the forced call comes back with no tool calls the loop
deliberately falls through and answers anyway, and the turn then reports
`steps=1, tool_calls=[]`: one step in which no tool ran. A turn that searched
nothing became indistinguishable from a turn that searched once, in the one field
that exists to tell them apart.

**Case 1 is that turn. Case 2 is its pair**, and the pair is the point: a fix that
simply deleted the assignment would pass case 1 and fail case 2. This is the
`refusal_pass = 0/2` guard applied in advance -- an assertion that the feature does
NOT fire is also passed by a feature that has been deleted, so it is only ever
written alongside one asserting it DOES.

Everything here is scripted: a stub chat model, a stub `search_corpus`, an agent
that is a `SimpleNamespace`. `run_agent_loop` reads nothing off `agent` itself --
it only hands it to `ToolContext` -- so stubbing `build_tools` makes the whole loop
hermetic. `emit=None`, so the per-step call is `ainvoke` rather than `astream`.

    backend/.venv/Scripts/python.exe scripts/agent_loop_check.py

ASCII in print(), exit 1 on any failure -- `new features/06-test-plan.md`.
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from langchain_core.messages import AIMessage  # noqa: E402
from langchain_core.tools import tool  # noqa: E402

from app.rag import agent_loop as loop_mod  # noqa: E402
from app.rag.agent_loop import ContextLedger, SEARCH_CORPUS, run_agent_loop  # noqa: E402

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "[ok]  " if condition else "[FAIL]"
    print(f"{status} {name}" + (f" -- {detail}" if detail else ""))
    if not condition:
        failures.append(name)


# The exact shape detect_gap is built for, quoted from its own docstring: an
# answer, and then an admission. Not a refusal -- the turn ANSWERED half.
GAP_ANSWER = (
    "The platform carries twenty-four lithium-ion battery modules [1]. The "
    "provided text does not cover the onboard storage for science instruments."
)
FINAL_ANSWER = "Final answer after the loop gave up forcing."


@tool
def search_corpus(query: str) -> str:
    """Search the course corpus."""
    return "[1] a stub chunk"


class _Bound:
    """One `bind_tools(...)` result. Returns whatever the script says."""

    def __init__(self, model: "ScriptedModel", tool_choice) -> None:
        self._model = model
        self._tool_choice = tool_choice

    async def ainvoke(self, messages):  # noqa: ANN001
        return self._model.respond(self._tool_choice)


class ScriptedModel:
    """A chat model whose reply depends only on which tool_choice was bound.

    That is the whole surface `run_agent_loop` uses: `bind_tools(tools)` for an
    ordinary step, `bind_tools(tools, tool_choice=SEARCH_CORPUS)` for the gap
    trigger's forced call, and `bind_tools(tools, tool_choice="none")` for the
    structurally final one.
    """

    def __init__(self, *, forced_returns_a_call: bool) -> None:
        self.forced_returns_a_call = forced_returns_a_call
        self.calls: list[str] = []

    def bind_tools(self, tools, tool_choice=None, **kwargs):  # noqa: ANN001
        return _Bound(self, tool_choice)

    def respond(self, tool_choice):  # noqa: ANN001
        self.calls.append(str(tool_choice))
        if tool_choice == SEARCH_CORPUS:
            if not self.forced_returns_a_call:
                # The model was FORCED to search and still returned prose. This
                # is not hypothetical: CLAUDE.md records `tool_choice="any"`
                # being silently ignored on this route, and a named tool being
                # the only thing that forces a call -- so "forced and declined"
                # is a real state, not a defensive branch.
                return AIMessage(content="I still cannot find it.")
            return AIMessage(
                content="",
                tool_calls=[
                    {"name": "search_corpus", "args": {"query": "science instrument storage"}, "id": "call-1"}
                ],
            )
        if tool_choice == "none":
            return AIMessage(content=FINAL_ANSWER)
        return AIMessage(content=GAP_ANSWER)


async def run(*, forced_returns_a_call: bool):
    model = ScriptedModel(forced_returns_a_call=forced_returns_a_call)
    return (
        await run_agent_loop(
            agent=SimpleNamespace(id="agent-under-test"),
            question="How many battery modules, and what is the science instrument storage?",
            ledger=ContextLedger(),
            system_prompt="You are a test.",
            model=model,
            max_steps=3,
        ),
        model,
    )


print("=" * 74)
print("agent loop -- LoopResult.steps counts steps in which a tool RAN")
print("=" * 74)

# `build_tools` is the only thing between this harness and Pinecone. Patched at
# the module the loop resolves it from, not at its definition.
loop_mod.build_tools = lambda ctx: [search_corpus]  # noqa: ARG005

# ---------------------------------------------------------------------------
print("\n-- 1. the gap trigger fired and the forced search returned NOTHING --")
# ---------------------------------------------------------------------------
result1, model1 = asyncio.run(run(forced_returns_a_call=False))

check(
    "1a. the forced search was actually attempted (the scenario is real)",
    SEARCH_CORPUS in model1.calls,
    f"tool_choice sequence={model1.calls}",
)
check(
    "1b. no tool ran, so tool_calls is empty",
    result1.tool_calls == [],
    f"tool_calls={result1.tool_calls}",
)
check(
    "1c. steps == 0 -- a step in which no tool ran is not a step",
    result1.steps == 0,
    f"steps={result1.steps} (agent_loop.py:413 says 'steps in which at least one tool actually ran')",
)
check(
    "1d. the turn still answered rather than failing",
    bool(result1.text),
    f"text={result1.text[:60]!r}",
)

# ---------------------------------------------------------------------------
print("\n-- 2. THE PAIR: forced search HONOURED, so the step is real --")
# ---------------------------------------------------------------------------
# Without this case, deleting the `steps` assignment outright would pass case 1
# and silently stop counting the forced search that DID happen.
result2, model2 = asyncio.run(run(forced_returns_a_call=True))

check(
    "2a. exactly one tool invocation was recorded",
    len(result2.tool_calls) == 1,
    f"tool_calls={[c.tool for c in result2.tool_calls]}",
)
check(
    "2b. steps == 1 -- the forced search ran, so it counts",
    result2.steps == 1,
    f"steps={result2.steps}",
)
check(
    "2c. the invocation is attributed to the gap trigger",
    bool(result2.tool_calls) and result2.tool_calls[0].detail.get("trigger") == "gap_detected",
    f"detail={result2.tool_calls[0].detail if result2.tool_calls else None}",
)

# ---------------------------------------------------------------------------
print("\n-- 3. the control: no gap, no forcing, no steps --")
# ---------------------------------------------------------------------------
# Proves cases 1 and 2 are measuring the TRIGGER and not merely the loop's
# default, which reports 0 for an ordinary answered turn.


class PlainModel(ScriptedModel):
    def respond(self, tool_choice):  # noqa: ANN001
        self.calls.append(str(tool_choice))
        return AIMessage(content="Twenty-four battery modules [1]. Fully answered.")


plain = PlainModel(forced_returns_a_call=False)
result3 = asyncio.run(
    run_agent_loop(
        agent=SimpleNamespace(id="agent-under-test"),
        question="How many battery modules?",
        ledger=ContextLedger(),
        system_prompt="You are a test.",
        model=plain,
        max_steps=3,
    )
)
check(
    "3a. an answered turn never forces a search",
    SEARCH_CORPUS not in plain.calls,
    f"tool_choice sequence={plain.calls}",
)
check(
    "3b. and reports steps == 0",
    result3.steps == 0,
    f"steps={result3.steps}",
)

print("\n" + "=" * 74)
if failures:
    print(f"{len(failures)} FAILED:")
    for name in failures:
        print(f"  - {name}")
    sys.exit(1)
print("all checks passed")
