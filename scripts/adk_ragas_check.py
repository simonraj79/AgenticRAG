"""Ragas A/B: the same golden set through both runtimes, scored by one judge.

Needs the DATABASE and the PROVIDERS. Writes NOTHING -- it answers through
`pipeline.answer_question` (the read-only half of a turn) and scores in memory. No
`eval_runs` row is created, so this cannot pollute EVAL.md's history with a
comparison run.

WHY THIS AND NOT `POST /api/agents/{id}/eval-runs`.

The real eval job is the right instrument for grading ONE agent and it is the
wrong one for comparing TWO runtimes, for three reasons that all cost something:
it persists a scorecard (so an A/B would leave two rows that look like a
regression), it re-asks the questions per run (so a difference between runtimes is
confounded with temperature 1.0 variance -- which is exactly the mistake CLAUDE.md
records under "Do not read 0.628 -> 0.769 as the judge delta"), and it takes 23-25
minutes for ten questions.

This asks each question once per runtime, back to back, and hands BOTH answers to
the SAME judge. The judge is `RAGAS_JUDGE_MODEL` (`google/gemini-3.7-flash`) while
generation is DeepSeek and the golden set was drafted by MiniMax, so no model on
this card grades its own work or grades against references it wrote.

**Read the caveat before reading the numbers.** Generation runs at temperature
1.0, so a single question per runtime is one sample of a distribution, not a
measurement. With a handful of questions this says "both engines are in the same
band" or "one of them is broken"; it does not rank them. Ranking needs the real
eval run, on a re-baselined golden set, and `--n` large enough to mean something.

    backend/.venv/Scripts/python.exe scripts/adk_ragas_check.py
    backend/.venv/Scripts/python.exe scripts/adk_ragas_check.py --n 6

ASCII in print(), exit 1 on any failure -- `new features/06-test-plan.md`.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import select  # noqa: E402

from app.config import settings  # noqa: E402
from app.db.models import Agent, GoldenQuestion  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.eval import metrics_guide, ragas_runner  # noqa: E402
from app.eval.ragas_runner import EvalTurn  # noqa: E402
from app.metering import collect_usage, meter_as  # noqa: E402
from app.rag import pipeline  # noqa: E402
from app.rag.refusal import detect_refusal  # noqa: E402

METRICS = ("faithfulness", "answer_relevance", "context_precision", "context_recall")

failures: list[str] = []
checks = 0


def check(label: str, ok: bool, detail: str = "", *, always: bool = False) -> None:
    global checks
    checks += 1
    show = detail if (detail and (always or not ok)) else ""
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" -- {show}" if show else ""))
    if not ok:
        failures.append(label)


async def answer_under(agent, question: str, runtime: str) -> dict:
    original = settings.agent_runtime
    settings.agent_runtime = runtime
    started = time.perf_counter()
    try:
        with collect_usage() as records:
            with meter_as(call_kind="generation"):
                # `rewrite=False`: a golden question must reach the embedder
                # VERBATIM. `EVAL_REWRITE_QUESTIONS` is false for exactly this
                # reason -- a rewritten golden question is no longer the question
                # every EVAL.md baseline was measured on.
                result = await pipeline.answer_question(agent, question, rewrite=False)
        return {
            "answer": result.answer or "",
            "contexts": [d.page_content for d in (result.documents or [])],
            "tool_calls": len(getattr(result, "tool_calls", None) or []),
            "ms": int((time.perf_counter() - started) * 1000),
            "cost": sum((r.cost_usd or 0) for r in records),
        }
    finally:
        settings.agent_runtime = original


def scorecard(name: str, summary: dict, runs: list[dict], results: list[dict]) -> None:
    """Print the card, and print each metric's OWN denominator beside it.

    `RunSummary` carries the four metrics as TOP-LEVEL keys, not nested under a
    `means` map -- the first draft of this reader invented the nesting and printed
    `n/a` four times over a run that had scored perfectly well.

    The per-metric `n` is computed here rather than read off the summary because
    the summary does not carry one, and CLAUDE.md is emphatic about why that
    matters: `summarise` appends to a metric's list only when the value is
    non-null, so **each mean has its own denominator and the card's
    `scored_count` is an upper bound, not the divisor**. The metric most likely to
    fail is the one with the smallest sample -- and it is the one
    `weakest_metric` then points you at.
    """
    print(f"--- {name.upper()} " + "-" * (56 - len(name)))
    for metric in METRICS:
        value = summary.get(metric)
        n = sum(1 for r in results if r.get(metric) is not None)
        rendered = "  n/a" if value is None else f"{value:0.3f}"
        print(f"  {metric:<20} {rendered}   (n={n})")
    for key in ("scored_count", "total_count", "refusal_pass", "refusal_total",
                "error_count", "weakest_metric", "weakest_score", "self_judged"):
        if key in summary:
            print(f"  {key:<20} {summary[key]}")
    total_ms = sum(r["ms"] for r in runs)
    total_cost = sum(r["cost"] for r in runs)
    print(f"  {'wall_ms_total':<20} {total_ms}")
    print(f"  {'cost_usd_total':<20} ${total_cost:.8f}")
    print(f"  {'tool_calls_total':<20} {sum(r['tool_calls'] for r in runs)}")
    print("")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=4, help="golden questions to use")
    args = parser.parse_args()

    print("adk_ragas_check -- one golden set, two runtimes, one judge")
    print(f"  judge={settings.ragas_judge_model}  generation={settings.generation_model}")
    print(f"  golden set drafted by {settings.golden_set_model}")
    print("")

    async with SessionLocal() as session:
        # **DERIVED, never named.** The first draft hardcoded an agent by name and
        # picked one with a corpus but no golden set, so the run aborted having
        # proved nothing. The selection rule IS the requirement: tools enabled
        # (or the ADK runtime has no tools to exercise) AND an active golden set
        # (or there is nothing to score). Ordered by `created_at` so two runs
        # compare the same agent -- an unordered pick would confound a runtime
        # difference with an agent difference.
        agents = (
            await session.execute(
                select(Agent).where(Agent.tools_enabled.is_(True)).order_by(Agent.created_at)
            )
        ).scalars().all()

        agent = None
        questions: list[GoldenQuestion] = []
        for candidate in agents:
            found = (
                await session.execute(
                    select(GoldenQuestion)
                    .where(
                        GoldenQuestion.agent_id == candidate.id,
                        GoldenQuestion.is_active.is_(True),
                    )
                    .order_by(GoldenQuestion.created_at)
                    .limit(args.n)
                )
            ).scalars().all()
            if found:
                agent, questions = candidate, found
                break

        if agent is None:
            print("No tools-enabled agent has an active golden set. Nothing to evaluate.")
            print("Candidates checked: " + ", ".join(repr(a.name) for a in agents))
            return 1

        print(f"agent: {agent.name} ({str(agent.id)[:8]})  questions: {len(questions)}")
        print("")

        per_runtime: dict[str, list[dict]] = {"langchain": [], "adk": []}
        turns: dict[str, list[EvalTurn]] = {"langchain": [], "adk": []}

        for index, gq in enumerate(questions, 1):
            print(f"  [{index}/{len(questions)}] {gq.question[:72]!r} "
                  f"({gq.expected_behaviour})")
            for runtime in ("langchain", "adk"):
                run = await answer_under(agent, gq.question, runtime)
                per_runtime[runtime].append(run)
                turns[runtime].append(
                    EvalTurn(
                        question=gq.question,
                        answer=run["answer"],
                        contexts=run["contexts"],
                        reference=gq.reference_answer,
                        expected_behaviour=gq.expected_behaviour,
                        # Read from the SAME detector the product uses, so a
                        # refusal row is classified identically to how the live
                        # `queries.refused` column would classify it.
                        refused=detect_refusal(run["answer"]),
                        golden_question_id=gq.id,
                    )
                )
                print(f"        {runtime:<10} {run['ms']:>6} ms  "
                      f"{len(run['answer']):>5} chars  {run['tool_calls']} tool calls")

    print("")
    print("scoring both sets with one judge...")
    summaries: dict[str, dict] = {}
    scored: dict[str, list[dict]] = {}
    for runtime in ("langchain", "adk"):
        results = await ragas_runner.score_samples(turns[runtime])
        scored[runtime] = results
        summaries[runtime] = metrics_guide.summarise(results, self_judged=False)
    print("")

    for runtime in ("langchain", "adk"):
        scorecard(runtime, summaries[runtime], per_runtime[runtime], scored[runtime])

    print("ASSERTIONS")
    for runtime in ("langchain", "adk"):
        summary = summaries[runtime]
        check(
            f"{runtime}: the run produced at least one scored metric",
            any(summary.get(m) is not None for m in METRICS),
            f"all four metrics null over {summary.get('scored_count')} scored rows",
        )
        check(
            f"{runtime}: no question errored",
            (summary.get("error_count") or 0) == 0,
            f"error_count={summary.get('error_count')}",
            always=True,
        )
        check(
            f"{runtime}: every answer was non-empty",
            all(t.answer.strip() for t in turns[runtime]),
        )

    # **The claim this file can actually support.** Not "ADK is better" -- one
    # sample per question at temperature 1.0 cannot support a ranking. What it can
    # support is that the new runtime is not BROKEN: it scores in the same band on
    # the metric that would collapse first if grounding had regressed.
    lang_f = summaries["langchain"].get("faithfulness")
    adk_f = summaries["adk"].get("faithfulness")
    if lang_f is not None and adk_f is not None:
        check(
            "ADK faithfulness is within 0.25 of the langchain runtime",
            abs(adk_f - lang_f) <= 0.25,
            f"langchain={lang_f:0.3f} adk={adk_f:0.3f} delta={adk_f - lang_f:+0.3f}",
            always=True,
        )
    else:
        check(
            "both runtimes produced a faithfulness score",
            False,
            f"langchain={lang_f} adk={adk_f}",
        )

    print("")
    print("CAVEAT: temperature is 1.0 and n is small. This says 'same band' or")
    print("'one of them is broken'. It does not rank them. See the docstring.")
    print("")
    if failures:
        print(f"FAILED {len(failures)} of {checks}:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"all {checks} ragas A/B checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
