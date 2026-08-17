# 13 — Object storage: the database holds structured data

Change set 3. Written to [build.md](../build.md); the audit is §1, the shared contracts are §3,
and the feature files in this folder **reference §3 and never restate it**.

**The change in one sentence.** Binary content — handout files today, original uploads for the
first time — moves out of Postgres into Cloudflare R2, reached by presigned URL rather than
proxied through FastAPI, leaving the database holding only structured, queryable data.

**What it is not.** It is not a re-chunk feature (§7.1), it does not drop the `content` column
(§3.6), and it does not touch `chunks.text`, which is a queryable corpus rather than a blob.

---

## 1. Audit

Five parallel audits, 2026-08-17: handout byte paths, schema and migrations, config/deps/deploy,
harnesses, document ingest. Everything below is cited. A sixth strand — the R2 account itself —
was probed live rather than read from documentation, per CLAUDE.md's *"probe the parameter, do
not read the list"*.

### 1.1 The capability probe, measured rather than assumed

Executed against the live account on 2026-08-17. The probe bucket was created and deleted; the
account's two pre-existing buckets were never touched.

| Probe | Result |
|---|---|
| `GET /accounts/{acct}/tokens/verify` | **active**, `expires_on: 2027-08-17T23:59:59Z` |
| `POST /r2/buckets`, `locationHint: apac` | created, `location: APAC` |
| S3 `put-object` / `list-objects-v2` | ✅ |
| **Presigned GET, fetched with no credentials** | **HTTP 200**, body intact |
| **`ResponseContentDisposition` override** | returned `attachment; filename="Ka band deck.pptx"` |
| **`ResponseContentType` override** | returned the pptx mime verbatim |
| Presigned URL expired 1 s, fetched after 2 s | **403** — expiry enforced |
| Unsigned direct GET | **400** — bucket private by default |

The two override rows are load-bearing. They are what lets a redirect reproduce
`handouts.py:632`'s `Content-Disposition: attachment; filename="<sanitised>"` exactly, so
`_safe()` keeps its job instead of being silently dropped (§3.4, R-6).

**Regions line up and no new hop is introduced:** Render `agentic-rag-api` is `singapore`,
Pinecone is `ap-southeast-1`, R2 accepted `apac`.

**The account is shared.** `mindfulspeak-uploads` and `mindfulspeak-uploads-backup` belong to a
different project, and the token is account-wide. Bucket naming is therefore namespaced, and the
token's blast radius is recorded as R-9 rather than fixed here.

### 1.2 The credentials are in `.env` and the application cannot read one of them

Lines 36–54 are a raw paste of the Cloudflare console panel — bare lines such as `Account ID`,
`Your API Token`, `S3 API endpoint`. There is no `R2_*` assignment anywhere in the file.

Measured: **the app boots.** `python-dotenv` catches its own parse error per line
(`dotenv/parser.py:169-176`) and downgrades it to `logger.warning`
(`dotenv/main.py:32-39`), and `extra="ignore"` (`config.py:30`) discards what survives. Thirteen
warnings at startup, nothing raised.

**The non-obvious half, and the reason this is in the audit rather than in a feature file:** five
of those lines *parse successfully*, as keys with a `None` value — a bare token with no `=` is a
legal key to python-dotenv (`parser.py:147-155`). So the account id, the API token, the access
key id, the secret and the endpoint are currently **dictionary key names**. Inert, because
`dotenv.py:148` skips falsy values before they reach the model. But the file does not mean what
it looks like it means, and *"the credentials are in `.env`"* is true only in the sense that the
characters are present.

### 1.3 Two different credentials, and conflating them is a security regression

| Credential | Drives | Who needs it |
|---|---|---|
| `cfat_…` API token | `api.cloudflare.com` — create/delete **buckets** | **provisioning script only** |
| Access Key ID + Secret | `<acct>.r2.cloudflarestorage.com` — put/get/presign **objects** | the backend |

The access key id is the API token's own id, which makes them look like one credential. They are
not. The backend never creates a bucket, so it never receives the management token — the same
argument that keeps `RENDER_API_KEY` off the deployed service (`create_render_services.py`, and
PRD §7).

### 1.4 What exists, at `file:line`

**Handout bytes — two writers, one reader.**

| Role | Site |
|---|---|
| Column | `models.py:808` — `content: Mapped[bytes \| None] = deferred(mapped_column(LargeBinary))` |
| Write 1 — recipe job | `jobs.py:839-861`, `content`/`byte_size`/`status="ready"` in **one commit** |
| Write 2 — chat/tool door | `ask.py:1084-1111`, inside the **turn's transaction** |
| Read — the only one | `handouts.py:613-626`, via the codebase's only `undefer` at `:329` |
| Filename sanitiser | `handouts.py:243-291` (`_safe`), regex `:77`, cap `:82` |
| Delete (row only) | `handouts.py:645-667` |
| Harvest + caps | `sandbox.py:466-523`; `config.py:578-579` (5 MB / 15 MB) |

**Document bytes — never persisted.**

| Role | Site |
|---|---|
| Entry | `documents.py:331` — `data = await file.read()` |
| Hash | `documents.py:359` — `sha256(data).hexdigest()` |
| Staged row | `documents.py:390-418` — `pending`, `byte_size`, `content_hash` |
| Handoff | `documents.py:456-468` — **ids and bytes only** |
| Text extraction | `ingest.py:115-136` (`_load_text`) — bytes in, `str` out |
| Splitting | `ingest.py:171-201` (`_prepare_chunks`), splitter built at `:139-168` |
| Delete | `delete.py:27-82` — **vectors first, rows second** |

`data` is reachable from `documents.py:331` until `run_ingest_job` returns (`jobs.py:185`). It is
never written to disk, never to Postgres, never to Pinecone.

**Nothing resembling object storage exists.** No client, no module, no credentials plumbing, no
dependency. Every textual hit under `backend/` is a comment saying it is *not* built
(`models.py:339`, `:730`, `config.py:588`, `ask.py:1076`, `handouts.py:470`,
`bc307f5fc31f:18`). This is built from zero.

### 1.5 What is closed, and by which property

**Serving bytes from a public bucket is closed.** The probe measured an unsigned GET returning
400. Making the bucket public would remove the per-agent authorisation that `OwnedAgent` provides
on every handout route — a handout is scoped to an agent, and a public object is scoped to
whoever has the URL. Presigned URLs keep the authorisation decision in FastAPI, where
`_load_owned` already makes it.

**Client-supplied storage keys are closed, structurally.** `Agent.namespace` is a derived
property precisely so a request cannot name another tenant's vectors, and
`SearchCorpusArgs` carries one field for the same reason (11 §1). A storage key derived from
`agent_id` and the row's own primary key keeps that property; accepting a key from a request body
would reintroduce exactly the parameter whose absence is the control.

**Dropping the `content` column in this change set is closed** — see §3.6. It is what makes the
change reversible, and it is what stops `agentic_check` S11 inverting (§1.6).

**A re-chunk route is closed for now** — §7.1, with the reason and what it would cost.

### 1.6 Two harness facts that decide the build order

**Nine assertions download real bytes, and `httpx.AsyncClient` does not follow redirects.**
`agentic_check.py:2787-2789` and `deck_rate_check.py:297-298` construct the client without
`follow_redirects=True`. S8×4, S8b, S8c, S28, S29 and `deck_rate_check.py:214` all
`GET .../download` and read `.content`. **The moment that route answers 302, all nine go red
simultaneously**, reading as "the migration broke downloads".

**`agentic_check.py` S11 asserts the absence of a thing this change set would delete.**
`:2292-2329` greps captured SQL for `"handouts.content"` and asserts it never appears. If the
column were dropped, S11 passes forever and measures nothing — build.md §7's table gaining a
**seventh** row, and a new mechanism: not a test that was too weak, but a test whose *referent*
was removed. §3.6 keeps the column, so S11 keeps its subject; §4 feature 01 additionally
rewrites it to assert the positive property.

### 1.7 The constraint being relaxed is misattributed

`ingest.py:118`, `jobs.py:120`, `documents.py:660` and `requirements.in:61` cite **"PRD section
7"** for *"original uploads are never stored"*. PRD §7 (`PRD.md:886-950`) does not contain that
bullet. The nearest statement is §4.3 at `PRD.md:591-593`, and it is itself wrong by one word:

> *"having the text locally means you can **re-chunk** or re-embed without re-parsing the
> original files"*

Re-embedding from `chunks.text` is genuinely cheap and that half is correct. **Re-chunking from
it is not possible** — chunk boundaries are lossy, so re-splitting already-split text at a larger
`chunk_size` cannot recover what a smaller split separated, and at a smaller size produces
different boundaries than splitting the original would. This sentence is corrected in §9
regardless of anything else in this change set, because it is wrong today.

### 1.8 What the change reduces to

Subtracting §1.4 and §1.5:

1. One new module that constructs an S3 client — the `retriever.py` / `llm.py` seam idiom applied
   a third time.
2. One migration adding two nullable `storage_key` columns. No drop, no backfill inside it.
3. Two write paths and one read path for handouts; one write path for documents.
4. Three delete paths that currently have nothing external to clean up and will.
5. A provisioning script, a backfill script, and harnesses.

The audit deleted a re-chunk route, a column drop, a public bucket, and a second dual-write code
path. What is left is smaller than the first sketch.

---

## 2. Architecture after the change

```
UPLOAD                                    HANDOUT
  browser --multipart--> documents.py       jobs.py / ask.py
                              |                    |
                    put_object(bytes)      put_object(bytes)
                              |                    |
                              v                    v
                    +---------------------------------------+
                    |  app/storage.py  -- the only place an  |
                    |  S3 client is constructed              |
                    |  key = derived, never client-supplied  |
                    +---------------------------------------+
                              |                    |
                              v                    v
                    R2  agents/{agent_id}/documents/{document_id}{ext}
                        agents/{agent_id}/handouts/{handout_id}{ext}

DOWNLOAD
  browser --GET /download--> handouts.py --302--> R2 presigned URL
                                  ^                     (bytes never
                          authorisation                  enter FastAPI)
                          happens HERE

POSTGRES holds: ids, filenames, mime types, byte_size, status, preview_text,
                source_code, meta, chunks.text  -- everything queryable
R2 holds:       bytes
```

---

## 3. Shared contracts

**Every feature file assumes this section and none of them restates it.** A contract stated twice
drifts, and the copy that drifted is never the one being read.

### 3.1 Settings

New in `backend/app/config.py`, following the file's conventions — no `Field(...)`, name matches
the env var case-insensitively, and the comment carries the measurement that chose the value.

| Setting | Default | Notes |
|---|---|---|
| `storage_route` | `"r2"` | `Literal["r2", "postgres"]`, validated by `@field_validator` exactly as `embedding_route` is (`config.py:400-436`). `"postgres"` is the rollback road and reads `content` as today |
| `r2_account_id` | `""` | |
| `r2_access_key_id` | `""` | |
| `r2_secret_access_key` | `""` | |
| `r2_bucket` | `"groundwork-media"` | Namespaced because the account is shared (§1.1) |
| `r2_endpoint` | `""` | Derived from `r2_account_id` when blank; explicit wins |
| `r2_presign_ttl_s` | `300` | Five minutes. Long enough for a slow mobile download to start, short enough that a URL in browser history is stale by the time anyone reads it (R-7) |

**The validator is the required-secret mechanism**, because the repo has no other. `config.py`
declares every secret as `str = ""` with no runtime gate; `embedding_route` is the sole precedent
for failing at construction, and its docstring is the argument for why prose was insufficient.
`storage_route="r2"` with any of the four R2 values blank must raise at load, not at first
download.

**`GET /api/config`'s `secrets_present` gains an `r2` key** (`main.py:151-167`), because that
endpoint is the existing diagnostic for exactly this class of question.

### 3.2 Dependency

`boto3` joins `backend/requirements.in` with a comment stating that it is an **S3-protocol client
pointed at Cloudflare**, that nothing calls AWS, and that no `AWS_*` variable is read — the same
note `langchain-openai` carries for OpenRouter. It is already installed in `backend/.venv`
(1.43.72, installed during the audit probe) and is in **neither** requirements file.

**`pip freeze` will flatten `pywin32==312; sys_platform == "win32"` for the fourth time.**
Restoring it is the second half of the freeze command, not a follow-up task
(`requirements.txt:115`).

**Playwright does not become a dependency.** `ui_check.py:14-16` states the policy; the browser
harness runs on the global interpreter, as `ui_check.py` and `mention_popup_check.py` already do.

### 3.3 The key scheme — derived, never supplied

```
agents/{agent_id}/handouts/{handout_id}{ext}
agents/{agent_id}/documents/{document_id}{ext}
```

Three properties, each load-bearing:

- **`agent_id` is the first path segment**, so agent deletion is one prefix delete — the
  structural mirror of `delete_agent_namespace` (`delete.py:85-108`).
- **The row's own primary key is the object name.** It is generated in Python before the insert
  (`uuid.uuid4()` at `ask.py:1085`, `handouts.py:507`, `documents.py:391`), so the key is known
  before the row exists, which is what makes §3.5's ordering possible.
- **The filename never enters the key.** It is model-written for handouts and user-supplied for
  documents; it reaches the user through `response-content-disposition` at presign time instead
  (§3.4). `ext` comes from the mime table, not from the filename.

Key derivation lives in `app/storage.py` and takes ids, never strings from a request.

### 3.4 The download contract

`GET /api/agents/{agent_id}/handouts/{handout_id}/download` keeps its path, its cookie
authentication and its **409 on a non-ready row** — `HandoutCard.tsx:216-218` gates the chart
thumbnail on that behaviour and `types.ts:289-300` encodes it.

On `storage_route="r2"` it returns **302** to a presigned URL carrying:

| Query parameter | Value |
|---|---|
| `response-content-disposition` | `attachment; filename="{_safe(handout.filename)}"` |
| `response-content-type` | `handout.mime_type` |

`_safe()` moves from the response header to this parameter and keeps its docstring's argument
intact — a model-written filename reaching a header is header injection whether FastAPI or R2
emits the header.

**`Cache-Control: private, no-store` cannot be reproduced** (R-7).

**No frontend change is required.** Both consumers are URL-only — the `<a href download>` at
`HandoutCard.tsx:324-331` and the `<img src>` at `:220-229` — so a redirect is transparent to
`api.ts:881`.

### 3.5 Write ordering, and the transaction that is lost

`ask.py:1071-1074` currently guarantees: *"If the turn rolls back so do its handouts."* An R2 put
is not transactional, so that guarantee has to be rebuilt rather than assumed.

**The rule for writes is object first, row second** — the same direction as `delete.py`, and for
the same reason inverted: a row with no object is **visible and re-deletable**, an object with no
row is **unreachable and permanent**, because the key is derived from an id that no longer exists.

```
1. put_object(key)          # key derived from the id we are about to insert
2. row.storage_key = key
3. commit
4. on rollback/exception: best-effort delete_object(key), logged, never raised
```

Step 4 is best-effort by design: a failed cleanup must not turn a recoverable turn into a failed
one. What it cannot catch is reconciled by §3.8's script.

**`_settle` is untouched.** `jobs.py:1194-1195` refuses to move a row that is not `pending`, and
this change introduces **no new status** — the same rule 12 §3.4 adopted, for the same reason
(R9 there, R-4 here).

### 3.6 Schema and migration

One migration for the whole change set. `down_revision = 'd4e91c2a7b58'`.

```python
op.add_column('handouts',  sa.Column('storage_key', sa.Text(), nullable=True))
op.add_column('documents', sa.Column('storage_key', sa.Text(), nullable=True))
```

**Nullable, and `content` is NOT dropped.** Three reasons, and the third is the one that
generalises:

1. Rollback needs it. `storage_route="postgres"` is only a real road while the bytes are there.
2. A row can legitimately have neither — a `pending` handout, and the one live row with
   `content IS NULL` (a failed deck).
3. **It is the blue/green rule the repo already applies to Pinecone.** `migrate_index.py` builds
   the replacement alongside the original and CLAUDE.md's procedure ends *"Only then delete the
   old one by hand."* Dropping the column in the same change set that introduces the new store is
   delete-then-create.

Dropping `content` is a later change set, gated on §6's definition of done holding in production.

**No data migration inside `upgrade()`.** Migrations run in Render's **start** command
(`create_render_services.py:213-218`), so a backfill there would need R2 egress during boot and
would fail the deploy if credentials were absent. Backfill is §3.8.

**Live data is trivial:** 6 handout rows, 159,639 bytes total, max 34,926. `byte_size` and
`octet_length(content)` agree exactly, so the denormalised size can be trusted afterwards.

### 3.7 Delete paths — three, and two have no Python today

| Path | Site | What is added |
|---|---|---|
| One handout | `handouts.py:645-667` | `delete_object(key)` **before** `db.delete`. Its docstring's *"there is no external store"* becomes false and must be rewritten, not left |
| One document | `delete.py:27-82` | `delete_object(key)` beside the existing vector delete, **before** the row deletes |
| **An agent** | `agents.py:1175-1183` | `delete_agent_objects(agent)` immediately after `delete_agent_namespace(agent)` at `:1171` |

The agent path is the dangerous one and the audit is unambiguous about why: it is a Core `DELETE`
chosen deliberately so Postgres performs the cascade, and **there is no `relationship()` on
`Handout` anywhere in `models.py`**. No Python sees the rows. Without an explicit prefix delete,
every object belonging to a deleted agent leaks permanently.

Conversation deletion is archive-only today (`conversations.py:709`), so its FK cascade does not
fire — recorded so nobody "fixes" it into a hard delete without adding cleanup.

### 3.8 Scripts

| Script | Interpreter | Does |
|---|---|---|
| `scripts/create_r2_bucket.py` | backend venv | Idempotent provisioning: verify token, create bucket if absent with `locationHint: apac`, assert it is **not** public, report drift. Uses the `cfat_…` token. `--dry-run` |
| `scripts/migrate_bytes_to_r2.py` | backend venv | Backfill: for every `handouts` row with `content IS NOT NULL AND storage_key IS NULL`, put and set the key. Idempotent, re-runnable, `--dry-run`, **never deletes `content`** |
| `scripts/storage_check.py` | backend venv | Layer 1 — key derivation, presign shape, `_safe` on the disposition parameter, ordering, all against a fake client. No network |
| `scripts/download_ui_check.py` | **global** | Playwright: sign in via the dev-login form, open an agent, click Download, assert a real file arrives (§5) |

Both provisioning scripts follow the repo's idempotent convention: detect, verify against this
plan, report drift, never recreate.

### 3.9 Trace events

**None.** No new trace event type, no new `queries` column. A storage put is not a model decision
and nothing about it is replayed into a thread. Failures surface through the existing
`error_kind` vocabulary that 12 §4 built.

`error_kind` gains one member: **`storage`**. It goes in `SANDBOX_ERROR_KINDS`' sibling set so
`agentic_check.py` S31's `known` check keeps passing, and so a 403 from R2 is distinguishable
from a model failure — which is precisely the token-expiry case in R-8.

---

## 4. Build sequence

Lowest layer first, harness before the code it measures.

| # | Feature | Ships |
|---|---|---|
| **01** | [Storage harness floor](01-storage-harness-floor.md) | `scripts/storage_check.py`, the `follow_redirects` fix, and S11 rewritten to assert a positive property. **Watch them fail before feature 02 exists.** |
| **02** | [The storage seam](02-storage-seam.md) | `app/storage.py`, settings, the validator, `boto3`, `create_r2_bucket.py`. No caller yet |
| **03** | [Handout bytes move](03-handout-bytes.md) | The migration, both writers, the redirect, three delete paths, `migrate_bytes_to_r2.py` |
| **04** | [Upload bytes are kept](04-upload-bytes.md) | `documents.py` writes originals to R2; the four misattributed comments corrected |
| **05** | [The browser proof](05-browser-proof.md) | `download_ui_check.py` — a real click producing a real file |

01 before 02 is the rule feature 12/01 established: building the code first means writing a case
against code that already exists, which is how S3 went green twice while proving nothing.

---

## 5. Acceptance criteria

**Every criterion names a harness file and a case id.** Prose criteria went unexecuted for three
documents (05, 06 and 04's deck promise); this table is the correction.

| # | Criterion | Harness case |
|---|---|---|
| A1 | A derived key never contains a request-supplied string | `storage_check.py` 71 |
| A2 | `_safe()` output reaches `response-content-disposition`, CRLF-free and quoted | `storage_check.py` 72 |
| A3 | A rollback after a put deletes the object | `storage_check.py` 73 |
| A4 | Missing R2 config with `storage_route="r2"` raises at **load**, not at download | `storage_check.py` 74 |
| A5 | The list route makes **zero** object-storage calls | `agentic_check.py` S11 (rewritten) |
| A6 | A recipe handout downloads and **opens** through the redirect | `agentic_check.py` S8 ×4 |
| A7 | A **chat-made** handout's bytes are retrievable end to end | `agentic_check.py` S34 (new — the path nothing asserts today) |
| A8 | Deleting an agent leaves zero objects under its prefix | `agentic_check.py` S35 (new) |
| A9 | An uploaded document's original is retrievable byte-identically | `agentic_check.py` S36 (new) |
| A10 | Download in a real browser yields a file with the right name and size | `download_ui_check.py` D1–D4 |

A7 is called out because the audit found it is the path most likely to be missed: `deck_check.py`
60–64 stop at `ctx.artifacts`, and no scenario has ever downloaded a chat-made handout.

**A5 is deliberately positive.** "`handouts.content` appears in no SQL" would pass forever once
the column stopped being read; "the list route makes zero object-storage calls" cannot pass by
deletion.

---

## 6. Definition of done

```
backend/.venv/Scripts/python.exe scripts/storage_check.py         # new, all green
backend/.venv/Scripts/python.exe scripts/deck_check.py            # 46 + new block, green
backend/.venv/Scripts/python.exe scripts/sandbox_check.py         # 22, unchanged
backend/.venv/Scripts/python.exe scripts/refusal_check.py
backend/.venv/Scripts/python.exe scripts/ledger_check.py
backend/.venv/Scripts/python.exe scripts/llm_check.py
backend/.venv/Scripts/python.exe scripts/agentic_check.py --setup / --run / --cleanup
cd frontend && npm test && npm run build
python scripts/ui_check.py                                        # global interpreter
python scripts/download_ui_check.py                               # global interpreter
grep -n pywin32 backend/requirements.txt                          # marker must survive
```

**And then open the page and download a deck by eye.** Every harness was green when a model's
own tool-call markup was rendering into the answer text, and that took opening the page. A
`[warn]`/`unmeasured` row is not a pass; a `[rate]` row is not a pass.

---

## 7. What this deliberately does not do

### 7.1 A re-chunk route

The audit establishes that there is none today: three document routes, `chunk_size` read once at
`ingest.py:415-425`, and `_prepare_chunks` requiring bytes the caller already holds. Storing
originals makes one *possible* and this change set does not build it.

Two reasons beyond scope. **It inherits PRD open item 18 in full**: replacing a document's chunks
destroys the `query_chunks` rows of every earlier answer, so re-chunking would silently invalidate
eval history exactly as deletion does — a scorecard keeping its scores and losing its evidence.
And **`ingestion_runs` records no `splitter`**, so two runs differing only in splitter are
indistinguishable on the columns that matter. Both want deciding before a button exists.

### 7.2 Dropping `handouts.content`

§3.6. Blue/green, not delete-then-create.

### 7.3 Serving `chunks.asset_uri`

Confirmed empirically: zero writers, zero populated rows. This change set gives it a plausible
home and writes nothing to it. PRD item 10's *"only gates citation images"* becomes wrong either
way and is corrected in §9.

### 7.4 Moving `chunks.text`

It is a queryable corpus, not a blob — ~1.4 MB across the whole document set, read by retrieval
and by the handout path, and the source of truth that makes re-embedding cheap. `models.py:331-333`
stays true and must not be swept.

### 7.5 Raising the handout quota

`handout_max_per_agent = 200` and `sandbox_max_artifact_bytes = 5 MB` are justified in
`config.py:588-590` by *"bytes live in Postgres… a storage bound, not a policy"*. That
justification dies here, and the numbers stay anyway pending a decision — but **their comments are
rewritten**, because a comment giving a dead reason is worse than none.

---

## 8. Risk register

| # | Risk | The tell |
|---|---|---|
| **R-1** | An object is written and the transaction rolls back | Orphan under a key whose id no longer exists. §3.5 step 4, plus reconciliation in `migrate_bytes_to_r2.py --orphans` |
| **R-2** | Agent delete leaks every object | **No `relationship()` on `Handout`; the delete is Core.** §3.7 |
| **R-3** | Nine assertions go red at once on the first 302 | Fixed in feature 01, before anything returns a redirect |
| **R-4** | A new status breaks `_settle`'s `pending` guard | **No new status is introduced.** If a feature file wants one it comes back to this plan |
| **R-5** | `pip freeze` flattens the `pywin32` marker | Fourth occurrence. `grep -n pywin32` is in §6 |
| **R-6** | `_safe()` is dropped because FastAPI no longer emits the header | A7/A2. The sanitiser moves, it does not retire |
| **R-7** | `Cache-Control: private, no-store` is unreproducible | A presigned URL is a bearer capability in a URL. Mitigated only by `r2_presign_ttl_s = 300`; **recorded as an accepted loss**, not solved |
| **R-8** | **The API token expires 2027-08-17** | Every download 403s at once, with the app provably unchanged and every offline harness green. `error_kind="storage"` (§3.9) is what makes it legible. Opened as a PRD item |
| **R-9** | The token is account-wide and the account is shared | This app's credential can reach `mindfulspeak-*`. Not fixed here; a scoped token is the answer |
| **R-10** | Backfill runs against production while `storage_route` is still `postgres` | Harmless by construction — it only ever adds keys and never deletes `content` |
| **R-11** | R2 unavailability turns the suite red | `[rate]`/`[warn]`, never `[FAIL]`. A suite that reddens because a provider said no teaches its reader to ignore red |

---

## 9. Fold-out — documents to correct on ship

Per build.md §9, and unusually large because §1.7 found the map wrong.

| Document | Change |
|---|---|
| `PRD.md` §4.3 (`:591-593`) | **"re-chunk or re-embed" → "re-embed"**, with a sentence saying re-chunking needs the original. Wrong today, independent of this change set |
| `PRD.md` §7 | Add the "original uploads are not stored" bullet the code cites — then strike it with the date, so the four citations resolve to a real place with a real history |
| `PRD.md` §10 | Items **10** and **25** resolved; item **12** stays open and is explicitly *not* resolved by a private bucket; new items for R-7, R-8, R-9, and for the `splitter`/`ingestion_runs` gap |
| `CLAUDE.md` | A storage section: the two credentials, the derived key, object-first-then-row ordering, the four probe results, and the dotenv "secret became a key name" finding |
| `EVAL.md` | Nothing measured changes. One line that a scorecard's contexts are still `chunks.text` and are unaffected |
| `loop.md` | Nothing. No model decides anything here — which is itself worth one line in this plan's as-built section |
| 7 Tier-1 comments | Listed by the audit, including user-visible copy at `AgentDocuments.tsx:150-152` |

---

## 10. As built — where the plan was wrong

Shipped 2026-08-17. Five things the plan got wrong or missed, in the order they bit.

### 10.1 The harness caught a real defect, and two of its own

`storage_check.py` was written before `app/storage.py` had a caller, and three rows went red on
the first run. **Two were the test being wrong**, which is the more useful half:

- **Case 72** asserted `"Set-Cookie" not in disposition` and went red against a *correctly
  sanitised* value: `_safe` had collapsed the CRLF to `_`, leaving the harmless filename
  `deck_Set-Cookie_a_b_script_.pptx`. The letters survive, the escape does not, and only the
  escape was ever the vulnerability.
- **Case 75b** grepped `HandoutOut`'s class body for `"content"` and matched **its docstring**,
  which exists to say at length that there is deliberately no content field. The prose
  documenting the guard tripped the test for the guard.

Both are the same defect in opposite directions — a **substring assertion standing in for a
semantic one**, which is the lesson the refusal-marker list learned four times. Fixed by
asserting on structure: the characters that can escape a quoted value, and
`HandoutOut.model_fields`.

The third was real. `delete_quietly` is contractually *"never raises"*, and it caught only
`StorageError` while `put_object`/`delete_object` wrapped only `ClientError` — so
`EndpointConnectionError` or `NoCredentialsError` would travel straight through it into an
`except` block already handling something else. **The two moments R2 is most likely to be
unreachable are exactly the moments that function runs.** Fixed to `except Exception`.

### 10.2 §3.4 said "no frontend change is required" and that was right for the wrong reason

Both consumers are URL-only, so the redirect is transparent — true. What the plan did not
anticipate is that **the browser harness needed three fixes before it could see anything**, and
none of them were about storage:

- The dock toggle is `handout-dock-toggle`, not `handouts-toggle`.
- An agent with **zero documents renders `EmptyAgentWorkspace`** — no composer, no dock, no
  download anchor. Correct product behaviour that makes D1 and D3 unmeasurable, so the fixture
  had to ingest a corpus.
- Navigating by constructing `/agents/{id}` produced a **blank page with no testids at all**,
  which reads as "the workspace is broken". Opening by clicking the card, as `ui_check.py` does,
  was the fix.

And `sign_in` hit the identical race `ui_check.py:124-129` already documents: the selector wait
is satisfied by `create-agent-toggle`, which renders before the agent list arrives, so counting
`agent-open` on the next line reports 0 on an account that has agents. **A documented trap in a
sibling harness was reproduced verbatim by writing a new one** — the note was in the file, not
in a place the next author would read.

### 10.3 §5's A7 could not be written as planned

The plan promised `agentic_check.py` **S34** for "a chat-made handout's bytes are retrievable
end to end", on the correct observation that nothing has ever downloaded one. It was not built:
provoking a tool-written artefact needs a model that chooses to call `run_python`, which makes
the scenario non-deterministic in exactly the way [loop.md](../loop.md) §5 warns about — a test
that passes without exercising anything. The **write path is shared** (`ask.py` and `jobs.py`
both go through `storage.put_object` and the same key derivation), and the **read path is
identical** — one route, no branch on `origin`. So S8 covers the download and the gap is
narrower than A7 claimed, but it is a gap: no test distinguishes the two writers.

### 10.4 The plan under-counted the leak sites

§3.7 named three delete paths. There is a **fourth**, and it is inside the test suite:
`agentic_check.py --cleanup` removes handout rows with a Core `DELETE`, bypassing the route
entirely, so every run would have left its artefacts in the bucket while reporting a clean
teardown. Measured: 11 objects before cleanup, 7 after, **4 that would have leaked**. Found by
the feature-01 agent reading the harness rather than the application.

### 10.5 What the plan got right and is worth repeating

**Keeping `content`** turned out to be load-bearing three times over, not once: it makes the
rollback real, it keeps S11's subject alive, and it made the whole change set safe to ship
without a cutover. **Probing rather than reading** caught nothing new — every documented R2
behaviour held — but the four probes cost minutes and would have cost hours if any had failed
after the code was written. And **`follow_redirects` needing `mounts` beside it** was found by
reading, before it could produce nine simultaneous red rows with a misleading cause.
