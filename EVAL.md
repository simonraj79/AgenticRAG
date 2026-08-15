# EVAL.md — Stage 3 evaluation

How Groundwork measures whether an agent is actually working, what every knob does, and
how to read a scorecard without being misled by it.

[PRD.md](PRD.md) §3.6 is the specification. [CLAUDE.md](CLAUDE.md) holds the debugging
gotchas. **This file is the operator's guide**: the tables you want open while running an
evaluation.

---

## 1. What an eval run does

For each **active golden question**, in order:

```
ask it through the REAL pipeline  ──►  writes conversations, queries,
(retrieve → rerank → generate)         query_chunks, trace_events
            │
            ▼
read back the FINAL contexts       ──►  from query_chunks, post-rerank,
                                        not the pre-rerank candidates
            │
            ▼
score with Ragas (4 metrics)       ──►  one eval_results row
            │
            ▼
summarise                          ──►  eval_runs.summary, weakest metric + advice
```

An eval answer is a real turn, so it is traceable in the same Trace view as a human's.
Each question costs one `conversations` row (archived), one `queries` row, its
`query_chunks`, its `trace_events`, and one `eval_results` row.

**Refusal questions are asked but not scored** — see §6.

---

## 2. Running one

| Action | Route | Notes |
|---|---|---|
| List golden questions | `GET /api/agents/{agent_id}/golden-questions` | |
| Add one by hand | `POST /api/agents/{agent_id}/golden-questions` | 201 |
| Edit one | `PATCH /api/golden-questions/{question_id}` | flips `source` → `edited` |
| Delete one | `DELETE /api/golden-questions/{question_id}` | |
| **AI-suggest a set** | `POST /api/agents/{agent_id}/golden-questions/suggest` | **202**, background job |
| Export | `GET /api/agents/{agent_id}/golden-questions/export` | JSON |
| Import | `POST /api/agents/{agent_id}/golden-questions/import` | wrapped object or bare list |
| **Start a run** | `POST /api/agents/{agent_id}/eval-runs` | **202**, background job |
| Run history | `GET /api/agents/{agent_id}/eval-runs` | |
| One scorecard | `GET /api/eval-runs/{run_id}` | |
| Delete a run | `DELETE /api/eval-runs/{run_id}` | |

In the UI: the **Evaluate** view (`frontend/src/views/AgentEvaluate.tsx`).

Both 202 routes stage a row and return immediately; the client polls
`progress_done` / `progress_total`. A run that dies mid-way keeps the questions it already
paid for — progress is committed per question, not once at the end.

---

## 3. The four metrics

| Metric | Reads | Needs `reference`? | Low score means | Change this |
|---|---|---|---|---|
| **faithfulness** | answer + contexts | no | Answer contains claims the contexts do not support | System prompt grounding clause, persona verbosity, generation model |
| **answer_relevance** | question + answer + **embeddings** | no | Grounded, but not answering what was asked | Prompt shape; preamble burying the answer |
| **context_precision** | contexts + reference | **yes** | Junk ranked into the top-n | `rerank_top_n`, `retrieve_k`, chunking |
| **context_recall** | contexts + reference | **yes** | Retrieval missed text the answer needed | `retrieve_k`, `chunk_size`, `chunk_overlap`, check the embedding model matches the index |

Two things about this table are load-bearing:

- **`answer_relevance` is the only one that can go NEGATIVE.** It is a cosine similarity —
  the judge writes questions the answer would suit and compares them to the real one — so
  an answer about something else scores below zero rather than at it. It is not clamped,
  because a clamp would turn "actively off-topic" into "merely unrelated".
- **Two metrics silently do not run without a reference answer.** They are omitted rather
  than scored against an empty string, and come back `None` meaning *not measured*. This is
  why `golden_questions.reference_answer` is not decorative.

---

## 4. Settings

All are environment variables. Defaults live in [`backend/app/config.py`](backend/app/config.py);
the committed template is [`.env.example`](.env.example).

### 4.1 Credentials — both providers are required

| Variable | Used for | Required |
|---|---|---|
| `OPENROUTER_API_KEY` | **Every chat model** — generation, decision, golden-set drafting, judge | ✅ |
| `GEMINI_API_KEY` | **Embeddings only** (`gemini-embedding-2`) | ✅ |
| `PINECONE_API_KEY` | Vector store | ✅ |
| `COHERE_API_KEY` | Reranking | ✅ |

> Chat moved to OpenRouter; embeddings deliberately did not. Every vector in Pinecone was
> written in `gemini-embedding-2`'s space, matching dimensions do not imply a shared space,
> and OpenRouter serves no embedding model. **Ragas therefore draws its judge LLM and its
> embedding model from two different providers**, and a deployment missing `GEMINI_API_KEY`
> fails at *retrieval*, which looks nothing like a missing model key.

### 4.2 Models

| Variable | Default | What it does |
|---|---|---|
| `GENERATION_MODEL` | `google/gemma-4-31b-it` | Writes the answers being graded |
| `DECISION_MODEL` | `google/gemma-4-31b-it` | Per-turn question rewrite / contextualisation |
| `GOLDEN_SET_MODEL` | `google/gemini-3.7-flash` | **Drafts** the golden set (§7) |
| `RAGAS_JUDGE_MODEL` | `google/gemini-3.7-flash` | **Grades** the answers |
| `RAGAS_JUDGE_REASONING_EFFORT` | `low` | `high` / `medium` / `low`. Thinking is **mandatory** on Flash — it cannot be switched off |
| `EMBEDDING_MODEL` | `models/gemini-embedding-2` | Google direct. **Changing it invalidates the index** |
| `EMBEDDING_DIMENSION` | `768` | Fixed at index creation |
| `RERANK_MODEL` | `rerank-v3.5` | Cohere |

Model ids are **`author/model`**. A bare `gemma-4-31b-it` returns a 404 naming a model that
plainly exists; `app/rag/llm.py` maps the known legacy ids and warns rather than guessing.

### 4.3 Sampling — one configuration, not four knobs

| Variable | Default | Notes |
|---|---|---|
| `GENERATION_TEMPERATURE` | `1.0` | ┐ |
| `GENERATION_TOP_P` | `0.95` | ├ **One configuration, not three knobs** — see below |
| `GENERATION_TOP_K` | `64` | ┘ Dropped automatically for the Gemini family, which has no `top_k` |
| `GENERATION_MAX_TOKENS` | `2048` | Sent as `max_tokens` via `extra_body`, never as `ChatOpenAI(max_tokens=…)` — see §9 |

Gemma 4's model card gives those first three as a single *"standardized sampling
configuration across all use cases"*. The reflex for grounded RAG is temperature 0; Gemma is
not calibrated for it and degenerates into repetition when squeezed far below its tuned
values. Change them against a measurement, not by habit — and change them **together**,
because sending two-thirds of the configuration runs the model outside its calibration while
looking correctly configured in the code.

The judge overrides temperature to **0.1** and sends no `top_k`. A scorecard that returns
different numbers for the same answers on consecutive runs cannot tell you whether a prompt
change helped, which is the whole point of Stage 3.

### 4.4 OpenRouter routing

| Variable | Default | What it does |
|---|---|---|
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | |
| `OPENROUTER_REQUIRE_PARAMETERS` | `true` | **Leave it on.** Routes only to providers advertising every parameter sent |
| `OPENROUTER_TIMEOUT_S` | `120.0` | Bounds one HTTP call. Distinct from `METRIC_TIMEOUT_S` |
| `OPENROUTER_APP_URL` / `_TITLE` | repo URL / `Groundwork` | Attribution headers only, no user data |

### 4.5 Eval execution

| Variable | Default | What it does |
|---|---|---|
| `RAGAS_MAX_CONCURRENCY` | `2` | Judge calls in flight **within one question**. Conservative on purpose: being greedy produces a scorecard full of nulls that looks like a broken judge |

---

## 5. Per-agent parameters

These live on the `agents` row (and are seeded from `agent_templates`), not in the
environment — they are editable per agent in the UI, which is what makes the weakest-metric
pointer actionable.

| Column | Default | Metric it moves |
|---|---|---|
| `chunk_size` | `800` | context_recall |
| `chunk_overlap` | `120` | context_recall |
| `retrieve_k` | `20` | context_recall, context_precision |
| `rerank_enabled` | `true` | context_precision |
| `rerank_top_n` | `3` | context_precision |
| `score_threshold` | `0.5` | **advisory only.** Governs neither refusing nor, since the agent loop, rewriting. See §6 |
| `system_prompt` | per template | faithfulness, answer_relevance |
| `generation_model` | `NULL` → service default | faithfulness |
| `tools_enabled` | `true` (new agents) / `false` (pre-existing) | **all four, and it is not a tuning knob** |
| `max_tool_steps` | `3` | context_recall, latency |

**`tools_enabled` is the one row in this table that changes what is being measured, not how
well it does it.** With it on, the agent can search its corpus again mid-turn and can write
and run Python, so the answer, the trace and the latency all move. Two consequences for
anyone comparing scorecards:

- **The migration backfilled every agent that existed before tools shipped to `false`**,
  precisely so that the runs recorded in §10 stay reproducible. New agents default to `true`.
  If a run's numbers look unlike its predecessors, check this column before checking the
  judge.
- **`eval_runs` does not record it.** `judge_model` and `generation_model` are captured per
  run; `tools_enabled` is not, so a scorecard cannot currently tell you whether the agent had
  tools when it was scored. Toggling it between runs makes them incomparable and nothing in
  the card will say so. That is a real gap, in the same family as PRD open item 23.

**Tool use itself is unmeasured, deliberately.** Ragas scores whether an answer is faithful
to its context; it has no opinion on whether the right tool was called, or whether calling
none was correct. Inventing a faithfulness-shaped number for tool choice would be a new
instrument of unknown validity — which is exactly the failure §11 and PRD items 15/16
record, where a broken measurement still rendered a confident scorecard. Trajectory
evaluation is Stage 4. Until then, read the trace: `TOOL_CALL`, `TOOL_RESULT` and
`TOOL_ERROR` rows say what happened, and `GENERATE.payload.stopped_reason` says whether the
answer was finished or forced.

Retrieval parameters differ *per persona* rather than being copied — a quiz generator draws
on more of the corpus than a Socratic tutor.

---

## 6. Refusals — scored pass/fail, never averaged

A golden question carries `expected_behaviour` of `answer` or `refuse`.

**Refusal rows are excluded from all four metric means** and graded on
`eval_results.behaviour_ok`, reported as `refusal_pass / refusal_total`. A *correct* refusal
retrieves nothing useful and returns an answer that deliberately does not follow from its
context — so faithfulness and context_recall score near zero for behaving perfectly.
Averaging them in would penalise correct refusals and, worse, aim the weakest-metric pointer
at whichever metric refusals punish hardest.

Skipping them is also a saving: four judged calls per refusal question.

### How a refusal is detected

**From the answer text, never from the retrieval score.** `score_threshold` governs
*rewriting*; the system prompt governs *refusing*. On one measured corpus, on-topic questions
scored 0.61–0.67 and off-topic 0.49–0.58 — overlapping bands — and a plainly off-topic
question scored 0.5765, *above* threshold, yet was correctly refused by the prompt.

`app/api/ask.py` scans sentence by sentence, in two tiers:

| Tier | Count | Matches | Window |
|---|---|---|---|
| `REFUSAL_MARKERS` | 17 | Phrasings a model uses **only** when declining — "cannot answer", "no information", "outside the scope" | Anywhere in the first **200** chars of content (`REFUSAL_LEAD_CHARS`) |
| `CAVEAT_MARKERS` | 12 | Ordinary qualifications — "does not mention", "does not say", "does not cover" | Only in the first **40** chars (`REFUSAL_PREAMBLE_CHARS`) |

The window is what separates *a refusal* from *an answer that later notes a gap*:

```
"The provided text does not cover the specific duties..."          consumed=0    → refusal
"...eleven permanent crew [1]. It also mentions ... but it does
 not cover their specific duties."                                 consumed=198  → answer
```

Budget is charged **after** each sentence is checked, so a single long opening sentence is
still examined in full and a leading apology barely dents it.

**Put a phrase in the hard tier only if a model would never say it while answering.** Both
`"does not say"` and `"does not cover"` fail that test, which is exactly why they are in the
soft tier. This is a heuristic over natural language and it will need calibrating again — the
phrase a model reaches for is not guessable in advance.

---

## 7. Authoring the golden set

Built three ways, which coexist: **AI-suggested**, **edited** (flips `source`, and re-running
*Suggest* never touches a human-edited row), and **plain JSON import/export**.

Default split is **8 answerable + 2 refusal**.

| Constant | Value | Where |
|---|---|---|
| Structured-output method | `json_schema` | `app/eval/generate.py` — the one call in the project that is *not* `function_calling`, because Gemma silently returns empty arrays of objects through a tool call |
| `MAX_OUTPUT_TOKENS` | `4096` | Ten Q/A pairs plus schema overhead; `GENERATION_MAX_TOKENS` (2048) is too tight |
| `NEAR_DUPLICATE_JACCARD` | `0.8` | Jaccard over content words — "four of five meaningful words shared" |

**Fewer questions than requested is correct behaviour**, not partial failure. Near-duplicates
are dropped, so are answerable questions with no reference answer, and the prompt tells the
model not to pad a thin corpus.

**Refusal questions must be plausible neighbours of the corpus, not absurdities.** "Which of
the fourteen launches took place in 2040?" — where the text gives only a range — probes
grounding. "What is the capital of France?" probes nothing. PRD §3.6.1 calls this *the single
largest determinant of whether the set measures anything*, and it is why `GOLDEN_SET_MODEL`
is its own setting: measured head-to-head, Flash produced probes hinging on details the
corpus *raises and leaves incomplete* (the propellant type behind a mentioned thruster),
where Gemma asked about facts the corpus never raises. Flash's reference answers were also
several attributable claims rather than one word — which matters because `context_recall`
decomposes that field.

> **Known cost:** `GOLDEN_SET_MODEL` currently equals `RAGAS_JUDGE_MODEL`, so the judge
> grades context precision and recall against reference answers it wrote. Faithfulness and
> answer relevance never read `reference`. Set `GOLDEN_SET_MODEL=google/gemma-4-31b-it` to
> buy independence back.

---

## 8. Reading a scorecard

`eval_runs.summary` fields:

| Field | Meaning |
|---|---|
| `faithfulness` … `context_recall` | Means over scored rows. `None` ≠ `0.0` — see below |
| `weakest_metric` / `weakest_score` | The deliverable. Null when nothing was scored |
| `investment` | `{headline, why, actions}` for the weakest metric, resolved at write time so a stored card is self-contained |
| `scored_count` | Rows where **at least one** metric survived |
| `total_count` | Every active question, refusals and failures included |
| `error_count` | Rows that produced no usable metric at all |
| `refusal_pass` / `refusal_total` | The refusal tally, reported not averaged |
| `self_judged` | True when the model that wrote the answers also graded them |
| `note` | Populated only when there was nothing to summarise |

### Five ways a scorecard misleads

1. **`scored_count` is an upper bound, not the denominator.** Each metric's mean has its
   *own* `n`, because a value is only collected when non-null — so the metric most likely to
   fail has the smallest sample, and it is the one the weakest-metric pointer selects.
   Run 1 reported answer relevance as a mean over **one** value while the card said 8.
2. **`None` and `0.0` are different facts.** `None` = not measured; `0.0` = measured and bad.
   A run whose judge was rate-limited must read "measured nothing", not "perfectly unfaithful".
3. **`status=completed` does not mean every metric ran.** Failures are per-metric by design,
   so one bad row lands in `eval_results.error` instead of voiding the card. A metric that
   silently declines to measure is worse than one that crashes the run, because the card
   still renders.
4. **Perfect retrieval scores on a small corpus mean "not yet measured".** Context precision
   and recall both returning 1.000 on a single-chunk corpus is retrieval that *cannot* fail.
5. **`self_judged: true` means the numbers are not independent.** Read the caveat before the
   pointer.

**Before acting on a weak metric, read the answers.** A refusal metric measures the detector
and the agent at once, and faithfulness measures the judge and the generator at once — the
two failures look identical on the card.

---

## 9. Timeouts and limits

| Constant | Value | Bounds |
|---|---|---|
| `METRIC_TIMEOUT_S` | `180.0` s | One judged metric (several chained LLM calls) |
| `OPENROUTER_TIMEOUT_S` | `120.0` s | One HTTP call |
| `RAGAS_MAX_CONCURRENCY` | `2` | Judge calls in flight within a question |
| `ResponseRelevancy(strictness=…)` | `1` | **Not a tuning choice** — `n` is unavailable on this route, so `candidate_count=3` has no eligible provider |

**`METRIC_TIMEOUT_S` silently doubles as the retry ceiling.** It is documented as a *hang*
ceiling, but a rate-limited call retried with backoff inside the budget dies reporting
`timed out after 180s` — the same string a hang produces, and the two need opposite fixes
(wait vs. raise the ceiling). Widen that error before trusting either.

---

## 10. Run history

Same agent (`Kestrel Feynman`), same corpus, same ten questions throughout.

| | Run 1 | Run 2 | **Run 3** |
|---|---|---|---|
| Judge | `gemma-4-31b-it` | `gemma-4-31b-it` | `google/gemini-3.7-flash` |
| Self-judged | yes | yes | **no** |
| Duration | 1497 s | 1380 s | **90 s** |
| `error_count` | 7 | 2 | **0** |
| faithfulness | 0.562 (n=**7**) | 0.628 (n=**6**) | **0.769 (n=8)** |
| answer relevance | 0.795 (n=**1**) | 0.938 (n=8) | 0.959 (n=8) |
| context precision / recall | 1.000 / 1.000 | 1.000 / 1.000 | 1.000 / 1.000 |
| `refusal_pass` | 0 / 2 | 0 / 2 | **1 / 2** |
| Cost | — | — | **$0.056** |

Run 1's 7 errors were `strictness=3` (`Multiple candidates is not enabled for this model`);
run 2's 2 were faithfulness timeouts at 165–196 s per call.

**Run 3 is the first run whose footnote is true** — every metric's `n` equals the printed
`scored_count`.

**Do not read 0.628 → 0.769 as the judge delta.** Every run re-asks the questions at
temperature 1.0, so answers differ between runs; judge change and answer variation are
confounded. The clean evidence is a controlled replay: the *same stored answer*, copied
verbatim from its context, scored **0.000** by Gemma and **1.000** by Flash.

---

## 11. Known defects in the measurement

| # | Defect | Status |
|---|---|---|
| **20** | **Faithfulness penalises a teaching persona for teaching.** The Feynman persona's analogy and its "restate this in your own words" prompt are unsupported by the context *by design*, so they count as unfaithful claims. The card then names faithfulness weakest and advises tightening the grounding clause — i.e. deleting the pedagogy | **Open** |
| **16** | The persona names the gap rather than declining, so one refusal row reads as an answer. Real finding, not a detector gap | Open |
| **18** | Deleting a document cascades `query_chunks` away, emptying the contexts behind past scorecards. Scores survive, evidence does not | Open |
| — | Judge and golden-set author are the same model (§7) | Accepted, documented |

On #20, measured in run 3 — every corpus fact in these answers was correct and retrieval
worked:

```
0.500  "The collision avoidance threshold ... 1 in 10,000 [1]."        grounded
       "Please restate this idea in your own words..."                 not in context
0.571  four correct figures from the context, then
       "**Analogy:** The Ka-band is like a high-speed highway..."      not in context
```

Caveat against over-reading it: **all eight** answers carried a pedagogical tail, including
the three scoring **1.000**, so the judge does not always extract an imperative as a claim.
Part of the spread is judge variance on non-claim sentences. The analogy, though, is an
unsupported statement by construction.

**So faithfulness is not yet a valid measure for a persona that invents explanatory
material.** Either exempt clearly-marked pedagogy, or measure faithfulness on the plain
`lecture-qa` template — still never tested here.
