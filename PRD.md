# Agentic RAG — Product Requirements & Tech Stack

**Project:** Multi-user agentic RAG over private documents
**Source material:** NTU "Agentic RAG Harness Engineering" (Topics 10–11)
**Repository:** https://github.com/simonraj79/AgenticRAG
**Deployment target:** Render.com
**GCP project:** `dsai-mod-2-group-project`
**Last updated:** 2026-08-15

> **This document is the specification; [CLAUDE.md](CLAUDE.md) is the operational companion,
> and [EVAL.md](EVAL.md) is the operator's guide to Stage 3 — every setting and per-agent
> parameter in tables, plus how to read a scorecard without being misled by it.**
> Where a section describes something that has since been built and measured, the measurement
> is recorded inline rather than left to fold back into prose — a spec that quietly disagrees
> with the code is worse than no spec. Sections carrying post-build measurements:
> §2.1 (eval judge), §2.3 (multimodal embeddings), §3.3 (ingest), §3.6 (Stage 3),
> §3.7 (conversations), §4.2 (personas), §10 (open items).

| § | Section | |
|---|---|---|
| 1 | [What we're building](#1-what-were-building) | Scope and the three stages |
| 2 | [Tech stack](#2-tech-stack) | Every layer, and what changed from the workshop |
| 3 | [Architecture](#3-architecture) | Auth, tenancy, the query pipelines, API surface |
| 4 | [Database schema](#4-database-schema) | 17 tables |
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

| Stage | Name | Behaviour | Status |
|---|---|---|---|
| 1 | Classic RAG | Fixed chain: ingest → chunk → embed → store → retrieve → answer | ✅ built |
| 2 | Agentic enhancements | Conditional query rewriting, reranking, decision tracing | ◐ rerank + trace built; the score-triggered rewrite loop is not |
| 3 | Measurement | Golden set + Ragas scorecard across four metrics | ✅ built |

The distinction that matters: **Stage 1 is a chain, Stage 2 is a loop.** "Agentic" here
means the system decides which behaviour fits the current turn — rewriting fires only
when retrieval looks weak, not on every query.

**Stage 2 is partly built by a side door.** Conversations (§3.7) needed a rewriter to resolve
follow-ups whose subject is a pronoun, so `pipeline.py` already contextualises a question
against history and records a `REWRITE` trace event. That is the same machinery Stage 2's
loop needs; only the *trigger* differs — coreference rather than a low top score. Stage 2
should extend the existing rewriter, not add a second one.

Three features were added after the original scope and are specified below rather than
retrofitted into the tables above: **conversation memory** (§3.7), **teaching personas**
(§4.2), and **golden-set authoring** (§3.6).

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
| **Vector DB** | **Pinecone serverless, 768d, cosine, `ap-southeast-1`** | Chroma embedded (SQLite) | 🔄 |
| Retriever | `PineconeVectorStore.as_retriever()` | `Chroma.as_retriever()` | — |
| **Model gateway** | **OpenRouter** (all chat models) | Ollama, local | 🔄 |
| **Generation LLM** | **`google/gemma-4-31b-it`** via OpenRouter | Ollama `llama3.2` | 🔄 |
| Decision LLM | Gemini Flash | `google/gemma-4-31b-it` — collapsed, §2 open item 11 | ➕ |
| Reranker | Cohere `rerank-v3` | same | — |
| Output parsing | `StrOutputParser`, Pydantic parser | same | — |
| Evaluation | Ragas **0.4.3** | same | — |
| **Eval judge** | **`google/gemini-3.7-flash`** (`RAGAS_JUDGE_MODEL`) | Gemini Flash Lite | ✅ |

**The eval judge is no longer the generation model. That split was earned by measurement,
and it is the single most consequential change Stage 3 produced.** Consolidating onto one
model was requested deliberately and `eval_runs` records both `judge_model` and
`generation_model` so a self-judged scorecard is visible as such — but two full runs showed
self-assessment failing concretely. On a turn whose answer was copied **verbatim** out of
the retrieved text, `gemma-4-31b-it` scored faithfulness **0.000**; `google/gemini-3.7-flash`
scores the same shape **1.000**, and **0.250** for an answer padded with invented facts.
Answer relevance was stable across judges (0.813 vs 0.811), so the defect was specific to
faithfulness rather than a general judge-quality gap.

Latency drove it too: Gemma took 165–196 s per faithfulness call against a 180 s ceiling,
so *which* questions scored was luck. Three turns now score in 14.4 s in total.

**Chat moved to OpenRouter; embeddings deliberately did not.** Every vector in Pinecone was
written in `gemini-embedding-2`'s space, matching dimensions do not imply a shared space,
and OpenRouter serves no embedding model — so moving them would force a full re-ingest to
gain nothing. Both `OPENROUTER_API_KEY` and `GEMINI_API_KEY` are required, and Ragas now
draws its judge LLM and its embedding model from two different providers. Every chat model
is constructed in exactly one place (`app/rag/llm.py`), which is what made this a one-file
change rather than four.

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
| Frontend | React + Tailwind CSS (Vite) | **Login · Dashboard · Chat · Documents · Evaluate** |
| Frontend verification | Vitest + Testing Library + Playwright | Component contracts first; real-browser layout, focus and accessibility second |
| Answer transport | **JSON (SSE deferred)** | See below |
| **Auth** | **Google OAuth 2.0** via Authlib | Authorization Code flow, server-side |
| **Session** | **Server-side, DB-backed** | httpOnly cookie carrying an opaque token |
| **Database** | **Render Postgres 18** | Lowest paid tier |
| **ORM / migrations** | **SQLAlchemy 2.0 (async) + Alembic** | Driver: `asyncpg` |
| Markdown rendering | `react-markdown` + `remark-gfm` | Personas emit lists and numbered steps |

The workshop leaves the UI open ("Streamlit, Gradio, React, or plain HTML — we won't
grade the framework"), so the frontend choice is within the rules rather than a
deviation.

**The five views changed shape.** The original list — Login · Ingest · Ask · Trace ·
Evaluate — assumed one corpus and one-shot questions. Tenancy moved to agents (§3.2) and
questions became conversations (§3.7), so Ingest and Ask became *tabs on an agent*, and
Trace stopped being a view at all: it is now opened inline from the message it explains,
because provenance a click and a screen away is provenance nobody checks.

**The source-first boundary is part of the product contract.** As of 2026-08-16, an agent
with zero documents does not render a chat composer; it explains the dependency and offers
one direct action into Sources. Agent creation is a modal Drawer rather than an expanding
dashboard card: background content is inert, Name owns initial focus, actions remain visible,
and progress is gated on a valid, unique name. These are behavior and accessibility
requirements, not presentation details.

**SSE streaming is specified but not built.** Streaming and durable recording pull in
opposite directions — the `queries` row is only complete once the last token has landed —
so the shape that works is to stream and then write the rows in the same handler after the
stream closes. It was deferred to keep Stage 1 correct first, and the cost has since risen:
a coaching persona takes 30–60 s to answer (§2.4), so the blank wait is now the single worst
part of the product. This is the first UX item to build next.

### 2.3 Model specifications (verified 2026-08-15)

**`gemini-embedding-2`** — [docs](https://ai.google.dev/gemini-api/docs/embeddings)

| Property | Value |
|---|---|
| Output dimension | 768 (configurable 128–3072; recommended tiers 768/1536/3072) |
| Default if unset | 3072 |
| Max input | 8,192 tokens |
| Normalization | **Automatic** at non-default dimensions |
| `task_type` | **Not supported** — convey retrieval intent in the prompt text |
| Modalities | **Text, images, video, audio, PDFs** — one unified embedding space |

**It is the first multimodal embedding model in the Gemini API, and we use it for text
only.** Verified 2026-08-15. The native path was evaluated and deferred rather than
overlooked: per-request limits are 1 PDF of 6 pages, 6 images, and 8,192 tokens across all
modalities, so a slide deck needs windowing plus retry-on-overflow; and
`langchain-google-genai` has no non-text entry point, so it means calling `google-genai`
directly and leaving the retriever seam that keeps §3.5 a one-line change. CLAUDE.md records
the full reasoning and the design to use if it is ever built. The one thing not to do is
retreat to `gemini-embedding-001` — the two spaces are incompatible, so it forces a full
re-ingest, and -001 is text-only anyway.

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

### 3.2 Multi-tenancy — one namespace per **agent**

Each agent owns a **Pinecone namespace** (`agent_{id}`). All retrieval is
namespace-scoped, and the namespace is derived from the session-authorised agent on the
server — never accepted from the client.

**Keyed on the agent, not the user.** A single user owns several agents, and each must
retrieve only its own corpus. Namespacing per user would let someone's "ML Lecture" agent
return chunks from their "HR Policy" agent — isolation between users, none between a
user's own agents. `documents.agent_id` is therefore the scoping key;
`documents.uploaded_by_user_id` is kept separately for audit and carries no access
meaning.

Namespace is baked into every vector at upsert time, so this cannot be changed later
without re-ingesting.

**Capacity.** Pinecone caps namespaces per index by plan: Starter 100, **Builder 1,000**
(current), Standard 100,000. That cap is the ceiling on total agents, and it — not
storage — is the binding constraint. The full 14-corpus document set is only ~1.4 MB of
text, roughly 700–900 chunks, about 2–3 MB of vectors against a 10 GB allowance.

### 3.3 Indexing (runs when documents change)

```
Upload → 202 Accepted, document row at status "pending"
       ↓ (background job, own DB session)
       Load → Chunk → Embed (gemini-embedding-2, 768d)
            → Upsert to Pinecone (namespace = agent_{id})
            → Record documents / chunks / ingestion_runs rows
       ↓
       status: pending → processing → ready | failed     (client polls)
```

**Limit: 50 MB** (`MAX_UPLOAD_MB`). Accepted: `.md`, `.markdown`, `.txt`, `.pdf`.

**The cap and the background job are one change, not two.** Ingest was originally
synchronous, and that — not the deployment target — was the real constraint: raising the
limit alone would have converted a clean `413` into a request that holds a socket open for
minutes and then times out somewhere less legible. The limit became configurable only once
it stopped being the thing protecting the request timeout.

Both size checks are kept: a cheap pre-check on the multipart `size` header so a 200 MB
upload is refused before it is pulled into memory, and the authoritative one on the bytes
actually read. The header is attacker-controlled and nothing upstream validates it against
the body, so it can shortcut a rejection but can never authorise an acceptance.

**Duplicate detection stays synchronous**, before the handoff. A duplicate that returns 202
and then fails quietly in the background is a worse experience than an immediate `409`.

**Failure text lives in `audit_log`**, not on the document — there is no `documents.error`
column — under `app.rag.ingest.INGEST_FAILURE_ACTION`, keyed to the document id.

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

For each active golden-set question: run it through the agent as a **real turn** (writing
`queries`, `query_chunks` and `trace_events`, so an eval answer is as traceable as any
other), then score with Ragas on faithfulness, answer relevance, context precision and
context recall. Results persist to `eval_runs` / `eval_results` so successive runs are
comparable over time.

Ragas needs **both a judge LLM and an embedding model** — both Gemini here. It defaults to
OpenAI for both, so both must be configured explicitly or it fails on a missing
`OPENAI_API_KEY`. `context_recall` additionally requires a reference answer, which is why
`golden_questions.reference_answer` is not optional.

#### 3.6.1 Authoring the golden set

The set is **per agent** — a question about lecture transcripts scored against a policy
corpus measures nothing — and is built three ways, which coexist:

1. **AI-suggested.** An LLM reads a sample of the agent's own indexed chunks (from Postgres,
   not Pinecone: the corpus, not a similarity search) and proposes ten questions with
   grounded reference answers. Default split is **8 answerable + 2 refusal**.
2. **Edited.** Every field of every question is editable. Editing a suggestion flips its
   `source` from `ai_suggested` to `edited`, and the UI badges the two differently — the
   point of the edit feature is knowing which questions a model wrote and which a human has
   vetted. Re-running *Suggest* replaces untouched suggestions and **never** touches a
   question a human has edited or written.
3. **Plain JSON, imported and exported.** The file is meant to be hand-edited: import accepts
   the wrapped object or a bare list, ignores unknown keys, reports per-row problems rather
   than rejecting the file, and takes the file's line order as the set's order.

**Refusal questions must be plausible neighbours of the corpus, not absurdities.** For a
station-engineering corpus, "which of the fourteen launches took place in 2040?" — where the
text gives only a range — probes grounding. "What is the capital of France?" probes nothing,
because any model declines it. The generator is instructed accordingly, and this is the
single largest determinant of whether the set measures anything.

**Which is why the drafting model is its own setting (`GOLDEN_SET_MODEL`), split out of
`DECISION_MODEL`.** Measured head-to-head over the same corpus and prompt: Gemma's refusal
probes named facts the corpus never raises, while `google/gemini-3.7-flash` produced
questions hinging on details the corpus raises and then leaves incomplete — the propellant
type behind a mentioned thruster, the individual names of six mentioned modules. Its
reference answers were also several attributable claims rather than a single word
("Nineteen"), which matters because `context_recall` decomposes that field. The cost is
recorded rather than hidden: the drafting model is currently also the judge, so context
precision and recall are graded against references the judge wrote. Faithfulness and
answer relevance never read `reference`, and both context metrics are pinned at 1.0 by the
single-chunk corpus anyway.

#### 3.6.2 Scoring rules

**Refusal questions are excluded from all four metric means** and graded pass/fail on
behaviour instead (`eval_results.behaviour_ok`, reported as `refusal_pass / refusal_total`).
A *correct* refusal retrieves nothing useful and returns an answer that deliberately does not
follow from its context, so faithfulness and context recall score near zero for behaving
perfectly. Averaging them in would penalise correct refusals and — worse — aim the
weakest-metric pointer at whichever metric refusals punish hardest.

**`None` and `0.0` are different facts and stay different all the way to the screen.** `None`
means *not measured*; `0.0` means *measured and bad*. Conflating them is how a scorecard
lies: a run whose judge was rate-limited must read "measured nothing", not "perfectly
unfaithful".

#### 3.6.3 The weakest metric is the deliverable

A scorecard that does not say what to do next is a dashboard. Each metric maps to the
specific **agent parameter** to change — all four are per-agent editable:

| Weakest metric | What it means | What to change |
|---|---|---|
| Faithfulness | Retrieval worked; generation went past it | System prompt grounding clause; persona verbosity; generation model |
| Answer relevance | Grounded, but not answering the question | Prompt shape; preamble burying the answer |
| Context precision | Junk ranked into the top-n | `rerank_top_n`, `retrieve_k`, chunking |
| Context recall | Retrieval missed text the answer needed | Raise `retrieve_k`, `chunk_size`/`chunk_overlap`, check embedding model matches the index |

**A run took 23–25 minutes for ten questions** (measured twice on the direct-Gemini stack),
and now takes **90 seconds** — same agent, same ten questions, generation and judging both
via OpenRouter, zero metric failures. It stays a background job with progress regardless:
the shape that made it one is a slow model or a large golden set, and either can come back
with one config change. A 90-second job is not an argument for making it a request.

**The weakest metric is not automatically the thing to fix — read the answers first.** Run 3
named faithfulness (0.769) as weakest, and inspection showed the deductions were the Feynman
persona's analogy and its "restate this in your own words" prompt: material absent from the
context by design, in answers whose every corpus fact was correct. Acting on the pointer as
written would strip the pedagogy. See the open items below.

**Measured caveat, recorded because it inverts a reading:** on a single-chunk corpus,
context precision and recall both return 1.00 — that is retrieval that *cannot* fail, not
retrieval that is excellent. Treat a perfect retrieval score on a small corpus as "not yet
measured".

### 3.7 Conversations — multi-turn memory

Originally out of scope (§11). Added because a chat interface makes single-shot turns
untenable.

```
Turn 1: "What altitude does Kestrel Station orbit at?"   → embedded as typed
Turn 2: "What is its power budget?"
        ↳ contextualise against history (decision model, function calling)
        ↳ embed "What is the power budget of Kestrel Station?"   ← REWRITE trace event
        ↳ retrieve → rerank → answer
```

A `conversations` row threads `queries`; `queries.conversation_id` is nullable, because
one-shot rows predate it and NULL legitimately means "asked outside a thread".

**Contextualisation is bounded and fails soft.** It reads at most the last ~6 turns —
unbounded history grows the prompt without improving the rewrite, and this call is on the
latency path (measured 3.8 s). If it fails for any reason the raw question is used and the
turn continues: a failed rewrite must degrade to Stage 1 behaviour, never fail the turn.

**`rewritten_question` is null when no rewrite happened**, never a copy of the question, so
the UI can distinguish "not rewritten" from "rewritten to the same string". It is surfaced
above the answer as *"Searched for …"*, which is the most useful thing a multi-turn RAG can
show about itself — it explains why an answer is about something the user did not type.

**One conversation per eval question, never one per run.** Sharing a thread across a golden
set would let contextualisation rewrite later questions through earlier ones, so the
scorecard would measure a different set of questions than the editor displays.

### 3.8 API surface

Routes are **nested under the agent** and resolve through an ownership dependency. The
original flat surface predates the move of tenancy from users to agents (§3.2); flat routes
would have to carry the agent id in a body or query parameter, which is exactly the
client-supplied scoping §7 forbids. Nesting makes the constraint structural.

| Endpoint | Method | Auth | Purpose |
|---|---|---|---|
| `/api/auth/google/login` · `/callback` | GET | — | OAuth begin / exchange |
| `/api/auth/me` · `/logout` | GET · POST | ✅ | Current user · revoke session |
| `/api/auth/dev-login` | POST | — | **Dev only**, triple-gated (§7) |
| `/api/agent-templates` | GET | ✅ | The eight persona presets |
| `/api/agents` | GET/POST | ✅ | List / create |
| `/api/agents/{id}` | GET/PATCH/DELETE | ✅ | Read / retune / delete + drop namespace |
| `/api/agents/{id}/documents` | GET/POST | ✅ | List / upload (**202**, background) |
| `/api/agents/{id}/documents/{doc}` | DELETE | ✅ | Delete rows **and** vectors |
| `/api/agents/{id}/ask` | POST | ✅ | One-shot; creates a conversation implicitly |
| `/api/agents/{id}/conversations` | GET/POST | ✅ | Threads for this agent |
| `/api/conversations/{id}` | GET/PATCH/DELETE | ✅ | Transcript / rename / delete |
| `/api/conversations/{id}/ask` | POST | ✅ | Ask **within** a thread (history-aware) |
| `/api/agents/{id}/queries` | GET | ✅ | Query history |
| `/api/trace/{query_id}` | GET | ✅ | Decision timeline |
| `/api/agents/{id}/golden-questions` | GET/POST | ✅ | List / add one |
| `…/golden-questions/suggest` | POST | ✅ | **202**, LLM proposes ten |
| `…/golden-questions/export` · `/import` | GET · POST | ✅ | Plain JSON out / in |
| `/api/golden-questions/{id}` | PATCH/DELETE | ✅ | Edit / remove |
| `/api/agents/{id}/eval-runs` | GET/POST | ✅ | History / start (**202**) |
| `/api/eval-runs/{id}` | GET/DELETE | ✅ | Scorecard / remove |
| `/api/health` · `/api/config` | GET | — | Render health check · non-secret config |

**Three routes are reached by their own id** — `/api/conversations/{id}`,
`/api/golden-questions/{id}`, `/api/eval-runs/{id}` — and so have no `agent_id` for the
ownership dependency to bind to. Each checks ownership by hand, by following the row to its
agent. They are the highest-risk lines in the codebase and are commented as such.

`/api/feedback` remains specified and unbuilt.

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

### 4.2 Agents

**`agent_templates`** — named parameter presets, the "premade" starting points
| Column | Notes |
|---|---|
| `slug`, `name`, `description` | |
| `chunk_size`, `chunk_overlap`, `splitter` | Indexing defaults |
| `retrieve_k`, `rerank_enabled`, `rerank_top_n` | Retrieval defaults |
| `score_threshold`, `max_rewrites` | Stage 2 loop bounds |
| `system_prompt`, `is_active` | |
| **`persona_role`, `pedagogy`, `icon`, `category`** | The teaching persona — see below |

#### Teaching personas

Nine templates ship. Three are parameter presets; **five are teaching personas grounded in
learning science**, because the workshop corpus is course material and "what the agent
retrieves" matters less to a learner than "what it does with it"; and one is an
**orchestrator** that chooses among the five per turn.

| Slug | Persona | Rests on | k / top_n |
|---|---|---|---|
| `lecture-qa` | Teaching assistant | PRD default, sized for transcripts | 20 / 3 |
| `policy-lookup` | Policy assistant | Clause-structured lookup | 20 / 3 |
| `feynman-explainer` | Explainer | Feynman technique; self-explanation effect; the illusion of explanatory depth | 20 / 3 |
| `socratic-tutor` | Socratic tutor | Elaborative interrogation | 20 / 4 |
| `polya-coach` | Problem coach | Pólya's four phases, *How to Solve It* | 20 / 5 |
| `quiz-generator` | Quiz writer | Retrieval practice / testing effect (Roediger & Karpicke) | **40 / 8** |
| `reflective-coach` | Reflection guide | Gibbs' reflective cycle; Schön | **12 / 4** |
| **`adaptive-tutor`** | **Teaching orchestrator** | **Adaptive instruction; the expertise-reversal effect — guidance that helps a novice measurably hinders a learner who already has the schema, so the useful unit is a *choice between kinds of help*** | **24 / 5, overridden per routed specialist** |
| `from-scratch` | Blank canvas | Model defaults | 20 / 3 |

**`adaptive-tutor` routes between the five, and that is PRD-catalogue "query routing" rotated
onto the only axis this system has.** Routing between *sources* is architecturally closed
here and deliberately so — one namespace per agent, and `SearchCorpusArgs` has a single field
precisely so a prompt-injected model cannot name another corpus. Routing between *teaching
strategies* is the decision this product actually has, and it moves retrieval breadth
(`retrieve_k`, `rerank_top_n`) as well as voice. It cannot move `chunk_size`, which is fixed
at ingest. Full design in
[new features/11-orchestrator-and-self-check.md](new%20features/11-orchestrator-and-self-check.md).

Retrieval parameters differ *per persona* rather than being copied: a quiz generator draws
items from across the whole corpus and needs breadth; a reflection guide works a narrow
point and needs focus.

**A persona changes the shape of a response, never its grounding.** Every persona prompt
states the refusal rule at least as forcefully as the persona rule, because a warm,
confident teaching voice makes an ungrounded answer *read* better than a blunt refusal —
personas are the most likely place in this system for hallucination to start.

**And Stage 3 measured that this is not sufficient.** On the first real golden-set run the
Feynman Explainer scored **`refusal_pass = 0 / 2`**, reproduced on a second run: it answered
both questions its corpus could not answer. The cause is not a missing rule but the persona
itself — it is designed to *name the gap* ("the material does not cover X, but here is what
it does say"), which is pedagogically right and structurally an answer rather than a
refusal. The behaviour the persona rewards and the behaviour the golden set measures are in
direct tension. Retesting the same set against the non-persona `lecture-qa` template is the
obvious next experiment and costs one run.

**`agents`** — one user-created RAG agent: one corpus, one config, one namespace
| Column | Notes |
|---|---|
| `owner_user_id` → users | Unique per owner + name |
| `template_id` → agent_templates | Nullable — "from scratch" leaves it null |
| *(all template parameter columns)* | **Copied** at creation, then independently editable |
| `visibility` | `private` only today; the column exists for later sharing models |
| `status` | `empty` / `indexing` / `ready` |
| `embedding_model` | Which model built this namespace; a mismatch means re-ingest |

**"Create from a template" and "create your own" are the same code path** — a template
only supplies starting values. Parameters are **copied onto the agent** rather than read
through the template, so editing a template never silently re-tunes agents somebody
already built and evaluated.

### 4.3 Corpus

**`documents`** — one row per uploaded file
| Column | Notes |
|---|---|
| `agent_id` → agents | **The scoping key.** Drives namespace selection |
| `uploaded_by_user_id` → users | Audit only — carries no access meaning |
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
metadata has a per-record size limit, and having the text locally means you can **re-embed**
without re-parsing the original files.

**Corrected 2026-08-17: this sentence used to say "re-chunk or re-embed", and the first half
was never true.** Re-embedding from `chunks.text` is genuinely cheap and is what makes a
model or dimension change bounded. Re-chunking is not possible from it, because chunk
boundaries are lossy: re-splitting already-split text at a larger `chunk_size` cannot recover
what a smaller split separated with overlap, and at a smaller size it produces different
boundaries than splitting the original would. Re-chunking needs the **original file**, which
§7 records as newly available in object storage — the capability now exists, and no route
uses it yet (open item 38).

**`ingestion_runs`** — one row per ingest job
| Column | Notes |
|---|---|
| `document_id` → documents | |
| `embedding_model`, `embedding_dimension` | **Enforces the §7 constraint** |
| `chunk_size`, `chunk_overlap` | Makes the Build #1 chunk-size experiment reproducible |
| `chunk_count`, `started_at`, `finished_at`, `status` | |

### 4.4 Query & trace

**`conversations`** — one thread of turns against one agent (§3.7)
| Column | Notes |
|---|---|
| `agent_id` → agents, `user_id` → users | Cascade on both |
| `title` | Auto-derived from the first question; renameable |
| `is_archived`, `updated_at` | The chat list sorts on `updated_at` |

`updated_at` is maintained by the ORM's `onupdate`, **not** a database trigger — appending a
`queries` row does not touch the conversation, so whatever records a turn must also write the
conversation or threads never reorder.

**`queries`** — one row per question asked
| Column | Notes |
|---|---|
| `user_id`, `session_id` | |
| `conversation_id` → conversations | **Nullable.** NULL means a one-shot question outside any thread — the rows that predate §3.7 — so every reader must handle it rather than assume a thread |
| `rewritten_question` | The standalone question actually embedded, or NULL when no rewrite fired. Never a copy of the question |
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

### 4.5 Evaluation

**`golden_questions`** — the test set, **per agent**
| Column | Notes |
|---|---|
| `agent_id` → agents | Nullable for legacy rows; **every read must filter on it explicitly** |
| `question`, `reference_answer` | Reference answer required by `context_recall` |
| `expected_behaviour` | `answer` / `refuse` — refusal is a correct outcome |
| `source` | `ai_suggested` / `edited` / `manual` / `imported` — provenance is the point of the editor |
| `order_index` | Stable display order; also the tiebreak that keeps two runs of one set comparable |
| `is_active` | Retire questions without deleting history |

Scoping was added late. A bare `select(GoldenQuestion).where(is_active)` now silently mixes
unscoped legacy rows into whichever agent is being scored — reintroducing at the query layer
exactly the failure the column was added to prevent. Filter on `agent_id` as well.

**`eval_runs`** — one row per scorecard
| Column | Notes |
|---|---|
| `agent_id`, `user_id`, `status` | `pending` / `running` / `completed` / `failed` |
| `judge_model`, `generation_model` | **Both**, recorded per run — see below |
| `progress_done`, `progress_total` | A run takes 23–25 min; the UI polls these |
| `summary` (JSONB) | Aggregate scores, `weakest_metric`, `scored_count`, refusal tally, resolved investment advice |
| `error` | Run-level failure. Distinct from `eval_results.error` |
| `started_at`, `finished_at`, `notes` | `notes` is what changed since last run — how two runs become an experiment rather than two numbers |

`generation_model` is stored **per run**, never read back through `agents.generation_model`
at display time: the agent's setting can change after a run, and reading it live would
attribute a score to a model that never produced the answer. When the two model columns are
equal the run is self-judged, and the scorecard says so.

`summary` is JSONB rather than four float columns because it also carries the weakest-metric
pointer and its advice, is written once at the end of a run, and will grow as metrics are
added. Nothing in the database validates its shape, so it is serialised through a single
Pydantic model rather than a hand-built dict.

**`eval_results`** — one row per question per run
| Column | Notes |
|---|---|
| `eval_run_id`, `golden_question_id`, `query_id` | `query_id` joins an eval answer back into the normal Trace view |
| `faithfulness`, `answer_relevance`, `context_precision`, `context_recall` | NULL means *not measured*, never zero |
| `behaviour_ok` | Did it answer / refuse as expected? Not derivable from the floats — a correct refusal is four NULLs and would otherwise be indistinguishable from a crash |
| `error` | One question failing must not void the run |

### 4.6 Supporting tables

**`feedback`** — `query_id`, `user_id`, `rating` (+1/−1), `comment`.
Ten golden questions catch regressions; real users catch what you didn't think to test.

**`api_usage`** — the spend ledger, and **one row per billable CALL, never per turn**.
Original columns `user_id`, `provider`, `operation`, `units`, `estimated_cost`; widened
2026-08-20 with `agent_id`, `query_id`, `call_kind`, `model`, `served_provider`,
`prompt_tokens`, `completion_tokens`, `reasoning_tokens`, `cached_tokens`, `cost_usd`,
`cost_is_estimated`, `generation_id` and `duration_ms`. The workshop names cost blowout as one
of four agentic failure modes; this is how you see it coming rather than discovering it on a
bill.

**A call, not a turn, because one question is nine billable calls** — measured on a
two-document agent: a rewrite, three embeddings, three reranks and two generation calls,
$0.00049354. A turn-shaped row would have shown a complete-looking total with the eval judge
and the golden-set drafter silently missing, since neither belongs to any `queries` row.

**`cost_usd` is REPORTED by OpenRouter, never computed here.** No price table exists or
should: two identical 282/2-token requests measured $2.002e-05 and $5.684e-06 because they
landed on different endpoints, so arithmetic over a published rate is *less* accurate than the
number already on the wire. `served_provider` records which endpoint answered.
`cost_is_estimated` is true only for Cohere reranking, the one provider that reports units and
not cost — and estimating it is opt-in (`COHERE_SEARCH_UNIT_USD`, default `0.0` = do not
estimate), landing in `estimated_cost` and never in `cost_usd`.

**`agent_id` and `query_id` are `ON DELETE SET NULL`, deliberately not CASCADE.** Deleting an
agent must not delete the record that it cost money. Contrast `query_chunks`, whose CASCADE
destroys eval evidence (open item 18) — the same choice made the other way, for a table where
it was wrong.

**`audit_log`** — `user_id`, `action`, `resource_type`, `resource_id`, `metadata` JSONB.
Day 1 lists "a full audit trail of every retrieval" as one of the reasons to build rather
than buy. This table is what makes that claim true.

### 4.7 Indexes

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
| `OPENROUTER_API_KEY` | **Every model call** — generation, decision, golden-set drafter, Ragas judge **and embeddings** (since 2026-08-16) | ❌ passed explicitly in `app/rag/llm.py` |
| `GEMINI_API_KEY` | The **rollback** embedding route only (`EMBEDDING_ROUTE=google`) — optional since 2026-08-16 | ✅ `langchain-google-genai` |
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
| `ENVIRONMENT` | Gates the dev-login route | ❌ **defaults to `development`** |
| `DEV_AUTH_ENABLED` | Gates the dev-login route | ❌ defaults to `false` |
| `MAX_UPLOAD_MB` | Upload cap (§3.3) | ❌ defaults to 50 |
| `INGEST_IN_BACKGROUND` | Off-request ingest (§3.3) | ❌ defaults to `true` |
| `GOLDEN_SET_MODEL` | Drafts the golden set (§3.6.1) | ❌ defaults to `minimax/minimax-m3` — a **third** vendor, so a run is not graded against references its own judge wrote |
| `RAGAS_JUDGE_MODEL` | Stage 3 judge (§2.1) | ❌ defaults to `google/gemini-3.7-flash` |
| `RAGAS_JUDGE_REASONING_EFFORT` | Thinking is **mandatory** on Flash — only turnable down | ❌ defaults to `low` |
| `RAGAS_MAX_CONCURRENCY` | Judged calls in flight | ❌ defaults to 2, for provider rate limits |
| `OPENROUTER_REQUIRE_PARAMETERS` | Route only to providers advertising every parameter sent | ❌ defaults to `true` — **leave it on** |
| `EMBEDDING_ROUTE` | Which gateway reaches the embedder — the ROAD, never the space | ❌ defaults to `openrouter`; `google` is the rollback |
| `ADMIN_EMAILS` | Comma-separated. Read **once, by a migration**, never on a request path | ❌ defaults to empty — nobody is promoted, and the console 403s for everyone |
| `METERING_ENABLED` | Master switch for the usage meter | ❌ defaults to `true`. Off: no rows, and every other code path is byte-identical |
| `METERING_STRICT` | Re-raise instead of swallowing a metering fault | ❌ defaults to `false`. **On in harnesses, off in production** |
| `COHERE_SEARCH_UNIT_USD` | Price per rerank search unit, for an **opt-in** estimate | ❌ defaults to `0.0` = do not estimate. A hardcoded price is a number nobody re-checks |

**`ENVIRONMENT` must be set to `production` on Render.** It defaults to `development`, so on
the deployed service only two of the dev-login route's three gates are doing work. The route
still fails closed there — `DEV_AUTH_ENABLED` is false and `request.client.host` behind
Render's proxy is the proxy, never loopback — but a gate that is inert is not a gate.

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
| Pinecone | Serverless index | **Builder** | **Singapore** | ~$20/mo |
| Cohere | Rerank API | Free tier (1,000 calls/mo) | US | $0 |
| Gemini | AI Studio API | Free tier | Global | $0 |

Estimated total: **~$33–34/mo**, plus Postgres storage at roughly $0.30/GB/month. Render's
pricing page is JavaScript-rendered and could not be read programmatically, so the
per-service figures are approximate; actual charges will be visible on the first invoice.

The Pinecone Builder plan buys two things at once: the agent ceiling rises from 100 to
1,000 namespaces (§3.2), and `ap-southeast-1` becomes available, removing the
trans-Pacific retrieval hop (§6.2).

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
| Pinecone index | AWS **`ap-southeast-1`** (Singapore) | Co-located with the backend; no trans-Pacific hop |

**The backend's region is not optional.** Render states plainly that services in different
regions cannot communicate over a private network. A Singapore database with an Oregon
backend would be forced onto the external connection string over the public internet —
slower, and needlessly exposed.

**Pinecone was initially forced into `us-east-1`.** On the free Starter plan, creating the
index in Singapore returns:

> `Your free plan does not support indexes in the ap-southeast-1 region of aws.`

That is also why the pre-existing indexes on this account are all `us-east-1` — the only
region Starter allows, not a stylistic default. The **Builder upgrade (~$20/mo)** removed
the restriction, and the index was recreated in `ap-southeast-1` while it still held zero
vectors, so the move cost nothing. Doing it after the first ingest would have required
re-embedding every document, because region is fixed at index creation.

Everything except Cohere is now co-located in Singapore.

### 6.3 Expected latency budget

| Hop | Path | Rough cost |
|---|---|---|
| Backend ↔ Postgres | Private network, same region | sub-millisecond |
| Backend ↔ Pinecone | Singapore ↔ Singapore | low tens of ms |
| Backend ↔ Gemini | Global endpoint | varies with model and prompt |
| **Backend ↔ Cohere Rerank** | **Singapore ↔ US** | **~100–300ms plus transit, once per query** |

Cohere is now the **only** cross-Pacific hop, and it fires once per query in Stage 2 —
unlike Pinecone, which is called two to three times per question and would have compounded
the penalty. Cohere has no Singapore endpoint, so this one is not avoidable on any plan.
Measured against multi-second LLM generation it is noticeable rather than fatal, but it is
the first place to look if answer latency disappoints.

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

The frontend was last verified live on 2026-08-16 at commit `1874950` (`Improve empty-agent
and creation UX`). Render published the static site from that commit. The backend skipped the
frontend-only rebuild as intended and continued returning 200 from health/config checks.

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

~~**Original uploads are never stored.** An HTTP upload is bytes in memory; text is
extracted and `chunks.text` becomes the source of truth. Render's filesystem is ephemeral,
so there is nowhere to put a file.~~ **Superseded 2026-08-17 by the object-storage change
set** (`new features/13-object-storage/`). Originals are now written to a private Cloudflare
R2 bucket before the `documents` row is staged, and `documents.storage_key` names them.

**This bullet is added and struck in the same edit, which needs explaining rather than
hiding.** Four code sites — `rag/ingest.py`, `rag/jobs.py`, `api/documents.py` and
`requirements.in` — cited "PRD section 7" as the authority for that rule, and **section 7
never contained it**. The constraint was real and correctly implemented; only its stated
home was wrong. Deleting the citations would have left no record that the rule ever existed;
writing the bullet and striking it puts the four references somewhere that resolves, with
the date they stopped being true. The nearest thing that did exist was a sentence in §4.3,
and that one additionally over-claimed what the stored chunk text buys — corrected there.

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
with no undo**: Pinecone dimension (768), Pinecone region (`ap-southeast-1`), and Render
region (Singapore, for every service).

A fourth belongs with them: **the Pinecone namespace scheme** (`agent_{id}`, §3.2). It is
written into every vector at upsert.

**None of the Pinecone ones are truly unrecoverable, and the cost varies enormously.**
Vectors can be fetched and re-upserted bit-identically, so changing index name, region,
cloud or namespace scheme is a **data copy with no re-embedding**. Only a change to
*dimension* or *embedding model* invalidates the vectors themselves and forces a rebuild
from `chunks.text`. `scripts/migrate_index.py` implements the blue/green procedure —
build alongside, copy, verify, swap, then delete — and CLAUDE.md documents the full cost
hierarchy. The Render region constraints are the genuinely painful ones, because they
require recreating a service and migrating the database across.

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

As of 2026-08-16. Provisioning scripts under `scripts/` are idempotent and safe to re-run;
test harnesses have the setup/cleanup contracts documented in
[`new features/06-test-plan.md`](new%20features/06-test-plan.md).

| Resource | Status | Detail |
|---|---|---|
| Render Postgres | ✅ **Live** | `agentic-rag-db` · `dpg-d9vt7v1t0dsc738c8kpg-a` · `basic_256mb` · **singapore** · PG **18** · `available` |
| Pinecone index | ✅ **Live** | `agentic-rag-ntu` · **768d** · cosine · serverless aws **ap-southeast-1** · Builder plan · `Ready` |
| Google OAuth client | ✅ **Live** | `Agentic RAG Web` · Web application · `dsai-mod-2-group-project` · **In production** · External · consent brand `Groundwork`, **shared with the `Bedtime Story Web` client in the same project** and unverified, so the screen shows the redirect host rather than either name |
| GitHub repo | ✅ **Live** | https://github.com/simonraj79/AgenticRAG (public) · `main` synced at `1874950` |
| Render web service | ✅ **Live** | `agentic-rag-api` · `srv-d9vtuhpt0dsc738dmgsg` · `starter` · **singapore** · https://agentic-rag-api-6x6b.onrender.com |
| Render static site | ✅ **Live** | `agentic-rag-web` · `srv-d9vtuj61egvs73fdfang` · free · https://agentic-rag-web-e9e9.onrender.com · commit `1874950` verified |

**All six resources are provisioned.** Verified end to end: `/api/health` returns
`{"status":"ok","database":"ok"}` from Singapore against the private-network Postgres, and
the static site serves.

Pinecone host: `agentic-rag-ntu-o3j2ojr.svc.aps-d9bb-582b.pinecone.io`
Pinecone tags: `embedding_model=gemini-embedding-2`, `dimension=768`, `project=agentic-rag-ntu`

**Postgres.** Created via `scripts/create_render_db.py`. Connection strings were written
directly into `.env` without being printed to a terminal. External access may require
adding your IP to the database's allow-list in the Render dashboard.

**Pinecone.** Created via `scripts/create_index.py`, now in Singapore on the Builder plan.
It took three attempts, and the failures are recorded because they shaped the
architecture:

1. `ap-southeast-1` was rejected — the free Starter plan permits only `us-east-1` (§6.2).
2. Creation was then rejected on quota: Starter caps a project at 5 serverless indexes and
   the account held exactly 5. Resolved by removing two unused indexes (`tbllive`,
   `localflowise`), leaving `postgrespace`, `pacecoursedemo` and `pace` untouched.
3. The index was created in `us-east-1` as a result, then **recreated in
   `ap-southeast-1`** once the Builder upgrade landed.

**The recreate was free because the index was still empty.** Region is fixed at creation,
so moving it after the first ingest would have meant re-embedding every document.
`scripts/create_index.py --recreate` enforces this: it checks `total_vector_count` and
**refuses to delete a populated index**, rather than silently discarding vectors.

Re-running the script without `--recreate` is always safe: it detects the existing index,
verifies dimension, metric and region against this document, and reports drift.

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
│   │   ├── api/                 Route modules (§3.8)
│   │   ├── auth/                Authlib wiring, session management
│   │   ├── db/                  SQLAlchemy models, seed data, personas
│   │   ├── rag/
│   │   │   ├── retriever.py     THE SEAM — built in exactly one place
│   │   │   ├── ingest.py        Load → chunk → embed → upsert
│   │   │   ├── pipeline.py      Stage 1 chain + history-aware rewrite
│   │   │   ├── delete.py        Vectors before rows (§7)
│   │   │   ├── jobs.py          Background ingest — own DB session
│   │   │   └── trace.py         Decision logging
│   │   └── eval/
│   │       ├── generate.py      LLM-suggested golden questions (§3.6.1)
│   │       ├── ragas_runner.py  The four metrics, judge + embeddings
│   │       ├── metrics_guide.py Weakest metric → next investment (§3.6.3)
│   │       └── jobs.py          Background eval run — own DB session
│   ├── alembic/                 Migrations
│   └── requirements.txt
├── frontend/
│   ├── src/
│   │   ├── views/               Login · Dashboard · AgentDetail
│   │   │                        (+ Chat · Documents · Evaluate tabs)
│   │   ├── components/          Message · CitationCard · TracePanel ·
│   │   │                        Scorecard · GoldenSetEditor
│   │   └── lib/                 api.ts — the only door to the backend
│   ├── package.json
│   └── vite.config.ts
├── scripts/
│   ├── create_index.py          Pinecone provisioning (idempotent)
│   ├── migrate_index.py         Blue/green index migration (§7)
│   ├── slice_check.py           RAG end-to-end check
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

Frontend verification:

```bash
cd frontend && npm test && npm run build
```

With the backend and frontend running on their required local ports:

```bash
python scripts/ui_check.py
```

Local defaults assume the backend on `http://localhost:8000` and the frontend on
`http://localhost:5173`, which are the values in `.env.example` and the registered
localhost redirect URI.

---

## 10. Open items

### Design decisions — 2026-08-15

| Decision | Choice | Rationale |
|---|---|---|
| Tenancy | **Namespace per agent** | True isolation; a query cannot physically reach another agent's vectors |
| Pinecone plan | **Builder (~$20/mo)** ✅ done | Raises the agent ceiling 100 → 1,000 **and** unlocks `ap-southeast-1` |
| Index region | **Singapore** ✅ done | Recreated while empty, so free; erases the trans-Pacific retrieval hop |
| Chunking default | **800 tokens / 120 overlap, markdown-aware** | Sized for lecture transcripts, which dominate the corpus |
| Slide PNGs | **Index the `.md` text; link images for citation display** | The PNGs render text already present in `{n}-slides.md` |
| Marketplace scope | **Private agents + admin oversight** | Delivers the full flow with no sharing-permission surface |

**Chunking rationale in full.** The corpus is dominated by lecture transcripts (24–71 KB
each), which are conversational and spread an answer across several turns of dialogue —
small chunks decapitate them. 800 tokens keeps that context together while using only 10%
of `gemini-embedding-2`'s 8,192-token ceiling, so nothing truncates. 15% overlap is the
standard insurance against answers straddling a boundary. Markdown-aware separators keep a
slide's heading attached to its body instead of cutting across it. Retrieval parameters
follow the workshop unchanged: top-20 → rerank → top-3, rewrite below 0.5, max 2 rewrites.

**The slide images need object storage to actually display.** `chunks.asset_uri` is in the
schema, but Render's filesystem is ephemeral and the PNGs are ~124 MB of NTU material that
cannot go in a public repo. Serving them requires R2/S3, which is not provisioned. The
text pipeline is unaffected — this only gates the citation-image feature.

---

Infrastructure is complete. Stages 1 and 3 are built; Stage 2 is half-built.

| # | Item | Status |
|---|---|---|
| 1 | Seed `agent_templates` | ✅ **8 templates**, five of them teaching personas (§4.2) |
| 2 | OAuth routes + session middleware (`app/auth/`) | ✅ done |
| ~~3~~ | ~~Agent CRUD~~ | ✅ **done, admin listing included 2026-08-20.** `/api/admin/agents` lists every agent across every owner with corpus size and spend, behind `AdminUser`. The listing was the last piece of "private agents + admin oversight" (§10 design decisions) |
| 4 | RAG dependencies | ✅ done, plus `ragas`, `langchain-community<0.4`, `python-multipart` |
| 5 | Ingest pipeline, namespace-scoped | ✅ done, now 50 MB and off-request (§3.3) |
| 6 | Retriever seam + Stage 1 chain | ✅ done |
| ~~7~~ | ~~**Stage 2 loop** + trace writing~~ | ✅ **done, and deliberately not as specified.** The score-triggered rewrite loop was **superseded** by the agent loop (§3.5a). A threshold could not have worked: on-topic questions measured 0.61–0.67 and off-topic 0.49–0.58, so 0.5 sits *inside* the overlap and fires late on bad retrievals and early on good ones. The loop now triggers on the model's own admission that something is missing, read off the answer text with the same marker list `queries.refused` uses |
| 8 | 10-question golden set + Ragas wiring | ✅ done, with authoring and editing (§3.6.1) |
| 9 | React views | ✅ done — Login · Dashboard · Chat · Documents · Evaluate; source-first empty workspace and modal creation flow audited 2026-08-16 |
| ~~10~~ | ~~Object storage (R2/S3)~~ | ✅ **done 2026-08-17, and the scope line was wrong.** It read "only gates citation images"; object storage also gated handout bytes (item 25) and — unrecorded anywhere — the ability to re-chunk a document at all, because the file a new split would need was discarded. A private R2 bucket (`groundwork-media`, APAC) now holds handout files and original uploads, reached by presigned URL. **`chunks.asset_uri` is still unwritten**: the slide-image half remains gated on item 12, which a private bucket does not resolve |
| 11 | Does `gemma-4-31b-it` support structured output? | ✅ **yes**, via `function_calling`; `DECISION_MODEL` collapsed onto it |
| 12 | Workshop PDFs in a public repo | Open — see below |
| **13** | **SSE streaming** (§2.2) | Open — **much less urgent since the move to OpenRouter**: a persona turn measured 6.3 s and a full ten-question run 90 s, against 30–60 s per turn before. Still the right shape, no longer the top UX problem |
| ~~14~~ | ~~**OAuth consent brand reads "Bedtime Story"**~~ | ✅ **done** — renamed to `Groundwork` on the Branding page, 2026-08-15. The unrecognised app name an attendee was being asked to trust is gone. It was **not** replaced by `Groundwork`: an unverified brand is not displayed at all, so the screen now reads the redirect host `agentic-rag-api-6x6b.onrender.com`. Showing the name needs branding verification, which the scopes themselves do not require (§8) |
| ~~15~~ | ~~Move the eval judge off the generation model~~ | ✅ **done** — judge is `google/gemini-3.7-flash` via OpenRouter; verbatim-context faithfulness went 0.000 → 1.000 (§2.1) |
| **16** | **Coaching personas fail refusals** (`1/2`) | Open — was `0/2` twice; three of those four rows were a detector gap, now fixed. The remaining row is real: the persona names the gap instead of declining (§4.2) |
| **17** | Multimodal embedding of PDFs and images | Open — deliberately deferred, see §2.3 |
| **18** | Deleting a document destroys past queries' stored contexts | Open — FK cascade; costs Stage 3 its evidence |
| **19** | Blocking SDK calls inside `async def` | Open — uniform deferral; fix all call sites together |
| **20** | **Faithfulness penalises a teaching persona for teaching** | Open — measured run 3. The analogy and the comprehension check are unsupported by construction, so the weakest-metric pointer advises deleting the pedagogy (§3.6.3) |
| **21** | **A refusal-first prompt suppresses tool use** | Open as a *tension*, mitigated not solved. Grounding-first prompting is why this system can be trusted to decline; it is also why the model would rather declare a gap than search for it. Three prompt variants (including an explicit "you MUST call search_corpus") produced zero tool calls. The gap trigger works around it; the underlying competition between the two instructions remains |
| **22** | **`tool_choice="any"` is silently ignored on OpenRouter** | Open — only a *named* tool forces a call. Same family as the `max_completion_tokens` 404: a parameter accepted and not honoured. Worse than an error, because a dropped "required" is indistinguishable from a model that declined |
| **23** | **Tool use is unmeasured** | Open. Ragas scores whether an answer is faithful to its context; it has no opinion on whether the right tool was called, and inventing a faithfulness-shaped score for tool choice would be a new instrument of unknown validity — the exact failure items 15 and 16 record. Trajectory evaluation is Stage 4 |
| **24** | **The sandbox is not a container** | Open, and deliberately so. Hardened subprocess: empty environment, import allowlist plus attribute denylist, no sockets, POSIX rlimits, hard timeout. It defends against a confused or prompt-injected model, not an adversary with arbitrary input. `pathlib` can still read outside the scratch directory. Full contract in `new features/02-code-interpreter.md` §5 |
| ~~25~~ | ~~**Handout bytes live in Postgres**~~ | ✅ **done 2026-08-17.** Bytes are in R2 and the download route answers 302 to a presigned URL, so they no longer pass through the single uvicorn worker. **`handouts.content` was deliberately NOT dropped** — it is what keeps `STORAGE_ROUTE=postgres` a working rollback, the same blue/green discipline `migrate_index.py` applies to a Pinecone index. Dropping it is a separate change set (open item 39) |

Items 13–20 were all discovered by building and measuring, not by planning. Items 15, 16 and
20 are the direct output of Stage 3 and are the strongest argument for having built it — and
20 is the sharpest of them, because it is a defect in the measuring instrument that only
became visible once the instrument was trustworthy enough to be believed.

**Resolved:** the redirect URI has been corrected to the real Render hostname;
`DATABASE_URL` on the service is the internal URL.

**Watch — and this has now bitten twice.** The Postgres allow-list holds fixed `/32`
entries, so moving between the campus network and anywhere else silently revokes local
database access; it surfaces as `ConnectionDoesNotExistError`, which names no firewall.
Currently allowed: `155.69.165.66/32` (campus) and `116.88.179.94/32`. Diagnose with
`curl -s https://api.ipify.org` before reading the traceback, and note that Render's PATCH
**replaces** the whole list, so every entry to keep must be resent. Deployed traffic is
unaffected — it uses the private network.

**On item 6.** The repository is public and the three source PDFs total roughly 17 MB of
NTU course material. Committing them is a copyright question rather than a technical one,
and large binaries in git are permanent — removing them later requires rewriting history.
Options: keep them local and gitignored, or confirm the course permits redistribution.
The `docs/` directory in §9.1 assumes they stay; adjust if not.

---

### Opened 2026-08-16 by the DeepSeek swap

Full record in [new features/09-deepseek-agentic.md](new%20features/09-deepseek-agentic.md).

**26. ~~`agents.generation_model` is unreachable from the API.~~ RESOLVED 2026-08-16 —
exposed.** It is now on `AgentTunables` (so create *and* update accept it) and on
`AgentOut`, with a `<select>` in the agent settings sheet. Null still means "use
`settings.generation_model`", and clearing the field is a supported operation rather than
a 422.

Three decisions inside it, recorded because each had a defensible opposite:

- **Free text, not a `Literal`.** The `SplitterName` precedent argues for enumerating;
  `llm.py`'s "a free-text column an operator can type into" argues against. Splitter won
  its enum because a bad value *silently downgrades* to a splitter the user cannot see they
  got. A bad model id cannot be invisible — it fails every subsequent turn loudly — and a
  whitelist would mean a code change and a deploy each time OpenRouter adds a model, in a
  workshop whose subject is trying models. The UI offers a measured shortlist; the API
  stays open.
- **A bare id is refused at save time.** `openrouter_slug()` guesses `google/<model>` and
  logs a warning nobody reads, so a typo used to be stored and then 404 on every answer —
  an error CLAUDE.md records as reading like an outage. `_reject_unroutable_model` turns
  that into a 422 naming the fix, at the moment a human can act on it. Shape only; it does
  not check the model exists, because that would put a third-party network call inside a
  settings save.
- **Not copied from templates.** `agent_templates` has no such column, and a persona is a
  claim about *how* to answer rather than about which model answers — the same argument
  that kept `tools_enabled` off templates.

**One bug had to be fixed before the picker was safe to ship.** `generation_reasoning` is
false, so every generation call carried `reasoning: {"enabled": false}` — which
`google/gemini-3.7-flash` answers with a hard 400, *"Reasoning is mandatory for this
endpoint and cannot be disabled."* Selecting Flash would have broken every turn on that
agent. `build_chat_model` now withholds the flag for families that refuse it. While the
value was settings-only nobody was going to point generation at Flash by accident; as a
menu item it was one click.

**27. `"does not contain"` sits in the hard refusal tier, against the stated rule.**
CLAUDE.md's rule is that the hard tier is for phrases a model would *never* write while
answering. `"does not contain"` fails it — `refusal.py`'s own docstring example is an
answer containing it, and that answer scores as a refusal.

Moving it to the position-gated caveat tier would follow the rule and would change what
`queries.refused` counts, and therefore what every scorecard in [EVAL.md](EVAL.md) is
comparable to. **That is an evaluation decision, not a swap decision.** Pinned by
`scripts/refusal_check.py` case 15b so the current behaviour cannot drift while it is
undecided.

**28. The retrieval budget is bounded in steps, not calls.** `max_tool_steps` limits
rounds; the generation model emits 1.50–2.00 tool calls per round, measured to
`tool_steps=3, tool_calls=6` on one turn. The slider a workshop attendee tunes therefore
controls something roughly half as large as its label implies. Both numbers are now
recorded and the turn chip renders the larger, so it is visible rather than hidden — but
whether the *budget* should count calls is unresolved.

---

### Opened 2026-08-16 by the orchestrator and self-check build

Full record in
[new features/11-orchestrator-and-self-check.md](new%20features/11-orchestrator-and-self-check.md).

**29. The routing fallback path is unexercised.** The router measured **18/18** across six
probes and reached all five specialists, with zero fallbacks — so `trigger="fallback"` is
reasoned-about rather than observed. It is reached only by an exception or by a model naming
a slug the owner disabled, and both are forced down the same branch as the agent's own
prompt. Nothing has run it. A probe that *makes* routing fail is the missing measurement.

**30. Self-check is unmeasured against a golden set, and it must not be measured with
faithfulness.** The critic exempts labelled analogies, questions to the learner and
"the material does not cover X", because open item 20 records faithfulness scoring exactly
those as unsupported and then advising their deletion. That makes Ragas the wrong instrument
for the thing this feature does — scoring the self-check with the metric it was built to
disagree with would confirm whichever one ran last. What it needs is a trajectory measure
(open item 23, Stage 4), not a faithfulness-shaped one.

**31. Routing costs ~222 ms and the orchestrator's `chunk_size` cannot be routed.** Measured
n=9: rewrite alone 1,211 ms, router alone 1,368 ms, both under `asyncio.gather` 1,433 ms — so
concurrency holds and the marginal cost is small against a 6.3 s persona turn. The unresolved
half is granularity: `quiz-generator` is seeded at `chunk_size=500` and an `adaptive-tutor`
corpus is ingested at 800, so a routed quiz turn gets the right *breadth* and the wrong
*chunking*. Fixing it properly means indexing one corpus at two granularities, which is the
same shape as open item 17 and should be decided with it.

**32. `queries` still has no column for any of this.** `specialist`, `route_trigger` and
`self_check_verdict` live only in `trace_events.payload` and are replayed with
`.get`-and-default, exactly as `tool_steps` is. That was the right call for shipping — JSONB,
no CHECK, no migration — and it means none of the three is queryable in aggregate. The first
question anyone asks of this feature ("which specialist does this agent actually get routed
to?") needs a JSONB scan or a column.

### Opened 2026-08-17 by the robust-handouts build

Full record in
[new features/12-robust-handouts/PLAN.md](new%20features/12-robust-handouts/PLAN.md), whose §8
carries the measurements.

**33. The handout brief never goes through the question rewriter.** `gather_material` calls
`aretrieve` directly, so `contextualize_question` — which `REWRITE_EVERY_TURN=true` exists to run
on every turn precisely because typos and shorthand reaching the embedder unrepaired degrade
retrieval — is never reached from the handout path. Wiring it is a few lines. **A brief is not a
question**, the rewriter's prompt is measured on questions, and its failure mode is silent
(`contextualize_question` swallows every exception and degrades to Stage 1). So it needs its own
measurement before it needs code.

**34. A hung handout job leaves a row at `pending` forever.** `_settle` runs in the job's own
`finally`, so a crash, a cancellation and an early return all reach it; a job that never returns
does not, and there is no sweeper. Reproduced accidentally on 2026-08-17 — a wedged deck
generation sat at `pending` for 26 minutes until the process was killed. The user can delete the
row, which is the only escape hatch. A sweeper needs a "started_at" notion the table does not
have; `created_at` is enqueue time.

**35. The deck outline does not reach the tool door.** `api/ask.py` never sets `preview_text`
when persisting a `run_python` artefact, so a deck asked for **in chat** has no preview while the
same deck made from the panel button does. Feature 06 closed the validation half of that
asymmetry and this half was not in its scope. It is the third time this door has needed the same
thing separately, which is itself the finding.

**36. Nothing measures whether a WIDER retrieval budget makes a BETTER deck, only a more
grounded-looking one.** Feature 03 is justified on citation density (52.9% → 75.1% pooled, and
two-in-five catastrophically ungrounded decks going to zero), which is a proxy: a citation can be
present and wrong. The honest measure is faithfulness against a golden set, and open item 20
records faithfulness scoring a teaching persona's own pedagogy as unsupported — so the instrument
needs choosing before the measurement, exactly as in item 30.

**37. The true bullet-overflow point of a slide is unmeasured and unmeasurable here.**
`handout_deck_max_bullet_chars = 400` bounds the model's *observed* behaviour (max seen 235), not
the geometry. Knowing where text actually overflows a placeholder means rendering the deck, which
needs LibreOffice or a font stack the sandbox deliberately lacks — and `fit_text`, the obvious
shortcut, raises `OSError` on Linux. So the threshold must be re-read whenever the retrieval
budget moves, because that is what moves bullet length.

### Opened 2026-08-17 by the object-storage change set

Full record in
[new features/13-object-storage/PLAN.md](new%20features/13-object-storage/PLAN.md).

**38. Re-chunking is now possible and is not built.** `documents.storage_key` holds the
original, so the bytes a new split would need exist for the first time. Nothing reads that key
back: `chunk_size` is still read once at ingest (`rag/ingest.py`), there is still no re-ingest
route, and `AgentSettingsSheet`'s "Takes effect on the next upload" is still true. Two things
want deciding before a button exists. It inherits **open item 18 in full** — replacing a
document's chunks destroys the `query_chunks` rows of every earlier answer, so re-chunking
would silently invalidate eval history exactly as deletion does. And **`ingestion_runs` records
no `splitter`** (item 40), so two runs differing only in splitter are indistinguishable on the
columns that matter.

**39. `handouts.content` is still written on the R2 road.** Every handout's bytes go to both
stores, which is what makes the rollback real and is pure waste once R2 is trusted. Dropping
the column is a separate change set, and it must not be done casually: `agentic_check.py` S11
asserts the string `handouts.content` appears in no SQL from the list route, and that assertion
becomes **unfalsifiable** the moment the column is gone — passing forever while measuring
nothing. `S11b` was added alongside it to assert the positive property (the list route makes
zero object-storage calls), and `storage_check.py` case 76 goes red if the column disappears
while S11 still greps for it. Drop the column, then delete S11 and case 76 together.

**40. `ingestion_runs` records `chunk_size` and `chunk_overlap` but not `splitter`.** So a
chunk-size experiment is already not fully reproducible from the row that exists to record it —
a pre-existing gap, surfaced by the re-chunk audit rather than caused by it. One nullable
column.

**41. The R2 API token expires 2027-08-17.** Every download will 403 simultaneously while the
application is provably unchanged and every offline harness stays green — a failure that cannot
report itself, the same shape as `EMBEDDING_ROUTE`. Mitigated only by legibility: the download
route answers **503 naming object storage** rather than 500, and `create_r2_bucket.py` prints
the expiry on every run. There is no renewal reminder.

**42. `Cache-Control: private, no-store` cannot be reproduced on the R2 road.** The old route
sent it with every download; a presigned URL **is** the capability, so the only remaining
control is its five-minute lifetime (`r2_presign_ttl_s`). The URL lands in browser history.
Recorded as an accepted loss rather than solved.

**43. The R2 token is account-wide, and the account is shared.** It can reach
`mindfulspeak-uploads`, which belongs to an unrelated project. Nothing in this codebase names
another bucket — `settings.r2_bucket` is the only one any call site uses — so this is blast
radius rather than a live path. A bucket-scoped token is the fix.

### Opened 2026-08-20 by the admin-observability change set

Full record in
[new features/14-admin-observability/PLAN.md](new%20features/14-admin-observability/PLAN.md),
whose §8 carries the two places the plan was wrong.

**44. `queries.prompt_tokens` / `completion_tokens` are a CACHE that can disagree with
`api_usage`, and nothing detects the disagreement.** `api_usage` is the source of truth — one
row per call — and the two `queries` columns are a denormalised SUM over that turn's rows,
written in the same transaction. They exist for the per-turn UI and for cheap sorting. If a
metering write fails, the answer still returns (R1: accounting never breaks a turn) and the
two numbers diverge silently. The admin console reads `api_usage` and never the cached
columns, so the console stays right; a per-turn chip reading the cache can be wrong. No
reconciliation job exists.

**45. The 76 queries that predate 2026-08-20 are unbackfillable, and that is permanent.**
OpenRouter's `GET /generation?id=` would return the cost of any past call, but the `gen-…` id
was never stored, so there is nothing to look up. `api_usage.generation_id` exists so this is
true only of the past. Those rows render as **not measured** rather than as zero — every admin
aggregate carries `measured_count` / `total_count` beside it for exactly this reason, and a
lifetime spend total is a **lower bound**, not a total.

**46. Rerank spend is unpriced by default and therefore invisible in dollars.** Cohere is the
only provider that reports **units** (`meta.billed_units.search_units`, measured 1.0 per call)
and not cost. The units are recorded as a measurement; the dollar figure is opt-in via
`COHERE_SEARCH_UNIT_USD`, defaults to off, and lands in `estimated_cost` — never in
`cost_usd`, because a guess must not sit in the reported column. Off by default because a
hardcoded price is a number nobody re-checks, and this repo already has that failure on this
exact provider. Consequence: a turn's *reported* cost omits three rerank calls.

**47. Metering coverage is now asserted, but only against a MISSING SCOPE — not a missing
collection.** `metering_check.py` case 12 walks the application's own call graph and fails
when an entry point reaches `build_chat_model` outside a `meter_as`. It found the one instance
that existed (the golden-set drafter). It would **not** catch a call site that opens
`meter_as` and forgets `collect_usage()`: `emit_record` returns `False` with no active
collection, so the record is logged and never written, and the scope check passes. The two
contextvars are independent by design — that is what makes nesting correct — and "metered" is
therefore two things a call site must remember, with a harness for one of them. See §8.2 of
the change set plan for why the register predicted the wrong tell.

**48. "What did this eval run cost" is not a question the console answers directly, and the
obvious way to ask it undercounts.** `/api/admin/eval-runs` returns no spend. Judge calls are
`call_kind='judge'`, attributed to the run's agent with **no `query_id`** — but a ten-question
run also drives ten real turns through `run_turn`, which records them as ordinary
`call_kind='generation'` rows indistinguishable from a user's own questions except by
timestamp. So `/api/admin/spend?group_by=call_kind` filtered to `judge` is the *judging* cost,
not the run's. The run's true cost is judge + goldenset + those ten turns, and no join
expresses it.

**49. `/api/admin/account` cannot reconcile exactly, by construction.** It is the only
external check that our per-call sum matches what OpenRouter billed, and it must stay. But
OpenRouter reports per **key** and per **account**, and this account's key also serves work
that is not Groundwork — measured 2026-08-20 at `total_usage: 66.61` account-wide against
`usage_monthly: 0.83` on this key. The console says so rather than hiding it. What the check
actually establishes is that **ours is not zero while theirs moves**, which is R5's tell, not
an audit.

**50. Nothing prevents the database from being AHEAD of the deployed code, and a health check
cannot see it.** Surfaced 2026-08-23 by this change set's own merge. `f6b28d4c1a73` was applied
to production during the build, per [build.md](new%20features/build.md) §8's
migration-before-merge rule; for the three days until the merge, the deployed code did not
contain that revision file, so the start command

```
alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

exited non-zero with `Can't locate revision identified by 'f6b28d4c1a73'`. The backend failed
**three consecutive deploys** — one Render initiated itself (`trigger: deployed_by_render`),
two manual retries — and had last deployed successfully on 2026-08-17. Merging the branch fixed
it on the first attempt, because the merge shipped the file.

Two properties make this worth a numbered item rather than a note. **`/api/health` returned 200
throughout**, because the old instance keeps serving while the new one crash-loops — so
uptime monitoring, the frontend and every external signal read healthy while the service was
undeployable. And **the failure is invisible until something restarts**, which is a schedule
Render owns, not us. The rule that would prevent it (keep the window short) is discipline, not
a guard; a real guard would be a start command that distinguishes "the DB is ahead" from "the
DB is behind" and refuses only the second, or a pre-deploy check comparing
`alembic heads` against `alembic current`. Neither exists.

---

## 11. Out of scope

Acknowledged as real production concerns, deliberately not built:

- Indirect prompt injection defenses (the five-layer model in the deck) — awareness only
- ~~Multi-turn conversational memory~~ — **built 2026-08-15.** Scope changed deliberately:
  the chat interface made single-shot turns untenable. `conversations` threads the `queries`
  rows, and follow-ups are contextualised against history before embedding, because a
  question like "what is its power budget?" carries its subject in a pronoun and retrieves
  nothing when embedded raw. That rewrite shares machinery with §3.5's Stage 2 rewrite loop —
  different trigger (coreference vs. low score), same mechanism — and Stage 2 should compose
  with it rather than add a second rewriter.
- Semantic caching, model routing, token budgets
- Retries, timeouts, and fallbacks on external calls
- Human-in-the-loop approval gates
- Graph RAG / entity extraction
- Background knowledge agents (corpus freshness monitoring)
