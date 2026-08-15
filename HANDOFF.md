# HANDOFF — start here

Written 2026-08-15, at the point where **all infrastructure is provisioned and verified**
and **the RAG vertical slice runs end to end**. Auth, the API layer and every real React
view are still unwritten. This document is the orientation for picking the work back up in
a fresh session.

## The three documents

| File | What it is | When to read it |
|---|---|---|
| **HANDOFF.md** | This file — current state and what to do next | First |
| **[PRD.md](PRD.md)** | The specification: stack, architecture, schema, deployment | Before designing anything |
| **[CLAUDE.md](CLAUDE.md)** | Conventions and platform gotchas that cost debugging time | Before touching infra or the DB driver |

Do not re-derive decisions already recorded in PRD §7 (hard constraints) or the decisions
table in §10. They were made deliberately, and several are expensive to reverse.

## Before writing any LangChain code — use the LangChain MCP servers

**This is not optional and not a fallback for when something breaks.** Query them *first*,
every time the task touches LangChain — imports, chains, retrievers, splitters, agents,
structured output, anything.

| Server | Use it for |
|---|---|
| `docs-langchain` | Concepts, guides, how-tos — the *why* and the recommended pattern |
| `reference-langchain` | Exact signatures, parameters, and module paths — the *where* |

Already configured at user scope. If a fresh machine lacks them:

```bash
claude mcp add --transport http docs-langchain --scope user https://docs.langchain.com/mcp
```

```bash
claude mcp add --transport http reference-langchain --scope user https://reference.langchain.com/mcp
```

**Why this rule is worth a section of its own.** LangChain 1.x relocated symbols with no
deprecation shims. Tutorials, blog posts and model training data all confidently show
imports that no longer resolve, and the failure — `ModuleNotFoundError` — reads as a
missing dependency rather than a moved class, which sends you to check your install instead
of the docs. This repo hit it twice in one afternoon (`langchain.text_splitter`,
`ContextualCompressionRetriever`), each costing several minutes of trial and error and each
answerable by one query. Treat the reference server as outranking both memory and any
example found elsewhere.

---

## Current state — verified live 2026-08-15

### Infrastructure: complete

| Resource | Identifier | Notes |
|---|---|---|
| Pinecone index | `agentic-rag-ntu` | 768d · cosine · aws `ap-southeast-1` · **Builder plan** |
| Pinecone host | `agentic-rag-ntu-o3j2ojr.svc.aps-d9bb-582b.pinecone.io` | **5 vectors in 1 namespace** — the slice-check agent, see below |
| Render Postgres | `dpg-d9vt7v1t0dsc738c8kpg-a` | `agentic-rag-db` · Singapore · PG 18 · 16 tables migrated |
| Render backend | `srv-d9vtuhpt0dsc738dmgsg` | https://agentic-rag-api-6x6b.onrender.com |
| Render static site | `srv-d9vtuj61egvs73fdfang` | https://agentic-rag-web-e9e9.onrender.com |
| Google OAuth client | `Agentic RAG Web` | project `dsai-mod-2-group-project` · **in production** |
| GitHub | https://github.com/simonraj79/AgenticRAG | public · clean tree · **`main` is 1 commit ahead of `origin`** |

Health check returns `{"status":"ok","version":"0.1.0","database":"ok"}`. Cost is roughly
**$33–34/mo** (Render backend ~$7, Postgres ~$6–7, Pinecone Builder ~$20).

> **The RAG slice is committed but not pushed.** `04f89a6` holds `backend/app/rag/`,
> `scripts/slice_check.py`, the `config.py` model decisions and the dependency additions.
> The working tree is clean; `main` sits one commit ahead of `origin/main`. Push when you
> are ready — the repository is public, so the push is what makes this code readable by
> anyone, not the commit.

### Code: scaffold plus a working RAG slice

**Exists:**

```
backend/app/
├── config.py      settings; URL rewriting + TLS fixes (do not "simplify" these);
│                  model ids, sampling defaults, rerank model
├── main.py        /api/health, /api/config, CORS, SessionMiddleware
├── db/
│   ├── models.py  all 16 tables from PRD §4
│   └── session.py async engine
└── rag/
    ├── retriever.py  THE SEAM - embeddings, vector store, retriever, scored search
    ├── ingest.py     load -> split -> embed -> upsert + documents/chunks/ingestion_runs
    └── pipeline.py   Stage 1 chain: retrieve -> prompt -> answer
frontend/src/      3 files: status card + four placeholder tiles
scripts/           create_index · create_render_db · create_render_services ·
                   migrate_index · slice_check
```

**Does not exist:** `app/auth/`, `app/api/`, `app/eval/`, `rag/trace.py`, the Stage 2 loop,
and every real React view.

**Verified working** (`scripts/slice_check.py`, 2026-08-15): a 12.5 KB markdown file
ingests to 5 chunks of 592–777 tokens, upserts into `agent_{id}`, retrieves at 0.61–0.67,
reranks through Cohere and produces a grounded, correctly-cited answer. Off-corpus
questions are refused. Run `--cleanup` to drop the namespace and seed rows — those are the
5 vectors currently in the index.

**Dependencies added this session:** `langchain`, `langchain-text-splitters`,
`langchain-classic`, `langchain-google-genai`, `langchain-pinecone`, `langchain-cohere`,
`pypdf`, `langchain-mcp-adapters`. The last one is installed but **nothing imports it yet** —
it is present so the Stage 2 agent can be given real MCP tools without a dependency change
mid-build. `langchain-text-splitters` and `langchain-classic` are explicit because 1.x
stopped providing them transitively.

---

## The vertical slice: done, and what it settled

The three assumptions the slice existed to test are now answered. Full detail is in
CLAUDE.md; the decisions are these.

1. **Does `gemma-4-31b-it` support structured output? Yes — through LangChain.**
   `with_structured_output(method="function_calling")` returned a valid Pydantic object
   10/10 across two temperatures. The raw `google-genai` `response_schema` path managed
   only 4/5 at Gemma's recommended temperature: the model occasionally wraps its JSON in a
   markdown fence, and `response.parsed` answers that with `None` rather than an error.
   LangChain strips the fence; function calling never opens the text channel at all.
   **`DECISION_MODEL` is now `gemma-4-31b-it`** — collapsed to one model, as PRD §2
   anticipated. Flash is one env var away.

2. **Is `gemini-embedding-2` at 768d good enough? Good enough to answer, not to
   threshold.** On-topic questions score 0.61–0.67, off-topic 0.49–0.58. The answers are
   well grounded, but that band is narrow and **PRD §3.5's `< 0.5` rewrite threshold sits
   inside it** — a refund-policy question scored 0.5765 and would not have triggered a
   rewrite. Refusal held anyway, because the *prompt* refuses, not the threshold. Treat
   0.5 as a placeholder awaiting Stage 3, and never as a safety control.

3. **Are 800/120 chunks right? Plausible.** A 12.5 KB markdown file split into 5 chunks of
   592–777 tokens, which is the intended shape. Not yet measured against retrieval quality
   — that is still Stage 3's job.

One finding nobody asked for: **generation is 89% of query latency** (13.2 s of ~14.8 s).
Pinecone in Singapore answers in 394 ms and the Cohere hop to the US costs ~830 ms. PRD §6
treats that cross-Pacific hop as the latency risk; measured, it is a rounding error next
to Gemma. If query latency needs to come down, it comes down at the generation step.

Re-run any time:

```bash
backend/.venv/Scripts/python.exe scripts/slice_check.py
```

## Then, in order

| # | Item | Blocks |
|---|---|---|
| 1 | Seed the 3 `agent_templates` (Lecture Q&A, Policy Lookup, From scratch) | Agent creation |
| 2 | Auth routes + session middleware (`app/auth/`) | Everything — it all sits behind login |
| 3 | Agent CRUD + admin listing | The marketplace flow |
| 4 | ~~Ingest pipeline~~ **done** — but `ingest_file()` takes a filesystem `Path`. An upload arrives as bytes; it needs a `(filename, bytes)` entry point before the API can call it | The upload route |
| 5 | ~~Stage 1 chain~~ **done** (`rag/pipeline.py`) — not yet writing `queries` / `query_chunks` rows | The Trace view, Ragas contexts |
| 6 | Stage 2 loop + `trace_events` writing (`rag/trace.py`) | Stage 2 |
| 7 | Golden set + Ragas | Stage 3 |
| 8 | React views: dashboard, create-agent, agent tabs, admin | Needs the API first |

Non-blocking: object storage for slide-image citations (PRD open item 10), and the
workshop-PDF licensing decision (item 12 — currently gitignored, which is the safe default).

---

## Rules that are easy to break silently

The full list is PRD §7. These are the ones that fail *without an error*:

- **Namespace is per AGENT (`agent_{id}`), never per user.** A user owns several agents
  and each must retrieve only its own corpus. Derive it server-side from the
  session-authorised agent; never accept it from the request body.
- **Never mix embedding models against one index.** Same dimensions do not mean the same
  vector space — querying with a different model returns confident nonsense, not an error.
  `agents.embedding_model` and `ingestion_runs.embedding_model` exist to make a mismatch
  detectable.
- **`gemini-embedding-2` has no `task_type`.** Tutorials for `embedding-001` show it. It
  does not apply, and that model's manual L2-normalization step does not either.
- **The OAuth scope must contain `openid`.** Without it Authlib never stores a nonce and
  `token['userinfo']` is silently absent — surfacing as a bare `KeyError`.
- **No secrets in `VITE_*`.** They are compiled into the bundle. The repo is public.
- **Do not "clean up" `config.py`'s URL rewriting or `db_connect_args`.** Three separate
  Render/asyncpg traps are handled there; each produces a different misleading error.
- **Build retrieval only in `rag/retriever.py`.** Scored search lives there too, precisely
  so that Stage 2's threshold check has no reason to reach past the seam. The moment
  `similarity_search()` appears elsewhere, the Stage 1 → Stage 2 change stops being a
  one-liner and the workshop loses its point.
- **Do not read Gemma's structured output through `response.parsed`.** On a fenced reply it
  returns `None` rather than raising — a decision the Stage 2 loop branches on, arriving as
  a silent null. Go through LangChain's `with_structured_output(method="function_calling")`.
- **`score_threshold` is not a safety control.** It decides whether to *rewrite*. Refusal
  comes from the system prompt, and an off-corpus question can score well above 0.5.
- **Check `pywin32` after every `pip freeze`.** `mcp` requires it only on Windows, but
  `pip freeze` drops environment markers, leaving a bare `pywin32==312` that Render's Linux
  build cannot install. It must read `pywin32==312; sys_platform == "win32"`. Freezing on
  Windows flattens it again every time, and the failure lands in CI, not locally.

## Local setup

**On the current dev machine this is already done** — `.env` is filled in,
`backend/.venv` exists with every dependency installed, and `frontend/node_modules` is
populated. Skip to "Useful commands". What follows is for a fresh machine.

```bash
cp .env.example .env    # then fill in - all five credential groups are documented there
```

```bash
cd backend && python -m venv .venv && .venv/Scripts/activate && pip install -r requirements.txt
```

`uv` (0.7.19) is installed and `uv pip install -r requirements.in` works and is much
faster. Do **not** run `uv add`: there is no `pyproject.toml`, so it would create one and
fork the dependency source of truth away from `requirements.txt` — which is what Render's
build command actually installs. `requirements.in` -> `pip freeze` -> `requirements.txt`
stays the pipeline regardless of which tool does the installing.

```bash
cd backend && alembic upgrade head
```

```bash
cd backend && uvicorn app.main:app --reload --port 8000
```

```bash
cd frontend && npm install && npm run dev
```

Local DB access requires your public IP on the Postgres allow-list. Currently
`155.69.165.66/32` (a campus address — if your IP changes, local connections will hang and
then fail; deployed traffic is unaffected because it uses the private network).

## Useful commands

```bash
python scripts/create_index.py            # verify Pinecone config against the PRD
```

```bash
python scripts/create_render_services.py --wire    # re-read Render URLs into env vars
```

```bash
cd backend && python -m alembic revision --autogenerate -m "..."
```

```bash
backend/.venv/Scripts/python.exe scripts/slice_check.py --cleanup   # drop the slice data
```

## Notes for whoever picks this up

- **All four `scripts/` are idempotent.** They detect existing resources, verify config
  against the PRD, and report drift rather than recreating. Re-running one is always safe.
- **`create_index.py --recreate` refuses to delete a populated index.** That is not an
  obstacle — the destructive path was never correct. Use `migrate_index.py`, which builds
  the replacement alongside the original. See CLAUDE.md, "Changing something immutable".
- **Provisioning failures are documented, not forgotten.** PRD §6.2 and §8 record every
  platform rejection hit during setup and why the architecture looks the way it does.
- This repo is public. Infrastructure IDs and hostnames are in these docs deliberately;
  credentials live only in `.env`, which is gitignored and has never been committed.
