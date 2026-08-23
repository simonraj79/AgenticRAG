# 01 — Metric calibration

> Contracts consumed: [PLAN.md](PLAN.md) §2.1 (what ragas ships), §2.4 (what is closed).
> Not restated here.

## What the user gets

Nothing directly. This is the harness that **decides what feature 03 is allowed to ship**, and it
is written and watched failing before any other line of this change set exists.

## Why it comes first

[EVAL.md](../../EVAL.md) records a judge scoring an answer copied verbatim from its own context
**0.000**, and a second judge scoring the identical stored answer **1.000**. Nothing on the
scorecard distinguished the two. The rule that came out of it is the constraint on this whole
change set:

> **A metric that cannot discriminate is worse than no metric, because the scorecard still
> renders.**

So every metric is measured against a **known-good and a known-bad case** before it is trusted,
and a metric that cannot separate them does not ship. That is a *calibration*, not a test of the
feature — which is why it is feature 01 rather than part of feature 03's acceptance criteria.

The two tool metrics need no LLM at all, so the entire tool half of this calibration costs **zero
tokens and runs offline in milliseconds**. That is what makes closing them a measurement rather
than an argument.

## Technical detail

`scripts/agent_metrics_check.py`, layer 1 by default: no database, no network, no model.

**Fixture trajectories** are built by one helper so every case scores the *same* shape, the way
`mention_popup_check.py` copies `ui_check.py`'s `GEOMETRY` string verbatim rather than writing
something similar:

```python
def trajectory(calls, *, question=..., final=...) -> list  # ragas messages
```

`--live` adds exactly two real judge calls through this repo's own `build_chat_model`, and is the
only part that can answer whether ragas' `PydanticPrompt` output parses on the OpenRouter route.
Offline, that is unknowable; `llm_check.py`'s documented limit is the same one.

**ASCII only in `print()`.** Three throwaway scripts here have been broken by the Windows console
codepage.

## Acceptance criteria

Each names a case id in `scripts/agent_metrics_check.py`.

| # | Case | Asserts |
|---|---|---|
| A1 | 1 | `ToolCallAccuracy` scores **1.0** when predicted and reference args are byte-identical — the control that proves the harness drives the metric at all |
| A2 | 2 | It scores **0.0** on the same tool with a *differently worded* query. **The reason the tool metrics are closed** |
| A3 | 3 | It scores **0.0** with `reference_tool_calls=[ToolCall(name=..., args={})]` — the "any args" escape hatch does not exist |
| A4 | 4 | It scores **0.0** for 2 predicted calls against 1 reference call, `strict_order=True` and `False` alike |
| A5 | 5 | `ToolCallF1` scores **0.0** on a differently worded query, and case 5b asserts `ToolCallF1` exposes **no** `arg_comparison_metric` attribute — it has no seam |
| A6 | 6 | `_get_arg_score` returns `0.0` for empty refs with non-empty preds, read off the installed source by `inspect.getsource`. **If a future ragas fixes this, the case goes red and the closed decision gets re-opened deliberately** |
| A7 | 7 | Both `AgentGoalAccuracy*` classes report `name == "agent_goal_accuracy"`, so 03 must rename if it ever uses both |
| A8 | 10 | `app/eval/trajectory.py` imports ragas' message types and **never** `langchain_core.messages` — asserted over the source text, because the two are same-named and a silent swap yields an empty trajectory rather than an error |
| A9 | 11 | A `MultiTurnSample` built from this repo's real fixture events constructs without raising — i.e. ragas' `field_validator` ordering rule is satisfied |
| A10 | 12 | A trajectory whose `TOOL_RESULT` row is **missing** still constructs, with a synthesised empty `ToolMessage`. A dropped result must not silently shorten the trajectory |
| A11 | 20 (`--live`) | `AgentGoalAccuracyWithReference` scores a **known-good** trajectory `1.0` |
| A12 | 21 (`--live`) | It scores a **known-bad** trajectory — the agent answered a different question than the reference states — `0.0` |
| A13 | 22 (`--live`) | Cases 20 and 21 **differ**. This is the case that would have caught the `0.000`-verbatim judge, and a metric failing it does not ship |
| A14 | 23 (`--live`) | A correct **refusal** trajectory against a refusal reference scores `1.0` — the proposition `faithfulness` structurally cannot express |

**Cases 1–7 are expected to pass immediately**, because they assert the *current* behaviour of an
installed package. They are regression pins on a closed decision, not a red-to-green cycle — and
case 6 is written so that a future ragas release changing `_get_arg_score` **breaks the build**
rather than silently leaving a wrong note in this plan. That is the deferral-with-a-re-check-date
rule from [insights.md §24](../../insights.md).

**Cases 10–12 must be watched failing**, because `app/eval/trajectory.py` does not exist yet.

## What must keep working

- `scripts/llm_check.py` stays green — this change set adds no request field. The judge is
  constructed through the existing `get_judge()`, which already omits `top_k`.
- No new dependency. `ragas`, `langchain-community` and their pins are untouched;
  `grep -n pywin32 backend/requirements.txt` still shows the marker.
