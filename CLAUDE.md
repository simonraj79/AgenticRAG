# CLAUDE.md — working notes

Conventions, insights and hard-won gotchas for this repo. **[PRD.md](PRD.md) is the
specification**; this file is the operational companion — the things that cost debugging
time and would cost it again. **[EVAL.md](EVAL.md) is the operator's guide to Stage 3**:
every setting and per-agent parameter in tables, how to run an evaluation, and how to read
a scorecard without being misled by it. What stays here is the *debugging* half — the
symptoms, and which knob caused them.

**[new features/loop.md](new%20features/loop.md) is the design pattern for anything the
MODEL decides** — a tool, a retry, a self-correction. Read it *before* writing the feature,
not after it fails to fire: its central finding is that binding a tool is the easy twenty
lines and the model then declines to call it, so the work is designing a trigger. §3 below
carries the measurement; that file carries the pattern and a checklist.
**[new features/loop-prompt.md](new%20features/loop-prompt.md)** is its companion — the
prompt structure for opening such a session, which front-loads the four questions that are
cheap on paper and expensive once the loop is built.

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
| **Frontend unit tests** | `cd frontend && npm test` |
| Provision Pinecone | `python scripts/create_index.py [--dry-run]` |
| Provision Postgres | `python scripts/create_render_db.py [--dry-run]` |
| RAG end-to-end check | `backend/.venv/Scripts/python.exe scripts/slice_check.py` |
| Tear down that check | `backend/.venv/Scripts/python.exe scripts/slice_check.py --cleanup` |
| **Sandbox check** (no DB, seconds) | `backend/.venv/Scripts/python.exe scripts/sandbox_check.py` |
| **Citation-marker contract** (no DB) | `backend/.venv/Scripts/python.exe scripts/ledger_check.py` |
| **Refusal/gap detectors** (no DB, no model) | `backend/.venv/Scripts/python.exe scripts/refusal_check.py` |
| **OpenRouter request body** (no network) | `backend/.venv/Scripts/python.exe scripts/llm_check.py` |
| **Agent loop + Handouts, end to end** | `backend/.venv/Scripts/python.exe scripts/agentic_check.py --setup` then `--run`, then **`--cleanup`** |
| **Layout assertions** (needs both servers) | `python scripts/ui_check.py` — **global** interpreter, not the venv |
| **Why is the DB unreachable?** | `curl -s https://api.ipify.org` — compare against the allow-list |
| **Did `pip freeze` break the build?** | `grep -n pywin32 backend/requirements.txt` — the marker must survive |
| **Which Pinecone namespaces exist?** | see "namespace counts" under *Embeddings and Pinecone* |
| **Why does every model call 404?** | a parameter no provider advertises, **not** a bad model id — see *OpenRouter* |

Those four are diagnostics for failures this project has hit repeatedly — the first two
**twice each**, the 404 **three times**, each time via a different parameter. Run them
before reading a traceback, not after.

**Local dev needs `DEV_AUTH_ENABLED=true` and `ENVIRONMENT=development` in `.env`** to use
the dev-login shim; both are absent from `.env.example` on purpose. See the Google OAuth
section for why that route is gated three ways.

---

## Configuration, by the failure it causes

Full reference with every default is in **[EVAL.md §4](EVAL.md)**. This table is the
reverse index: a symptom, and the setting behind it. Defaults are in
[`backend/app/config.py`](backend/app/config.py) — every one of them carries the
measurement that chose it.

| Setting | Default | Get it wrong and… |
|---|---|---|
| `OPENROUTER_API_KEY` | — | Every chat model fails. Generation, rewrite, golden set, judge |
| `GEMINI_API_KEY` | — | **Retrieval** fails, not generation — it is the EMBEDDING key now. Looks nothing like a missing model key |
| `OPENROUTER_REQUIRE_PARAMETERS` | `true` | Off: a `function_calling` request silently loses `tools` and returns prose. On: an unadvertised parameter 404s the call. **Leave it on** and fix the parameter |
| `GENERATION_MODEL` etc. | `author/model` | A bare id 404s naming a model that plainly exists — a namespace error that reads like an outage |
| `GENERATION_MODEL` | `deepseek/…-v4-flash-0731` | Point it at Gemma and the model stops initiating searches — **the gap trigger becomes load-bearing again**. Not a fault; `agentic_check.py` S13 pins it |
| `GENERATION_REASONING` | `false` | On: 60–79% of billed output becomes thinking, and handouts drop to 5/6 first-attempt files at 3.7× the latency. **Off is only safe while `TOOL_GUIDANCE`'s last paragraph survives** — see S16 |
| `GENERATION_TOP_K` | `64` | Sent to the Gemini family: no eligible provider, 404. Sent to DeepSeek: **no error at all**, and it silently routes around the only cached endpoint. `build_chat_model` drops it for `_NO_TOP_K_PREFIXES` |
| `GENERATION_MAX_TOKENS` | `2048` | Passed as `ChatOpenAI(max_tokens=…)` it is renamed to `max_completion_tokens`, which OpenRouter honours but does not advertise → 404. Must go via `extra_body` |
| `RAGAS_JUDGE_MODEL` | `google/gemini-3.7-flash` | Set to `GENERATION_MODEL` and the run is self-assessment; Gemma scored a verbatim-from-context answer **0.000** |
| `RAGAS_JUDGE_REASONING_EFFORT` | `low` | Thinking is **mandatory** on Flash — raising this buys latency back, which is what leaving Gemma was for |
| `GOLDEN_SET_MODEL` | `google/gemini-3.7-flash` | Currently equals the judge, so context precision/recall are graded against references the judge wrote |
| `RAGAS_MAX_CONCURRENCY` | `2` | Greedy gives a scorecard full of nulls that looks like a broken judge |
| `EMBEDDING_MODEL` / `_DIMENSION` | `gemini-embedding-2` / `768` | **Part of the index.** Changing either means deleting the index and re-ingesting |
| `score_threshold` (per agent) | `0.5` | Governs *rewriting*, **not refusing**. Not a safety control |

Per-agent retrieval parameters (`retrieve_k`, `rerank_top_n`, `chunk_size`, …) live on the
`agents` row, not in the environment — **[EVAL.md §5](EVAL.md)** maps each to the metric it
moves.

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
- **Anything the model decides goes through the pattern in
  [new features/loop.md](new%20features/loop.md) — read it first.** The trigger is not "I am
  adding a tool"; it is any of these, and each one has already been got wrong here once:
  - adding or changing a **tool** (`app/tools/`), or the loop in `app/rag/agent_loop.py`
  - a **retry** that depends on inspecting what the model produced — the Handouts job is
    one, and it initially retried on the wrong condition
  - a **detector over model output** (`app/rag/refusal.py`). Ask what a false positive costs
    versus a false negative *before* choosing strictness; they are rarely symmetric, and
    that asymmetry is why one marker list feeds two functions
  - deciding **whether a threshold should branch**. It usually should not: `score_threshold`
    is advisory precisely because the measured bands overlap
  - writing a **scenario that proves an agentic feature works**. It must make the feature
    necessary, or it passes without exercising anything

  The one-line version, if you read nothing else: **trigger on the absence of the outcome
  you wanted, never on the presence of an error.** The error-shaped test passes while the
  thing you wanted silently did not happen — it has cost this project two bugs in two
  different modules.
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

This is not hypothetical: it has now been flattened and restored **three times**, most
recently by the freeze that added `matplotlib` and `python-pptx` for the code interpreter.
Three occurrences in three unrelated dependency additions is the whole argument: it is not
something to remember, it is a property of `pip freeze` that fires every time. Treat
"re-add the marker" as the second half of the `pip freeze`
command, not as a thing to remember:

```bash
grep -n 'pywin32' backend/requirements.txt
```

### OpenRouter

**Every chat model goes through OpenRouter; embeddings deliberately do not.**
`app/rag/llm.py` is the only place a chat model is constructed — generation, the rewrite
decision, the golden-set generator and the Ragas judge all pass through it, the same way
`retriever.py` is the only place the retriever is built. Embeddings stay on
`langchain-google-genai` because the Pinecone index was written in `gemini-embedding-2`'s
space and OpenRouter serves no embedding model at all; moving them would force a full
re-ingest to gain nothing. **So both `OPENROUTER_API_KEY` and `GEMINI_API_KEY` are
required**, and Ragas now draws its judge LLM and its embedding model from two different
providers.

No new dependency. `ChatOpenAI` is an OpenAI-*protocol* client pointed at
`openrouter.ai`; `langchain-openai` was already installed as a `langchain-pinecone`
transitive. It finally earns its place — but the note above that "nothing calls OpenAI and
no `OPENAI_API_KEY` is needed" still holds.

**`provider.require_parameters` must be ON, and turning it on breaks two things that then
have to be fixed.** OpenRouter's default is to *silently drop* any parameter the routed
provider does not support. That is fatal for structured output: of the 18 endpoints serving
`google/gemma-4-31b-it`, a DeepInfra tier and a Together tier advertise no
`tools`/`tool_choice`, so a `function_calling` request routed there loses both fields and
comes back as prose — the same failure shape as a Gemma markdown fence, arriving from a
completely different direction. `{"provider": {"require_parameters": True}}` converts the
silent drop into a routing constraint.

The cost is that **every** parameter in the request must be one some provider advertises,
and two of them are injected by langchain-openai without being asked for. Both produce the
identical, unhelpful error:

```
404 No endpoints found that can handle the requested parameters
```

| Injected by | What it sends | Fix |
|---|---|---|
| `ChatOpenAI(max_tokens=…)` | renames to `max_completion_tokens` **unconditionally** — no flag | send `max_tokens` via `extra_body` |
| `with_structured_output(method="function_calling")` | also binds `parallel_tool_calls: False` (base.py:2514) | `disabled_params={"parallel_tool_calls": None}` |

**The `max_completion_tokens` case is the instructive one, and the obvious diagnosis is
wrong.** OpenRouter *honours* it — verified: `max_completion_tokens=10` stopped at exactly
10 tokens with `finish_reason=length`. It simply is not *advertised* in any provider's
`supported_parameters`. So routing and execution consult different sources of truth and
only the first is strict: a 404 on a working model id, caused by a parameter that works,
because of the name it was sent under. Do not go looking for a dropped cap.

`disabled_params` is langchain-openai's own answer to the second one — its docstring says a
disabled parameter "will not be used by default in any methods, e.g. in
`with_structured_output`". Reaching for `bind_tools` plus a parser instead would work and
would cost the canonical class.

**`stream` is absent from every provider's `supported_parameters` and streaming works
anyway. This is the INVERSE of the `max_completion_tokens` trap, and the two together are
the rule.** Checked 2026-08-16 across all 19 endpoints serving `google/gemma-4-31b-it`: not
one advertises `stream`. Under `require_parameters` that reads like a guaranteed 404, and it
is not — measured on this repo, `astream()` returned 12 chunks with the first at **0.50 s**,
*with `search_corpus` bound* and `top_k` in `extra_body`:

| Probe | Result |
|---|---|
| `astream`, no tools | 12 chunks, first token 0.83 s |
| `astream`, `bind_tools([search_corpus])` | 12 chunks, first token **0.50 s** |

The reason is the distinction worth keeping: **`supported_parameters` describes the sampling
surface, and routing consults only that.** `stream` is a transport flag on the response body,
so it never enters the routing filter — whereas `max_completion_tokens` *is* a sampling
parameter, is not advertised, and therefore 404s while being honoured when it arrives.

So the rule is not "an unadvertised parameter 404s". It is: **an unadvertised parameter that
routing consults 404s. Check by probing, not by reading the list** — the list is silent in
both directions, and it is silent for opposite reasons.

**`reasoning: {"enabled": false}` is a hard 400 on the Gemini family, and this is the third
distinct way a parameter can be wrong here.** The first two were "unadvertised and 404s at
routing" (`max_completion_tokens`) and "unadvertised and works anyway" (`stream`). This is a
third: **advertised, routed, and then rejected at execution**. Every Gemini endpoint lists
`reasoning` in `supported_parameters`, so routing succeeds; the provider then answers

```
Reasoning is mandatory for this endpoint and cannot be disabled.
```

because `reasoning.mandatory` is true for that family — the same property
`RAGAS_JUDGE_REASONING_EFFORT` exists to work around, where thinking can only be turned
*down*. `build_chat_model` withholds the flag for `_REASONING_ALWAYS_ON_PREFIXES` and drops
it rather than raising: the caller means "do not spend tokens thinking", and on a model that
cannot comply the honest outcome is the model's default, not a failed turn.

**The rule this completes:** `supported_parameters` tells you what will *route*, never what
will *execute*. Probe both. Three traps, three different mechanisms, and only one of them
produces the error you would predict from reading the list.

**`_LEGACY_SLUGS` mapped a bare id to a model that does not exist.**
`gemini-flash-lite-latest` → `google/gemini-3.7-flash-lite`, which OpenRouter answers with
`"is not a valid model ID"`. A guard whose whole purpose is to stop a bare id 404ing, and
which mapped to a 400, is worse than no entry — the unmapped path at least warns and names
its guess. Removed; `llm_check.py` case 25 now asserts every target contains a `/`.

**`top_k` can only travel in `extra_body`.** It is not an OpenAI-API parameter, so
`ChatOpenAI` has no field for it. This is load-bearing rather than tidy: Gemma 4's card
gives `temperature=1.0, top_p=0.95, top_k=64` as *one* configuration, and sending
two-thirds of it runs the model outside its calibration while looking correctly configured
in the code. Combined with `require_parameters`, a provider that cannot do `top_k` is now
routed around rather than quietly handed a partial config.

**And `top_k` must NOT be sent to the Gemini family at all** — it appears nowhere in
`google/gemini-3.7-flash`'s parameter list, so under `require_parameters` it leaves zero
eligible providers and 404s. That is not hypothetical: it is how the golden-set generator
broke the first time it was pointed at Flash, having inherited Gemma's card values from the
generation defaults. `build_chat_model` drops `top_k` for `_NO_TOP_K_PREFIXES` rather than
making every caller remember, and dropping it is *correct* rather than merely expedient —
`top_k` matters because Gemma's card gives it as part of one configuration, and a model
outside that family has no such configuration to honour.

**`n` is not available on this route, so `ResponseRelevancy(strictness=1)` stays.** The
earlier note said to raise it against a judge supporting multiple candidates, "which Gemini
Flash does" — true of the Gemini API, false here: `n` appears in
`google/gemini-3.7-flash`'s OpenRouter parameter list nowhere, so `candidate_count=3` has
no eligible provider. Changing the judge did not unlock this; changing the gateway closed
it.

**Model ids are `author/model`.** A bare `gemma-4-31b-it` returns a 404 naming a model that
plainly exists, which reads like an outage rather than a namespace. `agents.generation_model`
is free text an operator can type into, so `openrouter_slug()` maps the known legacy ids and
**warns** rather than guessing silently.

**Diagnostics.** `mcp__openrouter__list-model-endpoints` is the authority on which providers
serve a model and what each advertises — the per-model `supported_parameters` is a *union*
across providers and will tell you a parameter is supported when the endpoint you land on
does not have it. Check endpoints, not the model.

**Tool calling narrows routing, and the margin is worth knowing before you widen the
request.** `require_parameters: true` means the request is only routable to providers
advertising **every** parameter it carries, and this request already sends `top_k` through
`extra_body`. So adding `tools`/`tool_choice` routes on the *intersection*. Measured
2026-08-16 across the 19 endpoints serving `google/gemma-4-31b-it`:

| | advertises `tools` | does not |
|---|---|---|
| **advertises `top_k`** | **14 — eligible** | 2 (DeepInfra turbo, one Together tier) |
| **does not** | 3 (OpenInference, Morph, Cerebras) | — |

Fourteen eligible endpoints, several at 99–100% uptime, so tool calling has real headroom —
**and one more unadvertised parameter could empty it.** That is the number to re-check
before adding anything to a tool-bound request, not after a 404. Verified in the same pass
that `bind_tools(tools)` puts exactly one key in the request and that
`bind_tools(tools, tool_choice="none")` adds exactly one more; `disabled_params` keeps
`parallel_tool_calls` out of both.

### The agent loop

> **READ THIS FIRST, 2026-08-16: the default generation model is now
> `deepseek/deepseek-v4-flash-0731`, and it INVERTS the finding below.** It self-initiates
> a search **6/6** on the identical probe where Gemma scored 0, and it honours
> `tool_choice="any"`, which Gemma silently ignored. Everything in this section is still
> true *of Gemma*, and Gemma is still one `agents.generation_model` write away — which is
> exactly why none of the trigger machinery was deleted. The full inversion, the four new
> scenarios and the two new layer-1 harnesses are in
> **[new features/09-deepseek-agentic.md](new%20features/09-deepseek-agentic.md)**.
>
> Three consequences that are *operational* rather than historical:
>
> - **The gap trigger now also requires that no search has run this turn**
>   (`corpus_searched`). Without that gate, a self-initiating model earned a redundant
>   forced search on *every correct refusal*. The trigger asks "did the model search
>   before declining", and a search having already run means the answer is yes.
> - **Reasoning is off, and that is only affordable because a Gemma-era prompt paragraph
>   survives.** The two are redundant with each other: either alone holds tool use at 6/6,
>   removing both drops it to 2/6, and nothing raises. `TOOL_GUIDANCE`'s final paragraph
>   looks like dead weight from a superseded model and is not. S16 asserts the disjunction.
> - **The retrieval budget quietly doubled.** The new model emits 1.50–2.00 search calls
>   per step and `max_tool_steps` bounds steps, not calls. Three steps can now be six
>   retrievals — measured in the browser at `tool_steps=3, tool_calls=6`. Both numbers
>   are recorded on the GENERATE payload now, and the turn chip renders the larger.
> - **A model's own tool-call markup can arrive in the CONTENT channel, and the user
>   reads it.** On a budget-exhausted turn the loop re-invokes with `tool_choice="none"`;
>   DeepSeek still wanted to search and said so in text —
>   `<｜DSML｜tool_calls> <｜DSML｜invoke name="search_corpus">`. The delimiter is **U+FF5C
>   FULLWIDTH VERTICAL LINE, not ASCII `|`**, which is exactly why it survives provider-side
>   parsers. Stripped in `_message_text`, and separately gated during streaming, because
>   tokens are on screen before the strip runs. **Every harness was green; it took opening
>   the page.** If a new model is introduced, read one budget-exhausted answer by eye.

**Gemma 4 will not initiate a search on its own judgement, and no amount of prompting
changes that.** This is the single most important finding of the agentic build, because
every plausible fix except the right one is a prompt edit.

Measured 2026-08-16, `google/gemma-4-31b-it` with one chunk of context, a two-part question,
and `search_corpus` bound:

| Configuration | Result |
|---|---|
| `tool_choice="auto"`, full persona prompt | **no tool call** — answered half, declared the rest missing |
| Bare prompt with no grounding rule at all | **no tool call** |
| `"You MUST call search_corpus for any part not covered"` | **no tool call** |
| `tool_choice="search_corpus"` (named) | called it, correctly |
| `tool_choice="any"` / required | **no tool call** |

Two separate things are in that table.

**First, `tool_choice="any"` is silently ineffective on this route.** Only a *named* tool
forces a call. That is the same shape as every other OpenRouter trap here — a parameter that
is accepted and not honoured — and it is worse than a 404, because a dropped "required"
looks exactly like a model that considered the tools and declined.

**Second, and structurally: a refusal-first system prompt suppresses tool use.** Every
prompt in this project states the grounding rule before it establishes voice, and that is
deliberate — CLAUDE.md's own measurement is that *refusal comes from the prompt, not the
threshold*, and it is the reason the product can be trusted to say "I don't know". The cost
is that a model drilled to treat a gap in its context as a cue to **decline** will do exactly
that when handed a tool for **filling** gaps. The two instructions compete and the earlier,
more forceful one wins. Weakening the grounding rule would trade a hallucination-free system
for a tool-happy one, which is the wrong trade.

**So the loop uses a trigger, not a prompt.** When the model returns an answer with no tool
call, `detect_gap` scans it for the same phrases that write `queries.refused`; if one is
present, a step remains, and the trigger has not already fired this turn, the loop appends a
nudge and re-invokes with `tool_choice="search_corpus"` — a **named** tool, per the table
above. The model then answers with both halves.

Three things fall out of that which are worth keeping:

- **This is PRD open item 7, delivered with a trigger that works.** The specified loop
  compares `top_score` to `score_threshold`; the calibration measurement kills it, because
  on-topic questions scored 0.61–0.67 and off-topic 0.49–0.58 and 0.5 sits *inside* the
  overlap. "The model said it does not know" is read off the text rather than off a
  distribution.
- **The refusal detector is now load-bearing twice** — it measures refusals *and* drives the
  search. That raises the cost of the marker-list gap documented above, and is the reason
  the markers moved to `app/rag/refusal.py`: `agent_loop` cannot import from `app.api`, and a
  second copy of a list already wrong three times is the outcome worth ruling out
  structurally.
- **`detect_gap` is deliberately not `detect_refusal`.** Refusal is position-sensitive,
  because a caveat after a real answer must not be scored as a decline. A gap is any
  admission anywhere, because the turn that most needs a search is exactly the one that
  answered half and gave up on the rest — where every position rule is designed to look
  away. Feeding a retry, a false positive costs one retrieval; a false negative costs the
  whole behaviour.

A worthwhile side effect: it makes refusals *stronger*. "I searched and it is not there"
beats "it was not in the chunk I happened to be given".

### The code-interpreter sandbox

Full contract in `new features/02-code-interpreter.md` §5. Two things belong here because
they cost debugging time and would cost it again.

**An import allowlist does not stop an allowed library handing you a blocked module.** The
static check reads `import` statements; it saw nothing wrong with either of these:

```python
import matplotlib
matplotlib.os.environ            # matplotlib imported os for its own use; the binding is public
import numpy
numpy.ctypeslib.ctypes           # worse -- ctypes reaches native code, not just the filesystem
```

`matplotlib` is allowlisted, `os` is never imported by the user's source, and `os` is not a
dunder, so the import rule, the name denylist and the dunder rule all passed it. Blocked now
by `DENIED_MODULE_ATTRS`, applied to **attribute access** rather than only to imports, with
regression cases 13–15 in `scripts/sandbox_check.py`.

**The instructive part is that neither probe actually leaked anything.**
`matplotlib.os.environ.get("OPENROUTER_API_KEY")` returned `None`, because the child is
spawned with `env=_minimal_env()` — `PATH`, `TEMP`, `LANG`, `MPLBACKEND` and nothing else.
The cheap layer failed and the strong one held. **The strongest control in the sandbox is
not a limit, it is the empty environment**, and any change that starts passing variables
through to the child removes more protection than any change to the allowlist could.

Case 15 exists for the opposite failure and is the one likelier to bite over time: a
denylist wide enough to block `matplotlib.os` must not block `matplotlib.pyplot`. Without
that test the safe-looking response to any future scare is to keep adding names until the
tool quietly stops working.

**`RLIMIT_NPROC` cannot be set to zero on Linux, and the reason is not obvious.** A *thread*
counts as a task against it, so a limit of "current usage" kills numpy/OpenBLAS building its
thread pool — on production Linux only, invisibly from a Windows dev box where `resource`
does not import at all. It is set to current + 64: a fork bomb wants thousands, so the
ceiling still binds. **Windows development is measurably less protected than production**,
which is the reverse of the usual arrangement.

### Gemma 4 on the Gemini API

Measured before the move to OpenRouter. The structured-output finding survived it; the
latency numbers did not, and the judge findings were superseded outright — see the
OpenRouter and Ragas sections.

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

**`refusal_pass = 0 / 2` was THREE-QUARTERS a detector bug, and the scorecard blamed the
agent for all of it.** Feynman Explainer, 2026-08-15, both runs — four refusal rows in total.
An earlier version of this note said "half", and described the second row as answer-then-caveat
*in both runs*. Replaying `_detect_refusal` over the stored answers shows that was a
conflation: only run 2's second row was. Read back from the database:

| Run | Answer | Verdict |
|---|---|---|
| 1 & 2 | *"The provided text does not say which of the fourteen launches took place in 2040 [1]."* | **A perfect refusal, scored as a failure** |
| 1 | *"The provided text does not cover the specific duties of the eleven permanent crew members [1]."* | **Also a perfect refusal, also scored as a failure** |
| 2 | two sentences of real content, *then* "…but it does not cover their specific duties" | Genuinely an answer — the persona |

Three of the four were false negatives in `_detect_refusal`: **neither `"does not say"` nor
`"does not cover"` was in either marker tier.** The agent did exactly the right thing and the
measurement called it wrong — the same failure class as the `strictness=3` bug below, and
worse than a crash for the same reason: **the scorecard still renders, and points confidently
at the wrong thing.**

**Both phrases belong in `CAVEAT_MARKERS`, not `REFUSAL_MARKERS`, and that resolves a
dilemma this file previously recorded as a real trade-off.** The old note warned that adding
`"does not cover"` "would score this row as a refusal and quietly delete the finding" — true
of the hard tier, which matches anywhere in the 200-character lead, and false of the caveat
tier, which only counts before the model has answered anything. Position separates the two
turns cleanly, and it was already built to:

```
run 1   "The provided text does not cover the specific duties…"   consumed=0     -> refusal
run 2   "…states there are eleven crew [1]. It also mentions…
         but it does not cover their specific duties."            consumed=198   -> answer
```

So the fix costs nothing: three rows flip to passing, the persona finding survives untouched,
and five regression cases (including "answer first, caveat late") still read as answers. Put a
new phrase in the hard tier only if a model would *never* say it while answering — `"does not
say"` and `"does not cover"` both fail that test, which is exactly why they go in the soft one.

The fourth row is real, and is the tension worth keeping: the Feynman persona is *designed* to
**name the gap** rather than decline, which is pedagogically right and structurally an answer.
That is PRD open item 16, and it is a finding about the persona, not about the detector.

**Confirmed by run 3**: `refusal_pass` went `0 / 2` → `1 / 2` with no change to the agent.
The row that flipped is the detector fix; the row that did not is the persona, and it is now
the only one left. That is what a refusal metric should look like — measuring the agent
rather than the marker list.

The lesson generalises past this one bug: **a refusal metric measures the detector and the
agent at once, and the two failures look identical on the card.** Before acting on a low
`refusal_pass`, read the answers. The plain `lecture-qa` template still has **not** been
tested here, so nothing above is a finding about the system prompt in general.

**It happened a third time, and the third time is the one that should change the approach
rather than the list.** `scripts/agentic_check.py` scenario S7, 2026-08-16, on a corpus that
*raises* the modulation scheme and says it is documented elsewhere:

```
"The provided text does not state which modulation and coding scheme the
 Ka-band downlink uses; it notes that modulation..."          refused=False
```

A perfect refusal, scored as a failure, for the third time, on a phrase in neither tier.
Run 1 taught the list `"does not say"`; run 2 taught it `"does not cover"`; this taught it
`"does not state"`. Three independent discoveries of one bug is not three bugs — it is a
list being maintained one observation at a time against a model that has many ways to say
the same thing.

So `"does not state"`, `"does not describe"`, `"does not indicate"` and `"does not detail"`
went in **by pattern, not by observation**. The family is `does not <reporting verb>`, every
member fails the hard-tier test ("would a model never write this while answering?"), and
therefore every member belongs in the position-gated caveat tier where a false positive
costs nothing. Adding the family is free; not adding it costs another eval run
misattributed to the agent.

The generalisation worth keeping: **when a marker list has been wrong three times, stop
adding the string you just saw and add the shape it belongs to.**

**It then happened a fourth and a fifth time, on 2026-08-16 with the DeepSeek swap, and
neither was a missing phrase.** Both are now pinned by `scripts/refusal_check.py`, which is
the real remedy — this list had been corrected four times without ever acquiring a harness.

| # | The answer | Why the marker missed |
|---|---|---|
| 4 | `The material does **not** mention the vendor.` | **Markdown emphasis.** `"does not mention"` was already in the list. Whitespace was normalised; `**` was not. All 34 markers were equally blind |
| 5 | `... are not covered in this briefing.` | **A hard-coded determiner.** The marker was `"not covered in the"`. Truncated to `"not covered in"` |

The fifth is the instructive one, because it is the *inverse* defect: the marker was not
missing, it was **too specific**. So the rule generalises: **a marker should carry the
shape and nothing else** — not a determiner, and not the formatting a model happened to
wrap it in. Fixes at that level are free, because every string the marker matched before,
it still matches.

Two operational consequences. **Both fixes improve `queries.refused` as well as the retry**,
so a `refusal_pass` that rises after this date is partly the measurement being repaired —
EVAL.md §4.2 says so. And **the fifth surfaced as an INTERMITTENT red**: at temperature 1.0
the model picks a different phrasing each run, so S7 passed once and failed the next time
with no code change between. A flaky refusal scenario reads as model variance and gets
re-run. Read the answer before believing that.

**Latency is dominated by generation, not by the cross-Pacific hop.** PRD §6 flags Cohere
as the only Singapore → US round trip. Measured: embed 365 ms, Pinecone k=20 394 ms,
Cohere rerank ~830 ms, **Gemma generation 13.2 s — 89% of the total**. The hop the PRD
worried about costs a twentieth of what generation does. Optimise there or nowhere.

**The Cohere key is now a PRODUCTION key on a paid subscription. The trial limit no longer
binds.** Verified 2026-08-16: twelve consecutive `rerank-v3.5` calls with no 429, where a
trial key fails on the eleventh. `scripts/agentic_check.py` then ran **16 / 16 with zero
`[rate]` rows** — the first clean full pass of that suite.

The history is kept because the *shape* of the failure is the transferable part, and because
a key can be downgraded. It used to be a Trial key at **10 API calls per MINUTE** — not the
1,000-per-month figure `.env.example` quotes, which is the monthly cap and is not what bit.
`agentic_check.py` makes roughly twenty reranking calls (every scenario retrieves, several
retrieve twice, and each of the four handout recipes retrieves to gather its material), so
running the suite twice inside a couple of minutes tripped it.

**A 429 from Cohere surfaces as a handout stuck at `failed` and a scenario throwing —
indistinguishable, on the console, from a code defect.** That was the third instance of one
pattern here: the `strictness=3` metric that silently declined to measure, `METRIC_TIMEOUT_S`
doubling as a quota-retry ceiling so a rate limit and a hang printed the same string, and
that. **Every one made a working system look broken, and each was found by reading the error
rather than the summary line.**

So `[rate]` stays in the harness even though nothing should now produce it. It prints instead
of `[FAIL]` for anything matching a rate-limit phrase and does not exit non-zero — a suite
that goes red because a provider said no teaches its reader to ignore red. Treat such rows as
unmeasured, never as passing. **If `[rate]` rows reappear, suspect the key before the code.**

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

**Render's env vars DRIFT from `.env`, and presence is not correctness.** Found 2026-08-16
by comparing values rather than keys: every required key was present on the service, and
**two of them were different keys from the ones in `.env`**. The Cohere one mattered —
production was running the old **trial** key, rate-limited at ~10 calls/minute, which
CLAUDE.md already records as surfacing as "a handout stuck at `failed` and a scenario
throwing", indistinguishable from a code defect. It had been that way silently.

The API returns env-var *values*, so this is one command and worth running whenever
production behaves unlike local:

```bash
curl -s -H "Authorization: Bearer $RENDER_API_KEY" \
  "https://api.render.com/v1/services/srv-d9vtuhpt0dsc738dmgsg/env-vars?limit=100"
```

Compare by hash, never by eye, and **test the key rather than trusting its shape** — the
trial and production Cohere keys look identical and differ only under load. Twelve rapid
`rerank-v3.5` calls separates them: a trial key starts 429ing around the ninth, a production
key returns 12/12.

Two keys legitimately differ and must NOT be synced: `DATABASE_URL` (Render uses the
INTERNAL host, `.env` the external one) and `RENDER_API_KEY`, which must never reach the
deployed service at all.

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

**The consent screen shows the OAuth *brand* name, not the client name, and the brand is
per-PROJECT.** PRD §8 records the client as `Agentic RAG Web`, which is accurate and
irrelevant to what the user reads. `dsai-mod-2-group-project` holds **two** clients —
`Agentic RAG Web` (`…-bv4t…`) and `Bedtime Story Web` (`…-s5gg…`) — with separate ids,
secrets and redirect URIs, and **one shared consent brand between them**. There is no
per-client override, so this project cannot give the two apps different names on the
consent screen. Renaming the brand renames it for both.

The App name field was `Bedtime Story`; it is now **`Groundwork`** (changed 2026-08-15 on
the Branding page — console only, no API, same as client creation). The stale authorised
domain `agentic-rag-api.onrender.com` was dropped in the same save; the live host is
`agentic-rag-api-6x6b.onrender.com` and was already listed separately.

**But renaming did not put "Groundwork" on the consent screen, and the reason is the part
worth knowing: an unverified brand is not displayed at all.** The Verification centre says
it in as many words — *"Your branding is not being shown to users"* — and Google falls back
to the redirect host, so the screen reads:

```
You're signing back in to agentic-rag-api-6x6b.onrender.com
```

That is a real improvement over an unrelated app's name, and it is not the polished
outcome. Getting the name shown needs branding verification (the *Data access* half needs
nothing — `openid email profile` are non-sensitive, and the console states verification is
not required for them). Two consequences: the brand field is **not** a lever on what
attendees read while the app is unverified, and any future edit to it changes nothing
user-visible either.

**A note that says "the consent screen reads X" expires.** The earlier version of this one
recorded `Bedtime Story` on the consent screen, which was true when written and was not
reproducible afterwards — plausibly because unverified branding *is* shown while an app is
in Testing and is suppressed once it is In production, though that transition was not
observed directly and should not be quoted as fact. Re-check before acting on it.

**Force the consent screen without granting anything** — otherwise an already-authorised
account skips straight through and you verify nothing. Append `prompt=consent` to the
authorize URL, built by hand with the real `client_id` and `redirect_uri`; `state` and
`nonce` can be any placeholder because the screen renders before either is checked. Read
the heading, then navigate away rather than clicking Continue.

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

> Layout work? **[new features/07-workspace-shell.md](new%20features/07-workspace-shell.md)**
> is the record of the workspace shell, the settings sheet and the de-NotebookLM pass, with
> every before/after number. `scripts/ui_check.py` is the harness that keeps them true.

**The 2026-08-16 live UI audit added a source-first empty state and made agent creation a
real modal workflow.** An agent with zero documents renders `EmptyAgentWorkspace`: no chat
composer, a plain explanation of why asking is unavailable, and one **Add your first source**
action that opens Sources. The dashboard creates agents inside `Drawer`; while it is open the
dashboard is inert and `aria-hidden`, the Name field receives initial focus, actions stay
pinned to the bottom, and **Next** remains disabled until the name is valid and unique.

The focus detail is load-bearing. `CreateAgentWizard` passed its isolated focus test, but in
the integrated Drawer the focus trap's default heading focus won the race. `Drawer` now
accepts `initialFocusRef` and uses that same ref when establishing the trap. If a modal child
needs initial focus, pass the target into the primitive rather than adding a competing effect
inside the child.

Keep both test layers. `npm test` covers the empty-agent contract and wizard validation in
jsdom; `scripts/ui_check.py` covers the browser-only integration facts (focus ownership,
inert background, sticky actions, viewport fit and overflow). The audited baseline is **2/2
unit tests** and **15/15 browser assertions**, plus a clean production build. The live Render
frontend was verified on commit `1874950` (`Improve empty-agent and creation UX`).

**This repository is public.** Anything in a `VITE_*` variable is compiled into the bundle
and readable in devtools. The frontend gets exactly one config value: the backend URL.

**The dev server MUST be on port 5173.** `cors_origins` is an allow-list, so a second Vite
instance auto-incrementing to 5174 is a *different origin* and every request fails CORS. The
symptom is "Cannot reach the API at http://localhost:8000 (TypeError: Failed to fetch)" on a
backend that is running perfectly and answers `curl` on the same machine. Kill the other
server rather than using the new port.

**A measured height needs a floor, and this one did not have it.** `AgentDetail` sizes the
workspace as `calc(100dvh - top)` — a good technique, because the nav wraps at 320px and any
constant offset would be wrong. But when the chrome above it grew past the viewport the
complement went **negative**, CSS clamped it, and the panel collapsed to its own padding.
Measured 2026-08-16 at 1440x900: opening the two header disclosures took the chrome from
576px to **1092px** and the chat pane from 324px to **24px, with zero visible thread.**

Three things make it worth remembering rather than just fixing:

- **The interaction that triggered it is the one the workshop teaches.** "Change chunk_size,
  watch the answer change" begins by opening `Retrieval parameters`, which deleted the answer.
- **Every error-shaped check passed.** No exception, no console error, no failed request, no
  React warning, zero horizontal overflow. The page rendered perfectly with no product on it —
  [loop.md](new%20features/loop.md) T2 arriving in a module that has never seen a tool. The
  assertion that catches it is *"is the thread taller than zero?"*, never *"did anything
  throw?"*.
- **Desktop was worse than mobile**, 576px against 289px, because `compactHeader` collapsed
  the reference material with `hidden sm:block` — so the responsive fix applied only *below*
  640px. When a fix is written for a phone, check what it does at 1440.

The structural answer is that chrome which can grow must not be in the flow the height is the
complement of. Anything expandable belongs in an overlay, where opening it costs the
workspace nothing.

**`sticky bottom-0` resolves against the scroll container's PADDING box, not its border box,
and a negative margin does not move it.** The settings sheet lives in a `Drawer` panel that
carries `p-4`, so the sticky Save bar parked 16px short of the panel edge and the form
scrolled visibly through the gap underneath it — which reads as a rendering bug rather than a
spacing one. `-mx-4 -mb-4` makes the bar span the full width and changes nothing about where
it comes to rest; only the offset does. `-bottom-4`.

**`contents` and `hidden` are both display utilities of equal specificity**, so
`className={`contents ${x ? "" : "hidden"}`}` is a coin-flip decided by their order in the
generated stylesheet, not by the string. Use the `flex` / `hidden` swap the codebase already
uses for the conversation rail.

**Unmounting a child to "stop its poll" deletes whatever that child reports upward.** The
handout dock rendered `{open && children}` as an optimisation; the count in its own toggle is
produced by the panel's list request, so a shut dock read `Handouts 0` on a conversation whose
answer said "made 1 handout". The optimisation removed exactly the state it was protecting.
The premise was wrong too — `HandoutsPanel` and `useAgentDocuments` both stop polling on their
own once nothing is pending, so staying mounted costs **one** request, not one every three
seconds. Hide with `hidden`; unmount only a child that reports nothing and owns no timer.

**An effect keyed on a whole record re-runs when its owner refetches.** The settings sheet
re-seeded its draft from `agent`, and the owner refetches the agent whenever the corpus
changes — so an upload finishing while the sheet was open would have silently discarded
whatever the user had typed. The visible symptom was only that a "Saved." confirmation
vanished in the frame it appeared, which is how the real bug nearly got shipped. Key on
`agent.id`.

**The product is called Groundwork.** The landing page said "NTU Harness Engineering" until
2026-08-15, which was wrong twice over: nothing in the app is NTU-specific, and a course name
is not a product name. NTU still appears in `PRD.md` and once in `README.md`, deliberately —
those are **provenance and the copyright question on the workshop PDFs** (PRD §8 open item 6),
not branding. Do not sweep them with a global find-and-replace.

**`transform-style: preserve-3d` fails SILENTLY, and the landing scene depends on it.**
The page renders perfectly, just flat, and nothing in devtools names a cause. Two ways to
break it, both easy to reintroduce:

1. The element carrying `preserve-3d` must not also carry `filter`, `backdrop-filter`,
   `opacity < 1`, or `overflow` other than `visible`. The property silently wins.
2. The 3D context must be **contiguous** — every element between the `perspective` and the
   transformed children needs `preserve-3d` too. One ordinary wrapper div ends it.

So in `PipelineScene.tsx` the blur glows are *siblings* of `.gw-rig`, never wrappers, and the
depth-cue `opacity` sits on the leaf panes rather than on the rig. **Verify by measurement,
not by eye**: with perspective live the six panes foreshorten monotonically
(277.1 → 203.8 px at desktop, 237.1 → 174.3 px at 375 px wide). Flattened, all six report an
identical width — which is the one-line check worth running after touching that subtree.

No 3D library was added. `three.js` or Remotion would be megabytes on a static site whose
whole config surface is one backend URL, to draw six rectangles.

**The workshop PDFs are gitignored** pending a licensing decision (PRD open item 6). Large
binaries in git are permanent — removing them later means rewriting history.

**Native constraint validation ABORTS the submit event, so custom validation beside it is
dead code that looks like it works.** The create form carried a bare `required` on the name
input. Adding a wizard with its own step-gating did not replace that: the browser failed the
constraint first, showed "Please fill out this field" in a tooltip positioned on top of the
inline message saying the same thing, and never fired `onSubmit` — so `advance()` never ran.
The inline error appeared anyway, which is what made it convincing: it came from the `onBlur`
that the Next click happened to trigger. **The tell is that the custom message renders but the
step never changes.** `noValidate` on the `<form>` is the fix, and `required` stays on the
input — the attribute is the semantic that reaches the accessibility tree, the bubble is the
interaction, and only the second one is being replaced.

**`accent-color` cannot resize a range track, and the two vendor pseudo-elements cannot share
a rule.** Tailwind's `accent-emerald-500` colours a slider's thumb and filled track in one
class, and it leaves a 4px hit area in an app where `min-h-11` (44px) is a hard convention —
so `.gw-range` in `index.css` is a 44px transparent input with a 6px track drawn inside it.
Writing `::-webkit-slider-thumb, ::-moz-range-thumb { … }` as one selector list silently
leaves BOTH browsers unstyled: an unrecognised pseudo-element invalidates the whole rule.
Separate blocks, always. Webkit also aligns the thumb to the top of the track rather than
centring it, hence the negative `margin-top`; Firefox centres it and needs none.

**StrictMode's double-invoked effects turn a two-step focus into a blur.** A step-change
effect that focused the heading and then the input fired a blur between them on the second
invocation, which set the "field has been visited" flag and opened the form already showing
*Give the agent a name* — scolding the user before they had done anything. Focus exactly one
element per transition. More generally: **whether a field has been visited is a fact about the
user, and only a real user blur may assert it** — any effect that moves focus can forge one.

**A notice about something that already happened must not be cleared by the navigation that
delivers it.** Changing persona resets customised tuning, and the notice explaining that was
set while step 2 was on screen, then wiped by the same Next click that carried the user to
step 3 — the only step that renders it. Net effect: a silent reset, the exact thing the notice
existed to prevent. It is cleared on the way *out* of the tuning step now, never on the way in.

### Ragas

> Running an evaluation, rather than debugging one? **[EVAL.md](EVAL.md)** has the routes,
> every setting in a table, the refusal tiers, and the five ways a scorecard misleads. What
> follows is the packaging and judge-behaviour half — the things that broke.

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

**The golden set is drafted by `GOLDEN_SET_MODEL`, which is deliberately not
`DECISION_MODEL`.** They were the same setting by accident until a head-to-head over the
same corpus and prompt separated them. What decided it was not fluency but two specific
properties the metrics actually read:

| | `google/gemma-4-31b-it` | `google/gemini-3.7-flash` |
|---|---|---|
| Refusal probe | "Which launch vehicle was used?" — a fact the corpus never raises | "What propellant do the thrusters use?" — the corpus raises thrusters *and* propellant conservation, never the type |
| Reference answer | `"Nineteen"` (8 chars) | `"The permanent crew complement is eleven, which expands to nineteen during handover weeks."` |
| Time | 11.8 s | 5.8 s |

The refusal difference is the one PRD §3.6.1 calls "the single largest determinant of
whether the set measures anything" — Flash's probes hinge on a detail the corpus *starts*
and does not finish, which is a far tighter test of grounding. The reference difference
matters because `LLMContextRecall` decomposes that field into claims: a one-word reference
gives it nothing to attribute.

**The cost, recorded rather than hidden: the drafting model is currently also the judge**,
so context precision and recall are graded against references the judge wrote. Faithfulness
and answer relevance never read `reference` — and faithfulness is the metric that was
actually broken — while both context metrics are pinned at 1.0 by the single-chunk corpus
regardless. Set `GOLDEN_SET_MODEL=google/gemma-4-31b-it` to buy independence back at the
cost of both rows above.

**Exclude refusal questions from the metric means.** The golden set deliberately contains
questions the corpus cannot answer, and `golden_questions.expected_behaviour = 'refuse'` marks
them. A *correct* refusal retrieves nothing useful and returns an answer that deliberately
does not follow from the context — so faithfulness and context_recall score near zero for
behaving perfectly. Averaging them in penalises correct refusals, and worse, it aims the
weakest-metric pointer at whichever metric refusals punish hardest rather than at the real
weakness. Score them separately as pass/fail on `behaviour_ok`.

**The judge is no longer the generator. `RAGAS_JUDGE_MODEL` is now
`google/gemini-3.7-flash`** while generation stays on `google/gemma-4-31b-it`, so a run is
no longer self-assessment and `self_judged` on the scorecard should read False. Everything
below about Gemma-as-judge is kept as the evidence that forced the split, not as current
configuration. What still holds regardless of the models: `eval_runs` records
`judge_model` and `generation_model` separately per run, never reading either back from
`agents.generation_model`, which can change after a run.

**Verified on the new judge before trusting it** — the same shape Gemma scored 0.000:

| Case | Gemma judge | Gemini 3.7 Flash |
|---|---|---|
| Answer copied **verbatim** out of its context | **0.000** | **1.000** |
| Same answer plus two invented facts | — | **0.250** |
| Correct refusal | skipped | skipped, `behaviour_ok=True` |

Three turns scored in 14.4 s total, against Gemma's 165–196 s for a *single* faithfulness
call. The metric now discriminates grounded from ungrounded rather than failing to grade
its own job.

**Thinking cannot be turned off on Gemini 3.7 Flash** (`reasoning.mandatory = true`), only
turned down — high / medium / low, default medium, billed at the completion rate.
`RAGAS_JUDGE_REASONING_EFFORT` defaults to `low` because the judged metrics are natural
language inference over text already in the prompt, not a problem that rewards a long
chain of thought, and the whole reason for leaving Gemma was latency. It is not free even
so: a trivial YES/NO verdict measured **80 reasoning tokens out of 81 output tokens**.

#### Run 3 — the first trustworthy scorecard

Same agent, same corpus, same ten questions. Only the measurement changed: independent
judge, and the refusal detector fixed.

| | Run 1 | Run 2 | **Run 3** |
|---|---|---|---|
| Judge | `gemma-4-31b-it` | `gemma-4-31b-it` | `google/gemini-3.7-flash` |
| Self-judged | yes | yes | **no** |
| Duration | 1497 s | 1380 s | **90 s** |
| `error_count` | 7 | 2 | **0** |
| faithfulness | 0.562 (n=**7**) | 0.628 (n=**6**) | **0.769 (n=8)** |
| answer relevance | 0.795 (n=**1**) | 0.938 (n=8) | 0.959 (n=8) |
| context precision / recall | 1.000 / 1.000 | 1.000 / 1.000 | 1.000 / 1.000 |
| `refusal_pass` | 0 / 2 | 0 / 2 | **1 / 2** |

**Run 3 is the first run whose footnote is true.** Every metric's `n` equals the
`scored_count` of 8 that the card prints. Run 1's answer relevance was a mean over a
single value while the card claimed 8, and run 2's faithfulness over six — always the
metric most likely to fail, which is the one the weakest-metric pointer selects.

**Do not read 0.628 → 0.769 as the judge delta.** Every run re-asks the questions, and
generation runs at temperature 1.0, so the answers differ between runs — judge change and
answer variation are confounded there. The clean evidence is the controlled test above:
same stored answer, two judges, 0.000 versus 1.000.

**Faithfulness is still the weakest metric, and on a teaching persona it structurally
cannot be anything else.** This is the finding of the run, and the scorecard's own advice
is dangerous. The lowest-scoring answer, 0.500:

```
1. "The collision avoidance threshold ... is a probability of 1 in 10,000 [1]."   grounded
2. "Please restate this idea in your own words to ensure you have understood it."  not
```

And the 0.571 row (0.565 in run 2 — stable across judges *and* regenerated answers):
sentences 1–4 are four correct figures straight from the context; sentence 5 is
**"**Analogy:** The Ka-band is like a high-speed highway that is closed in several
sections…"**; sentence 6 asks the learner to paraphrase. Four of seven statements
supported.

Nothing was hallucinated *about the corpus*. Retrieval was right and every fact was right.
Faithfulness is counting the analogy and the comprehension check as unsupported claims —
which is correct, since they are not in the context, and they are the two things
`feynman-explainer` is explicitly designed to produce (§4.2). **The card then names
faithfulness as weakest and advises tightening the grounding clause and reducing persona
verbosity, i.e. deleting the pedagogy.** Same failure class as the refusal detector: the
measurement penalising correct behaviour and confidently recommending its removal.

One caveat against over-reading it: all eight answers carry a pedagogical tail, including
the three that scored **1.000**, so the judge does not always extract an imperative as a
claim. Part of the spread is genuine judge variance on non-claim sentences, not persona
cost. The analogy, though, is an unsupported statement by construction.

**So faithfulness is not yet a valid measure for a persona that invents explanatory
material.** Either score personas against a rubric that exempts clearly-marked pedagogy,
or measure faithfulness on the plain `lecture-qa` template — still never tested here — and
treat the persona number as a separate thing. Do not act on the pointer as written.

**`ResponseRelevancy(strictness=...)` must be 1 for Gemma. The default of 3 fails every
call.** `strictness` does not mean three requests — it asks for three *candidates* in one
request (`candidate_count=3`), and Gemma on the Gemini API answers:

> `400 INVALID_ARGUMENT: Multiple candidates is not enabled for this model`

Measured 2026-08-15: this failed **7 of 8** scored questions on the first real run. The
failure mode is the dangerous kind — it is per-metric, so the run still reported
`status=completed` with three metrics populated, `answer_relevance` almost entirely null, and
a confident weakest-metric pointer. **A metric that silently declines to measure is worse
than one that crashes the run**, because the scorecard still renders. The cost of `1` is a
noisier score (a mean over one generated question, not three). **It stays 1 under the new
judge, for a different reason:** `n` is absent from `google/gemini-3.7-flash`'s OpenRouter
parameter list, so `candidate_count=3` has no eligible provider at all. The Gemini API
would allow it; this route does not.

**Gemma was measurably unfit as a *faithfulness* judge, and this is not a theoretical
worry.** (Resolved by the judge split above; kept because it is the measurement that
justified it, and because the *method* — re-run under a second judge before acting on a
weak metric — applies to whatever judge is configured next.)
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

**Gemma is also too SLOW to be a judge, and `METRIC_TIMEOUT_S = 180` is not the problem.**
The second run reported `faithfulness: timed out after 180s` on 2 of 8 scored questions. The
intuitive cause — long answers produce more atomic statements — is **wrong**, and the data
kills it directly: the 1551-character answer scored fine while the 495-character one timed
out. Replayed with every judge call instrumented, same turns, same contexts:

| Answer | `gemma-4-31b-it` | `gemini-flash-latest` |
|---|---|---|
| 495 chars | 165.0 s → 0.50 | **10.3 s** → 0.67 |
| 1551 chars | 196.3 s → 0.565 | **28.9 s** → 0.611 |
| 933 chars | >240 s, 0 calls returned | quota-failed, see below |

**Flash is 7–16× faster on identical payloads.** Faithfulness is only ever two LLM calls
(`_create_statements`, then `_create_verdicts`), so this is raw per-call latency, not call
count — and Gemma's is wildly variable: single calls measured between 39.8 s and 124.9 s,
roughly 6 to 54 output chars/second, a 10× spread on one model. Note the middle row: on
replay the *control* question needed 196.3 s and would have timed out too. **This never was a
2-question problem — half the set sits at the ceiling and which rows fail is luck.**

Two things this replay ruled out, both worth not re-investigating. **Zero repair calls
fired.** Gemma fenced every single output (`fenced=True`, all six calls) and Ragas'
`extract_json` absorbed all of it — the `LangchainLLMWrapper` decision above is doing exactly
its job, and the parse-repair recursion in `RagasOutputParser.parse_output_string` never ran.

**`METRIC_TIMEOUT_S` silently doubles as the quota-retry ceiling, and that conflation is the
real trap.** It is documented as a *hang* ceiling. But when Flash hit
`RESOURCE_EXHAUSTED` (429) on the third question, LangChain retried with backoff inside the
budget and the metric died reporting `timed out after 180s` — the same string a hang produces.
**A rate limit and a hang are indistinguishable on the card, and they need opposite fixes**
(wait vs. raise the ceiling). Widen that error before trusting either. The 429 above may well
have been self-inflicted — two full replays inside twenty minutes on a free tier — which is
itself the warning: one run is 10 questions × 4 metrics × 2+ calls, and back-to-back runs
while iterating are exactly the workload that exhausts a free-tier quota.

**Each metric's mean has its OWN denominator, and the scorecard's footnote does not.**
`summarise` appends to `collected[key]` only when a value is non-null, so the reported
faithfulness of 0.63 was a mean over **6** values while the card said "Means rest on 8 scored
questions" — `scored_count` counts a row if *any* metric survived. The metric most likely to
fail is therefore the one with the smallest sample, and it is the one the weakest-metric
pointer selects and sends you to act on. Read `scored_count` as an upper bound, not as the
denominator of the number next to it.

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
