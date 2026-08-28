# 19 — Evaluating the agent architecture, and repairing the instrument first

**Runtime:** LangChain (`backend/app/rag/agent_loop.py`). Unchanged, and staying.
**Instrument:** Ragas 0.4.3 agent metrics + the counted rubric in `app/eval/trajectory_metrics.py`.
**Branch:** `feat/adk-runtime` (this change set is additive to it and touches no ADK file).

---

## 1. The controlling fact

**The agent-evaluation machinery had never produced a row.**

| Query | Result |
|---|---|
| `eval_results.trajectory IS NOT NULL` | **0** of 50 |
| `eval_runs.summary ? 'trajectory'` | **0** of 5 |
| `golden_questions.expected_tool_use IS NOT NULL` | **0** of 30 |
| eval turns that produced a `TOOL_CALL` | **0** of 50 |

Every claim about the trajectory rubric rested on offline fixtures and one live
scenario that authors its own question. So the first question was never *"did a
change help"* — there was no baseline to move. It was **"would the instrument tell
the truth if we switched it on?"**

The answer was no, in four places.

---

## 2. Where the eval data actually is, and why no agent could support one

Eight agents. The three properties an agent evaluation needs — tools on, a corpus
big enough that retrieval can fail, and an authored golden set — were spread
across three different agents so that none had more than two.

| Agent | chunks | tools | active golden | verdict |
|---|---|---|---|---|
| `Kestrel Feynman` | **1** | on | 10 | the actively misleading choice |
| `Topic 1` | 37 | **off** | 10 | the corpus and the questions |
| `prompt engineering` | 37 | on | **0** | the tools |

**`Kestrel Feynman` is the trap.** It is the only agent with tools ON *and* a
golden set, so it is what anyone would reach for — and with one chunk against
`rerank_top_n=3`, a single retrieval returns 100% of the corpus. No question can
require a second search, the agent loop cannot be exercised, and the scorecard
cannot go down. It is `agentic_check` S3 institutionalised at the evaluation
layer: *"retrieval returned the whole corpus; no question could need a second
search"*. Its faithfulness numbers (0.56 → 0.63 → 0.77) are the ones in EVAL.md.

**What made this solvable without authoring anything:** `Topic 1` and
`prompt engineering` hold the **same 37 chunks, byte for byte**, from the same
PDF. That is a natural experiment — same corpus, one arm with tools off and one
with tools on — and it needs no new data and no writes.

---

## 3. The four numbers the rubric would have rendered WRONG

Each was written as a failing case in `scripts/agent_metrics_check.py` first.
10 of 13 went red; the 3 that passed were both control pairs plus the one
behaviour that was already correct.

| # | The card would have said | Why it was false |
|---|---|---|
| **54** | *"Tool use OK — it searched."* | `tool_use_verdict` filtered on `TOOL_CALL` alone, and `ask.run_turn` records a `TOOL_CALL` for a call that **failed**, deliberately. A turn where every search raised reported `searched=True, tool_use_ok=True`. The loop's own gate requires `invocation.ok` — the rubric was the looser of the two. Unfired in production only because there are zero `TOOL_ERROR` rows there. |
| **55** | *"The agent used a tool when it should not have."* | `expected_tool_use="none"` graded over a list including **gap-forced** calls. The trigger re-invokes with a NAMED tool: the code compelled that call. Same category error as `refusal_pass` blaming the agent for a marker list. |
| **57** | *"Goal accuracy 9/10."* | Refusal and answerable turns pooled into one rate, undoing the twenty lines `summarise()` spends separating them. A refusal scores goal accuracy **1.0 in 9 of 9** measured attempts — including one that never searched — so 20% of the denominator is pinned at 1.0 and damps any real movement. |
| **59** | *"It searched, found nothing, and declined."* | **Measured 3/3 each way: a refusal that searched and a refusal that never searched both score 1.0.** Ragas discards the inferred goal and compares only `end_state` to the reference, and every stored refusal reference is a CONTENT statement (*"The passages do not provide information regarding…"*), never a process one. The proposition `trajectory_metrics.py` advertised as goal accuracy's "sharpest use" is **inexpressible as this system authors its references.** |

**59 is the one worth carrying past this repo.** The claim was not merely
unproven; it was disproven by the measurement it was written to justify. The
repair is to withdraw it and replace judgement with arithmetic: `self_initiated`
and `searches` now sit beside the verdict and need no judge at all.

### What was missing rather than wrong

`corpus.py` has been computing `new_chunks` on every search all along, and
nothing ever read it — **8 of 22 real searches in production returned zero new
chunks**, i.e. the model paid an embedding, a Pinecone query and a rerank for text
it already had. That is the clearest efficiency signal available and it was
already durable. Now `redundant_searches` / `searches` / `wasted_search_rate`.

And `self_initiated`: CLAUDE.md's central architectural finding is that Gemma
self-initiates a search **0/6** and DeepSeek **6/6** — a model swap inverts this
whole design — and nothing recorded which side of that inversion a run was
measured on.

---

## 4. Two loop defects, fixed harness-first

Neither changes an answer, so no EVAL.md baseline moves.

- **`agent_loop_check` 14** — a reply carrying **only** `invalid_tool_calls` fell
  through the early exit, assigned `steps = step`, and ran a loop body that
  executed nothing. Measured `steps=2` on a turn where no tool ran. That is the
  exact invariant cases 1 and 2 defend, breached through a different door — and it
  corrupted the `calls_per_step` denominator the rubric now reports.
- **`agent_loop_check` 15** — the gap branch answered only `forced.tool_calls`.
  `_invalid_call_message`'s own docstring states the consequence: langchain-openai
  serialises **both** lists back into the next request, so an unanswered invalid
  call leaves the conversation malformed and the next `ainvoke` raises — a 500
  rather than a degraded answer. Measured `answered=['good-1']`, `bad-1` dropped.

---

## 5. The evaluation design

`scripts/agent_eval_check.py`. **Writes nothing** — it answers through
`pipeline.answer_question` and builds the trajectory in memory from
`AnswerResult.tool_calls` in exactly the shape `ask.run_turn` would persist, so a
comparison run cannot pollute EVAL.md's history or look like a regression on an
operator's card.

| | control | treatment |
|---|---|---|
| agent | `Topic 1` | `prompt engineering` |
| corpus | the 37 chunks | **byte-identical** 37 chunks |
| `tools_enabled` | **off** | **on** |

**Confounders equalised in memory and never committed**, and the list grew once
during the build, which is the part worth recording. `retrieve_k` (20 vs 24),
`rerank_top_n` (3 vs 5) and `chunk_size` were obvious. **`system_prompt` was
not** — the treatment agent carries a `Teaching orchestrator` persona with 876
characters of adaptive-instruction pedagogy against the control's 670 and no
persona at all. CLAUDE.md measures persona verbosity as the single biggest lever
on latency (a persona turn ran 4.5× a bare one) and records faithfulness
structurally punishing a teaching persona. Left uncontrolled, every cost, latency
and goal-accuracy delta would have been **partly the persona and reported as the
agent loop.**

The harness now asserts the equalisation over the live objects after assignment,
so a field added to `agents` later that the list forgets fails loudly rather than
becoming a silently uncontrolled variable.

### Reading the result honestly

- **The counted signals are the decision metrics.** `searches`,
  `redundant_searches`, `self_initiated`, `calls_per_step`, `budget_exhausted` are
  arithmetic over rows — no model reads them, so their only variance is generation
  variance.
- **`goal_accuracy` is secondary and BINARY.** `--noise` re-scores one fixed
  trajectory k times and prints the flip rate; any smaller difference is refused
  as a result. That is `agent_metrics_check` case 22's missing sibling — case 22
  asserts a good and a bad trajectory **differ**; this asserts the **same one does
  not**.
- **`tool_use_ok` reads "not measured" and that is correct.** `expected_tool_use`
  is NULL on all 30 golden questions. Authoring it is a separate change; inventing
  an expectation here would be exactly the "new instrument of unknown validity"
  PRD open item 23 warns against.

---

## 6. Acceptance criteria

| # | Criterion | File | Case |
|---|---|---|---|
| 1 | A failed search does not report `searched=True` | `scripts/agent_metrics_check.py` | 54a ★ |
| 2 | A successful one still does (the pair) | `scripts/agent_metrics_check.py` | 54b |
| 3 | A gap-forced call does not fail `expected_tool_use='none'` | `scripts/agent_metrics_check.py` | 55a ★ |
| 4 | A model-chosen call still does (the pair) | `scripts/agent_metrics_check.py` | 55b |
| 5 | `total` counts rows that scored nothing | `scripts/agent_metrics_check.py` | 56 |
| 6 | Goal accuracy splits by behaviour class, separate denominators | `scripts/agent_metrics_check.py` | 57a/57b ★ |
| 7 | `redundant_searches` / `searches` / `self_initiated` counted | `scripts/agent_metrics_check.py` | 58a–58d ★ |
| 8 | The disproven refusal claim is withdrawn in source | `scripts/agent_metrics_check.py` | 59 ★ |
| 9 | Invalid-only reply reports `steps == 0` | `scripts/agent_loop_check.py` | 14 ★ |
| 10 | A forced valid+invalid pair does not raise, and both are answered | `scripts/agent_loop_check.py` | 15 ★ |
| 11 | The two arms differ only in `tools_enabled` | `scripts/agent_eval_check.py` | equalisation assertion ★ |
| 12 | The control performs zero searches; the treatment exercises the loop | `scripts/agent_eval_check.py` | arm assertions |
| 13 | The goal-accuracy delta is reported against a measured noise floor | `scripts/agent_eval_check.py` | noise assertion ★ |

★ watched failing before the code existed.

---

## 7. Measured — 2026-08-28, controlled, `--n 10 --noise 5`

`Topic 1` (tools off) against `prompt engineering` (tools on), same 37 chunks,
same 10 questions, judge `google/gemini-3.7-flash`, generation
`deepseek/deepseek-v4-flash-0731`. Five confounders equalised in memory.
**All 11 harness assertions passed.**

| | control (tools off) | treatment (tools on) |
|---|---|---|
| goal accuracy — **answerable** | 1.000 (8/8) | 1.000 (8/8) |
| goal accuracy — **refusal** | **0.500 (1/2)** | **1.000 (2/2)** |
| searches | 0 | 8 |
| redundant searches | 0 | 1 (**12.5%** of 8) |
| self-initiated turns | 0 | **4** |
| gap-forced turns | 0 | **1** |
| calls per step | n/a | 1.400 (mean over 5) |
| budget exhausted / tool errors | 0 / 0 | 0 / 0 |
| generation calls | 10 | **17** |
| wall clock | 94.3 s | **161.0 s (+71%)** |
| cost | $0.00114668 | **$0.00485384 (+323%, 4.2x)** |
| **judge noise floor** | — | **0.00** (5 of 5 unanimous on one fixed trajectory) |

### What this does and does not establish

**It does establish that the agent loop works as designed.** 4 of 10 turns
self-initiated a search, the gap trigger fired once, no turn exhausted its budget,
no tool errored, and 7 of 8 searches returned new material. Nothing is broken.

**The only metric that moved is the refusal pair: 1/2 -> 2/2.** That is exactly the
behaviour the architecture exists to buy — CLAUDE.md: *"I searched and it is not
there" beats "it was not in the chunk I happened to be given"*. The gap trigger
firing once on a two-question refusal set is the mechanism visibly doing its job.

**And it is n=2. One question. It cannot support a claim**, and this document will
not make one from it.

**On the 8 answerable questions the instrument is SATURATED: 8/8 in both arms.**
Goal accuracy cannot see a difference here, and that is a property of the question
set, not a finding about the agent. Every one of these questions is a single-fact
lookup the unconditional first retrieval already answers — the corpus is 37 chunks
of a prompt-engineering playbook and the questions ask what CO-STAR stands for.
**No question in this golden set REQUIRES a second search**, so the loop cannot
demonstrate its value and can only demonstrate its cost.

**The cost is real and it is large: 4.2x the money and 1.7x the latency**, for
8 searches and 7 extra generation calls.

### Two numbers on that card that must be discounted

- **`tool_use_ok` is an artefact of this run and is not a measurement.** It was
  produced by a version that hardcoded `expected_tool_use="search"`, inventing an
  expectation the data does not carry — and inventing it *wrongly*, since these
  questions do not need a search, so it grades the agent down for correctly not
  searching. Precisely the "new instrument of unknown validity" PRD open item 23
  warns against, built by accident inside the harness meant to avoid it. Now read
  from the column, which is NULL on all 30 rows, so it correctly reports NOT
  MEASURED.
- **`calls_per_step` rendered as `1.400 (7/5 achieved, of 10)`** — seven of five.
  The card formatter rendered every metric as a pass rate, which is right for a
  binary metric and nonsense for a mean. Found by reading the card, which is the
  one step in this repo's ladder that is not a command.

### The conclusion, stated as narrowly as the evidence allows

**The evaluation does not justify a behavioural change to the agent loop**, and
inventing one would repeat the mistake EVAL.md already documents — acting on a
weakest-metric pointer that advised deleting the pedagogy. What it justifies is:

1. The correctness fixes shipped in this change set (four instrument repairs, two
   loop defects), none of which alters an answer.
2. A recorded fact: **this golden set cannot discriminate an agentic architecture
   from a non-agentic one.** Goal accuracy is saturated at 8/8 on the answerable
   rows and n=2 on the refusals.
3. The next step, which is DATA and not code: **two-topic questions** of the form
   *"X, and separately, Y"* over semantically distant sections, where one topic is
   reachable from the first retrieval and the other only by a second search. That
   is the only question class on which the loop can be shown to earn its 4.2x, and
   `agentic_check.py` already documents the construction as the reliable forcing
   function. Until such questions exist, any claim that the loop helps or does not
   help on this corpus is unfalsifiable.

---

## 8. What NOT to do

- **Do not evaluate on `Kestrel Feynman`.** One chunk. The scorecard cannot go
  down, and it is the agent anyone would pick.
- **Do not adopt `ToolCallAccuracy` or `ToolCallF1`.** Re-verified against 0.4.3's
  installed source. One correction to this repo's own prose: the byte-exact
  argument objection is **fixable** — injecting a perfect comparator into the
  legacy `arg_comparison_metric` seam raises the reworded-query case from 0.0 to
  **1.0**. What survives any comparator is the empty-reference short-circuit
  (`if not refs: return 0.0`) and the **multiplicative sequence gate**, which
  returns 0.0 for 2-calls-vs-1-reference. At 1.50–2.00 calls per step that gate
  zeroes essentially every real turn. **Lead with the sequence gate, not the
  rewrite.**
- **Do not migrate to `ragas.metrics.collections` yet** — but correct the note
  that says it is unreachable. It is reachable:
  `llm_factory(..., provider="openai", client=AsyncOpenAI(base_url=openrouter))`
  builds an `InstructorLLM` that the collections metrics accept. The blocker was
  always `LangchainLLMWrapper`, never the gateway. Migrating now would cost the
  markdown-fence stripping that wrapper provides, and would make the tool metrics
  **worse** — `collections.ToolCallAccuracy` has no comparator seam at all.
- **Do not read a goal-accuracy delta off two runs.** Binary metric,
  non-deterministic judge, and every run re-asks at temperature 1.0. This is the
  mistake already made once on faithfulness (run 2 → run 3, `0.628 → 0.769`).
- **Do not grade `calls_per_step` against an invented threshold.**
  `score_threshold` is the standing precedent for what happens when a number is
  graded against a band that overlaps.
