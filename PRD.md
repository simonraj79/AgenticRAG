# Agentic RAG — Product Requirements & Tech Stack

**Project:** Multi-user agentic RAG over private documents
**Source material:** NTU "Agentic RAG Harness Engineering" (Topics 10–11)
**Repository:** https://github.com/simonraj79/AgenticRAG
**Deployment target:** Render.com
**GCP project:** `dsai-mod-2-group-project`
**Last updated:** 2026-08-15

| § | Section | |
|---|---|---|
| 1 | [What we're building](#1-what-were-building) | Scope and the three stages |
| 2 | [Tech stack](#2-tech-stack) | Every layer, and what changed from the workshop |
| 3 | [Architecture](#3-architecture) | Auth, tenancy, the query pipelines, API surface |
| 4 | [Database schema](#4-database-schema) | 14 tables |
| 5 | [Environment variables](#5-environment-variables) | What each is for, which are auto-read |
| 6 | [Deployment](#6-deployment--rendercom) | Plans, regions, latency, build config |
| 7 | [Hard constraints](#7-hard-constraints) | **Read before provisioning or coding** |
| 8 | [Provisioning status](#8-provisioning-status) | What exists, what doesn't, and why |
| 9 | [Repository layout & local development](#9-repository-layout--local-development) | How to run it |
| 10 | [Open items](#10-open-items) | Known gaps and decisions still pending |
| 11 | [Out of scope](#11-out-of-scope) | Deliberate omissions |

---

## 1. What we're building

A retrieval-augmented generation system over a private document set, built in three
progressive stages, with authenticated multi-user access and a durable record of every
query, decision and evaluation.

| Stage | Name | Behaviour |
|---|---|---|
| 1 | Classic RAG | Fixed chain: ingest → chunk → embed → store → retrieve → answer |
| 2 | Agentic enhancements | Conditional query rewriting, reranking, decision tracing |
| 3 | Measurement | Golden set + Ragas scorecard across four metrics |

The distinction that matters: **Stage 1 is a chain, Stage 2 is a loop.** "Agentic" here
means the system decides which behaviour fits the current turn — rewriting fires only
when retrieval looks weak, not on every query.

Authentication and persistence are not in the workshop, which is a single-user local
build. They are added here because the deployed system is multi-user, and because a
durable trace is what makes the Stage 2 and Stage 3 deliverables meaningful beyond a
single session.

---

## 2. Tech stack

Deviations from the workshop are marked. The guiding rule: keep the taught component
unless the deployment target or available credentials genuinely force a change.

### 2.1 Core RAG pipeline

| Layer | Choice | Workshop original | Δ |
|---|---|---|---|
| Language | Python 3.11+ | Python 3.11+ | — |
| Orchestration | LangChain | LangChain | — |
| Document loaders | `PyPDFLoader`, `TextLoader` | same | — |
| Text splitter | `RecursiveCharacterTextSplitter` | same | — |
| **Embeddings** | **`gemini-embedding-2` @ 768 dims** | Ollama `nomic-embed-text` | 🔄 |
| **Vector DB** | **Pinecone serverless, 768d, cosine, `us-east-1`** | Chroma embedded (SQLite) | 🔄 |
| Retriever | `PineconeVectorStore.as_retriever()` | `Chroma.as_retriever()` | — |
| **Generation LLM** | **`gemma-4-31b-it`** via Gemini API | Ollama `llama3.2` | 🔄 |
| Decision LLM | Gemini Flash | — | ➕ |
| Reranker | Cohere `rerank-v3` | same | — |
| Output parsing | `StrOutputParser`, Pydantic parser | same | — |
| Evaluation | Ragas | same | — |
| Eval judge | Gemini Flash Lite | same | — |

**Why the three swaps.** Ollama requires model weights in memory and on disk; Render's
free tier gives limited RAM and an ephemeral filesystem, so a local model runner is
impractical. Chroma's embedded store is a SQLite file on that same ephemeral disk — it
would be wiped on every deploy and cold start. Pinecone moves the index outside the
service, making the deployed app stateless. Cohere, Ragas, LangChain and the whole
retrieval architecture are unchanged.

The workshop already required a Gemini key for the Stage 3 judge, so consolidating onto
Gemini adds no new vendor — it removes one.

**Both swapped layers sit behind LangChain interfaces.** `Embeddings` and `ChatModel` are
provider-agnostic, so only the constructor lines differ from the workshop's code; the
retriever, the chain and everything downstream are identical.

### 2.2 Application layer

| Layer | Choice | Notes |
|---|---|---|
| Backend | FastAPI | Owns all secrets and all LangChain calls |
| Frontend | React + Tailwind CSS (Vite) | Five views: Login · Ingest · Ask · Trace · Evaluate |
| Answer transport | SSE streaming | Token-by-token from FastAPI |
| **Auth** | **Google OAuth 2.0** via Authlib | Authorization Code flow, server-side |
| **Session** | **Server-side, DB-backed** | httpOnly cookie carrying an opaque token |
| **Database** | **Render Postgres 18** | Lowest paid tier |
| **ORM / migrations** | **SQLAlchemy 2.0 (async) + Alembic** | Driver: `asyncpg` |

The workshop leaves the UI open ("Streamlit, Gradio, React, or plain HTML — we won't
grade the framework"), so the frontend choice is within the rules rather than a
deviation.

### 2.3 Model specifications (verified 2026-08-15)

**`gemini-embedding-2`** — [docs](https://ai.google.dev/gemini-api/docs/embeddings)

| Property | Value |
|---|---|
| Output dimension | 768 (configurable 128–3072; recommended tiers 768/1536/3072) |
| Default if unset | 3072 |
| Max input | 8,192 tokens |
| Normalization | **Automatic** at non-default dimensions |
| `task_type` | **Not supported** — convey retrieval intent in the prompt text |

We set `output_dimensionality=768` explicitly. It matches the workshop's numbers, costs
4× less Pinecone storage than 3072, and is a recommended MRL tier.

Two notes carried over from the older `gemini-embedding-001`, both of which no longer
apply and would be wrong to copy from tutorials: `task_type` is gone, and manual L2
normalization after truncation is no longer required because this model renormalizes
automatically.

**`gemma-4-31b-it`** — [docs](https://ai.google.dev/gemma/docs/core/gemma_on_gemini_api)

| Capability | Status |
|---|---|
| System instructions | ✅ Documented |
| Function calling | ✅ Documented |
| Context window | 256K |
| Structured / JSON output | ⚠️ **Not documented** |

Because structured output is undocumented for Gemma on the Gemini API, Stage 2's rewrite
decision — which must return a typed object — routes to **Gemini Flash** instead. This is
config-driven (`DECISION_MODEL`) so it can be pointed at Gemma and tested; if structured
output works, collapse to a single model.

---

## 3. Architecture

### 3.1 Authentication flow

```
Browser → [Login] → Google consent screen
                         ↓
        Google → GET /api/auth/google/callback?code=…
                         ↓
        FastAPI exchanges code for tokens (client_secret server-side)
        Verifies ID token → upserts users row on google_sub
        Creates sessions row → sets httpOnly cookie
                         ↓
        Redirect to frontend, authenticated
```

Google's access and refresh tokens are **discarded after the identity check**. We need
identity, not Google API access, so there is nothing to store and nothing to leak.

Key on `sub`, never on `email`. Google's own guidance is explicit that email addresses can
be reassigned within a Workspace domain, while `sub` is unique and never reused.

### 3.2 Multi-tenancy

Every user's documents are isolated by **Pinecone namespace**, one namespace per user
(`user_{id}`). All retrieval is namespace-scoped, and the namespace is derived from the
session on the server — never accepted from the client.

This is the difference between real access control and a login page that only looks like
one. Without namespace scoping, every authenticated user queries one shared index and
reads everyone else's documents.

### 3.3 Indexing (runs when documents change)

```
Upload → Load → Chunk → Embed (gemini-embedding-2, 768d)
      → Upsert to Pinecone (namespace = user)
      → Record documents / chunks / ingestion_runs rows
```

### 3.4 Query — Stage 1

```
Question → Embed → Pinecone top-k (user namespace)
        → Prompt + context → gemma-4-31b-it → Answer
```

### 3.5 Query — Stage 2

```
1. Question → Embed → Pinecone top-20 (user namespace)
     ↳ top score < 0.5 ?  yes → 2   no → 3
2. Rewrite query (Gemini Flash, Pydantic-typed) → re-embed → re-search
     ↳ bounded: max 2 rewrites
3. Cohere rerank-v3  (20 → 3)
4. Prompt + 3 chunks → gemma-4-31b-it → Answer

Throughout: write a trace_events row per decision
```

Pinecone with cosine metric returns a similarity score where higher is closer, so the
workshop's `< 0.5` threshold transfers directly with no inversion.

**The retriever is the seam.** Stage 1 constructs a plain retriever; Stage 2 wraps that
same object in `ContextualCompressionRetriever` (rerank) or `MultiQueryRetriever`
(rewriting). Everything downstream — `retriever.invoke(question)` and the chain built on
it — is byte-identical between stages. Construct the retriever in exactly one place and
Stage 2 is a one-line change; scatter `similarity_search()` calls through the codebase
and it becomes a refactor.

### 3.6 Evaluation — Stage 3

For each of 10 golden-set questions: run through the agent, capture retrieved chunks and
answer, score with Ragas on faithfulness, answer relevance, context precision and context
recall. Results persist to `eval_runs` / `eval_results` so successive runs are comparable
over time.

Ragas needs **both a judge LLM and an embedding model** — both Gemini here. It defaults to
OpenAI for both, so both must be configured explicitly or it fails on a missing
`OPENAI_API_KEY`. `context_recall` additionally requires a reference answer, which is why
`golden_questions.reference_answer` is not optional.

### 3.7 API surface

| Endpoint | Method | Auth | Purpose |
|---|---|---|---|
| `/api/auth/google/login` | GET | — | Begin OAuth |
| `/api/auth/google/callback` | GET | — | Exchange code, create session |
| `/api/auth/me` | GET | ✅ | Current user |
| `/api/auth/logout` | POST | ✅ | Revoke session |
| `/api/documents` | GET/POST/DELETE | ✅ | List, ingest, remove |
| `/api/ask` | POST | ✅ | Query pipeline, SSE stream |
| `/api/queries` | GET | ✅ | Query history |
| `/api/trace/{query_id}` | GET | ✅ | Decision timeline |
| `/api/feedback` | POST | ✅ | Thumbs up/down on an answer |
| `/api/evaluate` | POST | ✅ | Run golden set |
| `/api/eval-runs` | GET | ✅ | Historical scorecards |
| `/api/health` | GET | — | Render health check |

---

## 4. Database schema

PostgreSQL 18 on Render. All tables carry `id` (UUID or bigserial) and `created_at`
unless noted.

### 4.1 Identity

**`users`** — one row per Google account
| Column | Notes |
|---|---|
| `google_sub` | Google's stable subject ID. **Unique.** The real identity key |
| `email`, `name`, `avatar_url` | From the ID token. Email may change; `sub` does not |
| `role` | `user` / `admin` — gates the Evaluate view |
| `last_login_at`, `is_active` | |

**`sessions`** — server-side sessions, survives backend restarts
| Column | Notes |
|---|---|
| `user_id` → users | |
| `token_hash` | Store a hash, never the raw token |
| `expires_at`, `revoked_at` | Enables real logout and "sign out everywhere" |
| `user_agent`, `ip_address` | Optional; see §7 on PII |

### 4.2 Corpus

**`documents`** — one row per uploaded file
| Column | Notes |
|---|---|
| `user_id` → users | Ownership; drives namespace scoping |
| `filename`, `mime_type`, `byte_size` | |
| `content_hash` | SHA-256. Detects re-uploads of identical files |
| `status` | `pending` / `processing` / `indexed` / `failed` |

**`chunks`** — one row per chunk
| Column | Notes |
|---|---|
| `document_id` → documents | |
| `chunk_index`, `text`, `token_count` | Postgres is the source of truth for chunk text |
| `pinecone_id` | The vector ID in Pinecone; links the two stores |

Storing chunk text here as well as in Pinecone metadata is deliberate — Pinecone
metadata has a per-record size limit, and having the text locally means you can re-chunk
or re-embed without re-parsing the original files.

**`ingestion_runs`** — one row per ingest job
| Column | Notes |
|---|---|
| `document_id` → documents | |
| `embedding_model`, `embedding_dimension` | **Enforces the §7 constraint** |
| `chunk_size`, `chunk_overlap` | Makes the Build #1 chunk-size experiment reproducible |
| `chunk_count`, `started_at`, `finished_at`, `status` | |

### 4.3 Query & trace

**`queries`** — one row per question asked
| Column | Notes |
|---|---|
| `user_id`, `session_id` | |
| `question`, `answer` | |
| `model_used`, `latency_ms` | |
| `prompt_tokens`, `completion_tokens` | Cost attribution |
| `refused` | Boolean — did the agent correctly decline? A Stage 3 success case |

**`trace_events`** — one row per agent decision; this table *is* the Trace view
| Column | Notes |
|---|---|
| `query_id` → queries | |
| `step_index` | Ordering within the turn |
| `event_type` | `RETRIEVE` / `SCORE_CHECK` / `REWRITE` / `RERANK` / `GENERATE` / `REFUSE` |
| `payload` | **JSONB** — shape varies by event type; Postgres can still query into it |
| `score`, `duration_ms` | |

**`query_chunks`** — join table: what was retrieved for a given query
| Column | Notes |
|---|---|
| `query_id`, `chunk_id` | |
| `rank`, `similarity_score`, `rerank_score` | Before/after reranking — the Stage 2 demo |

This table does double duty: it powers citations in the UI, and it supplies the
`contexts` field Ragas needs for context precision and recall.

### 4.4 Evaluation

**`golden_questions`** — the fixed test set
| Column | Notes |
|---|---|
| `question`, `reference_answer` | Reference answer required by `context_recall` |
| `expected_behaviour` | `answer` / `refuse` — refusal is a correct outcome |
| `is_active` | Retire questions without deleting history |

**`eval_runs`** — one row per scorecard
| Column | Notes |
|---|---|
| `user_id`, `judge_model`, `status` | |
| `started_at`, `finished_at` | |
| `notes` | What changed since last run — the point of eval-driven development |

**`eval_results`** — one row per question per run
| Column | Notes |
|---|---|
| `eval_run_id`, `golden_question_id`, `query_id` | |
| `faithfulness`, `answer_relevance`, `context_precision`, `context_recall` | |

### 4.5 Supporting tables

**`feedback`** — `query_id`, `user_id`, `rating` (+1/−1), `comment`.
Ten golden questions catch regressions; real users catch what you didn't think to test.

**`api_usage`** — `user_id`, `provider`, `operation`, `units`, `estimated_cost`.
The workshop names cost blowout as one of four agentic failure modes. Per-loop token
accounting is how you see it coming rather than discovering it on a bill.

**`audit_log`** — `user_id`, `action`, `resource_type`, `resource_id`, `metadata` JSONB.
Day 1 lists "a full audit trail of every retrieval" as one of the reasons to build rather
than buy. This table is what makes that claim true.

### 4.6 Indexes

`users(google_sub)` · `sessions(token_hash)` · `sessions(user_id, expires_at)` ·
`documents(user_id)` · `chunks(document_id)` · `chunks(pinecone_id)` ·
`queries(user_id, created_at DESC)` · `trace_events(query_id, step_index)` ·
`eval_results(eval_run_id)`

---

## 5. Environment variables

Local values live in `.env` (gitignored); production values are set in Render's
environment settings. `.env.example` is the committed template.

| Variable | Consumer | Auto-read by SDK |
|---|---|---|
| `GEMINI_API_KEY` | Embeddings, generation, Ragas judge | ✅ `langchain-google-genai` |
| `PINECONE_API_KEY` | Vector store | ✅ `pinecone` SDK |
| `PINECONE_INDEX_NAME` | Vector store | ❌ app convention |
| `COHERE_API_KEY` | Stage 2 reranker | ✅ `langchain-cohere` |
| `DATABASE_URL` | SQLAlchemy — **external** URL, local dev | ❌ app convention |
| `DATABASE_URL_INTERNAL` | SQLAlchemy — **internal** URL, on Render | ❌ app convention |
| `GOOGLE_OAUTH_CLIENT_ID` | Authlib | ❌ app convention |
| `GOOGLE_OAUTH_CLIENT_SECRET` | Authlib | ❌ app convention |
| `OAUTH_REDIRECT_URI` | Authlib | ❌ must match Google console exactly |
| `SESSION_SECRET_KEY` | Cookie signing | ❌ app convention |
| `FRONTEND_URL` | CORS allowlist, post-login redirect | ❌ app convention |
| `RENDER_API_KEY` | Local deploy tooling only | ❌ **never** used by the app |

`langchain-google-genai` checks `GOOGLE_API_KEY` first, then `GEMINI_API_KEY`.

No `PINECONE_ENVIRONMENT` is needed — legacy pod-based configuration. Serverless indexes
carry their own cloud and region, fixed at creation.

**Two database URLs, deliberately.** The external URL crosses the public internet and is
what local development uses; the internal URL stays on Render's private network and is
substantially faster, but only resolves from a service in the same region. On Render, set
`DATABASE_URL` to the internal value. Both were written into `.env` by
`scripts/create_render_db.py`.

**Pin `OAUTH_REDIRECT_URI` rather than deriving it.** Behind Render's TLS-terminating
proxy, `request.url_for()` returns the internal `http://` URL, so Authlib would send a
redirect URI that does not match the one registered with Google, producing
`redirect_uri_mismatch`. Reading it from config keeps the string identical to the console
entry.

---

## 6. Deployment — Render.com

### 6.1 Services and plans

| Component | Service type | Plan | Region | Cost |
|---|---|---|---|---|
| React frontend | Static Site | Free (no paid tiers exist) | Global CDN | $0 |
| FastAPI backend | Web Service | Lowest paid (`starter`) | **Singapore** | ~$7/mo |
| PostgreSQL 18 | Render Postgres | Lowest paid (`basic_256mb`) | **Singapore** | ~$6–7/mo |

Estimated total: **~$13–14/mo**, plus Postgres storage at roughly $0.30/GB/month. Render's
pricing page is JavaScript-rendered and could not be read programmatically, so the
per-service figures are approximate; the Postgres instance is provisioned and its actual
charge will be visible on the first invoice.

Static sites have no paid instance tiers — they are free and CDN-served, with outbound
bandwidth and build minutes counted against the workspace's monthly allowance.

**Why Postgres is not on the free plan.** Free Render Postgres expires 30 days after
creation, followed by a 14-day grace period, after which Render deletes the database and
all of its data. For a system whose whole point is a durable record, that is a countdown
timer, not a tier.

**Why the backend is not on the free plan.** Free web services spin down after 15 minutes
of inactivity and take roughly a minute to wake. The first request after idle is slow —
and an OAuth callback is a particularly bad place to wait a minute, because the user is
mid-login with no feedback. The lowest paid tier buys always-on.

### 6.2 Regions

| Component | Region | Reason |
|---|---|---|
| Render Postgres | **Singapore** | Data residency; closest to users |
| Render backend | **Singapore — required** | Render services can only use the private network **within a region** |
| Pinecone index | AWS **`us-east-1`** | Forced: Pinecone's free plan permits no other region |

**The backend's region is not optional.** Render states plainly that services in different
regions cannot communicate over a private network. A Singapore database with an Oregon
backend would be forced onto the external connection string over the public internet —
slower, and needlessly exposed.

**Pinecone is in the US, and this is a constraint rather than a choice.** Attempting to
create the index in `ap-southeast-1` returns:

> `Your free plan does not support indexes in the ap-southeast-1 region of aws.`

Singapore requires Pinecone's Builder plan (~$20/mo), which would take the monthly total
from ~$13–14 to ~$33–34. That is also why the pre-existing indexes on this account are all
`us-east-1` — it is the only region the free plan allows, not a stylistic default.

The accepted cost is a trans-Pacific hop on every vector search, and Stage 2 issues two to
three of them per question. Two things make this tolerable:

- Cohere's reranker is US-hosted with no Singapore endpoint, so Stage 2 already contains
  one mandatory trans-Pacific call regardless.
- Chunk text lives in Postgres in Singapore; Pinecone holds vectors and minimal metadata.
  Document text therefore does not leave Singapore — though embeddings are not a hard
  privacy guarantee, so this reduces exposure rather than eliminating it.

If latency or residency later matters more than $20/mo, upgrading and re-creating the
index in `ap-southeast-1` requires a full re-ingest — cheap at workshop corpus size, which
is what makes starting on the free plan a low-regret decision.

### 6.3 Expected latency budget

| Hop | Path | Rough cost |
|---|---|---|
| Backend ↔ Postgres | Private network, same region | sub-millisecond |
| Backend ↔ Gemini | Global endpoint | varies with model and prompt |
| **Backend ↔ Pinecone** | **Singapore ↔ US** | **~200–250ms RTT, 2–3× per Stage 2 query** |
| **Backend ↔ Cohere Rerank** | **Singapore ↔ US** | ~100–300ms plus transit, once per query |

The two cross-Pacific hops are the dominant network cost. Both are US-side and neither is
avoidable on the current plans. Measured against multi-second LLM generation they are
noticeable rather than fatal, but they are the first place to look if answer latency
disappoints — and the Pinecone one is fixable with a $20/mo plan upgrade.

### 6.4 Build and start configuration

Both Render services deploy from https://github.com/simonraj79/AgenticRAG.

| | Backend (Web Service) | Frontend (Static Site) |
|---|---|---|
| Service name | `agentic-rag-api` | `agentic-rag-web` |
| Hostname | `agentic-rag-api-6x6b.onrender.com` | `agentic-rag-web-e9e9.onrender.com` |
| Root directory | `backend` | `frontend` |
| Runtime | Python 3.12.10 | Node |
| Plan / region | `starter` / `singapore` | free / CDN |
| Build command | `pip install -r requirements.txt` | `npm ci && npm run build` |
| Start / publish | `alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port $PORT` | Publish `dist` |
| Health check path | `/api/health` | — |

**Binding.** The backend must bind Render's injected `$PORT` on `0.0.0.0`. Binding
localhost passes local testing and fails Render's health check.

**Migrations run at START, not at build.** The internal database hostname does not resolve
from Render's build environment, so `alembic upgrade head` belongs in the start command.
It is idempotent, so re-running it on every restart is harmless, and a schema change still
ships with the deploy that needs it.

**`npm ci` requires `package-lock.json` to be committed.** It is.

### 6.5 Cross-origin configuration

The static site and the backend are different origins, so:

- The session cookie needs `SameSite=None; Secure`.
- CORS must set `allow_credentials=True` with an explicit origin allowlist — never `*`,
  which browsers reject outright in combination with credentials.
- Starlette's `SessionMiddleware` defaults to `same_site='lax'`. That survives the
  top-level redirect back from Google but breaks as soon as React makes an XHR expecting
  the cookie, so it must be set explicitly.

If third-party cookie restrictions become a problem, the alternative is to proxy `/api/*`
from the static site to the backend, making the API same-origin and removing the issue
entirely.

---

## 7. Hard constraints

These are the decisions that are expensive, impossible to reverse, or fail silently.

**The embedding model is part of the index, not part of the query code.** Indexing with
one model and querying with another produces confident garbage rather than an error,
because matching dimensions do not imply a shared vector space. The Pinecone index is
tagged `embedding_model: gemini-embedding-2`, and `ingestion_runs` records the model and
dimension per ingest so a mismatch is detectable rather than mysterious.

**Pinecone index dimension is immutable.** Fixed at creation. Wrong dimension means
delete, recreate, re-ingest. We are committed to 768.

**Region is immutable on both platforms.** Render "doesn't currently support changing the
region for an existing service or database" — correcting a mistake means creating a new
service and migrating data across. A Pinecone index's cloud and region are likewise fixed
at creation. Together with the dimension above, that makes **three create-time decisions
with no undo**: Pinecone dimension (768), Pinecone region (`us-east-1`), and Render region
(Singapore, for every service).

**The Render API defaults `region` to `oregon`.** Omitting the field does not inherit the
workspace's other services — it silently provisions in the wrong hemisphere, and the only
fix is delete-and-recreate. Every Render API call in `scripts/` sends `region` explicitly.

**Render Postgres `databaseName` and `databaseUser` are also immutable**, as is the
direction of a legacy→flexible plan migration. Storage can grow but never shrink.

**Retrieval must be namespace-scoped, server-side.** The namespace comes from the session,
never from the request body. A client-supplied namespace is a cross-tenant read waiting to
happen.

**No API keys in the frontend.** Anything in a `VITE_*` or `REACT_APP_*` variable is
compiled into the JS bundle and readable in devtools. Every key stays in FastAPI. This
matters more than usual here because **the repository is public.**

**`RENDER_API_KEY` never goes on the deployed service.** It is an account-management
credential that can create and delete services. Given this system feeds untrusted
retrieved text into an LLM, keeping a control-plane key out of that process is basic
blast-radius control.

**Store session token hashes, not tokens.** A database read should not yield a working
credential.

**Discard Google's OAuth tokens after identity verification.** We need `sub`, not API
access. Nothing stored is nothing breached.

**The OAuth scope string must contain `openid`.** Authlib only generates a nonce when the
scope includes it, and only attaches `token['userinfo']` when a nonce was stored. Drop
`openid` and user info silently vanishes, surfacing as a bare `KeyError` rather than as an
auth error. The scope is exactly `openid email profile`.

**IP addresses are personal data** under GDPR/PDPA. `sessions.ip_address` is optional —
truncate the last octet or omit it unless there is a stated reason to keep it.

---

## 8. Provisioning status

As of 2026-08-15. Scripts under `scripts/` are idempotent and safe to re-run.

| Resource | Status | Detail |
|---|---|---|
| Render Postgres | ✅ **Live** | `agentic-rag-db` · `dpg-d9vt7v1t0dsc738c8kpg-a` · `basic_256mb` · **singapore** · PG **18** · `available` |
| Pinecone index | ✅ **Live** | `agentic-rag-ntu` · **768d** · cosine · serverless aws **us-east-1** · `Ready` |
| Google OAuth client | ✅ **Live** | `Agentic RAG Web` · Web application · `dsai-mod-2-group-project` · **In production** · External |
| GitHub repo | ✅ **Live** | https://github.com/simonraj79/AgenticRAG (public) |
| Render web service | ✅ **Live** | `agentic-rag-api` · `srv-d9vtuhpt0dsc738dmgsg` · `starter` · **singapore** · https://agentic-rag-api-6x6b.onrender.com |
| Render static site | ✅ **Live** | `agentic-rag-web` · `srv-d9vtuj61egvs73fdfang` · free · https://agentic-rag-web-e9e9.onrender.com |

**All six resources are provisioned.** Verified end to end: `/api/health` returns
`{"status":"ok","database":"ok"}` from Singapore against the private-network Postgres, and
the static site serves.

Pinecone host: `agentic-rag-ntu-o3j2ojr.svc.aped-4627-b74a.pinecone.io`
Pinecone tags: `embedding_model=gemini-embedding-2`, `dimension=768`, `project=agentic-rag-ntu`

**Postgres.** Created via `scripts/create_render_db.py`. Connection strings were written
directly into `.env` without being printed to a terminal. External access may require
adding your IP to the database's allow-list in the Render dashboard.

**Pinecone.** Created via `scripts/create_index.py`. Provisioning failed twice first, and
both failures are recorded because they constrain the architecture:

1. `ap-southeast-1` was rejected — the free plan permits only `us-east-1` (see §6.2).
2. Creation was then rejected on quota: the free plan caps a project at 5 serverless
   indexes and the account held exactly 5.

The quota was resolved by removing two unused indexes (`tbllive`, `localflowise`), leaving
`postgrespace`, `pacecoursedemo` and `pace` untouched. Re-running the script is safe: it
detects the existing index, verifies dimension, metric and region against this document,
and reports drift rather than recreating anything.

**Web service and static site.** Created via `scripts/create_render_services.py` *after*
the scaffold was pushed, because `POST /v1/services` triggers a deploy immediately and a
service with nothing to build fails on creation. Two findings from the run:

**Render appends a random suffix to service hostnames.** The service named
`agentic-rag-api` resolves at `agentic-rag-api-6x6b.onrender.com`, not
`agentic-rag-api.onrender.com`. No hostname can be predicted before creation, which
invalidated the redirect URI registered earlier — it has since been corrected in the
Google console. `scripts/create_render_services.py --wire` reads the real hostnames back
and sets `FRONTEND_URL`, `OAUTH_REDIRECT_URI` and `VITE_API_URL` accordingly.

**The first backend deploy failed on TLS.** The internal Postgres endpoint presents a
self-signed certificate, so a verifying SSL context raised
`SSLCertVerificationError: self-signed certificate`. The external endpoint has a valid
public certificate, which is why local development had worked. `app/config.py` now
verifies against external FQDNs and relaxes verification only on the private network —
the connection stays encrypted either way. See §7.

**Google OAuth — created by hand, because no API exists.** There is no gcloud command and
no public API for creating a Web Application OAuth client. Two near-misses to avoid if
anyone tries to automate this later: `gcloud iam oauth-clients create` belongs to Workforce
Identity Federation and only works with Identity-Aware Proxy, and the IAP
`projects.brands.identityAwareProxyClients` API produces clients permanently locked to IAP
with uneditable redirect URIs. The client was created by driving the console directly.

| Setting | Value |
|---|---|
| Client name | `Agentic RAG Web` |
| Type | Web application |
| GCP project | `dsai-mod-2-group-project` (number 722888382160) |
| Publishing status | **In production** (not Testing) |
| User type | External |
| Authorized redirect URI 1 | `http://localhost:8000/api/auth/google/callback` |
| Authorized redirect URI 2 | `https://agentic-rag-api.onrender.com/api/auth/google/callback` |
| Authorized JavaScript origins | *(deliberately empty)* |

Client ID and secret are in `.env`, with a generated `SESSION_SECRET_KEY`.

**JavaScript origins are empty on purpose.** They are only needed if React calls Google
directly from the browser. This is a server-side authorization-code flow: the code exchange
requires the client secret, which must never reach the browser, so the redirect URI points
at FastAPI rather than at React. The two fields are not two places for the same URL.

**Redirect URI 2 is a prediction.** It assumes the Render web service will be named
`agentic-rag-api`. If Render assigns a different hostname, this entry must be corrected —
redirect URIs are editable at any time, unlike the immutable settings in §7. Matching is
exact: scheme, case and trailing slash all count.

**Already in production, so no 7-day expiry.** Had it been left in Testing, authorizations
would expire after 7 days and users would be silently logged out weekly. The "0 / 100 user
cap" on the Audience page applies only to unapproved sensitive or restricted scopes, which
this app does not request.

**Data Access lists no scopes, which is expected.** `openid`, `email` and `profile` are
base OIDC scopes granted without explicit registration. If sign-in ever fails with a scope
error, the Data Access page is the first place to look.

---

## 9. Repository layout & local development

### 9.1 Proposed layout

```
AgenticRAG/
├── backend/
│   ├── app/
│   │   ├── main.py              FastAPI app, CORS, middleware
│   │   ├── config.py            Settings from env
│   │   ├── api/                 Route modules (§3.7)
│   │   ├── auth/                Authlib wiring, session management
│   │   ├── db/                  SQLAlchemy models + session factory
│   │   ├── rag/
│   │   │   ├── retriever.py     THE SEAM — built in exactly one place
│   │   │   ├── ingest.py        Load → chunk → embed → upsert
│   │   │   ├── pipeline.py      Stage 1 chain / Stage 2 loop
│   │   │   └── trace.py         Decision logging
│   │   └── eval/                Ragas wiring, golden set runner
│   ├── alembic/                 Migrations
│   └── requirements.txt
├── frontend/
│   ├── src/                     Five views (§2.2)
│   ├── package.json
│   └── vite.config.ts
├── scripts/
│   ├── create_index.py          Pinecone provisioning (idempotent)
│   └── create_render_db.py      Render Postgres provisioning (idempotent)
├── docs/                        Workshop PDFs
├── .env.example
├── .gitignore
├── render.yaml                  Infrastructure as code (optional)
└── PRD.md
```

`rag/retriever.py` exists as its own module specifically because of §3.5 — one
construction site for the retriever is what keeps Stage 2 a one-line change.

### 9.2 Running locally

Backend:

```bash
cd backend && python -m venv .venv && .venv/Scripts/activate && pip install -r requirements.txt
```

```bash
alembic upgrade head
```

```bash
uvicorn app.main:app --reload --port 8000
```

Frontend:

```bash
cd frontend && npm install && npm run dev
```

Local defaults assume the backend on `http://localhost:8000` and the frontend on
`http://localhost:5173`, which are the values in `.env.example` and the registered
localhost redirect URI.

---

## 10. Open items

Infrastructure is complete. What remains is application code.

| # | Item | Blocking? |
|---|---|---|
| 1 | Implement OAuth routes + session middleware (`app/auth/`) | Yes — everything is behind login |
| 2 | Implement ingest pipeline (`app/rag/ingest.py`) | Blocks Stage 1 |
| 3 | Implement retriever seam + Stage 1 chain (`app/rag/`) | Blocks Stage 1 |
| 4 | Add RAG dependencies (langchain, langchain-google-genai, langchain-pinecone, langchain-cohere, pypdf) | Yes |
| 5 | Implement Stage 2 loop + trace writing | Blocks Stage 2 |
| 6 | Build the 10-question golden set + Ragas wiring | Blocks Stage 3 |
| 7 | Build the five React views | Yes — UI is a scaffold only |
| 8 | Test whether `gemma-4-31b-it` supports structured output; if so, drop `DECISION_MODEL` | No |
| 9 | Decide whether the workshop PDFs belong in a public repo | No — see below |

**Resolved:** the local IP `155.69.165.66/32` is on the Postgres allow-list; the redirect
URI has been corrected to the real Render hostname; `DATABASE_URL` on the service is the
internal URL.

**Watch:** that allow-list entry is a single IP on what looks like a campus network. If
your public IP changes, local database access stops working and
`scripts/create_render_db.py` will need the new address added. Deployed traffic is
unaffected — it uses the private network.

**On item 6.** The repository is public and the three source PDFs total roughly 17 MB of
NTU course material. Committing them is a copyright question rather than a technical one,
and large binaries in git are permanent — removing them later requires rewriting history.
Options: keep them local and gitignored, or confirm the course permits redistribution.
The `docs/` directory in §9.1 assumes they stay; adjust if not.

---

## 11. Out of scope

Acknowledged as real production concerns, deliberately not built:

- Indirect prompt injection defenses (the five-layer model in the deck) — awareness only
- Multi-turn conversational memory
- Semantic caching, model routing, token budgets
- Retries, timeouts, and fallbacks on external calls
- Human-in-the-loop approval gates
- Graph RAG / entity extraction
- Background knowledge agents (corpus freshness monitoring)
