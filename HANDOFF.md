# HANDOFF — start here

Written 2026-08-15, at the point where **all infrastructure is provisioned and verified**
and **application code has not been written**. This document is the orientation for
picking the work back up in a fresh session.

## The three documents

| File | What it is | When to read it |
|---|---|---|
| **HANDOFF.md** | This file — current state and what to do next | First |
| **[PRD.md](PRD.md)** | The specification: stack, architecture, schema, deployment | Before designing anything |
| **[CLAUDE.md](CLAUDE.md)** | Conventions and platform gotchas that cost debugging time | Before touching infra or the DB driver |

Do not re-derive decisions already recorded in PRD §7 (hard constraints) or the decisions
table in §10. They were made deliberately, and several are expensive to reverse.

---

## Current state — verified live 2026-08-15

### Infrastructure: complete

| Resource | Identifier | Notes |
|---|---|---|
| Pinecone index | `agentic-rag-ntu` | 768d · cosine · aws `ap-southeast-1` · **Builder plan** |
| Pinecone host | `agentic-rag-ntu-o3j2ojr.svc.aps-d9bb-582b.pinecone.io` | **0 vectors, 0 namespaces** |
| Render Postgres | `dpg-d9vt7v1t0dsc738c8kpg-a` | `agentic-rag-db` · Singapore · PG 18 · 16 tables migrated |
| Render backend | `srv-d9vtuhpt0dsc738dmgsg` | https://agentic-rag-api-6x6b.onrender.com |
| Render static site | `srv-d9vtuj61egvs73fdfang` | https://agentic-rag-web-e9e9.onrender.com |
| Google OAuth client | `Agentic RAG Web` | project `dsai-mod-2-group-project` · **in production** |
| GitHub | https://github.com/simonraj79/AgenticRAG | public · working tree clean |

Health check returns `{"status":"ok","version":"0.1.0","database":"ok"}`. Cost is roughly
**$33–34/mo** (Render backend ~$7, Postgres ~$6–7, Pinecone Builder ~$20).

### Code: scaffold only

**Exists:**

```
backend/app/
├── config.py      settings; the URL rewriting + TLS fixes (do not "simplify" these)
├── main.py        /api/health, /api/config, CORS, SessionMiddleware
└── db/
    ├── models.py  all 16 tables from PRD §4
    └── session.py async engine
frontend/src/      3 files: status card + four placeholder tiles
scripts/           create_index · create_render_db · create_render_services · migrate_index
```

**Does not exist:** `app/auth/`, `app/rag/`, `app/api/`, `app/eval/`, and every real React
view. Nothing has ever been ingested or retrieved.

---

## Do this next: a thin vertical slice

**Before building any UI, prove the model choices work.** Ingest one file into one agent
and ask one question. This exercises embedding → upsert → namespace scoping → retrieval →
generation in a single pass, and surfaces the riskiest unknowns in minutes rather than
after four views are built on top of them.

The three untested assumptions:

1. **Does `gemma-4-31b-it` support structured output?** Google does not document it, and
   Stage 2's rewrite decision needs a typed Pydantic object back. `DECISION_MODEL` is
   currently pointed at Gemini Flash as a hedge. Test Gemma directly; if it works, collapse
   to one model.
2. **Is `gemini-embedding-2` at 768 dims good enough on this corpus?** Reasonable, unverified.
3. **Are 800-token chunks with 120 overlap right?** A defensible starting point, not a
   measured one. Stage 3 exists to turn that into a number.

Concrete steps:

```
1. Add to backend/requirements.in:
     langchain langchain-google-genai langchain-pinecone langchain-cohere pypdf
   then: pip install -r requirements.in && pip freeze > requirements.txt
   (never hand-write a version - see CLAUDE.md process notes)

2. backend/app/rag/retriever.py   <- THE SEAM. Build the retriever in exactly one
                                     place. Stage 2 wraps this object; if retrieval
                                     is constructed anywhere else, Stage 2 becomes a
                                     refactor instead of a one-line change.

3. backend/app/rag/ingest.py      load -> split -> embed -> upsert to namespace
                                     + write documents / chunks / ingestion_runs rows

4. A throwaway script that: creates one Agent row, ingests ONE markdown file, asks
   ONE question, prints the answer and the retrieved chunks.

Test input: any single `*-lesson-gist.md` under
    D:\03 Module Machine Learning\Corpus\{n}-corpus\
(~12-17 KB, small enough to iterate quickly)
```

## Then, in order

| # | Item | Blocks |
|---|---|---|
| 1 | Seed the 3 `agent_templates` (Lecture Q&A, Policy Lookup, From scratch) | Agent creation |
| 2 | Auth routes + session middleware (`app/auth/`) | Everything — it all sits behind login |
| 3 | Agent CRUD + admin listing | The marketplace flow |
| 4 | Full ingest pipeline, namespace-scoped | Stage 1 |
| 5 | Stage 1 chain wired to the retriever seam | Stage 1 |
| 6 | Stage 2 loop + `trace_events` writing | Stage 2 |
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

## Local setup

```bash
cp .env.example .env    # then fill in - all five credential groups are documented there
```

```bash
cd backend && python -m venv .venv && .venv/Scripts/activate && pip install -r requirements.txt
```

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
