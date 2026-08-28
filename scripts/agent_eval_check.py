"""AAA agent evaluation: agentic vs non-agentic, same corpus, same questions, one judge.

Needs the DATABASE and the PROVIDERS. **Writes NOTHING.** It answers through
`pipeline.answer_question` (the read-only half of a turn) and builds the
trajectory in memory from `AnswerResult.tool_calls` in exactly the shape
`ask.run_turn` would persist. No `eval_runs` row is created, so a comparison run
cannot pollute EVAL.md's history or look like a regression on the operator's card.

------------------------------------------------------------------
WHY A NATURAL EXPERIMENT AND NOT A BEFORE/AFTER.

The controlling fact: **the trajectory machinery had never produced a row.**
`eval_results.trajectory IS NOT NULL` = 0 of 50, `eval_runs.summary ? 'trajectory'`
= 0 of 5, `expected_tool_use` = NULL on 30 of 30, and none of the 50 evaluated
turns ever produced a TOOL_CALL. So there is no baseline to move -- the first
question is not "did a change help" but "does the agent loop earn its cost at all".

Two agents in this database share a corpus BYTE FOR BYTE -- 37 identical chunks
from the same PDF -- and differ in the one variable of interest:

    CONTROL     tools OFF  -- one retrieval, one generation. The classic chain.
    TREATMENT   tools ON   -- the agent loop: it may search again, and the gap
                             trigger may force it to.

Same questions, same judge, same corpus, run back to back. That isolates the
AGENT ARCHITECTURE's contribution in a way a before/after cannot, because every
run re-asks at temperature 1.0 and CLAUDE.md already records this project reading
a judge delta off two runs that had also regenerated their answers.

The retrieval parameters differ between the two agents (`retrieve_k` 20 vs 24,
`rerank_top_n` 3 vs 5), so they are EQUALISED IN MEMORY before either arm runs --
mutating the loaded ORM object and never committing. Left alone they would be a
confounder: more chunks in the control's context is a different experiment.
------------------------------------------------------------------

WHAT IT REPORTS, and the order matters.

The COUNTED signals come first because they need no judge: `searches`,
`redundant_searches`, `self_initiated`, `calls_per_step`, `budget_exhausted`.
They are arithmetic over rows, so their only variance is generation variance.
`goal_accuracy` is secondary and corroborating -- it is BINARY and the judge is
non-deterministic, so `--noise` measures its flip rate on one fixed trajectory
and the run refuses to call any smaller difference a result.

    backend/.venv/Scripts/python.exe scripts/agent_eval_check.py
    backend/.venv/Scripts/python.exe scripts/agent_eval_check.py --n 10 --noise 5

ASCII in print(), exit 1 on any failure -- `new features/06-test-plan.md`.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
import warnings
from pathlib import Path
from types import SimpleNamespace

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import select  # noqa: E402

from app.config import settings  # noqa: E402
from app.db.models import Agent, GoldenQuestion  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.eval.trajectory import trajectory_from_rows  # noqa: E402
from app.eval.trajectory_metrics import score_trajectory, summarise_trajectory  # noqa: E402
from app.metering import collect_usage, meter_as  # noqa: E402
from app.rag import pipeline  # noqa: E402
from app.rag.trace import GENERATE, TOOL_CALL, TOOL_ERROR, TOOL_RESULT  # noqa: E402

failures: list[str] = []
checks = 0


def check(label: str, ok: bool, detail: str = "", *, always: bool = False) -> None:
    """`detail` prints on failure only, unless `always`.

    A passing line carrying a failure hint reads as a failure to anyone scanning
    output, and this repository's whole problem is people believing green lines.
    """
    global checks
    checks += 1
    show = detail if (detail and (always or not ok)) else ""
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" -- {show}" if show else ""))
    if not ok:
        failures.append(label)


def _event(event_type: str, payload: dict) -> SimpleNamespace:
    """A stand-in for a `TraceEvent` row. The readers use two attributes."""
    return SimpleNamespace(event_type=event_type, payload=payload)


def events_from(result) -> list[SimpleNamespace]:
    """`AnswerResult` -> the trace rows `ask.run_turn` would have written.

    Built HERE rather than read back from the database, because writing is what
    this harness refuses to do. The shape is copied from `ask.run_turn`'s own
    writer: a TOOL_CALL per invocation carrying `args` and `assistant_text`, then
    a TOOL_RESULT or TOOL_ERROR carrying `ok`, `summary`, `content` and the
    invocation's `detail` spread in -- which is where `new_chunks` lives and is
    what makes `redundant_searches` computable.
    """
    rows: list[SimpleNamespace] = []
    for inv in result.tool_calls or []:
        rows.append(
            _event(
                TOOL_CALL,
                {
                    "step": inv.step,
                    "tool": inv.tool,
                    "call_id": inv.call_id,
                    "args": inv.args,
                    "assistant_text": inv.assistant_text,
                },
            )
        )
        rows.append(
            _event(
                TOOL_RESULT if inv.ok else TOOL_ERROR,
                {
                    "step": inv.step,
                    "tool": inv.tool,
                    "call_id": inv.call_id,
                    "ok": inv.ok,
                    "summary": inv.summary,
                    "content": inv.content,
                    **(inv.detail or {}),
                },
            )
        )
    rows.append(
        _event(
            GENERATE,
            {
                "model": result.model,
                "tool_steps": result.tool_steps,
                "tool_calls": len(result.tool_calls or []),
                "stopped_reason": result.stopped_reason,
            },
        )
    )
    return rows


async def ask(agent, question: str) -> dict:
    started = time.perf_counter()
    with collect_usage() as records:
        with meter_as(call_kind="generation"):
            # `rewrite=False`: a golden question must reach the embedder VERBATIM.
            # `EVAL_REWRITE_QUESTIONS` is false for exactly this reason.
            result = await pipeline.answer_question(agent, question, rewrite=False)
    return {
        "result": result,
        "answer": result.answer or "",
        "contexts": [d.page_content for d in (result.documents or [])],
        "events": events_from(result),
        "ms": int((time.perf_counter() - started) * 1000),
        "cost": sum((r.cost_usd or 0) for r in records),
        "gen_calls": sum(1 for r in records if r.call_kind == "generation"),
    }


def card(name: str, summary: dict, runs: list[dict]) -> None:
    print(f"--- {name.upper()} " + "-" * max(0, 56 - len(name)))

    def measured(key: str) -> str:
        block = summary.get(key) or {}
        value, m, t = block.get("value"), block.get("measured"), block.get("total")
        # NUMERATOR / DENOMINATOR, never a bare rate. A value of 0.0 over
        # measured=0 and 0.0 over measured=400 render identically once the
        # denominator is dropped, and goal accuracy is binary so a rate over
        # n<20 is not a number anyone should read as one.
        if value is None:
            return f"  n/a   (measured {m} of {t})"
        return f"{value:0.3f}   ({int(round(value * (m or 0)))}/{m} achieved, of {t})"

    print(f"  goal_accuracy answer  {measured('goal_accuracy_answer')}")
    print(f"  goal_accuracy refuse  {measured('goal_accuracy_refuse')}")
    print(f"  tool_use_ok           {measured('tool_use_ok')}")
    print(f"  calls_per_step        {measured('calls_per_step')}")
    searches = summary.get("searches") or 0
    redundant = summary.get("redundant_searches") or 0
    rate = summary.get("wasted_search_rate")
    print(f"  searches              {searches}")
    print(f"  redundant_searches    {redundant}"
          + (f"   (wasted {rate:0.1%} of {searches})" if rate is not None else ""))
    print(f"  self_initiated turns  {summary.get('self_initiated')}")
    print(f"  gap_forced turns      {summary.get('gap_forced')}")
    print(f"  budget_exhausted      {summary.get('budget_exhausted')}")
    print(f"  tool_error            {summary.get('tool_error')}")
    print(f"  wall_ms_total         {sum(r['ms'] for r in runs)}")
    print(f"  cost_usd_total        ${sum(r['cost'] for r in runs):.8f}")
    print(f"  generation calls      {sum(r['gen_calls'] for r in runs)}")
    print("")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=10)
    parser.add_argument(
        "--noise",
        type=int,
        default=5,
        help="times to re-score ONE fixed trajectory, to measure judge flip rate",
    )
    args = parser.parse_args()

    print("agent_eval_check -- the agent loop, measured against its own control")
    print(f"  judge={settings.ragas_judge_model}  generation={settings.generation_model}")
    print("")

    async with SessionLocal() as session:
        agents = (
            await session.execute(select(Agent).order_by(Agent.created_at))
        ).scalars().all()
        by_name = {a.name: a for a in agents}
        control = by_name.get("Topic 1")
        treatment = by_name.get("prompt engineering")
        if control is None or treatment is None:
            print("The paired-corpus agents are not present. Nothing to compare.")
            return 1

        questions = (
            await session.execute(
                select(GoldenQuestion)
                .where(
                    GoldenQuestion.agent_id == control.id,
                    GoldenQuestion.is_active.is_(True),
                )
                .order_by(GoldenQuestion.created_at)
                .limit(args.n)
            )
        ).scalars().all()
        if not questions:
            print("The control agent has no active golden set.")
            return 1

        # EQUALISE THE CONFOUNDERS, in memory, never committed. Left alone,
        # `retrieve_k` 20 vs 24 and `rerank_top_n` 3 vs 5 mean the two arms read
        # different amounts of context and the comparison measures retrieval
        # width as much as the agent loop.
        equalised = {"retrieve_k": control.retrieve_k,
                     "rerank_top_n": control.rerank_top_n,
                     "chunk_size": control.chunk_size}
        before = {k: getattr(treatment, k) for k in equalised}
        for key, value in equalised.items():
            setattr(treatment, key, value)

        print(f"  control    {control.name!r} tools={control.tools_enabled}")
        print(f"  treatment  {treatment.name!r} tools={treatment.tools_enabled}"
              f"  (equalised {before} -> {equalised})")
        print(f"  questions  {len(questions)} from the control's golden set")
        print("")

        check(
            "the two arms share a corpus and differ only in tools_enabled",
            control.tools_enabled is False and treatment.tools_enabled is True,
            f"control={control.tools_enabled} treatment={treatment.tools_enabled}",
            always=True,
        )

        arms: dict[str, list[dict]] = {"control": [], "treatment": []}
        rubric: dict[str, list[dict]] = {"control": [], "treatment": []}

        for index, gq in enumerate(questions, 1):
            print(f"  [{index}/{len(questions)}] [{gq.expected_behaviour}] "
                  f"{gq.question[:64]!r}")
            for arm, agent in (("control", control), ("treatment", treatment)):
                run = await ask(agent, gq.question)
                arms[arm].append(run)
                sample = trajectory_from_rows(
                    question=gq.question, answer=run["answer"], events=run["events"]
                )
                row = await score_trajectory(
                    sample,
                    reference=gq.reference_answer,
                    # AUTHORED HERE rather than read from the column, which is
                    # NULL on all 30 rows. A refusal question expects a search
                    # (find nothing, then decline); an answerable one does too,
                    # since every question in this set is corpus-grounded. This
                    # is an assumption and it is stated rather than hidden --
                    # authoring the column properly is a separate change.
                    expected_tool_use="search",
                    events=run["events"],
                )
                row["expected_behaviour"] = gq.expected_behaviour
                rubric[arm].append(row)
                inv = run["result"].tool_calls or []
                print(f"        {arm:<10} {run['ms']:>6}ms  {run['gen_calls']} gen  "
                      f"{len(inv)} tool calls  "
                      f"goal={row.get('goal_accuracy')}")

    summaries = {a: summarise_trajectory(rubric[a]) for a in ("control", "treatment")}
    print("")
    for arm in ("control", "treatment"):
        card(arm, summaries[arm], arms[arm])

    # ---------------------------------------------------------------- noise
    #
    # THE NUMBER THAT MAKES THE REST HONEST. Goal accuracy is binary and the
    # judge is non-deterministic -- reasoning is mandatory on Flash and cannot be
    # disabled, so temperature buys nothing. Re-scoring ONE fixed trajectory k
    # times measures the flip rate directly, and any difference between the arms
    # smaller than it is noise being read as a result.
    #
    # This is `agent_metrics_check` case 22's missing sibling: that case asserts
    # a good and a bad trajectory DIFFER; this asserts the SAME one does not.
    noise_flips = None
    if args.noise > 1 and rubric["treatment"]:
        pilot_q = questions[0]
        pilot_run = arms["treatment"][0]
        pilot_sample = trajectory_from_rows(
            question=pilot_q.question,
            answer=pilot_run["answer"],
            events=pilot_run["events"],
        )
        print(f"judge noise floor -- re-scoring ONE fixed trajectory {args.noise}x")
        verdicts = []
        for _ in range(args.noise):
            row = await score_trajectory(
                pilot_sample,
                reference=pilot_q.reference_answer,
                expected_tool_use="search",
                events=pilot_run["events"],
            )
            verdicts.append(row.get("goal_accuracy"))
        clean = [v for v in verdicts if v is not None]
        noise_flips = (
            0.0 if not clean else 1.0 - (max(clean.count(0.0), clean.count(1.0)) / len(clean))
        )
        print(f"  verdicts={clean}  flip_rate={noise_flips:0.2f}")
        print("")

    # ---------------------------------------------------------------- verdict
    print("ASSERTIONS")
    for arm in ("control", "treatment"):
        s = summaries[arm]
        check(f"{arm}: produced a rubric block", bool(s))
        check(
            f"{arm}: every question is in the denominator",
            (s.get("goal_accuracy_answer") or {}).get("total", 0)
            + (s.get("goal_accuracy_refuse") or {}).get("total", 0)
            == len(questions),
            f"answer+refuse totals vs {len(questions)}",
        )
        check(f"{arm}: no rubric row errored",
              not any(r.get("error") for r in rubric[arm]),
              str([r.get("error") for r in rubric[arm] if r.get("error")]))

    # The architecture claims. Each is checkable; none is a ranking.
    ctl, trt = summaries["control"], summaries["treatment"]
    check(
        "the CONTROL performed zero searches (tools were off)",
        (ctl.get("searches") or 0) == 0,
        f"searches={ctl.get('searches')}",
        always=True,
    )
    check(
        "the TREATMENT actually exercised the agent loop",
        (trt.get("searches") or 0) > 0,
        f"searches={trt.get('searches')} self_initiated={trt.get('self_initiated')}",
        always=True,
    )
    ctl_goal = (ctl.get("goal_accuracy_answer") or {}).get("value")
    trt_goal = (trt.get("goal_accuracy_answer") or {}).get("value")
    if ctl_goal is not None and trt_goal is not None:
        delta = trt_goal - ctl_goal
        floor = noise_flips if noise_flips is not None else 0.0
        check(
            "goal-accuracy delta is reported AGAINST the measured noise floor",
            True,
            f"control={ctl_goal:0.3f} treatment={trt_goal:0.3f} "
            f"delta={delta:+0.3f} noise_floor={floor:0.2f} -> "
            + ("INSIDE the noise, not a result" if abs(delta) <= floor
               else "outside the noise floor"),
            always=True,
        )
    print("")
    print("READ THIS BEFORE THE NUMBERS: goal accuracy is BINARY and the judge is")
    print("non-deterministic. The counted signals (searches, redundant_searches,")
    print("self_initiated, calls_per_step) need no judge and are the decision")
    print("metrics. n is small; this detects a collapse, not a subtle regression.")
    print("")
    if failures:
        print(f"FAILED {len(failures)} of {checks}:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"all {checks} agent-eval checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
