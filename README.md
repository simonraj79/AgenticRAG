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

**Picks how to teach you.** An Adaptive Tutor reads what you are actually asking for — an
explanation, a worked problem, a set of practice questions — and answers in the matching
teaching voice. Type `@feynman` to choose one yourself, or `@feynman @polya` to get both, in
two sections over one shared set of citations.

**Checks its own answer.** Before an answer settles, the agent can test whether every claim is
carried by a passage it actually retrieved. When it is not, the draft is discarded and tried
again rather than shipped.

**Makes handouts.** Charts, slide decks, spreadsheets and study sheets, generated from your
material — either on request, or as a by-product of a conversation. Each one keeps the code
that produced it.

**Scores itself.** Build a set of questions with known answers and Groundwork grades its own
performance on four measures, question by question, and names the weakest one.

**Shows its working.** Retrieval, reranking, generation, tool calls and refusals are all
recorded per answer and readable in one place. Nothing about how an answer was produced is
hidden.

**Starts with evidence.** A new agent cannot present a question box before it has a source;
the empty workspace explains the dependency and takes the user straight to upload.

## Personas

An agent is a corpus plus a teaching style. The Feynman Explainer answers with an analogy, a
worked example, and an explicit note about anything the material does not cover. Others
answer plainly. The persona changes how you are taught, never what the agent is allowed to
claim.

Nine templates ship. Five are teaching personas grounded in learning science — Explainer,
Socratic tutor, Problem coach (Pólya), Quiz writer, Reflection guide (Gibbs) — three are
parameter presets, and one, the **Adaptive Tutor**, chooses among the five per question.

Routing changes retrieval as well as voice: a quiz draws on more of the corpus than an
explanation does. It cannot change how the corpus was *split*, because that happens at upload.

A specialist is a prompt, never a second agent — it answers the same corpus, over the same
citation ledger, so `@feynman @polya` produces two sections in which `[2]` means the same
passage. There is no way to address another agent's documents, by design.

## Stack

| Layer | Choice |
|---|---|
| Backend | FastAPI · SQLAlchemy 2.0 (async) · Alembic |
| Frontend | React 19 · Vite · Tailwind CSS 4 |
| Frontend tests | Vitest · Testing Library · Playwright |
| Database | Render Postgres 18 |
| Vector DB | Pinecone serverless, 768d cosine |
| Embeddings | `gemini-embedding-2` @ 768 dims |
| Generation | `deepseek/deepseek-v4-flash-0731` via OpenRouter |
| Question rewrite · routing · self-check | `google/gemma-4-31b-it` via OpenRouter |
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

Frontend verification:

```bash
cd frontend && npm test && npm run build
```

With both local servers running, the browser harnesses are:

```bash
python scripts/ui_check.py             # layout and accessibility
python scripts/mention_popup_check.py  # the @mention popup, measured with it OPEN
```

Both use the **global** interpreter, not the backend venv — Playwright lives there. They are
two files because `ui_check.py` passes with the popup never rendering: its fixture agent has
no roster and nothing types `@`, so a check that cannot fail would report success.

Backend checks that need no database, no provider and no browser — seconds each, so they are
the ones to run first and most often:

```bash
cd backend
./.venv/Scripts/python.exe ../scripts/refusal_check.py          # refusal and gap detectors
./.venv/Scripts/python.exe ../scripts/ledger_check.py           # citation-marker contract
./.venv/Scripts/python.exe ../scripts/route_specialist_check.py # mentions, routing, self-check
./.venv/Scripts/python.exe ../scripts/llm_check.py              # OpenRouter request body
./.venv/Scripts/python.exe ../scripts/sandbox_check.py          # code-interpreter sandbox
```

The agent loop, tools, routing and handouts end to end — needs a database and burns provider
quota, so it is deliberately manual:

```bash
python scripts/agentic_check.py --setup   # then --run, then --cleanup
```

## Deploying

**Pushing to `main` is the deploy.** `autoDeploy` is on for both Render services, so a merge
or a direct push rebuilds and releases them. There is no deploy step in this repository and no
CI: the checks above are run by hand, before the push.

Two more are worth running every time, because both failures land *after* the push, in
Render's build, where they read as an outage rather than as a mistake:

```bash
grep -n 'pywin32' backend/requirements.txt   # must keep '; sys_platform == "win32"'
cd frontend && npm run build                 # tsc -b under strict, same as Render's build
```

The first is not a formality. `pip freeze` **strips environment markers**, and
`langchain-mcp-adapters` pulls in `mcp`, which needs `pywin32` only on Windows. Freezing on a
Windows machine turns that into an unconditional requirement that Render, building on Linux,
cannot resolve. It has been flattened and restored three times, in three unrelated dependency
additions — so treat restoring the marker as the second half of the `pip freeze` command.

After the deploy, `GET /api/health` returns `{"status":"ok","version":…,"database":"ok"}`.
**Read the `database` field, not the status code.** `status` is hard-coded `"ok"` and the
route returns 200 either way, so a container that cannot reach Postgres reports
`"database":"unavailable"` inside a 200 — and the internal endpoint presents a self-signed
certificate, which means a TLS misconfiguration passes every local test and fails only once
deployed.

## Repository layout

```
backend/            FastAPI app, SQLAlchemy models, Alembic migrations
frontend/           React + Vite + Tailwind SPA
scripts/            Provisioning and end-to-end checks
```

## Documentation

| File | What it is |
|---|---|
| [PRD.md](PRD.md) | The specification: architecture, schema, deployment |
| [EVAL.md](EVAL.md) | How to run an evaluation and read a scorecard |
| [HANDOFF.md](HANDOFF.md) | Current state and what to do next |
| [new features/](new%20features/) | Design notes for each major change |
| [new features/loop.md](new%20features/loop.md) | The design pattern for anything the **model** decides — read before adding a tool, a retry or a detector |

---

*Built against the "Agentic RAG Harness Engineering" workshop (Topics 10–11).*
