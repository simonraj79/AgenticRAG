# 03 — The keyless handout download

> **Build order 2 of 3.** [PLAN.md §4](PLAN.md) — independent of feature 02, and 01 depends on
> neither. Owns `backend/app/api/handouts.py` and `scripts/storage_check.py`, and nothing else.

A `ready` handout row whose `storage_key` is `NULL` answers **500** on download under the
default R2 route. Not 409, not 503, not a message — a bare 500 whose traceback names neither
the column, the line, nor the table, and which reaches the browser as a **CORS error**. There
is no such row in the database today, which is exactly why this survived: the defect is
unreachable on the happy path, and the one thing that produces it is the procedure this repo
documents as a working rollback.

---

## 1. What is true today — the audit, with line numbers

Everything in this section was read off the running system, not inferred from the source.

> **Every line number below is from the PRE-FIX tree and none of them resolve any more.** That
> is fine here and only here: §1 is a record of the code as it was when the defect was found, so
> its numbers are part of the record rather than directions. Nothing describing the shipped code
> cites a line — see the note in §3. The row counts in §1.4 were re-verified on 2026-08-23 and
> still held; the reason the *comment* in `handouts.py` no longer quotes them is §9.3 REFINE-6.

### 1.1 The route decides whether to fetch the bytes before it knows which road it will take

```python
# handouts.py:645
handout = await _load_owned(db, agent, handout_id, with_content=not storage.enabled())
```

`storage.enabled()` (`app/storage.py:79-81`) is `settings.storage_route == "r2"`, and
`app/config.py:671` defaults `storage_route` to `"r2"`. `STORAGE_ROUTE` is absent from this
repo's `.env`, so **the R2 road is what runs locally and on Render**. On that road
`with_content=False`, so `_load_owned:331-332` never applies `undefer(Handout.content)`.

That line is the whole defect in one expression: `storage.enabled()` is a fact about the
*deployment*, and the branch it stands in for is a fact about the *row*. The two agree for
every row that exists today, and disagree for exactly one shape of row.

The route then runs three gates, in this order:

| Lines | Gate | What it does |
|---|---|---|
| `:647-655` | status | 409 unless `status == "ready"`. **Runs before any read of `content`** — and it is the only reason today's data is safe |
| `:657-678` | R2 | taken only when `storage.enabled() and handout.storage_key`; 302, or 503 on `StorageError` |
| `:680-693` | the documented Postgres fallthrough | **`:684 if handout.content is None:` is the FIRST read of the column**, not `:691` |

A `ready` row with no key falls past the first two gates into `:684`, and `:684` touches a
deferred attribute on a row that was loaded without it.

### 1.2 The mechanism, proved against the live database rather than asserted

`app/db/models.py:900` is `content: Mapped[bytes | None] = deferred(mapped_column(LargeBinary))`.
Verified against the mapper rather than the source, because a docstring is a claim and the
mapper is the fact:

```
Handout.__mapper__.attrs['content'].deferred      -> True
Handout.__mapper__.attrs['storage_key'].deferred  -> False
```

Loading a `Handout` through `SessionLocal` with no `undefer` and touching `.content` raises:

```
sqlalchemy.exc.MissingGreenlet: greenlet_spawn has not been called; can't call await_only() here
```

It names neither the column, nor the line, nor the table. Re-loading the same row **with**
`undefer` returned 33,726 bytes, so the deferred load is the only variable.

This repo already knows this failure — in a different module.
`app/api/conversations.py:493-495` justifies a fourth query with *"the alternative is a lazy
load per message, which on an async session raises MissingGreenlet rather than merely being
slow"*. The download route is the one place that lazy load is still reachable.

### 1.3 Why it surfaces as a CORS error, which points at the wrong subsystem

`app/main.py:43-47` registers no exception handlers and `debug` is False, so `MissingGreenlet`
propagates to a bare 500. `CORSMiddleware` never gets to add its headers, because the handler
had already raised — so the browser reports a CORS failure on a route whose CORS configuration
is perfect.

**This is the identical misleading symptom as the `/api/admin/spend` `GROUP BY` bug**, which is
the failure `admin_check.py --live` was written for and which `admin_check.py:262-268` records
in as many words. That is not a coincidence worth noting once; it is the argument for this
feature's `--live` mode existing at all. An offline harness reads source and introspects
routes. It cannot execute a request, so a route that *imports* and does not *run* is invisible
to it.

### 1.4 Nothing 500s today, and the reason is population, not correctness

Read read-only against production. `handouts` holds **10** rows:

| status | `storage_key` | `content` | count |
|---|---|---|---|
| `ready` | not null | not null | **8** |
| `failed` | **NULL** | NULL | **2** |

Zero rows are `ready`-and-keyless. Both keyless rows are `failed`, so the `:647` status gate
refuses them at 409 before `:684` is reached. **The 409 is load-bearing and nobody wrote it to
be** — it is holding back a defect two gates downstream.

### 1.5 Both writers make a keyless `ready` row impossible on the R2 road — which is the point

- `app/handouts/jobs.py:863-869` puts the object and sets `storage_key` **before** the row is
  committed.
- `app/api/ask.py:1145-1163` **skips the row entirely** on a put failure rather than writing it
  keyless.

So a `ready`-and-keyless row is producible by exactly one thing: `STORAGE_ROUTE=postgres`.
Which is precisely the rollback that `config.py:658-665`, `models.py:906-911` and
[13-object-storage/PLAN.md:222](../13-object-storage/PLAN.md) all promise works, and which
`handouts.py:680-683`'s own comment describes as the blue/green window where *"both kinds of
row exist and both must download"*. The comment is correct about the intent, and the code four
lines below it is wrong about the mechanism.

**So the defect is not "an impossible row". It is: the documented rollback is one restart away
from a 500, and the rollback is the thing you reach for when object storage is already
broken.**

---

## 2. What the user gets

A handout made while `STORAGE_ROUTE=postgres` downloads after the route is switched back to
`r2`, instead of failing with an error that names the wrong subsystem. A `ready` row that has
neither a key nor bytes says so — 409, *"This handout has no stored file"* — rather than 500.
Nothing else about the download changes: keyed rows still redirect, not-ready rows still 409,
and the R2 route still never reads bytea for a row it is going to redirect.

---

## 3. The fix — load the bytes only after the R2 branch has declined

One move, and its direction is the whole design: **the decision to fetch `content` belongs
below the branch that decides whether `content` is needed, not above it.**

```
_load_owned(..., with_content=not storage.enabled())   # an OPTIMISATION now, not a decision
status gate                                            # unchanged, and still first
R2 branch -> 302 / 503                                 # unchanged; reads no bytea
the Postgres fallthrough
  ^ HERE: if `content` is still unloaded, fetch just that column; else use what we have
if content is None -> 409                              # now reading a local variable
Response(content=content)
```

> **Line numbers were deliberately removed from this diagram, and the removal is the finding.**
> Every `handouts.py:NNN` in the first draft of this file and in `storage_check.py`'s new block
> was written against the *pre-fix* tree, and the fix moved all of them. `:684` — cited twice as
> "the read that raised `MissingGreenlet`" — landed after the edit on `if storage.enabled() and
> handout.storage_key:`, the redirect gate, which a reader chasing this defect would plausibly
> accept as the right place. **A stale line number is worse than no line number**, because it
> points confidently at the wrong code. §1's audit keeps its numbers: that section is a record
> of a tree that no longer exists, and it says so. Everything describing the *shipped* code
> cites a function, an argument or a case id instead.

**The re-load fetches the ONE COLUMN, guarded on whether the load actually left it deferred.**
The shipped shape is:

```python
if "content" in sa_inspect(handout).unloaded:
    content = await db.scalar(
        select(Handout.content).where(
            Handout.id == handout.id,
            Handout.agent_id == agent.id,
        )
    )
else:
    content = handout.content
```

Three properties, in descending order of what they would cost to rediscover:

1. **The condition asks the OBJECT, not the deployment.** That is the defect restated in
   reverse. The bug was a fact about the deployment (`storage.enabled()`) standing in for a
   fact about the row, so writing `if storage.enabled():` here would reproduce the same mistake
   in a new place. `sa_inspect(handout).unloaded` is set arithmetic over the instance's own
   attribute dict — no query, no IO, nothing that can raise — and it cannot disagree with the
   load that actually happened. It stays right if the `with_content` argument above is changed
   or deleted.
2. **The agent filter is repeated in the WHERE clause**, even though `_load_owned` already
   proved ownership, so the property *"no statement in this module fetches a handout without
   naming its agent"* holds statement by statement. That property is asserted by case **77c**
   and was not asserted at all in the first build — see §9.3.
3. `with_content` stays **load-bearing** on its original call. Deleting the keyword because
   nobody passes it would be the tidy edit that makes case **77b** unfalsifiable — it would
   still pass, against a parameter nobody uses. A dead parameter and a live one are the same
   shape from `inspect.signature`.

The cost is one extra `SELECT`, of one column, **only when the bytes are needed and were not
already loaded** — i.e. a keyless row on the R2 road, which is the rollback shape where the
alternative is a 500. On the R2 road with a key nothing is added: the redirect returns before
the fallthrough. On the Postgres road nothing is added either, and that is the `else`: the
first load already carried the column, so re-fetching it would make every rollback download
pay a second statement for bytes it was already holding.

**This is NOT what the first draft of this section specified**, which was an unconditional
re-load through `_load_owned(..., with_content=True)` at the fallthrough. That design is
measurably wrong and this change set's own harness rejects it — §9.3.

Two alternatives, rejected and named so they are not re-proposed:

- **`undefer` unconditionally at `:645`** (drop `with_content=not storage.enabled()` for a
  plain `True`). Passes case 78. Reintroduces the entire cost the R2 route exists to remove,
  silently, with every byte still arriving. This is [PLAN.md §6 R10](PLAN.md), and **78c plus
  78b's keyed leg are the only things that catch it.**
- **Un-defer `content` on the mapper.** [PLAN.md §6 R11](PLAN.md).
  `conversations.py:501-513` replays a thread by selecting whole `Handout` ORM objects with no
  `undefer`, so every message replay would start dragging bytea — reading as the app getting
  slower, months later, with no change to blame. **77** and **77b** exist for this and for
  nothing else.

---

## 4. Contracts consumed

By reference into [`PLAN.md`](PLAN.md), never restated here:

- **§3.1** — no new setting. `STORAGE_ROUTE` is read, not added.
- **§3.2** — no migration. `handouts.content` is neither dropped nor un-deferred.
- **§3.6** — the harness contracts this feature's `--live` mode is built on: three states never
  two; NOT MEASURED must be loud; `raise_app_exceptions=False` on any transport that expects a
  5xx; fixture rows cleaned in a `finally` and titled so a leak is findable; an empty SQL
  capture is not a passing capture.
- **§3.7** — the id ledger. This feature claims **77**, **77b**, **77c** offline and **78**,
  **78b**, **78c**, **78d**, **79**, **79b** in a new `--live` mode of
  `scripts/storage_check.py`, and nothing in `agentic_check.py`. **`77c` and `78d` were added
  during REFINE and the ledger in PLAN.md was amended to match** — an id that exists only in a
  harness comment is a collision waiting to happen in a change set three agents are editing at
  once, and by build.md rule 1 it is not an acceptance criterion at all. See §9.3.
- **§3.8** — no new trace event. A download is not a step the agent took.
- **§6 R8, R9, R10, R11, R12** — the five risks that are this feature's.

And the correction §3.7 makes to this feature's own audit: **`agentic_check.py` S34 does not
exist.** The audit listed it among the scenarios that must keep working. The download
regression set is S8 (×4 recipes), S8b, S8c, S28 and S29. No criterion below cites S34.

---

## 5. Acceptance criteria

**Harness: `scripts/storage_check.py`.** Offline cases run in the default invocation; the rest
need the new `--live` mode, which executes the real route against the real database through
`httpx.ASGITransport`.

```bash
backend/.venv/Scripts/python.exe scripts/storage_check.py           # 77, 77b, 77c
backend/.venv/Scripts/python.exe scripts/storage_check.py --live    # 78, 78b, 78c, 78d, 79, 79b
backend/.venv/Scripts/python.exe scripts/storage_check.py --cleanup # the sweeper, not a case
```

| # | Criterion | Case | Today |
|---|---|---|---|
| **A1** | `Handout.content` is still deferred **on the mapper** | `storage_check.py` **77** | green — guard |
| **A2** | `_load_owned`'s `with_content` keyword still exists and still defaults to `False` | `storage_check.py` **77b** | green — guard |
| **A3** | A `ready` keyless row with real bytes downloads: **200**, byte-identical, quoted CRLF-free `Content-Disposition` | `storage_check.py` **78** | **RED — 500** |
| **A4** | The keyless download **reads** the bytea — `handouts.content` SQL, and no 5xx — and a keyed download does not | `storage_check.py` **78b** | **RED** |
| **A5** | A `ready` **keyed** row still answers 302 with a presigned `Location`, and makes exactly one `presigned_get_url` call | `storage_check.py` **78c** | green — guard |
| **A6** | A keyless row that is **not** `ready` still answers 409, never 500 | `storage_check.py` **79** | green — guard |
| **A7** | A `ready` keyless row with `content IS NULL` answers 409 *"This handout has no stored file"*, never 500 | `storage_check.py` **79b** | **RED — 500** |
| **A8** | On the **rollback arm**, where the first load already carried the bytes, the download is 200 with byte-identical content and **exactly one** statement mentions `handouts.content` — no second fetch beside the one it already had | `storage_check.py` **78d** | added in REFINE — see §9.3 |
| **A9** | Every `select(...)` chain in `app/api/handouts.py` that mentions `Handout` also mentions `Handout.agent_id` | `storage_check.py` **77c** | added in REFINE — see §9.3 |

### Why each guard exists — the wrong build it kills

Per [build.md](../build.md) rule 3, a case asserting a thing does *not* happen is also passed
by a feature that has been deleted. Each green-today case below names the specific wrong fix it
is the only detector for.

- **77** kills *"just un-defer the column"*. Read off
  `Handout.__mapper__.attrs['content'].deferred`, not off the source — case **75b**'s own
  lesson, where grepping the class body for `"content"` went red against a docstring that
  documents the absence of a `content` field.
- **77b** kills *"delete the `with_content` keyword"* and *"default it to `True`"*. Read via
  `inspect.signature`, so no caller can opt every route in by editing one default.
- **78c** kills *"just read Postgres always"* — the fix that passes 78 and 79b perfectly while
  putting the bytea read back on every download. It is paired with 78b's keyed leg because the
  two ask the same question from opposite ends: 78c asserts the redirect still happens, 78b
  asserts no bytes were fetched while it happened.
- **79** kills *"move the content load above the status gate"*. `download_ui_check.py` **D4**
  is its browser sibling and asserts the same refusal from the other end; this is the API-layer
  half, and it is cheap enough to run every time.
- **78d** kills *"delete the `else` and just refetch"*, and it is the **only** case in the
  repository that does. Measured 2026-08-23 against exactly that edit: 78 stayed 200 at 52/52
  bytes, 78b stayed keyless=1 / keyed=0, 78c stayed 302, 79 and 79b stayed 409, and 78d alone
  went red at **2** statements mentioning the column where 1 is correct. The regression it
  catches is invisible to every assertion about a response body, because the bytes are perfect
  either way — only the statement count moves.
- **77c** kills *"delete the redundant `Handout.agent_id == agent.id`"*. Measured 2026-08-23:
  with that predicate removed **both** modes of `storage_check.py` stayed fully green, because
  `_load_owned` had already scoped the row so the behaviour was identical. That is what a
  defence-in-depth check is: redundant, therefore invisible, therefore deletable with nothing
  going red. Case **76** in the same file is the pattern — *"the harness that would otherwise go
  green by deletion"*.

### 78b is a pair inside one case, and an empty capture is not a pass

[PLAN.md §3.6](PLAN.md). `'handouts.content' not in statements` over an **empty** capture is
green forever if the `sqlalchemy.engine.Engine` logger stops emitting — the trap
`agentic_check.py` S11 already carries an `unmeasured` floor for. 78b asserts:

1. the capture is **non-empty**, or the case is NOT MEASURED;
2. the **keyless** leg's SQL **contains** `handouts.content` **and that request did not 5xx**;
3. the **keyed** leg's SQL **does not**.

Leg 2 is what makes leg 3 mean anything. Without it, 78b is passed by a build in which the
capture never sees anything at all.

**The `and that request did not 5xx` in leg 2 was added during the watch-it-fail run, because
without it 78b went GREEN against the defect.** See §9.2 — SQLAlchemy logs a statement before
the driver adaptation raises, so a deferred load that was *attempted and died* leaves exactly
the same line in the capture as one that succeeded. A read that raised is not a read.

### The two cases that must be loud rather than skipped

- **78 / 78b are NOT MEASURED on the `postgres` road**, never green. Run with
  `STORAGE_ROUTE=postgres`, `with_content` is `True`, the defect is structurally unreachable
  and 78 would pass having measured nothing ([PLAN.md §6 R9](PLAN.md)). The live block reads
  `settings.storage_route`, **prints it**, and gates on it. The tell for a vacuous run is the
  absence of a printed route, which is why printing it is mandatory rather than nice.
- **78c must never degrade to a quiet skip.** The audit specified it as NOT MEASURED when the
  database holds no keyed `ready` row — which means the only guard against R10 is simply absent
  on a fresh environment. **This feature inserts its own keyed fixture row instead**, so 78c is
  measurable everywhere. `presigned_get_url` is a local HMAC with no round trip (S11b's own
  control relies on that same property), so the object the key names does not have to exist.

---

## 6. Watch it fail — the protocol, and what a wrong red looks like

[build.md §5](../build.md): the case is written first and **run against unmodified code**. The
order is 77, 77b, 78, 78b, 78c, 79, 79b written together, then one `--live` run before a single
line of `handouts.py` changes. **78, 78b and 79b must print `[FAIL]` naming a 500.**

Two ways the red run can lie, both from [PLAN.md §6](PLAN.md):

- **R8 — green by abort.** `httpx.ASGITransport(app=app)` defaults to
  `raise_app_exceptions=True` (httpx 0.28.1, signature verified), so `MissingGreenlet`
  propagates **out of the harness**, aborting the file with a traceback and **zero recorded
  failures**. That is precisely the shape `storage_check.py`'s `_Missing` sentinel (`:52-70`)
  already exists to prevent, arriving through a new door. Every live transport in this file
  passes `raise_app_exceptions=False`. `admin_check.py --live` does not hit this only because
  all of its live cases assert 200.
- **R12 — a leaked fixture.** The live cases insert rows into the **shared production**
  database, because every keyless row already there is `failed` and the defect is unreachable
  with existing data. Three layers, and the first one is the one the shipped build got wrong:
  the fixture is owned by a user this harness **creates**, cleanup is in a `finally` keyed on
  the inserted ids *and* on that owner, and `--cleanup` sweeps by owner for the case a
  `finally` cannot reach.

The fixture is a whole disposable **agent**, under a whole disposable **user**, not rows
grafted onto somebody's real account.

> **The shipped build picked its owner with `select(User).limit(1)` and that was the most
> serious defect in this feature.** No `ORDER BY`, so *which* of the real people in `users` got
> a `storage_check --live fixture` agent in their dashboard was whatever Postgres returned
> first, and could differ between runs. A harness that writes into the account of the person it
> is testing for is worse than the defect it was written to catch. `slice_check.py`
> (`slice-check-local`) and `ui_check.py` (`ui-check@groundwork.local`) already had the pattern
> and it was not followed. It now creates and deletes `google_sub = "storage-check-local"`.
> **Never `select(User).limit(1)` in a mode that writes.**

> **And naming a leak is not removing it.** The shipped build's mitigation was the title
> convention — which makes a leak *findable* and leaves it removable only by hand. A `finally`
> covers a raise; it does not cover a hard kill or a database connection lost mid-run, and this
> repo has the scar (CLAUDE.md, *Background jobs*: a wedged job left a row at `pending` for 26
> minutes and "the only escape hatch is the user deleting the row"). `--cleanup` closes it,
> the way `slice_check.py --cleanup` and `agentic_check.py --cleanup` already do.

Three reasons for the disposable agent, unchanged: a leak is then one obviously-named row in a
dashboard rather than a mystery file in a working panel; `handouts.agent_id` is
`ON DELETE CASCADE`, so one delete removes everything; and `seed_download_fixture.py`'s own
idempotence check (*"if existing: nothing to do"*) would be confused by extra handouts
appearing under the agent it owns. The disposable **user** adds a fourth: `agents.owner_user_id`
is `ON DELETE CASCADE` too, so the sweeper needs exactly one key and cannot half-delete.

---

## 7. What must keep working

Each row names what would go red, so a fix cannot quietly cost one of them.

| Contract | Held by | How this feature could break it |
|---|---|---|
| A keyed `ready` handout downloads through the 302 and its bytes are read off the followed response | `agentic_check.py` **S8** ×4 recipes, **S8b**, **S8c**, **S28**, **S29** (`follow_redirects=True` + `mounts`, `:2885-2900`) | **These all pass if the fix routes keyed rows through Postgres** — the route's entire purpose gone, every scenario green. **78c** and **78b**'s keyed leg are the detectors, not these |
| The list route emits no `handouts.content` SQL and makes **zero** object-storage calls | `agentic_check.py` **S11**, **S11b** | Any `undefer` placed somewhere shared |
| The 409 status gate runs **before** any read of `content` | `storage_check.py` **75** (greps the route source), **79** (executes it) | Moving the load above `:647` |
| `HandoutOut` exposes no `content` / `storage_key` / `url` / `download_url` / `presigned_url` | `storage_check.py` **75b** | Not touched by this feature; listed because the temptation to "return the URL instead" lives next door |
| S11's subject still exists — the `content` column is not dropped | `storage_check.py` **76** | R11's neighbour |
| A real click yields a file with the right name and size, the bytes are what they claim, a chart thumbnail renders through the redirect, and a not-ready row refuses without redirecting | `download_ui_check.py` **D1-D4** | The browser half of the same contract |
| The 409-for-pending contract the card gates its thumbnail and anchor on | `frontend/src/components/HandoutCard.test.tsx:165`, `frontend/src/lib/types.ts:289-300` | Changing 409 to anything else for a not-ready row |
| Thread replay does not drag bytea | `conversations.py:501-513` selects `Handout` ORM objects with **no** `undefer` — the mapper must stay deferred | **77** |

---

## 8. What this deliberately does not do

- **It does not change the download contract.** 409 for not-ready, 302 for keyed-and-ready, 200
  with a quoted CRLF-free `Content-Disposition` for the Postgres fallthrough. All three are
  gated on by the frontend and none moves. [PLAN.md §7](PLAN.md).
- **It does not backfill the keyless rows.** `scripts/migrate_bytes_to_r2.py` already exists for
  that and is a separate, deliberate operation. A route that 500s on a row shape the rollback
  produces is a bug in the route, and fixing it by removing the rows would leave the next
  rollback exactly as broken.
- **It does not add a `content IS NULL AND storage_key IS NULL` constraint.** Both columns are
  nullable on purpose (`models.py:906-911`), and during the blue/green window a backfilled row
  has both. A constraint would make the rollback un-runnable, which is the opposite of the fix.
- **It does not touch `app/storage.py`, `app/handouts/jobs.py` or `app/api/ask.py`.** The two
  writers are already correct (§1.5); the reader is not.
- **It does not add a setting, a migration or a trace event.**
  [PLAN.md §3.1, §3.2, §3.8](PLAN.md).
- **Nothing here is model-decided.** No tool, no retry on model output, no detector over model
  text, so no [loop-prompt.md](../loop-prompt.md) session. [loop.md](../loop.md) is cited only
  as **T2** — *trigger on the absence of the outcome you wanted, never on the presence of an
  error* — because this defect is a pure instance of it: every offline harness in the repository
  is green, no exception is logged anywhere the application can see, and the only assertion that
  catches it is *"did a 200 with the right bytes come back?"*.

---

## 9. As built — where this file was wrong

[build.md §3](../build.md): the only section written with hindsight. §9.1 is the red run, §9.2
is the finding of the build phase, and §9.3 now carries the REFINE pass as well — six review
findings measured against the shipped build, four of which reproduced and two of which are
mutation-proven.

> **The fix went green and the review found real defects anyway, several by MUTATION — delete a
> line, whole suite stays green.** That is this repo's standing rule arriving on this feature:
> green is where the work starts. Two of the six were things no assertion in the repository
> could see (a redundant scoping predicate, and the `if`/`else` shape), and the most serious was
> not about the route at all — the harness wrote into a real user's account.

### 9.1 The red run — 2026-08-23, against unmodified `handouts.py`

Cases written first, run before a single line of `backend/app/api/handouts.py` changed. Verbatim:

```
-- 77  the two guards against the WRONG fix for a keyless download --
[ok]   77. `Handout.content` is still deferred ON THE MAPPER -- deferred={'content': True, 'storage_key': False, 'byte_size': False}
[ok]   77b. `_load_owned(*, with_content=...)` exists, is keyword-only, defaults False -- signature=(db: 'AsyncSession', agent: 'Agent', handout_id: 'uuid.UUID', *, with_content: 'bool' = False) -> 'Handout'

==========================================================================
--live: the download route, EXECUTED against the real database
==========================================================================
  storage_route = 'r2'   <- decides which cases can be measured
  inserted fixture agent 632f01a2-e67b-4cf2-bcb8-2fb58e59b3f0 with 4 handout(s)

-- 78  THE DEFECT: a `ready` row with no storage_key, on the R2 road --
[FAIL] 78. a `ready` keyless row downloads: 200, byte-identical, quoted disposition -- status=500 bytes=21/52 disposition='' body='Internal Server Error'
[FAIL] 78b. the bytea is READ on the keyless road and NOT on the keyed one -- keyless=1/8 stmts mention it (status=500; the SELECT was logged and then died in MissingGreenlet, so it is an ATTEMPT, not a read), keyed=0/6 (status=302)
[ok]   78c. a `ready` KEYED row still answers 302 with a presigned Location -- status=302 presign_calls=1 signed=True location='https://9c28368bde886a0415644fab7d9d9627.r2.cloudflarestorage.com/grou'

-- 79  the status gate still runs BEFORE anything reads content --
[ok]   79. a keyless row that is NOT ready still answers 409, never 500 -- status=409 body='{"detail":"This handout is not ready yet (status: pending)."}'
[FAIL] 79b. a `ready` keyless row with NO bytes answers 409, never 500 -- status=500 body='Internal Server Error'

  cleaned up fixture agent 632f01a2-e67b-4cf2-bcb8-2fb58e59b3f0 and 4 handout(s)

==========================================================================
3 FAILED:
  - 78. a `ready` keyless row downloads: 200, byte-identical, quoted disposition
  - 78b. the bytea is READ on the keyless road and NOT on the keyed one
  - 79b. a `ready` keyless row with NO bytes answers 409, never 500
```

Three reds, three greens, and **no traceback** — `raise_app_exceptions=False` did its job, so
R8's green-by-abort did not happen and the defect arrived as printed rows. `bytes=21/52` is
`len("Internal Server Error")` against the fixture's 52, which is what a 500 looks like when the
assertion is byte identity rather than truthiness.

### 9.2 78b went GREEN on the first red run, and the reason is a new mechanism

**This is the finding of the phase.** The first draft of 78b was the case the audit specified:
the keyless leg's SQL **contains** `handouts.content`, the keyed leg's does not, non-empty
capture or NOT MEASURED. It printed:

```
[ok]   78b. the bytea is read on the keyless road and NOT on the keyed one -- keyless=1/8 stmts mention it, keyed=0/6
```

Green, on a request that had just 500'd two rows above it. Isolated and confirmed against the
live database:

```
RAISED: MissingGreenlet
statements: 12 mentioning handouts.content: 1
    'SELECT handouts.content AS handouts_content 
FROM handouts 
WHERE handouts.id = $1::UUID'
```

**SQLAlchemy logs a statement in `_execute_context` before the driver adaptation reaches
`await_only()` and raises.** So a deferred load that was *attempted and died* leaves a capture
byte-identical to one that succeeded, and the positive control designed to prove *the column
really was loaded* was being satisfied by the defect it was written to control for. A case
written to pass, arriving through a door nobody had a name for.

It generalises past this case, and it is a new entry beside the ones
[build.md §7](../build.md) tabulates: **an SQL-log capture measures what was SENT, never what
came BACK.** Every harness in this repository that greps captured statements — `agentic_check.py`
S11 among them — inherits that limit. S11 is unaffected, because it asserts an *absence* and a
statement that was never sent is never logged; the asymmetry is precisely that the negative
direction is safe and the positive direction is not. Any future case using a capture as evidence
that something *happened* needs a second signal that the statement completed. Here that signal is
the response status, asserted in 78b itself rather than borrowed from 78 — not a duplicate of
78's byte comparison, but the precondition for 78b's own evidence being legible.

**And it is why the watch-it-fail run is not a formality.** 78b would have been written, would
have gone green after the fix, and would have been recorded as proving a cost property it never
measured. Nothing would have raised. It was caught by the one thing that catches this class:
running the case against unmodified code and being surprised that it passed.

### 9.3 Deviations from the plan, and what the REFINE review found

**Everything from here down under "REFINE" was written after an adversarial review measured it
against the shipped build. Four of its six findings reproduced; one did not.** They are kept in
full rather than folded away, because the two that were mutation-proven are the ones that
generalise.

#### REFINE-1 — the fixture wrote into a random real user's account

Covered in §6 above. The fix is in `scripts/storage_check.py`: a get-or-create fixture user
keyed on `google_sub = "storage-check-local"`, deleted in the `finally` alongside the agent, and
a new `--cleanup` mode that sweeps by owner. Verified by counting the production tables before
and after a full `--live` run: 10 handouts / 15 users both times, no `storage_check` agent left.
Fixed first, before any other finding, because a harness that corrupts the data it tests against
is worse than the defect it tests for.

#### REFINE-2 — §3 of this file specified a build its own harness rejects

The first draft of §3 said, in bold, *"The re-load goes through
`_load_owned(..., with_content=True)`, not through a bare `undefer`"*, and placed it
unconditionally at the fallthrough. The shipped code does neither — it is a one-column
`select(Handout.content)` guarded on `sa_inspect(handout).unloaded`, a third approach §3 did not
list even among its two rejected alternatives.

**Measured 2026-08-23: implementing this file's literal design and running the shipped harness
turns 78d RED at 2 statements mentioning the column where 1 is correct.** Everything else stays
green — 78 at 200 with 52/52 bytes, 78b keyless=1 / keyed=0, 78c at 302, 79 and 79b at 409. So
the spec of record described a build this change set's own harness refuses.

The doc's three reasons were not wrong, they were incomplete. Reason 3 in particular is
factually correct — the identity map does return the same instance with the previously-unloaded
column populated, which is *why* the design looks free. What it misses is that
`select(Handout)` emits SQL regardless of the identity map, so on the rollback road, where the
first load already carried `content`, the re-load is a second full-entity fetch of bytes the
session is already holding. §3 is now rewritten to the shipped shape.

The rule this instance is of: **a design paragraph and a harness case are two statements of one
contract, and when they disagree the harness is the one that ran.** Update the paragraph in the
same pass, or the next reader implements the paragraph.

#### REFINE-3 — the "pinned by" comment named a subset of the guards

`download_handout`'s fallthrough comment listed cases 78, 78b and 79b, and omitted **78d** — the
only case that pins the `if`/`else` it sits directly above. Mutation-proven: delete the `else`,
refetch unconditionally, and all three named guards stay green while 78d alone goes red. An
editor doing exactly what that comment invites — read the named cases, make the tidy edit, watch
them pass — ships the regression 78d exists to catch. **A comment naming a subset of the guards
is worse than one naming none, because it tells an editor when to stop looking.** Fixed; 78d and
77c are both named there now.

#### REFINE-4 — 78d existed in no ledger and no acceptance table

`grep -rn 78d "new features/"` returned nothing: the repo's strongest guard on this fix had its
only record in a harness comment. By build.md rule 1 that makes it not an acceptance criterion
at all, and in a change set three agents are editing concurrently the id ledger is what stops
two of them claiming one number. Fixed in both places — **A8** in §5 above, and PLAN.md §3.7's
`storage_check.py` row. **That is this feature's only edit to the shared PLAN.md**, and it is
confined to the ledger row and the R12 row.

#### REFINE-5 — the greppable-scoping claim was asserted by nobody

The fallthrough comment argued that repeating `Handout.agent_id == agent.id` keeps the property
*"no statement in this module fetches a handout without naming its agent"* establishable by
grep. Nothing grepped it. **Mutation-proven 2026-08-23: delete the predicate and BOTH modes stay
fully green** — offline all-pass, `--live` all-pass — because `_load_owned` had already scoped
the row, so behaviour is identical and no assertion about a response can see the deletion.

That is not a weakness of the harness; it is the definition of defence in depth. A redundant
check is invisible by construction, which is exactly what makes it deletable by a tidy edit with
nothing going red. Case **77c** now parses `app/api/handouts.py` with `ast` and requires every
`select(...)` chain mentioning `Handout` to mention `Handout.agent_id`. Four chains qualify
today and four comply; under the mutation it prints
`1/4 UNSCOPED: 'select(Handout.content).where(Handout.id == handout.id)'` and exits 1.

Two mechanics of 77c worth keeping. It is **AST, not regex**: a regex would match the string
inside the very comment making the argument, so the guard would be satisfied by its own
justification — `deck_check.py` case 14 has that scar. And it carries the same **empty-capture
floor** as 78b and 78d: zero chains found is NOT MEASURED, never green, because
`not unscoped` over an empty list is green forever the day the module stops calling `select` by
that name.

#### REFINE-6 — the stale line citations, and the one claim that did not reproduce

Every `handouts.py:NNN` citation in the new harness block was a pre-fix line number, and the fix
moved all of them. Two were actively misleading rather than merely stale: `:684`, cited twice as
"the read that raised `MissingGreenlet`", now lands on the redirect gate. All are replaced with
named anchors — a function, an argument, a case id. §1's audit keeps its numbers on purpose: it
is a record of a tree that no longer exists.

**The related claim about production counts did NOT reproduce.** The review reported that "2
rows have `storage_key IS NULL`" had "already drifted to 4". Counted read-only against
production on 2026-08-23, before and after this work: **10 handouts, 2 keyless and both
`failed`, 0 `ready`-and-keyless, 15 users** — exactly what the comment said. The most likely
explanation is a count taken while a `--live` run's own fixture rows were in the table, which is
itself an argument for the change made: the underlying rule holds regardless of whether this
instance of it had fired yet, so the absolute count is gone from the comment and only the
load-bearing **zero** is kept, dated.

### Deviations from the plan

- **78b is not the case [PLAN.md §3.6](PLAN.md) originally described. §3.6 has now been
  corrected** — during REFINE, in the same pass as the ledger and R12, and it is the only other
  edit this feature makes to the shared plan. It had said the keyless leg is *"the positive
  control proving the counter can move"*, which is true and is not sufficient: §9.2 above. The
  third clause — the leg that proves a read HAPPENED must also prove the request did not 5xx —
  is stated there now, in the one place, and is not restated here. The case id, the mode and the
  NOT MEASURED floor are all unchanged.
- **The file is `03-keyless-handout-download.md`, not `03-keyless-download.md`.**
  [PLAN.md](PLAN.md) links to the longer name twice (§1 and the §4 build table) and PLAN.md is
  the shared contract; a second file at the shorter name would be a contract stated twice,
  which §3 of PLAN.md forbids. One file, the plan's name.
- **78c does not degrade to NOT MEASURED on a database with no keyed `ready` row.** §5 above.
  The audit specified a conditional case; a guard that is absent on a fresh environment is not
  a guard, so the live block inserts its own keyed row.
