# 04 — Upload bytes are kept

The newly-in-scope half, and the only place in this change set where a byte that has **never** been
stored starts being stored. Everything else moves bytes between two stores; this one relaxes a
constraint the codebase has designed around in four separate files.

---

## What the user gets

The original file survives the upload.

Today it does not. `data = await file.read()` at `documents.py:331` produces a `bytes` object that
lives as a background-task argument and dies with the process — `:454-455` says so plainly
(*"`data` stays resident until the job finishes, which at a 50 MB cap is the real ceiling on
concurrent uploads"*). Three recovery paths are shaped around that absence:

- a row stuck at `processing` *"can never be RESUMED. The original bytes were never stored … and
  existed only as this task's `data` argument, which died with the process"* (`rag/jobs.py:120-123`)
- the delete route's stale-row escape hatch exists because *"the original bytes were never stored,
  so it cannot be resumed"* (`documents.py:659-662`)
- `_load_text` takes bytes rather than a path because *"there is no file to read"*
  (`ingest.py:118-121`)

What ships here is those sentences stopping being true. What does **not** ship is a button that
uses them — see the last section, which is longer than usual for exactly that reason.

---

## Where it goes — `documents.py:256-521`

Read the whole route before touching it. The insertion point is narrow and everything around it is
load-bearing.

The order of operations as it stands:

| Site | Step |
|---|---|
| `:290-301` | filename present, else 422 |
| `:308-316` | extension in `SUPPORTED_SUFFIXES`, else 415 |
| `:325-329` | `file.size` — cheap early rejection, *deliberately not* the enforcement point |
| `:331` | **`data = await file.read()`** |
| `:337-341` | `len(data)` — **the** enforcement point |
| `:343-352` | empty file, else 422 |
| `:359` | `content_hash = sha256(data).hexdigest()` |
| `:361-387` | duplicate check, 409 |
| `:405-418` | **the staged row**, `id=uuid.uuid4()` at `:406`, `pending` |
| `:428-438` | audit row |
| `:439` | **commit** |
| `:445` | refresh |
| `:456-468` | handoff — **ids and bytes only** |

**The R2 put belongs between `:359` and `:418`, and the reason is that everything §3.5 needs is
already true there and nowhere else in the route.** `data` is in hand and has passed every
rejection; `content_hash` is computed; and the row's id is generated in Python at `:406` before the
insert, so §3.3's key is derivable *before the row exists*. Put the object, set `storage_key` on
the `Document` constructed at `:405-417`, and the commit at `:439` is unchanged — one commit, with
the object already durable. That is §3.5's ordering with no restructuring at all.

**The handoff at `:456-468` does not change shape.** It already passes `data` positionally to
`run_ingest_job` and must go on doing so. Reading the bytes back out of R2 inside the job to save
an argument would add a network round trip to the ingest path and reopen the question the comment
at `:448-455` closed — *"IDS AND BYTES ONLY. Passing `agent`, `document` or `db` would hand a
background task objects belonging to a session FastAPI closes as this request finishes."* A
`storage_key` is a string, so if the job ever needs one it travels as an id, which is the same
rule, not an exception to it.

The synchronous fallback at `:478-497` (`ingest_in_background=false`) inherits the put because it
inherits the staged row. It is the workshop path — *"a failure that happens inside the request is a
failure a workshop can watch"* (`:471-477`) — and a configuration where originals are kept beside
one where they are not is worse than either.

---

## The column, and what `content_hash` is not

`documents` gains `storage_key` in the change set's **single** migration (§3.6). This feature
defines none.

**`content_hash` (`models.py:314`) is `String(64)`, nullable, and carries no unique index.**
Checked. The constraint is application-level and it is *deliberately* stated twice — at
`documents.py:370-379` to produce the 409, and again at `ingest.py:331-338` for the idempotent
path. `documents.py:361-369` explains why the duplication is intended rather than an oversight:
*"deliberately the same predicate ingest uses (agent + hash + status 'ready'); if that predicate
ever changes there, it has to change here too."*

Two consequences for this feature.

**The key is derived from `document_id`, never from `content_hash`** (§3.3). Content-addressed
storage would collapse the same file uploaded to two agents into one object, and `:372-374` states
the opposite as the design: *"The same bytes uploaded to two different agents are two different
corpus entries; dedup is per corpus, and the corpus is the agent."* A shared object makes one
agent's delete reach into another's corpus — which is the tenancy property §3.3's first path
segment exists to keep structural.

**Two rows can legitimately hold one hash.** `?force=true` (`:361`) and the race the 409 cannot
close (`ingest.py:346-364`) both produce it. Two rows, two ids, two keys, two objects. Nothing to
reconcile, and no uniqueness may be assumed anywhere downstream.

Everything else about the bytes is already on the row: `byte_size` at `:410`, `content_hash` at
`:411`, and `mime_type` left to ingest on purpose (`:402-404`), which derives it from the extension
through `MIME_TYPES` (`ingest.py:75-82`). §3.3's `ext` comes from that same table, so the key and
the stored MIME type cannot drift apart.

---

## Delete — `delete.py:27-82`

`delete_object(document.storage_key)` beside the vector delete at `:67`, **before** the row deletes
at `:78-79`, per §3.7.

This module already carries the rule the whole change set adopted, at `:50-67`:

> *"VECTORS FIRST, ROWS SECOND, and the order is the whole design … Orphaned vectors -- rows gone,
> vectors remaining -- have lost the only record of which ids to delete, so they are unreachable,
> permanent, and still matching every query in this namespace. Delete the recoverable thing last."*

R2 is a third store with the identical asymmetry, so it takes the identical position. One
difference worth stating explicitly: **the `if pinecone_ids:` guard at `:49` does not extend to
it.** A document with no chunks — an ingest that failed inside `_load_text`, the scanned-PDF case
the route already handles at `:498-511` — still has an object, and it is precisely the row most
likely to be deleted by hand.

Two surrounding properties carry over unchanged. The `document.agent_id != agent.id` check at
`:36-39` exists because *"if those two disagree the delete would aim one agent's id list at another
agent's namespace"*; §3.3 puts `agent_id` first in the key, so a mismatch now also produces a wrong
prefix, and the check stays one line preventing a silent failure in two stores instead of one. And
the module docstring at `:11-14` — everything reaches Pinecone through `get_vector_store(agent)` so
*"there is no parameter in this module through which a delete could be aimed at another agent's
namespace"* — is the exact shape `app/storage.py` is built in (§3.3: keys from ids, never strings
from a request).

The route above it (`documents.py:669-679`) still refuses to delete a document mid-`processing`,
and that refusal is now protecting one more thing than its comment claims.

---

## The four misattributed comments

`ingest.py:118`, `rag/jobs.py:120-121`, `documents.py:660-661` and `requirements.in:61-62` all
assert that original uploads are never stored, and three of them cite **"PRD section 7"** for it.
Verified against `PRD.md:886-950`: §7 contains no such bullet (§1.7). The citations point at a
section that does not say the thing.

Each needs a *different* correction, which is why they are listed rather than swept with one
find-and-replace.

| Site | What it says | What happens to it |
|---|---|---|
| `ingest.py:118-121` | *"Bytes, not a path. Original uploads are never stored (PRD section 7) and Render's disk is ephemeral, so there is no file to read"* | **Conclusion survives, premise does not.** `_load_text` takes bytes because the caller holds them and pypdf accepts a file-like object. Storing an original does not put a file on Render's ephemeral disk. Strike the citation and the premise; keep the design |
| `rag/jobs.py:120-123` | *"Such a row can never be RESUMED. The original bytes were never stored (PRD section 7) and existed only as this task's `data` argument"* | **Becomes false, and it is the interesting one.** It is the entire argument for *"delete it and upload again, never retry it"*. With the original in R2 a retry becomes possible for the first time — and **this change set does not build one**, so the comment must say the bytes now exist *and that nothing reads them*, or a future reader acts on a half-truth |
| `documents.py:659-662` | *"the original bytes were never stored, so it cannot be resumed, and `app/rag/jobs.py` states the recovery as 'delete it and upload again'"* | Same premise, and it justifies the `PROCESSING_STALE_AFTER` hatch at `:669-672`. The hatch stays; its reason changes from *"it cannot be resumed"* to *"nothing resumes it"* |
| `requirements.in:61-62` | *"Only pypdf is needed on top of the stdlib: markdown and text are read directly, and we deliberately do not store original uploads"* | One clause deleted. pypdf is still the only loader needed |

### And one that is user-visible

`frontend/src/views/AgentDocuments.tsx:150-152`, rendered directly under the file input:

> *"Markdown, plain text or PDF, up to 50 MB. **Original files are not stored** — text is chunked
> into Postgres and embedded into this agent's namespace."*

That is a **statement to the user about what happens to their file**, and it becomes false. It is
the only item in this change set where a stale comment is a stale *promise*, so it is corrected in
the same commit that starts storing the bytes rather than deferred to the fold-out. The replacement
states what is now true and nothing more: the original is kept, and the text is still what
retrieval reads.

---

## Contracts consumed

By reference into [`PLAN.md`](PLAN.md): **§3.3** (`agents/{agent_id}/documents/{document_id}{ext}`
— derived from ids, `ext` from `ingest.MIME_TYPES`, the user-supplied filename never in the key),
**§3.5** (object first, row second; here the put precedes the commit at `documents.py:439`),
**§3.6** (the single migration adding `documents.storage_key` — *this feature adds none*),
**§3.7** (the document delete path), **§3.9** (`error_kind` gains `storage`; no new trace event,
and the upload route emits none today).

---

## Acceptance criteria

| # | Criterion | Harness case |
|---|---|---|
| **A9** | An uploaded document's original is retrievable **byte-identically** | `agentic_check.py` **S36** (new) |

S36 uploads a fixture through `POST /api/agents/{agent_id}/documents`, waits for the row to reach
`ready`, reads `storage_key` off it, fetches the object, and compares. Three properties, each
ruling out a way of passing while proving nothing:

- **Byte-identical, not "non-empty".** `byte_size > 0` is the assertion that let a 28-byte fake
  `.pptx` through as a `ready` handout — the sixth entry in `build.md` §7's table. Compare
  `sha256` of what came back against `documents.content_hash`, which the route wrote from the same
  buffer at `:359` and `:411`, so the reference value is free and was not derived by the scenario.
- **A PDF as well as a text file.** A `.md` round-trips through almost any mistake; a PDF is where
  an encoding assumption or a text-mode handle would surface, and it is the only supported binary
  format (`ingest.py:75-80`).
- **Read through the same seam the application uses**, not through a second boto3 client the
  scenario constructs for itself. A scenario with its own client is testing R2, and R2 is not the
  thing that can regress.

And one explicit **non**-assertion: **S36 must not assert that ingest read the object.** It does
not, by design (below), and a case asserting otherwise would be asserting a feature that was
deliberately not built — which is how a green suite comes to describe a system that does not exist.

---

## What this deliberately does not do

### It does not build a re-chunk route, and it does not make `chunk_size` retroactive

§7.1 owns the reasons and they are not scope-shaped: re-chunking inherits PRD open item 18 in full
(replacing a document's chunks destroys the `query_chunks` rows of every earlier answer, so eval
history keeps its scores and loses its evidence), and `ingestion_runs` records no `splitter`, so
two runs differing only in splitter are indistinguishable on the columns that matter. Storing
originals makes a re-chunk route *possible*. It does not make it exist.

**So `AgentSettingsSheet.tsx:553-555` stays exactly as it is, word for word.** Its section title is
*"Takes effect on the next upload"* and its note reads *"Read at ingest time. Documents already
indexed keep the chunking they were ingested with -- re-upload to apply a change."* That is still
true: `chunk_size` is read once, on the loop, at `ingest.py:415-425` and handed to `_prepare_chunks`
as a plain value. **Do not edit it.**

It is worth naming the pair, because they sit two files apart and pull in opposite directions.
`AgentDocuments.tsx:150-152` describes something that becomes **false** and must change.
`AgentSettingsSheet.tsx:553-555` describes something that stays **true** and must not. Both will
look, to someone sweeping the frontend for copy affected by object storage, like the same kind of
line. Editing the second promises a behaviour nothing implements, which is worse than leaving the
first.

### It does not read the stored original anywhere

Nothing in the backend fetches a `documents` storage key back. Ingest continues to work from the
`data` argument (`documents.py:456-468`), retrieval continues to work from `chunks.text`, and the
handout path continues to work from retrieval. The bytes are written and, for now, read only by a
human or by a future feature.

That is deliberate rather than incomplete: a store with one writer and no reader is trivially
correct, and A9 is what proves it is a *store* rather than a `PUT` that returned 200.

### It does not deduplicate by content

Above. Two agents, two objects, on purpose.

### It does not change the upload limits or the rejection order

`MAX_UPLOAD_BYTES` (`documents.py:91`, 50 MB) keeps both checks in both places, with `len(data)` at
`:337` still the authoritative one — *"nothing a client sends … is consulted, because all of it is
attacker-controlled and none of it is checked against the body by anything upstream of this
function"* (`:333-336`). **The put sits after every rejection, never in place of one.** The 415 at
`:309`, the 413 at `:337`, the 422 at `:343` and the 409 at `:380` all still fire before a byte
reaches R2, which is what keeps a rejected upload from costing storage.

### It does not touch `chunks.text` or `chunks.asset_uri`

§7.4 and §7.3. `chunks.text` is a queryable corpus rather than a blob and stays where it is;
`asset_uri` gains a plausible home in this change set and still gets nothing written to it.

---

## What must keep working

- **The staged-row contract** (`documents.py:394-418`): the row exists, is committed and is
  addressable before any slow work starts, and `run_ingest_job` **adopts** it rather than creating
  one. An R2 put that moved after the commit would put an object under a key the client can already
  poll for.
- **`ids and bytes only` at the handoff** (`:448-468`), including `force` travelling positionally —
  *"a forced upload that loses the flag at this handoff is accepted with a 202 and then quietly
  written `failed`."*
- **The duplicate predicate stated twice, identically** (`documents.py:370-379`,
  `ingest.py:331-338`). This feature reads `content_hash`; it does not become a third place the
  predicate lives.
- **`mime_type` is derived by ingest from the extension** (`:402-404`, `ingest.py:303-304`), not
  from the browser's `Content-Type`. §3.3's `ext` reads the same table; it does not read the
  uploaded filename.
- **Deleting mid-`processing` stays refused** (`documents.py:669-679`), and `pending` stays
  deletable (`:655-657`).
- **The vectors-first ordering in `delete.py`** (`:50-67`) is unchanged in direction. The object
  delete joins it on the same side of the row deletes; it does not reorder anything that is already
  there.
