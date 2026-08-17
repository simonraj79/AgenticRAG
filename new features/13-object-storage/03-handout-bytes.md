# 03 — Handout bytes move

The centre of the change set. Two writers, one reader, three delete paths, one backfill — and one
transactional guarantee that is free today and has to be paid for afterwards.

---

## What the user gets

Nothing visible, and that is the acceptance test. Download still hands over the same file under the
same name; the panel still lists the same rows at the same speed. What changes is where the bytes
were on the way, and what a full agent costs the database: `handout_max_per_agent = 200` against
`sandbox_max_artifact_bytes = 5 MB` (`config.py:578`, `:593`) is a nominal **1 GB of `bytea` per
agent**, and the live total across every agent is 159,639 bytes in 6 rows (`PLAN.md` §3.6). The
ceiling has never been approached, and the column is the only reason it exists.

---

## Writer 1 — the recipe job, one commit and nothing else in it

`jobs.py:839-861`. `handout.content = content` at `:839`, then `byte_size`, `filename`,
`mime_type`, `preview_text`, `source_code`, `meta`, `status="ready"`, then `await db.commit()` at
`:861`. The job opened this session itself, so the transaction contains exactly this row.

§3.5's ordering is a straight insertion: put the object at the derived key before `:839`, assign
`storage_key` beside `byte_size`, leave the commit where it is. Step 4's best-effort cleanup has a
home already built for it — the `except Exception` at `:871-877` catches everything, logs, and
lets nothing escape, which is precisely the discipline §3.5 requires of the delete (*a failed
cleanup must not turn a recoverable turn into a failed one*).

`byte_size = len(content)` at `:840` stays and gains weight. It becomes the **only** record of a
size the database can no longer compute — `octet_length(content)` is NULL for every row written
after this — and §3.6 measured the two agreeing exactly on all six live rows, which is what makes
the denormalised value trustworthy going forward.

---

## Writer 2 — the chat/tool door, inside the turn's transaction

`ask.py:1084-1111`. Rows are built in a loop over `result.artifacts`, `content=artifact.content`
at `:1099`, `db.add(row)` at `:1110-1111`. **Nothing commits here.** The turn's single commit is at
`ask.py:1297`, after the trace and the query.

The comment at `ask.py:1071-1074` states the guarantee:

> *"Written into THIS transaction, alongside the query and the trace. If the turn rolls back so do
> its handouts, which is the only correct outcome: an artefact attributed to an answer that was
> never stored is orphaned, and it would be orphaned holding megabytes."*

**That guarantee is currently free.** It costs no code because the bytes and the row are one write
— a rollback un-writes both because there is only one thing to un-write. After this change there
are two stores and one of them has no rollback, so the same sentence has to be *paid for* rather
than inherited. §3.5 is the payment, and its ordering answers the question this comment already
asked and answered in the easy direction: a row with no object is visible in the panel and
deletable; an object with no row is unreachable, because §3.3's key is derived from a `uuid4()`
that was never committed and now exists in no table.

The id is generated in Python at `ask.py:1085`, which is what makes the key derivable before the
row exists. The recipe door does the same at `handouts.py:509`, and `:506-508` already explains
why it is explicit rather than a column default — that reasoning now has a second beneficiary.

The comment at `ask.py:1076-1079` — *"Bytes go in Postgres rather than object storage. No bucket is
provisioned (PRD open item 10)"* — becomes false in the same commit that makes it false.

---

## The read path — `handouts.py:590-637` becomes a 302

What stays, exactly: the path, the cookie authentication, `_load_owned`'s WHERE-clause agent filter
(`:324-327`), and the **409 on a non-`ready` row** (`:615-623`) with both of its detail strings.

What changes: `Response(...)` at `:625-637`. Its three parts split three ways.

| Today | After |
|---|---|
| `:632` `Content-Disposition: attachment; filename="{_safe(handout.filename)}"` | the `response-content-disposition` presign parameter (§3.4) |
| `:627` `media_type=handout.mime_type` | the `response-content-type` presign parameter |
| `:635` `Cache-Control: private, no-store` | **unreproducible.** §8 R-7, an accepted loss |

`with_content=True` at `:613`, and the `undefer` it reaches at `:328-329`, are not deleted —
`storage_route="postgres"` is still a road and this is still its read path. But the docstring at
`:320-322` (*"the ONLY place `content` is ever undeferred"*) becomes route-conditional and must say
so rather than be left standing.

The `handout.content is None` half of the `:615` guard becomes `storage_key is None`, and the
argument at `:607-611` transfers verbatim: it is checked *alongside* the status rather than
trusting the status alone, because this is the one place a missing value would reach the response
and produce a downloaded file that is not a file.

### `_safe()` moves. It does not retire.

`handouts.py:243-291`. Its docstring is the argument, and every clause of it survives the move:
the input is model-written and *"attacker-influenced from two directions at once"* (`:246-251`),
the rule is an **allowlist** rather than a denylist (`:258-262`), and an unescaped `\r\n` or `"`
is header injection.

**R2 emits the header now instead of FastAPI, and nothing about that changes who wrote the
filename.** A presign parameter that becomes a header at the far end is a header value with an
extra hop. Dropping the sanitiser because "we no longer set a header" is exactly the failure §8 R-6
exists to name in advance. Two incidental properties matter *more* than before, not less: no `/`
or `\` survives the character class and leading dots are stripped (`:266-269`), and the value is
now also a URL query parameter.

---

## Three delete paths, and one of them is the reason this feature is risky

§3.7 owns the table. What each site needs, and what the audit found there.

### 1. One handout — `handouts.py:645-667`

`delete_object(key)` before `db.delete` at `:665`. The docstring at `:649-657` is the feature's own
subject and is false the moment this ships: *"Delete one handout. Row only -- there is nothing else
to clean up"*, and *"Here there is no external store. A handout's bytes live in its own row and
nowhere else."* Rewritten, not left.

The rest of it survives intact and does not need special-casing. A `pending` handout stays
deletable (`:659-663`) and has no `storage_key`, so the object delete is a no-op by data rather
than by an `if`.

### 2. An agent — `agents.py:1175-1183`. **This is the one.**

`delete_agent_objects(agent)` immediately after `delete_agent_namespace(agent)` at `:1171` (§3.7).

Why this path leaks silently where the others cannot. The delete is a Core
`sa_delete(Agent).where(...)` at `:1183`, and the comment at `:1175-1182` records that this was
chosen deliberately so **Postgres performs the FK cascade in one statement** rather than the ORM
walking relationships. And **there is no `relationship()` on `Handout` anywhere in
`app/db/models.py`** — `Agent` declares `documents`, `Document` declares `chunks`, `Handout`
declares nothing and nothing declares it. So on this path no Python object for a handout is ever
constructed, no per-row hook can exist, and there is nothing to iterate. The rows vanish; every
object under `agents/{agent_id}/handouts/` survives with **nothing left anywhere to enumerate it
from**, because the key is derived from an id that no longer appears in any table.

`delete_agent_namespace` at `:1171` is the precedent to mirror in shape, not merely in position:

- Its route docstring at `agents.py:1147-1155` already states VECTORS FIRST, ROWS SECOND and argues
  from the recoverability asymmetry §3.7 adopts — *"Only one of those is recoverable, so the
  recoverable one is what a failure is allowed to leave behind."*
- `delete.py:100-108` swallows `NotFoundException` because an agent that never ingested has no
  namespace to delete, and *"raising would make deleting an empty agent fail for precisely the
  reason that guarantees it is safe."* The prefix delete needs the same tolerance for an agent that
  made no handouts.
- `delete.py:11-14` explains why the namespace is never a parameter. §3.3's prefix is derived from
  `agent.id` for the same reason and by the same means.

**A second leak site the plan does not list, found reading the harness.**
`scripts/agentic_check.py:492-523`'s `cleanup()` does **not** go through the route. It issues its
own Core deletes — `delete(Handout).where(Handout.agent_id == agent.id)` at `:510`,
`db.delete(agent)` at `:518` — so `--cleanup` would leak every object the suite ever created, on
every run. It needs the same prefix delete, and this is also the reason **A8 must exercise the API
route rather than the cleanup helper**: they are two different deletes and only one of them is the
product.

### 3. One document — `delete.py:27-82`

Feature [04](04-upload-bytes.md)'s, not this one's.

---

## The backfill — `scripts/migrate_bytes_to_r2.py`

§3.8. For every `handouts` row with `content IS NOT NULL AND storage_key IS NULL`: put, then set
the key. Idempotent by that predicate — a second run selects nothing, which is the repo's
provisioning convention (detect, verify, report drift, never recreate) expressed as a `WHERE`
clause. `--dry-run` reports the set it would move without moving it.

**It never deletes `content`.** That is §3.6's blue/green rule expressed as a script property
rather than as an instruction: `storage_route="postgres"` is only a real road while the bytes are
still there, and a backfill that tidied up would close the rollback in the same command that opened
the new store. §8 R-10 is the same fact from the other side — running this against production while
the route is still `postgres` is harmless *by construction*, because it only ever adds.

Scale, per §3.6: 6 rows, 159,639 bytes, largest 34,926. It finishes in seconds. It is written for
correctness rather than throughput, and the reconciliation half (`--orphans`, §8 R-1) is the part
that will still be needed when the row count is not six.

---

## Comments this feature rewrites rather than leaves

A comment giving a dead reason is worse than none — §7.5 argues that about the quota and it
generalises to all six.

| Site | What it says | What it becomes |
|---|---|---|
| `handouts.py:649-657` | *"Row only -- there is nothing else to clean up … here there is no external store"* | False, and it documents the delete path that must now reach one |
| `handouts.py:469-471` | *"Handout bytes live in Postgres and nowhere else (PRD open item 10 tracks object storage), so an eviction here is a permanent deletion"* | The premise dies; the conclusion (refuse, never evict) survives and needs a new reason |
| `ask.py:1071-1079` | *"If the turn rolls back so do its handouts"* + *"Bytes go in Postgres rather than object storage"* | The first stops being free (above); the second is simply false |
| `models.py:800-807` | The `deferred()` argument — *"a `SELECT *` that eagerly loads bytea returns tens of megabytes … two independent guards"* | Still true, still load-bearing on the rollback road. Rewrite to say `content` is a legacy column that must **stay** deferred, not that the pressure is gone |
| `config.py:586-593` | *"Bytes live in Postgres (no object storage is provisioned -- PRD open item 10), so this quota is a storage bound, not a policy"* | §7.5: the justification dies, the number stays, the comment must say why it stays |
| `HandoutsPanel.tsx:71-74` | *"the `content` column is `deferred()` server-side so 200 rows do not drag tens of megabytes of bytea along with them"* | `LIST_LIMIT = 200` stays. Its stated reason becomes a legacy-column fact; **A5 is what keeps the live property asserted** |

The last is the only one outside `backend/` and it is not user-visible copy — `AgentDocuments.tsx`
is feature 04's problem. Changing it changes no behaviour, and `LIST_LIMIT` stays at 200.

---

## Contracts consumed

By reference into [`PLAN.md`](PLAN.md): **§3.3** (the key scheme; the id is generated at
`ask.py:1085` and `handouts.py:509`, and the filename never enters the key), **§3.4** (the download
contract, both presign parameters, and that no frontend change is required), **§3.5** (object
first, row second; best-effort cleanup that logs and never raises), **§3.6** (the single migration
for the whole change set — *this feature defines none* — and why `content` is not dropped),
**§3.7** (all three delete paths), **§3.9** (`error_kind` gains `storage`; no new trace event and
no new `queries` column).

---

## Acceptance criteria

| # | Criterion | Harness case |
|---|---|---|
| **A5** | The list route makes **zero** object-storage calls | `agentic_check.py` **S11** (rewritten) |
| **A6** | A recipe handout downloads and **opens** through the redirect | `agentic_check.py` **S8** ×4 |
| **A7** | A **chat-made** handout's bytes are retrievable end to end | `agentic_check.py` **S34** (new) |
| **A8** | Deleting an agent leaves zero objects under its prefix | `agentic_check.py` **S35** (new) |

**A5.** S11 today (`:2292-2329`) captures `sqlalchemy.engine.Engine` statements around a `GET` with
`limit=200` and asserts `"handouts.content"` appears in none. §1.6 records why that inverts once
the column stops being read. It already carries an `unmeasured` floor for an empty capture
(`:2317-2325`) — the same defect one layer up, and it stays. Feature 01 owns the rewrite to the
positive property; this feature must not regress it.

**A6.** S8 (`:2164-2289`) makes all four recipes and **opens** each artefact — `artifact_problem`
at `:2228` is what "opens" means, and it is the check a 27,387-byte zero-slide `.pptx` fails. Its
download half is S8b at `:2244-2253`, which reads `content-disposition` off the response and
asserts no CR, no LF, and a `"` present. **That is `_safe`'s contract asserted end to end, and it
must survive the move to a presign parameter** — meaning S8b now reads the header off the
*followed* response. Feature 01's `follow_redirects` fix (§8 R-3) is what makes that possible;
without it these four go red simultaneously for a reason that has nothing to do with them.

### A7 — read this one twice

**No scenario in this repository has ever downloaded a chat-made handout.** Three pieces of
evidence, all checked:

- `deck_check.py` cases **60–64** stop at `ctx.artifacts`. `run_tool` (`:1931-1962`) returns
  `[a.filename for a in ctx.artifacts]`, and case 62 (`:2057-2067`) asserts
  `kept62 == ["good.csv"]`. That is the tool's own in-process context object, before anything is
  persisted at all.
- `agentic_check.py` **S32** (`:1892-1934`) and **S33** (`:1962-1984`) read rows straight out of
  Postgres — `select(Handout).where(Handout.query_id == out.query_id)` — and count them. S32's own
  comment at `:1907-1911` is explicit that the payloads decide only whether a deck was *attempted*,
  and *"the database below is what decides whether one was kept."* Neither ever asks for bytes.
- **S8b**, the one download assertion in the suite, takes `ready[0]` from the four **recipe**
  handouts (`:2235-2245`). `origin="tool"` rows are not in that list and never have been.

So `ask.py:1084-1111` — the writer whose transactional guarantee this feature has to rebuild by
hand, the one whose commit it does not own — is covered end to end by nothing. **S34** asks a
question that makes a file through `run_python`, finds the row it produced, `GET`s
`.../download`, and asserts the returned bytes open as their declared type.

Write S34 and watch it run **before** the redirect exists. On `storage_route="postgres"` it should
pass immediately, and that is the useful part: it proves the scenario reaches the row and the bytes
at all, so a later red is attributable to the storage change rather than to the scenario.

**A8.** S35 creates a throwaway agent, makes at least one handout on it, calls
`DELETE /api/agents/{agent_id}`, lists the `agents/{agent_id}/` prefix and asserts it is empty.
Three properties, each ruling out a way of passing while proving nothing:

- **It goes through the route** (`agents.py:1141-1186`), never through `cleanup()`. Two different
  deletes; only one is the product.
- **It asserts the prefix is empty**, not that a delete call was made. A `delete_agent_objects`
  computing the wrong prefix satisfies "an object-storage delete happened" perfectly.
- **It creates the object it expects to be removed.** An agent with no handouts leaves zero objects
  under its prefix trivially — the shape S3 had when it went green twice while proving nothing
  (`build.md` §7). A scenario that does not first make a handout measures the empty set.

---

## What this deliberately does not do

**It does not define a migration.** §3.6 owns the one migration for the whole change set,
`down_revision = 'd4e91c2a7b58'`, adding both `storage_key` columns together. A second revision in
this folder would give feature 04 something to chain off and leave the change set with two heads.

**It does not drop `handouts.content`.** §7.2 and §3.6. Blue/green, not delete-then-create — the
rule `migrate_index.py` already applies to Pinecone, whose procedure ends *"Only then delete the
old one by hand."* Dropping it here would also invert S11 per §1.6: a test whose referent has been
removed passes forever.

**It does not introduce a status.** §3.5 and §8 R-4. `_settle` (`jobs.py:1194-1195`) returns
without touching any row that is not `pending`, so a `stored` or `uploading` state between
`pending` and `ready` walks straight into that guard. A handout is `pending` until it has both an
object and a row, then `ready`.

**It does not make `byte_size` an integrity check.** It stays a stored column written at
`jobs.py:840` and read by the panel (`models.py:788-790`). Comparing a downloaded length against it
would be an error-shaped assertion of exactly the kind that let a 28-byte fake `.pptx` through as
`ready` — A6 and A7 open the artefact instead.

**It does not change the frontend.** §3.4: both consumers are URL-only. The single frontend edit is
a comment (`HandoutsPanel.tsx:71-74`).

**It does not replace what `Cache-Control: private, no-store` (`:635`) was doing.** §8 R-7 records
it as an accepted loss mitigated only by `r2_presign_ttl_s`. Do not put a `Cache-Control` on the
302 and call it equivalent — the 302 is not the response carrying the bytes.

---

## What must keep working

- **The 409 on a non-`ready` row**, with both detail strings intact (`handouts.py:615-623`).
  `HandoutCard` gates on that behaviour (§3.4).
- **`_load_owned`'s WHERE-clause agent filter and its 404-not-403 argument** (`:306-318`). The
  presign is issued *after* that check, inside FastAPI — which is the entire reason a private
  bucket is the correct shape (§1.5) and a public one is closed.
- **`_safe()` unchanged as a function.** It gains call sites; it does not gain a URL-encoding
  responsibility, a new signature, or a relaxed character class. §8 R-6.
- **A `pending` handout stays deletable** (`handouts.py:659-663`), still the escape hatch for a row
  abandoned by a restart or a deploy mid-job.
- **`content` stays `deferred()`** (`models.py:808`) and stays absent from both response models
  (`handouts.py:225-236`). Two independent guards, both still wanted while the column exists.
- **The recipe job still opens its own session** and still writes a terminal status in a `finally`
  from a second one (`jobs.py:1187-1201`). An R2 put inside `run_handout_job` must not become a
  reason to hold the request's session open.
