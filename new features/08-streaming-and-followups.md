# Feature 8 — SSE streaming, and three things that were quietly broken

> Written after the fact rather than before it, which is itself the note: this change set
> was built by a coordinated fan-out of agents, and **the two worst defects in it were
> created by how the work was divided, not by how it was written.** Both are recorded below
> under the decision that caused them.
>
> Measured on 2026-08-16 against the running stack and, where it says so, against
> production.

---

## 0. What shipped

| | |
|---|---|
| **SSE streaming** | Two new routes; the answer arrives token by token instead of after 25–45 s of spinner |
| **Duplicate upload** | The 409 is now recoverable from the UI — and the `force` flag now actually survives to the ingest job |
| **`tab-evaluate`** | No longer resolves to two live elements |
| **Real Google OAuth** | Verified against the new shell, in production |
| **Sandbox** | `static_check` refuses instead of raising when the parser gives up |

`agentic_check` 16/16 · `ui_check` 15/15, 0 not measured · `sandbox_check` 16/16.

---

## 1. The finding worth keeping: probe the parameter, do not read the list

CLAUDE.md's T5 rule is to check `mcp__openrouter__list-model-endpoints` **before** adding
anything to a tool-bound request, because `provider.require_parameters` is on and an
unadvertised parameter 404s. So that check ran first, and it said **do not build this**:

> `stream` appears in the `supported_parameters` of **zero** of the 19 endpoints serving
> `google/gemma-4-31b-it`.

It was probed anyway, because "no provider anywhere advertises it" is a suspicious shape —
it would make streaming impossible for every model on the gateway, which cannot be true of a
product that documents streaming. Measured on this repo:

| Probe | Result |
|---|---|
| `astream`, no tools | 12 chunks, first token 0.83 s |
| `astream`, `bind_tools([search_corpus])` | 12 chunks, first token **0.50 s** |

Both fine, under `require_parameters: true`, with `top_k` in `extra_body`.

**The distinction, which is the transferable part:** `supported_parameters` describes the
**sampling** surface, and OpenRouter's routing filter consults only that. `stream` is a
transport flag on the response body, so it never enters the filter.
`max_completion_tokens`, by contrast, **is** a sampling parameter, is equally unadvertised,
and 404s — while being honoured perfectly if it arrives.

So the rule this project has been operating on is too broad. It is not *"an unadvertised
parameter 404s"*. It is:

> **An unadvertised parameter that ROUTING CONSULTS 404s. The list is silent in both
> directions and silent for opposite reasons — so probe it.**

Two minutes of probing against a design that the documented rule said was impossible.

`tools` ∩ `top_k` remains **14 of 19** endpoints, unchanged from the last measurement.

---

## 2. Shape — streaming is a transport, and nothing else moved

```
POST /api/agents/{agent_id}/ask/stream
POST /api/conversations/{conversation_id}/ask/stream
```

Nine event types, in [`app/rag/events.py`](../backend/app/rag/events.py):

| Event | Carries |
|---|---|
| `start` | `query_id`, `conversation_id` — how a new thread learns its id |
| `phase` | `rewrite` / `retrieve` / `rerank` / `generate`, `started` \| `finished`, durations, `top_score` |
| `token` | one delta, never cumulative, concatenated verbatim |
| `tool_call` · `tool_result` · `tool_error` | live, per step, as the loop runs |
| `answer_reset` | the gap trigger fired and the text so far is being discarded |
| `done` | the **exact** non-streaming `AskOut` body, wrapped in `result` |
| `error` | only after headers are already on the wire |

Four properties are load-bearing, and each one is a rule from
[loop.md](loop.md) applied rather than restated:

- **The JSON routes are not edited at all.** New paths, not a `?stream=` flag — a flag would
  have forced `response_model=AskOut` off the existing handler and deleted the validation
  that makes the terminal payload identical. `agentic_check` S1 "classic path unchanged"
  therefore passes *structurally* rather than by care, and it does pass.
- **One `emit` callback threads through `run_turn → answer_question → run_agent_loop`.**
  With `emit is None` every branch is the **identical line** it was, not a similar one.
  Streaming is the `else`.
- **`emit` is not a `TraceRecorder`.** The pipeline still never touches the database; trace
  rows are still written at the boundary in the one existing transaction (loop.md S3).
- **The complete answer text still reaches `detect_refusal` and `detect_gap`**, which are
  position-sensitive over the whole string. Streaming the text does not mean detecting on
  fragments of it.

### Measured

Backend probe: `start` → 5 × `phase` → 14 × `token` → `done`; streamed text identical to
`done.result.answer`; all ten `AskOut` keys present; `X-Accel-Buffering: no` set.

In the browser, on a persona turn: the thread grows **3538 → 4850 characters between 1.2 s
and 16.8 s** while `aria-busy="true"`. Previously the user watched a spinner for the whole
turn, which CLAUDE.md already called "the worst part of the product".

**Streaming does not make a short answer faster** — the 39-character probe reached its first
token at 6.2 s of a 6.7 s turn, because retrieval is 5.3 s of that and nothing streams
before generation starts. The gain is entirely on the long persona answers, which is where
the complaint was.

---

## 3. The blocker, and the decision that caused it

**The "Upload it again anyway" button could not work on the default configuration.**

`documents.py` accepts `?force=true` and skips its own duplicate pre-check. But
`run_ingest_job` had **no `force` parameter**, so the flag was dropped at the background
handoff, and `ingest_bytes` — which repeats the same check — re-deduplicated the upload
inside the job. `settings.ingest_in_background` defaults to `True`, so this was the default
path, not an edge case.

The sequence:

```
POST .../documents?force=true   ->  202, a `pending` row committed
UI                              ->  prompt cleared, picker reset, polling started
~60 s later                     ->  status: failed
                                    "Already in this corpus as 'x.md'.
                                     Re-upload with force to ingest it again."
```

The message told the user to do the thing they had just done.

**This is loop.md T2 in its purest observed form.** Every error-shaped check passed: the
POST returned 202, nothing raised, no console error, the UI's own success path ran end to
end. The *outcome* — a second copy being indexed — silently did not happen, and the only
symptom arrived a minute later on a row nobody was still watching.

### Why nobody caught it

The investigation phase correctly reported that "the backend already supports `force`". It
does — **on the route.** The implementation task was then scoped *frontend-only* on the
strength of that sentence, and the file where the flag was actually lost belonged to no one.

The lesson is about fan-out, not about Python: **when an investigation says "the backend
already supports X", the implementation brief must still own the whole path X travels.** A
capability that exists at the entry point is not a capability that reaches the worker. Scope
by the data's journey, not by the layer.

It was found by an adversarial review pass whose only instruction was *"find the case where
nothing throws but the outcome did not happen"* — and it was found twice, independently, by
two reviewers with different lenses.

Verified end to end after the fix: 409 without `force`, `ready` with it, two copies both
indexed.

---

## 4. `static_check` raised instead of refusing

`agentic_check` S8 "recipe table" failed with:

```
MemoryError: Parser stack overflowed - Python source too complex to parse
  File "app/tools/sandbox.py", line 289, in _static_refusal
    tree = ast.parse(code)
```

The model emitted a deeply nested literal and CPython's parser gave up. `_static_refusal`
caught `SyntaxError` and `ValueError` and nothing else, so the exception escaped and took
the whole handout job with it.

**That is the failure [loop.md §4](loop.md) exists to prevent, occurring inside the one
function whose entire job is to prevent it.** A static check that raises instead of refusing
denies the model the thing that makes a code interpreter valuable: reading its own error and
fixing it. The row lands `failed` with a traceback nobody can act on.

`MemoryError` and `RecursionError` are now caught **narrowly, around the parse only**, and
that placement is the whole safety argument — CPython raises `MemoryError` there for a
parser stack overflow rather than for genuine exhaustion, so this swallows a parse failure
and not an out-of-memory condition elsewhere in the process.

Both branches were proven against inputs that really crash it, because a defensive `except`
that has never fired is indistinguishable from one that does not work:

| Input | Raw | Now |
|---|---|---|
| `'['*100000 + ']'*100000` | `SyntaxError` | already handled |
| `'-'*200000 + '1'` | **`MemoryError`** | refused, actionable |
| `'+'.join(['1']*200000)` | **`RecursionError`** | refused, actionable |

---

## 5. Three more from the review

- **`onStart` promoted a draft to an uncommitted `conversation_id`.** The row is created
  inside the turn's transaction and committed 25–45 s later; the client learned the id at
  ~0.1 s. Stop → follow-up → 404 on a turn that was working perfectly. The old JSON route
  could not reach this state because the client only learned the id *after* the commit —
  **streaming created the window.** `send` now waits for the id to appear in the list, and
  says so on screen, because a silently disabled button is the same defect in a smaller
  costume.
- **`ChatMessage.stopped` was written and read nowhere.** A truncated answer rendered
  identically to a complete one — and the server commits the *full* answer under the same
  `query_id`, so a reload silently replaced the text with longer text and nothing explained
  why. Now an amber chip. Amber, not rose: the user pressed Stop and got what they asked
  for; this is a caveat about completeness, not an error.
- **A comment asserted `parallel_tool_calls` is disabled.** It is deliberately **not sent**,
  to dodge a 404 under `require_parameters` — the opposite meaning. The one-call-per-step
  assumption behind a React key was resting on it.

---

## 6. What is verified in production

| | |
|---|---|
| Both services | `live` — web immediately, api at 125 s |
| Streaming routes | present in the production OpenAPI |
| New shell | `agent-settings-open` present in the deployed bundle |
| CORS | preflight 200 from the web origin to the API |
| **Real Google OAuth** | production carries a genuine Google session — the dev-login shim 404s there, since it requires loopback *and* `ENVIRONMENT=development` — and the new workspace shell renders under it |
| Production redirect URI | registered: the authorize URL yields Google's account chooser, not `redirect_uri_mismatch` |

**On the OAuth item specifically:** CLAUDE.md says a real Google login cannot be automated,
and that is still true of the *sign-in gesture*. It is not true of the question that
mattered. `create_session` has exactly one caller (`_issue_session`), used by both the OAuth
callback and dev-login — the auth module's own docstring says dev-login "stubs the identity
assertion and NOTHING else". So the authenticated code path is provably identical, and the
remaining span was closed by observing production directly rather than by asking anyone to
click anything.

---

## 7. Where the code is

| Concern | File |
|---|---|
| Event names, phase constants, the frame envelope | [`app/rag/events.py`](../backend/app/rag/events.py) |
| The streaming routes and the queue that drains `emit` | [`app/api/stream.py`](../backend/app/api/stream.py) |
| `emit` threaded through the loop | [`app/rag/pipeline.py`](../backend/app/rag/pipeline.py) · [`app/rag/agent_loop.py`](../backend/app/rag/agent_loop.py) |
| `force`, all the way to the worker | [`app/api/documents.py`](../backend/app/api/documents.py) · [`app/rag/jobs.py`](../backend/app/rag/jobs.py) |
| Refuse, never raise | [`app/tools/sandbox.py`](../backend/app/tools/sandbox.py) |
| SSE parser, stream client | [`frontend/src/lib/api.ts`](../frontend/src/lib/api.ts) |
| The streamed turn, the settle guard | [`frontend/src/views/AgentChat.tsx`](../frontend/src/views/AgentChat.tsx) |
| The duplicate prompt | [`frontend/src/components/DuplicatePrompt.tsx`](../frontend/src/components/DuplicatePrompt.tsx) |
