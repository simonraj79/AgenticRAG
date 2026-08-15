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
| **Why is the DB unreachable?** | `curl -s https://api.ipify.org` — compare against the allow-list |
| **Did `pip freeze` break the build?** | `grep -n pywin32 backend/requirements.txt` — the marker must survive |
| **Which Pinecone namespaces exist?** | see "namespace counts" under *Embeddings and Pinecone* |

Both of the middle two are diagnostics for failures this project has hit **twice each**. Run
them before reading a traceback, not after.

**Local dev needs `DEV_AUTH_ENABLED=true` and `ENVIRONMENT=development` in `.env`** to use
the dev-login shim; both are absent from `.env.example` on purpose. See the Google OAuth
section for why that route is gated three ways.

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
- **API routes are nested under the agent** (`/api/agents/{agent_id}/...`) and resolve through
  `owned_agent` in `app/api/deps.py`. PRD §3.8's original flat `/api/documents` predates the
  move of tenancy from users to agents, and flat routes would have to carry the agent id in a
  body or query param — exactly the client-supplied scoping §7 forbids. Nesting makes the
  constraint structural: no request can be expressed without naming an agent. The three routes
  reached by their own id (`/api/conversations/{id}`, `/api/golden-questions/{id}`,
  `/api/eval-runs/{id}`) have no `agent_id` to bind, so they check ownership by hand — those
  are the highest-risk lines in the codebase and each says so in a comment.
- **There is one question-rewriter, not two.** History-aware contextualisation (turning "what
  is its power budget?" into a standalone question) and PRD §3.5's Stage 2 rewrite loop are
  different *triggers* — coreference versus a low top score — on the same machinery. Stage 2
  must compose with the existing rewriter in `pipeline.py` rather than add a second one, or
  the trace will show two REWRITE events with no way to tell which fired why.
- **ASCII in `print()`.** The Windows console codepage mangles em-dashes into `�`. Use
  them freely in Markdown and comments, not in terminal output. This has now broken three
  throwaway verification scripts in this repo — an emoji from `agent_templates.icon`, a `§`,
  and a `│` copied out of the repo-layout tree. It is not only about em-dashes, and it is not
  only about application code: **any** script that prints text read from the database or from
  a Markdown file will hit it. `ascii(value)` when you just need to see what is there.

---

## Gotchas

### Embeddings and Pinecone

**`gemini-embedding-2` has no `task_type`.** The older `gemini-embedding-001` did, and
most tutorials show `task_type="RETRIEVAL_DOCUMENT"` / `"RETRIEVAL_QUERY"`. Passing it to
`gemini-embedding-2` is wrong. Convey retrieval intent in the prompt text instead.

**`gemini-embedding-2` renormalizes automatically** at non-default dimensions.
`embedding-001` did not — it required manual L2 normalization after MRL truncation, and
skipping that silently degraded cosine similarity. Do not port that normalization code.

**`gemini-embedding-2` is multimodal. We use it as a text embedder, on purpose.** It is the
first multimodal embedding model in the Gemini API — text, images, video, audio and PDFs into
one *unified* space, so a text query can retrieve a page image directly. That is genuinely
useful for a slide-heavy corpus, and we are not using it, because the native path costs more
than it currently returns:

| Constraint | Value |
|---|---|
| PDF per request | **1 file, 6 pages** |
| Images per request | 6 (PNG/JPEG only) |
| Audio / video | 180 s / 120 s |
| Token ceiling | **8,192 across all modalities combined** |

So a slide deck cannot be embedded whole — it needs 6-page windowing plus halve-and-retry
when a dense window busts the token ceiling. And **`langchain-google-genai` cannot do it at
all**: `GoogleGenerativeAIEmbeddings.embed_documents` is `(texts: list[str], ...)` with no
`embed_images` (verified against 4.3.4), so multimodal means calling `google-genai` directly
and stepping outside the retriever seam that keeps the Stage 1 → Stage 2 change a one-liner.

**Do not "fix" this by switching to `gemini-embedding-001`.** That reasoning is backwards
three separate ways: the two embedding spaces are **incompatible**, so it forces a full
re-ingest of every existing namespace; -001's input ceiling is 2,048 tokens, a quarter of
-2's; it does not auto-normalize at 768d, so the manual L2 code deleted above would have to
come back; and it is text-only, so it does not make multimodal reliable — it removes the
option permanently. The fragility is in the multimodal *path*, never in the model.

The design if it is ever built: index a PDF **twice into the same namespace** — text chunks
via the existing path, plus 6-page visual windows via the native path — with
`chunks.text` for a visual window holding that window's extracted text, so generation and
re-embedding still work. Same model, same space, which is the whole point.

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

This is not hypothetical: it has now been flattened and restored **twice**, most recently by
the freeze that added Ragas. Treat "re-add the marker" as the second half of the `pip freeze`
command, not as a thing to remember:

```bash
grep -n 'pywin32' backend/requirements.txt
```

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

**And now Stage 3 has measured something the prompt alone did not fix: the coaching personas
weaken refusal.** First real golden-set run, Feynman Explainer, 2026-08-15:
**`refusal_pass = 0 / 2`.** Both questions the corpus cannot answer were answered anyway.

The cause is not a missing rule — the persona prompts all carry the grounding clause. It is
that the Feynman persona is *designed* to **name the gap** rather than decline: it says "the
material does not cover X, but here is what it does say", which is pedagogically right and is
also, structurally, an answer rather than a refusal. The behaviour the persona rewards and
the behaviour the golden set measures are in direct tension.

This is the concrete instance of the risk written into every persona prompt — that a warm,
confident teaching voice makes an ungrounded answer read better than a blunt refusal. It was
a prediction; it is now a measurement, and it is the strongest argument in this codebase for
having built Stage 3 at all. Note that the plain `lecture-qa` template was **not** tested
here, so this is a finding about coaching personas specifically, not about the system prompt
in general. Retesting the same golden set against a non-persona agent is the obvious next
experiment, and it costs one run.

**Latency is dominated by generation, not by the cross-Pacific hop.** PRD §6 flags Cohere
as the only Singapore → US round trip. Measured: embed 365 ms, Pinecone k=20 394 ms,
Cohere rerank ~830 ms, **Gemma generation 13.2 s — 89% of the total**. The hop the PRD
worried about costs a twentieth of what generation does. Optimise there or nowhere.

**Persona verbosity *is* latency.** Because generation is token-bound and already 89% of the
turn, anything that makes the model write more is the single biggest lever on response time —
larger than retrieval, reranking and the network combined. Measured 2026-08-15 on the same
one-chunk corpus:

| Turn | Output | Latency |
|---|---|---|
| Bare Stage 1 | 136 chars | **9.8 s** |
| Feynman persona, first turn | ~600 chars | **30.6 s** |
| Feynman persona, follow-up | ~1,800 chars | **44.8 s** |

Retrieval was identical in all three. The persona prompt asks for an analogy, a worked
example and a named gap, so it emits roughly ten times the text — and costs 4.5× the time.
A follow-up adds a further **3.8 s** for history contextualisation, before the question is
even embedded.

Two consequences. First, any UI copy quoting a fixed "10–15 s" becomes a promise the system
cannot keep the moment a persona is selected, and a progress note that under-promises is
worse than none — the user starts counting against it and concludes it has hung. Second,
SSE streaming (PRD §2.2, still unbuilt) went from a nice-to-have to the main outstanding UX
problem: 45 s of blank waiting is the worst part of the product now.

### Background jobs

Two things now run off the request thread — document ingest (`app/rag/jobs.py`) and eval runs
(`app/eval/jobs.py`) — and both hit the same traps.

**A `BackgroundTasks` callback must open its OWN database session.** The request's session is
already closed by the time the task runs. Passing it in, or passing ORM objects loaded from
it, fails later with a closed-connection or wrong-loop error whose traceback points at
SQLAlchemy internals rather than at the handoff. **Pass ids and bytes; re-load inside the
job.** Both job modules do this, and both say so in a comment.

**A background task that dies silently leaves a row stuck at `processing` forever**, which is
worse than a row marked `failed`, because "processing" looks like progress and nobody
investigates. Every job writes a terminal status in a `finally`, and the failure text goes
somewhere the API can surface — `documents` has no `error` column, so ingest failures land in
`audit_log` under `app.rag.ingest.INGEST_FAILURE_ACTION`. Import that constant; do not retype
the string.

**Blocking SDK calls inside `async def` are the standing deferral here.** Pinecone, Cohere
and the embedding calls are synchronous, and Render's starter plan runs a single uvicorn
worker, so a minutes-long blocking call stalls every other request. The background jobs wrap
theirs; the in-request call sites in `ingest.py` still do not. Fix them together when it
matters, not one at a time.

**A ten-question eval run takes 23–25 minutes** (measured twice: 1497 s and 1380 s). That is
ten full agent turns at 30–60 s each plus four judged calls per question. It is why the run is
a job with `progress_done`/`progress_total` and not a request, and why the UI warns before
starting rather than after.

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

**That same error means "your IP changed" far more often than it means anything else.**
It has now happened twice. The allow-list holds fixed `/32` entries, so moving between the
campus network and anywhere else silently revokes local database access — `alembic`, the
local backend and every script fail identically, with an asyncpg traceback that names no
firewall. Deployed traffic is unaffected, because Render uses the private network, so
"production works but my laptop doesn't" is the tell.

Diagnose in one command before reading the traceback:

```bash
curl -s https://api.ipify.org
```

Fix it by **PATCHing the whole list back**, not by appending — `PATCH /v1/postgres/{id}`
with `ipAllowList` replaces the entire set, exactly like the env-var `PUT` above, so every
entry you want to keep must be resent or it is dropped:

```bash
curl -s -X PATCH -H "Authorization: Bearer $RENDER_API_KEY" -H "Content-Type: application/json" -d '{"ipAllowList":[{"cidrBlock":"155.69.165.66/32","description":"campus"},{"cidrBlock":"YOUR.IP.HERE/32","description":"off-campus"}]}' https://api.render.com/v1/postgres/dpg-d9vt7v1t0dsc738c8kpg-a
```

**The database id needs its `-a` suffix** — `dpg-d9vt7v1t0dsc738c8kpg-a`. Without it the API
returns a bare 404 that reads like the database does not exist.

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

**The consent screen shows the OAuth *brand* name, not the client name — and ours is
wrong.** PRD §8 records the client as `Agentic RAG Web`, which is accurate, but signing in
renders **"You're signing back in to Bedtime Story"**: the brand/app name on the GCP project
`dsai-mod-2-group-project` belongs to an unrelated earlier app, and a brand is per-project,
not per-client. Observed in Chrome 2026-08-15. This is worse than cosmetic — a user is being
asked to hand over their identity to an app whose name they do not recognise, which is
exactly the shape of a phishing prompt, and it is the first screen a workshop attendee sees.
Fix on the Branding page of the Google Auth Platform console; there is no API for it, same
as client creation.

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
Configure both explicitly or it fails on a missing `OPENAI_API_KEY`. Only `AnswerRelevancy`
actually uses the embeddings — it generates questions back from the answer and compares them
to the original in embedding space — but `evaluate()` takes both and omitting either is the
OpenAI failure.

**Ragas will not import at all without `langchain-community<0.4`.** `ragas/llms/base.py` does
`from langchain_community.chat_models.vertexai import ChatVertexAI` at *module scope*, and
langchain-community 0.4.x **deleted** that module — only `google_palm` survives. So the
latest Ragas and the latest langchain-community are mutually incompatible out of the box, and
the failure is `ModuleNotFoundError` on an import nothing in this project wrote.

Downgrading Ragas does **not** help: every version from 0.2.15 through 0.4.3 carries the same
import (checked). The fix is pinning the *other* side. `langchain-community==0.3.31` installs
cleanly and — importantly — does **not** drag `langchain-core` back below 1.x, so the whole
LangChain 1.x stack is unaffected. Verified working together: ragas 0.4.3, langchain-community
0.3.31, langchain-core 1.5.5, langchain 1.3.15.

**Keep the deprecated `ragas.metrics` import. Do not "fix" the warning.** Importing from
`ragas.metrics` emits a DeprecationWarning pointing at `ragas.metrics.collections`, and
following it breaks the project twice over.

First, the class names differ, so a literal move is an `ImportError`:

| Old `ragas.metrics` | New `ragas.metrics.collections` |
|---|---|
| `Faithfulness` | `Faithfulness` |
| `ResponseRelevancy` | **`AnswerRelevancy`** |
| `LLMContextPrecisionWithReference` | **`ContextPrecisionWithReference`** |
| `LLMContextRecall` | **`ContextRecall`** |

Second — and this is the part that actually blocks the move — fixing the names still fails at
construction:

```
ValueError: Collections metrics only support modern InstructorLLM.
            Found: LangchainLLMWrapper.
```

The collections metrics require an `InstructorBaseRagasLLM`. For Gemini that means routing
through `instructor.from_genai()`, and Ragas' own source carries a warning that that path
sends invalid safety settings to Google (`HARM_CATEGORY_JAILBREAK`, instructor issue #1658).
`LangchainLLMWrapper` is not optional here either: it is what lets Gemma survive as a judge,
because it strips the markdown fence Gemma sometimes wraps its JSON in — the same fence that
makes raw `response.parsed` return `None` (see the Gemma section above).

So in 0.4.3 the deprecated import is the working one. `app/eval/ragas_runner.py` suppresses
the DeprecationWarning **at that import statement only**, not globally, so a deprecation from
anywhere else still surfaces. This was found by construction, not from docs — the warning is
confidently wrong for this stack.

**Exclude refusal questions from the metric means.** The golden set deliberately contains
questions the corpus cannot answer, and `golden_questions.expected_behaviour = 'refuse'` marks
them. A *correct* refusal retrieves nothing useful and returns an answer that deliberately
does not follow from the context — so faithfulness and context_recall score near zero for
behaving perfectly. Averaging them in penalises correct refusals, and worse, it aims the
weakest-metric pointer at whichever metric refusals punish hardest rather than at the real
weakness. Score them separately as pass/fail on `behaviour_ok`.

**Judge and generator are the same model right now, and that is self-assessment.**
`RAGAS_JUDGE_MODEL` defaults to `gemma-4-31b-it`, which is also `GENERATION_MODEL`. Asking a
model "is this answer supported by these contexts?" about its own output is a known
self-preference bias; PRD §2.1 specifies Gemini Flash Lite as judge for exactly this reason.
It is one env var to change, `eval_runs` records both models, and the scorecard says so on
screen — but do not read faithfulness as an independent measurement until they differ.

**`ResponseRelevancy(strictness=...)` must be 1 for Gemma. The default of 3 fails every
call.** `strictness` does not mean three requests — it asks for three *candidates* in one
request (`candidate_count=3`), and Gemma on the Gemini API answers:

> `400 INVALID_ARGUMENT: Multiple candidates is not enabled for this model`

Measured 2026-08-15: this failed **7 of 8** scored questions on the first real run. The
failure mode is the dangerous kind — it is per-metric, so the run still reported
`status=completed` with three metrics populated, `answer_relevance` almost entirely null, and
a confident weakest-metric pointer. **A metric that silently declines to measure is worse
than one that crashes the run**, because the scorecard still renders. The cost of `1` is a
noisier score (a mean over one generated question, not three); raise it only for a judge that
supports multiple candidates, which Gemini Flash does.

**Gemma is measurably unfit as a *faithfulness* judge, and this is not a theoretical worry.**
Same turn, answer copied **verbatim** out of its context, scored twice:

| Judge | faithfulness |
|---|---|
| `gemma-4-31b-it` | **0.000** |
| `gemini-flash-latest` | 0.667 |

A word-for-word copy of the context scoring zero is the judge failing, not the generator
drifting. That matters because the whole point of the scorecard is the weakest-metric
pointer: the first real run named faithfulness (0.562) as the weakest metric and advised
tightening the system prompt, when a large part of that number was judge error. **Before
acting on a low faithfulness score, re-run with `RAGAS_JUDGE_MODEL=gemini-flash-latest` and
see whether the finding survives.** Answer relevance was stable across both judges
(0.813 vs 0.811), so this is specific to faithfulness, not a general judge-quality problem.

**Context precision and recall both scoring exactly 1.0 usually means the corpus is too
small to measure.** The first run returned 1.0 and 0.9999999999 — not excellent retrieval,
but a single-chunk corpus where retrieval cannot fail. Treat a perfect retrieval score on a
tiny corpus as "not yet measured", the same as a null.

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
