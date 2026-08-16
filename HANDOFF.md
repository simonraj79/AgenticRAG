# HANDOFF — start here

Written 2026-08-15, at the point where **all infrastructure is provisioned and verified**
and **the RAG vertical slice runs end to end**. Auth, the API layer and every real React
view are still unwritten. This document is the orientation for picking the work back up in
a fresh session.

## The documents

| File | What it is | When to read it |
|---|---|---|
| **HANDOFF.md** | This file — current state and what to do next | First |
| **[PRD.md](PRD.md)** | The specification: stack, architecture, schema, deployment | Before designing anything |
| **[CLAUDE.md](CLAUDE.md)** | Conventions and platform gotchas that cost debugging time | Before touching infra or the DB driver |
| **[EVAL.md](EVAL.md)** | The operator's guide to Stage 3 | Before running or reading an evaluation |
| **[new features/loop.md](new%20features/loop.md)** | The design pattern for anything the **model** decides — tools, retries, detectors over model output | **Before** writing such a feature, not after it fails to fire |

**`CLAUDE.md` is gitignored** (deliberately, since commit `aaa381f`), so a fresh clone does
not have it. That is why `loop.md` is listed here as well: it is tracked, and it is the one
document in `new features/` that is a living reference rather than the record of a change
that has shipped.

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

## Current state — verified live 2026-08-16

### Infrastructure: complete

| Resource | Identifier | Notes |
|---|---|---|
| Pinecone index | `agentic-rag-ntu` | 768d · cosine · aws `ap-southeast-1` · **Builder plan** |
| Pinecone host | `agentic-rag-ntu-o3j2ojr.svc.aps-d9bb-582b.pinecone.io` | **5 vectors in 1 namespace** — the slice-check agent, see below |
| Render Postgres | `dpg-d9vt7v1t0dsc738c8kpg-a` | `agentic-rag-db` · Singapore · PG 18 · 16 tables migrated |
| Render backend | `srv-d9vtuhpt0dsc738dmgsg` | https://agentic-rag-api-6x6b.onrender.com |
| Render static site | `srv-d9vtuj61egvs73fdfang` | https://agentic-rag-web-e9e9.onrender.com |
| Google OAuth client | `Agentic RAG Web` | project `dsai-mod-2-group-project` · **in production** |
| GitHub | https://github.com/simonraj79/AgenticRAG | public · `main` synced at `1874950` |

Health check returns `{"status":"ok","version":"0.1.0","database":"ok"}`. Cost is roughly
**$33–34/mo** (Render backend ~$7, Postgres ~$6–7, Pinecone Builder ~$20).

### 2026-08-16 frontend audit and deployment

The production frontend is live on commit `1874950` (`Improve empty-agent and creation UX`).
Render built and published the static site from that commit; the backend correctly skipped a
rebuild because the change was frontend-only and its health/config endpoints remained 200.

Two changes shipped from the Playwright audit:

1. An agent with no documents shows a source-first workspace with **Add your first source**;
   the chat composer is absent until the corpus exists.
2. New-agent creation opens in an accessible Drawer with an inert background, Name autofocus,
   sticky actions and **Next** disabled until the name is valid and unique.

Verification is green: `npm test` **2/2**, `npm run build`, and `scripts/ui_check.py`
**15/15**. The 390x844 pass specifically verified that the creation panel fits the viewport,
its action bar remains visible, and the page has zero horizontal overflow.

### 2026-08-16 orchestrator, @mentions and self-check

Live on commit `86a0f15`. Both Render services report `live`; the backend answers
`{"status":"ok","database":"ok"}` and the static site serves a fresh hashed bundle. The
migration `bc307f5fc31f -> d4e91c2a7b58` was applied before the merge, so the start command's
`alembic upgrade head` was a no-op rather than a first run against production traffic.

What shipped, and the reasoning is in
[new features/11-orchestrator-and-self-check.md](new%20features/11-orchestrator-and-self-check.md):

1. **`adaptive-tutor`**, a ninth template that routes each question to one of the five
   teaching personas. Routing moves retrieval breadth as well as voice — it cannot move
   `chunk_size`, which is fixed at ingest.
2. **`@feynman`** overrides the router; two mentions produce two sections over one citation
   ledger, so `[2]` means the same passage in both.
3. **Self-check**, off by default, per agent. A free set operation against the citation ledger
   decides whether a critic runs at all.

Three of the five catalogue patterns already shipped here (ReAct, rewriting, multi-hop).
**Routing between SOURCES was deliberately not built**: one namespace per agent, and
`SearchCorpusArgs` has a single field precisely so a prompt-injected model cannot name another
corpus. Building it means adding back the parameter that omission exists to prevent.

Measured before the wiring existed: the router chose correctly **18/18** across six probes and
reached all five specialists; rewrite alone 1,211 ms, router alone 1,368 ms, both under
`asyncio.gather` **1,433 ms** — so routing costs ~222 ms on a 6.3 s persona turn.

Verification: `agentic_check.py` S20–S27 **9/9** live, `route_specialist_check.py` **40/40**,
`mention_popup_check.py` **17/17**, `ui_check.py` **15/15**, `npm test` **35/35** (was 2/2).

**Off is off.** `agents.specialists IS NULL` and `self_check_enabled = false` *are* the classic
path, so the migration needed no backfill — unlike `bc307f5fc31f`, which had to `UPDATE` every
existing row. All six pre-existing agents are unaffected.

### Code: full RAG product, evaluation and handouts

**Exists:**

```
backend/app/
├── api/           agents, conversations, documents, evaluation, handouts
├── auth/          Google OAuth plus the gated local dev-login shim
├── db/            models, async sessions, seeds, and personas.py /
│                  specialists.py (the roster)
├── eval/          golden-set generation, Ragas scoring, background jobs
├── rag/           ingest, retrieval seam, pipeline, bounded agent loop, traces,
│                  specialist routing (route.py) and answer self-check (selfcheck.py)
└── tools/         corpus search and the sandboxed Python tool
frontend/src/
├── views/         Login, Dashboard, Chat, Sources and Evaluate
├── components/    workspace shell, drawers, messages, citations, traces, handouts,
│                  the @mention popup
└── lib/           API/types/hooks; the frontend's only door to the backend
scripts/           provisioning plus offline, agentic and browser harnesses
```

The original RAG slice remains a useful low-cost smoke test, but it is no longer the product
boundary. The live system includes multi-turn conversations, SSE streaming, model-driven
corpus search, sandboxed Python handouts, stored traces, golden-set authoring, Ragas
scorecards, and — since 2026-08-16 — an orchestrator persona that routes each question to one
of five teaching specialists, `@mentions` that override the routing, and an optional
self-check that can discard an ungrounded draft and try again.

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

Items 1–8 of the original list are **all done**, including Stage 2 (as the agent loop, not
as the specified score-triggered rewrite — PRD open item 7 explains why the threshold could
not have worked) and Stage 3. The list below is what is actually left; PRD §10 is the
authoritative tracker.

| # | Item | Why it matters |
|---|---|---|
| 1 | **The routing fallback path has never run** (PRD 29) | The router measured 18/18 with zero failures, so `trigger="fallback"` is reasoned-about rather than observed. The first time it fires will be in front of a user. A probe that *makes* routing fail is the missing measurement |
| 2 | **`eval_runs` records neither `tools_enabled` nor the specialist roster** (PRD 23, 29) | Two scorecards for one agent are incomparable if either was toggled between them, and nothing on the card says so. An orchestrator run averages across teaching methods — EVAL.md §5 |
| 3 | **Blocking SDK calls inside `async def`** (PRD 19) | Pinecone, Cohere and embeddings are sync at the in-request call sites in `ingest.py`. One uvicorn worker on Render. Fix them together |
| 4 | **Deleting a document destroys past queries' contexts** (PRD 18) | FK cascade. Costs Stage 3 its evidence while the scores still render |
| 5 | **Faithfulness penalises a teaching persona** (PRD 20) | The weakest-metric pointer currently advises deleting the pedagogy |
| 6 | Object storage for handouts and slide images (PRD 10, 25) | Handout bytes are in Postgres, capped at 5 MB and 200 per agent |
| 7 | **Self-check is unmeasured, and faithfulness is the wrong instrument** (PRD 30) | The critic exempts exactly the sentences faithfulness penalises, so the two disagree by design. Needs a trajectory measure, not a faithfulness-shaped one |

**SSE streaming is done** and has been since `new features/08-streaming-and-followups.md` —
`app/api/stream.py`, `apiStream` in `lib/api.ts`. It was item 1 on this list and PRD item 13
still reads "Open"; both are stale, and the code is the authority.

Verify anything you change with the four layers, lowest layer first — a fix in
`app/tools/` or `app/rag/` invalidates the layers above it:

```bash
backend/.venv/Scripts/python.exe scripts/sandbox_check.py             # 16 cases, no DB, seconds
backend/.venv/Scripts/python.exe scripts/ledger_check.py              # citation markers, no DB
backend/.venv/Scripts/python.exe scripts/refusal_check.py             # refusal + gap detectors
backend/.venv/Scripts/python.exe scripts/route_specialist_check.py    # mentions, routing, self-check
backend/.venv/Scripts/python.exe scripts/llm_check.py                 # request body, no network
cd frontend && npm test                                              # component behavior
backend/.venv/Scripts/python.exe scripts/agentic_check.py --setup    # then --run, then --cleanup
cd frontend && npm run build
python scripts/ui_check.py                                           # both servers running
python scripts/mention_popup_check.py                                # both servers, popup OPEN
```

**There is no CI — every one of these is run by hand**, so the ordering above is the whole
protocol rather than a suggestion. The first five need no database, no provider and no
browser and take seconds; the rest need one of the three. **`ui_check.py` and
`mention_popup_check.py` are two files for a reason**: `ui_check` passes with the mention
popup never rendering, because its fixture agent has no roster and nothing types `@`. A check
that cannot fail reports success.

`--cleanup` matters more than usual: a leaked Pinecone namespace is a real cost, and the
Builder plan's 1,000-namespace cap *is* the maximum number of agents this deployment can
hold.

Non-blocking: the workshop-PDF licensing decision (item 12 — currently gitignored, which is
the safe default).

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
  Windows flattens it again every time, and with no CI the failure lands in the Render build
  minutes after the push, where it reads as an outage rather than as a mistake.

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

```bash
cd frontend && npm test && npm run build          # fast frontend gate
```

```bash
python scripts/ui_check.py                        # browser UI/accessibility gate
python scripts/mention_popup_check.py             # the @mention popup, measured OPEN
```

```bash
# Why is production behaving unlike local? Compare env vars BY VALUE, not by key.
curl -s -H "Authorization: Bearer $RENDER_API_KEY" \
  "https://api.render.com/v1/services/srv-d9vtuhpt0dsc738dmgsg/env-vars?limit=100"
```

That last one found a stale Cohere **trial** key on production in August, present and
correct-looking, differing only under load. Compare by hash to find candidates, then *test* the
key — on 2026-08-16 a hash comparison flagged `PINECONE_API_KEY` as drift and a functional test
showed both keys reach the identical index. The hash finds candidates; only the test decides.
Note that `DATABASE_URL`, `ENVIRONMENT`, `FRONTEND_URL` and `OAUTH_REDIRECT_URI` differ
legitimately, and `RENDER_API_KEY` must never be on the service at all.

## Notes for whoever picks this up

- **The provisioning scripts are idempotent.** They detect existing resources, verify config
  against the PRD, and report drift rather than recreating. Test harnesses document their own
  setup/cleanup contract; `agentic_check.py --cleanup` is not optional after a throwaway run.
- **`create_index.py --recreate` refuses to delete a populated index.** That is not an
  obstacle — the destructive path was never correct. Use `migrate_index.py`, which builds
  the replacement alongside the original. See CLAUDE.md, "Changing something immutable".
- **Provisioning failures are documented, not forgotten.** PRD §6.2 and §8 record every
  platform rejection hit during setup and why the architecture looks the way it does.
- This repo is public. Infrastructure IDs and hostnames are in these docs deliberately;
  credentials live only in `.env`, which is gitignored and has never been committed.
