# Agentic RAG

Multi-user agentic RAG over private documents, built for the NTU "Agentic RAG Harness
Engineering" workshop and deployed on Render.

**New here, or resuming work? Start with [HANDOFF.md](HANDOFF.md)** — current state, what
exists, and what to do next.

**[PRD.md](PRD.md) is the specification** — tech stack, architecture, database schema,
deployment configuration, and the constraints that cannot be reversed. Read §7 before
changing anything infrastructural.

## Stack

| Layer | Choice |
|---|---|
| Backend | FastAPI · SQLAlchemy 2.0 (async) · Alembic |
| Frontend | React 19 · Vite · Tailwind CSS 4 |
| Database | Render Postgres 18 (Singapore) |
| Vector DB | Pinecone serverless, 768d cosine (`ap-southeast-1`, Singapore) |
| Embeddings | `gemini-embedding-2` @ 768 dims |
| Generation | `gemma-4-31b-it` via the Gemini API |
| Reranker | Cohere `rerank-v3` |
| Evaluation | Ragas, judged by Gemini Flash Lite |
| Auth | Google OAuth 2.0 via Authlib |

## Getting started

Copy the environment template and fill it in — every value is documented there:

```bash
cp .env.example .env
```

Backend:

```bash
cd backend && python -m venv .venv && .venv/Scripts/activate && pip install -r requirements.txt
```

```bash
cd backend && alembic upgrade head
```

```bash
cd backend && uvicorn app.main:app --reload --port 8000
```

Frontend:

```bash
cd frontend && npm install && npm run dev
```

Then open http://localhost:5173. The landing page reports whether it can reach the
backend and the database.

## Infrastructure

Cloud resources are provisioned by idempotent scripts — safe to re-run, and they verify
existing resources against the PRD rather than recreating them:

```bash
python scripts/create_index.py --dry-run
```

```bash
python scripts/create_render_db.py --dry-run
```

Drop `--dry-run` to actually create. Current provisioning status is tracked in PRD §8.

## Repository layout

```
backend/    FastAPI app, SQLAlchemy models, Alembic migrations
frontend/   React + Vite + Tailwind SPA
scripts/    Idempotent cloud provisioning
PRD.md      The specification
CLAUDE.md   Working notes: conventions, insights, and hard-won gotchas
```

## Notes

The workshop PDFs are gitignored pending a decision on whether redistributing them in a
public repository is permitted (PRD open item 6).
