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
`query_chunks`, its `trace_events`, one `eval_results` row — and, since 2026-08-20, a
handful of `api_usage` rows recording what it actually cost (§9.1).

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

### 3.1 Handouts are OUTSIDE the scorecard, deliberately

A scorecard says nothing about charts, decks, tables or study sheets. It is worth saying so
here, because this document otherwise reads as "how good is this agent" and a reader would
reasonably assume the panel's output is included in that.

**Nothing here scores an artefact.** All four metrics read a question, an answer, the retrieved
contexts and a reference; a handout has none of those shapes. A deck is not an answer, its
"contexts" are the same chunks with no marker ledger, and there is no reference deck to compare
against. Inventing a faithfulness-shaped score for a `.pptx` would be a new instrument of unknown
validity — the same objection PRD open item 23 raises against scoring tool *choice*.

**What DOES check handouts, and at which layer:**

| Question | Where it is answered |
|---|---|
| Does the deck open, have slides, carry titles, keep bullets sane? | `scripts/deck_check.py` — layer 1, no DB, no model |
| Does a live job actually produce one, and fail when it should? | `scripts/agentic_check.py` S8, S8c, S28–S33 |
| How often is it right first time, and how grounded is it? | `scripts/deck_rate_check.py` — a MEASUREMENT, not a gate |

**The one number that behaves like an eval metric is citation density**, and it is a proxy rather
than a measure: `DECK_PROMPT` asks for a `[filename]` on each bullet, so the share of bullets
carrying one is computable from the bytes with no judge. Measured 2026-08-17, and it moved a real
decision — at `rerank_top_n=2` it is 52.9% pooled with **two decks in five under 50%**; at 10 it
is 75.1% with none. A citation can still be present and wrong, so read it as "did the model have
material to cite" and never as "is this deck correct". PRD open item 36 records what a real
measure would take.

**If handout quality is ever brought into an eval run, do not reach for faithfulness first.**
Open item 20 already records faithfulness scoring a teaching persona's analogies and
comprehension checks as unsupported claims, and then recommending the pedagogy's removal. A deck
built by the same personas would be scored the same way, for the same wrong reason.

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
| `specialists` | `NULL` | **all four, and it makes a run a MIXTURE rather than a measurement** — see below |
| `self_check_enabled` | `false` | faithfulness, latency — it can replace the answer being scored |

**`specialists` is the row that most changes what a scorecard means, and NULL is the only
value that keeps a run comparable to every baseline in §10.**

A non-null roster makes the agent an orchestrator, and routing runs per question. So a
ten-question golden set on one agent can be answered by four different personas — one
explaining, one asking a question back, one writing quiz items — and the four metric means are
then averaged **across teaching methods**, not across questions. The weakest-metric pointer
does not know that, and will name faithfulness on a run where two answers were Socratic
questions with nothing to be faithful to.

Worse, it is not stable: routing is a model call at temperature 1.0, so re-running the same
set can route differently and move a mean with no change to the agent. That is the same
confound §8 warns about for regenerated answers, one level up.

`eval_runs` records `judge_model` and `generation_model`, and **records neither the roster nor
which specialist answered each question** — that lives only in the per-turn `ROUTE` trace
payload. So unlike `generation_model`, a scorecard cannot tell you this happened. Evaluate an
orchestrator by pointing the golden set at a single-persona agent over the same corpus, or
read the `ROUTE` rows before believing a mean.

**`self_check_enabled` is milder but has the same shape.** When the check fires and the draft
is redrafted, the answer Ragas scores is the *second* one. That is the correct product
behaviour and it is a confound in a measurement: two runs of the same set can score different
answers to the same question because the check fired on one and not the other. It fires rarely
by construction — the trigger is free and most answers pass it — but "rarely" is not "never",
and the `SELF_CHECK` trace rows are the only record that it happened.

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
  the card will say so. **Closed 2026-08-23**: `eval_runs` now records `tools_enabled` and
  `max_tool_steps`, and the admin console renders them. `NULL` on both means the run
  predates the columns, which is **not** the same fact as tools being off.

**Tool use is measured as of 2026-08-23 — as a JUDGED outcome and a COUNT, never as a
score over the path.** The old wording deferred it to "Stage 4" and warned that inventing a
faithfulness-shaped number for tool choice would be a new instrument of unknown validity.
That warning is what shaped the answer rather than something the answer overrode:

- **Judged: `goal_accuracy`.** `AgentGoalAccuracyWithReference` over the turn's real
  trajectory, judged against `reference_answer`. It scores *outcome, not path*, which is the
  property that makes it usable on an agent whose path legitimately varies. **It is BINARY
  per turn**, so the aggregate is a pass rate — the console renders `7 / 9 achieved` and
  deliberately never `0.78`, because a decimal invites a comparison with faithfulness that
  is not meaningful. Validated before it was trusted: known-good `1.0`, known-bad `0.0`, and
  `scripts/agent_metrics_check.py --live` case 22 asserts the two **differ**.
- **Counted: `tool_use_ok`.** Read off `trace_events` against
  `golden_questions.expected_tool_use` — a proposition (`search` / `none` / `python`), not
  a reference call sequence. **`NULL` means no expectation was authored: the row is in
  `total` and not in `measured`,** and the card says "not measured" rather than showing a
  zero.
- **Reported and NOT graded: `calls_per_step`.** `max_tool_steps` bounds *steps*, and this
  model emits 1.50–2.00 calls per step — measured at **2.00** on a real eval turn. Nobody
  has authored a threshold, and `score_threshold` is the standing precedent for what happens
  when a number is graded against a band that overlaps, so this is a diagnostic rather than
  a verdict.

**Where to read it:** the admin console's **Trajectory** tab
(`GET /api/admin/agent-trajectory`), and per turn on `eval_results.trajectory`. The raw
trace is still the finest-grained view: `TOOL_CALL` now carries `assistant_text` and
`TOOL_RESULT` carries `content` — the string the model actually read, not just its
one-line summary — and `GENERATE.payload.stopped_reason` still says whether the answer was
finished or forced.

**Two of Ragas' own agent metrics are deliberately NOT used, and the reason is this
system's rewriter rather than the metrics.** `ToolCallAccuracy` and `ToolCallF1` compare
tool arguments byte-exactly; `REWRITE_EVERY_TURN=true` rewrites the search query every turn
at temperature 1.0. Measured, they score a correct agent **0.0**. Full record in CLAUDE.md
and PRD open item 49. A sixth way a scorecard misleads, avoided rather than shipped.

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

### 9.1 What a run costs, and how to read it wrong

Since 2026-08-20 every model call is metered — one `api_usage` row per **call**, carrying the
cost OpenRouter reported for it. Nothing here is computed from a price table, and nothing
should be: two identical requests have measured a 3.5x cost spread because they landed on
different endpoints.

**An eval run does not appear as one line item, and the obvious query undercounts it.**

| `call_kind` | What it is | Carries a `query_id`? |
|---|---|---|
| `judge` | Ragas — the four metrics | **No.** Judged calls belong to no turn |
| `goldenset` | The drafter, when you press **Suggest** | **No** |
| `generation` | The ten real turns the run drives through `run_turn` | Yes |
| `embedding`, `rerank` | Retrieval inside those turns | Yes |

So `/api/admin/spend?group_by=call_kind` filtered to `judge` is the **judging** cost, not the
run's. The run's true cost is that plus the ten turns it drove — and those are recorded as
ordinary `generation` rows, indistinguishable from a human's questions except by timestamp.
There is no join that expresses "this run's spend" (PRD open item 48).

**Two things are missing from any dollar total, and both are silent.** Reranking is priced
only if you opt in (`COHERE_SEARCH_UNIT_USD`, default off) because Cohere reports **units**
and not cost — so three rerank calls per turn contribute measured units and zero dollars. And
every query recorded before 2026-08-20 is **unbackfillable**: the OpenRouter generation id was
never stored, so those rows are *not measured* rather than zero. Read a lifetime total as a
**lower bound**. The console prints `n/m measured` beside every aggregate for this reason —
the same discipline §8 applies to `scored_count`.

**Golden-set drafting was invisible until it was fixed, which is the warning worth keeping.**
The drafter reached the model through a background task that opened no metering scope, so its
spend was written to a log and never to `api_usage` — not mis-attributed, *absent*. A sum over
zero rows is `0.0`, and `0.0` reads as a quiet week rather than as a broken meter. If a spend
figure looks implausibly low, check whether the call site is metered before concluding the
model got cheaper.

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

**Run 3's `$0.056` is not a metered figure and must not be trended against one.** All three
runs predate 2026-08-20, so no `api_usage` row exists for any of them; that number came from
token counts and a rate, which is precisely the arithmetic §9.1 says to stop doing. The first
run recorded after that date is the first whose cost is *reported* rather than computed — and
it will also be the first that includes the ten driven turns, so expect it to read **higher**
than these without anything having got more expensive.

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
| **29** | **A run on an orchestrator averages across teaching methods, and the scorecard cannot say so.** `eval_runs` records the judge and generation models but not the roster or the per-question route | **Open** — §5. Evaluate a single-persona agent, or read the `ROUTE` trace rows |
| **30** | **Self-check must not be scored with faithfulness, and the reason is that they disagree by design** | **Open** — see below |
| **31** | **The trajectory rubric had never produced a row, and four of its numbers were wrong.** `eval_results.trajectory IS NOT NULL` = 0 of 50; `summary ? 'trajectory'` = 0 of 5; `expected_tool_use` NULL on 30 of 30; 0 of 50 eval turns ever made a tool call | **Repaired 2026-08-28** — `agent_metrics_check` 54-59. See §12 |
| **32** | **No golden set here can discriminate an agentic architecture from a non-agentic one.** Measured: goal accuracy 8/8 in BOTH arms over a byte-identical corpus. Every question is a single-fact lookup the first retrieval already answers | **Open** — PRD item 56. Needs two-topic questions, not code |
| **33** | **`expected_tool_use` is unauthorable.** The drafter's schema has no field and the editor has no control, so the counted half reports NOT MEASURED forever | **Open** — PRD item 57 |
| **34** | **Nothing records which RUNTIME produced a scorecard.** An ADK run and a LangChain run are byte-indistinguishable in the database | **Open** — PRD item 58. Harmless only while `AGENT_RUNTIME` is pinned |

**On #30, and it is the sharpest instrument problem in this file.** The self-check's critic
prompt *explicitly exempts* labelled analogies, questions put to the learner, and "the
material does not cover X". Faithfulness *counts those as unsupported claims* — that is
defect #20, measured in run 3.

So the two instruments are built to disagree about the same sentences, deliberately, and
scoring the self-check with faithfulness would confirm whichever one ran last rather than
measuring anything. A run where the check fired and redrafted could score **worse** on
faithfulness precisely because the redraft kept the pedagogy the critic was told to allow.

What it actually needs is a trajectory measure — did the check fire when it should have, and
stay quiet when it should not — which is PRD open item 23 and Stage 4. Until then the honest
statement is that self-check is **unmeasured**, and `scripts/route_specialist_check.py` cases
25 and 26 are the only assertion standing behind it: the run-3 teaching answer must not fire
the check, and the same answer with its citations stripped must.

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


---

## 12. The agent rubric — what it says, and what it cannot

Added 2026-08-23 (change set 16), **repaired 2026-08-28** (change set 19). Full record in
[new features/19-agent-evaluation/PLAN.md](new%20features/19-agent-evaluation/PLAN.md).

The four metrics in §3 score an ANSWER. This block scores the TURN — did the agent achieve
the goal, and what did it spend getting there. It renders beside the four metrics and never
inside them: the judged half is BINARY and must not be shown in the same visual grammar as a
continuous score.

### The judged half — one metric, and only one

`AgentGoalAccuracyWithReference`. It scores **outcome, not path**, which is why it survives
here where the tool metrics do not: this agent rewrites its search query on every turn at
temperature 1.0, so any metric comparing tool arguments is measuring a string designed never
to repeat.

**Rejected, re-verified against ragas 0.4.3's installed source on 2026-08-28:**

| Metric | Why it stays closed |
|---|---|
| `ToolCallAccuracy` | Byte-exact argument comparison is **fixable** — a perfect comparator in the legacy `arg_comparison_metric` seam raises the reworded-query case from 0.0 to **1.0**. What survives any comparator is `if not refs: return 0.0` and the **multiplicative sequence gate** (2 calls vs 1 reference = 0.0, strict *and* loose). At 1.50-2.00 calls per step the gate zeroes essentially every real turn. **Lead with the gate, not the rewrite** |
| `ToolCallF1` | Worse: hashes `(name, args)` into a set. No comparator field at all |
| `AgentGoalAccuracyWithoutReference` | Self-judged by construction, and collides on the metric name with the variant in use |
| `TopicAdherenceScore` | No topics authored; eval is single-turn; costs `2 + N` sequential judged calls |

**`ragas.metrics.collections` is reachable and is still not adopted.** `llm_factory(...,
provider="openai", client=AsyncOpenAI(base_url=openrouter))` builds an `InstructorLLM` the
collections metrics accept — the blocker was always `LangchainLLMWrapper`, never the gateway.
Migrating costs the markdown-fence stripping that wrapper provides, and
`collections.ToolCallAccuracy` has **no comparator seam at all**, so the move makes the tool
metrics worse. `ragas.metrics` now emits a real v1.0 removal notice; the scoped suppression
in `ragas_runner.py` is hiding it.

### The counted half — arithmetic, no model, no threshold

| Key | Meaning |
|---|---|
| `searches` / `redundant_searches` | A redundant search returned **zero new chunks** — the model paid an embedding, a Pinecone query and a rerank for text it already had. Measured at **8 of 22** real production searches |
| `wasted_search_rate` | The rate, always rendered with `searches` beside it |
| `self_initiated` | Did the MODEL choose to search, as opposed to the gap trigger forcing it. Load-bearing: Gemma self-initiates 0/6 and DeepSeek 6/6, so a model swap inverts the architecture and nothing else records which side a run was on |
| `gap_forced` / `budget_exhausted` / `tool_error` | Turn counts |
| `calls_per_step` | **Reported, never graded.** Nobody has authored a threshold, and `score_threshold` is the standing precedent for grading a number against a band that overlaps |

### Four ways this block used to lie, all repaired 2026-08-28

Each was written as a failing case first; 10 of 13 went red.

1. **A FAILED search read as "it searched"** (`agent_metrics_check` 54). The verdict filtered
   on `TOOL_CALL`, and `ask.run_turn` records one for a failed call *deliberately*. Now read
   off a successful `TOOL_RESULT`.
2. **A gap-FORCED call was scored against the agent** (55). `expected_tool_use="none"` means
   *no reflex tool use*; the trigger re-invokes with a NAMED tool, so the CODE compelled it.
3. **Refusals were pooled with answerable turns** (57). A refusal scores goal accuracy **1.0
   in 9 of 9** measured attempts — including one that never searched — so 20% of a
   ten-question denominator sat pinned at 1.0. Now split, with separate denominators.
4. **"It searched, found nothing, and declined" was measured FALSE** (59). 3/3 each way.
   Withdrawn from `trajectory_metrics.py` rather than softened, and replaced with
   `self_initiated` + `searches`, which need no judge.

### Reading it without being misled

- **Goal accuracy is BINARY.** Render `n/m achieved`, never a rate, below n=20. Wilson 95% CI
  on 7/8 is [0.53, 0.98].
- **Measure the judge's noise floor before believing a delta.** `agent_eval_check --noise k`
  re-scores ONE fixed trajectory k times and prints the flip rate. It is
  `agent_metrics_check` case 22's missing sibling: case 22 asserts a good and a bad
  trajectory **differ**; this asserts the **same one does not**. Measured 0.00 on 2026-08-28,
  and 3-in-8 on a borderline turn in an earlier probe — so it is not always zero.
- **The counted signals are the decision metrics.** They need no judge, so their only
  variance is generation variance.
- **Never read a delta off two runs.** Every run re-asks at temperature 1.0. This file
  already records that mistake once, on faithfulness (`0.628 -> 0.769`, §10).

### Running it

```bash
backend/.venv/Scripts/python.exe scripts/agent_eval_check.py --n 10 --noise 5
```

Writes **nothing** — it answers through `pipeline.answer_question` and builds the trajectory
in memory, so a comparison run cannot pollute this file's run history or look like a
regression on an operator's card. It equalises every confounding agent field in memory and
asserts the equalisation, because the first version forgot `system_prompt` and reported a
persona's cost as the agent loop's.
