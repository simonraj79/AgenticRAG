# Feature 6 — test plan

Five layers, run in this order. Each catches what the next cannot see.

```
1. offline harnesses      no DB, no API, no model, no network.
     scripts/sandbox_check.py    the sandbox and its controls
     scripts/ledger_check.py     the citation-marker contract
     scripts/refusal_check.py    the refusal and gap detectors      (added by 09)
     scripts/llm_check.py        request body + routing width       (added by 09, extended by 10)
     scripts/rewrite_check.py    cases 1-5, prompt structure        (added by 10)
1.5 network harnesses     real provider calls. NO DB, no Pinecone, no writes.  (added by 10)
     scripts/embed_check.py      both embedding routes, one space
     scripts/goldenset_check.py  golden-set reference-length distribution
     scripts/rewrite_check.py    cases 6-11, live rewrites (--models for a bake-off)
2. frontend unit tests    jsdom, no backend. cd frontend && npm test
3. agentic harness        real DB, real model, no browser. scripts/agentic_check.py
4. Playwright             real browser, four viewports.
     scripts/ui_check.py         scripted, GLOBAL interpreter       (added by 07)
     Playwright MCP              exploratory, for what a script cannot judge
```

**Layer 1 grew twice, and both additions came from the same realisation.** Layer 3
needs a database, a live model, a Pinecone namespace and several minutes; anything
it is the *only* check for is a thing nobody verifies while iterating. So when
[09](09-deepseek-agentic.md) found the refusal markers wrong a fourth time and the
request body carrying a parameter it should not, neither got a layer-3 scenario —
both are pure functions of their inputs, and a pure function tested through a
20-minute integration suite is a pure function that is not tested.

The rule that follows: **before writing a layer-3 scenario, ask what part of the
property is decidable offline, and move that part down.** `refusal_check.py` runs
27 cases in under a second; the layer-3 scenario that would have covered one of
them costs a full agent turn.

**[10](10-routing-and-embeddings.md) then found the rule had a gap, and layer 1.5 is
it.** Three of its properties are not decidable offline and do not need a database
either: a cosine between two embedding providers, a reference-length distribution
over real drafting runs, and whether a live model actually leaves a clean question
alone. Under the old two-way split those land in layer 3 by default — a database, a
namespace and twenty minutes to measure something that needs none of them, which is
the same "nobody runs it while iterating" outcome the rule was written to prevent.
So the axis is not *offline vs integrated*, it is **what state does this need**, and
"a network call and nothing else" is a real answer.

Two structural rules carried over into the new layer:

- **Order structural cases before behavioural ones inside one file.**
  `rewrite_check.py` is layer 1 for cases 1–5 and layer 1.5 for 6–11, in that order,
  so a red structural case *explains* a red behavioural one rather than sending the
  reader to debug a model over a prompt that no longer says what they think it says.
  Same S16-before-S15 ordering `agentic_check.py` arrived at.
- **Run the production constructor, not a copy.** `embed_check.py` goes through
  `app.rag.retriever.get_embeddings` rather than building its own client. A harness
  that builds its own client certifies a configuration the application does not use.

---

## 1. Sandbox harness — `scripts/sandbox_check.py`

Twelve cases, listed in [02-code-interpreter.md §8](02-code-interpreter.md). Runs in
seconds, needs nothing but the venv.

**Cases 8-12 are the security cases and they are the point.** A change to `app/tools/` that
leaves 1-7 green and turns any of 8-12 red has removed a control without failing a build.

**ASCII in `print()`.** This has broken three throwaway scripts in this repo — an emoji, a
`§`, and a `│` copied from a tree diagram. The Windows console codepage mangles them and the
script dies while reporting a result. Status markers are `[ok]` / `[FAIL]`, and anything
echoed back from a model or the database goes through `ascii()`.

Exit code 1 on any failure, so it can gate a commit.

---

## 1.5 Network harnesses — added by [10](10-routing-and-embeddings.md)

Real provider calls, **no database, no Pinecone, no writes**. Each one measures a
property that has no error attached to it, so every assertion names an outcome rather
than the absence of a failure ([loop.md](loop.md) T2).

### `scripts/embed_check.py` — both embedding routes land on one vector

Needs `OPENROUTER_API_KEY` and `GEMINI_API_KEY`; roughly 115 texts across six
requests.

| Case | Asserts | Catches |
|---|---|---|
| 1–2 | The two routes agree on `embed_documents` **and** `embed_query` | a document/query space split — `langchain-google-genai` injects `RETRIEVAL_DOCUMENT`/`RETRIEVAL_QUERY` at call time, the OpenRouter route sends neither, and the index is written with one shape and queried with the other |
| 3 | Cross-string control scores far below 1.0 | a similarity metric that hands out 1.000000 for free |
| 5 | **101** texts succeed in one `embed_documents` call | a missing `chunk_size` — a hard 100-input provider ceiling. 25 or 100 texts fit in one request, succeed, and certify a broken configuration |
| 6 | 768 dimensions and an L2 norm of 1.0 | a dropped `dimensions` (3072 comes back with **no error**); and anyone re-adding the manual normalisation `gemini-embedding-001` needed, which would double-normalise silently |

Case 5 is the one to read if this file is ever trimmed. It is the **only** case that
can see a missing `chunk_size`, and `app/rag/ingest.py` hands a whole document's
chunks to `store.add_texts` in one call — so the first document over 100 chunks is
where a passing 25-text probe gets paid for.

### `scripts/goldenset_check.py` — reference answers that decompose into claims

A **distribution, not a pass/fail**, because 48/48 golden sets parsed while the median
reference sat at 24 characters. "Did it parse" and "did it raise" both stay green
through the failure this file exists for: `context_recall` decomposes
`reference_answer` into claims, and a bare `"14 knots"` decomposes into none while
still rendering a number on the scorecard.

Cases 0a/0b assert that the complete-sentence rule is present in **both**
`generate.py` prompt strings — the field description and `SUGGEST_SYSTEM_PROMPT` —
because the field alone was measured as insufficient. Case 2 measures what they are
for; cases 4–5 guard the `reasoning=False` that keeps the set from being truncated.

### `scripts/rewrite_check.py` cases 6–11 — the rewriter, live

Real OpenRouter calls through `pipeline.contextualize_question`, about a minute.
Cases 1–5 in the same file are layer 1 and run first.

Two mechanics that are not optional:

- **Trials, not single samples.** The rewriter runs at temperature 1.0, so one call is
  an anecdote — the acronym fabrication that motivated cases 7a–7c appeared in 2 of 5.
  Repair cases tolerate one bad sample in five; **case 8, the over-firing guard, is
  held to 5/5**, because it is the only assertion protecting the user's meaning.
- **7a–7c guard a prohibition, not a feature.** Acronym expansion was built, measured
  fabricating, narrowed to a conditional version that scored 5/5, and then removed
  outright — so 7a asserts the acronym survives *while the typo beside it is still
  repaired*, 7c asserts nothing is invented for an unknown one (`"LS&T"`), and **7b
  asserts groundedness rather than silence**: coreference may still legitimately
  produce the spelled-out term when the conversation supplied it, and a silence
  assertion would fail the turn that behaved correctly.
  [10-routing-and-embeddings.md](10-routing-and-embeddings.md) §5.2 is the record.
- **`get_contextualizer.cache_clear()` before anything that changes the prompt or the
  settings the chain was built from.** The chain is `@lru_cache(maxsize=1)`; without
  it the file tests the previously-built chain and reports green.

`--models` runs a model bake-off instead of the standard pass.

---

## 2. Frontend unit tests — `cd frontend && npm test`

Vitest runs in jsdom with Testing Library and `@testing-library/jest-dom`. It needs no
backend, session or model and is the first gate for a frontend change.

| File | Contract |
|---|---|
| `EmptyAgentWorkspace.test.tsx` | Renders the source-first explanation and CTA, exposes no textbox, and calls the Sources navigation callback |
| `CreateAgentWizard.test.tsx` | Name owns focus, **Next** starts disabled, and duplicate-name validation appears as soon as the user types a duplicate |

The suite deliberately does not pretend jsdom is a browser. The wizard focus test passed in
isolation while the integrated Drawer still focused its heading. Playwright found that race;
the fix was a shared `initialFocusRef` owned by the focus-trap primitive. Component tests
protect the local contract, and layer 4 protects composition, layout and accessibility.

Exit code 1 on failure, so `npm test` can gate a commit.

---

## 3. Agentic harness — `scripts/agentic_check.py`

Real database, real OpenRouter, real Pinecone. Follows the shape of the existing
`scripts/slice_check.py`, including its `--cleanup`.

```
--setup     create a throwaway agent, ingest a two-topic fixture corpus
--run       execute the scenarios below
--cleanup   delete the agent, its namespace and its handouts
```

The fixture corpus is **two small markdown files on different topics**, written for this
test. That is deliberate: [CLAUDE.md](../CLAUDE.md) records that context precision and
recall both scoring exactly 1.0 on the current single-chunk corpus means retrieval *cannot
fail*, so a multi-hop test against it would prove nothing.

| # | Scenario | Asserts |
|---|---|---|
| S1 | `tools_enabled=false`, ordinary question | Trace has exactly the six pre-existing event types. **This is the regression test** — the classic path must be untouched |
| S2 | `tools_enabled=true`, single-topic question | Zero `TOOL_CALL`. The model must not call tools reflexively |
| S3 | `tools_enabled=true`, question spanning both files | `>= 1 TOOL_CALL` for `search_corpus`; `new_chunks > 0`; the answer cites markers from both files |
| S4 | "Chart the figures in that answer" | `TOOL_CALL` for `run_python`; a `handouts` row, `status="ready"`, `mime_type="image/png"`, non-empty `source_code` |
| S5 | Question that provokes bad Python (e.g. an unavailable library) | `TOOL_ERROR` then a corrected `TOOL_CALL` then an answer. **Self-correction is the assertion** |
| S6 | `max_tool_steps=1` on the S3 question | `stopped_reason="max_steps"` and an answer is still returned |
| S7 | Off-corpus question with tools on | Refusal. Tools must not turn "I don't know" into invention |
| S8 | Each of the four recipes via `POST /handouts` | All reach `ready`; the `.pptx` opens with `python-pptx`; the `.png` opens with `PIL`; the `.csv` parses; the `.md` is non-empty |
| S9 | Timing | `contextualize_ms + retrieval_ms + tool_ms + generation_ms` within 15% of `latency_ms` |
| S10 | Citation integrity after two searches | Markers contiguous 1..N; every `[n]` in the answer resolves to a citation |
| S11 | List 50 handouts with SQL echo on | No emitted statement selects `handouts.content` |
| S12 | Quota | Creating one past `handout_max_per_agent` returns 409 and deletes nothing |

**S1 and S7 are the two that protect what already works.** Everything else tests the new
feature; those two test that the new feature did not eat the old one.

`--cleanup` matters more than usual: a leaked Pinecone namespace is a real cost and the
Builder plan's 1,000-namespace cap is the ceiling on agents.

---

## 4. Playwright MCP

Four viewports, run against a locally running stack.

```
backend   cd backend && uvicorn app.main:app --reload --port 8000
frontend  cd frontend && npm run dev            # 5173
auth      POST /api/auth/dev-login              # DEV_AUTH_ENABLED=true, ENVIRONMENT=development
```

The dev-login shim exists because a Google consent screen cannot be automated. It is gated
three ways — flag, environment, loopback client address — and returns **404** (not 403) when
any gate fails, so a failure here looks like a missing route. Check `.env` first.

### 4.1 Viewports

| Name | Size | Represents |
|---|---|---|
| desktop | 1440 x 900 | Panel docked, three columns |
| tablet | 834 x 1112 | Panel is a drawer, two columns behind it |
| mobile | 390 x 844 | Panel is a full-width drawer, one column |
| narrow mobile | 320 x 844 | Overflow and compact-navigation stress pass |

The 320 px pass is narrow enough to catch the `min-w-[18rem]` class of bug. It is a stress
pass, not a separate product layout.

### 4.2 Journeys

| # | Journey | Run at |
|---|---|---|
| J1 | Sign in, open an agent, ask a question, read the answer, expand a citation | all three |
| J2 | Expand the trace on a turn that used tools; the Python is readable, not JSON-escaped | desktop, mobile |
| J3 | Open Handouts, pick "Chart", type a brief, submit, watch `pending` -> `ready`, download | all three |
| J4 | Ask the agent to chart something in chat; the handout appears in the panel with no manual refresh | desktop, mobile |
| J5 | Open the "Code" reveal on a handout; the source renders in a scrollable mono block | desktop |
| J6 | Delete a handout; confirm the two-step `ConfirmDeleteButton` behaves | tablet |
| J7 | Keyboard only: Tab to the Handouts toggle, Enter, Tab through the panel, Escape, confirm focus returned | mobile |
| J8 | Documents tab: cards below `sm`, table at `sm`+, delete reachable without sideways scroll | mobile, desktop |
| J9 | Open a zero-document agent: source-first explanation and CTA are present, no chat textbox exists, CTA opens Sources | desktop, mobile |
| J10 | Open New agent: dashboard is inert, Name owns focus, Next is initially disabled, actions remain visible inside the 390x844 viewport | desktop, mobile |

### 4.3 Assertions run as page script

```js
// zero horizontal overflow
document.documentElement.scrollWidth <= document.documentElement.clientWidth

// tap targets
[...document.querySelectorAll('button, a[href], input, textarea, select, [role="button"]')]
  .filter(el => el.offsetParent !== null)
  .filter(el => el.getBoundingClientRect().height < 44)
  .filter(el => !el.classList.contains('gw-chip'))
  .map(el => el.dataset.testid ?? el.textContent.trim().slice(0, 30))
// -> must be []

// exactly one scroll region in the chat column
[...document.querySelectorAll('*')].filter(el => el.scrollHeight > el.clientHeight + 2)

// source-first empty state
document.querySelector('[data-testid="empty-agent-workspace"]') !== null &&
document.querySelector('textarea, input[type="text"], [role="textbox"]') === null
```

`browser_console_messages` after every journey: **zero errors, zero React key warnings.**

### 4.4 Reduced motion

One pass with `prefers-reduced-motion: reduce` forced. The drawer must still open and close.
The global `!important` rule in `index.css` kills every transition, so anything gated on
`transitionend` fails only here — which is precisely why this pass exists rather than being
assumed.

---

## 5. Iteration protocol

Findings are fixed in this order, because a fix at a lower layer invalidates the layers
above it:

```
offline  ->  network  ->  frontend unit  ->  agentic harness  ->  Playwright
   ^           ^               ^                   ^                  |
   +-----------+---------------+-------------------+------------------+
                        re-run from the lowest layer touched
```

A frontend-only fix runs `npm test`, `npm run build`, then Playwright. Anything touching
`app/tools/` or `app/rag/` re-runs from layer 1. Start at the lowest layer that can decide
the property, but always finish with the first real-browser layer for UI changes.

**Layer 1.5 sits where it does because it is cheap enough to run on every backend
change and expensive enough that it is not free.** A change to `app/rag/retriever.py`
or either eval prompt should re-run it; a change to a route handler need not.

**Stop condition** is the [00-IMPLEMENTATION-PLAN.md §7](00-IMPLEMENTATION-PLAN.md)
checklist, not a feeling that it looks finished.

---

## 6. What is not tested, and why

- **Ragas scoring of tool use.** Ragas scores whether an answer is faithful to its context.
  It has no opinion on whether the right tool was called, and inventing a faithfulness-like
  score for tool choice would be a new measuring instrument of unknown validity — exactly
  the failure [CLAUDE.md](../CLAUDE.md) records twice (the `strictness=3` bug and the
  refusal-detector gap), where a broken measurement still renders a confident scorecard.
  Trajectory evaluation is Stage 4.
- **Load or concurrency.** One uvicorn worker on Render's starter plan; the sandbox spawns a
  subprocess per call. Concurrent tool use will contend. That is a known deferral, in the
  same class as PRD item 19 (blocking SDK calls inside `async def`), and it should be fixed
  with those rather than alone.
- **Cross-browser.** Playwright runs Chromium. The one place this is a real gap is the
  range-input styling, where `::-webkit-slider-thumb` and `::-moz-range-thumb` must be
  separate rules — already documented and unchanged by this work.
- **Sandbox escape by a determined attacker.** The threat model is a confused or
  prompt-injected model, not an adversary with arbitrary input.
  [02-code-interpreter.md §5](02-code-interpreter.md) states the limits; the harness tests
  the controls that exist, not the ones that do not.
