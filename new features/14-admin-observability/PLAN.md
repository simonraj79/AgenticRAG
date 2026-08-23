# 14 — Admin observability — PLAN

Phase 2 of [build.md](../build.md). **This file owns every shared contract. The feature
files reference it and never restate it** — a contract stated twice drifts, and the copy
that drifted is never the one you are reading.

Audit: [00-AUDIT.md](00-AUDIT.md). Read it first; nothing below re-derives it.

---

## 1. What this change set is, in one paragraph

`admin@example.com` becomes an administrator and gets a console that reads across every
user: their agents, their conversations in full, their eval runs, and what all of it cost in
tokens and dollars. The spend half does not exist today in any form — two columns have sat
unwritten since the initial schema — and it is obtained by **recovering data OpenRouter is
already sending and LangChain is discarding**, not by adding a proxy, a price table or a
second database.

---

## 2. Architecture after the change

```
                        ┌──────────────────────────────────────────┐
   every model call ───▶│ build_chat_model()      llm.py:241       │
   (8 call sites,       │   ChatOpenAI(callbacks=[UsageMeter])     │
    none bypassing)     │   └─ MeteredChatOpenAI  ◀── NEW subclass │
                        └────────────────┬─────────────────────────┘
                                         │ on_llm_end
                                         ▼
                        ┌──────────────────────────────────────────┐
                        │ UsageMeter  (app/metering/meter.py) NEW  │
                        │   tokens  ← usage_metadata  (both paths) │
                        │   cost    ← llm_output   (ainvoke)       │
                        │            ← generation_info (astream,   │
                        │              via the subclass)           │
                        └────────────────┬─────────────────────────┘
                                         │ contextvar: who / which agent / why
                                         ▼
                        ┌──────────────────────────────────────────┐
                        │ api_usage   (EXISTS, 0 rows, + columns)  │
                        └────────────────┬─────────────────────────┘
                                         │
       ┌─────────────────────────────────┴────────────────────────┐
       ▼                                                          ▼
┌──────────────────────┐                            ┌──────────────────────────┐
│ /api/admin/*   NEW   │                            │ queries.prompt_tokens    │
│   AdminUser dep      │                            │ queries.completion_tokens│
│   (EXISTS, 0 callers)│                            │   ← denormalised SUM     │
│   + audit_log write  │                            └──────────────────────────┘
└──────────┬───────────┘
           ▼
┌──────────────────────┐
│ frontend/src/views/  │
│   Admin.tsx     NEW  │
└──────────────────────┘
```

**Everything marked EXISTS is why this change set is small.** The two boxes that are new are
the meter and the console; the authorisation seam, the usage table, the audit table and the
two token columns were all built already.

---

## 3. Shared contracts

### 3.1 Settings — `backend/app/config.py`

| Setting | Default | Meaning | Failure if wrong |
|---|---|---|---|
| `admin_emails` | `""` | Comma-separated. Read **only** by the promotion script/migration, never on a request path (00-AUDIT §3.6) | Empty: nobody is promoted; the console 403s for everyone, which is the safe direction |
| `metering_enabled` | `true` | Master switch for the meter | Off: no rows written, and **every existing code path is byte-identical to today** — this is what feature 01's regression case asserts |
| `metering_strict` | `false` | Re-raise instead of swallowing a metering error | On in harnesses, off in production. See §6 R1 |

`.env.example` gains all three with the reasoning inline, per repo convention.

### 3.2 Schema — ONE migration for the whole change set

Settled here, never in a feature file, so two features cannot race for the same
`down_revision`. Parent is `e5a17c3f9b62_object_storage_keys`.

**`api_usage` — additive only. The four existing columns keep their meaning.**

| Column | Type | Note |
|---|---|---|
| *(existing)* `id`, `created_at`, `user_id`, `provider`, `operation`, `units`, `estimated_cost` | | `provider` = `"openrouter"` / `"cohere"` / `"pinecone"`; `operation` = `"chat"` / `"embedding"` / `"rerank"` |
| `agent_id` | `uuid FK agents ON DELETE SET NULL` | **SET NULL, not CASCADE** — deleting an agent must not delete the record that it cost money. Contrast `query_chunks`, whose CASCADE is documented in CLAUDE.md as destroying eval evidence |
| `query_id` | `uuid FK queries ON DELETE SET NULL` | same reasoning |
| `call_kind` | `varchar(32)` | `generation` \| `rewrite` \| `route` \| `critic` \| `handout` \| `goldenset` \| `judge` \| `embedding` \| `rerank` |
| `model` | `varchar(128)` | the resolved slug |
| `served_provider` | `varchar(64)` | **which OpenRouter endpoint served it.** 00-AUDIT F3 |
| `prompt_tokens` / `completion_tokens` / `reasoning_tokens` / `cached_tokens` | `int` | nullable = not measured |
| `cost_usd` | `double` | **reported by OpenRouter.** Null when not reported |
| `cost_is_estimated` | `bool default false` | true only for embeddings/rerank (00-AUDIT §3.5) |
| `generation_id` | `varchar(128)` | OpenRouter's `gen-…`. Makes `GET /generation?id=` possible **later, offline** — the thing whose absence makes the existing 76 rows unbackfillable |
| `duration_ms` | `int` | |

Indexes: `(created_at)`, `(user_id, created_at)`, `(agent_id, created_at)`, `(query_id)`.

**`users`** — no schema change. The migration's data step sets `role = 'admin'` where
`lower(email)` is in the admin list, **matching every row with that email** including the
`dev|` one (00-AUDIT §3.6).

**`queries`** — no schema change. `prompt_tokens` / `completion_tokens` (`models.py:458-459`)
are finally written, as a denormalised SUM over that turn's `api_usage` rows.

> **The one thing to get right about that SUM:** `api_usage` is the source of truth and
> `queries.*_tokens` is a cache of it. They can disagree if a write fails. Feature 02 owns
> the rule — the admin console reads `api_usage`, never the cached columns, and the cached
> columns exist for the existing per-turn UI and for cheap sorting.

### 3.3 The metering contract — `app/metering/`

The **only** interface the rest of the app sees:

```python
# app/metering/context.py
@contextmanager
def meter_as(*, user_id, agent_id=None, query_id=None, call_kind: str): ...
```

A `ContextVar`, set by the caller, read by the meter. **Why a contextvar and not a
parameter:** `build_chat_model` has eight callers and the model object is often built once
and reused (`get_contextualizer`, `get_router`, `get_critic` are module-level singletons —
see `pipeline.py:562`, `route.py:268`, `selfcheck.py:275`). Binding identity at construction
time would attribute every later call to whoever happened to build the model first. The
contextvar binds it at **call** time, which is the only correct moment.

`ContextVar` is asyncio-task-local, so concurrent turns do not cross-contaminate — and
`app/eval/jobs.py` runs `RAGAS_MAX_CONCURRENCY=2`, so that is load-bearing, not theoretical.
Feature 01's acceptance case A5 asserts it under real concurrency.

**Unset context is not an error.** A call outside any `meter_as` block writes a row with
`user_id = NULL` and `call_kind` from the model's own default. Losing attribution must never
lose the *cost* — that is the same reasoning `ask.py:667` gives for a nullable `session_id`.

### 3.4 New trace event type

`events.py` gains **nothing.** Metering is not a step the agent took and putting it on the
trace would imply the model decided something. Spend appears in the admin console and on
`queries`, never in the user-facing trace.

*(This row exists because build.md §3 requires the plan to state new trace events. The
answer being "none" is the contract.)*

### 3.5 API surface — all under `AdminUser` (`app/auth/deps.py:83`, currently 0 callers)

| Route | Returns |
|---|---|
| `GET  /api/admin/overview` | totals: users, agents, queries, spend, **plus `measured_count` / `total_count` on every aggregate** |
| `GET  /api/admin/users` | every user, with agent/query/conversation counts and spend |
| `GET  /api/admin/users/{id}` | one user, their agents, their recent activity |
| `GET  /api/admin/agents` | every agent across all owners, with corpus size and spend |
| `GET  /api/admin/conversations` | paged, filterable by user/agent |
| `GET  /api/admin/conversations/{id}` | **full transcript** — writes an `audit_log` row |
| `GET  /api/admin/eval-runs` | every run, all agents, with scorecard summary |
| `GET  /api/admin/spend` | time series, grouped by `user` \| `agent` \| `model` \| `call_kind` \| `served_provider` |
| `GET  /api/admin/account` | OpenRouter `/credits` + `/key` — **the reconciliation number** |

**`/api/admin/account` is not decoration.** It is the only external check that our per-call
sum matches what OpenRouter actually billed. Feature 03 owns the comparison.

**Routing note, from CLAUDE.md's own rule.** Every other route in this app is nested under
an agent so that tenancy is structural — *"no request can be expressed without naming an
agent."* Admin routes are flat **by definition**: their purpose is to cross the tenancy
boundary. That inverts the invariant, so `AdminUser` is doing the entire job alone, and
these become the highest-risk lines in the codebase — the same standing that
`/api/conversations/{id}` already has. **Every admin route carries a comment saying so.**

### 3.6 Audit-log contract

```python
# app/api/admin.py
ADMIN_READ_ACTION = "app.api.admin.ADMIN_READ"
```

Imported, never retyped — the rule `app.rag.ingest.INGEST_FAILURE_ACTION` already sets.
Written on transcript reads only, not on aggregate reads: a row per dashboard render would
bury the reads that actually exposed someone's text.

### 3.7 Frontend contract

`Admin.tsx` mounts at `/admin`. The nav entry renders only when `me.role === "admin"`, so
`GET /api/auth/me` must return `role`. **That is a server-side check duplicated in the UI
for tidiness, never for security** — hiding a link is not access control, and the 403 from
`require_admin` is what actually holds.

Every number that can be unmeasured renders as `n/m measured`, never as a bare total
(00-AUDIT §5.3, and the `scored_count` trap in EVAL.md).

---

## 4. Build sequence — lowest layer first

| # | Feature | File | Depends on |
|---|---|---|---|
| 01 | The meter — subclass, callback, contextvar | [01-metering.md](01-metering.md) | nothing |
| 02 | Persistence — migration, `api_usage` writes, `queries` SUM | [02-persistence.md](02-persistence.md) | 01 |
| 03 | Admin API + promotion + reconciliation | [03-admin-api.md](03-admin-api.md) | 02 |
| 04 | Admin console | [04-admin-console.md](04-admin-console.md) | 03 |
| 05 | Embeddings + rerank metering | [05-embeddings-and-rerank.md](05-embeddings-and-rerank.md) | 02 |

**05 was not in the original plan and is not scope creep.** The audit concluded that
embedding cost would have to be ESTIMATED, and that conclusion was measured and found wrong
([00-AUDIT.md](00-AUDIT.md) §3.5): OpenRouter reports embedding cost exactly as it reports
chat cost, and the `openai` SDK preserves it. A plan that was right about the shape of the
gap and wrong about its cause is the plan doing its job -- the correction is recorded there
rather than quietly applied here.

01 ships useful on its own: with `metering_enabled=true` and no persistence, it logs. That
is deliberate — it means the riskiest piece (§6 R1) can be run in production for a day
before anything writes.

---

## 5. Definition of done

```bash
backend/.venv/Scripts/python.exe scripts/llm_check.py            # 30 existing + new
backend/.venv/Scripts/python.exe scripts/metering_check.py       # NEW, layer 1
backend/.venv/Scripts/python.exe scripts/admin_check.py --live   # NEW, executes every route
backend/.venv/Scripts/python.exe scripts/embed_check.py          # the two routes still agree
backend/.venv/Scripts/python.exe scripts/route_check.py --live   # provider recovery
backend/.venv/Scripts/python.exe scripts/refusal_check.py
backend/.venv/Scripts/python.exe scripts/ledger_check.py
backend/.venv/Scripts/python.exe scripts/agentic_check.py --setup && ... --run && ... --cleanup
cd frontend && npm test && npm run build
python scripts/ui_check.py                                       # global interpreter
```

**And the last step is not a command.** build.md's verification phase ends by opening the
page and reading one real answer; here it is: **ask one question, then open the console and
confirm that turn's cost is there and is not zero.** A green suite has been wrong six times
in this repo. Metering is exactly the shape that goes green while recording nothing —
`0.0` is a number, and a sum of zero rows is `0.0`.

## 6. Risk register

| # | Risk | The tell | Mitigation |
|---|---|---|---|
| **R1** | The meter raises inside a live turn and kills the answer | user-visible 500 on a working model | Every callback body wrapped; failure logs and continues. `metering_strict=true` in harnesses so a swallowed bug is still caught somewhere |
| **R2** | `generation_info` merge raises on a second usage frame (00-AUDIT §3.3) | `TypeError` mid-stream | Store as a single-element **list**; assert exactly one record |
| **R3** | Metering adds a parameter to the request → 404 / silent reroute | the four documented traps | The subclass changes **parsing only, never the request body**. `llm_check.py` case 31 asserts `extra_body` is byte-identical with the subclass in place |
| **R4** | Rows written with `user_id = NULL` because context was not set | spend that belongs to nobody | `metering_check.py` case: every call site in the eight is covered by a `meter_as` |
| **R5** | Everything green, nothing recorded | `sum = 0.0` reads as free, not as broken | Assert **cost > 0 and tokens > 0**, never "no exception". §5's manual step |
| **R6** | Admin console leaks to a non-admin | none until it matters | `AdminUser` on every route; a harness case asserting a plain user gets 403 on all of them |
| **R7** | The promotion migration matches one of the two simoraj rows | dev-login is not admin, console untestable locally | Migration asserts **≥2 rows updated** for a duplicated email, and the harness reads both back |

## 7. What this change set deliberately does NOT do

Straight from the audit, so the deleted work does not return.

- **No LiteLLM proxy, no second database.** 00-AUDIT §2.3. Reversal condition: enforcement
  (hard per-user budgets that reject before billing) or multi-vendor failover — neither is
  requested, and §3.2's schema supports adding enforcement without the proxy.
- **No local price table.** OpenRouter reports actual cost per call, per serving endpoint.
  Computing it would be *less* accurate, not more (00-AUDIT F2, §2.3).
- **No new authorisation primitive.** `require_admin` exists and is unchanged.
- **No per-user OpenRouter keys.** Attribution is ours by construction (00-AUDIT §3.4).
- **No budgets, quotas or rate limits.** Visibility only, as asked.
- **No backfill of the 76 existing queries.** The generation ids were never stored, so there
  is nothing to look up. They render as *not measured*.
- **Nothing model-decided**, so no [loop.md](../loop.md) session. An "AI summary of the
  admin dashboard" is T1 in advance — a tool nobody calls.

---

## 8. As built — where the plan was wrong

Written after verification, per [build.md](../build.md) §9. Two entries, and the second is
the one that generalises.

### 8.1 The audit was wrong about embedding cost, and said so in time

Recorded in [00-AUDIT.md](00-AUDIT.md) §3.5 and already visible in §4 above as the reason
feature 05 exists. The audit concluded embedding spend would have to be **estimated** from a
published rate. Measured, OpenRouter reports it exactly as it reports chat cost
(`usage.cost = 1.4e-06`, `provider = "Google AI Studio"`) and the `openai` SDK preserves it.
The cause of the mistake is the durable part: **the absence of a CALLBACK was mistaken for
the absence of a SEAM.** LangChain has no `on_llm_end` for embeddings, so there was nothing
to hook — but `self.client` sits one layer below the method and takes a wrapper.

A plan that was right about the shape of a gap and wrong about its cause is the plan working.
Cost of the error: one extra feature file, no rework.

### 8.2 R4's mitigation was planned and never built — and the risk it named had already fired

The register says:

> **R4** · Rows written with `user_id = NULL` because context was not set · **the tell:**
> spend that belongs to nobody · **mitigation:** `metering_check.py` case: every call site in
> the eight is covered by a `meter_as`

**That case was not written.** It was found missing by hand after the change set was
committed and pushed, by asking why `"goldenset"` appeared in `CALL_KINDS` and in no
`meter_as` call anywhere. `app/api/eval.py::_suggest_job` — the golden-set drafter's
BackgroundTask — reached `build_chat_model` with both ids in scope as parameters and opened
no scope at all.

**Three things about it are worth more than the fix.**

**The tell in the register was wrong, and wrong in the direction that hides the bug.** R4
predicts `user_id = NULL` — a row you can find by querying for it. What actually happens is
that `emit_record` returns `False` when no collection is active, so the record was **logged
and never written to `api_usage` at all**. That is R5's tell, not R4's: not spend belonging
to nobody, but *no row*, summing to `0.0`, reading as a quiet week. The two risks were
written as separate lines and are the same line for an unwrapped call site — and the fix
needs **both** context managers, `collect_usage()` as well as `meter_as()`, which is the half
a reader of R4 alone would omit.

**The ten cases that existed could not have caught it.** Every one of them opens its own
`meter_as` and then asserts attribution survived. So the harness only ever tested call sites
it wrote *itself*, never the one the application forgot. Stated generally, and this is the
entry that belongs in [CLAUDE.md](../../CLAUDE.md):

> **A harness cannot prove instrumentation is COMPLETE, only that the instrumentation it was
> handed works.** Coverage is a property of the application's call graph, so a case that
> asserts it has to read the application's source — not a shape the harness invented.

It is the seventh-green-suite failure one storey up. The commit message's own rule was *a
layer-1 harness cannot prove a query RUNS, only that it was WRITTEN*; this is the sibling —
it cannot prove a call site was WRAPPED, only that wrapping works.

**Fixed harness-first**, per [build.md](../build.md) §5, and the cases were watched failing
before a line of `app/` changed:

| Case | Asserts | Red before the fix |
|---|---|---|
| `11a` | every kind in `CALL_KINDS` is set by some call site | `declared but never set: ['goldenset']` |
| `11b` | no call site sets a kind `CALL_KINDS` does not declare | (green — the reverse direction, free) |
| `12` | no entry point reaches `build_chat_model` outside a `meter_as` | `unmetered: ['api/eval.py:949 _suggest_job()']` |

`unknown` is exempt from 11a **and the exemption is the contract**: it is the default for a
call made outside any scope, so a call site passing it explicitly would be asserting it does
not know who it is working for. It must stay unreachable.

Case 12 resolves callee names **bare, ignoring the module**, so two same-named functions
merge into one graph node. That can invent an edge and never lose one — it can therefore
report a false ALARM but not a false all-clear, which is the correct direction to be wrong in
for a safety property, and the comment in the harness says so.

### 8.3 Verification, as run

Migration `f6b28d4c1a73` applied before the merge (`alembic current` → `f6b28d4c1a73 (head)`),
per [build.md](../build.md) §8. `pywin32==312; sys_platform == "win32"` intact.

| Harness | Assertions |
|---|---|
| `metering_check.py` | **28** offline (was 25 — cases 11a/11b/12 are new) + 7 `--live` |
| `admin_check.py` | 18 offline + 19 `--live` |
| `llm_check.py` | 32, including case 31's byte-identical request body |
| `refusal_check.py` | 38 |
| `ledger_check.py` | 10 |

Case 12 reports **8 scoped functions cover the graph**, up from 7. The eighth is the one this
section is about.
