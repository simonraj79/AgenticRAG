# 03 — The rubric: one judged metric, one counted one

> Contracts consumed: [PLAN.md](PLAN.md) §2.4 (why the tool metrics are closed), §4.3, §4.4.
> Not restated here. **Feature [01](01-metric-calibration.md) gates this file** — a metric that
> fails calibration does not ship, whatever this document says.

## What the user gets

A verdict on whether the agent *did the right thing*, next to the existing verdict on whether its
answer was faithful. Two numbers, and they are deliberately of different kinds.

## The split, and why it is not one metric

**Goal accuracy is judged. Tool use is counted.** That is [loop.md §1](../loop.md) applied
literally — *if a number decides it, write the `if`* — and it is also the only reading of
[PRD.md](../../PRD.md) open item 23 that respects its warning about "a new instrument of unknown
validity". The invented half is a count; the scored half is one Ragas already ships.

### The judged half — `AgentGoalAccuracyWithReference`

It scores **outcome, not path**, which is the property that makes it usable here: this agent's
path legitimately varies (the rewriter rewrites every query, the model over-calls by design), and
every metric that scores the path reads zero on correct behaviour (§2.4).

`reference` comes from `golden_questions.reference_answer`, which already exists. A question with
no reference answer is **not scored** — `None`, never `0.0`.

**Its sharpest use is the refusal question**, and this is the argument for the whole feature.
"Did the agent search, find nothing, and decline" is a proposition about the trajectory that
`faithfulness` cannot express — a correct refusal retrieves nothing useful and deliberately does
not follow from its context, so faithfulness scores it near zero for behaving perfectly, and
[EVAL.md §6](../../EVAL.md) excludes it from the means for exactly that reason. Goal accuracy
reaches a verdict on it **without consulting the marker list in `app/rag/refusal.py`**, which
CLAUDE.md records being wrong five times in five different ways.

**Binary, not continuous.** It returns 1 or 0. Aggregated it is a *pass rate over n*, and it must
never be rendered in the same visual grammar as a faithfulness mean.

### The counted half — tool use

Read straight off the trajectory. No model, no threshold, no invented score:

| Field | Meaning |
|---|---|
| `expected_tool_use` | from the golden question: `search` / `none` / `python` / `NULL` |
| `tool_use_ok` | `bool` when an expectation was authored, **`None` when it was not** |
| `searched` | at least one successful `search_corpus` call |
| `tool_calls`, `tool_steps` | as `GENERATE.payload` already records them |
| `calls_per_step` | `tool_calls / tool_steps`, or `None` when `tool_steps == 0` |
| `gap_forced` | any call carrying `args.trigger == "gap_detected"` |
| `stopped_reason` | `max_steps` / `tool_error` / `None` |

**`calls_per_step` is reported and never graded.** Nobody has authored a threshold, and
`score_threshold` is the standing precedent for what happens when a number is graded against a
band that overlaps — CLAUDE.md calls it advisory for that reason. Reporting it is still the point:
CLAUDE.md records the model emitting 1.50–2.00 calls per step and names the resulting silent
budget doubling as **"the test you never wrote"**. This is that number, on the card.

**`tool_use_ok` is `None`, not `False`, when nothing was authored.** A run over a golden set with
no expectations must report *not measured*, not a perfect zero.

## Technical detail

`app/eval/trajectory_metrics.py`:

```python
async def score_trajectory(sample, *, reference, expected_tool_use, events) -> dict
def summarise_trajectory(rows: list[dict]) -> dict | None
```

- The metric is constructed **per turn**, never shared, for the reason `ragas_runner.py:208-267`
  already documents: one mutable metric instance across concurrent scoring is a race nobody would
  find from the symptom.
- The judge is the existing `get_judge()` — same `RAGAS_JUDGE_MODEL`, same `reasoning_effort`, no
  `top_k`.
- Same `METRIC_TIMEOUT_S` handling and the same `_clean()` NaN discipline as the RAG path.
- The judged half is wrapped so a failure lands in `error` and leaves the counted half intact.
  **A judge outage must not delete the deterministic numbers**, which need no provider at all.

`summarise_trajectory` returns `Measured`-shaped entries — value, measured, total — because
`admin.py`'s module rule is that every aggregate reports its own denominator.

## Acceptance criteria

| # | Case | Asserts |
|---|---|---|
| C1 | `agent_metrics_check.py` 30 | `expected_tool_use="search"` + a trajectory containing a search → `tool_use_ok is True` |
| C2 | `agent_metrics_check.py` 31 | `expected_tool_use="search"` + a trajectory with **no** call → `tool_use_ok is False`. **The pair with C1 is the point** — a deleted detector would pass C1 alone |
| C3 | `agent_metrics_check.py` 32 | `expected_tool_use="none"` + a search → `False`; + no call → `True` |
| C4 | `agent_metrics_check.py` 33 | `expected_tool_use=None` → `tool_use_ok is None`, and `summarise_trajectory` counts the row in `total` but not in `measured` |
| C5 | `agent_metrics_check.py` 34 | `calls_per_step` is `None` when `tool_steps == 0`, and `2.0` for 6 calls over 3 steps |
| C6 | `agent_metrics_check.py` 35 | `gap_forced` is `True` only when a call carries `args.trigger == "gap_detected"` |
| C7 | `agent_metrics_check.py` 36 | A raising judge leaves `goal_accuracy is None` **and** every counted field populated |
| C8 | `agent_metrics_check.py` 37 | `summarise_trajectory([])` returns `None`, never a dict of zeros |
| C9 | `agent_metrics_check.py` 22 (`--live`) | Known-good and known-bad goal-accuracy verdicts differ. **Feature 01's gate.** If this fails, `TRAJECTORY_GOAL_ACCURACY` ships defaulting to `false` and the card says the metric is unvalidated |

## What must keep working

- **The four RAG metrics and their means do not move.** This is a second pass over a second
  dataset; `summarise()` in `metrics_guide.py` is untouched, so every
  [EVAL.md §10](../../EVAL.md) baseline stays comparable.
- **`EVAL_TRAJECTORY_ENABLED=false` reproduces today byte-for-byte** — `eval_results.trajectory`
  stays `NULL` and `eval_runs.summary` gains no key. Asserted by `agent_metrics_check.py` 38.
- **Refusal rows keep their existing treatment.** They stay excluded from the four RAG means and
  graded on `behaviour_ok`; goal accuracy is reported *beside* that, never folded into it.
