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
| Agent runtime | LangChain loop (`AGENT_RUNTIME=langchain`, the default) · Google ADK 2.8.0 available behind the same flag |
| Agent tools | `search_corpus` · `run_python` (sandboxed) |
| Reranker | Cohere `rerank-v3.5` |
| Evaluation | Ragas 0.4.3 — four RAG metrics + an agent rubric, judged by `google/gemini-3.7-flash` |
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
./.venv/Scripts/python.exe ../scripts/deck_check.py             # handout pure functions, and what a .pptx IS
```

The agent loop, tools, routing and handouts end to end — needs a database and burns provider
quota, so it is deliberately manual:

```bash
python scripts/agentic_check.py --setup   # then --run, then --cleanup
```

## Working on this codebase

**Query the LangChain MCP servers before writing or changing LangChain code — first, every
time, not as a fallback once an import fails.** LangChain 1.x relocated symbols with no
deprecation shims, so tutorials, blog posts and model training data all confidently describe
imports that no longer resolve. The failure arrives as `ModuleNotFoundError`, which reads as a
missing dependency rather than a moved class and sends you to check your install instead of
the docs. This repo hit it twice in one afternoon — `langchain.text_splitter` and
`ContextualCompressionRetriever` — and each was one query. Treat these as outranking both
memory and any example found elsewhere.

| Server | Use it for |
|---|---|
| `docs-langchain` | Concepts, guides, how-tos — the *why* and the recommended pattern |
| `reference-langchain` | Exact signatures, parameters and module paths — the *where* |

```bash
claude mcp add --transport http docs-langchain --scope user https://docs.langchain.com/mcp
claude mcp add --transport http reference-langchain --scope user https://reference.langchain.com/mcp
```

Adding a feature rather than fixing one? [new features/build.md](new%20features/build.md) is
the procedure — audit first, shared contracts in one plan file, acceptance criteria that name
a harness case. [new features/loop.md](new%20features/loop.md) is the pattern for anything the
**model** decides, which is a harder problem than it looks: binding a tool is twenty lines
that work first time, and then the model declines to call it.

Both assume [insights.md](insights.md), which is the shortest useful thing to read here.
Its first rule is the one that generalises furthest — **trigger on the absence of the
outcome you wanted, never on the presence of an error** — and its second is the reason this
repo has the harnesses it does: a green suite here has been wrong eleven times, in eleven
different modules, and every one was found by reading an answer, opening a page or asking how
many real rows an instrument had ever produced — never by a passing assertion.

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
| [insights.md](insights.md) | **What this project learned that outlives it** — ~35 rules with the incident behind each. Trigger on the absence of the outcome, never the presence of an error; a green suite here has been wrong eleven times; mutate, because a passing suite says nothing about what it would let through; an instrument that has never produced a row has never been tested. Read before starting work, not after something breaks |
| [new features/](new%20features/) | Design notes for each major change. **Un-numbered files are living references; numbered ones record a change that shipped** |
| [new features/build.md](new%20features/build.md) | **Start here for a new feature bigger than one prompt** — audit, plan, one file per feature, harness-first, verify, ship |
| [new features/loop.md](new%20features/loop.md) | The design pattern for anything the **model** decides — read before adding a tool, a retry or a detector |
| [new features/18-adk-runtime/PLAN.md](new%20features/18-adk-runtime/PLAN.md) | The Google ADK runtime, why it is a `BaseLlm` over `build_chat_model` rather than `LiteLlm`, and why it ships switched off |
| [new features/19-agent-evaluation/PLAN.md](new%20features/19-agent-evaluation/PLAN.md) | Evaluating the agent architecture — and the four numbers the rubric would have rendered wrong before anyone read one |

Each of those two has a `-prompt.md` companion holding the session structure.
[PRD.md §10](PRD.md) is the authoritative tracker for what is still open.

---

*Built against the "Agentic RAG Harness Engineering" workshop (Topics 10–11).*
