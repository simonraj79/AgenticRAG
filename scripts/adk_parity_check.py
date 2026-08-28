"""Live A/B: the same question through both runtimes, on the same agent.

Needs the DATABASE and the PROVIDERS. Writes NOTHING -- it calls
`pipeline.answer_question`, which is the read-only half of a turn; `ask.run_turn`
is what persists, and it is deliberately not used here. So this harness owns
nothing and therefore needs no `--cleanup`, which is the exception that proves the
rule in CLAUDE.md's "a harness OWNS its subject or it does not write".

WHY IT EXISTS.

Everything below it is offline: `adk_loop_check` drives the loop with a scripted
model, `tenancy_check` reads declarations, `metering_check` walks an AST. Those
can prove the ADK runtime is WIRED. None of them can prove it ANSWERS -- and this
repo has been wrong eleven times about exactly that gap, most sharply when a
model's own `<|DSML|tool_calls>` markup was on screen with every harness green.

So this is `build.md` phase 6's "read one real output by eye", made into a command
so it is not optional. It prints both answers in full.

    backend/.venv/Scripts/python.exe scripts/adk_parity_check.py
    backend/.venv/Scripts/python.exe scripts/adk_parity_check.py --question "..."

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
from app.db.models import Agent  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.metering import collect_usage, meter_as  # noqa: E402
from app.rag import pipeline  # noqa: E402
from app.rag.textguard import _LEAKED_TOOL_MARKUP  # noqa: E402

DEFAULT_QUESTION = (
    "What is the power budget, and how does it compare with the thermal budget?"
)

failures: list[str] = []
checks = 0


def check(label: str, ok: bool, detail: str = "", *, always: bool = False) -> None:
    """`detail` prints on FAILURE only, unless `always`.

    A passing line that carries a failure hint ("TOOL_GUIDANCE missing?") reads as
    a failure to anyone scanning output, and this repo's whole problem is people
    believing green lines. Measurements worth seeing on a pass pass `always=True`.
    """
    global checks
    checks += 1
    show = detail if (detail and (always or not ok)) else ""
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" -- {show}" if show else ""))
    if not ok:
        failures.append(label)


async def pick_agent(session) -> Agent | None:
    """The agent with a corpus and tools on. Never `select().limit(1)`.

    CLAUDE.md's rule is about WRITE paths, and this one only reads -- but the
    ordering matters for a different reason here: an unordered pick makes the
    comparison non-reproducible between runs, so a difference between the two
    runtimes could be a difference between two agents.
    """
    rows = (
        await session.execute(
            select(Agent).where(Agent.tools_enabled.is_(True)).order_by(Agent.created_at)
        )
    ).scalars().all()
    for agent in rows:
        if agent.name == "Orbital Platform":
            return agent
    return rows[0] if rows else None


async def run_one(agent: Agent, question: str, runtime: str) -> dict:
    """One turn under one runtime, with its spend collected."""
    original = settings.agent_runtime
    settings.agent_runtime = runtime
    started = time.perf_counter()
    try:
        with collect_usage() as records:
            with meter_as(call_kind="generation"):
                result = await pipeline.answer_question(agent, question)
        return {
            "runtime": runtime,
            "answer": result.answer or "",
            "tool_calls": list(getattr(result, "tool_calls", None) or []),
            "tool_steps": getattr(result, "tool_steps", None),
            "stopped_reason": getattr(result, "stopped_reason", None),
            # `AnswerResult.documents`, not `.retrieval`. The first draft read a
            # field that does not exist, `getattr` returned None, and the case
            # reported "0 chunks" for BOTH runtimes -- a harness bug that looked
            # like a retrieval outage in two engines at once.
            "citations": len(getattr(result, "documents", None) or []),
            "wall_ms": int((time.perf_counter() - started) * 1000),
            "records": list(records),
        }
    finally:
        settings.agent_runtime = original


def report(run: dict) -> None:
    cost = sum((r.cost_usd or 0) for r in run["records"])
    providers = sorted({r.served_provider for r in run["records"] if r.served_provider})
    print(f"--- {run['runtime'].upper()} " + "-" * (60 - len(run["runtime"])))
    print(f"  wall {run['wall_ms']} ms | model calls {len(run['records'])} | "
          f"cost ${cost:.8f} | providers {providers}")
    print(f"  tool_steps={run['tool_steps']} tool_calls={len(run['tool_calls'])} "
          f"stopped_reason={run['stopped_reason']} chunks={run['citations']}")
    for inv in run["tool_calls"]:
        trig = inv.args.get("trigger")
        print(f"    - {inv.tool}({inv.args.get('query') or ''!r:.50}) ok={inv.ok} "
              f"{inv.duration_ms}ms" + (f" trigger={trig}" if trig else ""))
    print("  ANSWER:")
    for line in (run["answer"] or "(empty)").splitlines():
        print(f"    {line}")
    print("")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--question", default=DEFAULT_QUESTION)
    args = parser.parse_args()

    print("adk_parity_check -- the same question through both runtimes")
    print("")

    async with SessionLocal() as session:
        agent = await pick_agent(session)
        if agent is None:
            print("No tools-enabled agent with a corpus. Nothing to compare.")
            return 1
        print(f"agent: {agent.name} ({str(agent.id)[:8]})  question: {args.question!r}")
        print("")

        legacy = await run_one(agent, args.question, "langchain")
        adk = await run_one(agent, args.question, "adk")

    report(legacy)
    report(adk)

    print("ASSERTIONS")
    for run in (legacy, adk):
        name = run["runtime"]
        check(f"{name}: produced a non-empty answer", bool(run["answer"].strip()),
              f"{len(run['answer'])} chars", always=True)
        check(
            f"{name}: no leaked tool markup reached the answer",
            _LEAKED_TOOL_MARKUP.search(run["answer"]) is None,
        )
        check(
            f"{name}: the answer carries a citation marker",
            "[1]" in run["answer"] or "[2]" in run["answer"],
            "no [n] marker" if "[1]" not in run["answer"] else "",
        )
        # **Cost is asserted on CHAT calls only, and that is not a loosened
        # assertion -- it is the correct one.** A turn is nine billable calls, not
        # one: a rewrite, three embeddings, three reranks and two generations. Of
        # those, COHERE REPORTS UNITS AND NOT COST by design (`meta.billed_units.
        # search_units`), because a dollar figure would be arithmetic over a
        # published rate -- opt-in via COHERE_SEARCH_UNIT_USD, default 0.0, "do
        # not estimate". Requiring cost > 0 on every record would demand that this
        # project invent the number it deliberately refuses to invent.
        chat = [r for r in run["records"] if r.call_kind == "generation"]
        check(
            f"{name}: every CHAT call was metered with a cost",
            bool(chat) and all((r.cost_usd or 0) > 0 for r in chat),
            f"{len(chat)} chat of {len(run['records'])} records",
            always=True,
        )
        check(
            f"{name}: rerank calls record UNITS rather than an invented cost",
            all(
                (r.cost_usd in (None, 0.0))
                for r in run["records"]
                if (r.served_provider or "").lower() == "cohere"
            ),
        )
        check(
            f"{name}: every model call names its served provider",
            bool(run["records"]) and all(r.served_provider for r in run["records"]),
        )
        check(
            f"{name}: the answer cites with the ledger's [n] markers",
            any(f"[{i}]" in run["answer"] for i in range(1, 10)),
            "cited by filename instead -- TOOL_GUIDANCE missing?",
        )
    # The parity claim itself. Not "identical text" -- generation runs at
    # temperature 1.0, so two runs of the SAME runtime differ. What must hold is
    # that both engines retrieved, both grounded, and neither leaked.
    check(
        "both runtimes retrieved context",
        legacy["citations"] > 0 and adk["citations"] > 0,
        f"langchain={legacy['citations']} adk={adk['citations']}",
        always=True,
    )
    print("")
    if failures:
        print(f"FAILED {len(failures)} of {checks}:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"all {checks} parity checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
