# 02 — A failed turn's metering

Contracts consumed: [PLAN.md](PLAN.md) §3.2 (no migration), §3.3 (an unattributed row carries
`query_id = NULL`, and what does *not* move in the console), §3.5 (what "the turn already
persisted" is allowed to mean), §3.6 (harness contracts), §3.7 (the id ledger).
**Nothing here restates them.** Where this file and PLAN.md disagree, PLAN.md wins — except in
§6 below, which exists to record two places where PLAN.md disagrees with *itself* and one
where its case classification is wrong.

Build order: first of the three ([PLAN.md](PLAN.md) §4). Depends on nothing. Feature 01
depends on this one.

Not model-decided — no tool, no retry over model output, no detector. [loop.md](../loop.md) is
cited throughout as **T2** only: *trigger on the absence of the outcome you wanted, never on
the presence of an error*. This defect is a pure instance of T2 and nothing in it needs a
trigger designed.

---

## What the user gets

Nothing visible in the product. What an **operator** gets is the half of the bill that has
been invisible since metering shipped: the money a turn spent before it died.

A turn that raises has already paid OpenRouter for a rewrite, one to three embeddings, one to
three reranks and possibly a generation call or two. Today every one of those records is
discarded. The spend is not mis-attributed and it is not sitting in a row nobody joined to —
**it is absent**, and so is the log line that would have named it, because
`LoggingSink.record` returns the moment `emit_record` accepts the record into the turn's
bucket (`app/metering/meter.py:118-131`, `app/metering/context.py:186-196`). A failed turn
costs real money and leaves no trace of any kind.

The failure this makes visible is exactly the one an operator most needs to see, and CLAUDE.md
records this project hitting it three times: an OpenRouter `404 No endpoints found` storm,
where **every turn fails and the console reads as a quiet week**.

---

## Technical detail

### 1. What is true today, at `file:line`

> **Every line number in this section is the file BEFORE the fix**, and the fix moved all of
> them — the persist, the commit and the flush each shifted by hundreds of lines. Kept as
> written, because an audit is a record of what was found; the post-fix anchors are function
> names, in the harness and in §8. A citation the next edit invalidates reads as precise, which
> is worse than none, so nothing new in this file cites a line in `ask.py`.

`run_turn` (`ask.py:591`) is a thin wrapper. Lines 620-622 open

```python
with collect_usage() as records, meter_as(
    user_id=user.id, agent_id=agent.id, call_kind="generation"
):
```

and lines 623-633 are a bare `return await _run_turn(..., usage_records=records)`. There is no
`try`, no `except` and no `finally` anywhere in the wrapper. `_run_turn` (`ask.py:636-1425`)
holds exactly three `try` blocks and **none of them covers metering**: a storage upload at
`1149-1161`, the commit at `1409-1414` (whose `except` deletes staged R2 keys and re-raises),
and that is all. `grep -n "finally" backend/app/api/ask.py` returns one hit and it is inside a
docstring (`:605`).

So the buffer is drained in exactly one place — `ask.py:1395-1397` —

```python
query.prompt_tokens, query.completion_tokens = metering_store.persist_quietly(
    db, usage_records or [], query_id=query.id
)
```

— and `persist_quietly` only `db.add()`s rows (`store.py:109`); the commit that makes them real
is fifteen lines later at `ask.py:1410`. **Any raise between the `flush()` at `ask.py:727` and
that commit discards the whole buffer**, and a raise *at* the commit discards rows that were
already added.

### 2. The shape, and where it comes from

Four sites in this repository already persist a buffer from a `finally`, and all four are
byte-identical in structure:

| Site | Line |
|---|---|
| `app/rag/jobs.py` | 230-240 |
| `app/eval/jobs.py` | 299-309 |
| `app/handouts/jobs.py` | 947-983 |
| `app/api/eval.py` (`_suggest_job`) | 1086-1098 |

```python
finally:
    if usage_records:
        try:
            async with SessionLocal() as meter_db:
                metering_store.persist(meter_db, usage_records)
                await meter_db.commit()
        except Exception:      # accounting never breaks the job
            log.warning("could not persist ... usage", exc_info=True)
```

**The SHAPE transfers. The connection-cost reasoning does not**, and conflating the two is
PLAN §6 R3. All four precedents close their own `async with SessionLocal() as db:` *before*
their `finally` runs, so their second session is never concurrent with their first.
`run_turn`'s callers hold the turn session open across the `finally` — the FastAPI dependency
in `ask.py`/`conversations.py`, and `stream.py:200`'s own `async with`. Against
`pool_size=5, max_overflow=5` on a single uvicorn worker (`db/session.py:11-18`), with
`stream.py:331-334` already naming that cap as reachable, and with the 404 storm above being
precisely the moment every turn demands the extra connection at once.

No harness can prove a pool property. **The mitigation is therefore the assertion, written
into the code:** the `finally` guards on `if records:` and **awaits nothing else**, so the
second connection is opened only when there is something to write and is held for one insert
and one commit. That sentence belongs in the comment at the call site, with the reason —
otherwise the next reader copies the fourth precedent and inherits a justification that does
not apply.

### 3. The guard, and why it is the whole feature

[PLAN.md](PLAN.md) §3.5 settles what the guard may key on. Restated here only as the
consequence, never the contract: a `finally` that fires on the **success** path too writes
every normal turn's spend twice, and this repository has already had that bug once —
`app/metering/context.py:130-160` records the eval-job / `run_turn` double-count, roughly
double the real spend, **no error anywhere and two plausible totals**.

The property is one sentence: **the seam writes the buffer if and only if the buffer is not
already durable.** "Already durable" is a conjunction of two facts, and dropping either one is
silent in a different direction:

| Fact | Dropped, it fails how |
|---|---|
| The turn's single `await db.commit()` **returned** | Key on `persist_quietly` having run (`ask.py:1395`) and the seam declines to write on a **failing commit** — the one failure it was built for |
| The persist actually **added rows** | `persist_quietly` returns `(None, None)` both when there was nothing to write (`store.py:118-125`) and when the write raised and was swallowed (`store.py:104-107`). A swallowed persist followed by a good commit would mark the buffer durable having written nothing — **reproducing the original hole through the fix** |

So the signal is a small mutable **receipt** the wrapper creates and `_run_turn` writes, with
exactly two fields and exactly two writers:

```python
@dataclass
class _TurnReceipt:
    rows_written: int | None = None   # set where `_run_turn` persists; None means it wrote nothing
    committed: bool = False           # set on the line IMMEDIATELY AFTER that function's one commit returns

    @property
    def durable(self) -> bool:
        return self.committed and bool(self.rows_written)
```

and the seam is a named function in `ask.py`, so that it can be executed by a harness without
a database, a model or a turn:

```python
async def _persist_orphaned_usage(records, receipt) -> int:
    """The failed turn's spend, written from a SECOND session. Returns rows written."""
```

**The seam carries the guard, not only the call site.** It returns `0` *without opening a
session* when there is nothing to write or the buffer is already durable. Case 14c asserts
`sessions_opened == 0` on the durable path as well as `rows == 0`, and the two are different
claims: a seam that opens a connection and adds nothing still pays R3's cost on **every
successful turn**, which is the one path where the extra connection is never justified.

**FOUR receipt states, not three and not two.** 14a/14b/14c drive the first three; **14d**
drives the fourth, which an earlier version of this table omitted while calling itself
exhaustive:

| `committed` | `rows_written` | Means | Seam writes |
|---|---|---|---|
| `False` | `None` | the turn raised at or before its persist | **N rows** |
| `True` | `N` | the ordinary successful turn | **zero** |
| `True` | `None` | the persist raised and was swallowed, then the commit succeeded | **N rows** |
| `False` | `N` | the persist added rows and then the **commit RAISED**, rolling them back | **N rows** |

The third row is the one no plain boolean can express, and it is why the conjunction exists.
The fourth is §3.5's *headline* case — the exact failure the feature was built for — and it is
the only one of the four that goes red when `durable` drops `committed`: mutate it to
`bool(self.rows_written)` and 14c stays green on all three of its states while 14d fails. Two
halves of one conjunction, two different cases; see §7.2.

### 4. `query_id` is omitted, and the omission is free

Per [PLAN.md](PLAN.md) §3.3. The mechanism, because the harness cites it: no `meter_as` call
site in the repository passes `query_id` (nine sites, verified), so `UsageRecord.query_id` is
`None` throughout a turn, and `store.to_row` (`store.py:41`) reads
`query_id=record.query_id or query_id`. Calling `persist(meter_db, records)` with **no
`query_id=` kwarg** therefore yields NULL for free. The column is nullable and
`ForeignKey("queries.id", ondelete="SET NULL")` (`models.py:744-746`).

It is not merely convenient. The `queries` row was flushed inside the turn's transaction and
discarded with it, so naming its id from the second session is a referential-integrity
violation — **D2 executes that insert and asserts the database rejects it**, which is what
turns a stated constraint into a measured one.

### 5. What must NOT change, and why each is load-bearing

- **`queries.prompt_tokens` / `completion_tokens` stay a cache atomic with the `api_usage`
  rows.** A failure-path row has no `queries` row to cache into, so the seam must not touch
  those columns; the success path's sums must keep landing in the same commit
  (`ask.py:1395` inside `ask.py:1410`).
- **No new `call_kind`.** The failure-path rows go through the same `to_row`, so `CALL_KINDS`
  is unchanged (cases 11a/11b) and `estimated_cost` still never lands in `cost_usd`
  (10d/10e/10f).
- **`_run_turn`'s observable effect on the success path is unchanged.** It gains one
  assignment after its persist and one after its commit; it writes no row, opens no session
  and takes no branch it did not take before.
- **Nested-collection isolation is untouched** (cases 8/8b/8c). The `finally` is a second
  *writer* of one bucket's contents, guarded so only one of the two ever fires; it is not a
  second *bucket*.

---

## Acceptance criteria

Every row names a harness file and a case id. [build.md](../build.md): a prose criterion is a
wish, and two whole documents in this repository went unexecuted proving it.

| id | Harness | Asserts | Today |
|---|---|---|---|
| **13** | `scripts/metering_check.py` | Every function that opens `collect_usage()` reaches a `persist`/`persist_quietly` call from a `finally` **in that same function**. Derived by AST over every file under `backend/app`, never from a list | **RED**, naming `ask.py:620 run_turn()` |
| **13b** | `scripts/metering_check.py` | The `finally` that satisfies 13 opens a session of its **own** (`SessionLocal`) and passes **no** `query_id=`. Asserted over all five sites, derived from source | **RED** |
| **14a** | `scripts/metering_check.py` | Driving the seam with a receipt that says the turn raised turns N buffered records into **exactly N** `api_usage` rows. A COUNT, never "nothing threw" | **RED** (missing symbol) |
| **14b** | `scripts/metering_check.py` | Every one of those rows has `query_id is None` | **RED** (missing symbol) |
| **14c** | `scripts/metering_check.py` | **The double-write guard, all three receipt states in one case** (§3): raised → N, ordinary success → **zero**, swallowed-persist-then-commit → N | **RED** (missing symbol) |
| **15** | `scripts/metering_check.py` | `to_row(record)` with no `query_id=` kwarg yields `query_id is None`, **and** no `meter_as` call site in `backend/app` passes `query_id=`. Pinned at the unit, so 14b cannot be vacuously true | **GREEN — a guard.** See §6 |
| **D1** | `scripts/metering_check.py --db` | One unattributed row inserted through the real seam against the **real schema** and committed, read back with `query_id IS NULL`, `cost_usd` intact and `user_id`/`agent_id` still the ones the turn was for | premise |
| **D2** | `scripts/metering_check.py --db` | An `api_usage` row naming a `queries.id` that was never committed is **rejected by the foreign key** — an `IntegrityError` naming one, with the three attribution columns established NULLABLE first so nothing else could be doing the refusing | premise |
| **D3** | `scripts/metering_check.py --db` | **Both directions**: after D1's unattributed row the admin coverage numerator is **unchanged**; after one row with a real committed `query_id` it rises by **exactly one** | premise |
| **M1** | manual, [PLAN.md](PLAN.md) §5.1 step 2 | After a deliberately failed streamed turn, `/api/admin/spend` shows the rewrite and embeddings with non-zero cost, and **coverage has not moved** (§3.3) | — |

### The rows added after the first adversarial review

Every one of these covers a line the review proved was pinned by **nothing**: it deleted the
line, the whole suite stayed green, and the defect it re-opened was silent. §7.2 carries the
red run for each.

| id | Harness | Asserts | Why it was missing |
|---|---|---|---|
| **13c** | `scripts/metering_check.py` | The drain names are **derived from `app/metering/store.py`** rather than listed, and no log line of store.py's is restated by another module | `_PERSIST_NAMES` was a literal beside a store.py that had stopped being called, and `persist_quietly`'s swallow had been copied into `_run_turn` **log string included** |
| **13d** | `scripts/metering_check.py` | `app.api.ask.log` is a real `logging.Logger` answering `info`/`warning`/`exception`, **and** every module on this failure path that reads `log.<x>` binds `log` at module level | The `log = logging.getLogger(__name__)` this change set added — fixing a `NameError` that would have killed a turn on the artefact-storage branch — shipped with **zero** coverage |
| **14d** | `scripts/metering_check.py` | The **fourth** receipt state, `(rows_written=N, committed=False)`: rows added, then the commit RAISED and rolled them back → the seam writes N | The receipt's own table called three states exhaustive and omitted [PLAN.md](PLAN.md) §3.5's *headline* case. 14c stays green when `durable` drops `committed`; this is the case that does not |
| **14e** | `scripts/metering_check.py` | An **empty** buffer opens **no** second session, on both non-durable receipts (metering off after a success; a raise before the first model call) | Every case passed `n=3`, so the seam's `not records` clause — the whole of R3's mitigation — was never executed by anything |
| **16** | `scripts/metering_check.py` | AST over `_run_turn`: exactly one `await db.commit()`, the statement **immediately after** it assigns `receipt.committed = True`, and that is the only such assignment | M2 below was carried as manual and is not manual. Deleting that line left 13/13b/14a/14b/14c/15 **and** `admin_check.py` 5/5b/5c/5d green while every successful turn double-wrote |
| **16b** | `scripts/metering_check.py` | `receipt.rows_written` is assigned exactly once in `_run_turn`, **not inside an `except` handler**, and after the persist call in the same block | The third receipt state depends on the handler NOT writing it; nothing said so |
| **17a** | `scripts/metering_check.py` | A `CancelledError` raised inside the seam does not escape it — the seam returns `0` after really attempting to open a session | `except Exception` does not catch it (BaseException-only, Python 3.12.10), so a shutdown landing in the seam replaced the turn's real error with an accounting one |
| **17b** | `scripts/metering_check.py` | …and a **genuine** cancellation is re-armed on the current task, so the shutdown still happens | Containment alone is passed by a seam that eats cancellation whole — correct for this frame, and a server that declines to stop |
| **D4** | `scripts/metering_check.py --db` | Everything `--db` created is gone, **verified from a second session**: zero rows matching the run's marker and no fixture user | D1 committed fabricated spend against `select(User).limit(1)` — a **real person** — and cleanup was a `[warn]` that failed the run not at all |
| **M2** | now automated as **16** | The receipt's `committed` assignment moved above `await db.commit()`, or deleted | R2's control was specified as "14a run with the flag set at the wrong point", which is **unsatisfiable**: 14a constructs its own receipt and never executes `_run_turn`. Corrected in [PLAN.md](PLAN.md) §6 R2 |

**The pairing rule, made explicit** ([build.md](../build.md) rule 3). Each case below is
passed by a build in which the feature was **deleted**, and the case beside it is what stops
that:

| Negative-ish case | Also passed by | Paired with |
|---|---|---|
| 14c's "success path writes **zero** rows" | a `finally` that was never wired up | 14c's own first half (raised → **N**), in the same case |
| 14b's "every row has `query_id is None`" | a build that writes **no rows at all** | **14a** (there are N rows) and **15** (the unit really can produce a non-NULL) |
| 13's "a `finally` exists" | `finally: pass`, and by a `finally` that always persists | **13b** (substance) and **14c** (behaviour). PLAN §6 R13 |
| D3's "the numerator did not move" | a coverage query that is simply broken | D3's own second half (an attributed row moves it by exactly one) |

**And the case that is passed by deleting the guard rather than the feature**: 14c's second
half is the only thing standing between this fix and a silently doubled console (PLAN §6 R1).
Case 13 is passed *perfectly* by a `finally` that always persists — it cannot see R1 at all.

---

## What must keep working

Each of these is green today and named because a plausible wrong build turns it red.

- **`scripts/metering_check.py` 8 / 8b / 8c** — nested-collection isolation, this repo's
  double-count scar (`context.py:130-160`). A `finally` that fires on the success path
  recreates that bug from the other direction, with two plausible totals and no error.
- **`scripts/metering_check.py` 12** — the AST call-graph case: no entry point reaches
  `build_chat_model` outside a `meter_as`. Any rewrap of `run_turn` must leave `meter_as`
  textually inside the function the graph resolves.
- **`scripts/metering_check.py` 11a / 11b** — `CALL_KINDS` derived from application source.
  The failure path introduces no new kind string.
- **`scripts/metering_check.py` 10d / 10e / 10f** — the failure path goes through the same
  `to_row`, so `estimated_cost` must still never land in `cost_usd`.
- **`scripts/admin_check.py` 5 / 5b / 5c / 5d and `--live` "coverage carries measured AND
  total, measured <= total"** — unattributed rows cannot make `measured` exceed `total`
  because the numerator filters `query_id IS NOT NULL`. That is an argument, so it must be
  re-run rather than believed. D3 is its offline-executable half.
- **`_run_turn`'s commit handler** — it deletes staged R2 keys and **re-raises the original
  exception**. The new `finally` runs *after* that raise and must never replace or mask it.

  **The sentence that stood here was wrong, and it was wrong about the one exception that
  matters.** It read: *"A bare `except Exception: log.warning(...)` inside the `finally` is what
  guarantees it."* It does not. `asyncio.CancelledError` derives from `BaseException`, not
  `Exception` (verified on this venv, Python 3.12.10), so a uvicorn shutdown or `--reload`
  landing inside the seam's single DB round trip escaped the swallow and replaced the turn's
  real `404 No endpoints found` with `CancelledError` — the accounting error arriving in place
  of the one the whole feature exists to surface. Measured by driving the seam with a session
  factory that raises it.

  What guarantees it is `except BaseException`, plus a re-arm so a genuine cancellation is
  deferred rather than eaten: `asyncio.current_task().cancelling()` is non-zero only when a
  cancel was really requested, so the seam can tell a shutdown from a driver raising
  `CancelledError` spuriously. Cases **17a** (containment) and **17b** (re-arm) — two cases,
  because a seam that swallows cancellation whole passes 17a and quietly declines to stop the
  server. Reachability is low: `stream.py` deliberately never cancels a turn on client
  disconnect, so this needs a shutdown inside one insert-and-commit. The claim was
  unconditional, and the plan rested on it.
- **`scripts/agentic_check.py` S1** and every scenario that calls `run_turn` directly with a
  real database and asserts trace rows — the success path must stay byte-identical **in
  observable effect**.

---

## 6. Where PLAN.md is in tension with itself, and one case it classifies wrongly

Recorded here rather than fixed silently, and carried to [PLAN.md](PLAN.md) §8 at ship time.
PLAN.md owns the contracts; a feature file may not edit it, but it may not pretend either.

**(a) `_run_turn`'s body cannot be literally untouched.** PLAN §2's table says the fix leaves
"`_run_turn`'s body untouched, which is what keeps `agentic_check.py` S1's *byte-identical to
the classic path* a structural claim". PLAN §3.5 requires the commit flag to be "set on the
line immediately after" `await db.commit()` — which is `ask.py:1410`, **inside `_run_turn`**.
Both cannot hold. §3.5 is the more specific and the load-bearing one (it is the R1 control),
so it wins: `_run_turn` gains two assignments and nothing else. S1's claim survives as
*byte-identical in observable effect*, which is what S1 actually asserts.

**(b) File ownership for `persist_quietly`'s return.** PLAN §4's build table assigns
`backend/app/metering/store.py` ("return shape only") to this feature, and §3.5 explains why
the return must distinguish *wrote N* from *had nothing* from *raised and was swallowed*. The
session brief that produced this file restricted it to `backend/app/api/ask.py` and
`scripts/metering_check.py`. **Settle this before writing code, not during.** The
`ask.py`-only alternative — calling `metering_store.persist` directly inside a `try` in
`_run_turn` and counting the rows there — duplicates `persist_quietly`'s swallow logic at the
call site and is worse; it is recorded so it is not rediscovered as an idea. Cases 14a/14b/14c
drive the seam with an explicit receipt and are **independent of the resolution**, which is
deliberate: the guard's behaviour is pinned either way.

**What shipped is the alternative this paragraph argues against, and the ownership line is
why.** The build could not edit `store.py`, so `_run_turn` calls `metering_store.persist`
inside its own `try`. Disclosed rather than chosen — the comment at that call site says so in
as many words — and it has a live cost that must not be left to be rediscovered:

- **`store.persist_quietly` now has ZERO call sites.** `grep -rn persist_quietly backend/app`
  returns its definition and comments about it. All four `finally` precedents call `persist`;
  the fifth site — the one this feature added — calls `persist` too. It is dead code carrying
  the only other copy of the swallow.
- **Two consequences were fixed inside this change set's own files.** The turn's log line no
  longer restates store.py's byte-identical `"could not persist usage for this turn"`, so an
  operator grepping a warning lands on the code that emitted it (case **13c**); and the
  harness's drain names are now derived from store.py rather than hardcoded beside it, so the
  set stops accepting a function nothing calls (also **13c**).
- **What is left for whoever owns `store.py`:** delete `persist_quietly`, or widen its return
  as this section prefers and collapse the six lines in `_run_turn` back to one call. Either
  way 13c and 14a-14e follow without an edit — the first is derived, the rest drive an explicit
  receipt.

**(c) Case 15 is GREEN today, not "red by missing symbol".** PLAN §4.1 lists 15 under *RED
today* with that parenthetical. It is wrong: `store.to_row` exists and already defaults
`query_id` to `None`, and no `meter_as` call site passes one. 15 belongs in §4.1's **second**
row — *green today, red under a wrong fix* — and the wrong fix it kills is a seam that helpfully
threads `query_id=query.id` through to the failure path, which would make every unattributed
row a foreign-key violation against a rolled-back id. Forcing it red would have meant writing
a case against a symbol invented for the purpose, which is precisely the case-written-to-pass
[build.md](../build.md) rule 2 forbids. It is left green and labelled.

**(d) The filename.** PLAN §4 links this file as `02-failed-turn-metering.md`; the session
brief named it `02-lost-usage.md`. PLAN.md is the authority on names — §3.7 already settles one
filename correction for feature 01 — and a second copy under a second name is the "contract
stated twice" that drifts. This file carries PLAN.md's name so PLAN.md's links resolve.

---

## 7. Watch it fail — the red run, before a line of `app/` changed

[build.md](../build.md) rule 2. Recorded verbatim because a case added after the code is a
case written to pass, and the only proof that this one was not is the failure text.

```
-- 13-15. a turn that RAISES must not discard the spend it paid for --
[FAIL] 13. every collect_usage() site drains its buffer from a finally -- 5 site(s), unguarded: ['api/ask.py:620 run_turn()']
[FAIL] 13b. each of those opens its OWN session and stamps no query_id -- covered 4/5; no own session: none; stamps query_id: none
[FAIL] 14a. the seam turns N buffered records into N api_usage rows -- app.api.ask has no failure-path seam yet (ImportError: cannot import name '_TurnReceipt' from 'app.api.ask')
[FAIL] 14b. and every one of those rows carries query_id = NULL -- app.api.ask has no failure-path seam yet (ImportError: ...)
[FAIL] 14c. the seam writes IFF the buffer is not already durable (R1) -- app.api.ask has no failure-path seam yet (ImportError: ...)
[ok]   15. to_row omits query_id -> NULL, stamps it -> a value, and no meter_as sets it -- bare=None stamped=set meter_as sites passing query_id: none

--db: what the real schema does with these rows
  database: dpg-d9vt7v1t0dsc738c8kpg-a.singapore-postgres.render.com:5432
[FAIL] D1. the seam writes an unattributed row the schema accepts -- app.api.ask has no failure-path seam yet
[ok]   D2. an api_usage row naming an uncommitted queries.id is REJECTED -- IntegrityError
[ok]   D3. an unattributed row leaves coverage flat, an attributed one raises it -- unattributed 6->6 (must not move); attributed 6->7 (must be +1)

6 FAILED  (exit 1).   Cases 1-12 unchanged and green; admin_check.py 5/5b/5c/5d green.
```

**13 names the defect at `ask.py:620` and reports the denominator with it — five sites, one
unguarded.** That is the number that matters: it is derived from the source rather than from a
list, so a sixth `collect_usage()` added later without a `finally` fails this case on the day
it lands. 13b reads *covered 4/5*, which is what its denominator being `_collect_sites` rather
than the already-guarded sites buys: deleting a `finally` must never make 13b easier to pass.

**D2 and D3 are the two facts this change set was resting on, and both are now measured rather
than argued.** The foreign key really does refuse a row naming an uncommitted `queries.id`
(`IntegrityError`), so §4's omission is mandatory rather than tidy. And coverage moved
`6 -> 6` for an unattributed row and `6 -> 7` for an attributed one — **both directions in one
case**, because "the number did not move" is also true of a query that is simply broken.
Neither leg commits: both flush and roll back, so `--db` leaves nothing behind. Verified after
the run: `harness rows left: 0`.

### 7.1 And the run that proves they can go GREEN

A case that can only ever be red is as useless as one written to pass, so the resolution rules
were run against synthetic sources and a reference seam before being trusted. All five
discriminations hold:

| Source shape | 13 | 13b |
|---|---|---|
| the fix, persist reached through a one-hop same-module helper | **guarded 1/1** | own session, no `query_id` |
| `finally: pass` (R13) | **guarded 0/1 — red** | — |
| persists through the **turn's** session (the one being rolled back) | guarded 1/1 | **own_session=False — red** |
| stamps `query_id=query.id` (the FK violation D2 measured) | guarded 1/1 | **no_query_id=False — red** |
| the `finally` lives in a **nested** def inside `run_turn` | **guarded 0/1 — red** | — |

The last row is why the AST walk refuses to descend into nested functions. Crediting an outer
function with an inner one's `finally` is a false **all-clear**, and this case asserts a path
must *exist* — the opposite polarity from case 12, which resolves names bare across the whole
app precisely because an invented edge there is only a false alarm.

Against a reference seam, 14a/14b/14c produce `raised -> 3 rows, committed -> 0 rows
(0 sessions opened), swallowed-then-committed -> 3 rows`. The three-state guard is
satisfiable, and the middle column is the one standing between this fix and a doubled console.

### 7.2 The second red run — every case the review proved was missing, mutated

[build.md](../build.md) rule 1: *a case that passes with AND without the code it guards
measures nothing*. The first build's suite was 231 assertions green, and an adversarial review
then deleted single lines and watched it stay green. So each case below was run **under the
exact mutation it exists to catch**, and then again with the line restored. Both runs are
recorded, because "it goes red under a mutation" is the only evidence that a case added after
the code was not written to pass.

| Mutation applied to `backend/app/api/ask.py` | Red | Green after restore |
|---|---|---|
| delete `receipt.committed = True` | **16** — *"1 commit(s) at [1689]; next statement is the flag: [False]; receipt.committed assigned at NOWHERE"* | all 42 |
| hoist `receipt.committed = True` above `await db.commit()` | **16** — *"assigned at [1689, 1708] (must be exactly one line)"* | all 42 |
| `durable` → `bool(self.rows_written)` (drop `committed`) | **14d** — *"persist-then-failed-commit->0 rows (sessions opened 0, must be 1)"*, with **14c still green** | all 42 |
| delete `not records` from the seam's guard | **14e** — *"metering-off success: 0 rows / 1 sessions"* | all 42 |
| `except BaseException` → `except Exception` in the seam | **17a** *"seam raised CancelledError"* and **17b** | all 42 |
| delete the `task.cancel()` re-arm only | **17b** alone — *"seam returned 0; task ended cancelled: False"* | all 42 |
| delete `log = logging.getLogger(__name__)` | **13d** — *"ask.log=None; unbound: ['api/ask.py uses [log]']"*, then the suite aborts on `NameError` inside the seam's own swallow | all 42 |
| restore store.py's log string in `_run_turn` | **13c** — *"restated elsewhere: {'could not persist usage for this t': ['api/ask.py', 'metering/store.py']}"* | all 42 |
| add `receipt.rows_written = 0` to the persist's `except` | **16b** — *"assigned at [1665, 1672]; inside an except handler: [1672]"* | all 42 |
| `--db` cleanup keyed on a marker that matches nothing | **D4** — *"api_usage rows matching gen-harness-D1-088c6d7d3aa1: 1"* | D1-D4 green |

Quoted verbatim, so the line numbers inside the failure text are the ones that run printed
and have already moved — which is the same reason nothing else in this file cites a line in
`ask.py`. The case ids have not moved and are the durable half.

The two rows worth reading twice. **The `durable` mutation leaves 14c green** — three states
driven, all three still correct — which is precisely why the fourth state needed its own case
rather than a fourth line inside 14c. And **the D4 row deliberately leaked a row into
production** to prove the cleanup detects a leak rather than assuming one; the row was removed
by hand immediately afterwards and the table re-counted at 464 rows, 15 users, zero
`gen-harness%`.

### 7.3 `--db` writes to production, and it used to write on a stranger's name

The safety defect, fixed before anything else in this pass. `D1` is the only case in
`metering_check.py` that COMMITS — the seam it executes opens its own session and commits, so
a flush-and-roll-back would not be executing the thing under test. It selected its foreign
keys like this:

```python
_uid = (await db.execute(_sa_select(_UserRow.id).limit(1))).scalar()
_aid = (await db.execute(_sa_select(_AgentRow.id).limit(1))).scalar()
```

`DATABASE_URL` points at the live Render Postgres holding 16 real users' work, so that is **a
real person**, plus an agent that person may not even own, carrying a committed
`cost_usd = 5.338e-05` in the very accounting table this feature exists to make trustworthy.
Cleanup was `except Exception: print("[warn] could not clean up ...")` — it did not call the
file's own `not_measured()`, did not fail the run, and printed *all checks passed* underneath.
Nothing had leaked when the review checked, so the hazard was the handling.

What it does now, following `slice_check.py` (`slice-check@localhost`) and `ui_check.py`
(`ui-check@groundwork.local`):

- **its own subject**, `metering-check@groundwork.local` keyed on `google_sub =
  "metering-check-local"` — a value neither a Google `sub` (21 digits) nor the `dev|` shim can
  produce — created idempotently and **printed with its ids** before the write;
- **one marked row**, `gen-harness-D1-<uuid>`, which is also what makes D1's assertions
  stronger: `user_id` and `agent_id` can now be asserted to have SURVIVED while `query_id` did
  not, which is the whole shape of a recovered row;
- **deletion verified from a second session** as case **D4**, because a delete that was rolled
  back still looks deleted from inside the transaction that issued it. Bulk `DELETE`s, never
  `await db.delete(user)` — the ORM path lazy-loads `User.agents` to de-associate them and
  raises `MissingGreenlet` from a validator;
- **a failed cleanup is a FAILURE**, not a warning: D4 prints the exact `DELETE` to run by hand
  and exits 1.

D2 and D3 still flush and roll back. And D2 no longer accepts any exception as proof of the
foreign key: it establishes that `user_id`, `agent_id` and `query_id` are all NULLABLE first —
so nothing else could be doing the refusing — and then requires an `IntegrityError` naming a
foreign key. Without that, a NOT NULL on an unrelated column would have passed it as *"the
foreign key refused"*.
