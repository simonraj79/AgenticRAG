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

### 2.1 Evaluation UI checks are not metric checks

The frontend now has two fast regression layers:

| Check | Command | What it establishes |
|---|---|---|
| Component behavior | `cd frontend && npm test` | Vitest + Testing Library; currently covers source-first empty agents and create-wizard validation/focus |
| Browser layout and accessibility | `python scripts/ui_check.py` | Playwright against both local servers; viewport fit, focus/inert behavior, tap targets, overflow and console errors |

These checks protect the interface used to author and read evaluations; they do **not** prove
that the four metrics are correct. Metric correctness still belongs to the real pipeline,
stored contexts and Ragas checks described below. Conversely, a trustworthy score does not
prove the scorecard is reachable or usable on a phone, so both layers are required before a
frontend-affecting evaluation change is considered done.

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

### 4.1 Credentials

| Variable | Used for | Required |
|---|---|---|
| `OPENROUTER_API_KEY` | **Every model call** — generation, decision, golden-set drafting, judge, **and embeddings** | ✅ |
| `PINECONE_API_KEY` | Vector store | ✅ |
| `COHERE_API_KEY` | Reranking | ✅ |
| `GEMINI_API_KEY` | The **rollback** embedding route only (`EMBEDDING_ROUTE=google`) | optional |

> **Embeddings moved to OpenRouter on 2026-08-16, and this changed the road, not the
> space.** Same model (`gemini-embedding-2`), same 768 dimensions, same vectors already in
> Pinecone — measured cosine 1.000000 between the two routes on both `embed_documents` and
> `embed_query`, so no re-ingest was needed and none was done. `EMBEDDING_MODEL` still names
> the space and did not change; `EMBEDDING_ROUTE` names the gateway.
>
> Two consequences for reading a run. Ragas now draws its judge LLM and its embedding model
> from **one** provider, where it used to straddle two. And `OPENROUTER_API_KEY` is now the
> key without which *retrieval* fails as well as generation — a failure that looks nothing
> like a missing model key. See `scripts/embed_check.py` and
> `new features/10-routing-and-embeddings.md`.

### 4.2 Models

| Variable | Default | What it does |
|---|---|---|
| `GENERATION_MODEL` | `deepseek/deepseek-v4-flash-0731` | Writes the answers being graded |
| `GENERATION_REASONING` | `false` | Thinking on the generation path. On, it was 60–79% of billed output tokens |
| `DECISION_MODEL` | `google/gemma-4-31b-it` | The question rewriter. **Runs on every turn** now — but not on eval turns, see §4.6 |
| `GOLDEN_SET_MODEL` | `minimax/minimax-m3` | **Drafts** the golden set (§7). A third vendor, so it no longer shares a model with the judge |
| `RAGAS_JUDGE_MODEL` | `google/gemini-3.7-flash` | **Grades** the answers |
| `RAGAS_JUDGE_REASONING_EFFORT` | `low` | `high` / `medium` / `low`. Thinking is **mandatory** on Flash — it cannot be switched off |
| `EMBEDDING_MODEL` | `models/gemini-embedding-2` | The vector **space**. **Changing it invalidates the index** |
| `EMBEDDING_ROUTE` | `openrouter` | The **gateway** to that space. `openrouter` or `google`. Changing it invalidates nothing — same model, same vectors |
| `EMBEDDING_DIMENSION` | `768` | Fixed at index creation |
| `RERANK_MODEL` | `rerank-v3.5` | Cohere |

Model ids are **`author/model`**. A bare `gemma-4-31b-it` returns a 404 naming a model that
plainly exists; `app/rag/llm.py` maps the known legacy ids and warns rather than guessing.

> **The generation model changed on 2026-08-16, and every scorecard recorded before that
> date was written by a different agent.** `eval_runs` captures `generation_model` per run
> precisely so this is visible rather than inferred — read it before comparing two runs, and
> treat a comparison across the boundary as two measurements, not a trend. Judge
> independence is unaffected and slightly improved: generation and judge were already
> different models, and still are.
>
> Two things change what a scorecard *means*, beyond the numbers moving:
>
> - **The agent now searches on its own.** Answers are drawn from chunks retrieved
>   mid-turn, not only from the initial retrieval, so `contexts` for a question can differ
>   run to run even with identical retrieval parameters. Context precision and recall are
>   measuring a wider and less deterministic set than they were.
> - **`refusal_pass` should read slightly higher for a reason that is not the agent.** Two
>   detector fixes landed with the swap: the marker lists now match through markdown
>   emphasis (`does **not** mention` was defeating them), and two markers lost a hard-coded
>   determiner, so `"not covered in this briefing"` now scores like `"not covered in the
>   briefing"` — it previously did not. Some of any improvement is the measurement being
>   repaired, not the agent improving; that is the confound §10 already warns about, and it
>   has now occurred five times in this project.
>
> `new features/09-deepseek-agentic.md` has the measurements.

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

### 4.6 Eval turns do not use the question rewriter

| Variable | Default | What it does |
|---|---|---|
| `REWRITE_EVERY_TURN` | `true` | The rewriter runs on **every** chat turn, first turns included — typos, shorthand and references. **Never acronyms**: expansion was built, measured fabricating, and removed (CLAUDE.md), so an acronym in a trace's REWRITE payload should read exactly as the user typed it |
| `EVAL_REWRITE_QUESTIONS` | **`false`** | **Eval turns opt out.** A golden question reaches the embedder verbatim |

**Why the opt-out exists, and why it is a measurement decision rather than a default.**
Until 2026-08-16 the rewriter only fired when there was conversation history to resolve
against. An eval run creates one fresh, archived conversation per golden question, so "no
history" meant "no rewrite" and every golden question was embedded verbatim **by accident of
an empty thread**. When the rewriter started running on first turns, that guarantee
disappeared silently.

Three things would have broken, none of them loudly:

- **Every baseline in §10 would stop being comparable.** The questions would still be the
  same rows in the database, and a different string would reach the embedder.
- **The golden-question editor would keep showing a question that is not the one that was
  asked.** There is no `queries.rewritten_question` column — the rewrite lives only in the
  REWRITE trace payload — so nothing in the UI could reveal the difference.
- **Nothing would error.** `contextualize_question` swallows every exception by design and
  degrades to the raw question, so a rewriter that is running, or failing, is invisible
  either way.

`eval_rewrite_questions=True` turns it back on. **Do it only together with a full re-run of
the §10 baselines and an editor that displays the rewritten string** — otherwise the run
measures a set of questions nobody can read back.

Note this does not make an eval turn identical to a chat turn any more. It is one deliberate
difference, recorded here rather than inferred from a missing trace row: a chat turn's
retrieval may benefit from a repaired typo, and the eval turn measuring "the same" question
does not.

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
| `generation_model` | `NULL` → service default | faithfulness, **and what the agent does** — see below |
| `tools_enabled` | `true` (new agents) / `false` (pre-existing) | **all four, and it is not a tuning knob** |
| `max_tool_steps` | `3` | context_recall, latency |

**`generation_model` became editable in the settings sheet on 2026-08-16, and it is the
second row here that changes what is being measured.** It used to be reachable only by
direct SQL, so in practice every agent used the service default and the row was theoretical.
It is not any more.

The models differ in *behaviour*, not just quality: measured on the same probe,
`deepseek/deepseek-v4-flash-0731` initiated a corpus search unprompted 6/6 where
`google/gemma-4-31b-it` scored 0/6, which is the whole reason the gap trigger exists. So two
agents on the same corpus with the same retrieval parameters can retrieve different context
and be scored on it. Unlike `tools_enabled`, **`eval_runs` does record `generation_model`**,
so a scorecard can tell you — read it before comparing two runs, and treat a comparison
across a model change as two measurements rather than a trend.

**`tools_enabled` is the other row in this table that changes what is being measured, not how
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

### 7.1 The drafter moved off the judge's model — 2026-08-16

`GOLDEN_SET_MODEL` used to equal `RAGAS_JUDGE_MODEL`, so the judge graded context precision
and recall against reference answers it had written itself. That was recorded here as an
accepted cost; it is now paid off. **`minimax/minimax-m3` is a third vendor**, so generation
(DeepSeek), judging (Google) and reference authorship (MiniMax) are three different models.

**It cost one regression, and it is the silent kind — read this before editing any prompt in
`app/eval/generate.py`.** Pooled over 8 runs on the same corpus and prompt:

| | MiniMax M3 | Flash |
|---|---|---|
| reference answer, median | **24 chars** | 95 chars |
| references under 20 chars | **43%** | 0% |
| shortest seen | `"14 knots"`, `"31 hours"` | — |
| refusal probes | hit every planted gap | as good |

A reference answer of `"14 knots"` is not wrong. It validates, it persists, it renders in the
editor — and `context_recall`, which decomposes `reference` into claims, gets **nothing to
decompose**. Two of the four metrics read this field. This is the same defect that ruled
Gemma out as the drafter (`"Nineteen"`, 8 characters), arriving through the model chosen for
independence.

**The mitigation is two prompt strings and both halves are load-bearing.** A stronger
`reference_answer` field description *and* a matching bullet in `SUGGEST_SYSTEM_PROMPT`. The
field description alone moved the median from 24 to 68 and still left 34% under 20 — an
improvement that fails. With both, the median measured **119–157** with **0/168** references
under 20 characters. `scripts/goldenset_check.py` measures the distribution, because a
wording edit can undo this without anything raising.

Two further properties of this model, so they are not mistaken for defects:

- **A well-formed EMPTY set arrives roughly 1 call in 6** (`finish_reason=stop`, no error).
  Pre-existing model behaviour; *Suggest* returning nothing is worth simply re-running.
- **Reasoning is default-ON**, measured at 93–99.98% of completion tokens on this prompt, so
  `generate.py` passes `reasoning=False`. Without it the call hits `finish_reason=length` and
  returns a **truncated** set — which reads as a thin corpus rather than as a truncation.

Set `GOLDEN_SET_MODEL=google/gemini-3.7-flash` to trade the independence back for longer
references and half the latency.

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
| `self_judged` | True when the model that wrote the answers also graded them. **False on every current run** — and read §7.1, because it compares judge to *generator* only and says nothing about who wrote the references |
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
5. **`self_judged: true` means the numbers are not independent — and `false` is a narrower
   promise than it sounds.** It compares the judge to the *generator*, nothing else. Until
   2026-08-16 it read `false` on runs where the judge had also written every reference
   answer, which is what `context_precision` and `context_recall` are scored against. Both
   are independent now (§7.1); the flag was never the thing that established it.

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

> **All three runs predate 2026-08-16, and three things have changed underneath them since:**
> the generation model (§4.2), the golden-set drafter (§7.1), and the embedding route (§4.1).
> Only the third is provably neutral — same model, same vectors, cosine 1.000000. Treat a
> run recorded after that date as a **new baseline**, not the next point on this trend. The
> next run is also the first that could be compared question-for-question against these,
> because eval turns still embed verbatim (§4.6).

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
| — | Judge and golden-set author are the same model | **Resolved 2026-08-16** — `GOLDEN_SET_MODEL` is `minimax/minimax-m3`, a third vendor. It cost a short-reference regression that a prompt mitigation holds down; §7.1 |
| — | `reference_answer` length depends on a prompt string with nothing enforcing it at write time | Open — `scripts/goldenset_check.py` measures it out of band, but a short reference still saves cleanly |
| — | The rewrite is stored only in the REWRITE trace payload, so a rewritten question cannot be read back from `queries` | Open — the reason eval turns skip the rewriter entirely (§4.6) rather than rewriting and recording |

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
