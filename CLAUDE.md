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

---

## Process notes

**Do not invent package versions.** A hand-written `httpx==0.29.0` did not exist and broke
the first install. Resolve with an unpinned `requirements.in`, then freeze.

**Provisioning failures are documentation.** Every rejection this project hit —
Pinecone's region lock, its index quota, Render's blocked external access, the TLS
requirement — was a platform constraint that no amount of reading the docs had surfaced.
They are recorded in PRD §6.2 and §8 rather than left as folklore.
