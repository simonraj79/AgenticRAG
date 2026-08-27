# 16 — Agent evaluation: measuring the trajectory, not just the answer

**Status: in progress.** Change set 16. Branch `feat/agent-trajectory-eval`, stacked on
`feat/failure-paths` (PR #11 → PR #10 → `main`).

This plan owns every contract the feature files reference. **They do not restate it** — a
contract stated twice drifts, and the copy that drifted is never the one you are reading
([build.md §3](../build.md)).

---

## 1. What this closes

**[PRD.md](../../PRD.md) open item 23 — "Tool use is unmeasured."** Its own words set the bar
this plan has to clear:

> Ragas scores whether an answer is faithful to its context; it has no opinion on whether the
> right tool was called, and **inventing a faithfulness-shaped score for tool choice would be a
> new instrument of unknown validity — the exact failure items 15 and 16 record.** Trajectory
> evaluation is Stage 4.

And **open item 30** — self-check is unmeasured, and must not be measured with faithfulness,
because open item 20 records faithfulness scoring a teaching persona's analogy as an unsupported
claim and then advising the pedagogy's deletion. It says what is needed instead: *a trajectory
measure*.

So the deliverable is Stage 4, and the constraint is that it must not be a second instrument of
unknown validity.

---

## 2. Audit

### 2.1 What Ragas 0.4.3 actually ships — verified by import, never from docs

Five agent metrics. **The legacy `ragas.metrics` path works; `ragas.metrics.collections` does
not**, failing at construction with the `ValueError` [EVAL.md](../../EVAL.md) already records for
the RAG metrics:

```
ValueError: Collections metrics only support modern InstructorLLM.
            Found: LangchainLLMWrapper. Use: llm_factory('gpt-4o-mini', client=openai_client)
```

That decision therefore extends to agent metrics unchanged, and `ragas_runner.py`'s scoped
`warnings.catch_warnings()` is the pattern to copy.

| Class | LLM? | `_required_columns[MULTI_TURN]` | Output |
|---|---|---|---|
| `ToolCallAccuracy` | **no** | `{user_input, reference_tool_calls}` | float × binary sequence gate |
| `ToolCallF1` | **no** | `{user_input, reference_tool_calls}` | `round(f1, 4)` |
| `AgentGoalAccuracyWithReference` | yes | `{user_input, reference}` | **binary 1 / 0** |
| `AgentGoalAccuracyWithoutReference` | yes | `{user_input}` | **binary 1 / 0** |
| `TopicAdherenceScore` | yes | `{user_input, reference_topics}` | P / R / F1, default `f1` |

None is `MetricWithEmbeddings`. None accepts `n` / multiple candidates, so the
`ResponseRelevancy(strictness=1)` trap does not recur. Both goal-accuracy classes report
`name="agent_goal_accuracy"`, so putting both in one `evaluate()` collides on the result column.

**`EvaluationDataset` is homogeneous.** Mixing a `MultiTurnSample` with a `SingleTurnSample`
raises `ValueError: Sample at index 1 is of type <SingleTurnSample>, expected <MultiTurnSample>`.
**Agent metrics therefore need a second, separate scoring pass beside the existing RAG one** —
this is a structural fact, not a style choice.

Input types are ragas' own pydantic models in `ragas.messages`, **not langchain's**:

```python
class ToolCall(BaseModel):     name: str; args: dict[str, Any]
class HumanMessage(Message):   type: Literal["human"] = "human"
class ToolMessage(Message):    type: Literal["tool"]  = "tool"
class AIMessage(Message):      type: Literal["ai"] = "ai"; tool_calls: list[ToolCall] | None = None

class MultiTurnSample(BaseSample):
    user_input:           list[HumanMessage | AIMessage | ToolMessage]
    reference:            str | None = None
    reference_tool_calls: list[ToolCall] | None = None
    rubrics:              dict[str, str] | None = None
    reference_topics:     list[str] | None = None
```

A `field_validator` on `user_input` enforces ordering: a `ToolMessage` must directly follow an
`AIMessage` **whose `tool_calls` is non-empty**, or another `ToolMessage`. A trajectory builder
that emits a `ToolMessage` after a bare `AIMessage` raises at construction — loudly, which is the
right direction.

### 2.2 What this system already persists

Every tool call is durably recorded, **with its name and its full argument dict**, from exactly
one place — `backend/app/api/ask.py:1259-1297`:

```python
for invocation in result.tool_calls:
    trace.record(TOOL_CALL, payload={
        "step": invocation.step, "tool": invocation.tool,
        "call_id": invocation.call_id, "args": invocation.args})
    if invocation.ok:
        trace.record(TOOL_RESULT, payload={..., "ok": True,
                     "summary": invocation.summary, **invocation.detail},
                     duration_ms=invocation.duration_ms)
    else:
        trace.record(TOOL_ERROR, payload={..., "ok": False,
                     "error": invocation.error, **invocation.detail},
                     duration_ms=invocation.duration_ms)
```

| Fact | Where |
|---|---|
| tool name | `trace_events.payload->>'tool'` |
| tool arguments, verbatim | `trace_events.payload->'args'` |
| ordering | `trace_events.step_index`, plus `payload->>'step'` (loop step) |
| call ↔ result pairing | `payload->>'call_id'`, identical on both rows |
| model-chosen vs code-forced | `args.trigger == "gap_detected"` (`agent_loop.py:1048-1049`) |
| turn totals | `GENERATE.payload.tool_steps`, `.tool_calls`, `.stopped_reason` |

**Eval turns get full trace rows**, because `eval/jobs.py:368` goes through `run_turn`.

### 2.3 What is missing, and the good news about where

1. **Intermediate assistant text is discarded.** `run_agent_loop` builds a local
   `messages: list[BaseMessage]` (`agent_loop.py:814`) and returns only `LoopResult.text`.
2. **The tool's returned content — the string the model actually read — is discarded.**
   `_execute` returns `(ToolInvocation, ToolMessage)`; `ToolMessage.content` goes into the local
   list and is dropped. `TOOL_RESULT` keeps only `summary` plus the structured `detail`.
3. **No reference trajectory exists.** `golden_questions` is
   `id, created_at, agent_id, question, reference_answer, expected_behaviour, is_active, source,
   order_index` — nothing agent-shaped.
4. **`eval_runs` does not record the agent's tool configuration.** It captures `judge_model` and
   `generation_model` but not `tools_enabled` or `max_tool_steps`, so **two runs with tools
   toggled between them are incomparable and nothing on the card says so.** EVAL.md §5 already
   names this.

**Gaps 1 and 2 need no migration.** `trace_events.event_type` is `String(32)` with **no CHECK
constraint** and `payload` is JSONB (`db/models.py:475-496`), and `TraceRecorder.record` raises
only on an unknown *type*. Adding payload **keys** to existing types is a pure code change.

Gaps 3 and 4 need one migration, settled here in §4.4 and written once.

### 2.4 What is CLOSED, and by which property

> This section exists so the deleted work does not return in six weeks
> ([build.md §2](../build.md)). Each row names the invariant, not a preference.

**`ToolCallAccuracy` and `ToolCallF1` are unfit for this system, and the property is the
rewriter.** `REWRITE_EVERY_TURN=true` means `search_corpus(query=...)` is rewritten on **every**
turn, first turns included, and generation runs at temperature 1.0 — so the one meaningful
argument is *designed* never to be the same string twice. Both metrics compare arguments
byte-exactly. Measured through the installed package:

| Trajectory vs reference | TCA strict | TCA loose | F1 |
|---|---|---|---|
| identical args | 1.0000 | 1.0000 | 1.0000 |
| **same intent, different wording** | **0.0000** | 0.0000 | 0.0000 |
| **empty reference args** ("only that it searched") | **0.0000** | 0.0000 | 0.0000 |
| **2 calls vs 1 reference** (normal for this model) | **0.0000** | 0.0000 | 0.0000 |

Row 3 is the decisive one and it is not a tuning problem. `_tool_call_accuracy.py:66-72`:

```python
async def _get_arg_score(self, preds, refs, callbacks) -> float:
    if not refs and not preds:
        return 1.0
    if not refs:
        return 0.0          # <-- reference says "any args"; prediction has args; ZERO
```

So the "I care *that* it searched, not *what* it searched for" escape hatch returns **0.0 exactly
when it would be used.** `ToolCallF1` is worse: `_tool_call_f1.py:46-56` hashes `(name, args)`
into a `set` with **no comparator field at all**, so it has no configuration seam whatsoever.

And `ToolCallAccuracy`'s sequence gate is multiplicative and length-sensitive —
`return score * sequence_aligned`, where `sequence_aligned` requires
`pred_names == ref_names`. CLAUDE.md records this model emitting **1.50–2.00 search calls per
step**, with a browser turn measured at `tool_steps=3, tool_calls=6`. A reference authored from
one observed run therefore scores a differently-but-equally-correct run at **zero**.

That is the `refusal_pass = 0/2` failure shape exactly — an instrument reading zero while the
agent behaves perfectly — and CLAUDE.md's rule is that **a metric which cannot discriminate is
worse than no metric, because the scorecard still renders.** `scripts/agent_metrics_check.py`
cases 1–7 pin the table above so this is a measurement rather than an opinion.

**`AgentGoalAccuracyWithoutReference` is closed by the `self_judged` property.** It infers the
user's goal *and* the achieved outcome from the same conversation, so it grades against something
it wrote itself — it cannot catch an agent that satisfied a misunderstood goal, because the
misunderstanding contaminates both halves. This project spent a change set making `self_judged`
false structurally across three vendors; adding a metric that is self-referential by construction
walks that back.

**`TopicAdherenceScore` is closed by three independent properties.** It needs `reference_topics`
per agent, which nobody has authored and which for a corpus-grounded agent *is* the corpus. It
scores a multi-turn conversation, while `eval/jobs.py:351-359` creates **one archived
conversation per question**, so there is no multi-turn eval trajectory to score. And it costs
`2 + N` sequential judged calls with no `gather` (`_topic_adherence.py:167-173`).
*Worth revisiting only if eval ever runs multi-turn conversations* — its recall term is an
LLM-graded refusal measure that does not depend on the marker list, which is the list CLAUDE.md
records being wrong five times.

**Nothing here invents a score.** Per open item 23, the only *scored* metric added is one Ragas
already ships and this plan validates against known cases. The tool-use half is a **count**.

### 2.5 What the change reduces to

Persist two payload keys, reconstruct a trajectory, add one Ragas metric that survives
calibration, add a deterministic tool-use rubric, record the tool configuration a run was
measured under, and put the result behind `AdminUser`.

---

## 3. Architecture after the change

```
run_turn (ask.py)                 [unchanged transaction, two new payload keys]
   TOOL_CALL   payload += assistant_text
   TOOL_RESULT payload += content        <- the string the model actually read
   TOOL_ERROR  payload += content

eval/jobs.py::_run_one_question
   ... existing RAG scoring (SingleTurnSample) ...
   + build_trajectory(db, query_id)  ->  MultiTurnSample      [app/eval/trajectory.py]
   + score_trajectory(sample, turn)  ->  {goal_accuracy, tool_use...}
                                                              [app/eval/trajectory_metrics.py]
   -> eval_results.trajectory (JSONB)

eval/jobs.py::run_eval_job
   -> summarise_trajectory(rows) -> eval_runs.summary["trajectory"]

GET /api/admin/agent-trajectory   [AdminUser]   ->  Admin.tsx "Trajectory" tab
```

**Two scoring passes, never one dataset.** §2.1's homogeneity constraint forces it, and keeping
them separate has a second benefit: the RAG scorecard's numbers do not move, so every baseline in
[EVAL.md §10](../../EVAL.md) stays comparable.

---

## 4. Shared contracts

### 4.1 Settings

| Setting | Default | Meaning |
|---|---|---|
| `EVAL_TRAJECTORY_ENABLED` | `true` | Off → the second pass is skipped entirely and `eval_results.trajectory` stays `NULL`. **With it off, a run is byte-identical to today** — the regression assertion every feature file inherits |
| `TRAJECTORY_GOAL_ACCURACY` | `true` | Off → the deterministic tool-use rubric still computes; only the judged metric is skipped. Exists so a run can cost zero extra judge tokens |
| `TRAJECTORY_MAX_TOOL_CONTENT_CHARS` | `2000` | Per-`ToolMessage` truncation before it reaches the payload **and** the judge |

`TRAJECTORY_MAX_TOOL_CONTENT_CHARS` is load-bearing twice, not tidy: it bounds JSONB growth on a
table that already gets one row per event, **and** it bounds the judge prompt, because
`MultiTurnSample.pretty_repr()` renders every `ToolOutput` in full into the judge's context.

### 4.2 Trace payload keys — additive, no new event types

**No new `EVENT_TYPES` member, therefore no `TracePanel` map entry and no migration.** The three
tool event types gain keys:

| Event | New key | Value |
|---|---|---|
| `TOOL_CALL` | `assistant_text` | The text the assistant emitted alongside the call, after `_message_text` stripping. `""` when it emitted none |
| `TOOL_RESULT` | `content` | `ToolMessage.content`, truncated to `TRAJECTORY_MAX_TOOL_CONTENT_CHARS` |
| `TOOL_ERROR` | `content` | Same. For a failed call this is the traceback the model read |

Carried up from the loop on `ToolInvocation` (`agent_loop.py:379-405`), which gains exactly two
fields — `content: str = ""` and `assistant_text: str = ""`. **`_execute` stamps `content`**, one
place, covering the success path, the sandbox-failure path and the `failed()` path alike.

This is [loop.md](../loop.md) S3 unchanged: accumulate as data in the loop, record at the
boundary in `run_turn`, inside the one existing transaction.

**Older turns have neither key.** Every reader uses `.get(...)` with a default, the same
`.get`-as-migration `conversations.py:477-491` already uses for `tool_steps`.

### 4.3 The trajectory contract

`app/eval/trajectory.py` exposes:

```python
async def build_trajectory(db, query_id: uuid.UUID) -> MultiTurnSample | None
def trajectory_from_rows(question: str, answer: str | None,
                         events: list[TraceEvent]) -> MultiTurnSample | None
```

The pure function is the one under test; the async wrapper only fetches. It returns `None` — never
raises, never fabricates — when the turn has no trace rows.

**Ordering rule, and it is the whole correctness of the file:** events are read
`ORDER BY step_index`, and a `TOOL_CALL` is paired to its `TOOL_RESULT`/`TOOL_ERROR` by
`call_id`, never by adjacency. A call whose result row is missing gets a synthesised
`ToolMessage(content="")` so ragas' `field_validator` still passes — a dropped result must not
silently shorten the trajectory.

### 4.4 Schema and migration — ONE revision for the whole change set

`down_revision = "<head at branch time>"`. Settled here so two features cannot race for the same
parent ([build.md §3](../build.md)).

```sql
ALTER TABLE golden_questions ADD COLUMN expected_tool_use VARCHAR(16);
ALTER TABLE eval_results     ADD COLUMN trajectory JSONB;
ALTER TABLE eval_runs        ADD COLUMN tools_enabled BOOLEAN;
ALTER TABLE eval_runs        ADD COLUMN max_tool_steps INTEGER;
```

**`expected_tool_use` is an enum-ish string, deliberately NOT a reference tool-call list.** §2.4
is the reason: a byte-exact reference sequence cannot be scored. The vocabulary is the set of
propositions the golden set can actually assert about a turn:

| Value | Asserts |
|---|---|
| `search` | at least one `search_corpus` call happened |
| `none` | no tool call happened — the S2 "no reflex tool use" proposition |
| `python` | at least one `run_python` call happened |
| `NULL` | no expectation authored; the row is counted but not graded |

**`NULL` is the default and it is not laziness** — it keeps every existing golden question valid
without a backfill that would be inventing expectations nobody stated.

**`eval_runs.tools_enabled` / `max_tool_steps` are nullable on purpose.** `NULL` means "this run
predates the column", which is a different fact from `false`, and the console must render it as
such — the "not measured is not zero" rule that
[14-admin-observability](../14-admin-observability/) records shipping wrong twice.

### 4.5 API surface

```
GET /api/admin/agent-trajectory?days=30      ->  AdminUser
```

Four constraints, each from `scripts/admin_check.py`'s existing cases:

1. It takes `admin: AdminUser`, so **case 1 picks it up automatically** by router introspection.
2. **It must not contain `{conversation_id}` in its path** — case 1c asserts exactly one route
   does, and it is the transcript.
3. **It must not call `_audit(`** — case 1e asserts no non-transcript route audits. It reads
   aggregates, and the module docstring's rule is *reading a transcript writes an `audit_log`
   row; aggregates do not.*
4. It declares a pydantic `response_model` carrying `Measured`, so case 5's denominator rule
   applies. **Every aggregate reports its own denominator.**

`days` is validated `Annotated[int, Query(ge=1, le=365)] = 30`, copying `/spend`.

### 4.6 Frontend contract

One new tab in `frontend/src/views/Admin.tsx` — one entry in the `Tab` union, one in `TABS`, one
line in the render chain, one component. It follows **admin** conventions (`rounded-lg`,
`bg-slate-900`, `tabular-nums`, `min-h-11`) and borrows the *structure* of
`components/Scorecard.tsx`, not its classes.

**`Admin.tsx` currently contains zero `data-testid` attributes.** The new panel adds them from the
start, matching `Scorecard.tsx`'s naming: `trajectory-panel`, `trajectory-metric-card`,
`trajectory-row`, `trajectory-unmeasured`.

One new entry in the `admin` object in `frontend/src/lib/api.ts`, one new type block in
`lib/types.ts` beside the existing admin types.

---

## 5. Build sequence — lowest layer first

| # | Feature | Layer | File |
|---|---|---|---|
| 01 | Calibration harness — **written and watched failing before anything else** | 1 | [01-metric-calibration.md](01-metric-calibration.md) |
| 02 | Trajectory persistence + builder | app | [02-trajectory-capture.md](02-trajectory-capture.md) |
| 03 | The rubric: goal accuracy + deterministic tool use | app | [03-trajectory-rubric.md](03-trajectory-rubric.md) |
| 04 | Migration + eval-run wiring | db | [04-eval-run-wiring.md](04-eval-run-wiring.md) |
| 05 | Admin surface | api + ui | [05-admin-trajectory.md](05-admin-trajectory.md) |

01 comes first because **it decides what 03 is allowed to ship.** That inversion is the point:
the calibration is not a test of the feature, it is the input to the feature's design.

---

## 6. Risk register

| Risk | The tell | Mitigation |
|---|---|---|
| **A judged metric that cannot discriminate** — the `0.000`-on-a-verbatim-answer failure, arriving in a new instrument | Every row scores 1, or every row scores 0 | `agent_metrics_check.py` cases 20–23: a known-good and a **known-bad** trajectory, same judge, asserting the verdicts differ. A metric that cannot separate them does not ship |
| **Binary output read as a mean** | `goal_accuracy: 0.60` rendered as if it were faithfulness | It is a *pass rate over n*, and the card says so. `Measured` carries the denominator |
| **The trajectory shown to the judge is not what the model saw** | Judge verdicts that make no sense against the transcript | Persist the real `ToolMessage.content` (§4.2). The truncation constant is the only divergence and it is named on the card |
| **Trace payload growth** | `trace_events` table size | `TRAJECTORY_MAX_TOOL_CONTENT_CHARS = 2000`, applied at the loop boundary, before the payload |
| **The new route ships as a 500** — the eighth green-suite failure, a query that compiles and does not run | Nothing; the offline harness is green and the browser reports **CORS** | The route goes into **both** hardcoded lists in `admin_check.py --live`, and §7 runs `--live` |
| **A run's tool config is not recorded, so runs stay incomparable** | Two runs differ and nothing explains why | `eval_runs.tools_enabled` / `max_tool_steps`, rendered on the card |
| **`ragas.messages` confused with `langchain_core.messages`** | `AttributeError` on `.tool_calls`, or a silent empty trajectory | `trajectory.py` imports ragas' types under an explicit alias and case 10 asserts the module never imports the langchain ones |
| **The judge parses ragas' prompt output badly on this route** | `RagasOutputParserException`, plus an extra `fix_output_format` call per retry | The live case in `agent_metrics_check.py --live` makes **two** real calls and asserts a verdict came back. Not reachable offline |

---

## 7. Definition of done

```bash
backend/.venv/Scripts/python.exe scripts/agent_metrics_check.py          # new, layer 1
backend/.venv/Scripts/python.exe scripts/agent_metrics_check.py --live   # two real judge calls
backend/.venv/Scripts/python.exe scripts/sandbox_check.py
backend/.venv/Scripts/python.exe scripts/ledger_check.py
backend/.venv/Scripts/python.exe scripts/refusal_check.py
backend/.venv/Scripts/python.exe scripts/route_specialist_check.py
backend/.venv/Scripts/python.exe scripts/llm_check.py
backend/.venv/Scripts/python.exe scripts/agent_loop_check.py
backend/.venv/Scripts/python.exe scripts/admin_check.py
backend/.venv/Scripts/python.exe scripts/admin_check.py --live
cd frontend && npm test && npm run build
backend/.venv/Scripts/python.exe scripts/agentic_check.py --setup   # then --run, then --cleanup
```

Then the two steps that are not commands:

- **Read one real trajectory verdict by eye.** A green suite here has been wrong eight times.
- **Mutate.** Delete the three lines whose loss would be worst — the `call_id` pairing, the
  `expected_tool_use is None` guard, and the `content` truncation — one at a time, and confirm
  each goes red. A line that survives its own deletion is undefended.

---

## 7a. As built — where the plan was wrong

> The only section written with hindsight, and the reason the next plan is better rather
> than merely longer ([build.md §3](../build.md)).

**1. The audit's single most decisive claim was wrong, and it came from a subagent that had
"verified by execution".** The package audit reported that a reference of
`ToolCall(name=..., args={})` scores **1.0** — the escape hatch that would have made
`ToolCallAccuracy` usable, and the difference between shipping it and closing it. It scores
**0.0**. Reading `_get_arg_score` settled it in thirty seconds: `if not refs: return 0.0`.
The report was honest — it had almost certainly exercised the `not refs and not preds` branch,
which does return 1.0 and never occurs in reality, because a real call always carries a query.

> **A delegated finding is evidence, not a conclusion — and the one to re-derive yourself is
> the one the decision turns on.** The cost of checking was one file read. The cost of not
> checking was a metric column that would have read zero forever.

**2. Two acceptance criteria were written as checks that could not do their job, and both
failed in a way the plan could not have predicted.**

- **A1–A7's case 40** grepped *every* migration for four column names and reported two already
  satisfied — because `tools_enabled` and `max_tool_steps` exist on **`agents`** from an
  earlier revision. It would have passed with this change set's migration absent. Now it names
  the revision file *and* the target table.
- **A8's case 10** asserted that `trajectory.py` never imports `langchain_core.messages`, as a
  substring search, and went **red against a correct file**: the module's docstring *explains
  the collision*, and the check matched its own explanation. Parsed with `ast` now — an
  `ImportFrom` node cannot be prose. This is `deck_check.py` case 14's rule arriving a second
  time, which is what makes it a rule rather than an anecdote.

**3. The risk register missed the defect that mattered, and a live scenario went green over
it.** `eval_runs.summary` is JSONB, and `RunSummary`'s docstring already said *"this model IS
the schema… do not write the column from a hand-built dict."* The job did exactly that —
`{**run.summary, "trajectory": …}` — and pydantic's default `extra="ignore"` meant the key was
**stored and then silently dropped when the API read it back**. No error at either end.

**S35 passed over it**, because a live scenario reads the *column*. It was found by reading
the model definition, and the case that now guards it (`agent_metrics_check.py` 38) is shaped
as a **round trip** rather than as "is the key in the dict" — the only shape that can see it.
Confirmed by mutation: deleting the declared field turns 38 red.

> One more entry for the "a green suite was wrong" table, and its own variety: **a write and a
> read that disagree about a schema, where the storage layer enforces nothing and both sides
> succeed.**

**4. `_message_text` was called once per tool CALL rather than once per step**, so one leaked
markup event produced N identical `log.warning` lines. Found by reading the suite's log —
never by an assertion, because nothing was wrong with the value. Hoisted above the dispatch
loop.

**5. Smaller things.** Inserting the new scenarios anchored on `SCENARIOS = [`, which matched
`HTTP_SCENARIOS = [` first and split it. And the first frontend assertion located `"7 / 9"`
with a bare `getByText`, which matched the metric card *and* the agent row — an ambiguity that
would have resolved itself the moment a second agent existed, i.e. in production.

**What the plan got right.** No new `EVENT_TYPES` member was needed, so no `TracePanel` map
entry and no migration for the trace half (§4.2). Cases 1–7 passed on the first run exactly as
predicted, because they pin an installed package's current behaviour. And the calibration
gate did its job in the direction that mattered: it *cleared* the judged metric (1.0 / 0.0 /
differ) and *closed* the two tool metrics, which is the audit deleting work rather than the
plan adding it.

**Verification numbers.**

| | |
|---|---|
| `agent_metrics_check.py` | 24 offline + 4 live, all green |
| `agent_loop_check.py` | 10 existing + **4 new**, all green |
| `admin_check.py` | offline + `--live`, all green, including the new route at 200 and 403 |
| `npm test` | **62** (was 56) — the admin console's first six tests |
| Migration | `a3c81f5d2e07`, four nullable columns, applied and verified |
| Mutation | 4 lines deleted one at a time; **4/4 went red** |
| Measured on a real eval turn | `tool_use_ok=True goal_accuracy=1.0 calls_per_step=2.00 tools_enabled=True` |

---

## 8. What this deliberately does not do

- **Does not score tool calls with `ToolCallAccuracy` or `ToolCallF1`.** §2.4, with the
  measurement.
- **Does not add `rapidfuzz`.** It is the only way to get `NonLLMStringSimilarity` as an argument
  comparator, it is not installed, and it would not rescue either metric from the sequence gate.
  A new dependency also re-triggers the `pywin32` marker flattening, which has fired three times.
- **Does not add a reference tool-call sequence to `golden_questions`.** There is no metric that
  could read it honestly.
- **Does not make eval multi-turn.** That is what `TopicAdherenceScore` would need, and it is a
  change to what an eval run *is*.
- **Does not surface the rubric in the owner-facing Evaluate tab.** Admin only, this pass.
- **Does not touch the four existing RAG metrics or their means.** Every EVAL.md §10 baseline
  stays comparable, and `EVAL_TRAJECTORY_ENABLED=false` reproduces today byte-for-byte.
