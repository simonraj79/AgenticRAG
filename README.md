# Groundwork

**An agentic RAG harness.** Retrieval over your own documents that tells you when it is
wrong — multi-user, deployed on Render, and scored on a golden set rather than on vibes.

Ingest → embed → retrieve → rerank → generate → **measure**. The last stage is the point:
four Ragas metrics, a per-question breakdown, and a weakest-metric pointer that names the
next thing to fix.

**The agent has tools.** Generation is a bounded loop rather than a single call: the model
can search its own corpus again mid-answer when one retrieval did not cover the question,
and it can write and run Python in a sandbox to produce a chart, a slide deck or a table.
Every tool call is a row in the same trace as every other decision, including the program it
wrote — a pipeline that claims to be inspectable should not hide the step that generates
your files.

**Handouts** is where those files land: charts, `.pptx` decks, `.csv` tables and markdown
study sheets, produced either as a side effect of a conversation or straight from a recipe
button. Each one keeps the code that made it.

*Source material for the corpus and the design brief: the "Agentic RAG Harness Engineering"
workshop (Topics 10–11). See [PRD.md](PRD.md) §8 open item 6 for the licensing question on
the workshop PDFs.*

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
| Embeddings | `gemini-embedding-2` @ 768 dims, via Google directly |
| Generation | `google/gemma-4-31b-it` **via OpenRouter** — every chat model goes through one gateway; embeddings deliberately do not |
| Agent tools | `search_corpus` (the retriever, model-driven) · `run_python` (sandboxed subprocess: matplotlib, python-pptx, pandas) |
| Reranker | Cohere `rerank-v3.5` |
| Evaluation | Ragas, judged by `google/gemini-3.7-flash` — **not** the generation model, so a run is not self-assessment |
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
EVAL.md     Stage 3 operator's guide: settings, parameters, reading a scorecard
```

## Notes

The workshop PDFs are gitignored pending a decision on whether redistributing them in a
public repository is permitted (PRD open item 6).
