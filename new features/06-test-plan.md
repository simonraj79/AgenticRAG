# Feature 6 — test plan

Three layers, run in this order. Each catches what the next cannot see.

```
1. offline harnesses      no DB, no API, no model, no network.
     scripts/sandbox_check.py    the sandbox and its controls
     scripts/ledger_check.py     the citation-marker contract
     scripts/refusal_check.py    the refusal and gap detectors      (added by 09)
     scripts/llm_check.py        the OpenRouter request body        (added by 09)
2. agentic harness        real DB, real model, no browser. scripts/agentic_check.py
3. Playwright             real browser, three viewports.
     scripts/ui_check.py         scripted, GLOBAL interpreter       (added by 07)
     Playwright MCP              exploratory, for what a script cannot judge
```

**Layer 1 grew twice, and both additions came from the same realisation.** Layer 2
needs a database, a live model, a Pinecone namespace and several minutes; anything
it is the *only* check for is a thing nobody verifies while iterating. So when
[09](09-deepseek-agentic.md) found the refusal markers wrong a fourth time and the
request body carrying a parameter it should not, neither got a layer-2 scenario —
both are pure functions of their inputs, and a pure function tested through a
20-minute integration suite is a pure function that is not tested.

The rule that follows: **before writing a layer-2 scenario, ask what part of the
property is decidable offline, and move that part down.** `refusal_check.py` runs
27 cases in under a second; the layer-2 scenario that would have covered one of
them costs a full agent turn.

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

## 2. Agentic harness — `scripts/agentic_check.py`

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

## 3. Playwright MCP

Three viewports, run against a locally running stack.

```
backend   cd backend && uvicorn app.main:app --reload --port 8000
frontend  cd frontend && npm run dev            # 5173
auth      POST /api/auth/dev-login              # DEV_AUTH_ENABLED=true, ENVIRONMENT=development
```

The dev-login shim exists because a Google consent screen cannot be automated. It is gated
three ways — flag, environment, loopback client address — and returns **404** (not 403) when
any gate fails, so a failure here looks like a missing route. Check `.env` first.

### 3.1 Viewports

| Name | Size | Represents |
|---|---|---|
| desktop | 1440 x 900 | Panel docked, three columns |
| tablet | 834 x 1112 | Panel is a drawer, two columns behind it |
| mobile | 390 x 844 | Panel is a full-width drawer, one column |

Plus a **320 x 568** pass for horizontal-overflow only — narrow enough to catch the
`min-w-[18rem]` class of bug, not a layout we optimise for.

### 3.2 Journeys

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

### 3.3 Assertions run as page script

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
```

`browser_console_messages` after every journey: **zero errors, zero React key warnings.**

### 3.4 Reduced motion

One pass with `prefers-reduced-motion: reduce` forced. The drawer must still open and close.
The global `!important` rule in `index.css` kills every transition, so anything gated on
`transitionend` fails only here — which is precisely why this pass exists rather than being
assumed.

---

## 4. Iteration protocol

Findings are fixed in this order, because a fix at a lower layer invalidates the layers
above it:

```
sandbox harness  ->  agentic harness  ->  Playwright
      ^                    ^                  |
      +--------------------+------------------+
                    re-run from the lowest layer touched
```

A frontend-only fix re-runs Playwright alone. Anything touching `app/tools/` or
`app/rag/` re-runs from layer 1.

**Stop condition** is the [00-IMPLEMENTATION-PLAN.md §7](00-IMPLEMENTATION-PLAN.md)
checklist, not a feeling that it looks finished.

---

## 5. What is not tested, and why

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
