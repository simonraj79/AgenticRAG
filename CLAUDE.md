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

**Pinecone free plan: `us-east-1` only, 5 indexes maximum.** `ap-southeast-1` returns
`Your free plan does not support indexes in the ap-southeast-1 region of aws`, and a sixth
index returns a quota error. Both need the Builder plan (~$20/mo).

**The SDK's `AwsRegion` enum is stale.** pinecone 8.0.0 lists only `us-east-1`,
`us-west-2`, `eu-west-1` — no `ap-southeast-1`, despite the region existing. The signature
accepts a raw `str`, so pass the string and let the API validate. The enum is not the
authority.

**Dimension, cloud and region are fixed at index creation.** No in-place change.

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

**Bind `$PORT` on `0.0.0.0`.** Binding localhost passes local tests and fails Render's
health check.

### Database driver

These two are a **pair**, and fixing only one produces a misleading error:

1. **Render hands out `postgresql://`,** which SQLAlchemy maps to psycopg2. We use
   asyncpg, so the URL must be rewritten to `postgresql+asyncpg://`.
2. **asyncpg does not understand libpq's `sslmode`** and errors if it appears in the
   query string — but Render *requires* TLS. Strip `sslmode` from the URL **and** pass
   `connect_args={"ssl": ssl.create_default_context()}`.

Do only the stripping and you get
`InvalidAuthorizationSpecificationError: SSL/TLS required`. Both halves live in
`backend/app/config.py` (`async_database_url` and `db_connect_args`) and are used by the
app engine *and* `alembic/env.py` — a migration run will fail without them.

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
