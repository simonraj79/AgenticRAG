# 02 — The storage seam: the only place an S3 client is constructed

The centre of the change set. One new module, seven settings, one validator, one dependency,
one provisioning script — and **no caller**. Feature 03 is the first line of code that moves a
byte.

---

## What the user gets

Nothing. No route changes, no pixel moves, no log line in normal operation. The visible half is
feature 03.

What ships here is the property that makes feature 03 cheap and reversible: `storage_route`
flips between R2 and Postgres by editing one environment variable rather than by finding every
place a client was built, and a storage key cannot be spelled by a request because there is no
parameter through which a request could spell one. Two things do become observable, and both are
deliberate: `GET /api/config`'s `secrets_present` gains an `r2` key, and a process configured for
R2 with a blank credential **stops booting**.

---

## The seam, and why this is the third time

`app/rag/retriever.py:1-14` is *"THE SEAM -- the only place a retriever is constructed"*.
`app/rag/llm.py:1-9` is the same argument with the nouns changed, and it states the cost of not
having one plainly: there were four construction sites for `ChatGoogleGenerativeAI`, *"and moving
providers meant finding all four and getting the same four decisions right in each."*

`app/storage.py` is that idiom applied a third time, and it buys two different properties that
should not be collapsed into one.

**It is what makes the rollback a one-liner.** `storage_route="postgres"` (`PLAN.md` §3.1) is a
real road only because §3.6 keeps `handouts.content`, and it is a *cheap* road only because there
is exactly one file that knows what an object store is. This is the shape `embedding_route`
already has — and note the failure modes are opposite, which is why the validator in §C is
different from `embedding_route`'s. A wrong embedding route returns confident nonsense; a wrong
storage route reads bytes that are still there and works.

**It is what keeps key derivation structurally unable to accept a request-supplied string.**
`app/rag/delete.py:11-14` states the pattern already: everything goes through
`get_vector_store(agent)` rather than a raw index handle, *"so there is no parameter in this
module through which a delete could be aimed at another agent's namespace."* `Agent.namespace` is
derived for the same reason and `SearchCorpusArgs` carries one field for the same reason
(`PLAN.md` §1.5). A storage key derived inside this module from `uuid.UUID` arguments inherits
that property; a `key: str` in a request body would hand it back.

A third, quieter property: when every download 403s on 2027-08-17 (R-8), there is one file to
look in.

---

## Technical detail

### A. `backend/app/storage.py` — new module

The public surface. Names and signatures only; `PLAN.md` §3.3 gives the key scheme and §3.1 the
settings.

```python
def get_client() -> Any                                    # @lru_cache(maxsize=1)

def handout_key(agent_id: uuid.UUID, handout_id: uuid.UUID, extension: str) -> str
def document_key(agent_id: uuid.UUID, document_id: uuid.UUID, extension: str) -> str
def agent_prefix(agent_id: uuid.UUID) -> str

async def put_object(key: str, data: bytes, content_type: str) -> None
def presigned_get_url(key: str, *, filename: str, content_type: str,
                      ttl_s: int | None = None) -> str
async def delete_object(key: str) -> None
async def delete_prefix(prefix: str) -> int

def safe_filename(filename: str) -> str    # moved verbatim from handouts.py:243-291
```

**The three derivation functions take ids and nothing else.** A `uuid.UUID` cannot spell `../`,
cannot carry a `/`, and cannot be an empty string. That is the whole control, and it is the same
control `Agent.namespace` uses.

**`extension` is the one string, and it is allowlisted rather than trusted.** `^\.[a-z0-9]{1,8}$`
or `ValueError`. Its real sources are `Recipe.extension` (`recipes.py:147`) and `MIME_TYPES`
(`ingest.py:75-82`) — §3.3's *"`ext` comes from the mime table, not from the filename"* — so the
allowlist is never expected to fire. It is `_safe`'s own argument at one remove
(`handouts.py:258-262`): a denylist has to be right about every encoding a proxy might normalise;
an allowlist only has to be right about what an extension needs.

**Nothing takes a caller-supplied key**, and the honest form of that claim is: the three
functions above are the only producers, so any key reaching `put_object` came out of this module.
That is a property of the call graph, not of the type system — `key: str` is still a `str`. The
seam is what makes it *checkable* (A1) and what makes the day someone wants to accept a key from
a request a one-file edit that a reviewer will see.

**`presigned_get_url` is the only synchronous function, and that is a fact about SigV4 rather
than a shortcut.** Signing is a local HMAC over a canonical request; there is no round trip. So a
presign cannot time out, cannot 429, and adds no measurable latency to the download route — the
redirect is *computed*, not fetched. The other three are network I/O through a blocking SDK and
go through `asyncio.to_thread`, per CLAUDE.md's standing deferral: Render runs a single uvicorn
worker, and a 15 MB deck (`config.py:578-579`) put inline in an `async def` stalls every other
request in the process.

**`presigned_get_url` takes the RAW filename and sanitises inside the seam.** This is R-6 in
structure instead of in memory: there is no parameter that skips `safe_filename`, so a caller
cannot forget it, and §3.4's move of the sanitiser from a response header to a query parameter
cannot quietly become a deletion. Which is why `_safe` moves here — verbatim, docstring intact
(`handouts.py:243-291`) — and `handouts.py` keeps the local name with
`from app.storage import safe_filename as _safe`, leaving its one call site at `:632` untouched
until feature 03 rewrites the route. One copy, not two. The import runs `app.api` → `app.storage`,
which is the direction this codebase already flows; the reverse is what put the refusal markers in
`app/rag/refusal.py`, because `agent_loop` cannot import from `app.api`.

**`delete_object` is idempotent by protocol.** S3 answers a DELETE on an absent key with a 204,
not an error — unlike Pinecone, where `delete.py:19` imports `NotFoundException` for exactly that
case. §3.5 step 4's best-effort cleanup can therefore run twice, and so can
`migrate_bytes_to_r2.py --orphans`.

**`delete_prefix` paginates in both directions and returns a count.** `list_objects_v2` caps a
page at 1000 keys and `delete_objects` caps a request at 1000 — the same 1000-id batching CLAUDE.md
records for `PineconeVectorStore.delete`. It returns the number deleted so §3.7's agent call site
can log a number: R-2 is a leak that is invisible unless somebody counts.

**What this module does not import.** No model, no session, no `TraceRecorder` — §3.9 adds no
trace event, and a storage put is not a model decision. It imports `app.config`, the standard
library and boto3, which is what lets `storage_check.py` exercise the whole surface against a fake
client with no database and no network (§3.8).

**It raises; it does not swallow.** Deliberately unlike `validate.check` in
[12 §02](../12-robust-handouts/02-artefact-validation.md), which must never raise because it runs
inside a job whose contract is not to die. A validator that raises loses a handout; a put that
failed and returned `None` reports success for bytes that are not there. Classification into
`error_kind="storage"` (§3.9) belongs to the caller, because only the caller holds a row to write
it on.

---

### B. The four things boto3 does that this repo did not ask for

CLAUDE.md's OpenRouter section ends on a count: langchain-openai / openai-python silently put a
parameter into a request this repo did not write **three times**, and *"three occurrences across
three unrelated features is not three coincidences — it is a property of the library."* The
diagnostic that follows from it is **print the request body, do not read the call site**, because
two of those three were invisible where the call was written.

boto3 against a non-AWS endpoint is that sentence with the nouns changed, and it starts at one
known injection on day one.

| # | The thing | Why it must be pinned, and what it costs to assume |
|---|---|---|
| 1 | **Signature version** | `Config(signature_version="s3v4")`. R2 accepts SigV4 only. botocore's choice for a custom `endpoint_url` is a version-dependent default, not a contract, and a wrong one fails as an auth error |
| 2 | **Region** | `region_name="auto"`. The region string is inside SigV4's credential scope, so the wrong value returns `SignatureDoesNotMatch` — a 403 that reads as a bad key and is caused by geography. `"auto"` is R2's scope value, not a placeholder. It is unrelated to `locationHint: apac`, which is a bucket-*creation* parameter (§1.1) |
| 3 | **The credential chain** | Both keys are passed explicitly at construction. Left blank, botocore silently falls back to `AWS_ACCESS_KEY_ID`, `~/.aws/credentials`, `AWS_PROFILE` and container/IMDS — so on a laptop that has real AWS credentials the request is signed as somebody else and the 403 is attributed to anything but a blank setting. **This is the second argument for §C's validator**, and it is what makes `requirements.in`'s "no `AWS_*` variable is read" a true statement rather than an intention |
| 4 | **Flexible checksums** | botocore >= 1.36 computes and sends a checksum on every `put_object` and validates one on `get_object`, by default, with nothing about it at the call site. Whether R2 accepts the `aws-chunked` trailer encoding is exactly the class of question that must be **probed with a real put and a real get**. If it is rejected, the fix is `Config(request_checksum_calculation=..., response_checksum_validation=...)` — you cannot remove a parameter you never wrote |

A fifth candidate is **addressing style** — path versus virtual-hosted. It is listed separately
because its failure arrives late: signing succeeds, and the presigned URL fails at *fetch* time
against a host that does not resolve. So a probe that prints the URL proves nothing; a probe must
fetch it.

**What is already probed, and must not be re-derived from documentation.** §1.1, measured against
the live account on 2026-08-17: a presigned GET fetched with **no credentials** returns 200 with
the body intact; `ResponseContentDisposition` and `ResponseContentType` come back verbatim; a URL
presigned for 1 s is a **403** after 2 s; an unsigned GET is a **400**. The two override rows are
load-bearing — they are the entire reason §3.4's redirect can reproduce today's
`Content-Disposition` exactly, and therefore the reason R-6 is a move rather than a loss.

The rule underneath all of it is CLAUDE.md's, unchanged: `supported_parameters` tells you what
will route and never what will execute, and neither one tells you what your client library added
on the way out. Probe all three.

---

### C. Settings and the validator — `backend/app/config.py`

The seven settings and their defaults are `PLAN.md` §3.1 and are not restated here.

**The point worth making is that this repo has no required-secret pattern to follow.**
`database_url` (`config.py:34`), `gemini_api_key`, `pinecone_api_key`, `cohere_api_key`
(`:101-104`) and `openrouter_api_key` (`:107`) are all `str = ""` with no runtime gate anywhere;
`main.py:151-167` reports presence and enforces nothing. The sole precedent for a setting that
fails at construction is `embedding_route`'s `@field_validator` (`config.py:400-436`), and its
docstring is the argument for why prose was insufficient: *"The paragraph above claims this
setting is explicit 'because a wrong route is the one failure this subsystem cannot report'. Free
text did not deliver that claim."*

So the validation here does two separable jobs:

- **Reject a route this code does not implement.** An exact mirror, including a module-level
  `STORAGE_ROUTES` tuple beside `EMBEDDING_ROUTES` (`config.py:23`) and for the reason its comment
  gives at `:19-22` — a bare constant in a pydantic v2 model body raises
  `PydanticUserError`, and annotating it would make it a settable field. `"R2"`, `"r2 "` and `""`
  must raise rather than fall through to a road.
- **Reject `storage_route="r2"` with any of the four R2 values blank.** This half is new and it is
  a `model_validator(mode="after")`, not a field validator, because it reads four fields at once.

**Why at load.** The alternative is discovering it at the first byte movement — inside a
background job or on a user's click, minutes to days after a deploy that went green, surfacing as
"handouts are broken" with every offline harness passing. That is the failure that cannot report
itself, arriving one layer below the setting that caused it, which is the same shape
`embedding_route` was written against. Trap 3 above sharpens it further: blank credentials do not
merely fail, they hand the request to the ambient AWS credential chain.

**Not `validate_assignment`**, for the reason `config.py:424-427` already gives: `storage_check.py`
assigns these attributes to probe both roads in one process, and turning every assignment into a
validated write is a wider behaviour change than the defect being fixed. Case 74 therefore
constructs `Settings(...)` directly rather than assigning to `settings`.

**`secrets_present` gains `"r2"`** (`main.py:151-167`), and it is the AND of the three
credentials, not the presence of one. A half-filled configuration is the interesting failure, and
a key that reads `True` on one value present would hide exactly it.

**This is the first setting in this repo that can stop the process from starting, and the
ordering consequence is real.** `storage_route` defaults to `"r2"` (§3.1), so on the day this
merges a `.env` without R2 values fails at import with a pydantic `ValidationError` — not a
regression, but A4 working. Two consequences to sequence rather than discover:

1. **Render's three variables go up before the deploy that carries this code**, with a per-key
   `PUT /services/{id}/env-vars/{key}`. The keyless PUT replaces the entire set and silently drops
   everything else. Then verify by reading the values back — CLAUDE.md's Render section records
   that presence is not correctness there, measured: every required key was present and two held
   the wrong values, one of them the rate-limited trial Cohere key.
2. **`.env` must carry real assignments.** §1.2 found that lines 36-54 are a raw console paste,
   five of which parse as *keys with a `None` value*, so the credentials are currently dictionary
   key names. Nothing warns: python-dotenv downgrades its own parse errors to a logger warning and
   `extra="ignore"` (`config.py:30`) eats what survives.

`.env.example` gains the keys, empty, with §1.3's split spelled out in a comment — the `cfat_…`
management token is not an application credential and does not belong in the backend's
environment at all. `BACKEND_SECRET_KEYS` (`create_render_services.py:53-62`) gains
`R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID` and `R2_SECRET_ACCESS_KEY`, and **not** the management token —
the same exclusion `RENDER_API_KEY` gets, for the same reason. That list is read only when the
service is *created* (`:190`), and the service exists, so adding a name there is documentation
against future drift and not a way to set a value.

---

### D. `boto3` in `backend/requirements.in`

Two lines, with a comment in the register of the `langchain-openai` block at `requirements.in:38-52`:
**`boto3` is an S3-PROTOCOL client pointed at `<account>.r2.cloudflarestorage.com`. Nothing here
calls AWS, no AWS account exists, and no `AWS_*` variable is read** — the last clause being true
because trap 3 passes every credential explicitly rather than leaving it to the default chain.

`botocore` is listed **as well**, and it is not redundant. `app/storage.py` imports
`botocore.config.Config` by name, which is precisely the argument `requirements.in:94-104` makes
for promoting `pandas` and `numpy` from ragas transitives to direct dependencies: a package
imported by name whose presence depends on another package's dependency tree fails later, as an
`ImportError` nowhere near the change that caused it.

Both are already in `backend/.venv` (boto3 1.43.72, installed during the audit probe) and in
**neither** requirements file (§3.2).

**`pip freeze` will flatten `pywin32==312; sys_platform == "win32"` for the fourth time**
(`requirements.txt:115`, R-5). Restoring the marker is the second half of the freeze command, not
a follow-up task:

```bash
grep -n pywin32 backend/requirements.txt
```

---

### E. `scripts/create_r2_bucket.py`

Backend venv (§3.8). Follows `create_index.py`'s conventions exactly — the module docstring says
*"Idempotent: safe to re-run. If the index already exists, it verifies the configuration matches
what the PRD requires and reports any drift rather than recreating anything"* (`:1-10`), and the
code is `detect` (`:49-57`) → `_report` (`:107-115`) → `_check_drift` (`:117-135`).

1. `load_dotenv(ROOT / ".env")`; require `R2_ACCOUNT_ID` and the `cfat_…` management token.
   **Refuse to run on the S3 keys**, and say why in the error: §1.3, and the confusion is real
   because the access key id *is* the API token's own id. Nothing else in this change set is
   allowed to hold that token, and the backend never sees it.
2. `GET /accounts/{id}/tokens/verify`; print `status` and `expires_on`. Not decoration — R-8 is a
   dated expiry whose failure mode is every download 403ing at once with the application provably
   unchanged and every offline harness green. The one script that talks to the management API is
   where that date gets printed.
3. List buckets. If `settings.r2_bucket` exists, verify and report drift; never recreate.
4. Drift is `location` against `apac` and the name against the setting. **Location is fixed at
   creation**, exactly as a Pinecone index's region is, so drift here is reported as a
   move-it-while-empty problem in the register of `_check_drift`'s *"none of these can be altered
   in place"* — never fixed in place. Blue/green is the repo's answer (`migrate_index.py`).
5. Create when absent, with `locationHint: apac` (§1.1).
6. **Assert the bucket is not public, and treat public as a failure rather than as drift.** R2's
   managed `r2.dev` domain and any custom domain must both be absent or disabled. §1.5: a public
   object is scoped to whoever holds the URL, which deletes the per-agent authorisation
   `OwnedAgent` makes on every handout route. The check must be *positive* — a bucket can be made
   public from the dashboard by someone who never ran this script, so "we never enabled it" is not
   evidence.
7. `--dry-run` prints the plan and creates nothing (`create_index.py:87-89`).

**There is no delete path and no `--recreate`.** `create_index.py` has one and guards it by
refusing on a populated index (`:59-70`); the simpler answer is available here, and it matters
because the account is shared — `mindfulspeak-uploads` and `mindfulspeak-uploads-backup` belong to
a different project and the token reaches them (§1.1, R-9). The script operates on exactly one
name, read from settings.

**ASCII in `print()`.** No `§`, no em dash, no box-drawing character. Three throwaway scripts in
this repo have died on the Windows console codepage.

---

## Contracts consumed

By reference into [`PLAN.md`](PLAN.md): §3.1 (the seven settings and the validator), §3.2 (the
dependency and the freeze rule), §3.3 (the key scheme), §3.4 (where `safe_filename`'s output is
going, which is why it moves here), §3.8 (script conventions and interpreters), §3.9 (**no trace
event**; `error_kind="storage"` is recorded by feature 03, which has a row). Audit facts §1.1
(the probe), §1.2 (`.env`), §1.3 (two credentials), §1.5 (what is closed). Risks R-2, R-5, R-6,
R-8, R-9.

---

## Acceptance criteria

Cases from `PLAN.md` §5. All layer 1: no network, no database, a fake client.

| # | Criterion | Harness case |
|---|---|---|
| **A1** | Keys derived for a `Document` whose filename is `../../etc/passwd` contain neither the filename nor any character outside the scheme, and `handout_key(..., extension="/../x")` raises `ValueError` | `storage_check.py` **71** |
| **A2** | `presigned_get_url(filename='a"; rm -rf /')` yields a URL whose `response-content-disposition` decodes to `attachment; filename="a_rm_-rf_"` — the worked example in `_safe`'s own docstring (`handouts.py:264`) — CRLF-free after URL-decoding, with `response-content-type` equal to the mime passed | `storage_check.py` **72** |
| **A4** | `Settings(storage_route="r2", r2_access_key_id="")` raises at **construction**; the same with `storage_route="postgres"` does not; `"R2"`, `"r2 "` and `""` raise rather than selecting a road | `storage_check.py` **74** |

Two properties have no case of their own and ride inside those blocks, because they are
configuration rather than behaviour: the client is constructed with `signature_version="s3v4"`,
`region_name="auto"` and both credentials passed explicitly (traps 1–3, asserted off the
constructed client's config in **74**), and `agents/` appears as a key literal in no module but
`app/storage.py` (asserted in **71**). Whoever writes that second assertion should read
`deck_check.py` case 14 first: the equivalent check there matched its own source line on the first
run, because looking for the thing was doing the thing.

**A3 (`storage_check.py` 73 — a rollback after a put deletes the object) is enabled here and
proven in feature 03.** This feature supplies the idempotent `delete_object` that §3.5 step 4
needs; it has no transaction to roll back.

**And that is the honest limit of this document.** A1, A2 and A4 test functions. Nothing here
tests a path, because this feature has no path — a green `storage_check.py` is not evidence that
anything is stored anywhere. `agentic_check.py` S8/S34/S35/S36 and `download_ui_check.py` D1–D4
are where that gets decided, in features 03 and 05. `agentic_check.py` S3 went green twice while
proving nothing for exactly this reason.

---

## What must keep working

- **`handouts.py:632` is byte-identical after the `_safe` move.** The alias import is the whole
  edit; feature 03 rewrites the route. If that line changes here, R-6 has been walked into a
  feature early.
- **`config.py`'s existing validator and `EMBEDDING_ROUTES` are untouched**, and adding
  `STORAGE_ROUTES` beside it must not turn either constant into a settable field (`:19-22`).
- **Every script that imports `app.config` still runs**, which now means every one of them needs
  either R2 credentials or `STORAGE_ROUTE=postgres`. That includes `slice_check.py`,
  `agentic_check.py` and the eval harnesses, none of which touch storage.
- **Nothing imports `app.storage` yet.** The module is unreachable code on purpose. A grep that
  finds an import outside `scripts/storage_check.py` means feature 03 has started early.
- `backend/requirements.txt` still carries `pywin32==312; sys_platform == "win32"` after the
  freeze.

---

## What this deliberately does not do

**No caller.** No route, no job, no ORM field, no migration. The migration and all four byte paths
are feature 03; document originals are feature 04.

**No backfill.** §3.8's `migrate_bytes_to_r2.py` ships with feature 03, and §3.6 explains why no
data movement happens inside `upgrade()`.

**No bucket lifecycle rules** — no expiry, no auto-delete, no versioning. Deletion here is
explicit and code-driven (§3.7). A lifecycle rule that removes an object whose row still exists
produces a `ready` handout that 404s on click, which is the failure §3.6 keeps `content` to avoid;
adding one is a decision that wants a measurement, not a default.

**No CDN, no custom domain, no `r2.dev`.** §1.5 — a public or custom-domain bucket is reachable
without a signature, which deletes the authorisation decision `_load_owned` already makes. Item 6
of the provisioning script exists to assert the negative.

**No scoped token.** R-9 stays open: the credential is account-wide and the account is shared. A
scoped token is the answer and it is not this change set.

**No `Cache-Control: private, no-store` equivalent.** R-7, recorded as an accepted loss and
mitigated only by `r2_presign_ttl_s`. A presigned URL is a bearer capability in a URL, and this
feature does not pretend otherwise.

**No async S3 client** (`aioboto3`, `aiobotocore`). `asyncio.to_thread` around a blocking SDK is
what this repo already does for Pinecone, Cohere and the embedding calls. Adding a fourth client
library — with its own injection habits, per §B — to save a thread is not a trade this change set
makes.
