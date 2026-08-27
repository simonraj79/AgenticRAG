# 15 — Failure paths — PLAN

Phase 2 of [build.md](../build.md). **This file owns every shared contract. The feature
files reference it and never restate it** — a contract stated twice drifts, and the copy
that drifted is never the one you are reading.

Audits: one per feature, carried in the feature files themselves
([01](01-streamed-turn-address.md), [02](02-failed-turn-metering.md),
[03](03-keyless-handout-download.md)). Nothing below re-derives them; where this file
contradicts an audit, this file wins and says so in §8.

---

## 1. What this change set is, in one paragraph

Three defects, and the single thing they have in common is the thesis: **every one of them
lives on a failure path, so none of them is visible on a happy path, and that is why all
three survived this long.** A streamed turn that raises leaves the browser holding a
conversation id the server rolled back, so the user's next question 404s on a thread that
never existed. A turn that raises anywhere between its `flush()` and its single `commit()`
discards every `api_usage` record it already paid OpenRouter for — the spend is not
mis-attributed, it is *absent*, and the admin console's coverage number cannot reveal it. And
a `ready` handout row with no `storage_key` 500s on download under the default R2 route,
because the route decides whether to undefer `content` before it knows which road it will
take. Nothing here is a new capability; all three are the code doing the wrong thing at
exactly the moment the code is not being watched. The change set adds **no migration, no
setting, no trace event and no schema change** — it adds three fixes, two new harness modes
and eleven new harness cases, most of which exist to stop the fix being satisfiable by
deleting the feature it protects.

**Why the three travel together rather than as three small changes.** They share a shape, and
the shape is the reusable half: *a check written against the success path reports success
about a path it never entered.* [build.md §7](../build.md) tabulates eight green suites that
were wrong; three of these eight — S3, `ui_check`'s 24px pane, and `metering_check`'s
`goldenset` hole — are the same defect class as all three defects here. Fixing them one at a
time would produce three risk registers that each rediscover it.

---

## 2. Architecture — where each fix sits

```
   BROWSER                                    SERVER
   ───────                                    ──────

   AgentChat.tsx                              stream.py:_run_turn_streamed
   ┌──────────────────────┐                   ┌──────────────────────────────────┐
   │ onStart(event)       │◀── start frame ───│ async with SessionLocal() as db: │
   │   wasDraft?          │    ~0.1 s         │   Conversation(uuid4())          │
   │   setActiveId(id) ───┼──┐                │   db.add / await db.flush()      │  DEFECT 1
   │   activeIdNow = id   │  │                │   run_turn(...)  ──────────────┐ │  the window
   └──────────────────────┘  │ PROMOTED       │                                │ │
                             │                │                                │ │
   ┌──────────────────────┐  │                │  ┌─────────────────────────────▼─┴──────┐
   │ catch (cause)        │◀─┼── error frame ─│  │ run_turn (ask.py:591)                │
   │   NEW: revert, but   │  │   25-45 s      │  │  with collect_usage() as records,     │
   │   only on EVIDENCE   │  │                │  │       meter_as(kind="generation"):    │
   │   (§3.4)             │──┘                │  │    return await _run_turn(...)        │  DEFECT 2
   └──────────────────────┘                   │  │  NEW: finally -> persist in a SECOND  │  no try,
                                              │  │        session, guarded on committed  │  no finally
                                              │  └───────────────────────────────────────┘
   ┌──────────────────────┐                   │      _run_turn: flush -> ... -> commit    │
   │ HandoutCard anchor   │── GET download ───▶└──────────────────────────────────────────┘
   └──────────────────────┘                          │
                                                     ▼
                                        handouts.py:download_handout
                                        ┌──────────────────────────────────┐
                                        │ _load_owned(with_content=        │
                                        │             not storage.enabled())│  DEFECT 3
                                        │ status gate -> 409               │  decided before
                                        │ if enabled AND key -> 302        │  the road is known
                                        │ else: reads .content  <-- 500    │
                                        └──────────────────────────────────┘
```

Three files change in `app/` and `src/`, one each:

| Defect | Fix lives in | Layer | Nothing else in that module moves |
|---|---|---|---|
| 01 | `frontend/src/views/AgentChat.tsx` | client state | `api.ts` untouched — the `error` frame already arrives as a thrown `ApiError` with the right shape, and the documented 404 route-fallback (`api.ts:465-470`) stays |
| 02 | `backend/app/api/ask.py` (`run_turn` wrapper, and `persist_quietly`'s return) | request path | `_run_turn`'s body is untouched, which is what keeps `agentic_check.py` S1's "byte-identical to the classic path" a structural claim |
| 03 | `backend/app/api/handouts.py` (`download_handout`) | request path | `Handout.content` stays `deferred` on the mapper; `_load_owned`'s `with_content` default stays `False` |

**No backend change is required for defect 01**, and that is a finding rather than an
omission: `stream.py:206-226` is correct — it creates the row inside its own session and lets
the session roll back, which is exactly right. The client is what is wrong about it.

---

## 3. Shared contracts

Everything in this section is owned here. A feature file that needs one cites the subsection
number.

### 3.1 Settings — none

No new setting, on either side. All three fixes are unconditional behaviour changes on paths
that are currently wrong; there is nothing an operator would want to turn off, and a flag
would mean the failure path had two behaviours and only one of them was ever exercised, which
is the defect class this change set is about.

Two **existing** settings are read by harnesses and must not be edited to make a case pass:

| Setting | Read by | Rule |
|---|---|---|
| `storage_route` (`config.py:671`, default `"r2"`) | `storage_check.py --live` | The live block must READ it, PRINT it, and mark cases 78/78b **NOT MEASURED** when it is `postgres`. On the postgres road `with_content=True` and defect 03 is unreachable, so a green row there measures nothing. See §3.6 |
| `metering_enabled` / `metering_strict` (`config.py`) | `metering_check.py` | Unchanged. The failure-path persist inherits the same off switch as the success-path one; with metering off there are no records to write and the new `finally` is inert |

### 3.2 Schema — NO migration. None. Zero alembic revisions.

Stated explicitly because [build.md §3](../build.md) requires the plan to settle the
migration, and because "settle it here so two features cannot race for the same
`down_revision`" has no force if the answer is silence. The head stays
`f6b28d4c1a73_admin_observability`.

Every column all three fixes need already exists and already has the right nullability:

| What the fix needs | Already true | Where |
|---|---|---|
| An `api_usage` row with no owning query | `query_id` is `Mapped[uuid.UUID \| None]`, `ForeignKey("queries.id", ondelete="SET NULL")`, indexed | `models.py:744-746` |
| `to_row` yielding NULL when no query is named | `query_id=record.query_id or query_id`, and **no `meter_as` call site in the repo passes `query_id`** (9 call sites, verified) | `store.py:41-48` |
| A handout row that is `ready` with a null key and real bytes | `storage_key` nullable, `content` nullable `LargeBinary`, kept deliberately as the rollback road | `models.py:892-911` |
| A conversation created and rolled back | Ordinary transaction semantics | `stream.py:206-214` |

**If a feature file finds itself wanting a column, stop and come back here.** A schema change
would make this a different change set with a different risk profile — a migration opens the
deploy window [build.md §8](../build.md) documents crash-looping the service, and none of
these three defects is worth that.

### 3.3 An unattributed usage row carries `query_id = NULL`

The contract, in one sentence: **a usage record persisted from a failed turn is written with
its user, agent, call kind, model, served provider, tokens and cost, and with `query_id`
omitted — never with the id of the `queries` row that was rolled back.**

Three reasons, and the first alone settles it:

1. **The foreign key forbids the alternative.** The `queries` row was flushed inside the
   turn's transaction and discarded with it. Writing its id from a second session is a
   referential-integrity violation, and the second session is the only session that survives.
   `metering_check.py` **D2** executes that insert and asserts the database rejects it, so
   this is a measured constraint rather than a stated one.
2. **`to_row` already does the right thing for free.** Call `persist(meter_db, records)` with
   no `query_id=` kwarg and every row gets NULL, because no `meter_as` in the codebase sets
   `UsageRecord.query_id`. Case **15** pins that at the unit, which is what stops case **14b**
   ("every failure-path row has `query_id is None`") from being vacuously true of a build
   where no rows are written at all.
3. **It is the honest value.** NULL means *this spend belongs to no turn that exists*, which
   is precisely what happened. This is the same argument `store.persist` already makes for
   `queries.prompt_tokens` being NULL rather than `0`: a zero says free, a NULL says not
   measured.

**The consequence for the admin console, stated here because it is the thing most likely to
be misread as the fix not working.** After this change:

| Console surface | Moves? | Why |
|---|---|---|
| Total spend, `calls`, group-by user / agent / model / kind / provider (`admin.py:202-208, 350, 404, 607-609, 663-671, 766-768`) | **Yes — this is the evidence** | These are aggregates over `api_usage` with no join |
| The four `INNER JOIN queries` sites (`admin.py:479, 533-539, 553`) | No | They join on `query_id`; unattributed rows are correctly excluded |
| **Coverage** — `count(distinct query_id) WHERE query_id IS NOT NULL / count(queries.id)` (`admin.py:273-277`) | **No, and it must not** | On a failed turn both the numerator row and the denominator row are absent *together*. Adding a NULL-`query_id` row moves neither half |

> **So the evidence that defect 02 is fixed is total spend and `calls`. It is never
> coverage.** Anyone re-running `admin_check.py --live` after the fix and reading a flat
> coverage number must not conclude the fix did nothing. `admin_check.py`'s live case
> *"coverage carries measured AND total, measured <= total"* must stay green for the same
> reason — and **D3 asserts both directions**, that an unattributed row leaves the numerator
> unchanged *and* that an attributed one raises it by exactly one, because a check that a
> number did not move is also passed by a query that is broken.

### 3.4 A conversation-id revert must be evidence-based, never unconditional

The client may un-promote an address **only** when both of these hold:

1. **The turn is definitively over.** An `error` frame — the stream said so. Never an abort:
   Stop stops the *reader*, `stream.py:301-329` records that the turn "runs to completion and
   commits. Nothing is cancelled", so on an abort the row is still coming.
2. **`chat.list(agentId)` has been asked and has not returned the id.** That request runs in
   its own server session, so **a row it returns is by definition committed** — it is the only
   committed/rolled-back discriminator that exists on the client. Its absence from one
   response is weaker evidence than its presence, which is why (1) is required beside it.

Both halves are load-bearing and each kills a different wrong fix:

- **Drop (1)** and the abort path reverts while the turn commits underneath, opening a
  SECOND conversation and splitting the user's thread across two rows with no indication why
  — the exact outcome `AgentChat.tsx:735-752` already argues against in prose. That is why
  hole (a) (`error` frame) and hole (b) (Stop before first token) **must not share one code
  path**: hole (a) reverts, hole (b) settles.
- **Drop (2)** and a turn that committed and *then* raised gets its live conversation
  orphaned. That is four concrete code paths, not a hypothesis: `stream.py:247`'s
  `result.model_dump(mode="json")` and the session close both sit outside the
  `async with SessionLocal()` and inside the `try`, and `ask.py:1422-1423`'s handout
  `db.refresh` loop and the `AskOut(...)` construction at `:1425` both run after
  `ask.py:1410`'s commit. **AC2 is the case that holds this**, and it is green today and red
  under an unconditional revert.

**Two mechanical rules that come with it**, because both have already produced silent bugs in
this file:

- **`activeIdNow.current` is written in the same statement as every `setActiveId`.** Four
  existing writers pair them (`:499/500`, `:516/517`, `:601/602`, `:694/695`); the revert is
  the fifth writer of the state and the fifth writer of the ref. Forgetting the ref makes
  `onAddress()` (`:582`) disagree with the screen for the rest of the session — no exception,
  no console error, answers appended to the wrong thread. This is the same defect class as the
  one being fixed, and no existing check would see it, which is why **AC3 asserts the address
  the second send USED** rather than inspecting component state.
- **`settling` stays derived** (`activeId !== null && activeId === unsettledId`, `:398`), never
  promoted to stored state. That derivation is what keeps every *other* thread usable while
  one settles, and it is why `startDraft` (`:514`) correctly does not clear `unsettledId`.

### 3.5 The commit flag — what "the turn already persisted" is allowed to mean

Feature 02's `finally` must not write rows the success path already wrote. The guard is named
here so the feature file cannot invent its own and the harness cases can cite it.

**The signal is: the turn's single `await db.commit()` returned. It is set on the line
immediately after, and nothing else sets it.**

Two plausible alternatives are both wrong, and each is wrong silently in a different
direction:

| Candidate signal | Fails how |
|---|---|
| *"`persist_quietly` ran"* | It runs at `ask.py:1395`, **fifteen lines before** the commit at `:1410`. Rows it added are rolled back by a failing commit — the exact case the fix exists for. Too early: the fix would decline to write on the one failure it was built for |
| *"`_run_turn` returned normally"* | `ask.py:1416-1425` — the handout `db.refresh` loop and `AskOut(...)` — runs after the commit and can raise. Too late: a raise there means committed rows AND a `finally` that writes them again. **Double-count, which is this repo's existing scar** |

**And `persist_quietly` cannot report what it did.** It returns `(None, None)` both when there
was nothing to write (`store.py:118-125`) and when the write raised and was swallowed
(`store.py:104-107`). Any flag derived from its return inherits that ambiguity, and the
failure mode is that a swallowed persist followed by a good commit clears the buffer having
written nothing — **reproducing the original hole through the fix**. It has exactly one caller
in the repository (`ask.py:1395`), so making it report is cheap; feature 02 owns the shape.

**Case 14c is the whole guard**, and it asserts both halves in one case: flag unset → N rows,
flag set → **zero** rows. The negative half alone is also passed by a `finally` that was never
wired up, and the AST case (13) is passed by a `finally` that always persists. Neither sees
the double-write. 14c does.

### 3.6 Harness contracts shared across the three features

These are properties of the *harnesses*, and two or three features each depend on them, so
they live here.

**Three states, never two.** `storage_check.py`'s header (lines 20-24) is the authority:
*"a row that could not be measured must never print green, and must not fail the suite
either."* Its new `--live` block uses that file's own `check` / `not_measured` vocabulary and
**must not** import `admin_check.py`'s pass/fail-only shape or its `[skip]` line. The same
applies to `metering_check.py --db` and to `agentic_check.py` S37/S38.

**NOT MEASURED must be loud.** A case that degrades to unmeasured on a database with no
suitable row is a guard that is simply absent in a fresh environment. Case **78c** (a keyed
`ready` row still answers 302) is the only thing standing between the fix and "just read
Postgres always", so its absence must print a reason and a hint, not a quiet skip.

**An ASGI transport used to observe a failure must not raise the failure.**
`httpx.ASGITransport(app=app)` defaults to `raise_app_exceptions=True`. Under the red run,
defect 03 raises `MissingGreenlet` **out of the harness**, aborting the file with a traceback
and **zero recorded failures** — green-by-abort, the shape `storage_check.py`'s `_Missing`
sentinel already exists to prevent. Every live block that expects a 5xx passes
`raise_app_exceptions=False`. (`admin_check.py --live` does not hit this only because all its
live cases assert 200.)

**Fixture rows inserted into the shared database are OWNED by a user the harness created,
cleaned in a `finally` keyed on the ids inserted, titled so a human can find a leak, and
removable by a `--cleanup` sweeper.** `storage_check.py --live` and `metering_check.py --db`
both insert into production tables. A crash between insert and cleanup leaves a `ready` handout
visible in somebody's panel. Follow `seed_download_fixture.py`'s titling convention.

> **Amended during REFINE, after feature 03 shipped violating the first and fourth clauses.**
> As written this rule had only the middle two, and that was not enough twice over.
> **`select(User).limit(1)` is never the owner of a fixture** — no `ORDER BY`, so it writes into
> whichever real person Postgres returned first, and which one differs between runs;
> `slice_check.py` (`slice-check-local`) and `ui_check.py` (`ui-check@groundwork.local`) already
> had the get-or-create-your-own-identity pattern. And **naming a leak makes it findable, not
> removable**: a `finally` covers a raise but not a hard kill or a lost connection, so a
> `--cleanup` mode that sweeps by owner is the only thing that closes it, exactly as
> `slice_check.py --cleanup` and `agentic_check.py --cleanup` already do.

**A committed/rolled-back assertion is made from a SECOND, independent session.** Reading back
through the session that wrote proves nothing about commitment. S37 and S38 both do this, and
it is the entire mechanism of the pair.

**An empty capture is not a passing capture.** `78b` inherits S11's trap verbatim: asserting
`'handouts.content' not in statements` over an EMPTY statement list is green forever if the
`sqlalchemy.engine.Engine` logger stops emitting. The case asserts the capture is non-empty
and reports NOT MEASURED otherwise, and the keyless leg is the positive control proving the
counter can move. `78d` carries the same floor for the same reason, and so does `77c` — zero
`select` chains found is NOT MEASURED, never green.

> **Third clause, added after the watch-it-fail run: the leg that proves a read HAPPENED must
> also prove the request did not 5xx.** SQLAlchemy logs a statement in `_execute_context`
> BEFORE the driver adaptation reaches `await_only()` and raises, so a deferred load that was
> *attempted and died* leaves a capture byte-identical to one that succeeded — and 78b's first
> draft went GREEN against the defect it was written for. **An SQL-log capture measures what was
> SENT, never what came BACK.** The asymmetry is that the negative direction is safe (a
> statement never sent is never logged, so S11 is unaffected) and the positive direction is not.
> Full record in [03-keyless-handout-download.md §9.2](03-keyless-handout-download.md).

### 3.7 Harness id ledger — the ids this change set claims, and the ones it must not

Settled here so no feature file takes "the next free number".

| Harness | Claimed by this change set | Free but deliberately unused |
|---|---|---|
| `scripts/agentic_check.py` | **S37**, **S38** | — |
| `scripts/metering_check.py` | **13**, **13b**, **13c**, **13d**, **14a**–**14e**, **15**, **16**, **16b**, **17a**, **17b** offline; **D1**, **D2**, **D3**, **D4** in a new `--db` mode. (13c/13d/14d/14e/16/16b/17a/17b/D4 were claimed in the refine pass, each covering a line the first review proved was pinned by nothing — feature 02 §7.2) | **L8** — no new model call site is added, so there is nothing a live call could measure that 13-17b and D1-D4 do not. Leave it free |
| `scripts/storage_check.py` | **77**, **77b**, **77c** offline; **78**, **78b**, **78c**, **78d**, **79**, **79b** in a new `--live` mode | — |
| `frontend/src/views/AgentChat.address.test.tsx` (NEW file) | **AC1**–**AC5**, as an id prefix inside each `it()` name | — |
| `frontend/src/views/AgentChat.test.tsx` (NEW file) | **AC6**–**AC10**, same prefix convention | **R1–R14 are NOT available as case ids anywhere in this change set** — they are §8's risk register. The first draft of this file used R1–R3 and a grep for "R2" returned a metering risk and a chat case with nothing to tell them apart |

> **`77c` and `78d` were added to feature 03 during REFINE and are recorded here rather than
> only in the harness.** They were built first and appeared in no ledger and no acceptance
> table, which is the exact collision this section exists to prevent when three agents are
> editing concurrently — and by build.md rule 1 a case with no named criterion is not an
> acceptance criterion at all. `78d` is the only guard on the fix's `if`/`else` (delete the
> `else` and every other case stays green); `77c` is the only guard on the repeated
> `Handout.agent_id` predicate (delete it and both modes stay green). Both are mutation-proven
> in [03-keyless-handout-download.md §9.3](03-keyless-handout-download.md).

> **`agentic_check.py` stops at S33. S34, S35 and S36 are RESERVED and were never built** —
> `13-object-storage/03-handout-bytes.md:217-218` claims S34/S35, `01-storage-harness-floor.md:403`
> claims S36, and `13-object-storage/PLAN.md:577` records why S34 could not be written as
> planned. Taking "the next free number" would collide with a written plan for work that may
> still land. **S37/S38 are the first genuinely free ids**, and this row exists so the next
> change set does not have to rediscover that.

**File-naming correction, settled here.** Feature 01's audit names the new vitest file twice
and differently — `AgentChat.test.tsx` in its *owns* line and `AgentChat.address.test.tsx` in
its harness-target section. **The ACCEPTANCE harness is
`frontend/src/views/AgentChat.address.test.tsx`**: the name says which contract the file holds,
and it leaves room for a general `AgentChat.test.tsx` later without a rename that would
invalidate every acceptance criterion citing it.

*Update, refine pass:* that general file now exists and holds AC6–AC10 (the four conditions on
the revert plus the cost of one of them). Two files rather than one because the fixtures are
opposites — the acceptance file's `chat.load` throws by design so that no case in it can open
an existing thread, and two of the edge cases need exactly that.

**A second correction, and it matters more.** Feature 03's audit lists `agentic_check.py`
**S34** among the scenarios that must keep working. **S34 does not exist.** The download
regression set is S8 (×4 recipes), S8b, S8c, S28 and S29. Do not write a criterion against
S34 and do not "restore" it.

### 3.8 New trace event types — none

`app/rag/events.py` gains nothing. All three defects are about what happens when a turn or a
request fails; none of them is a step the agent took, and putting a failure-path persist or a
client-side address revert on the user-facing trace would imply the model decided something.

*(This subsection exists because [build.md §3](../build.md) requires the plan to state new
trace events. The answer being "none" is the contract — and it is worth stating rather than
omitting, because `TraceRecorder.record` raising on an unknown type is the only gate there is,
and a frontend `TracePanel` map missing an entry degrades **silently**.)*

---

## 4. Build sequence — lowest layer first

| # | Feature | File | Owns | Depends on |
|---|---|---|---|---|
| **02** | A failed turn's metering | [02-failed-turn-metering.md](02-failed-turn-metering.md) | `backend/app/api/ask.py`, `backend/app/metering/store.py` (return shape only), `scripts/metering_check.py` | nothing |
| **03** | The keyless handout download | [03-keyless-handout-download.md](03-keyless-handout-download.md) | `backend/app/api/handouts.py`, `scripts/storage_check.py` | nothing |
| **01** | The stranded conversation id | [01-streamed-turn-address.md](01-streamed-turn-address.md) | `frontend/src/views/AgentChat.tsx`, **new** `frontend/src/views/AgentChat.address.test.tsx`, `scripts/agentic_check.py` S37/S38 | **02** — see below |

**The numbering in the folder is the audit's numbering and is kept; the build order is
different and is this table.** `01` is a client fix and the highest layer, so it is built
last.

**01 depends on 02 for one concrete reason.** S37 and S38 drive the real
`stream._run_turn_streamed`, which calls `run_turn` — the exact function feature 02 rewraps.
Writing them before 02 lands means writing them twice, or running them against a `run_turn`
that is about to grow a `finally` that opens a second session. Build 02 first and S37/S38 are
written once, against the shape that ships.

**02 and 03 are independent of each other** and may be built in either order; 02 is listed
first because it is the deeper of the two (`run_turn` sits under every chat surface, including
the one 01's scenarios drive) and because its AST case 13 reads every file under `backend/app`,
so it is the case most likely to be perturbed by any other backend edit.

**One feature per session, cleared between**, per [build.md §6](../build.md). None of the three
is model-decided — no tool, no retry on model output, no detector over model text — so **none
of them opens with [loop-prompt.md](../loop-prompt.md)**. [loop.md](../loop.md) is still
relevant to all three as T2 (*trigger on the absence of the outcome you wanted, never on the
presence of an error*), and each feature file cites it, but there is no model decision here to
design a trigger for.

### 4.1 Harness-first, and what "watch it fail" means for each case

[build.md §5](../build.md): add the case, **run it, watch it fail**, then build. The honest
complication is that **not every case in this change set can be red today**, and pretending
otherwise would produce a case written to pass. Three kinds, labelled:

| Kind | Cases | How it is made falsifiable |
|---|---|---|
| **RED today** — asserts the defect is fixed | `13`, `13b`, `14a`/`14b`/`14c`, `15` (red by missing symbol), `78`, `78b`, `79b`, **AC1**, **AC4** | Run and record the failure text before a line of `app/` or `src/` changes. For AC1 specifically: **confirm it fails on its own assertion, not on a render error** — see §6 R6 |
| **GREEN today, RED under a wrong fix** — the guard | `77`, `77b`, `78c`, `79`, **AC2**, **AC3**, **AC5** | Each names the wrong build it kills, in §6 and in its feature file. These are the cases build.md's rule 3 is about, and none is optional |
| **PREMISE, not defect** — establishes the fact the fix rests on | `D1`, `D2`, `D3`, **S37**, **S38** | These characterise the server. S37 asserts the rolled-back row is *absent*, which is TRUE today — if it were red the client fix would be unnecessary. **The pair is what makes each non-vacuous**: S37 and S38 run the same code path and differ only in whether the injected failure fires, so a build where the conversation is never created at all fails S38, and a build where the failure never fires fails S37. Neither may be written without the other |

---

## 5. Definition of done — the exact commands

Low to high. There is no CI; every one of these is run by hand, so the order is the protocol.

```bash
# layer 1 -- no DB, no network, seconds
backend/.venv/Scripts/python.exe scripts/sandbox_check.py
backend/.venv/Scripts/python.exe scripts/deck_check.py
backend/.venv/Scripts/python.exe scripts/ledger_check.py
backend/.venv/Scripts/python.exe scripts/refusal_check.py
backend/.venv/Scripts/python.exe scripts/route_specialist_check.py
backend/.venv/Scripts/python.exe scripts/llm_check.py
backend/.venv/Scripts/python.exe scripts/storage_check.py          # + cases 77, 77b, 77c
backend/.venv/Scripts/python.exe scripts/metering_check.py         # + cases 13, 13b, 14a-c, 15
backend/.venv/Scripts/python.exe scripts/admin_check.py

# executes real SQL / real routes -- the half a layer-1 harness structurally cannot do
backend/.venv/Scripts/python.exe scripts/metering_check.py --db    # NEW mode: D1, D2, D3
backend/.venv/Scripts/python.exe scripts/storage_check.py --live   # NEW mode: 78, 78b, 78c, 78d, 79, 79b
#   ^ writes fixture rows under a user it CREATES (`storage-check-local`) and deletes both.
#     After a hard kill:  scripts/storage_check.py --cleanup
backend/.venv/Scripts/python.exe scripts/admin_check.py --live
backend/.venv/Scripts/python.exe scripts/metering_check.py --live

# layer 2
cd frontend && npm test          # 5 files / 46 tests today, + AgentChat.address.test.tsx AC1-AC5
cd frontend && npm run build

# layer 3 -- DB + providers
backend/.venv/Scripts/python.exe scripts/agentic_check.py --setup
backend/.venv/Scripts/python.exe scripts/agentic_check.py --run    # + S37, S38
backend/.venv/Scripts/python.exe scripts/agentic_check.py --cleanup

# layer 4 -- browser, both servers up, GLOBAL interpreter
python scripts/ui_check.py
python scripts/mention_popup_check.py
python scripts/download_ui_check.py
```

`--cleanup` is not optional. A leaked Pinecone namespace is a real cost, and the Builder plan's
1,000-namespace cap **is** the maximum number of agents this deployment can hold.

### 5.1 And then the step that is not a command — three of them

[build.md §7](../build.md) ends by opening the page and reading one real output. **This change
set needs three, one per defect, and a happy-path glance proves nothing about any of them** —
that is the thesis in §1 arriving in the verification phase. Each of these deliberately
manufactures the failure:

1. **Defect 01 — make a streamed turn fail for real.** Point the fixture agent's
   `agents.generation_model` at a slug that does not exist (it is free text an operator can
   type into, and `openrouter_slug()` warns rather than guessing). Ask a question on a **new**
   thread. Watch: the error banner appears, the composer re-enables, and **the next question
   opens a new conversation rather than showing a 404 banner**. Restore the model afterwards.
   Read the network tab for the second send: today it is two 404s
   (`/ask/stream` then the JSON fallback, `api.ts:465-470`); after the fix it must be one
   `askNewStream`.
2. **Defect 02 — open the console after that same failed turn.** The turn above cost a
   rewrite and at least one embedding before it died. Confirm those appear in `/api/admin/spend`
   with non-zero cost. **Confirm coverage did NOT move** — §3.3 — and do not read that as the
   fix doing nothing.
3. **Defect 03 — manufacture a `ready` keyless row using only the documented rollback.** Set
   `STORAGE_ROUTE=postgres` in `.env`, restart the backend, make a handout (it lands with
   `content` and no `storage_key`), set `STORAGE_ROUTE` back to `r2`, restart, and click
   download. That is exactly the blue/green window `handouts.py:680-683` describes, produced
   without hand-editing a row. Today: a 500 that surfaces in the browser as a **CORS error**,
   naming the wrong subsystem. After: the file downloads.

**Also confirm what each fix did not break, by eye.** After (1), send a follow-up in an
existing thread and confirm it still lands in that thread. After (3), download a normal keyed
handout and confirm the network tab still shows a **302**, not a 200 — `78c` asserts it, but
this is the thirty seconds that catches a fix which quietly routed everything through
Postgres.

---

## 6. Risk register

**Read this preamble before the table.** The previous change set's register
([14-admin-observability/PLAN.md §8.2](../14-admin-observability/PLAN.md)) carries a scar that
governs how this one is written: **R4 predicted the wrong tell, in the direction that hides the
bug.** It predicted `api_usage` rows with `user_id = NULL` — rows you could find by querying
for them. What actually happened was that no row was written at all, summing to `0.0`, reading
as a quiet week. And the harness case R4 prescribed **was never written**, so the risk fired
unseen and was found by hand, after the change set was pushed.

Two rules follow, and both are enforced in the table below:

- **A tell that names a row you could query is worthless if the failure produces no row.**
  Every tell here is stated as something an observer would actually see, including "nothing"
  where that is the truth.
- **Every row names a harness case id.** A mitigation without a case is a wish, and this
  register is the place that has already proved it.

| # | Risk | The tell — what you would actually observe | Case that catches it | Mitigation |
|---|---|---|---|---|
| **R1** | **Double-write.** Feature 02's `finally` fires on the SUCCESS path too, and every normal turn's spend is recorded twice | **There is no tell.** No error, no log line, and both totals are plausible — the console just reads roughly 2x. This repo has had exactly this bug once (`context.py:130-160`, the eval-job/`run_turn` double-count) and it was found by reasoning, not by observation | `metering_check.py` **14c**, which asserts BOTH halves in one case: flag unset → N rows, flag set → **zero** | §3.5's flag, set on the line after `await db.commit()` returns and nowhere else. Note that case **13** (the AST case) is passed perfectly by a `finally` that always persists — it cannot see this |
| **R2** | The commit flag is set too early (on the persist running) — or late, or deleted | The console looks correct on every failure except the one the fix was built for — a **failing commit**, which is also the rarest and the least likely to be reproduced by hand | `metering_check.py` **16** (AST over `_run_turn`: exactly one `await db.commit()`, the statement immediately after it is `receipt.committed = True`, and nothing else assigns it) and **16b** (`rows_written` is written once, never inside the persist's `except`). **CORRECTED after the first review**: this cell said *"14a run with the flag set at the wrong point"*, which is unsatisfiable — 14a constructs its own receipt and never executes `_run_turn`, so it is green under every position of that line. Deleting the line was measured leaving 13/13b/14a/14b/14c/15 and `admin_check.py` 5/5b/5c/5d all green while every successful turn double-wrote. Feature 02 §7.2 carries the mutation runs | The flag keys on the commit, not the persist. NOTE what shipped: `persist_quietly`'s return was **not** widened (file ownership, feature 02 §6b), so `_run_turn` calls `persist` inside its own `try` and counts the records it handed over. `persist_quietly` is consequently dead code |
| **R3** | **A second concurrent pool connection.** `run_turn`'s callers hold the turn session open across the new `finally` — the FastAPI dependency in `ask.py`/`conversations.py`, and `stream.py:200`'s own `async with`. None of the four `finally` precedents shares this: `rag/jobs.py`, `eval/jobs.py`, `handouts/jobs.py` and `_suggest_job` all close their session first | Pool exhaustion, and **it surfaces as latency and timeouts rather than as an error naming the pool**. Worst exactly when demanded: an OpenRouter `404 No endpoints found` storm — every turn failing at once — which CLAUDE.md records this project hitting three times. `pool_size=5, max_overflow=5` on one uvicorn worker (`db/session.py:11-18`), and `stream.py:331-334` already names that cap as reachable | No harness can prove this; it is a load property. **The mitigation is the assertion**: the `finally` guards on `if records:` and awaits nothing else, so the second session is opened only when there is something to write and is held for one insert. Feature 02 states the bound in a comment | Guard on `if records:`. Do not await anything else inside the `finally`. Record the reasoning: the `finally` SHAPE transfers from the four precedents, the connection-cost reasoning does not |
| **R4** | **An unconditional address revert orphans a live conversation.** A turn that committed and then raised — four real code paths, §3.4 — has its id thrown away by the client | The user's thread silently vanishes from the sidebar and the answer they were reading is stranded in a row nothing points at. **Reads as data loss, not as a bug in a revert** | **AC2** — green today, red under an unconditional revert | §3.4: revert only on an `error` frame AND only after `chat.list` has failed to return the id |
| **R5** | **AC1 is satisfied by DELETING the promotion** — `setActiveId`/`activeIdNow` inside `onStart`'s `if (wasDraft)`, cited as `AgentChat.tsx:601` when this was written and moved by the fix. A build with no promotion at all never strands an id | None. Every assertion in AC1 passes, and the sidebar is merely wrong for the whole of every new thread — which is the behaviour the promotion was added to fix in the first place | **AC3** — a stream ending in `done` keeps the promoted id, asserted via **the address the second send used**, not via component state | The AC1/AC2/AC3 triple is indivisible. Same shape backend-side: **S37 without S38 is passed by never creating the row at all** |
| **R6** | **AC1 goes red for the wrong reason and the red looks like a caught bug.** jsdom has no `Element.prototype.scrollIntoView`, which `AgentChat.tsx` calls from `scheduleScroll` and from the turn-level scroll effect, so an unstubbed render throws before any assertion runs | A red row whose message names `scrollIntoView`, or a render error — easy to skim past as "the defect" | The protocol, not a case: **confirm AC1 fails on its own assertion text before writing any fix.** Record the failure message in the feature file's as-built notes | Stub `scrollIntoView`. Mounting `AgentChat` also pulls `SourceRail`, `HandoutsPanel` and `useAgentDocuments`, all of which reach `lib/api.ts`; a `vi.mock` that misses a member leaves an unhandled rejection that can fail an unrelated assertion later in the file **or pass one for the wrong reason** |
| **R7** | **AC5 asserts the wrong thing** — that the address reverted after the poll budget expires | Split threads, again: `settleAddress` gives up at 20 attempts x 3 s ≈ 60 s, and CLAUDE.md measures persona turns at 30-60 s. On the **abort** path the turn is still running and will commit, so give-up is not evidence of rollback | **AC5 as specified**: it asserts *the composer is usable again*, never *the address reverted*. That is the property that matters — the user must not be locked out of their own agent | §3.4's rule that hole (a) and hole (b) do not share a code path. The abort path settles and, on give-up, releases; it never reverts |
| **R8** | **The `--live` red run aborts instead of failing.** `httpx.ASGITransport` defaults to `raise_app_exceptions=True`, so defect 03's `MissingGreenlet` propagates out of `storage_check.py` | A traceback and **zero recorded failures** — a file that ends with no red rows, which reads as green-by-abort | §3.6, enforced when 78/79b are written and watched failing | `raise_app_exceptions=False` on every live transport that expects a 5xx |
| **R9** | **`storage_check.py --live` passes vacuously.** Run with `STORAGE_ROUTE=postgres`, `with_content=True` and defect 03 is unreachable, so 78 is green having measured nothing | A green suite on a road the defect cannot exist on. **The tell is the absence of a printed route**, which is why printing it is mandatory rather than nice | **78 / 78b print NOT MEASURED, loudly, on the postgres road** (§3.1, §3.6) | Read `settings.storage_route`, print it, gate the cases on it |
| **R10** | **The fix for 03 loads `content` unconditionally**, and the entire cost the R2 route exists to remove comes back | None on the download itself — every byte still arrives. Silent bytea reads through a single uvicorn worker | **78c** (a keyed `ready` row still answers 302, exactly one `presigned_get_url` call) and **78b**'s keyed leg (no `handouts.content` in captured SQL) | Load `content` only after the R2 branch has been declined, and before `:684`'s `content is None` guard — §3 of feature 03 |
| **R11** | **`Handout.content` gets un-deferred on the mapper** as the "simple" fix | `conversations.py:501-513` replays a thread by selecting whole `Handout` ORM objects with no `undefer`, so **every message replay starts dragging bytea**. Reads as the app getting slower, months later, with no change to blame | **77** (`Handout.__mapper__.attrs['content'].deferred is True`, read off the MAPPER) and **77b** (`_load_owned`'s `with_content` still defaults to `False`) | Both are green today and exist only to go red under the wrong fix — build.md rule 3 |
| **R12** | **Live fixture rows leak into production, or land in a real user's account in the first place.** `storage_check.py --live` and `metering_check.py --db` insert into shared tables | A stranger's handout in the panel, with no indication it came from a harness — and, as SHIPPED and caught in REFINE, an agent written into whichever real user `select(User).limit(1)` happened to return | §3.6's rule, and D1/D3's rollback | **Three layers, and the first is not optional. (1) The fixture is owned by a user the harness CREATES — `google_sub = "storage-check-local"`, per `slice_check.py` and `ui_check.py`. NEVER `select(User).limit(1)` in a mode that writes. (2) Cleanup in a `finally`, keyed on the inserted ids AND on that owner. (3) A `--cleanup` sweeper, because a `finally` does not survive a hard kill or a lost connection — naming a leak makes it findable, only a sweeper makes it removable** |
| **R13** | **Case 13 is unfalsifiable** — an AST case is satisfied by the SHAPE, not the behaviour. `finally: pass` passes it; so does a `finally` that opens a session and persists an empty list | It goes green after the code is written and proves only that the code was written — precisely what build.md rule 2 forbids | **13b** (the finally must open its own `SessionLocal` and must NOT pass `query_id=`, derived from source over all five sites, never from a hardcoded list) plus **D1/D2/D3**, which execute real SQL | Watch 13 go red naming `ask.py:620` before writing anything. A layer-1 harness cannot prove a query runs, only that it was written |
| **R14** | **The dead-id send is misread as legible.** `api.ts:470` treats the stream route's 404 as "this API has no such route" and re-issues on the JSON route, which 404s identically — two round trips, one banner | The user's error cannot distinguish a dead conversation from a stale deploy, so **the symptom points at the wrong subsystem** — same shape as defect 03's CORS error | AC1 asserts the second send goes to `askNewStream` and **never** to `askStream("c-new", ...)`, so the fallback is never entered | Repair the address; do not rely on the error being readable. `api.ts` is not changed — the fallback at `:465-470` is documented, intended behaviour |

---

## 7. What this change set deliberately does NOT do

Written so the deleted work does not return, and so a reader can tell a decision from an
oversight.

- **It does not change `backend/app/api/stream.py`.** `stream.py:206-226` creating the
  conversation inside its own session and letting the session roll back is *correct*. The
  window it opens is real and the client is what handles it wrongly. Feature 01's S37 exists to
  assert that the server behaves this way and to keep it that way; it is not a step toward a
  server-side change. **Reversal condition:** if the client ever needs the id to be durable
  before the turn ends, that is a different design (commit the conversation row separately,
  before the turn) with its own orphan-row problem, and it needs its own plan.
- **It does not revert an address on the abort path.** §3.4. The turn is still running and
  will commit; reverting would split the thread across two rows. Hole (b) settles, as it does
  today, and AC4 asserts the banner and the disabled Send that make settling visible.
- **It does not touch `frontend/src/lib/api.ts`.** The `error` frame already arrives as a
  thrown `ApiError` with the right shape (`api.ts:544-562`), and the 404 route-fallback at
  `:465-470` is documented, intended behaviour. Fixing the two-round-trip cost is a separate
  question and is not this change set's (R14).
- **It does not add a sweeper for rows stuck at `pending`.** CLAUDE.md records a real one — a
  wedged deck generation whose row sat at `pending` for 26 minutes and is still there, because
  `_settle` runs in the job's own `finally` and a job that never returns never reaches it. That
  is a genuine gap, it is on a failure path, and it is **not** one of these three. Adding it
  here would mean a background sweeper, a schedule and a staleness threshold — a change set,
  not a fix.
- **It does not add a price table, a new `call_kind`, or a new provider.** Feature 02's rows
  go through the same `to_row`, so `estimated_cost` still never lands in `cost_usd`
  (`metering_check.py` 10d/10e/10f) and `CALL_KINDS` is unchanged (11a/11b).
- **It does not claim cancellation coverage.** The four `finally` precedents exist partly
  because `CancelledError` is a `BaseException` (`handouts/jobs.py:948-957`). `stream.py:301-329`
  states the opposite for turns: on client disconnect *"the turn runs to completion and commits.
  Nothing is cancelled."* So `finally` is still the right construct here, but its justification
  is an ordinary raise — an OpenRouter 404, a sandbox failure, a failing commit — and **no case
  in this change set cancels anything**, so no feature file may claim it.
- **It does not un-defer `Handout.content`, drop the column, or change `HandoutOut`.** The
  column is the object-storage rollback road and S11's subject (`storage_check.py` 76). R11.
- **It does not change the download contract.** 409 for not-ready, 302 for keyed-and-ready,
  200 with a quoted CRLF-free `Content-Disposition` for the Postgres fallthrough. The frontend
  (`HandoutCard.tsx:216-227, 325-327`, `types.ts:289-300`) gates on all three and none of them
  moves.
- **It adds no setting, no migration, no trace event.** §3.1, §3.2, §3.8. Stated three times
  because each is a place a feature file would otherwise be tempted to add one quietly.
- **Nothing here is model-decided**, so no [loop-prompt.md](../loop-prompt.md) session and no
  new tool. There is no place in these three fixes where a model chooses anything;
  [loop.md](../loop.md) is cited throughout as T2 (*trigger on the absence of the outcome you
  wanted*) because all three defects are instances of it, not because any of them needs a
  trigger designed.
- **It does not commit, push, or touch git.** Everything lands in the working tree.

---

## 8. As built — where the plan was wrong

*(Empty until the change set ships. [build.md §3](../build.md): this is the highest-value
section in a plan file because it is the only one written with hindsight. Fill it after
verification, and put the harness counts here rather than in a separate status file.)*

---

## As built — where the plan was wrong

Written after the refine pass, per [build.md](../build.md) §9.

### The plan was right about the defects and wrong about what would prove them

All three fixes were built harness-first, every acceptance case watched failing before a line
of source changed, and every suite went green: **280 backend assertions across 10 harnesses,
56 frontend tests across 7 files.** By this project's own standard that is a finished change
set.

An adversarial review then deleted one line at a time and re-ran. **Three lines were guarded
by nothing:**

| Deleted | Suite | What was actually true |
|---|---|---|
| `receipt.committed = True` (`ask.py`) | green | every successful turn wrote its usage **twice** — this repo's own named double-count scar, unpinned |
| the fourth revert gate (`AgentChat.tsx`) | green | the revert fires on a thread the user has since navigated away from |
| the `else:` on the download fallthrough (`handouts.py`) | green | every R2 download reads the bytea the route exists to avoid |

Each carried a comment explaining why it was load-bearing. Every reader agreed it was
load-bearing. Only deletion showed that nothing would notice if it went. **That is now
[build.md](../build.md) §7's mutation step**, and it is the durable output of this change set —
worth more than any of the three fixes.

### The review also caught the two things that could have done real damage

Both new write modes borrowed a **real user**:

```python
select(User).limit(1)      # no ORDER BY -- whichever of 15 real people Postgres returns first
```

`storage_check --live` would have attached a `ready` handout to a stranger's dashboard;
`metering_check --db` committed fabricated spend into the live accounting table with
best-effort, silently-failing cleanup. Neither was ever run against production — verified by
counting back to the baseline of 15 users / 464 `api_usage` rows / 10 handouts — but neither
could ship. Harnesses own their own fixture users now, `storage_check` grew an independent
`--cleanup`, and the rule is in [CLAUDE.md](../../CLAUDE.md) under *Harnesses that write to
the database*.

### Where the review itself was wrong

Recorded because a reviewer is not more reliable than an auditor, and this file's own §6 warns
against trusting a predicted tell.

- The claim that the production handout count *"had already drifted to 4"* **did not
  reproduce** — counted three times, read-only, before and after: 10 handouts, 2 keyless, both
  `failed`, zero ready-and-keyless. The most likely explanation is a count taken while a
  `--live` run's own three fixture rows were in the table. The underlying rule (a live row
  count in a comment is perishable) is right regardless, so the change was made anyway.
- The claim that the fix *"routes every `status === 0` failure into `settleAddress`"* is
  wrong: the whole block is under `if (promotedAddress)`, so a status-0 failure in an existing
  thread settles nothing. The cost falls on new threads only.

### Verification, as run

```
metering_check      42 offline  ·  --db D1-D4 green, production counted back to baseline
storage_check       19 offline  ·  --live 25  ·  --cleanup sweeps and is idempotent
agent_loop_check     9   deck_check 50   llm_check 32   refusal_check 38
route_specialist_check 40   ledger_check 10   sandbox_check 22   admin_check 18
frontend            56 tests / 7 files  ·  tsc -b clean  ·  vite build clean

20 mutation proofs, each verified by applying the mutation, capturing red, restoring,
capturing green. Two re-verified independently by the orchestrator: deleting
`receipt.committed = True` reddens case 16; `except BaseException` -> `except Exception`
reddens 17a and 17b.
```

**Not run: `agentic_check.py`.** It needs `--setup`/`--run`/`--cleanup`, makes real model and
Cohere calls, and takes ~10 minutes. Justified by proof rather than by cost for defect 03 —
`ast.dump()` of `handouts.py` is identical to its pre-review baseline, so those edits are
comment-only. Defects 01 and 02 changed behaviour and it is the suite to run once at ship.
Open items 51-55 in [PRD.md](../../PRD.md) carry what is left.
