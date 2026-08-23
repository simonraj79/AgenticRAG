# 14 — Admin observability: audit

Phase 1 of [build.md](../build.md). **No plan below this line** — three deliverables only:
what exists at `file:line`, what is closed and by which property, and what the change
reduces to once the first two are subtracted.

Routing check ([build.md §1](../build.md)): a new API surface, a new frontend view and at
least one new table. **This is a change set.** The one model-decided part (none, as it
turns out — see §3.4) would have gone to [loop-prompt.md](../loop-prompt.md).

---

## 1. The question that was asked

> Add admin access for `admin@example.com` to analyse other users' agents, conversation
> logs, Ragas metrics and token spend. Analyse LiteLLM for token consumption — or would
> OpenRouter have this?

The LiteLLM half is answered first because it is the only part that could have changed the
architecture. It does not. §2.

---

## 2. LiteLLM vs OpenRouter — measured, then decided

### 2.1 What OpenRouter already returns, measured 2026-08-20

Every probe below ran against this repo's own `build_chat_model` and this account's key,
on `deepseek/deepseek-v4-flash-0731` — the shipped generation model, with `top_k` in
`extra_body`, `require_parameters: true` and `search_corpus` bound. The request shape is
therefore the one generation actually sends, not a synthetic one.

| Path | `usage_metadata` (tokens) | `cost` (USD) | serving provider | `gen-` id |
|---|---|---|---|---|
| `ainvoke` | **yes** | **yes** — `2.002e-05` | — | **yes** |
| `ainvoke`, tools bound | **yes** | **yes** | — | **yes** |
| `astream` accumulated | **yes** | **no** | no | no |
| `astream`, `stream_usage=True` | yes — **identical** | no | no | no |
| **raw SSE, no client library** | yes | **yes** — `9.1e-07` | **yes — `Relace`** | **yes** |

Five findings, in descending order of how much they change the design.

**F1 — Tokens are already free on both paths, and the note that says otherwise is stale.**
[`agent_loop.py:771-772`](../../backend/app/rag/agent_loop.py) records that
`ChatOpenAI(stream_usage=True)` "injects an unadvertised `stream_options` key that has
never been probed; it is never set here." That caution was right to write and is no longer
load-bearing, for a reason better than "it turned out to be safe":
`langchain_openai/chat_models/base.py:1417` reads `chunk.get("usage")` **unconditionally**.
`stream_usage` gates only whether `stream_options` is *sent* (base.py:1646, 1906), never
whether arriving usage is *parsed*. OpenRouter now returns usage on every response with no
parameter at all, and has **deprecated** `usage: {include: true}` and
`stream_options: {include_usage: true}` outright. So the risky parameter buys exactly
nothing — row 4 above is byte-identical to row 3 — and the safe path was always available.

> **The generalisable half, and it is a new shape for CLAUDE.md.** The four documented
> parameter traps are all about what happens when you *send* something. This is the fifth:
> a parameter can become **unnecessary** rather than dangerous, and nothing raises when it
> does. `stream_options` was never probed because probing it looked like a risk; the actual
> cost of not probing was two years of tokens going unrecorded on the streaming path. **A
> deferral written down as a risk needs a re-check date the way a measurement does.**

**F2 — Cost is on the wire on both paths. LangChain discards it when streaming.** The raw
SSE probe returns `cost`, `cost_details`, `provider` and the `gen-` id in the final frame;
`_create_usage_metadata` (base.py:4175) keeps only the OpenAI-standard fields and drops the
rest. This is *not* an OpenRouter limitation — it is a lossy normalisation in the client,
and that distinction is what makes a local price table unnecessary. See §3.3.

**F3 — The serving provider is in that same discarded frame.** `provider: "Relace"`.
CLAUDE.md currently records that `llm_check.py` "structurally cannot" tell a provider pin
that worked from one that was ignored, because an offline harness only sees what this repo
put in the request. That is still true of `llm_check.py` — and the answer it cannot compute
is being handed to us on every single turn and thrown away. Recovering it turns the
`provider.order` NO_GO from a one-off probe into a standing measurement.

**F4 — Account-level endpoints, and which key opens them.**

| Endpoint | Inference key | Returns |
|---|---|---|
| `/api/v1/credits` | **200** | `total_credits: 80`, `total_usage: 66.61` |
| `/api/v1/key` | **200** | `usage`, `usage_daily`, `usage_weekly`, `usage_monthly` for this key |
| `/api/v1/activity` | **403** | *"Only management keys can fetch activity for an account"* |
| `/api/v1/generation?id=` | **200, but it lags** | see F5 |
| `/analytics/query` | not probed | management key required (docs) |

**F5 — `/api/v1/generation` works, and the first probe here was wrong about why it didn't.**
An initial probe read three 404s at 2 s intervals and this document briefly recorded the
endpoint as broken. Re-checked against the harness that already depends on it:
`scripts/route_check.py --live` passes **3 of 3**, one call needing **4 lookup attempts**.
The endpoint is real; it propagates slower than a six-second budget. The correction matters
because the two failure modes have opposite responses — a dead endpoint means design around
it, a lagging one means poll — and the harness that already knew this was sitting in
`scripts/`.

**The conclusion survives, on the stronger reason.** Do not put this endpoint on the request
path: it costs an extra HTTP round trip *per model call*, with a lag long enough that
`route_check.py` had to build a retry loop for it. That is unacceptable inside a turn already
89% generation-bound, and F2 makes it unnecessary — the same numbers arrive in the response
we are already receiving.

> The generalisable half, and it is this repo's own rule turned on the audit: **three
> attempts is not a measurement, it is a guess with a number attached.** `route_check.py`
> polls because someone already learned this. An audit that probes an endpoint the
> repository already exercises should run *that* harness first.

### 2.2 What LiteLLM would add

Read: `D:\litellm-litellm_internal_staging\...`, v1.99.0, 575 proxy modules, 39 MB.
`schema.prisma` carries `LiteLLM_SpendLogs` (per-request `spend`, `prompt_tokens`,
`completion_tokens`, `cache_hit`, `request_tags`, `session_id`, `end_user`) and six
`LiteLLM_Daily*Spend` rollups keyed by user / team / org / tag / agent / tool. It is a
genuinely good spend-tracking schema and it is roughly the schema §4 proposes, which is a
point in LiteLLM's favour and is worth saying plainly.

What it costs here:

| | |
|---|---|
| Deployment | a second container **plus its own Postgres 16** (`docker-compose.yml`), i.e. a second Render web service and a second database — and CLAUDE.md records the lowest paid tier for a new Render database is `basic_256mb`, free Postgres being a 30-day countdown |
| Price table | ships a 1.7 MB `model_prices_and_context_window.json` to *estimate* cost — **which F2 makes redundant**, and which would disagree with OpenRouter's actual per-endpoint billing (StreamLake $0.06426/M vs Baidu $0.0644/M vs Decart $0.0657/M are all the same model) |
| Attribution | attributes spend to *LiteLLM's* users/keys/teams. Groundwork's identities are Google `sub` values in its own `users` table. Bridging them means minting a virtual key per Groundwork user and threading it through — identity duplicated across two systems, with the join being the thing that breaks |
| Latency | a third network hop in front of a turn already measured at 89% generation |

### 2.3 The decision, and the reason is not cost

**NO_GO on LiteLLM, and the disqualifying argument is none of the four rows above.**

LiteLLM does not forward this repo's request body — it **builds its own** from its own
provider adapter. CLAUDE.md's OpenRouter section is, almost in its entirety, hard-won
knowledge about the exact bytes that leave this process:

- `max_tokens` must travel in `extra_body`, because `ChatOpenAI` renames it to
  `max_completion_tokens`, which OpenRouter honours and does not advertise → 404 at routing
- `disabled_params={"parallel_tool_calls": None}`, because that parameter alone collapses
  routing from 28 endpoints to **1**
- `top_k` dropped for `_NO_TOP_K_PREFIXES`, because sending it costs 9 of 28 endpoints
  silently — no error, a different bill
- `reasoning: {"enabled": false}` withheld for `_REASONING_ALWAYS_ON_PREFIXES` → hard 400
- `encoding_format: "float"`, because `openai-python` injects `base64` unasked → hard 400
  on **every** embedding call
- `provider.require_parameters: true`, asserted by exact dict equality in `llm_check.py`
  cases 13/14 precisely so a second assignment cannot silently delete it

**Inserting LiteLLM re-opens every one of those, and the ones that fail silently fail
silently again.** Two of the five traps produce no error — a narrowed route and a wrong
`dimensions` default — so the tell would be a bill that is quietly not the predicted one and
retrieval that is quietly worse. That is the exact failure class CLAUDE.md was written to
close, and it would be re-opened to obtain a number OpenRouter is already sending for free.

Consistent with the standing preference to keep the tutorial stack and swap components only
when deployment truly forces it. It does not force it here.

**Reversal condition** — state it so this does not get re-litigated from memory: revisit
LiteLLM if Groundwork ever needs *enforcement* rather than *visibility* — hard per-user
budgets that must reject a request before it is billed, or multi-provider failover across
vendors OpenRouter does not front. Neither is in this request, and §4 is deliberately built
so that adding enforcement later does not require the proxy either.

---

## 3. What already exists — the audit proper

### 3.1 Shipped, unused, and correct

**The admin authorisation seam is already built and is wired to nothing.**

| What | Where | State |
|---|---|---|
| `users.role`, default `"user"` | `backend/app/db/models.py:70` | column exists, **15/15 rows are `user`** |
| `require_admin` — 403 not 401, with the reason written down | `backend/app/auth/deps.py:69-80` | **zero callers** |
| `AdminUser` annotated dependency | `backend/app/auth/deps.py:83` | **zero callers** |
| re-exported for route modules | `backend/app/api/deps.py:38,43` | **zero callers** |

`require_admin`'s docstring already names its intended consumers — *"the Evaluate view and
the marketplace oversight list"*. This feature is the thing that was anticipated. **No new
authorisation primitive is needed, and writing one would be the mistake.**

**Two token columns exist and have never been written.**

| Column | Where | Rows populated |
|---|---|---|
| `queries.prompt_tokens` | `models.py:458` | **0 of 76** |
| `queries.completion_tokens` | `models.py:459` | **0 of 76** |
| `queries.model_used` | `models.py:456` | written, `ask.py:1310` |
| `queries.latency_ms` | `models.py:457` | written, `ask.py:1311` |

`ask.py:1309-1312` sets `answer`, `model_used`, `latency_ms` and `refused` on the same
object, four lines apart from two columns it does not set. The schema was right; the write
was never added.

**`api_usage` exists, has never been written, and its columns are already the right ones.**
`models.py:696-708` — `user_id`, `provider`, `operation`, `units`, `estimated_cost`.
**0 rows.** Note `estimated_cost` is already named honestly, which matters in §3.3.

**`audit_log` exists and is written on exactly one path** — ingest failures, under
`app.rag.ingest.INGEST_FAILURE_ACTION`. It is a general-purpose table already carrying a
`metadata` JSONB.

### 3.2 The single chokepoint, and it is the whole feature

`build_chat_model` at `backend/app/rag/llm.py:241` is the **only** place a chat model is
constructed in this project. Eight call sites reach it and not one bypasses it:

```
app/rag/pipeline.py:443    get_chat_model  -> generation
app/rag/pipeline.py:476                     -> the question rewriter
app/rag/route.py:210                        -> specialist routing
app/rag/selfcheck.py:237                    -> the critic
app/handouts/jobs.py:208                    -> deck / handout code generation
app/eval/generate.py:651                    -> the golden-set drafter
app/eval/ragas_runner.py:166                -> the Ragas judge
app/rag/agent_loop.py                       -> via pipeline.get_chat_model
```

This is the same property CLAUDE.md already relies on for the retriever — *"constructed in
exactly one place… do not call `similarity_search()` anywhere else"* — and it is what makes
the whole of §4 small. `ChatOpenAI` accepts `callbacks`, and `on_llm_end` fires on **both**
`ainvoke` and `astream` (measured — §3.3). **One handler registered in one factory meters
every model call in the project, including the two — the judge and the golden-set drafter —
that no per-call-site instrumentation would have remembered to cover.**

### 3.3 What the callback sees, measured

| | `llm_output` keys | `cost` | tokens |
|---|---|---|---|
| `ainvoke` | `id`, `model_name`, `model_provider`, `system_fingerprint`, `token_usage` | `2.002e-05` | 282 / 2 |
| `astream` | **`[]`** | **`None`** | **via `usage_metadata`: 282 / 2** |

So the handler gets tokens unconditionally and cost on the non-streamed calls — which is
six of the eight call sites above, including the Ragas judge and the handout coder. Only the
streamed generation turn loses cost, and that is the largest single cost item.

**The recovery seam is a subclass, not a monkey-patch.**
`ChatOpenAI._convert_chunk_to_generation_chunk(self, chunk, default_chunk_class,
base_generation_info)` (base.py:1408) is the method that reads the usage frame. Its
`len(choices) == 0` branch is the usage-only chunk, and it already builds a
`ChatGenerationChunk` there — attaching the raw `chunk["usage"]` and `chunk["provider"]` to
`generation_info` is ~15 lines and touches nothing else.

**One trap, found in the same probes, that this design must not walk into.** Streaming
merges `generation_info` across chunks, and the merge is *not* idempotent:

```
generation_info  {"finish_reason": "stopstop",
                  "model_name": "deepseek/…-0731deepseek/…-0731"}
```

Strings **concatenate**. `langchain_core`'s `merge_dicts` concatenates strings, concatenates
lists, recurses into dicts — and for two unequal scalars it **raises**. So a bare
`generation_info["cost"] = 9.1e-07` is safe only while exactly one usage frame ever
arrives; a provider that emitted two different ones would raise `TypeError` **mid-stream, on
a live turn**. Store it as a single-element **list**, which concatenates instead of raising.

> This is [loop.md](../loop.md) T2 in a new place: the error-shaped test ("did the
> subclass throw?") passes on every model measured today and says nothing about the model
> that arrives next. The assertion has to be *"is there exactly one usage record, and does
> its cost parse as a float?"*

Corollary worth carrying into CLAUDE.md's frontend/gotchas habit: **`model_name` read off
an accumulated stream chunk is doubled and must not be trusted.** `queries.model_used` is
written from `result.model` rather than from the message, so nothing is broken today — but
anything that starts reading it off the message will silently record a nonsense slug.

### 3.4 What is architecturally closed

Three things that look like part of this feature and are not. These are the entries that
stop the idea returning in six weeks.

**Per-user attribution cannot come from OpenRouter, at any key class, ever.** OpenRouter
sees one API key for the whole deployment. `/api/v1/key` reports `usage_monthly: 0.8335`
for *the application*, and `/analytics/query` groups by `api_key_id`, `model` and date.
There is no dimension in which "which Groundwork user asked this" is expressible, because
that fact never leaves this process. **Attribution is therefore ours by construction, not
by preference** — which is also precisely why LiteLLM's per-user spend tables do not solve
it for free either (§2.2, row 3). Any design that hopes to read per-user spend off a vendor
dashboard is closed.

**Cost is an estimate on the streamed path only if we compute it, and we do not have to.**
The obvious plan — ship a price table and multiply — is what LiteLLM does and it is
*strictly worse here*, because OpenRouter's actual charge depends on which of 28 endpoints
served the turn (§2.3), and F3 shows we are being told which one and discarding it. Recover
the reported cost; never multiply. `api_usage.estimated_cost` keeps its name for the one
place estimation is unavoidable — §3.5.

**No part of this feature is model-decided, so it does not go through
[loop.md](../loop.md).** Metering is arithmetic over a response the model already returned.
The temptation to add "an agent that summarises the admin dashboard" is
[loop.md](../loop.md) T1 in advance — a tool nobody calls — and is out of scope.

### 3.5 The coverage gap — and the CORRECTION that closed most of it

Chat is not all the spend.

| Cost centre | Reports cost? | Reachable through the chat callback? | Outcome |
|---|---|---|---|
| Chat — all 8 call sites | yes | **yes**, `on_llm_end` | reported |
| **Embeddings** | **yes** | no | **reported** — see below |
| Cohere rerank | **no** — reports `search_units` | no | **units measured**, cost opt-in |
| Pinecone | per-namespace storage | no | out of scope |

**This section originally concluded that embedding cost would have to be estimated, and
that was wrong.** The reasoning was that `OpenAIEmbeddings` exposes no usage attribute and
LangChain has no `on_llm_end` for embeddings, so a *count* would have to stand in for a
*report*. The first half is true; the conclusion does not follow. Measured 2026-08-20
through this repo's own `get_embeddings()`:

```
usage     Usage(prompt_tokens=7, total_tokens=7, cost=1.4e-06, cost_details={...})
provider  "Google AI Studio"
id        "gen-emb-1787199865-2n8YI6ISF4fmdg3MBDvM"
```

All three survive into the `openai` SDK's response object — `cost` on
`usage.model_extra`, `provider` and `id` on the response's. **Nothing discards them.** What
was missing was a reader, not the data, and the absence of a *callback* was mistaken for
the absence of a *seam*.

**The seam is the client, not the method**, and that distinction is the reusable part.
Overriding `embed_documents` would have worked and would have covered only the branch this
repo takes today: `check_embedding_ctx_length=False` routes through a plain batching loop
while `True` routes through `_get_len_safe_embeddings`. Both call `self.client.create(...)`.
Wrapping the client meters both, so flipping a flag CLAUDE.md documents as one 400 away
from reconsideration cannot silently un-meter the system. `OpenAIEmbeddings` sets
`model_config extra="forbid"`, so the assignment is verified rather than assumed —
`metering_check.py` case 9e.

**Rerank is the one place a number genuinely cannot be read**, and it is handled by
refusing to invent one. Cohere returns `meta.billed_units.search_units` (measured: `1.0`)
and no cost. Units are therefore a measurement and go in `api_usage.units`; a dollar figure
would be arithmetic over a published price, so it is opt-in via
`cohere_search_unit_usd` (default `0.0` = do not estimate) and lands in `estimated_cost`,
never in `cost_usd`. The default is off because a hardcoded price is a number nobody
re-checks — and this repository already has that exact failure on this exact provider, a
Cohere key silently downgraded to a trial tier and discovered only under load.

**The honest position, restated:** chat and embedding cost are *reported*; rerank is
*counted*; and the console shows `priced_calls` beside `calls` so the remaining gap is
visible rather than absorbed into a total.

### 3.6 The identity finding, and it changes the feature's first line

**`admin@example.com` is two user rows, and both carry real work.**

```
email                 google_sub                      agents  queries  last_login
admin@example.com     dev|admin@example.com                1       10  2026-08-16
admin@example.com     10954xxxxxxxxxxx16065                3       62  2026-08-20
```

This is `POST /api/auth/dev-login` working exactly as CLAUDE.md documents it — *"it stores
`google_sub = "dev|<email>"`, so a dev identity can never collide with a real Google `sub`
— signing in for real creates a separate user row."* The property that makes the dev shim
safe is the property that makes "make admin@example.com the admin" **ambiguous**.
`second.user@example.com` is doubled the same way; 2 of 15 rows are affected.

Both readings are wrong on their own:

- **Grant by `sub`, real identity only** → the dev-login route is not admin, so the admin UI
  cannot be developed or `ui_check.py`-tested locally. That is the same reason
  `dev-login` exists at all: *"a real Google login cannot be automated — it needs a human at
  a consent screen."* Building an admin console that only a human at a consent screen can
  reach re-creates the exact hole the shim was written to fill.
- **Grant by email at request time** → contradicts CLAUDE.md's hard rule, *"key on `sub`,
  never `email`. Google reassigns emails within a Workspace domain"*, and puts a
  string-compare against an env var on the authorisation path.

**Resolution: neither. Promote by email once, authorise by `role` forever.** A migration
sets `role = 'admin'` on **every** row whose email is in the admin list — both simoraj rows
— and `require_admin` (`deps.py:69`) is left byte-for-byte unchanged, still reading
`user.role` off a row that was still found by `sub`. Email is used exactly once, at
promotion, where a reassigned-email risk is a human-reviewable data change rather than a
live authorisation decision. Dev-login lands on an admin row, so the console is testable.

The residual, stated: a future Workspace email reassignment would leave a stale `role`.
That is a promotion-time concern with a promotion-time answer (re-run the grant), not a
reason to move email onto the request path.

---

## 4. What the change reduces to

Subtracting §3.1 (the auth seam and both token columns already exist), §3.2 (one factory
meters everything), §3.3 (cost is recoverable, no price table) and §3.4 (three closed
directions), the change set is:

1. **A metering callback** registered inside `build_chat_model` — one file, plus the ~15-line
   `ChatOpenAI` subclass that stops LangChain discarding `cost` / `provider` on the streamed
   path. Covers all eight call sites at once.
2. **Persist it** — write the two columns that already exist (`queries.prompt_tokens`,
   `queries.completion_tokens`); add cost, provider and a call-kind discriminator. Whether
   that is three columns on `queries` or rows in the empty `api_usage` is the one real
   schema decision, and it turns on whether a *turn* or a *call* is the unit — a single turn
   makes 1–3 generation calls plus a rewrite plus a route plus possibly a critic.
3. **Admin read routes** under `AdminUser`, which already exists and has no callers.
4. **An admin view** — users, agents, conversations, eval runs, spend.
5. **The promotion migration** of §3.6, plus a harness case asserting both rows flipped.

Item 1 is the only part with a measurement risk, and §3.3 names it: assert *one usage
record whose cost parses*, never *"nothing threw"*.

**What this audit deleted:** the LiteLLM proxy and its second database, a local price
table, a per-user virtual-key bridge, a new authorisation primitive, and the two token
columns that would otherwise have been added a second time.

---

## 5. Open questions — answered 2026-08-20, carried into `PLAN.md`

1. **Turn or call as the metering unit** (§4.2). **Decided: per CALL.** One row per model
   call, tagged with its kind; turn totals are a SUM. The deciding argument is not
   granularity but coverage — the Ragas judge and the golden-set drafter belong to no
   `queries` row at all, so a turn-shaped unit has nowhere to put them and eval spend would
   have gone unmetered while looking complete.
2. **Does an admin read conversation *bodies*, or only metadata?** **Decided: full
   transcripts, and the read is itself audited.** Every admin read of another user's content
   writes an `audit_log` row. The table already exists (`models.py:711`) and is already
   written on exactly one path, so this is a second caller, not a new mechanism.
3. **Backfill or not.** 76 queries have no token data and it cannot be reconstructed — not
   because the endpoint is unavailable (F5: it works) but because **the generation ids were
   never stored**, so there is nothing to look up. The admin view must therefore distinguish
   "zero spend" from "not measured", which is the same `scored_count` trap EVAL.md already
   documents. **Decided: show as not measured, with the measured-row denominator printed
   next to every total.**
4. **Is `queries.refused` + Ragas the whole of "quality" for the admin view**, or does the
   self-check verdict belong there too?
