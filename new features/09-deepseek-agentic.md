# 09 — DeepSeek drives the loop and the handouts

> Moves generation, the agent loop and handout production from
> `google/gemma-4-31b-it` to `deepseek/deepseek-v4-flash-0731` on OpenRouter.
>
> **This document exists because the swap invalidated
> [loop.md](loop.md)'s central finding.** T1 says to assume the model will not call
> your tool, and every trigger in `agent_loop.py` was built on that. The new model
> calls it 6/6 unprompted. That does not make the trigger wrong — it makes it
> *conditional*, and conditional machinery is the kind that rots quietly.
>
> Read [loop.md §3](loop.md) first. This is a worked instance of it where the
> answer came out the other way round.

---

## 1. What changed, in one table

| | Before | After |
|---|---|---|
| `generation_model` | `google/gemma-4-31b-it` | **`deepseek/deepseek-v4-flash-0731`** |
| `generation_reasoning` | — | **`False`** (new setting) |
| `decision_model` (rewriter) | `google/gemma-4-31b-it` | **unchanged — measured, §6** |
| `ragas_judge_model` | `google/gemini-3.7-flash` | unchanged |
| `_NO_TOP_K_PREFIXES` | `("google/gemini-",)` | `("google/gemini-", "deepseek/")` |
| Gap trigger gate | `gap and not used and step < max` | **`… and not corpus_searched`** |
| Marker matching | lowercase + collapse whitespace | **+ strip markdown emphasis** |

Handouts follow `generation_model` automatically — `_model_for` already read
`agent.generation_model or settings.generation_model`. The only edit there was
passing `reasoning`.

**Nothing about the swap is per-agent.** `agents.generation_model` still overrides
it, still holds `NULL` everywhere, and is still unreachable from the API (§8).

---

## 2. The five questions, answered before the code

Per [loop-prompt.md](loop-prompt.md).

**1. Is this a tool, a prompt change, or a plain code path?**
None of the three — it is a *model* change, which the template does not cover and
which turns out to be the most dangerous kind, because it silently revalues every
measurement in the repository. Four documents assert Gemma behaviour by name.

**2. Smallest surface. What does it close over?**
`build_chat_model` gained exactly one parameter, `reasoning: bool | None`, default
`None` so every existing caller's request is byte-identical. It closes over the
provider quirk: callers pass `top_k` and `llm.py` decides per family, so no call
site learns which models want it.

**3. Assume the model will not call the tool. What signal says it was needed?**
**Inverted, and this is the finding.** The signal now needed is the opposite one:
what says a tool was called when it should *not* have been? The gap trigger fired
a redundant search on every correct refusal (§4.2).

**4. False positive vs false negative on the detector?**
Unchanged asymmetry, new failure mode. `detect_gap`'s cost model is still "a false
positive costs one retrieval, a false negative costs the feature" — but with a
self-initiating model, a false positive now costs a retrieval *on every refusal*
rather than occasionally. That is what moved the gate rather than the strictness.

**5. What makes the feature necessary rather than merely present?**
S13 owns `generation_model` and points it back at Gemma. Without that scenario the
entire gap branch could be deleted and every other test would stay green.

---

## 3. Measurements

All 2026-08-16, against the live OpenRouter route, one-chunk context, two-part
question, real refusal-first persona prompt. Probe scripts were throwaway; the
numbers are reproduced in the code comments that depend on them.

### 3.1 The T1 inversion

| Configuration | `gemma-4-31b-it` | `deepseek-v4-flash-0731` |
|---|---|---|
| `tool_choice="auto"`, full grounding prompt | **0** | **5/5 searched** |
| bare prompt, no grounding rule | 0 | searched |
| `"You MUST call search_corpus"` | 0 | searched |
| `tool_choice="any"` | **ignored** | **honoured** |
| `tool_choice="search_corpus"` (named) | called | called |

`with_structured_output(function_calling)` 3/3. `astream` with tools bound: 148
chunks, first at 0.55 s. Generation p50 **6.05 s** against Gemma's measured 13.2 s
on a route where CLAUDE.md puts generation at 89% of the turn.

### 3.2 Reasoning — the 2×2 that decides everything

`reasoning.mandatory=false, default_enabled=true, default_effort="high"`, so
leaving it alone is a decision. Reasoning consumed **60–79% of billed output**
(out `[118, 296, 371]`, reasoning `[70, 198, 293]`) at the completion rate.

6 trials per cell, "did it search unprompted":

| | guidance paragraph | no guidance paragraph |
|---|---|---|
| **reasoning on** | 6/6 | 6/6 |
| **reasoning off** | **6/6** | **2/6** |

**The `TOOL_GUIDANCE` paragraph and reasoning are redundant with each other, and
either alone holds the behaviour.** That is what makes `generation_reasoning=False`
affordable — and it is precisely what makes it dangerous, because the paragraph
now reads as dead weight from a superseded model. Delete it and nothing raises;
tool use drops to a third. **S16 pins the disjunction.**

Turning it off also reduced redundant work: 2.00 → 1.50 search calls per step,
p50 3.27 s → 1.07 s.

### 3.3 Handouts — the hypothesis that was wrong

The expectation was that code generation would want reasoning ON. 6 chart recipes
per arm, scoring the outcome `_problem()` already triggers on — *`chart.png`
present on the **first** attempt*, never "the call succeeded":

| | first-try file | p50 |
|---|---|---|
| reasoning on | 5/6 | 30.4 s |
| reasoning off | **6/6** | **8.1 s** |

Off won on both axes, so there is **one** setting, not two. Note what a
first-attempt miss looks like from the outside: `_problem` catches it, the retry
succeeds, the row still ends `ready`. It costs a whole extra model call and
sandbox run and is invisible to any "did it error" check — [loop.md](loop.md) T2
applied to a configuration choice.

Probe 3 also reproduced T2's own example live: one trial exited 0 having computed
the chart and never called `savefig`. The existing artefact-absence trigger caught
it, at roughly 1 in 3.

### 3.4 Routing

28 endpoints serve the model. **All 28** advertise `tools` and `tool_choice`, so
tool binding has far more headroom than Gemma's 14. `reasoning` is advertised by
all 28, so sending it narrows nothing. `parallel_tool_calls` is advertised by
**exactly one**, which is why `disabled_params` remains load-bearing.

`top_k` is advertised by 18 of 28 — so unlike Gemini it does **not** 404. It
routes, returns 200, and silently excludes DeepSeek's own first-party endpoint,
the only one with `supports_implicit_caching: true` and a $0.0028/M cache read
against $0.028/M elsewhere. **An unadvertised parameter does not only 404; under
`require_parameters` it also narrows routing, and a narrowed route is a cost with
no error attached.**

---

## 4. The two behavioural changes

### 4.1 Markdown emphasis — the marker list is wrong a fourth time

10 unanswerable questions; `detect_gap` fired on 8. One miss:

```
"The material does **not** mention the cell chemistry vendor."
```

`"does not mention"` is in `CAVEAT_MARKERS` and has been for some time. The
phrase is present, the meaning is present, and the substring match cannot see it
because the negation is bolded.

The first three corrections — `"does not say"`, `"does not cover"`,
`"does not state"` — produced [loop.md](loop.md) T3's rule: *when a list has been
wrong three times, add the shape rather than the string.* This is the same lesson
one level up. **The list was not short of a phrase; the normaliser was short of a
rule**, and all 34 markers were equally blind. Fixing it in the list would have
meant a bolded variant of every one.

It reaches `detect_refusal` too, which writes `queries.refused` — so a model that
bolds its negations, an utterly ordinary habit, would have produced a fourth
scorecard blaming the agent for the detector.

The second miss — *"I can answer the first part of your question but not the
second"* — is **not fixed**, deliberately. Every phrasing that would catch it
(`"but not"`) is broad enough to fire on ordinary prose, and the tier it would
land in feeds a retry on every turn. Its cost is now bounded by the model
self-initiating anyway. Recorded, not papered over.

**Then a fifth correction, and it is the opposite defect.** `agentic_check.py` S7
went red on a textbook refusal:

> "The corpus explicitly states that the modulation and coding schemes ... are
> **not covered in this briefing**. ... The search did not surface any additional
> material containing this information."

`refused` came back `False`. The marker is `"not covered in the"`; the model wrote
`"not covered in this"`. **The determiner.**

The four earlier corrections were markers that were *missing*. This one was
present and **too specific** — locked to one determiner, so it missed every other
member of its own family: `this briefing`, `this material`, `that document`. The
same miss had already happened unnoticed in a browser turn writing `"not covered
in this material"`, where it defeated `detect_gap` **as well** — one
over-specified marker suppressing a retry *and* corrupting a metric.

Fixed by truncating to `"not covered in"` and `"not found in"`. It costs nothing:
every string they matched before, they still match. Same T3 lesson from the other
side — **a determiner is not part of the shape.**

That this surfaced as an *intermittent* red is the part worth keeping. At
temperature 1.0 the model reaches for a different phrasing each run, so the
scenario passed on the first run and failed on the second with no code change
between them. A flaky refusal scenario reads as model variance and gets
re-run; it was a real bug both times.

### 4.2 The gap trigger learns what it was actually asking

Old gate: `gap and not gap_search_used and step < max_steps`.

That was exact only while the model never searched. On a self-initiating model the
ordinary shape of a **correct refusal** became:

```
step 1   model searches, finds nothing
step 2   model answers "the corpus does not cover X"
...      detect_gap fires, forces the SAME search again
```

A guaranteed wasted retrieval on every correct refusal, plus a nudge inviting the
model to re-answer a question it had already answered correctly.

Restated as T2 the gate is obvious rather than clever: **the outcome this trigger
wants is "the model searched before it declined", never "an admission appeared in
the text".** If a search has run, the outcome occurred. So: `and not
corpus_searched`.

A search returning **zero results still counts as searched** — `search_corpus`
reports `ok=True` with "0 results above the retrieval floor" precisely because
finding nothing is an answer, and that is exactly the case that must not re-run.

**Residual risk, stated:** a turn could search for topic A and admit a gap about
topic B. Accepted on the measurement that this model emits 1.50–2.00 calls per step
and covered both halves of a two-part question in 8/8 steps.

### 4.3 Leaked tool-call markup — found in a browser, not by any harness

The whole layer-1 and layer-2 suite was green. Driving the real UI produced this,
on a turn that spent its entire step budget:

```
Let me try searching for the thermal rejection budget document and the
modulation/coding document explicitly

<｜DSML｜tool_calls> <｜DSML｜invoke name="search_corpus"> …
```

When the budget runs out the loop re-invokes with `tool_choice="none"`. The model
still *wanted* to search, so it expressed that the only way left to it — as text.
The delimiter is **U+FF5C FULLWIDTH VERTICAL LINE**, not ASCII `|`, which is
exactly why it survives every provider-side parser looking for the ASCII form.

**Nothing raised. `out.answer` was long and non-empty. S6 asserted `bool(out.answer)`
and stayed green.** S6 is the only scenario that exercises the forced-final-answer
path, so it was the only one that could have caught it, and it was asserting the
error-shaped question.

Three consequences, in the order they matter:

1. **S6 now asserts the answer is free of machinery** rather than merely present,
   and imports the sentinel rather than retyping it.
2. **The strip lives in `_message_text`**, the single place the loop reads text off
   a message — not at the three call sites.
3. **Streaming needed its own gate, and stripping the stored answer is not enough.**
   Tokens are on the user's screen before `_message_text` ever runs; a correction
   after the read is not a correction. `_emit_until_markup` latches — once markup
   appears nothing further is emitted — and it tests the **join** of everything
   streamed so far, because a two-character sentinel arriving as `"<"` then `"｜"`
   is invisible to any per-chunk test. Same class as `sentences()` refusing to
   split on a lone newline: the transport's chunk boundaries are not meaningful,
   and a matcher that assumes they are fails on a schedule nobody can reproduce.

Residual, stated: a chunk ending exactly on the `<` has already written that one
character. Holding a one-character tail back would fix it and would delay every
legitimate answer's last character. A stray `<` is the cheaper defect.

**The lesson is about where the bug was found.** Four layer-1 harnesses, twenty
layer-2 scenarios and a fifteen-assertion layout harness all passed. It took
opening the page and reading an answer. The suite tests what it was told to look
for, and this was in the one place nobody thought to look: the text itself.

---

## 5. Test suites

Two new layer-1 harnesses, four new layer-2 scenarios, one layer-3 pass.

### 5.1 `scripts/llm_check.py` — 15 cases, no network

Every OpenRouter trap CLAUDE.md records is a property of the **request body**, and
every one was found by reading a traceback from a live call. They are all decidable
offline: `build_chat_model` returns a configured client, so `extra_body`,
`disabled_params` and `model` can be inspected without a key or a quota.

Pins: `max_tokens` in `extra_body` (never the renamed client field);
`parallel_tool_calls` disabled; `top_k` kept for Gemma, dropped for Gemini *and*
DeepSeek; `reasoning` **absent** when nobody asked (case 9 — the byte-identical
guarantee); `require_parameters` on; and what the shipped generation path really
sends.

### 5.2 `scripts/refusal_check.py` — 27 cases, no network

`refusal.py` is the most-corrected module in the repo — wrong four times, every
regression silent — and had **no harness at all**. Cases come in pairs, because a
detector widened four times fails next by *over*-firing: cases 20–24 are the
must-not-fire guard.

Writing it immediately found a fifth defect, pre-existing and unrelated to this
swap: `detect_gap`'s docstring illustrated the two-tier asymmetry with
`"does not contain"`, a **hard**-tier phrase that matches in any sentence within
the lead. `detect_refusal` returns a marker for that example, not `None` as the
docstring claimed. Docstring corrected to a caveat-tier phrase; behaviour pinned
by case 15b and **not changed** — see §7.

### 5.3 `scripts/agentic_check.py` — S13–S16

| | Asserts | Owns |
|---|---|---|
| **S16** | at least one of {guidance sequencing, reasoning} survives | — (structural, cannot be flaky) |
| **S15** | a search with no `trigger` — the model *chose* it | `retrieve_k` |
| **S14** | a refusal that searched is not made to search again | — |
| **S13** | the gap trigger still fires on a non-self-initiating model | `generation_model`, `retrieve_k` |

Registered S16 → S13 on purpose: a red S16 *explains* a red S15, and the reverse
order sends the reader to debug the model instead of the config.

**S13 is the one that earns its place.** With the default model searching on its
own, every other scenario stays green if the entire gap branch is deleted. Its
only remaining proof is a model that behaves the way Gemma does — which
`agents.generation_model` can still select.

**S15's two trials are arithmetic, not superstition.** The shipped configuration
self-initiates 6/6; the configuration this suite must catch scores 2/6. Two trials
send ~89% of broken runs red without spending four full turns on a property S16
also pins deterministically.

### 5.4 Layer 3

`scripts/ui_check.py` unchanged and re-run: **15/15, zero not-measured** — the swap
touches no layout.

Playwright MCP drove the real UI, and earned its place twice. It confirmed a
model-chosen search end to end (both `TOOL_CALL` rows `trigger=None`, one per half
of the question, no gap-forced third), and it found §4.3, which every scripted
layer had passed over.

It also produced the turn that made the retrieval-budget doubling concrete:
`tool_steps=3`, **`tool_calls=6`**.

### 5.5 A UI change the swap forced

The turn chip read **"searched once"** on a turn that ran two retrievals. Not a
regression — `tool_steps` was an honest search count while the model made one call
per step, and the field's own docstring said it "counts every tool round-trip".
That sentence became false.

So `tool_calls` is now recorded in the GENERATE payload beside `tool_steps`,
carried on `AskOut` and `MessageOut`, and rendered as `Math.max(steps, calls)`.

**`Math.max` rather than preferring `tool_calls`**, because it is 0 on every turn
recorded before the server sent it — taking it outright would relabel an old
two-step turn as *no searches at all*, replacing an undercount with a wrong count,
invisibly, on exactly the conversations nobody re-reads. Verified in the browser:
an old turn still reads "searched once", and the six-call turn reads
**"searched 6 times"**.

This matters beyond cosmetics. `max_tool_steps` is a slider a workshop attendee is
invited to tune, and until this change nothing on screen revealed that three steps
can be six retrievals.

---

## 6. What was deliberately NOT changed

**`decision_model` stays on `google/gemma-4-31b-it`.** This leaves the only Gemma
call site in the codebase, which is exactly the asymmetry a later reader tidies
away, so the measurement is recorded at the setting. 9 trials each — two
coreference cases plus one already-standalone question that must be left alone:

| | parsed | correct action | p50 |
|---|---|---|---|
| `google/gemma-4-31b-it` | 9/9 | **9/9** | **1.02 s** |
| deepseek, reasoning off | 9/9 | 9/9 | 1.58 s |
| deepseek, reasoning default | 9/9 | 8/9 | 0.85 s |

Gemma is not worse and is faster than the arm that matched it. CLAUDE.md measures a
follow-up already paying 3.8 s for contextualisation, so 0.5 s on every
conversational turn buys nothing but consistency. A regression here would also be
**invisible** — `contextualize_question` swallows every exception and degrades to
Stage 1, so a broken rewriter surfaces only as quietly worse retrieval.

**`ragas_judge_model` and `golden_set_model` stay on `google/gemini-3.7-flash`.**
Judge independence is preserved and slightly improved: generation and judge were
already different, and are still different.

**`TOOL_GUIDANCE` is unchanged**, including its Gemma-era final paragraph. §3.2 is
why.

**No marker moved between tiers.** §7.

---

## 7. One open question, raised not resolved

CLAUDE.md's rule is that a phrase belongs in the hard tier *"only if a model would
never say it while answering"*. `"does not contain"` plainly fails that test — the
docstring's own example is an answer containing it, and it scores as a refusal.

Moving it to the caveat tier would be correct by the stated rule and would change
what `queries.refused` means, and therefore what every scorecard in
[EVAL.md](../EVAL.md) is comparable to. **That is an evaluation decision, not a
swap decision**, so it is pinned by `refusal_check.py` case 15b, written down here,
and left alone.

---

## 8. The model became switchable — and that promoted a latent bug

`agents.generation_model` was unreachable from the API: absent from
`AgentTunables` and `AgentOut`, with `extra="forbid"` making a PATCH a 422. It
could only be set by direct SQL, which mattered more after this swap than before
it — pointing one agent back at Gemma is now a *meaningful* operation, and it is
the configuration S13 exists to protect.

**Exposed 2026-08-16.** `AgentTunables` (so create and update both accept it),
`AgentOut`, and a `<select>` in the settings sheet with a measured shortlist plus
an "Other" box. Null still means "use the server default" and clearing is
supported.

### 8.1 The bug the picker would have shipped

`generation_reasoning` is false, so every generation call carried
`reasoning: {"enabled": false}`. `google/gemini-3.7-flash` answers that with a
hard 400:

```
Reasoning is mandatory for this endpoint and cannot be disabled.
```

Verified across plain generation, a tool-bound call and
`with_structured_output` — **all three failed identically.** So Flash was
unusable as a generation model, and the picker would have offered it as a menu
item that breaks every turn.

This is the shape worth keeping: **a configuration value nobody could reach was
hiding a defect, and exposing it is what made the defect reachable.** While it
was settings-only, nobody was going to point generation at Flash by accident. The
audit for "add a field to a schema" is therefore not the schema — it is *what
values become possible that were not before*.

`build_chat_model` now withholds the flag for families that refuse it
(`_REASONING_ALWAYS_ON_PREFIXES`), and drops it rather than raising: the caller's
intent is "do not spend tokens thinking", and on a model that cannot comply the
honest outcome is the model's default, not a failed turn.

A second, smaller find in the same pass: `_LEGACY_SLUGS` mapped
`gemini-flash-lite-latest` to `google/gemini-3.7-flash-lite`, and OpenRouter says
that **is not a valid model ID**. A legacy-id guard whose whole purpose is to stop
a bare id 404ing, which mapped to a model that 400s, is worse than no entry —
the unmapped path at least warns and names its guess. Removed, and `llm_check.py`
case 25 now asserts every mapping target is a real `author/model` id.

### 8.2 Free text, with the failure moved to save time

The API accepts any `author/model` id rather than a `Literal` whitelist —
`llm.py` calls this "a free-text column an operator can type into", and enumerating
it would mean a deploy every time OpenRouter adds a model, in a workshop about
trying models. The `SplitterName` precedent points the other way, and the
distinction is why: a bad splitter *silently downgrades* to one the user cannot see
they got, whereas a bad model id fails loudly.

Loudly, but in the wrong place. `openrouter_slug()` guesses `google/<model>` for a
bare id and logs a warning nobody reads, so a typo was stored and then 404'd on
every answer — which CLAUDE.md records as reading like an outage rather than a
namespace error. `_reject_unroutable_model` makes that a 422 at the moment a human
can fix it, checking **shape only**: verifying existence would put a third-party
network call inside a settings save and still would not prove the model serves the
parameters this app sends.

The shortlist in the UI is the other half of that answer. Every entry was measured
against the exact request this app sends — `require_parameters` routing,
`max_tokens` in `extra_body`, the `reasoning` flag, and `tools` + `tool_choice`:

| Model | plain | tools | structured |
|---|---|---|---|
| `deepseek/deepseek-v4-flash-0731` | ok | ok | ok |
| `google/gemma-4-31b-it` | ok | ok | ok |
| `google/gemini-3.7-flash` | ok *(after 8.1)* | ok | ok |

A `<select>`, not a `Segmented`: `Segmented` lays its options in one non-wrapping
`inline-flex` row, and three model slugs would push the sheet past the viewport and
fail `ui_check.py` A7 (zero horizontal overflow at 320px). Verified 15/15 with the
control in place, including A8's 44px sweep with the sheet open.

---

## 9. Where the code is

| Concern | File |
|---|---|
| Model + reasoning defaults, with the 2×2 | [`app/config.py`](../backend/app/config.py) |
| `top_k` per family, the `reasoning` parameter | [`app/rag/llm.py`](../backend/app/rag/llm.py) |
| The `corpus_searched` gate | [`app/rag/agent_loop.py`](../backend/app/rag/agent_loop.py) |
| `strip_emphasis`, corrected docstring | [`app/rag/refusal.py`](../backend/app/rag/refusal.py) |
| `reasoning` on the generation path | [`app/rag/pipeline.py`](../backend/app/rag/pipeline.py) |
| `reasoning` on the handout path | [`app/handouts/jobs.py`](../backend/app/handouts/jobs.py) |
| Request-body harness | [`scripts/llm_check.py`](../scripts/llm_check.py) |
| Detector harness | [`scripts/refusal_check.py`](../scripts/refusal_check.py) |
| S13–S16 | [`scripts/agentic_check.py`](../scripts/agentic_check.py) |
