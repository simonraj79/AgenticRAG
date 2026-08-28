"""Calibration of the Ragas AGENT metrics against this system. Layer 1 by default.

This harness exists to answer one question before any feature reads a number:
**can the metric tell a good trajectory from a bad one?** EVAL.md records a judge
scoring an answer copied verbatim out of its own context 0.000, while a second
judge scored the identical stored answer 1.000 -- and nothing on the scorecard
distinguished them. The rule that came out of it governs this whole change set:

    A metric that cannot discriminate is worse than no metric, because the
    scorecard still renders.

So it is organised in four groups, and the first is the reason the second exists.

  1-7    WHY THE TOOL METRICS ARE CLOSED. `ToolCallAccuracy` and `ToolCallF1`
         compare arguments byte-exactly. This system rewrites `search_corpus`'s
         query on EVERY turn by design (`REWRITE_EVERY_TURN=true`) at temperature
         1.0, so the one meaningful argument is never the same string twice.
         These cases pin that as a MEASUREMENT rather than an opinion -- including
         case 6, which reads the installed source, so a future ragas that fixes
         `_get_arg_score` breaks the build and forces the decision to be re-opened
         deliberately rather than leaving a wrong note in a plan file.

  10-13  THE TRAJECTORY BUILDER. Whether a turn stored in `trace_events` can be
         rebuilt into a `MultiTurnSample` that ragas will accept.

  30-42  THE RUBRIC. The deterministic half -- which needs no model at all -- plus
         the migration and API contracts around it.

  20-23  --live. Two real judge calls. Offline it is unknowable whether ragas'
         `PydanticPrompt` output parses on the OpenRouter route; `llm_check.py`
         documents the same limit about request bodies. Case 22 is the gate:
         known-good and known-bad verdicts must DIFFER.

Usage:
    backend/.venv/Scripts/python.exe scripts/agent_metrics_check.py
    backend/.venv/Scripts/python.exe scripts/agent_metrics_check.py --live

Exits 1 if anything fails.

ASCII only in print(). The Windows console codepage mangles em-dashes, and it has
broken three throwaway scripts in this repo already.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
import warnings
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

# Ragas 0.4.3 routes these through a module-level __getattr__ that emits a
# DeprecationWarning pointing at `ragas.metrics.collections`. Following it breaks
# twice over -- the class names differ, and the collections metrics reject
# `LangchainLLMWrapper`, which is the only wrapper this project can supply. Same
# scoped suppression `app/eval/ragas_runner.py` uses: at the import statement
# only, so a deprecation from anywhere else still surfaces.
with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    from ragas.metrics import (  # noqa: E402
        AgentGoalAccuracyWithoutReference,
        AgentGoalAccuracyWithReference,
        ToolCallAccuracy,
        ToolCallF1,
    )

from ragas.dataset_schema import MultiTurnSample  # noqa: E402
from ragas.messages import AIMessage, HumanMessage, ToolCall, ToolMessage  # noqa: E402

from app.rag.trace import GENERATE, TOOL_CALL, TOOL_ERROR, TOOL_RESULT  # noqa: E402

failures: list[str] = []
LIVE = "--live" in sys.argv


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" -- {detail}" if detail else ""))
    if not condition:
        failures.append(name)


def section(title: str) -> None:
    print()
    print(f"-- {title} " + "-" * max(0, 66 - len(title)))


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
# ONE builder, so every case scores the same SHAPE. `mention_popup_check.py`
# copies `ui_check.py`'s geometry string verbatim for the same reason: a
# divergence here would let one case pass a weaker test than its neighbour.

QUERY = {"query": "Ka-band downlink rate"}
QUERY_REWORDED = {"query": "Ka band downlink data rate"}


def trajectory(calls, *, question="What is the Ka-band downlink rate?",
               final="The Ka-band downlink sustains 1.2 Gbps [1]."):
    """A ragas message list: human, then (assistant + tool result)*, then answer."""
    messages = [HumanMessage(content=question)]
    for name, args in calls:
        messages.append(
            AIMessage(content="Searching the corpus.",
                      tool_calls=[ToolCall(name=name, args=args)])
        )
        messages.append(ToolMessage(content="[1] comms.md#3 ... 1.2 Gbps ..."))
    messages.append(AIMessage(content=final))
    return messages


def sample(pred_calls, ref_calls, **kw):
    return MultiTurnSample(
        user_input=trajectory(pred_calls, **kw),
        reference_tool_calls=[ToolCall(name=n, args=a) for n, a in ref_calls],
    )


def event(event_type, payload):
    """A stand-in for a `TraceEvent` row. The builder reads two attributes."""
    return SimpleNamespace(event_type=event_type, payload=payload)


def tool_events(*, call_id="c1", tool="search_corpus", args=None,
                content="[1] comms.md#3 ... 1.2 Gbps ...", ok=True,
                assistant_text="Searching the corpus."):
    args = QUERY if args is None else args
    call = event(TOOL_CALL, {"step": 1, "tool": tool, "call_id": call_id,
                             "args": args, "assistant_text": assistant_text})
    result = event(
        TOOL_RESULT if ok else TOOL_ERROR,
        {"step": 1, "tool": tool, "call_id": call_id, "ok": ok,
         "summary": "3 results, 2 new", "content": content},
    )
    return [call, result]


def generate_event(*, tool_steps=1, tool_calls=1, stopped_reason=None):
    return event(GENERATE, {"model": "deepseek/deepseek-v4-flash-0731",
                            "tool_steps": tool_steps, "tool_calls": tool_calls,
                            "stopped_reason": stopped_reason})


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------
section("1-7. why the tool metrics are closed")
# --------------------------------------------------------------------------
# These assert the CURRENT behaviour of an installed package, so they pass on the
# first run. They are regression pins on a closed decision, not a red-to-green
# cycle -- and case 6 is the re-check date that a deferral needs as much as a
# measurement does.

tca = ToolCallAccuracy()
tca_loose = ToolCallAccuracy(strict_order=False)
f1 = ToolCallF1()

exact = run(tca.multi_turn_ascore(sample([("search_corpus", QUERY)],
                                         [("search_corpus", QUERY)])))
check(
    "1. ToolCallAccuracy scores 1.0 on byte-identical arguments",
    exact == 1.0,
    f"score={exact}",
)

reworded = run(tca.multi_turn_ascore(sample([("search_corpus", QUERY_REWORDED)],
                                            [("search_corpus", QUERY)])))
check(
    "2. it scores 0.0 on the SAME tool with a differently worded query",
    reworded == 0.0,
    f"score={reworded} -- this is why the tool metrics are closed",
)

empty_ref = run(tca.multi_turn_ascore(sample([("search_corpus", QUERY_REWORDED)],
                                             [("search_corpus", {})])))
check(
    "3. an EMPTY reference arg dict scores 0.0, not 1.0",
    empty_ref == 0.0,
    f"score={empty_ref} -- the 'any args' escape hatch does not exist",
)

two_vs_one_strict = run(tca.multi_turn_ascore(
    sample([("search_corpus", QUERY), ("search_corpus", QUERY_REWORDED)],
           [("search_corpus", QUERY)])))
two_vs_one_loose = run(tca_loose.multi_turn_ascore(
    sample([("search_corpus", QUERY), ("search_corpus", QUERY_REWORDED)],
           [("search_corpus", QUERY)])))
check(
    "4. 2 predicted calls vs 1 reference scores 0.0 in BOTH order modes",
    two_vs_one_strict == 0.0 and two_vs_one_loose == 0.0,
    f"strict={two_vs_one_strict} loose={two_vs_one_loose} -- this model emits 1.50-2.00 calls/step",
)

f1_reworded = run(f1.multi_turn_ascore(sample([("search_corpus", QUERY_REWORDED)],
                                              [("search_corpus", QUERY)])))
check(
    "5. ToolCallF1 also scores 0.0 on a differently worded query",
    f1_reworded == 0.0,
    f"score={f1_reworded}",
)

check(
    "5b. ToolCallF1 exposes no arg_comparison_metric -- it has NO seam",
    not hasattr(f1, "arg_comparison_metric"),
    "args are hashed into a set with no comparator field",
)

arg_score_src = inspect.getsource(ToolCallAccuracy._get_arg_score)
normalised = " ".join(arg_score_src.split())
check(
    "6. _get_arg_score still returns 0.0 for empty refs with non-empty preds",
    "if not refs: return 0.0" in normalised,
    "if this goes RED, ragas changed and the closed decision must be re-opened",
)

check(
    "7. both AgentGoalAccuracy classes report the same metric name",
    AgentGoalAccuracyWithReference().name
    == AgentGoalAccuracyWithoutReference().name
    == "agent_goal_accuracy",
    "so using both in one evaluate() would collide on the result column",
)


# --------------------------------------------------------------------------
section("10-13. the trajectory builder")
# --------------------------------------------------------------------------

try:
    from app.eval import trajectory as trajectory_mod  # noqa: E402

    trajectory_from_rows = trajectory_mod.trajectory_from_rows
    TRAJECTORY_IMPORT_ERROR = ""
except Exception as exc:  # noqa: BLE001 - the point is to report, not to crash
    trajectory_mod = None
    trajectory_from_rows = None
    TRAJECTORY_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

if trajectory_mod is None:
    check("10-13. app/eval/trajectory.py imports", False, TRAJECTORY_IMPORT_ERROR)
else:
    # `ragas.messages` and `langchain_core.messages` both export HumanMessage,
    # AIMessage and ToolMessage. Importing the wrong pair does not raise -- it
    # yields a MultiTurnSample ragas silently reads as empty, so no error-shaped
    # check can see it and this has to read the source.
    #
    # Parsed with `ast`, NOT grepped. The first version of this case did
    # `"langchain_core.messages" not in source` and went red against a correct
    # file, because the module's own docstring EXPLAINS the collision and the
    # check matched its own explanation. Same defect `deck_check.py` case 14
    # records -- looking for the thing must not be doing the thing -- and the
    # general form is that a check over source text cannot tell code from prose
    # about code. An import node can only be an import.
    import ast

    tree = ast.parse(
        Path(ROOT / "backend" / "app" / "eval" / "trajectory.py").read_text(
            encoding="utf-8"
        )
    )
    modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    check(
        "10. trajectory.py imports ragas' message types, never langchain's",
        "ragas.messages" in modules
        and not any(m.startswith("langchain_core.messages") for m in modules),
        f"message modules imported: {sorted(m for m in modules if 'messages' in m)}",
    )

    built = trajectory_from_rows(
        question="What is the Ka-band downlink rate?",
        answer="The Ka-band downlink sustains 1.2 Gbps [1].",
        events=[*tool_events(), generate_event()],
    )
    check(
        "11. a MultiTurnSample builds from real fixture events",
        isinstance(built, MultiTurnSample) and len(built.user_input) == 4,
        f"messages={len(built.user_input) if built else None}",
    )

    # A dropped result row must not silently SHORTEN the trajectory: ragas'
    # field_validator requires a ToolMessage after an AIMessage carrying calls,
    # so skipping it would leave the call in place and change what the judge reads.
    orphan = trajectory_from_rows(
        question="q",
        answer="a",
        events=[tool_events()[0], generate_event()],
    )
    check(
        "12. a TOOL_CALL with no result row synthesises an empty ToolMessage",
        orphan is not None
        and any(isinstance(m, ToolMessage) for m in orphan.user_input),
        f"messages={[type(m).__name__ for m in orphan.user_input] if orphan else None}",
    )

    # Two calls in one step, results recorded in the OPPOSITE order. Pairing by
    # adjacency passes case 11 and fails here.
    interleaved = trajectory_from_rows(
        question="q",
        answer="a",
        events=[
            event(TOOL_CALL, {"step": 1, "tool": "search_corpus", "call_id": "a",
                              "args": {"query": "first"}, "assistant_text": ""}),
            event(TOOL_CALL, {"step": 1, "tool": "search_corpus", "call_id": "b",
                              "args": {"query": "second"}, "assistant_text": ""}),
            event(TOOL_RESULT, {"step": 1, "tool": "search_corpus", "call_id": "b",
                                "ok": True, "summary": "s", "content": "RESULT-B"}),
            event(TOOL_RESULT, {"step": 1, "tool": "search_corpus", "call_id": "a",
                                "ok": True, "summary": "s", "content": "RESULT-A"}),
            generate_event(tool_steps=1, tool_calls=2),
        ],
    )
    contents = [m.content for m in (interleaved.user_input if interleaved else [])
                if isinstance(m, ToolMessage)]
    check(
        "13. calls pair to results by call_id, never by adjacency",
        contents == ["RESULT-A", "RESULT-B"],
        f"tool contents={contents}",
    )

    # --------------------------------------------------------------------
    # 14. A NO-TOOL turn still produces a sample. Watched failing.
    # --------------------------------------------------------------------
    # The first version returned None whenever there were no tool calls, which is
    # correct for the two TOOL-CALL metrics and wrong for the only metric this
    # project uses: goal accuracy asks whether the agent achieved what was asked,
    # and "it answered without searching" is an OUTCOME to judge, not a turn to
    # skip. Nothing offline could see it -- every fixture here has a tool call --
    # and one live run showed it immediately: `searched=False goal_accuracy=None`
    # on a turn with a question, an answer and a reference.
    #
    # The failure mode is the dangerous kind: the card reads "not measured", which
    # a reader would take to mean the judge was unavailable rather than that the
    # model had been efficient.
    direct = trajectory_from_rows(
        question="What is the Ka-band downlink rate?",
        answer="The Ka-band downlink sustains 1.2 Gbps [1].",
        events=[generate_event(tool_steps=0, tool_calls=0)],
    )
    check(
        "14. a turn with NO tool call still yields a judgeable sample",
        direct is not None and len(direct.user_input) == 2,
        f"messages={[type(m).__name__ for m in direct.user_input] if direct else None}",
    )

    # The pair. Returning a sample unconditionally would pass case 14 while making
    # "there is no turn here" unrepresentable.
    check(
        "14b. and None is reserved for a turn with no question at all",
        trajectory_from_rows(question="", answer=None, events=[]) is None,
        "the one state with nothing to describe",
    )


# --------------------------------------------------------------------------
section("30-38. the deterministic rubric")
# --------------------------------------------------------------------------

try:
    from app.eval import trajectory_metrics as rubric_mod  # noqa: E402

    tool_use_verdict = rubric_mod.tool_use_verdict
    summarise_trajectory = rubric_mod.summarise_trajectory
    RUBRIC_IMPORT_ERROR = ""
except Exception as exc:  # noqa: BLE001
    rubric_mod = None
    RUBRIC_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

if rubric_mod is None:
    check("30-38. app/eval/trajectory_metrics.py imports", False, RUBRIC_IMPORT_ERROR)
else:
    searched = [*tool_events(), generate_event()]
    silent = [generate_event(tool_steps=0, tool_calls=0)]

    v = tool_use_verdict(searched, expected="search")
    check("30. expected=search + a search -> tool_use_ok True",
          v["tool_use_ok"] is True, f"{v['tool_use_ok']}")

    # The PAIR is the point. A detector deleted entirely would pass case 30.
    v = tool_use_verdict(silent, expected="search")
    check("31. expected=search + NO call -> tool_use_ok False",
          v["tool_use_ok"] is False, f"{v['tool_use_ok']}")

    v_none_ok = tool_use_verdict(silent, expected="none")
    v_none_bad = tool_use_verdict(searched, expected="none")
    check("32. expected=none discriminates in both directions",
          v_none_ok["tool_use_ok"] is True and v_none_bad["tool_use_ok"] is False,
          f"silent={v_none_ok['tool_use_ok']} searched={v_none_bad['tool_use_ok']}")

    v_null = tool_use_verdict(searched, expected=None)
    summary_null = summarise_trajectory([v_null])
    check(
        "33. expected=None -> tool_use_ok is None, counted in total not measured",
        v_null["tool_use_ok"] is None
        and summary_null["tool_use_ok"]["total"] == 1
        and summary_null["tool_use_ok"]["measured"] == 0,
        f"ok={v_null['tool_use_ok']} summary={summary_null['tool_use_ok']}",
    )

    zero_steps = tool_use_verdict(silent, expected=None)
    six_over_three = tool_use_verdict(
        [*tool_events(), generate_event(tool_steps=3, tool_calls=6)], expected=None
    )
    check(
        "34. calls_per_step is None at zero steps and 2.0 for 6 calls over 3 steps",
        zero_steps["calls_per_step"] is None
        and six_over_three["calls_per_step"] == 2.0,
        f"zero={zero_steps['calls_per_step']} six={six_over_three['calls_per_step']}",
    )

    forced = tool_events()
    forced[0].payload["args"] = {**QUERY, "trigger": "gap_detected"}
    check(
        "35. gap_forced is True only when a call carries trigger=gap_detected",
        tool_use_verdict([*forced, generate_event()], expected=None)["gap_forced"] is True
        and tool_use_verdict(searched, expected=None)["gap_forced"] is False,
        "read off args.trigger",
    )

    check(
        "37. summarise_trajectory([]) returns None, never a dict of zeros",
        summarise_trajectory([]) is None,
        "a mean over nothing is not zero",
    )

    # --------------------------------------------------------------------
    # 38. THE ROUND TRIP, and it caught a live defect the moment it existed.
    # --------------------------------------------------------------------
    # `eval_runs.summary` is JSONB with no enforced shape, so `RunSummary` IS the
    # schema -- and pydantic's default is `extra="ignore"`. The first version of
    # this feature merged the block on afterwards
    # (`{**run.summary, "trajectory": ...}`), which the database stored happily
    # and the API then silently DROPPED on the way out: the column held it, no
    # reader could see it, and nothing raised at either end.
    #
    # S35 went green over it, because a live scenario reads the COLUMN. Only a
    # round trip through the model that the API reads back through can see this,
    # which is why the assertion is shaped as a round trip rather than as "is the
    # key present in the dict".
    from app.eval.metrics_guide import RunSummary, summarise  # noqa: E402

    block = summarise_trajectory([tool_use_verdict(searched, expected="search")])
    written = summarise([], self_judged=False, trajectory=block)
    read_back = RunSummary.model_validate(written).model_dump()
    check(
        "38. the trajectory block survives a RunSummary round trip",
        read_back.get("trajectory") is not None
        and "tool_use_ok" in (read_back.get("trajectory") or {}),
        f"round-tripped keys={sorted((read_back.get('trajectory') or {}).keys())[:3]}",
    )

    # The pair. A declared field that is always populated would pass case 38 while
    # making "the pass did not run" unrepresentable -- and None is the only way to
    # say that, since a dict of zeros claims it ran and found nothing.
    absent = RunSummary.model_validate(
        summarise([], self_judged=False, trajectory=None)
    ).model_dump()
    check(
        "38b. and is None -- not {} -- when the pass did not run",
        absent.get("trajectory") is None,
        f"trajectory={absent.get('trajectory')!r}",
    )


# --------------------------------------------------------------------------
section("40-42. migration and API contracts")
# --------------------------------------------------------------------------

# Read from ONE revision, and check the TABLE as well as the column.
#
# The first version of this case concatenated every file in versions/ and grepped
# for four column names. It reported `tools_enabled` and `max_tool_steps` already
# satisfied -- because both exist on the `agents` table from an earlier revision.
# A check that passes on a different revision adding a same-named column to a
# different table is a check that cannot fail for the reason it was written, which
# is the whole "a test that cannot fail reports success" family.
REVISION = "a3c81f5d2e07"
versions = ROOT / "backend" / "alembic" / "versions"
revision_files = list(versions.glob(f"{REVISION}*.py"))
migration_text = (
    revision_files[0].read_text(encoding="utf-8") if revision_files else ""
)
wanted = [
    ("golden_questions", "expected_tool_use"),
    ("eval_results", "trajectory"),
    ("eval_runs", "tools_enabled"),
    ("eval_runs", "max_tool_steps"),
]
normalised_migration = " ".join(migration_text.split())
missing = [
    f"{table}.{column}"
    for table, column in wanted
    if f'add_column( "{table}", sa.Column("{column}"' not in normalised_migration
    and f'add_column("{table}", sa.Column("{column}"' not in normalised_migration
]
check(
    "40. ONE revision adds all four columns, each to the right table",
    bool(revision_files) and not missing,
    f"missing={missing}" if missing or not revision_files else f"{REVISION}",
)

dropped = [
    f"{table}.{column}"
    for table, column in wanted
    if f'drop_column("{table}", "{column}")' not in normalised_migration
]
check(
    "40b. downgrade() drops exactly those four",
    not dropped,
    f"not dropped={dropped}" if dropped else "reversible",
)

try:
    from app.eval.trajectory_metrics import EXPECTED_TOOL_USE_VALUES  # noqa: E402

    check(
        "41. expected_tool_use is validated against a set, never defaulted",
        EXPECTED_TOOL_USE_VALUES == frozenset({"search", "none", "python"}),
        f"{sorted(EXPECTED_TOOL_USE_VALUES)}",
    )
except Exception as exc:  # noqa: BLE001
    check("41. EXPECTED_TOOL_USE_VALUES is defined", False, f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------
if LIVE:
    section("20-23. --live: two real judge calls")
    # ----------------------------------------------------------------------
    # The only part that can answer whether ragas' PydanticPrompt output parses
    # on the OpenRouter route. A failed parse costs an extra fix_output_format
    # call per retry and surfaces as RagasOutputParserException.
    from app.eval.ragas_runner import get_judge  # noqa: E402
    from app.config import settings  # noqa: E402

    judge = get_judge(settings.ragas_judge_model)

    async def goal(messages, reference):
        metric = AgentGoalAccuracyWithReference(llm=judge)
        return await metric.multi_turn_ascore(
            MultiTurnSample(user_input=messages, reference=reference)
        )

    good = run(goal(
        trajectory([("search_corpus", QUERY)]),
        "The agent reports the Ka-band downlink rate as 1.2 Gbps.",
    ))
    check("20. a known-GOOD trajectory scores 1.0", good == 1.0, f"score={good}")

    bad = run(goal(
        trajectory([("search_corpus", QUERY)],
                   final="Solar arrays generate 12 kW at end of life [1]."),
        "The agent reports the Ka-band downlink rate as 1.2 Gbps.",
    ))
    check("21. a known-BAD trajectory scores 0.0", bad == 0.0, f"score={bad}")

    # THE GATE. EVAL.md records a judge scoring a verbatim-from-context answer
    # 0.000 while another scored it 1.000; a metric that cannot separate these
    # two does not ship, whatever the plan says.
    check(
        "22. the two verdicts DIFFER -- the metric discriminates",
        good != bad,
        f"good={good} bad={bad}",
    )

    refusal = run(goal(
        trajectory(
            [("search_corpus", {"query": "modulation and coding scheme"})],
            question="Which modulation scheme does the Ka-band downlink use?",
            final="The corpus does not cover the modulation and coding scheme.",
        ),
        "The agent searches the corpus, finds nothing on the modulation scheme, "
        "and declines to answer rather than inventing one.",
    ))
    check(
        "23. a correct REFUSAL trajectory scores 1.0",
        refusal == 1.0,
        f"score={refusal} -- the proposition faithfulness cannot express",
    )
else:
    print()
    print("  (20-23 not run: pass --live for two real judge calls)")



# --------------------------------------------------------------------------
section("54-59. INSTRUMENT REPAIRS -- numbers the rubric would render WRONG")
# --------------------------------------------------------------------------
# These cases were written BEFORE their fixes and watched failing. None is a new
# feature: each pins a number the trajectory rubric renders TODAY that would be
# confidently wrong on a real scorecard -- the failure mode this repository has
# now recorded eleven times.
#
# The scorecard has never rendered any of them: `eval_results.trajectory IS NOT
# NULL` is 0 of 50 and `eval_runs.summary ? 'trajectory'` is 0 of 5. That is
# exactly why now is the moment. An instrument is cheapest to repair before
# anyone has acted on a number it produced.

# --- 54: a FAILED search must not read as "it searched" ---------------------
#
# `tool_use_verdict` filtered on TOOL_CALL alone, and `ask.run_turn` records a
# TOOL_CALL for a call that FAILED, deliberately. So a turn in which every search
# raised reported `searched=True, tool_use_ok=True`. The loop's own gate is
# stricter and requires `invocation.ok` -- the rubric was the looser of the two,
# and unfired in production only because there are zero TOOL_ERROR rows there.
failed_only = tool_events(ok=False, content="ConnectionError: pinecone unreachable")
verdict_failed = tool_use_verdict(
    [*failed_only, generate_event(tool_steps=1, tool_calls=1)], expected="search"
)
check(
    "54a. a turn whose only search FAILED does not report searched=True",
    verdict_failed["searched"] is False,
    f"searched={verdict_failed['searched']}",
)
check(
    "54a. and it is not graded as satisfying expected_tool_use='search'",
    verdict_failed["tool_use_ok"] is False,
    f"tool_use_ok={verdict_failed['tool_use_ok']}",
)
# THE PAIR. An assertion that a feature does NOT fire is also passed by a feature
# that has been deleted, so it is only ever written beside one asserting it DOES.
verdict_ok = tool_use_verdict(
    [*tool_events(ok=True), generate_event(tool_steps=1, tool_calls=1)],
    expected="search",
)
check(
    "54b. a turn whose search SUCCEEDED still reports searched=True",
    verdict_ok["searched"] is True and verdict_ok["tool_use_ok"] is True,
    f"searched={verdict_ok['searched']} ok={verdict_ok['tool_use_ok']}",
)

# --- 55: a gap-FORCED call is a code decision, not an agent decision --------
#
# `expected_tool_use="none"` asserts "the agent showed no reflex tool use". The
# gap trigger re-invokes with a NAMED tool, so the CODE compelled that call.
# Scoring it against the agent is the same category error as `refusal_pass`
# blaming the agent for a marker list.
forced_events = tool_events(args={"query": "vendor", "trigger": "gap_detected"})
verdict_forced = tool_use_verdict(
    [*forced_events, generate_event(tool_steps=1, tool_calls=1)], expected="none"
)
check(
    "55a. a gap-FORCED call does not fail expected_tool_use='none'",
    verdict_forced["tool_use_ok"] is True,
    f"tool_use_ok={verdict_forced['tool_use_ok']} "
    f"gap_forced={verdict_forced['gap_forced']}",
)
verdict_chosen = tool_use_verdict(
    [*tool_events(), generate_event(tool_steps=1, tool_calls=1)], expected="none"
)
check(
    "55b. but a MODEL-CHOSEN call still does (the pair)",
    verdict_chosen["tool_use_ok"] is False,
    f"tool_use_ok={verdict_chosen['tool_use_ok']}",
)

# --- 56: crashed turns must stay in the denominator -------------------------
#
# EVAL.md's misleading-way #1, reproduced inside the new block: each metric's
# mean has its own denominator and the footnote does not. A turn that crashed has
# no trajectory, and dropping it from `total` reports "8 of 8 achieved" on a
# ten-question run.
mixed_rows = [
    {"goal_accuracy": 1.0, "tool_use_ok": True, "calls_per_step": 2.0},
    {"goal_accuracy": 0.0, "tool_use_ok": False, "calls_per_step": 1.0},
    {"error": "judge timed out"},
    {},
]
summary_mixed = summarise_trajectory(mixed_rows)
check(
    "56. total counts EVERY row, including those that scored nothing",
    summary_mixed["goal_accuracy"]["total"] == 4
    and summary_mixed["goal_accuracy"]["measured"] == 2,
    f"measured={summary_mixed['goal_accuracy']['measured']} "
    f"total={summary_mixed['goal_accuracy']['total']}",
)

# --- 57: refusal rows must not be pooled with answerable ones ---------------
#
# `summarise()` separates refusals from the four RAG metrics in twenty lines of
# comment, because a correct refusal retrieves nothing and its metrics are
# meaningless. The trajectory block pooled them into one rate -- and refusals
# score 1.0 near-constantly, so on a ten-question set 20% of the denominator is a
# near-constant pass that damps any real movement.
split_rows = [
    {"goal_accuracy": 1.0, "expected_behaviour": "answer"},
    {"goal_accuracy": 0.0, "expected_behaviour": "answer"},
    {"goal_accuracy": 1.0, "expected_behaviour": "refuse"},
    {"goal_accuracy": 1.0, "expected_behaviour": "refuse"},
]
summary_split = summarise_trajectory(split_rows)
check(
    "57a. goal accuracy is split by behaviour class",
    "goal_accuracy_answer" in summary_split
    and "goal_accuracy_refuse" in summary_split,
    f"keys={sorted(k for k in summary_split if k.startswith('goal_accuracy'))}",
)
check(
    "57b. and the two carry SEPARATE denominators",
    (summary_split.get("goal_accuracy_answer") or {}).get("total") == 2
    and (summary_split.get("goal_accuracy_refuse") or {}).get("total") == 2,
    f"answer={summary_split.get('goal_accuracy_answer')} "
    f"refuse={summary_split.get('goal_accuracy_refuse')}",
)

# --- 58: the counted efficiency signals ------------------------------------
#
# `loop.md` section 1 applied literally: a number decides it, so write the
# branch. No model, no threshold, no new instrument of unknown validity.
# `new_chunks` is already computed by `corpus.py` and stored on every TOOL_RESULT
# payload -- and 8 of 22 of the model's own searches in production returned zero
# new chunks while nothing aggregated it.
wasteful = [
    *tool_events(call_id="a", content="x"),
    *tool_events(call_id="b", content="y"),
    generate_event(tool_steps=1, tool_calls=2),
]
wasteful[1].payload["new_chunks"] = 2
wasteful[3].payload["new_chunks"] = 0
v_waste = tool_use_verdict(wasteful, expected="search")
check(
    "58a. a search returning zero NEW chunks is counted redundant",
    v_waste.get("redundant_searches") == 1,
    f"redundant_searches={v_waste.get('redundant_searches')}",
)
check(
    "58b. and both searches are counted in the denominator",
    v_waste.get("searches") == 2,
    f"searches={v_waste.get('searches')}",
)
v_forced_only = tool_use_verdict(
    [*forced_events, generate_event()], expected="search"
)
check(
    "58c. self_initiated separates a model decision from a forced one",
    v_waste.get("self_initiated") is True
    and v_forced_only.get("self_initiated") is False,
    f"chosen={v_waste.get('self_initiated')} forced={v_forced_only.get('self_initiated')}",
)
agg = summarise_trajectory(
    [
        {"redundant_searches": 1, "searches": 2, "self_initiated": True},
        {"redundant_searches": 0, "searches": 1, "self_initiated": False},
    ]
)
check(
    "58d. the aggregate reports a wasted-search RATE with its denominator",
    agg.get("redundant_searches") == 1
    and agg.get("searches") == 3
    and abs((agg.get("wasted_search_rate") or 0) - (1.0 / 3.0)) < 1e-9,
    f"redundant={agg.get('redundant_searches')} searches={agg.get('searches')} "
    f"rate={agg.get('wasted_search_rate')}",
)

# --- 59: the refusal claim this module makes is MEASURED to be false -------
#
# `trajectory_metrics` documented goal accuracy's "sharpest use" as the refusal
# question -- "did it search, find nothing, and decline". Measured 3/3 each: a
# refusal that searched and a refusal that never searched BOTH score 1.0, because
# ragas discards the inferred goal and compares only `end_state` to the
# reference, and every stored refusal reference is a CONTENT statement rather
# than a process one.
#
# The honest repair is to withdraw the claim and report `searched` beside the
# verdict, which 58c makes available. This case asserts the withdrawal, so the
# claim cannot quietly return.
_TM_SRC = (ROOT / "backend" / "app" / "eval" / "trajectory_metrics.py").read_text(
    encoding="utf-8"
)
check(
    "59. the 'searched, found nothing, declined' claim is withdrawn in source",
    "MEASURED TO BE FALSE" in _TM_SRC,
    "replaced by self_initiated + searches, which need no judge"
    if "MEASURED TO BE FALSE" in _TM_SRC
    else "the docstring still asserts a proposition measured to be inexpressible",
)

print()
if failures:
    print(f"FAILED: {len(failures)} check(s):")
    for name in failures:
        print(f"  - {name}")
    sys.exit(1)
print("All agent-metric checks passed.")
