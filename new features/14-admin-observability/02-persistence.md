# 02 — Persistence

Contracts consumed: [PLAN.md](PLAN.md) §3.2 schema and the single migration, §3.3 the
metering contract. **Nothing here restates them.**

## What the user gets

Still nothing visible. What a turn cost stops being a log line and becomes a row that can
be grouped, summed and compared.

## Technical detail

**The buffer, not a write-per-call.** `app/metering/context.collect_usage()` opens a
per-turn list; the sink appends to it; `run_turn` drains it once, immediately before the
single commit it already makes. Two reasons, both from CLAUDE.md rather than taste:

* A callback must not borrow a session it did not open. The meter fires part-way through a
  request whose session is mid-transaction and will flush again — writing there interleaves
  metering INSERTs with the turn's own writes.
* Opening a session per model call means up to **six connections per question** (1–3
  generation steps, plus rewrite, route, critic) on a Render starter plan running one
  uvicorn worker.

Draining inside the existing commit also makes `queries.prompt_tokens` a trustworthy cache
of the `api_usage` SUM rather than a second source of truth: either both land or neither
does.

**`run_turn` is wrapped, not edited.** A thin `run_turn` enters `collect_usage()` and
`meter_as(...)` and delegates to `_run_turn`, which is otherwise unchanged. The engine has
several returns and a `finally` that must not grow a second job — and a wrapper keeps "the
classic path is byte-identical" structural instead of careful.

**`query_id` is stamped at persist time**, not carried in the scope: the row does not exist
until it is flushed, and the history-aware rewrite can run either side of that. Every record
collected during one turn belongs to that turn by construction.

**Nested kinds.** `pipeline.contextualize_question`, `route.choose_specialist` and
`selfcheck` each open `meter_as(call_kind=...)`, inheriting the user and agent from the
turn's scope. That is the whole reason `meter_as` merges rather than replaces.

**Reported cost and estimated cost live in different columns** (`cost_usd` vs the original
`estimated_cost`) and are never summed together. Adding a measurement to a guess produces a
number that is neither.

## Acceptance criteria

| id | Harness | Asserts |
|---|---|---|
| **B1** | `scripts/admin_check.py --live` | `GET /api/admin/overview` returns `coverage` with `measured <= total` on real data |
| **B2** | `scripts/metering_check.py --live` L6 | Attribution set by `meter_as` survives into the record |
| **B3** | manual, `PLAN.md` §5 | One real turn writes ≥1 `api_usage` row whose summed `cost_usd` is **> 0**, and `queries.prompt_tokens` equals the SUM of those rows |
| **B4** | `scripts/admin_check.py` case 5c | No aggregate ever sums `estimated_cost` into a reported total |

**B3 as measured, 2026-08-20** — one turn on a 2-document agent:

```
rewrite     google/gemma-4-31b-it            CoreWeave    493/12    $5.338e-05
generation  deepseek/deepseek-v4-flash-0731  Relace      1823/396   $0.00018305
generation  deepseek/deepseek-v4-flash-0731  Relace      2706/401   $0.00024556
queries.prompt_tokens = 5022  ==  SUM(api_usage.prompt_tokens) = 5022
turn cost = $0.00048199
```

Note what that table shows and no log line would have: the **rewrite runs on a different
model from generation**, and the agent loop made **two** generation calls for one turn.

## What must keep working

- **A metering failure must not cost the user their answer.** `persist_quietly` runs inside
  the caller's transaction, so an uncaught raise would roll back the *answer*. That is the
  only reason it exists rather than the caller calling `persist`.
- `queries.prompt_tokens` stays **NULL** for the 76 pre-metering turns. Zero would mean free.
- Every FK on `api_usage` is `ON DELETE SET NULL`. Deleting an agent must not delete the
  record that it cost money — contrast `query_chunks`, whose CASCADE CLAUDE.md records as
  silently destroying eval evidence.
