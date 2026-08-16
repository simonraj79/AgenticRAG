# The agent loop — a design pattern for model-decided features

> **Read this before adding a tool, a retry, or any feature where the model decides
> something rather than the code deciding it.** Referenced from
> [CLAUDE.md](../CLAUDE.md).
>
> This is the one file in `new features/` that is a **living reference** rather than a
> record of one change. The others describe a build that has shipped; this one describes how
> to do the next one.

---

## 1. What the pattern is

A bounded loop in which the model may act — call a tool, and see the result — instead of
producing one answer from one fixed pass. It exists in
[`app/rag/agent_loop.py`](../backend/app/rag/agent_loop.py), and everything below is
generalised from building it plus the Handouts job that reuses half of it.

**It is not "bind tools and iterate".** That part is twenty lines and works first time. The
content of this pattern is what happens when the model then declines to use them, and how to
keep the rest of the system honest while it does.

### When it applies

Use it when the answer needs something the request cannot know in advance:

- **a second lookup whose query depends on the first result** — "compare X with Y" needs two
  searches, and no rewrite of one question produces both
- **an artefact whose content depends on the answer** — a chart of figures that were not
  known until retrieval happened
- **a repair that depends on a failure** — code that raised, fixed by reading the traceback

### When it does not

- **The step is unconditional.** Retrieval runs on every real turn, so it stays *before* the
  loop. Making the model ask for the first search costs a round trip and buys nothing.
- **The decision is a threshold.** If a number decides it, write the branch. A tool call is
  ~1.6 s and a model round trip; an `if` is free.
- **You want it to happen every time.** That is a prompt or a code path, not a tool. A tool
  the model must always call is a tool you should call yourself.

---

## 2. Shape — how the loop composes with what exists

Four rules that keep an agentic feature from becoming a second, parallel pipeline.

### S1 — The tool closes over the tenant. It never takes it as an argument.

`SearchCorpusArgs` has exactly one field, `query`. No `namespace`, no `agent_id`, no `k`.

This is PRD §7 made structural rather than remembered: *"the namespace comes from the
session, never from the request body."* A model that can be prompt-injected by a retrieved
document must not have a parameter through which another tenant's corpus could be named.
The tool is built by a factory that closes over the `Agent` object
([`app/tools/registry.py`](../backend/app/tools/registry.py)), so there is no argument to
abuse.

`k` is excluded for a different reason and it is worth keeping: `retrieve_k` is an
operator-tuned parameter that Stage 3 measures. A model overwriting it per call makes
retrieval parameters unmeasurable.

### S2 — Bounded budget, and it always returns an answer

`max_tool_steps` (default 3, ceiling 8). When it runs out, the loop re-invokes with
`tool_choice="none"` and returns whatever comes back, recording
`stopped_reason="max_steps"`.

The number comes from the latency budget, not from taste: a search is ~1.6 s with reranking
against a persona turn of ~6.3 s. Three is room for a genuinely multi-part question and not
enough to explore. **The user always gets an answer**; what changes is whether the trace says
it was finished or forced.

### S3 — Accumulate as data; record at the boundary

`pipeline.py` never touches the database. `TraceRecorder` is constructed in
`ask.run_turn` and is unreachable from the loop. So the loop accumulates
`ToolInvocation` and `ToolArtifact` objects onto `AnswerResult`, and `run_turn` turns them
into `trace_events` and `handouts` rows inside the single existing transaction.

Keep this split. It is what lets a tool be added without either module learning the other's
concerns, and it is why a rolled-back turn cannot leave an orphaned artefact holding
megabytes.

### S4 — With the feature off, the output is byte-identical

Not "similar" — identical. `_tools_active(agent)` gates on both a global setting and a
per-agent column, `ContextLedger.seed` is inside the branch so the classic path does not even
pay for a dedupe pass, and `format_context` gained a keyword-only argument whose absence
provably reproduces the old rendering.

This is not tidiness. Every scorecard in [EVAL.md §10](../EVAL.md) was measured without
tools, and the migration backfilled every pre-existing agent to `false` for the same reason.
**A feature that cannot be turned off cannot be compared against.** Scenario S1 of
`agentic_check.py` asserts it on every run.

---

## 3. Getting the model to act — the part that is actually hard

### T1 — Assume the model will not call your tool

Budget for designing a trigger, not for wordsmithing guidance. Measured against
`google/gemma-4-31b-it` with one chunk of context, a two-part question, and `search_corpus`
bound:

| Configuration | Tool calls |
|---|---|
| `tool_choice="auto"`, full persona prompt | **0** — answered half, declared the rest missing |
| Bare prompt, no grounding rule at all | **0** |
| `"You MUST call search_corpus for any part not covered"` | **0** |
| `tool_choice="any"` / required | **0** |
| `tool_choice="search_corpus"` (named) | called it, correctly |

The model calls `run_python` readily, because *"draw me a chart"* is an instruction. Noticing
a gap and deciding to go looking is a **judgement**, and this model does not make it.

There is a structural reason, and it will apply to any well-grounded RAG system: every system
prompt here states the grounding rule before it establishes voice, which is exactly why the
product can be trusted when it says "I don't know". A model drilled to treat a missing fact
as a cue to **decline** will not spontaneously treat it as a cue to **search**. The two
instructions compete and the earlier, more forceful one wins. Weakening the grounding rule
trades a hallucination-free system for a tool-happy one — the wrong trade.

### T2 — Trigger on the absence of the outcome, not the presence of an error

**This is the most transferable idea in this document.** It arrived twice, independently, in
different parts of the build:

| Where | The error-shaped test | What it silently missed | The trigger that works |
|---|---|---|---|
| Agent loop | `detect_refusal` — did the turn decline? | The turn that answered *half* and gave up on the rest. Not a refusal, and precisely the turn that needed a search. | `detect_gap` — any admission of a gap, anywhere in the text |
| Handout retry | `SandboxResult.ok` — did the code crash? | Code that computes the chart correctly and forgets `savefig`. Exit 0, no file. Among the *most* recoverable failures there is. | "the expected artefact is absent" |
| **Layout** ([07](07-workspace-shell.md)) | console errors, failed requests, horizontal overflow — did the page break? | The agent header growing past the viewport, so `calc(100dvh - top)` went negative and the chat pane collapsed to **24px with 0px of thread**. Rendered perfectly. Threw nothing. | "is the thread taller than zero?" |
| **Forced upload** ([08](08-streaming-and-followups.md)) | the POST returned **202** and nothing raised | `force` was dropped at the background handoff, so the job re-deduplicated the upload and wrote `failed` a minute later — telling the user to do the thing they had just done | "is there a second copy in the corpus?" |

In all three the error-shaped test **passed while the thing we wanted had not happened**.
That is the failure mode to design against, and the question to ask is always *"did the goal
occur?"* rather than *"did an error occur?"*.

**And note where the third one came from.** Feature 07 is not a model-decided feature at all —
no tool, no retry, no detector; every branch is code reading a viewport width. It was written
against §6.1's gating question, which correctly said this pattern did not apply. T2 applied
anyway, and it was the only thing that found the bug. **The rest of this file is about the
model; T2 is about you.** Any check whose subject is "did an error occur" is a check that will
one day pass over a working system with the product missing from it.

### T3 — Strictness follows the cost of being wrong, in each direction

The same marker list in [`app/rag/refusal.py`](../backend/app/rag/refusal.py) feeds two
functions with deliberately different rigour:

- **`detect_refusal`** writes `queries.refused`, a Stage 3 success metric. A false positive
  **corrupts a measurement**, so it is position-sensitive: a caveat after a real answer must
  not be scored as a decline.
- **`detect_gap`** drives a retry. A false positive **costs one retrieval**; a false negative
  costs the entire feature. So it is position-insensitive, both tiers, anywhere in the text.

One source of truth for the phrases, two tests over it, and the asymmetry stated at each.
Generalise this: before writing a detector, ask what being wrong costs in *each* direction,
and set strictness from the answer rather than from instinct. They are rarely symmetric.

A corollary, learned the expensive way: **when a marker list has been wrong three times, stop
adding the string you just saw and add the shape it belongs to.** `"does not say"`,
`"does not cover"` and `"does not state"` were three separate discoveries of one gap. The
family is `does not <reporting verb>`, and adding the family costs nothing because the tier
is position-gated. The same bug then appeared a fourth time in `agentic_check.py`'s own
rate-limit detector, which matched `"too many requests"` and missed Cohere's
`TooManyRequestsError`.

### T4 — Force with a NAMED tool

`tool_choice="any"` is silently ignored on this route. Only naming the tool works.

That is worse than an error, and in the same family as the `max_completion_tokens` 404 in
[CLAUDE.md](../CLAUDE.md): a parameter accepted and not honoured. A dropped "required" is
indistinguishable from a model that considered the tools and declined.

### T5 — Do not widen a tool-bound request

`provider.require_parameters` is on, so a request routes only to providers advertising
**every** parameter it carries — and this one already sends `top_k` via `extra_body`.
Measured 2026-08-16 across the 19 endpoints serving `google/gemma-4-31b-it`: **14 advertise
both `tools` and `top_k`**. That is the headroom, and one more unadvertised parameter could
empty it.

Never pass `parallel_tool_calls`; `disabled_params={"parallel_tool_calls": None}` exists
precisely to keep it out. Check `mcp__openrouter__list-model-endpoints` **before** adding
anything to a tool-bound request, not after a 404.

---

## 4. Failure — a tool that fails is a message, not an exception

`_execute` catches everything and returns `ok=False` plus a `ToolMessage` the model reads.

**This is the single most valuable behaviour a code interpreter has**, and it is lost the
moment an exception escapes: a model that wrote bad Python reads its own traceback and fixes
it on the next step. Scenario S5 asserts exactly that round trip.

Consequences worth copying:

- **One failure never stops the loop.** Two *consecutive* steps in which every call failed
  do, via `MAX_CONSECUTIVE_FAILED_STEPS`, setting `stopped_reason="tool_error"` — nothing is
  converging and each further step costs a full round trip to reproduce the error.
- **Refusals from the static check are failures too, and good ones.** They name the offending
  module and list what is allowed, so the retry is usually correct. A refusal the model
  cannot act on wastes a step.
- **A configuration fault still propagates.** An OpenRouter 404 is not a turn failure;
  swallowing it would hide the exact failure CLAUDE.md documents three times.
- **`TOOL_CALL` is recorded even when the call failed**, and separately from `TOOL_ERROR`.
  The arguments the model chose are the decision worth keeping either way — a program that
  raised is the one a reader most wants to see.

---

## 5. Proof — a test that the feature is needed must make the feature necessary

Scenario S3 passed twice while proving nothing, and both ways are easy to repeat:

1. **The fixtures chunked to one chunk per file.** Two chunks, `retrieve_k=20` — one
   retrieval returned the entire corpus, so no question could ever require a second search.
2. **Then `retrieve_k=3` over seven chunks** still returned both topics, because both
   briefings use the word "storage". The model was right not to search.

Only when the scenario starved retrieval to `k=1` *itself* did the absence of a search become
a real finding — and it immediately was one: the model answered half and refused the rest,
which is what produced the whole trigger design.

This is the same trap [CLAUDE.md](../CLAUDE.md) records for context precision: **1.000 on a
single-chunk corpus is not excellent retrieval, it is retrieval that cannot fail.** A test
that cannot fail is worse than no test, because it reports success.

So: **a scenario owns the conditions it needs**, the way S1 owns `tools_enabled`, S6 owns
`max_tool_steps` and S3 owns `retrieve_k` — each restoring in a `finally`. Do not rely on the
fixture's defaults happening to be hostile enough.

And distinguish an environment failure from a defect. `agentic_check.py` prints `[rate]`
rather than `[FAIL]` for an upstream refusal and does not exit non-zero for it, because
Cohere's trial key allows ten calls a minute and the suite makes about twenty. A red row that
means "wait sixty seconds" sends its reader to debug working code — the same conflation as
`METRIC_TIMEOUT_S` doubling as a quota-retry ceiling.

---

## 6. Checklist — adding a third tool

> Starting a session for one of these? [loop-prompt.md](loop-prompt.md) is the prompt
> structure that front-loads steps 1, 2 and 5 below — the ones that are cheap to answer on
> paper and expensive to discover after the loop is built, which is how the first one went.

1. **Is it a tool?** Does it change what the agent can *find* or what it can *output*? If
   neither, it is a prompt change. If it must run every time, call it yourself.
2. **Write the args schema with the smallest possible surface.** Anything the server already
   knows — tenant, model, tuning — is closed over, never passed. Re-read S1.
3. **Add it to `build_tools`** in [`registry.py`](../backend/app/tools/registry.py). Order is
   stable and the cheaper tool goes first; some models weight the first entry more heavily.
4. **Return a string to the model and side-band the payload.** Both tools use
   `response_format="content_and_artifact"`: the string is what the model reads, the
   `ToolOutcome` becomes the trace payload. Never return bytes — one base64 PNG in a
   `ToolMessage` costs the whole context window.
5. **Decide the trigger before you write the guidance.** Assume auto-selection will not fire
   (T1). What deterministic signal, read off the model's own output, says this tool was
   needed and not used? Trigger on the missing outcome (T2), and force with a named call
   (T4).
6. **Make failure a message** (§4), and make the refusal text actionable.
7. **Extend `EVENT_TYPES` before the first write** — `TraceRecorder.record` raises on an
   unknown type, and that guard is the only gate. No migration: `event_type` is `String(32)`
   with no CHECK and `payload` is JSONB.
8. **Add the frontend entries** in `TracePanel`'s two `Record<string, string>` maps. New
   types degrade gracefully rather than crashing, so a missing entry is silent.
9. **Write the scenario that makes it necessary** (§5), plus a regression scenario asserting
   the classic path is unchanged.
10. **Do not widen the request** (T5).

---

## 7. Where the code is

| Concern | File |
|---|---|
| The loop, `ContextLedger`, `TOOL_GUIDANCE`, the gap trigger | [`app/rag/agent_loop.py`](../backend/app/rag/agent_loop.py) |
| Tool registry, `ToolContext`, `ToolArtifact`, `ToolOutcome` | [`app/tools/registry.py`](../backend/app/tools/registry.py) |
| The two tools | [`app/tools/corpus.py`](../backend/app/tools/corpus.py) · [`app/tools/interpreter.py`](../backend/app/tools/interpreter.py) |
| Sandbox, and **what it does not protect against** | [`app/tools/sandbox.py`](../backend/app/tools/sandbox.py) · [02-code-interpreter.md §5](02-code-interpreter.md) |
| The two detectors and their asymmetry | [`app/rag/refusal.py`](../backend/app/rag/refusal.py) |
| Branch into the loop; `AnswerResult` fields | [`app/rag/pipeline.py`](../backend/app/rag/pipeline.py) |
| Trace rows, handout rows, one commit | [`app/api/ask.py`](../backend/app/api/ask.py) |
| The same trigger idea in a background job | [`app/handouts/jobs.py`](../backend/app/handouts/jobs.py) |
| Scenarios S1–S12 | [`scripts/agentic_check.py`](../scripts/agentic_check.py) |
| T2 applied to layout, and the harness for it | [07-workspace-shell.md](07-workspace-shell.md) · [`scripts/ui_check.py`](../scripts/ui_check.py) |
| Streaming the loop without forking it — the `emit` seam | [08-streaming-and-followups.md](08-streaming-and-followups.md) · [`app/rag/events.py`](../backend/app/rag/events.py) · [`app/api/stream.py`](../backend/app/api/stream.py) |

---

## 8. The pattern in one paragraph

Run a bounded loop where the model may act, keep it composed with the existing pipeline
rather than beside it (tenant closed over, budget bounded, activity accumulated as data and
recorded at the boundary, off byte-identical). Then assume the model will not act, and give
it a deterministic trigger read off its own output — one that fires on **the absence of the
outcome you wanted**, not the presence of an error — forced with a **named** tool call. Let
tool failures come back as messages so the model can repair them. And prove the feature is
needed by making it necessary, because a scenario that passes without exercising the loop is
worse than no scenario at all.
