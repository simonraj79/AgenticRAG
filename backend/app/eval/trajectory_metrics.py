"""The trajectory rubric: one judged metric, one counted one.

PRD open item 23 asks for trajectory evaluation and warns, in the same sentence,
that *inventing a faithfulness-shaped score for tool choice would be a new
instrument of unknown validity -- the exact failure items 15 and 16 record*. This
module is written to respect both halves of that:

  * The **judged** half is `AgentGoalAccuracyWithReference`, which ragas already
    ships and which `scripts/agent_metrics_check.py --live` cases 20-23 validate
    against a known-good and a known-bad trajectory before it is trusted.
  * The **counted** half invents nothing. It is arithmetic over rows that already
    exist, which is `new features/loop.md` §1 applied literally: if a number
    decides it, write the branch.

**Why goal accuracy and not a tool-call score.** Both of ragas' tool metrics
compare arguments byte-exactly, and this system rewrites `search_corpus`'s query
on every turn by design at temperature 1.0 -- so the one meaningful argument is
never the same string twice. Measured: 0.0 for a differently worded query, 0.0
for an empty reference arg dict, 0.0 for two calls against one reference. That is
the `refusal_pass = 0/2` shape, an instrument reading zero while the agent behaves
perfectly. `agent_metrics_check.py` cases 1-7 pin it, and case 6 reads the
installed ragas source so a future release that fixes it goes RED rather than
leaving a stale note in a plan file. Full record in
`new features/16-agent-evaluation/PLAN.md` §2.4.

**Goal accuracy scores OUTCOME, not path**, which is exactly why it survives where
they do not: this agent's path legitimately varies. Its sharpest use is the
refusal question -- "did it search, find nothing, and decline" is a proposition
`faithfulness` structurally cannot express, and it reaches that verdict without
consulting the marker list in `app/rag/refusal.py`, which CLAUDE.md records being
wrong five times in five different ways.

**It is BINARY.** It returns 1 or 0. Aggregated it is a pass rate over n, never a
mean, and nothing may render it in the same visual grammar as faithfulness.
"""

from __future__ import annotations

import asyncio
import logging
import math
import warnings
from typing import Any, Iterable

from ragas.dataset_schema import MultiTurnSample

from app.config import settings
from app.eval.ragas_runner import METRIC_TIMEOUT_S, get_judge
from app.rag.trace import GENERATE, TOOL_CALL

with warnings.catch_warnings():
    # Same scoped suppression `ragas_runner.py` uses, and for the same reason:
    # `ragas.metrics.collections` rejects `LangchainLLMWrapper` outright, so the
    # deprecated path is the working one. Scoped to the import statement, so a
    # deprecation from anywhere else still surfaces.
    warnings.simplefilter("ignore", DeprecationWarning)
    from ragas.metrics import AgentGoalAccuracyWithReference

log = logging.getLogger("uvicorn.error")

# The propositions a golden question can assert about a turn. Deliberately NOT a
# reference tool-call sequence: there is no metric that could read one honestly
# (see the module docstring), and a column nothing can score is a column that
# invites someone to score it wrongly.
#
#   search  -- at least one successful `search_corpus` call happened
#   none    -- no tool call happened; the "no reflex tool use" proposition
#   python  -- at least one `run_python` call happened
#   None    -- no expectation authored. Counted, not graded.
EXPECTED_TOOL_USE_VALUES = frozenset({"search", "none", "python"})

SEARCH_TOOL = "search_corpus"
PYTHON_TOOL = "run_python"


def tool_use_verdict(
    events: Iterable[Any], *, expected: str | None
) -> dict[str, Any]:
    """Read the counted half straight off the trace rows. No model, no threshold.

    `expected=None` yields `tool_use_ok=None`, which is the whole reason this
    returns a nullable rather than a bool: a run over a golden set with no
    authored expectations must report NOT MEASURED, not a perfect zero. That
    distinction has shipped wrong twice in this codebase's admin console, in one
    file, after every harness was green.
    """
    rows = list(events)

    tools_called: list[str] = []
    gap_forced = False
    for row in rows:
        payload = getattr(row, "payload", None) or {}
        if getattr(row, "event_type", None) != TOOL_CALL:
            continue
        tools_called.append(str(payload.get("tool") or "unknown"))
        args = payload.get("args")
        if isinstance(args, dict) and args.get("trigger") == "gap_detected":
            gap_forced = True

    generate = next(
        (r for r in rows if getattr(r, "event_type", None) == GENERATE), None
    )
    gen_payload = (getattr(generate, "payload", None) or {}) if generate else {}
    # `.get`-as-migration, exactly as `conversations.py` replays `tool_steps` for
    # turns that predate the agent loop.
    tool_steps = int(gen_payload.get("tool_steps") or 0)
    tool_calls = int(gen_payload.get("tool_calls") or len(tools_called))

    searched = SEARCH_TOOL in tools_called
    ran_python = PYTHON_TOOL in tools_called

    if expected is None:
        tool_use_ok: bool | None = None
    elif expected == "search":
        tool_use_ok = searched
    elif expected == "none":
        tool_use_ok = not tools_called
    elif expected == "python":
        tool_use_ok = ran_python
    else:
        # An unrecognised value is not graded. Failing closed would put a false
        # negative into a measurement, and the API validates this against
        # EXPECTED_TOOL_USE_VALUES before it can ever reach here.
        log.warning("Unknown expected_tool_use %r; not graded", expected)
        tool_use_ok = None

    return {
        "expected_tool_use": expected,
        "tool_use_ok": tool_use_ok,
        "searched": searched,
        "ran_python": ran_python,
        "tool_calls": tool_calls,
        "tool_steps": tool_steps,
        # REPORTED, never graded. CLAUDE.md records this model emitting 1.50-2.00
        # search calls per step while `max_tool_steps` bounds STEPS, so the
        # retrieval budget silently doubled and no assertion could see it -- "the
        # test you never wrote". This is that number. It is not branched on,
        # because nobody has authored a threshold and `score_threshold` is the
        # standing precedent for what happens when a number is graded against a
        # band that overlaps.
        "calls_per_step": (tool_calls / tool_steps) if tool_steps > 0 else None,
        "gap_forced": gap_forced,
        "stopped_reason": gen_payload.get("stopped_reason"),
    }


async def score_trajectory(
    sample: MultiTurnSample | None,
    *,
    reference: str | None,
    expected_tool_use: str | None,
    events: Iterable[Any],
) -> dict[str, Any]:
    """The full rubric for one turn: counted always, judged when it can be.

    The counted half is computed FIRST and unconditionally. A judge outage must
    not delete numbers that need no provider at all -- and a rubric that returns
    nothing when the network is down is a rubric nobody will trust when it does
    return something.
    """
    row = tool_use_verdict(events, expected=expected_tool_use)
    row["goal_accuracy"] = None
    row["error"] = None

    if not settings.trajectory_goal_accuracy:
        return row
    if sample is None or not reference:
        # No trajectory, or no reference to judge against. `None`, never 0.0 --
        # EVAL.md's second way a scorecard misleads.
        return row

    try:
        # Constructed per turn, never shared. `ragas_runner.py` documents why:
        # one mutable metric instance across concurrent scoring is a race nobody
        # would find from the symptom.
        metric = AgentGoalAccuracyWithReference(llm=get_judge(settings.ragas_judge_model))
        scored = MultiTurnSample(user_input=sample.user_input, reference=reference)
        value = await asyncio.wait_for(
            metric.multi_turn_ascore(scored), timeout=METRIC_TIMEOUT_S
        )
        row["goal_accuracy"] = _clean(value)
    except asyncio.TimeoutError:
        # Named separately from a generic failure. CLAUDE.md records
        # METRIC_TIMEOUT_S doubling as a quota-retry ceiling, so a hang and a rate
        # limit printed the same string and needed opposite fixes; saying which
        # one this was is the whole cost of not repeating that.
        row["error"] = f"goal_accuracy: timed out after {METRIC_TIMEOUT_S:.0f}s"
    except Exception as exc:  # noqa: BLE001 - a judge failure is not a run failure
        row["error"] = f"goal_accuracy: {exc.__class__.__name__}: {exc}"

    return row


def _clean(value: Any) -> float | None:
    """NaN and Inf become None at the point where the cause is still visible.

    Same discipline as `ragas_runner._clean`: NaN survives the JSONB write
    intact and then explodes at the API serialiser, a long way from anything
    that explains it.
    """
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _measured(values: list[float], total: int) -> dict[str, Any]:
    """A number and the population it rests on. Never collapse this to a float.

    `admin.py`'s `Measured` carries the same rule and states it: a value of 0.0
    over `measured=0` and a value of 0.0 over `measured=400` are completely
    different facts and render identically once the denominator is dropped.
    """
    return {
        "value": (sum(values) / len(values)) if values else None,
        "measured": len(values),
        "total": total,
    }


def summarise_trajectory(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Aggregate per-turn rubric rows into the block stored on `eval_runs.summary`.

    Returns None for an empty list rather than a dict of zeros. A mean over
    nothing is not zero, and a card that renders 0.00 for "we scored nothing"
    sends its reader to fix an agent that was never measured.
    """
    if not rows:
        return None

    total = len(rows)
    goal = [r["goal_accuracy"] for r in rows if r.get("goal_accuracy") is not None]
    graded = [r["tool_use_ok"] for r in rows if r.get("tool_use_ok") is not None]
    per_step = [r["calls_per_step"] for r in rows if r.get("calls_per_step") is not None]

    return {
        # A PASS RATE over n, not a mean of a continuous score. The metric returns
        # 1 or 0 per turn; the UI renders "7 / 9 achieved" and never "0.78".
        "goal_accuracy": _measured([float(v) for v in goal], total),
        "tool_use_ok": _measured([1.0 if v else 0.0 for v in graded], total),
        "calls_per_step": _measured([float(v) for v in per_step], total),
        "searched": sum(1 for r in rows if r.get("searched")),
        "gap_forced": sum(1 for r in rows if r.get("gap_forced")),
        "budget_exhausted": sum(
            1 for r in rows if r.get("stopped_reason") == "max_steps"
        ),
        "tool_error": sum(1 for r in rows if r.get("stopped_reason") == "tool_error"),
        "errors": sum(1 for r in rows if r.get("error")),
    }
