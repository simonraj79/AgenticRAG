# 10 — Embeddings move to OpenRouter, and the rewriter loses its trigger

> Four changes that landed together on 2026-08-16, related by one theme: **three of
> them are about a road rather than a destination**, and the fourth is what happens
> when a prompt is asked to do a job it has no information for.
>
> | | Before | After |
> |---|---|---|
> | Embedding gateway | `langchain-google-genai` | **OpenRouter** (`settings.embedding_route`) |
> | Embedding model / dimension | `gemini-embedding-2` / 768 | **unchanged — that is the point** |
> | `golden_set_model` | `google/gemini-3.7-flash` | **`minimax/minimax-m3`** |
> | DeepSeek provider pin | not attempted | **attempted, NO_GO, §4** |
> | Question rewriter | fired only with history | **every turn**, eval turns excepted |
>
> Read [loop.md](loop.md) §6 item 1 first — *"if it must run every time, call it
> yourself"* — because it is the rule that decides the shape of the fourth change,
> and T2 is the rule that decides how three of the four are tested.

---

## 1. The theme, stated once

Every change here except the golden-set swap is about the difference between **what
a thing is** and **how you reach it**, and about a failure mode that follows from
confusing the two: *a wrong road reports success*.

- A vector embedded through the wrong gateway does not raise. It retrieves, ranks
  badly, and every downstream check stays green.
- A request pinned to the wrong provider does not raise. It returns 200 from
  somebody else.
- A rewriter that stopped working does not raise. `contextualize_question` swallows
  every exception by design and degrades to the raw question.

So every assertion written for this change names an **outcome** rather than the
absence of an error, per [loop.md](loop.md) T2. Nothing here passes because a call
succeeded.

---

## 2. Embeddings: the road changed, the space did not

### 2.1 The claim, and why it needed measuring rather than reasoning about

[CLAUDE.md](../CLAUDE.md) states the hazard flatly: *"indexing with one model and
querying with another returns confident nonsense rather than an error, because
matching dimensions do not imply a shared vector space."* Shipping this swap without
a re-ingest rests entirely on the claim that the two routes reach one space — and
that claim is the one thing in this subsystem that cannot report its own failure.

Measured 2026-08-16, three strings through both routes:

| Comparison | Cosine |
|---|---|
| `embed_documents` (Google) vs `embed_documents` (OpenRouter) | **1.000000** |
| `embed_query` (Google) vs `embed_query` (OpenRouter) | **1.000000** |
| cross-string control | **0.616–0.625** |

**Both call shapes were checked on purpose, and the reason is a detail that would
otherwise have hidden a split.** `langchain-google-genai` 4.3.4 injects a `task_type`
its constructor never sets — `RETRIEVAL_DOCUMENT` on `embed_documents`,
`RETRIEVAL_QUERY` on `embed_query` — while the OpenRouter route sends neither. The
index is **written** with one shape and **queried** with the other, so proving only
one would have left the other unverified. `task_type` turns out to be inert on the
wire for this model, which is what CLAUDE.md always claimed of the *constructor* and
is now also known of the *request*.

The control row is not decoration. `1.000000` means nothing until something in the
same run scores lower.

### 2.2 Two settings, because two facts

`embedding_model` names the **space**. It is stamped onto `agents.embedding_model`
and onto every `ingestion_runs` row, and is the only durable statement of what a
stored vector *means*. `embedding_route` names the **road**, and is new.

Renaming `embedding_model` to the OpenRouter slug was the obvious edit and is the
wrong one, four ways:

- old rows and new rows would spell one space two ways, for **byte-identical**
  vectors;
- it disarms the mismatch detector `app/rag/ingest.py` exists to provide, which is
  the guard against a *real* model change;
- `app/eval/metrics_guide.py` advises checking that an agent's `embedding_model`
  matches the index, and that advice would start manufacturing false alarms;
- the settings sheet would show two spellings side by side.

`embedding_route` is an explicit setting rather than something inferred from which
key happens to be set, for the reason in §1: a wrong route is the one failure this
subsystem cannot report, and nothing else in the system records which provider wrote
a vector.

`"google"` remains as the rollback and still works. It is now the **only** reason
`langchain-google-genai` is installed, which is recorded in `requirements.in` so that
removing the package reads as a decision about rollback rather than as a cleanup.

### 2.3 The four kwargs, and the 400 each one prevents

`OpenAIEmbeddings` on this route carries four keyword arguments that all read like
style. Each is a different 400, and **you only see the next one after fixing the one
before it**.

| Kwarg | What goes wrong without it | Failure |
|---|---|---|
| `model_kwargs={"encoding_format": "float"}` | `openai-python` injects `encoding_format="base64"` unconditionally when the caller has not set it (`openai/resources/embeddings.py:111-112`) | `400 ... do not support base64 encoding_format` on **every** call |
| `check_embedding_ctx_length=False` | The default `True` routes through `_get_len_safe_embeddings`, which tiktoken-encodes the input and sends **arrays of integers** (`base.py:560, 624`) — observed on the wire as `input[0]=[791, 15690, 13941, ...]` | `400 Invalid input format` |
| `chunk_size=100` | A hard provider batch ceiling. `OpenAIEmbeddings` defaults to **1000** and does not re-batch; `langchain-google-genai` re-batched at 100 internally | `400 ... at most 100 requests can be in one batch` |
| `dimensions=768` | The model returns its **3072d** default | **No error at all** |

Four properties of that table are worth more than the table.

**The first one is a new OpenRouter parameter mechanism, and CLAUDE.md's taxonomy of
three did not cover it.** The three recorded traps are all properties of the
*gateway*: unadvertised-and-404s-at-routing (`max_completion_tokens`),
unadvertised-and-works-anyway (`stream`), and advertised-then-rejected-at-execution
(`reasoning`). This is a property of the **client library** — injected without being
asked for. Reading your own call site proves nothing, because the parameter is not
there.

**And it is the third time this library has done it here.** `max_completion_tokens`,
`parallel_tool_calls`, now `encoding_format`. Three occurrences across three
unrelated features is not three coincidences; it is a property of the library, in the
same way `pip freeze` flattening the `pywin32` marker three times is a property of
`pip freeze`. **The diagnostic step is to print the request body, never to read the
call site** — two of the three are invisible there by construction.

**`chunk_size` is the one whose absence passes every small test.** 25 texts go in a
single request, succeed, and certify a broken configuration. `app/rag/ingest.py`
hands a whole document's chunks to `store.add_texts` in one call, so the first
document over 100 chunks is where a passing 25-text probe gets paid for. Case 5 of
`embed_check.py` sends **101** texts for exactly this reason, and it is the only case
in the file that can catch it.

**`dimensions` is the silent one**, and the only member of the group with no error
attached. It is also the one that would have been *fine* to omit if the request
carried a `provider` block, and it does not: unlike the chat path, the embeddings
request sends no provider block at all, so `require_parameters` never applies and
`dimensions` is not filtered at routing. Verified live — 768d came back. **Do not add
a provider block there.**

One kwarg that is not in the table because it is not a 400: `timeout`.
`OpenAIEmbeddings` defaults to `request_timeout=None`, and embedding is now on the
request hot path for every question asked, on a single uvicorn worker. A stalled
connection would hang the turn and its SSE stream with no error and no bound. The
chat path has carried this ceiling since it moved to OpenRouter; this was the missing
half.

### 2.4 What `embed_check.py` asserts

`scripts/embed_check.py` (network, no DB, ~115 texts across six requests) exercises
the production `get_embeddings` rather than constructing its own client — a harness
that builds its own client certifies a configuration the application does not use.

| Case | Asserts | Catches |
|---|---|---|
| 1–2 | Both routes agree on `embed_documents` **and** `embed_query` | a document/query space split — the `task_type` asymmetry in §2.1 |
| 3 | Cross-string control scores far below 1.0 | a metric that hands out 1.000000 for free |
| 5 | 101 texts succeed | a missing `chunk_size`, invisible below 100 |
| 6 | 768 dimensions and an L2 norm of 1.0 | a dropped `dimensions`; and anyone re-adding the manual normalisation `gemini-embedding-001` needed, which would double-normalise silently |

Case 6's second half is the [loop.md](loop.md) §5 lesson applied to a control: the
test exists for the *opposite* failure from the one that prompted it.

---

## 3. The golden set moves to a third vendor

`golden_set_model`: `google/gemini-3.7-flash` → **`minimax/minimax-m3`**.

Flash is also `ragas_judge_model`, so while it drafted the set the judge was grading
context precision and recall against reference answers it had written itself. That
was recorded in `config.py` as an accepted cost, tolerable *only* while both context
metrics sat pinned at 1.0 on a single-chunk corpus — i.e. only while they were not
measuring anything. A third vendor makes `self_judged` false structurally rather than
nominally.

### 3.1 The regression it bought, and it is silent

Pooled over 8 runs, same corpus, same prompt:

| | MiniMax M3 | Flash |
|---|---|---|
| reference answer, median | **24 chars** | 95 chars |
| references under 20 chars | **43%** | 0% |
| shortest seen | `"14 knots"`, `"31 hours"` | — |
| p50 | ~9.8 s | ~4.9 s |
| refusal probes | hit every planted gap | as good |

`"14 knots"` is not a wrong answer. It validates, it persists, it renders in the
editor, and `LLMContextRecall` — which decomposes `reference` into claims and
attributes each to the retrieved contexts — gets **nothing to decompose**. Two of the
four metrics read this field.

**It is the same defect that ruled Gemma out as the drafter** (`"Nineteen"`, 8
characters), arriving through the model chosen to fix a *different* independence
problem. Worth naming as a shape: fixing one property of a component can reintroduce
a property you had already rejected a candidate for. The refusal probes — the thing
PRD §3.6.1 calls the single largest determinant of whether the set measures anything
— came through fine, which is why the swap survived at all.

### 3.2 The mitigation is two strings, and both halves are load-bearing

| Where | Effect |
|---|---|
| `reference_answer` field description alone | median 24 → **68**, still **34%** under 20 chars |
| **plus** a matching bullet in `SUGGEST_SYSTEM_PROMPT` | median **119–157**, **0/168** under 20 chars |

**A schema field description reads as documentation *about a field*; a system prompt
reads as *the job*.** The duplication is the measurement, not an oversight, and
deleting either half is a silent regression.

**And the first version of that system-prompt bullet cost refusal probes.** Ninety
words of rationale went in above the refusal section — the longest and
most-emphasised part of that prompt — and 3 of 12 populated runs came back with no
refusal question at all. Cut to a worked example and one clause, the length result
held and the rate fell to 1 in 16. **Prompt real estate is zero-sum and was measured
to be**, which is why the comment above that bullet says to trim it before trimming
anything below it.

### 3.3 Two model properties, recorded so they are not debugged

- **MiniMax returns a well-formed EMPTY set roughly 1 call in 6** — 5 of 30,
  `finish_reason=stop`, 46–386 output tokens, no error. A property of the model,
  present before any of this work. *Suggest* returning nothing is worth re-running,
  not investigating.
- **Reasoning is default-ON with `mandatory=false`**, measured at 93–99.98% of
  completion tokens on this prompt, so `generate.py` passes `reasoning=False`.
  Without it, three consecutive calls at production counts came back
  `finish_reason=length`. Same shape as the DeepSeek 60–79% finding in
  [09](09-deepseek-agentic.md) §3.2 and worse here: `MAX_OUTPUT_TOKENS` is sized for
  the golden-set JSON, so **the headroom thinking eats is the headroom the questions
  needed**, and the failure is a truncated set — which reads as a thin corpus.

`scripts/goldenset_check.py` measures the length **distribution** rather than
pass/fail, because 48/48 parsed while the median sat at 24: "did it parse" and "did
it raise" both stayed green throughout.

---

## 4. The DeepSeek provider pin — NO_GO, with evidence

**Recorded as a refusal rather than omitted, because an unrecorded rejection gets
re-attempted.** [09](09-deepseek-agentic.md) §3.4 notes that `top_k` silently routes
around DeepSeek's first-party endpoint, the only one with
`supports_implicit_caching: true`. The obvious follow-up is to pin the provider. It
was probed on 2026-08-16 and it is a NO_GO on all three of its own terms.

| Attempt | Result |
|---|---|
| `provider.order: ["DeepSeek"]` | **HTTP 200 on 3/3, served by Baidu every time** |
| a deliberately *misspelled* provider name | **behaves identically** |
| `provider.only: ["deepseek"]` | `404 No endpoints available matching your guardrail restrictions and data policy` |

Three findings, in the order they matter.

**1. `order` is a silent no-op here, and it is worse than an error.** It returns 200
and is served by someone else — and because a misspelling behaves the same way, there
is no typo to find and nothing to distinguish "the pin was ignored" from "the pin
worked". This is the same family as `tool_choice="any"` being accepted and not
honoured ([loop.md](loop.md) T4): a parameter that is accepted and not honoured is
worse than one that 404s.

**2. The 404 explains the 200.** `only` fails because the first-party endpoint is
filtered out by the **account's privacy setting**, and a per-request
`data_collection: "allow"` does **not** override it. So the endpoint the pin was
aiming at was never eligible for this account. `order` expresses a preference among
*eligible* endpoints and had nothing to prefer.

**3. The economics are inverted anyway.** DeepSeek first-party is **$0.14/$0.28** per
M against Baidu's **$0.0644/$0.1288** — **2.2× more** on uncached tokens — and
`cached_tokens` measured **0**. The pin would have cost more, for a cache that was
not being hit.

**So the decision is enforced rather than written down.** `llm_check.py` cases 27–29
assert that no `provider.order` and no `provider.sort` appears on any family, checked
per-family because a pin would arrive keyed on a prefix. `sort` is banned for a
second reason: it is one of exactly three documented ways to opt **out** of
quality-based provider selection for tool-calling requests, and `agent_loop.py` binds
tools on all three model invocations, so every generation turn here is one.

**What that harness structurally cannot check**, stated in its own docstring: it
asserts what this repo put in the request, never what OpenRouter did with it. A pin
that is silently ignored looks exactly like a pin that worked. Only a live call
reading the served-provider field off the response separates them — which is how the
`order` row above was found, and it is the reason cases 27–29 are a **tripwire**
rather than a proof.

---

## 5. The rewriter runs on every turn

### 5.1 Why it stopped having a trigger

The rewriter used to return immediately when there was no history, on the reasoning
that a first turn has no references to resolve. That is **true, and too narrow**: a
typo and a piece of shorthand are not references, and they are the two things most
likely to put a question's vector somewhere the corpus is not.

By [loop.md](loop.md) §6 item 1 — *"if it must run every time, call it yourself"* —
something that must happen on every turn is a plain code path, not a tool and not a
trigger. So the step widened along with its firing condition: it still resolves
references, and it now also repairs typos and expands shorthand. It does **not**
expand acronyms — that widening was built and then removed, and §5.2 is the record.

`settings.rewrite_every_turn` exists for the reason [loop.md](loop.md) S4 asks for a
flag: *"with the feature off the output is byte-identical to before"* has to be
expressible. `False` restores the old behaviour exactly. This matters more than usual
here, because there is no per-agent column to switch and because every way this
component breaks is silent — `contextualize_question` swallows every exception by
design and degrades to Stage 1, so a broken rewriter surfaces only as quietly worse
retrieval.

### 5.2 The acronym bullet fabricated, was fixed, and was removed anyway

**This is the finding of the change, and the shape of it is the transferable part:
built → fabricated → narrowed → measured green → removed regardless.** A feature
passing its own harness is not the same as a feature worth keeping.

The first version of the prompt said, flatly:

> Expand acronyms and initialisms into the full term, KEEPING the acronym as well.

That **contradicted the do-not-invent bullet four lines below it**, because an
acronym a first-turn question does not define can only be expanded from world
knowledge. The model resolved the contradiction in favour of the more specific
instruction and invented. Every line below is an observation:

```
"how fast is the Ka-band downlink?"  ->  "Ka-band (Kurtz-band) downlink"
                                     ->  "Ka-band (Kurzwellen-band) downlink"
        2 of 5 trials, both fabricated, both wrong
"wats the LS&T alloc"  ->  "Link System and Telemetry (LS&T)"
        invented, and it moved retrieval to the WRONG FILE
"hskpng tlm vol per day"  ->  "Hong Kong SpacePort (HSKP)"
```

Two things about this are worth more than the fix.

**It is [loop.md](loop.md) T1's mechanism in a module with no tool in it.** T1
explains Gemma declining to search as two competing instructions resolving in favour
of the earlier, more forceful one. Here two competing instructions resolved in favour
of the *more specific* one, with the same structure and the opposite polarity. The
lesson generalises past tools: **a prompt containing a general prohibition and a
specific licence to violate it is not a prompt with a conflict, it is a prompt with a
decision already made.**

**And the harness case guarding that exact question PASSED while printing the
fabrication.** The typo case asserts that the repaired words are *present* —
`has(r, "throughput") and has(r, "downlink") and (has(r, "Ka-band") or …)` — and
`"Ka-band (Kurzwellen-band)"` contains `"Ka-band"`. A presence assertion cannot see an
**addition**. So the case watched the model invent a German etymology and reported
green. That is T2 one more time: the assertion named a substring rather than the
outcome, and the outcome wanted here is *"is every added word recoverable from the
input?"* — which is why the guard is now its own case (7a–7c) rather than a
tightening of case 6.

#### The fix that worked, and was thrown away

The bullet was then gated on **recoverability** — the same line the repair bullets
already sit on:

> Expand an acronym ONLY when the question or the conversation above already spells
> it out […] NEVER guess what an acronym stands for. If the question and the
> conversation do not spell it out, leave it exactly as written — an unexpanded
> acronym is the document's own wording and will match it; a wrong guess will not.

**That version measured green: 5/5 in both directions** — expanding when the thread
spelled the term out, leaving it alone when nothing did. It was **removed anyway, and
deliberately — the reasoning is the part worth keeping.** The feature's
whole value was the *first-turn* case, where nothing has spelled anything out and a
bare `"LS&T"` is all the rewriter has. Gated on recoverability it therefore fires
almost never — and what remains on the page is a standing invitation to expand,
addressed to a model already observed reaching past exactly that kind of guard. A
conditional prohibition is the same shape that failed the first time.

So the rule shipped **unconditional**, and that is why it says "not even when the
conversation spells it out":

> - Leave acronyms and initialisms exactly as the user wrote them. This does not
>   apply to an ordinary misspelled word, which you should still repair.
> - NEVER expand an acronym, not even when the conversation above spells it out and
>   not even when you are confident. An acronym is the document's own wording and
>   will match it; an expansion is a guess, and a wrong guess will not match.

An unknown acronym is left exactly as written, because **a corpus that writes "C&T"
matches "C&T"**. Replacing it with a guess is precisely the false positive the whole
prompt is biased against. `rewrite_check.py` cases 7a and 7c pin it, 7c with the
measured-harmful `"LS&T"` string.

The repair bullets survive and this one did not, and the difference is measurement
rather than taste: typos and shorthand were 5/5 with **no fabrication in any trial**,
while this bullet fabricated in the first run it was given. The thing it existed for
is exactly the thing that is unsafe.

#### A leave-alone bullet is not free

The first draft of the prohibition read *"Leave acronyms, initialisms and **product
names** exactly as the user wrote them"*, and **typo repair immediately fell from 5/5
to 3/5**. The regression was `"ka bnd"` — a string the model had been repairing to
`"Ka band"` and now read as a product name to be preserved. Same two words, opposite
instructions, and the more recent one won.

Narrowing the bullet to acronyms and initialisms, with an explicit carve-out for
ordinary misspellings, restored 5/5 on the next run. **A prohibition written to stop
one behaviour will stop its neighbours unless its edge is stated.** The only reason
this was caught is that case 6 asserts *the repair happened* rather than merely
asserting *nothing was fabricated* — a silence assertion would have called the
regression a pass.

#### What survives is coreference, not expansion

The two are easy to confuse and the difference here is measured, not argued:

| Input | History | Result |
|---|---|---|
| `"C&T"`, `"LS&T"` | none | passes through untouched, **5/5** |
| the same acronym | a thread that spells the term out | `"S-band command and telemetry uplink rate"`, **5/5** |

The only variable is the conversation, so those words came from the user's own thread
and not from priors. That is the coreference bullet resolving a reference — the
rewriter's oldest job — and **an unconditional acronym ban could only beat it by
damaging coreference.**

Which is why case 7b asserts **groundedness** rather than silence: every content word
in the rewrite must be traceable to the question or the conversation. Groundedness is
the property whose absence produced `"Kurtz-band"`; silence would have failed the
turn that behaved correctly.

### 5.3 The asymmetry that shapes the prompt

A **false positive** — rewriting a question that was already fine — risks changing
what the user asked, and the answer then arrives confidently about something adjacent
with no visible cause. A **false negative** — a typo left alone — costs slightly
worse retrieval on a question the corpus may well answer anyway.

Those are not symmetric, so the prompt is biased toward minimal edits and closes with
an explicit instruction to return a clean question unchanged. **`rewrite_check.py`
case 8 is that bias made testable** — a well-formed question must come back
byte-identical — and it is held to 5/5 where the repair cases are held to 4/5,
because it is the only assertion protecting the user's meaning.

The line between repair and invention, stated once: **a repair must be recoverable
from the question itself plus the conversation — never from the corpus, never from
world knowledge about the subject.**

### 5.4 `None` stopped meaning what it meant

`rewritten_question is None` used to mean "the rewriter was not asked". It now means
"the rewriter was asked and came back with nothing", and **the two need opposite
responses from anyone reading a trace**: the first is a configuration, the second is a
degraded turn. `AnswerResult.rewrite_attempted` carries the difference, the REWRITE
trace event is recorded on it rather than on `rewritten_question is not None`, and the
payload gained `failed` and a real `trigger` (`first_turn` vs `conversation_history`,
where it had been hardcoded to the latter and mislabelled every first-turn rewrite).

**A rewriter that read six turns of history and concluded the question already stood
on its own made a decision too**, and a trace showing nothing for it is
indistinguishable from a turn where it was never called. `changed` carries the
difference for anyone who only wants the interesting rows.

### 5.5 Eval turns opt out

`settings.eval_rewrite_questions = False`.

`app/eval/jobs.py` creates one fresh, archived conversation per golden question. Until
now "no history" also meant "no rewrite at all", so a golden question was embedded
verbatim **by accident of an empty thread**. The rewriter running on first turns
removes that guarantee silently, and three things break, none of them loudly:

- **every [EVAL.md](../EVAL.md) baseline stops being comparable** — same rows in the
  database, different string reaching the embedder;
- **the golden-question editor keeps showing a question that is not the one that was
  asked**, because there is no `queries.rewritten_question` column and the rewrite
  lives only in a trace payload;
- **nothing errors**, per §5.1.

So the guarantee is bought back deliberately rather than inherited as a side effect.
Turning it on is a measurement decision that requires a full re-run of the baselines
**and** an editor that displays the rewritten string.

### 5.6 What `rewrite_check.py` asserts

Two layers in one file, structural first (cases 1–5, no network, milliseconds) and
behavioural second (cases 6–11, real OpenRouter calls, about a minute). The order is
the point: a red structural case **explains** a red behavioural one, instead of
sending the reader to debug a model over a prompt that no longer says what they think
it says. Same S16-before-S15 ordering `agentic_check.py` uses, arrived at for the same
reason.

Two mechanics worth copying:

- **Trials, not single samples.** The rewriter runs at `generation_temperature` (1.0,
  from Gemma's card), so one call is an anecdote. The Ka-band fabrication appeared in
  2 of 5.
- **`get_contextualizer.cache_clear()` before anything that changes the prompt or the
  settings the chain was built from.** The chain is `@lru_cache(maxsize=1)`, so
  without it the file would happily test the previously-built chain and report green.

---

## 6. Deferred, with the reason

### 6.1 Grounded acronym expansion — the version worth building next

§5.2 forbids expansion rather than grounding it, and **forbidding is the safe answer,
not the good one.** The bullet was reaching for something real: a workshop attendee
typing `"LS&T"` at an agent whose corpus spells it out on page one should get the
expansion, and today they do not.

**The removal in §5.2 makes this section more relevant, not less.** What was thrown
away there was a version gated on the *conversation* — and the objection to it was
never that it scored badly (it scored 5/5) but that the conversation is empty on the
turn the feature exists for. Grounding in the **corpus** is the one gate that is
populated on a first turn. It is the same feature aimed at a source that is actually
there.

The version worth building passes **the agent's own document titles** into the
rewriter prompt, so an expansion comes from the corpus rather than from priors. That
turns the currently-forbidden operation into a *recoverable* one under §5.3's own
rule — the expansion would be recoverable from material the agent actually holds.

It is deferred because it is not a prompt edit:

- `contextualize_question(question, history)` would have to take the **agent**, which
  changes a signature `pipeline.py` currently keeps free of tenancy;
- `get_contextualizer` is `@lru_cache(maxsize=1)` over **one static prompt**. A
  per-agent prompt needs a cache that is not keyed on nothing, and getting that wrong
  means one agent's document titles reaching another agent's rewriter — a tenancy
  leak in the one module that had none;
- it needs its own measurement, against the same 5-trial bar and the same case-8
  over-firing guard, because more prompt material is exactly what §3.2 measured as
  zero-sum.

**Recorded here rather than left as a widened bullet.** The failure mode to avoid is
someone reading §5.2, agreeing that forbidding is unsatisfying, and relaxing the
prompt without building the grounding — which is precisely how the fabricated
`"Kurtz-band"` was produced the first time. Reinstating the *conditional* bullet is
the same mistake wearing a green harness: it has already been built, already measured
5/5, and already removed on the grounds that passing was not the bar.

### 6.2 `queries.rewritten_question`

The rewrite is durable only in the REWRITE trace payload, which is why
`conversations._load_messages` reads `after` back out of that payload to fill
`MessageOut.rewritten_question` — making those key names part of the contract rather
than implementation details.

A column would be the honest home, and it is a schema decision this module does not
get to make alone. Two things now depend on the absence:

- §5.5's opt-out is partly a *consequence* of it. If the rewritten question were
  readable off `queries`, rewriting eval turns would become an option rather than a
  silent loss of the baseline.
- Stage 2's score-triggered rewrite (PRD §3.5) will write REWRITE rows too, and only
  `trigger` says which one fired. A column would need to answer "which rewrite" as
  well as "what".

---

## 7. Test suites

| Layer | File | Cost | New here |
|---|---|---|---|
| 1 (structural) | `scripts/rewrite_check.py` cases 1–5 | ms, no network | ✅ |
| 1 | `scripts/llm_check.py` cases 26–29 | ms, no network | routing width, the `order`/`sort` tripwire |
| 1.5 | `scripts/embed_check.py` | ~6 requests | ✅ both routes, one space |
| 1.5 | `scripts/goldenset_check.py` | several drafting runs | ✅ reference-length distribution |
| 1.5 | `scripts/rewrite_check.py` cases 6–11 | ~1 min | ✅ live rewrites |

Layer 1.5 is a category this change introduced and [06-test-plan.md](06-test-plan.md)
now names: **network, but no database, no Pinecone and no writes.** It exists because
the properties above are not decidable offline — a cosine between two providers, a
length distribution over real drafts — and putting them in layer 3 would mean nobody
runs them while iterating. That is the same argument
[06-test-plan.md](06-test-plan.md) records for moving `refusal_check.py` and
`llm_check.py` down out of layer 3, applied one step further up.

---

## 8. Where the code is

| Concern | File |
|---|---|
| `embedding_route`, `openrouter_embedding_model`, `embedding_batch_size`, `rewrite_every_turn`, `eval_rewrite_questions`, `golden_set_model` | [`app/config.py`](../backend/app/config.py) |
| `get_embeddings`, the four kwargs and the 400 each prevents | [`app/rag/retriever.py`](../backend/app/rag/retriever.py) |
| `CONTEXTUALIZE_SYSTEM_PROMPT`, the acronym **prohibition** and why expansion was removed, `rewrite_attempted` | [`app/rag/pipeline.py`](../backend/app/rag/pipeline.py) |
| REWRITE payload: `trigger`, `failed`, `changed` | [`app/api/ask.py`](../backend/app/api/ask.py) |
| `rewrite=` on the eval turn | [`app/eval/jobs.py`](../backend/app/eval/jobs.py) |
| `reference_answer` description, `SUGGEST_SYSTEM_PROMPT`, `reasoning=False` | [`app/eval/generate.py`](../backend/app/eval/generate.py) |
| One embedder for the judge too | [`app/eval/ragas_runner.py`](../backend/app/eval/ragas_runner.py) |
| Two routes, one space | [`scripts/embed_check.py`](../scripts/embed_check.py) |
| Rewriter, structural then live | [`scripts/rewrite_check.py`](../scripts/rewrite_check.py) |
| Reference-length distribution | [`scripts/goldenset_check.py`](../scripts/goldenset_check.py) |
| Routing width, the NO_GO enforced | [`scripts/llm_check.py`](../scripts/llm_check.py) |
