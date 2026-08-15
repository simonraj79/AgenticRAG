# CLAUDE.md — working notes

Conventions, insights and hard-won gotchas for this repo. **[PRD.md](PRD.md) is the
specification**; this file is the operational companion — the things that cost debugging
time and would cost it again.

---

## Commands

| Task | Command |
|---|---|
| Backend deps | `cd backend && .venv/Scripts/python.exe -m pip install -r requirements.txt` |
| Run backend | `cd backend && uvicorn app.main:app --reload --port 8000` |
| New migration | `cd backend && python -m alembic revision --autogenerate -m "..."` |
| Apply migrations | `cd backend && python -m alembic upgrade head` |
| Frontend dev | `cd frontend && npm run dev` |
| Frontend build | `cd frontend && npm run build` |
| Provision Pinecone | `python scripts/create_index.py [--dry-run]` |
| Provision Postgres | `python scripts/create_render_db.py [--dry-run]` |
| RAG end-to-end check | `backend/.venv/Scripts/python.exe scripts/slice_check.py` |
| Tear down that check | `backend/.venv/Scripts/python.exe scripts/slice_check.py --cleanup` |

---

## Conventions

- **Dependencies are resolved, then pinned.** `backend/requirements.in` holds direct
  dependencies unpinned; `requirements.txt` is `pip freeze` output and is what Render
  installs. Never hand-write a version number into `requirements.txt`.
- **Provisioning scripts are idempotent.** They detect existing resources, verify the
  configuration against the PRD, and report drift rather than recreating. Re-running one
  is always safe.
- **Secrets go into `.env` by script, never through a terminal.** `create_render_db.py`
  writes connection strings straight to the file and prints only masked confirmations.
- **Consult the LangChain MCP servers before writing or changing LangChain code.** Not
  after an import fails — before. `docs-langchain` answers *how and why*,
  `reference-langchain` gives exact signatures and module paths. LangChain 1.x moved
  symbols without deprecation shims, so training data and tutorials confidently describe
  imports that no longer resolve, and the resulting `ModuleNotFoundError` reads like a
  missing dependency rather than a moved class. Two relocations in this repo were found the
  slow way; both were one query. This outranks memory and outranks a plausible-looking
  example found anywhere else.
- **The retriever is constructed in exactly one place** (`backend/app/rag/retriever.py`).
  That is what keeps the Stage 1 → Stage 2 change a one-liner. Do not call
  `similarity_search()` anywhere else.
- **ASCII in `print()`.** The Windows console codepage mangles em-dashes into `�`. Use
  them freely in Markdown and comments, not in terminal output.

---

## Gotchas

### Embeddings and Pinecone

**`gemini-embedding-2` has no `task_type`.** The older `gemini-embedding-001` did, and
most tutorials show `task_type="RETRIEVAL_DOCUMENT"` / `"RETRIEVAL_QUERY"`. Passing it to
`gemini-embedding-2` is wrong. Convey retrieval intent in the prompt text instead.

**`gemini-embedding-2` renormalizes automatically** at non-default dimensions.
`embedding-001` did not — it required manual L2 normalization after MRL truncation, and
skipping that silently degraded cosine similarity. Do not port that normalization code.

**The embedding model is part of the index.** Indexing with one model and querying with
another returns confident nonsense rather than an error, because matching dimensions do
not imply a shared vector space. The index is tagged `embedding_model`, and
`ingestion_runs` records model + dimension per ingest. Changing the model means deleting
the index and re-ingesting.

**Plan limits differ sharply, and this account is on Builder.** On the free Starter plan
`ap-southeast-1` returns `Your free plan does not support indexes in the ap-southeast-1
region of aws`, and a sixth index returns a quota error — both bit us before the upgrade.

| | Starter (free) | **Builder (current)** | Standard |
|---|---|---|---|
| Regions | `us-east-1` only | all | all |
| Indexes | 5 | 10 | 20 |
| Namespaces/index | 100 | **1,000** | 100,000 |
| Storage/org | 2 GB | 10 GB | unlimited |

**Region is fixed at index creation, so move it while the index is empty.** Recreating
after ingest means re-embedding everything. `scripts/create_index.py --recreate` checks
`total_vector_count` and refuses to delete a populated index.

**Namespaces per index are capped by plan: Starter 100, Builder 1,000, Standard
100,000.** With one namespace per agent, that cap *is* the maximum number of agents. It
binds long before storage does — the whole 14-corpus document set is ~1.4 MB of text,
roughly 700–900 chunks, against a 2 GB allowance.

**The namespace is keyed on the AGENT, not the user.** A user owns several agents and each
must retrieve only its own corpus. `Agent.namespace` returns `agent_{id}`;
`documents.agent_id` is the scoping key and `documents.uploaded_by_user_id` is audit only.
Namespace is baked into every vector at upsert, so changing the scheme means re-ingesting.

**The SDK's `AwsRegion` enum is stale.** pinecone 8.0.0 lists only `us-east-1`,
`us-west-2`, `eu-west-1` — no `ap-southeast-1`, despite the region existing. The signature
accepts a raw `str`, so pass the string and let the API validate. The enum is not the
authority.

**Dimension, cloud and region are fixed at index creation.** No in-place change — but see
"Changing something immutable" below, which is cheaper than it sounds.

**`IndexTags` breaks `dict()`.** `describe_index()["tags"]` returns an `IndexTags` whose
`keys` attribute is `None` rather than a method, so `dict(tags)` raises
`TypeError: 'NoneType' object is not callable` — an error that points nowhere near the
cause. Use `.to_dict()`.

**`delete_namespace()` has a different signature than the docs show.** The published
Pinecone docs give `index.delete_namespace(name="...")`; the method installed in the
backend venv (pinecone 7.3.0) is `delete_namespace(namespace: str)`. The documented call
raises `TypeError`. This is the 7.x/8.x split above biting in a new place — the docs
describe 8.x, the app runs 7.x because `langchain-pinecone` pins `pinecone<8.0.0`.
`app/rag/delete.py` sidesteps it entirely by going through
`PineconeVectorStore.delete(delete_all=True)`, which also **batches ids at 1000 per
request** and **defaults to the namespace the store was constructed with** — so the
namespace stays structurally underivable from caller input rather than merely
un-passed.

**`describe_index_stats()` lags writes.** Reading a namespace's vector count immediately
after an upsert or delete can still return the previous value. Anything asserting on it —
a test, a UI badge — must poll rather than read once, or it fails in a way that looks
like a broken delete.

### Changing something "immutable"

`create_index.py --recreate` refuses to delete a populated index. That guard is not an
obstacle to work around — **the destructive path was never the correct procedure.** Use
`scripts/migrate_index.py`, which builds the replacement alongside the original.

**"Irreversible" is too blunt. There is a cost hierarchy**, and most of it is cheap:

| What changes | Re-embed? | Cost | How |
|---|---|---|---|
| Index name | No | data transfer | `migrate_index.py` |
| **Region / cloud** | **No** | data transfer | `migrate_index.py --to-region` |
| **Namespace scheme** | **No** | data transfer | `migrate_index.py --namespace-map` |
| Dimension | **Yes** | embedding API calls | rebuild from `chunks.text` |
| Embedding model | **Yes** | embedding API calls | rebuild from `chunks.text` |

**The insight: vectors can be fetched and re-upserted verbatim.** `list_paginated` →
`fetch` → `upsert` copies values and metadata bit-identically (verified: 8 vectors across
2 namespaces, us-east-1 → ap-southeast-1, values and metadata compared element-wise). Only
changes that alter what a vector *means* — dimension or model — force re-embedding.

**Blue/green, never delete-then-create:**

1. `python scripts/migrate_index.py --to-region <r> --new-name <n> --dry-run`
2. Run it for real. The old index stays live and queryable the whole time.
3. Spot-check queries against the new index.
4. Point `PINECONE_INDEX_NAME` at it, locally and on Render. Redeploy.
5. **Only then** delete the old one by hand.

The Builder plan allows 10 indexes, so there is always room to stand one up beside
another. Nothing is deleted until a human has confirmed the replacement works.

**Even the expensive case is bounded**, because `chunks.text` in Postgres is the source of
truth. A dimension or model change re-embeds from the database — it never re-parses
original uploads. That is what makes "we do not store original files" a safe design rather
than a corner we painted ourselves into.

### LangChain 1.x packaging

**Ask the official docs MCP servers before guessing where something moved.** The 1.x split
relocated several classes with no deprecation shim, so a stale import fails as
`ModuleNotFoundError` and reads like a missing dependency rather than a moved symbol. Two
servers, worth adding both — the guides say *why*, the reference gives exact signatures:

```bash
claude mcp add --transport http docs-langchain --scope user https://docs.langchain.com/mcp
```

```bash
claude mcp add --transport http reference-langchain --scope user https://reference.langchain.com/mcp
```

**`langchain` 1.x no longer bundles the text splitters.** Under 0.x,
`from langchain.text_splitter import RecursiveCharacterTextSplitter` worked because
`langchain-text-splitters` arrived transitively. Under 1.x it does not, and the import
raises `ModuleNotFoundError`. It is listed explicitly in `requirements.in`.

**`ContextualCompressionRetriever` moved to `langchain-classic`.** PRD §3.5 names this
class as the Stage 1 → Stage 2 wrapper, and it is in neither `langchain` nor
`langchain-core` in 1.x — `langchain.retrievers` does not exist as a module at all. It now
lives at `langchain_classic.retrievers`. Keeping the canonical class is worth the extra
package: hand-rolling the equivalent would make the stage change read as bespoke code
rather than the one-liner the workshop is teaching.

**`langchain-pinecone` pins `pinecone<8.0.0`, so two SDK versions coexist.** The backend
venv resolves to pinecone 7.3.0; the global interpreter that runs `scripts/` has 8.0.0.
That split is tolerable — the app only ever queries and upserts, while the admin API calls
(`describe_index`, `IndexTags.to_dict()`, the stale `AwsRegion` enum) live exclusively in
`scripts/` — but the two environments are genuinely different and a gotcha verified in one
is not automatically true in the other.

`langchain-pinecone` also drags in `langchain-openai` (and `tiktoken`) as hard
dependencies. Nothing calls OpenAI; no `OPENAI_API_KEY` is needed. `tiktoken` is used
deliberately, for chunk sizing.

**`pip freeze` strips environment markers, and that breaks the Render build.**
`langchain-mcp-adapters` pulls in `mcp`, which requires `pywin32` **only** under
`sys_platform == 'win32'`. Freeze on a Windows machine and `requirements.txt` gains a bare
`pywin32==312` with the marker gone — an unconditional requirement that Render, building on
Linux, cannot satisfy. The failure is at build time, in CI, caused by a dependency added
successfully on a developer laptop.

The line is kept as `pywin32==312; sys_platform == "win32"`. **Re-check it after every
`pip freeze`**, because freezing will silently flatten it again. Any future Windows-only
transitive dependency has the same problem.

### Gemma 4 on the Gemini API

**Structured output works, but only through a tolerant parser.** PRD §2 recorded it as
undocumented and hedged Stage 2's rewrite decision to Gemini Flash. Measured 2026-08-15,
5 trials per configuration:

| Path | T=1.0 | T=0.2 | p50 |
|---|---|---|---|
| raw `google-genai` `response_schema` | **4/5** | 5/5 | 2.2 s |
| LangChain `with_structured_output(method="function_calling")` | 5/5 | 5/5 | 3.5 s |
| LangChain `with_structured_output(method="json_mode")` | 5/5 | 5/5 | 2.6 s |
| `gemini-flash-latest`, function calling (control) | 5/5 | — | 2.3 s |

Gemma emits schema-correct JSON but sometimes wraps it in a markdown fence.
`response.parsed` is strict and returns **`None`** on that — not an exception, a `None`,
which is the worst possible failure shape for a decision the Stage 2 loop branches on.
LangChain strips the fence, and that is the entire difference between the failing row and
the passing ones; it is not a different API capability. `function_calling` avoids the text
channel altogether, so no fence can appear. **Use `function_calling`.** `DECISION_MODEL` is
now `gemma-4-31b-it`; Flash is one env var away if this ever regresses.

**Sampling defaults come from the model card, not from RAG instinct.** Gemma 4 specifies
`temperature=1.0, top_p=0.95, top_k=64` as a "standardized sampling configuration across
all use cases". The reflex for grounded RAG is temperature 0; Gemma is not calibrated for
it, and squeezing sampling far below the card's values risks repetition loops for a
determinism gain that grounding already provides. Structured-output reliability was
unaffected by temperature in the table above.

**Gemma 4 supports the system role natively.** Gemma 3 did not, which is why so much
example code sets `convert_system_message_to_human=True`. Doing that here would flatten
the grounding rules into the user turn, where they carry less weight. It stays `False`.

### Retrieval calibration (measured on one corpus file)

**The 0.5 rewrite threshold sits inside the noise, not above it.** On `3.1-lesson-gist.md`,
on-topic questions score 0.61–0.67 and off-topic ones 0.49–0.58 — a narrow band with no
clean separation. "What is the refund policy for this course?" scored **0.5765**, above
threshold, so Stage 2 would not have rewritten it.

**Refusal comes from the prompt, not the threshold.** That refund question was refused
correctly anyway, because the system prompt forbids answering outside the context. Worth
knowing which mechanism is actually doing the work: the threshold governs *rewriting*, the
prompt governs *refusing*, and only the second one was load-bearing in every case tested.
Do not treat `score_threshold` as a safety control. Stage 3 exists to turn 0.5 into a
measured number.

**Latency is dominated by generation, not by the cross-Pacific hop.** PRD §6 flags Cohere
as the only Singapore → US round trip. Measured: embed 365 ms, Pinecone k=20 394 ms,
Cohere rerank ~830 ms, **Gemma generation 13.2 s — 89% of the total**. The hop the PRD
worried about costs a twentieth of what generation does. Optimise there or nowhere.

### Render

**The API defaults `region` to `oregon`.** It does not inherit from the workspace's other
services. Omit the field and you silently provision in the wrong hemisphere, with
delete-and-recreate as the only fix. Always send `region` explicitly.

**Services can only use the private network within a region.** A Singapore database with
an Oregon backend is forced onto the public internet.

**Region is immutable** for both services and databases.

**Free Postgres expires after 30 days**, gets a 14-day grace period, and is then *deleted*
with all its data. Not a tier — a countdown timer.

**Legacy plan names are still in the API enum but rejected for new databases.**
`starter` / `standard` / `pro` exist in the schema for existing resources only. The lowest
paid tier for a new database is **`basic_256mb`**.

**`databaseName` and `databaseUser` are immutable.** Storage grows but never shrinks.

**External Postgres access is blocked by default.** A newly created database has
`ipAllowList: None`, and connections are dropped mid-handshake — surfacing as
`ConnectionDoesNotExistError: connection was closed in the middle of operation`, which
looks like a network fault rather than a firewall. Add a `/32` entry to connect locally.

**`POST /v1/services` triggers a deploy immediately.** Creating a service before there is
code to build produces a failed deploy, and Render does not document whether a
permanently-failing service still bills.

**Render appends a random suffix to service hostnames.** A service named
`agentic-rag-api` is served at `agentic-rag-api-6x6b.onrender.com`. **No URL can be
predicted before creation** — anything that needs the hostname (OAuth redirect URIs,
`VITE_API_URL`, CORS origins) must be wired *after* the service exists.
`scripts/create_render_services.py --wire` does this by reading the URLs back.

**Migrations belong in the START command, not the build command.** The internal database
hostname does not resolve from Render's build environment. `alembic upgrade head` is
idempotent, so running it on every start is harmless.

**`npm ci` needs a committed `package-lock.json`**, or the static site build fails.

**Bind `$PORT` on `0.0.0.0`.** Binding localhost passes local tests and fails Render's
health check.

**Updating env vars: use `PUT /services/{id}/env-vars/{key}`.** The keyless
`PUT /services/{id}/env-vars` *replaces the entire set* and will silently drop every other
variable.

### Database driver

Three separate traps, each producing a different misleading error. All are handled in
`backend/app/config.py` (`async_database_url` and `db_connect_args`), which is used by the
app engine *and* `alembic/env.py` — migrations fail without them too.

1. **Render hands out `postgresql://`,** which SQLAlchemy maps to psycopg2. We use
   asyncpg, so the URL must be rewritten to `postgresql+asyncpg://`.

2. **asyncpg does not understand libpq's `sslmode`** and errors if it appears in the query
   string — but Render *requires* TLS. Strip `sslmode` from the URL **and** pass TLS via
   `connect_args`. Do only the stripping and you get
   `InvalidAuthorizationSpecificationError: SSL/TLS required`.

3. **The INTERNAL endpoint presents a self-signed certificate.** A verifying context
   raises `SSLCertVerificationError: certificate verify failed: self-signed certificate`.
   The EXTERNAL endpoint has a valid public cert and verifies fine — so this passes every
   local test and fails only once deployed. Internal hostnames have no dots
   (`dpg-xxx-a`); external ones are FQDNs. Verify when the host is an FQDN, relax when it
   is not. The connection stays encrypted either way.

Trap 3 is the nastiest of the three: local development exercises the external endpoint, so
nothing warns you until the first production deploy.

### Google OAuth

**There is no API for creating Web Application OAuth clients.** Console only. Two
near-misses that waste time:
- `gcloud iam oauth-clients create` belongs to Workforce Identity Federation and only
  works with Identity-Aware Proxy.
- The IAP `projects.brands.identityAwareProxyClients` API creates real clients, but they
  are permanently locked to IAP with uneditable redirect URIs.

**The client secret is displayed exactly once.** No recovery — only regeneration.

**`Authorized JavaScript origins` and `Authorized redirect URIs` are not two places for
the same URL.** Redirect URIs are where Google sends the auth *code*, so they point at the
**backend** (the exchange needs the client secret). JavaScript origins authorize the
browser to call Google *directly*, which a server-side flow never does — ours is
deliberately empty.

**Redirect URI matching is exact** — scheme, case and trailing slash. Mismatch gives
`redirect_uri_mismatch`.

**Never derive the redirect URI from `request.url_for()`.** Behind Render's
TLS-terminating proxy it returns the internal `http://` URL, which will not match the
registered `https://` one. Pin it in config.

**Testing mode expires authorizations after 7 days.** Users get silently logged out
weekly. Publish the app — no verification is needed for `openid email profile`, which are
non-sensitive.

**The scope string must contain `openid`.** Authlib only generates a nonce when it is
present, and only attaches `token['userinfo']` when a nonce was stored. Drop it and user
info vanishes with a bare `KeyError`, nowhere near the actual cause. Scope is exactly
`openid email profile`.

**Key on `sub`, never `email`.** Google reassigns emails within a Workspace domain; `sub`
is never reused.

**`POST /api/auth/dev-login` is an authentication bypass, in a public repo, on a service
that deploys to production.** It exists because a real Google login cannot be automated —
it needs a human at a consent screen — so without it nothing downstream of identity can be
tested end to end. Three gates must *all* pass or it returns 404 (not 403; the route does
not advertise itself): `DEV_AUTH_ENABLED=true`, `ENVIRONMENT=development`, and a loopback
client address. It logs a WARNING on every success.

Two properties keep it safe rather than merely discouraged. It stores
`google_sub = "dev|<email>"`, so a dev identity can never collide with a real Google `sub`
— signing in for real creates a *separate* user row. And it reaches the same
`create_session` path as the OAuth callback, so only the identity assertion is stubbed and
the session machinery under test is the real one. **`ENVIRONMENT` defaults to
`development`**, so on Render only the flag and the loopback check hold the gate — set
`ENVIRONMENT=production` there explicitly.

**`SessionMiddleware` defaults to `same_site="lax"`.** That survives the top-level
redirect back from Google, so login appears to work — then the first XHR from React fails
because the cookie is not sent. Set `same_site="none", https_only=True` explicitly.

### Frontend and repo

**This repository is public.** Anything in a `VITE_*` variable is compiled into the bundle
and readable in devtools. The frontend gets exactly one config value: the backend URL.

**The workshop PDFs are gitignored** pending a licensing decision (PRD open item 6). Large
binaries in git are permanent — removing them later means rewriting history.

### Ragas

**Ragas needs a judge LLM *and* an embedding model**, and defaults to OpenAI for both.
Configure both explicitly or it fails on a missing `OPENAI_API_KEY`.

**`context_recall` requires a reference answer.** The other three metrics work from
question + contexts + answer alone. That is why `golden_questions.reference_answer` is not
decorative.

**Deleting a document destroys the stored contexts of every past query that cited it.**
`query_chunks.chunk_id` is `ON DELETE CASCADE` to `chunks`, and `chunks.document_id`
cascades from `documents` — so removing one source file silently empties the `contexts`
that `context_precision` and `context_recall` read. A scorecard keeps its *scores* and
loses its *evidence*, which is worse than losing both, because the numbers still render
and nothing signals that they are no longer reproducible. Verified 2026-08-15: a query
with one cited chunk left zero `query_chunks` rows after its document was deleted.
Re-verified that the write path is correct — a fresh query records rank, similarity and
rerank score properly — so this is the cascade, not a missing write. Decide before Stage 3
whether eval history should pin its contexts by copying the text, or whether deleting a
document that an `eval_run` depends on should be refused.

---

## Process notes

**Do not invent package versions.** A hand-written `httpx==0.29.0` did not exist and broke
the first install. Resolve with an unpinned `requirements.in`, then freeze.

**Provisioning failures are documentation.** Every rejection this project hit —
Pinecone's region lock, its index quota, Render's blocked external access, the TLS
requirement — was a platform constraint that no amount of reading the docs had surfaced.
They are recorded in PRD §6.2 and §8 rather than left as folklore.
