# 01 — The storage harness floor

**Ships first, and alone.** Its deliverable is the ability to watch every other feature in this
folder fail. Building `app/storage.py` first means writing a case against code that already
exists, which is how `agentic_check.py` S3 went green twice while proving nothing
([loop.md §5](../loop.md)).

---

## What the user gets

Nothing, directly — this feature is entirely test infrastructure and touches no route. What the
*project* gets is three things it does not have: somewhere to put `PLAN.md` §5's A1–A4 so they
are executed rather than wished, a harness client that survives the first 302 instead of turning
nine assertions red at once, and an S11 that will still have a subject after `handouts.content`
stops being read.

---

## Why this is a feature and not a chore

`PLAN.md` §1.6 names two harness facts that decide the build order, and both are defects that
exist **today**, before a byte moves:

- **Nine assertions download real bytes through a client that does not follow redirects.** They
  go red simultaneously on the first 302 and read as "the migration broke downloads" (R-3).
- **S11 asserts the absence of a string.** Its referent is a column this change set is designed
  to make droppable, and the shape of the defect it guards changes inside this change set
  (§C below).

`build.md` §5 is unambiguous: **add the case, run it, watch it fail, then build.** This feature
is the "add the case" half for the whole change set, plus the two repairs that have to land
before anything can be watched failing honestly.

---

## Technical detail

### A. `scripts/storage_check.py` — a NEW layer-1 harness, cases 71–74

Nothing under `scripts/` imports anything resembling object storage, because nothing resembling
object storage exists (`PLAN.md` §1.4). This is that file.

- **Invocation:** `backend/.venv/Scripts/python.exe scripts/storage_check.py`
- **Needs:** the venv only. **No DB, no network, no model, no subprocess, no bucket.** Target
  < 2 s.
- **Shape:** copy `deck_check.py:81-93` exactly — a `check(name, condition, detail)` helper, a
  `short()` that is ASCII-safe and bounded, a `failures` list, `sys.exit(1)` at the end. Two
  states on purpose; see §E.
- **ASCII in `print()`.** Three throwaway scripts in this repo have been broken by the Windows
  console codepage. `ascii(value)` when you need to see what is there — and a storage key is
  exactly the kind of value that will one day be derived from a CJK filename.
- **Case ids continue `deck_check.py`'s sequence**, whose last case is `64` (`deck_check.py:2169`).
  71–74 leaves 65–70 for that file to grow into. A case id names one case across the whole
  layer-1 ladder, and `PLAN.md` §5 already cites these four by number.

**Every subject of every case is absent on the red run** — `app/storage.py` is feature 02,
`settings.storage_route` is feature 02, `storage_key` is feature 03. So the whole file leans on
§D1's sentinel, and the correct red run is four `[FAIL]` rows naming what is missing, not a
traceback.

#### Case 71 — a derived key never contains a request-supplied string (`PLAN.md` §5 A1)

Two halves, and the second one alone is satisfied by a function that returns a constant:

- **Positive.** `key(agent_id, handout_id, mime)` matches
  `^agents/<uuid>/(handouts|documents)/<uuid>\.[a-z0-9]{1,8}$`, both uuids round-trip out of it,
  and the extension comes from the mime table. This is what separates a correct derivation from
  an absent one — the `route_specialist_check.py` 25/26 pairing, which exists because a deleted
  detector passes every test that only checks bad input is rejected.
- **Adversarial.** The hostile filenames this repo already keeps — `'a"; rm -rf /'`
  (`handouts.py:264`, named in prose at `04-handouts-panel.md:405`), `"../../etc/passwd"`, a
  name carrying `\r\n`, a pure-CJK name — are passed **alongside** the ids, and no substring of
  any of them appears in the returned key.

**And the strongest form is structural, not behavioural.** Assert with `inspect.signature` that
the derivation takes `uuid.UUID` parameters and no `str` that could come from a request body.
`Agent.namespace` is a derived property and `SearchCorpusArgs` carries one field for the same
reason (11 §1): the control is the **absence of the parameter**, never the sanitising of one. A
case that only proves hostile strings are stripped is a case that licenses adding the parameter
back with a filter on it.

#### Case 72 — `_safe()` reaches `response-content-disposition`, CRLF-free and quoted (A2, R-6)

This is the case that stops `_safe` being quietly retired the moment FastAPI is no longer the
thing emitting the header. Presigning is a **local signing operation** — no request leaves the
process, which is what `PLAN.md` §1.1 measured when a presigned URL fetched with no credentials
returned 200 — so this is layer 1 honestly, and on the red run it runs against §D2's fake.

Over the same hostile filename set:

- The value handed to `ResponseContentDisposition` is exactly
  `attachment; filename="{_safe(name)}"`, with `_safe` **imported from `app.api.handouts`**
  (`:243`) rather than reimplemented. A second copy of the sanitiser is the marker-list defect
  arriving in a new module, and the list in this repo has already been wrong five times.
- No `\r`, no `\n`, exactly two `"` in the value.
- **Percent-decode the parameter back off the generated URL and assert on the decoded form.**
  The encoding is not the control; the sanitiser is. Asserting the encoded string alone passes on
  an unsanitised name that happened to encode cleanly, which is the same shape as reading
  `supported_parameters` instead of probing.
- **Negative control:** a filename `_safe` collapses to nothing — `"..."` — yields
  `filename="handout"` (`handouts.py:291`), never `filename=""`.

#### Case 73 — a rollback after a put deletes the object (A3, §3.5, R-1)

Ordering is the assertion, so §D2's fake records `(method, kwargs)` in a **list**, not a counter.
Three sub-assertions:

- **Commit path:** put, set key, commit → exactly one `put_object`, zero `delete_object`.
- **Rollback path:** put, then the commit raises → one `put_object` and one `delete_object`
  **of the same key**, in that order.
- **The cleanup is best-effort, and that is the half worth writing.** Make `delete_object` itself
  raise, and assert the *original* exception propagates unchanged — a failed cleanup must not turn
  a recoverable turn into a failed one. §3.5 says so in one line; nothing executes it otherwise.

This case needs the fake session as well as the fake client, because the property under test is
"object, then row, then commit" and two of those three are database-shaped.

#### Case 74 — missing R2 config with `storage_route="r2"` raises at LOAD (A4, §3.1)

Construct `Settings` directly with `storage_route="r2"` and **each of the four R2 values blank in
turn** — four sub-assertions, because a validator that only checks `r2_account_id` passes a
three-field test and fails in production on the one field nobody set.

**The control that makes it non-vacuous:** `storage_route="postgres"` with every R2 value blank
constructs fine. That is the rollback road, and a validator that rejected it would make rollback
unreachable — which is the failure this change set's blue/green rule (§3.6) exists to prevent.

Model it on `config.py:400-436` and read that docstring first. Its argument is the argument here:
*"free text did not deliver that claim"*. It is deliberately **not** `validate_assignment`
(`config.py:423-427`) because `embed_check.py` assigns the route in-process to probe both
branches, and `storage_check.py` will want the same.

---

### B. The `follow_redirects` fix — R-3, and it is not one line

**The sites.** `agentic_check.py:2787-2789` and `deck_rate_check.py:297-298` both construct
`httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", ...)` with no
`follow_redirects`.

**The nine assertions**, all of which `GET .../download` and read `.content`:

| Site | Rows |
|---|---|
| `agentic_check.py:2219`, inside the recipe loop at `:2205` | S8 × 4 |
| `agentic_check.py:2246` | S8b — and it reads `content-disposition` off that response |
| `agentic_check.py:2266-2287` | S8c, which consumes `deck_body` captured at `:2227` |
| `agentic_check.py:2484` | S28 |
| `agentic_check.py:2556` | S29 |
| `deck_rate_check.py:214` | the measurement's own `analyse(body_r.content, run)` |

**`follow_redirects=True` alone is the wrong fix, and it fails in a way that looks like a
different bug.** A client's explicit `transport=` serves **every** URL it is asked for, not only
the ones under `base_url`. Turn redirects on and the client re-issues
`GET https://<account>.r2.cloudflarestorage.com/agents/...` **into the FastAPI app**, which has
no such route: the nine rows go from reading an empty redirect body to reading a 404 body. Same
red, and the second cause reads as a routing defect in the application.

So both clients get `follow_redirects=True` **and** a second transport:

```
mounts={"https://": httpx.AsyncHTTPTransport()}
```

`httpx` consults `mounts` before falling back to the `transport=` argument, so the ASGI transport
stays the default and only genuine `https://` leaves the process. `base_url` is `http://test`, so
**not one of the nine call sites changes** — which is the point: this is the minimal diff that
keeps `.content` meaning what it means today.

**The consequence, which is the part that must be written down: those nine rows acquire a network
dependency they have never had.** R-11 applies to them from now on. They live in
`agentic_check.py`, so they inherit `is_rate_limited` (`:322-337`, `:370-372`) for free — a 429,
502, 503 or a quota phrase from R2 lands `[rate]` and does not fail the suite.

**`403` is deliberately NOT added to `RATE_LIMIT_PHRASES`.** A 403 from a presigned URL is a
defect — a wrong key, a wrong signature, a TTL that elapsed — in every case but one, R-8's token
expiry on 2027-08-17. Widening the phrase list to catch that one case would make every signing
bug green, which is the marker-list mistake running in the opposite direction: not too specific,
too wide. R-8 is made legible by `error_kind="storage"` (§3.9), not by a phrase.

**This fix ships before anything returns a redirect, so it must be a no-op today, and that is its
own assertion.** Run `agentic_check.py --run` once before and once after the two-line change, with
nothing else touched, and the nine rows must be identical. A transport change that alters
behaviour while every route still answers 200 is a defect in the fix.

`deck_rate_check.py` gets the same two lines and nothing else — see §E for why it gets no tier.

---

### C. `agentic_check.py` S11, rewritten to assert a positive property (A5)

**Today** (`:2292-2329`): capture `sqlalchemy.engine.Engine` at INFO around one `GET` of the list
route, grep the captured statements for `"handouts.content"`, `ok = not leaked`. 12/01 already
added the zero-statement guard at `:2317-2325`, which catches the logger going silent. It does
not catch the other way this check can stop having a subject.

**The mechanism, and it is a seventh row for `build.md` §7's table.** The five recorded there are
tests that were too *weak* — an assertion the fixture could not falsify, a measurement confounded
by loop order, a marker list missing a phrasing. This one is different in kind: **the assertion is
correctly written, and its referent can be deleted.** `"handouts.content"` appears in SQL only
while a column of that name is selected. §7.2 drops that column in a later change set; the moment
it does, `leaked` is empty by arithmetic, `statements` is non-empty so the guard does not fire, and
S11 is green forever having measured nothing. Nobody edits it, because nothing about it looks
wrong. **Not a test that could not fail — a test whose subject was removed out from under it.**

**And the near-term half is worse, because it arrives inside this change set.** After feature 03
the defect S11 exists to prevent changes shape: a list route that presigned a URL per row emits no
`handouts.content` SQL at all, and *is* the bug — 200 signings on the cheapest route in the API.
**The old S11 passes on it.** So the rewrite is not housekeeping ahead of a column drop; it is the
difference between an assertion that covers feature 03 and one that does not.

**The rewrite.** S11 counts the object-storage calls the list route makes and asserts **zero**.
Wired the way the SQL capture already is — installed around one request, restored in a `finally` —
by swapping the module-level client `app/storage.py` hands out for §D2's recorder, which counts
every method reaching it, `generate_presigned_url` included.

**Zero is a can't-fail number, which is the same defect one level up:** a recorder that was never
wired counts zero and the row goes green. So S11 returns a **pair**, the
`route_specialist_check.py` 25/26 shape for the third time in this repo:

| Row | Asserts |
|---|---|
| `S11 list route makes zero object-storage calls` | `count == 0` across one `GET` of the list route |
| `S11b the counter is wired` | a control that **must** be non-zero, same recorder, same block |

The control is cheap and needs no `ready` handout: presigning performs no request, so calling the
presign helper once with a fabricated key increments the counter without touching the network.
**If the control reads zero, both rows are `unmeasured`** — a zero on the first row proves nothing
when the instrument is dead.

Two states the row has to get right:

- **`storage_route="postgres"`** makes zero calls on every route, so the control cannot fire and
  the assertion has no subject. Read `settings.storage_route` and report `unmeasured`, never green.
- **The SQL capture stays** as a subordinate second clause while `content` exists (§3.6). It is no
  longer the assertion; it is corroboration, and it retires with the column instead of outliving it.

---

### D. Two patterns reused from `scripts/deck_check.py`

#### D1 — the defensive import sentinel (`deck_check.py:506-534`)

This feature ships before `app/storage.py` and before the migration, so on the run that watches it
fail **every import of every subject fails**. A bare top-level `from app import storage` aborts the
file with a traceback and the four cases go **unreported** — "watch it fail" showing no cases
failing at all, which is the one outcome a red run must not produce. A harness reports; it does not
crash. Copy the shape:

```python
try:
    from app import storage          # noqa: E402
    _storage_why = ""
except Exception as exc:             # noqa: BLE001
    storage = None
    _storage_why = f"{type(exc).__name__}: {exc}"


class _Missing:
    def __repr__(self) -> str:
        return f"<app.storage absent: {_storage_why}>"
```

Both properties stated at `deck_check.py:510-512` are load-bearing. The sentinel is **neither
`None` nor `str`**, so no case passes by luck on an `is None` test or on truthiness; and its
`repr` names the import error, so the red run says *why* rather than reporting a bare absence.

Case 74's subject is an **attribute** rather than a module — `settings.storage_route` does not
exist yet on a `Settings` class that imports fine — so it needs the same treatment through
`getattr(settings, "storage_route", MISSING)`. Absent for the same reason, to the same effect,
and it would otherwise be the one case that dies with an `AttributeError` while the other three
report properly.

#### D2 — the fake client (`deck_check.py:1436-1491`)

`_FakeSession` is *"enough of `AsyncSession` for `_settle`"*, and its docstring at `:1449-1451`
carries the argument this file inherits verbatim: *"`_settle`'s subject is what it WRITES, and
every branch of that is decided before SQLAlchemy is involved."* Identically here — the subject
of cases 71–73 is what this repo **hands to** boto3, never what Cloudflare does with it. A
recording fake measures the code and keeps the file layer 1.

Three properties to copy rather than reinvent:

- **Record, do not assert inside the fake.** `_FakeSession` counts `commits` (`:1456`, `:1468`)
  and lets the case decide. The storage fake appends `(method, kwargs)` tuples in order, because
  ordering *is* case 73's assertion.
- **Install by swapping a module attribute; restore in a `finally`.** `settle_onto`
  (`:1471-1491`) swaps `handout_jobs.SessionLocal`, calls, and restores unconditionally. The
  storage equivalent swaps whatever `app/storage.py` names as its client. **Feature 02 owes that
  seam a name**; what this file requires is that there *be* one module-level attribute to swap —
  the `retriever.py` / `llm.py` single-construction-point idiom applied a third time (§1.8).
- **An unknown keyword must raise at the call, outside the code under test's own `try/except`**
  (`:1471-1477`). A fake that swallows `**kwargs` is how a renamed parameter goes green. The
  fake's `put_object` takes the arguments boto3 takes and no others.

The fake is why case 73 can assert "a rollback deletes the object" with no bucket, and why the
whole file runs in under two seconds.

---

### E. Two states here; three where R2 is real

`deck_check.py` is strictly **two-state**: `check()` at `:81-86` prints `[ok]` or `[FAIL]` and
appends to `failures`. There is no unmeasured tier, and that is correct for it — nothing outside
the process can stop one of its cases running. `storage_check.py` inherits the helper *and the
licence*: it never touches R2, so a case either runs or its subject is absent, and absence is a
`[FAIL]` by design on the red run.

`agentic_check.py` has **four** (`:340-367`): `[ok]`, `[warn]` for unmeasured, `[rate]` for an
upstream refusal, `[FAIL]` for a defect — with `_is_failure` (`:358-367`) excluding the middle two
from the exit code. **Every case in this change set that reaches R2 needs the middle two**, R-11:

| Case | How it gets the three states |
|---|---|
| S11 / S11b (§C) | `unmeasured` when the control is dead or the route is `postgres` |
| The nine byte-readers (§B) | Inherited — they live in `agentic_check.py`. The change is that they now *rely* on it |
| Feature 03's S34/S35, feature 04's S36 (A7–A9) | Inherited. Their feature files must use it, not wrap an outage in a `try/except` that reddens |
| **Feature 05's `download_ui_check.py`** | **Ported.** It runs on the global interpreter and imports nothing from `agentic_check.py` |

`ui_check.py:75-99` is the implementation to copy for that last row, **including `:565-569`**,
which prints the unmeasured list even on a green run — an unmeasured row that only surfaces when
something else fails is a row nobody reads.

A suite that reddens because a provider said no teaches its reader to ignore red. The inverse —
R-8, where every download 403s at once with the app provably unchanged and every layer-1 harness
green — is the case this discipline has to get right, and it is why `403` stays out of
`RATE_LIMIT_PHRASES` (§B).

`deck_rate_check.py` has no tier at all: its flags are spelled inline at `:303-305` as
`[ok]`/`[retry]`/`[FAIL]`. It is a measurement, it gates nothing, and it exits on its own terms,
so it takes §B's two lines and nothing else.

---

## Contracts consumed

By reference into [`PLAN.md`](PLAN.md), never restated: **§3.1** (settings and the validator —
case 74's subject), **§3.3** (the key scheme — case 71), **§3.4** (the download contract — case 72
and §B), **§3.5** (write ordering — case 73), **§3.6** (the column stays, which is what leaves S11
a subject at all), **§3.9** (`error_kind="storage"`), **§5** A1–A5, and risks **R-3**, **R-6**,
**R-11**.

---

## Acceptance criteria

| # | Criterion | `PLAN.md` §5 |
|---|---|---|
| **A1** | `storage_check.py` **71**: a derived key matches the §3.3 shape, round-trips both ids, contains no substring of any hostile filename, and its signature accepts no request-supplied `str` | A1 |
| **A2** | `storage_check.py` **72**: `_safe()` — imported from `app.api.handouts:243`, not reimplemented — reaches `response-content-disposition` with no CR, no LF, exactly two quotes, verified after percent-decoding; `"..."` yields `filename="handout"` | A2 |
| **A3** | `storage_check.py` **73**: commit → 1 put / 0 deletes; rollback → 1 put then 1 delete of the same key; a raising `delete_object` leaves the original exception unchanged | A3 |
| **A4** | `storage_check.py` **74**: `storage_route="r2"` with any one of the four R2 values blank raises at construction — four sub-assertions — while `storage_route="postgres"` with all four blank constructs fine | A4 |
| **A5** | `agentic_check.py` **S11 + S11b**: the list route makes zero object-storage calls, *and* the same recorder counts non-zero on a control in the same block; both `unmeasured` if the control is zero or the route is `postgres` | A5 |
| **A6** | `agentic_check.py:2787-2789` and `deck_rate_check.py:297-298` carry `follow_redirects=True` **and** `mounts={"https://": httpx.AsyncHTTPTransport()}` | R-3 |
| **A7** | With A6 landed and no route yet returning a 302, one `agentic_check.py --run` produces the nine byte-reading rows **identical** to the run before it | R-3 |
| **A8** | S11 has been **seen red**: a presign call planted in the list route turns it red, removing it turns it green | — |

**A1–A4 must be seen failing before feature 02's code exists**, and the correct red run is four
`[FAIL]` rows whose detail names the missing module — never a traceback. A case written after the
code is a case written to pass.

**A8 is not ceremony.** `deck_check.py` case 14 was proved the same way (12/01 as-built #2) and
the proof found a real defect — the check had matched its own source line. A rewritten check that
nobody has watched fail is the defect it was rewritten to fix, wearing new words.

---

## What must keep working

- **The nine byte-reading rows are byte-identical after §B and before any 302.** That is
  `build.md` §4's standing regression form — with the feature off, output is identical to today,
  *assert it* — and it is A7.
- **`deck_check.py`'s 46 cases and `sandbox_check.py`'s security block are untouched.** New case
  ids start at 71, so nothing renumbers and no existing criterion changes what it points at.
- **`agentic_check.py --cleanup` still deletes the Pinecone namespace first** (`:500-508`). A
  leaked namespace burns one of the Builder plan's 1,000, and that cap **is** the maximum number
  of agents this deployment can hold.
- **`--setup` remains a no-op when documents already exist** (`:476-481`).
- **The fixture's hostile retrieval settings are not relaxed.** A scenario that needs different
  conditions owns them and restores them in a `finally`, per `loop.md` §5 — S29 (`:2521-2531`) is
  the pattern.
- **`_safe` keeps exactly one definition.** Case 72 imports it; it does not copy it.

**One forward-note, found while reading cleanup and belonging to feature 03 rather than here:**
`cleanup` deletes handout rows with a Core `DELETE` at `:510`, so after feature 03 the harness's
own cleanup leaks every object it created — the exact shape of §3.7's agent-delete defect (R-2),
inside the tool that is supposed to leave nothing behind. There is nothing to fix today because
there are no objects. It is named so feature 03 does not ship a cleanup that leaks its own
fixtures.

---

## What this deliberately does not do

- **It does not create `app/storage.py`, the settings, the validator or the migration.** Features
  02 and 03. Cases 71–74 are red until they exist, and **being red is the deliverable**.
- **It does not assert the 302.** The redirect is feature 03's; §B only ensures the harness
  survives one. A case asserting a redirect that no route returns is a case that cannot fail.
- **It does not touch R2.** No bucket, no credentials, no network. `create_r2_bucket.py` is
  feature 02 (§3.8), and the capability probe in `PLAN.md` §1.1 is already the measurement — this
  file does not re-run it under a harness that would then need a live account to be green.
- **It does not add S34, S35 or S36** (A7–A9). They belong to the features whose code they
  measure; written here they would be written against nothing, and their preconditions chosen
  blind — which is how S3 acquired a fixture that made it unfalsifiable.
- **It does not widen `RATE_LIMIT_PHRASES`.** §B: a 403 is a defect except in R-8's single case,
  and catching that one case would green every signing bug.
- **It does not give `deck_check.py` or `deck_rate_check.py` an unmeasured tier.** §E. Neither can
  be prevented from running by anything outside its own process, and a third state that can never
  be reached is one more thing to keep true.
- **It does not drop `handouts.content` or change what the list route selects.** §3.6 and §7.2 —
  blue/green, not delete-then-create.

---

## As built

*Written after it ships. `12-robust-handouts/01-deck-harness-floor.md`'s "Where the plan was
wrong" is the model — five of its six entries were only learnable by running the thing.*
