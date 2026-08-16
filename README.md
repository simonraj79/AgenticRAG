# Groundwork

**Ask questions about your own documents, and see exactly where every answer came from.**

Groundwork is a teaching agent. You give it a set of documents, pick a persona, and talk to
it — and it answers only from what you gave it. When your documents do not cover the
question, it says so instead of inventing something.

## What it does

**Answers with receipts.** Every claim carries a numbered citation you can click to read the
exact passage it came from, with its retrieval and reranking scores.

**Searches again when it needs to.** If one search does not cover a two-part question, the
agent goes back to your documents mid-answer rather than guessing at the rest.

**Makes handouts.** Charts, slide decks, spreadsheets and study sheets, generated from your
material — either on request, or as a by-product of a conversation. Each one keeps the code
that produced it.

**Scores itself.** Build a set of questions with known answers and Groundwork grades its own
performance on four measures, question by question, and names the weakest one.

**Shows its working.** Retrieval, reranking, generation, tool calls and refusals are all
recorded per answer and readable in one place. Nothing about how an answer was produced is
hidden.

## Personas

An agent is a corpus plus a teaching style. The Feynman Explainer answers with an analogy, a
worked example, and an explicit note about anything the material does not cover. Others
answer plainly. The persona changes how you are taught, never what the agent is allowed to
claim.

## Stack

| Layer | Choice |
|---|---|
| Backend | FastAPI · SQLAlchemy 2.0 (async) · Alembic |
| Frontend | React 19 · Vite · Tailwind CSS 4 |
| Database | Render Postgres 18 |
| Vector DB | Pinecone serverless, 768d cosine |
| Embeddings | `gemini-embedding-2` @ 768 dims |
| Generation | `google/gemma-4-31b-it` via OpenRouter |
| Agent tools | `search_corpus` · `run_python` (sandboxed) |
| Reranker | Cohere `rerank-v3.5` |
| Evaluation | Ragas, judged by `google/gemini-3.7-flash` |
| Auth | Google OAuth 2.0 |

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

Then open http://localhost:5173. The landing page reports whether it can reach the backend
and the database.

## Repository layout

```
backend/    FastAPI app, SQLAlchemy models, Alembic migrations
frontend/   React + Vite + Tailwind SPA
scripts/    Provisioning and end-to-end checks
```

## Documentation

| File | What it is |
|---|---|
| [PRD.md](PRD.md) | The specification: architecture, schema, deployment |
| [EVAL.md](EVAL.md) | How to run an evaluation and read a scorecard |
| [HANDOFF.md](HANDOFF.md) | Current state and what to do next |
| [new features/](new%20features/) | Design notes for each major change |

---

*Built against the "Agentic RAG Harness Engineering" workshop (Topics 10–11).*
