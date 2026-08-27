# 04 — Migration and eval-run wiring

> Contracts consumed: [PLAN.md](PLAN.md) §4.1, §4.4 (the single migration for this change set).
> Not restated here.

## What the user gets

The trajectory rubric is computed on every eval run and stored beside the scorecard, and a run
finally records **the tool configuration it was measured under** — so two runs can be compared
without guessing.

## The gap this closes beyond the rubric

[EVAL.md §5](../../EVAL.md) already names it: `eval_runs` captures `judge_model` and
`generation_model` but not `tools_enabled` or `max_tool_steps`. Toggle tools between two runs and
the numbers move for a reason the card cannot state. That is the same defect class as
`self_judged` — provenance the scorecard needs in order to mean anything — and it is cheap to fix
in the migration this change set already needs.

`NULL` means *this run predates the column*, which is a different fact from `false`. The console
renders it as "not recorded", never as "off". [14-admin-observability](../14-admin-observability/)
records that exact confusion shipping **twice** in one file.

## Technical detail

**One alembic revision**, `down_revision` pinned to the head at branch time, adding the four
columns in PLAN.md §4.4. Additive and nullable throughout, so it is safe to apply before the merge
— and per [build.md §8](../build.md), **merge promptly afterwards**: between the apply and the
merge the database claims a revision the deployed code does not contain, and any restart inside
that window crash-loops the service while `/api/health` keeps answering 200.

**`eval/jobs.py::_run_one_question`** gains the second pass, after the existing RAG scoring and
inside the same per-question try/except that already re-loads objects after a rollback. The
trajectory row is written onto `eval_results.trajectory`.

**`run_eval_job`** stamps `tools_enabled` and `max_tool_steps` from the agent at run-open time,
beside where it already stamps `judge_model` and `generation_model` — read from the agent, never
back from `agents` at read time, for the reason `eval_runs` already keeps its own copy of the
models: the row can change after the run.

**Metering.** The job already runs inside `collect_usage()` + `meter_as(call_kind="judge")`, so
the extra judged calls are attributed with no new scope. `metering_check.py` case 12 walks the
call graph and would fail if the new call site escaped one — it is the case that caught the
unmetered golden-set drafter.

**Golden-question authoring.** `expected_tool_use` is exposed on the existing
`GoldenQuestionOut` / PATCH / POST shapes and on export/import. Import must accept a payload
without the field — an older export is still valid and must not 422.

## Acceptance criteria

| # | Case | Asserts |
|---|---|---|
| D1 | `agent_metrics_check.py` 40 | The migration file's `upgrade()` adds all four columns, and `downgrade()` drops exactly those four. Read from the revision source |
| D2 | `agent_metrics_check.py` 41 | `expected_tool_use` accepts `search` / `none` / `python` / `NULL` and the API **422s** on anything else — validated against a set, never silently defaulted, copying `/spend`'s `group_by` |
| D3 | `agent_metrics_check.py` 42 | A golden-question import payload **without** `expected_tool_use` succeeds |
| D4 | `metering_check.py` 12 | Unchanged and still green — no entry point reaches `build_chat_model` outside a `meter_as` scope |
| D5 | `agentic_check.py` S35 | A real eval run writes `eval_results.trajectory` non-null and `eval_runs.tools_enabled` non-null. **Layer 1 cannot prove a query runs** — this is the `--live` lesson from `admin_check.py` |

## What must keep working

- **Every existing golden question stays valid** with `expected_tool_use = NULL`. No backfill:
  inventing expectations nobody authored would make D2's `None` branch untestable and would put
  fabricated ground truth into a measurement.
- **`eval_runs.summary` stays readable by the existing UI.** The trajectory block is a new key;
  `RunSummary`'s existing fields are untouched, so `Scorecard.tsx` renders exactly as before.
- **A run still completes when the judge fails.** `status="failed"` remains reserved for run
  machinery breaking, per `eval/jobs.py`'s existing contract.
