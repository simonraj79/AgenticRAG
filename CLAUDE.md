# CLAUDE.md — working notes

Conventions, insights and hard-won gotchas for this repo. **[PRD.md](PRD.md) is the
specification**; this file is the operational companion — the things that cost debugging
time and would cost it again. **[EVAL.md](EVAL.md) is the operator's guide to Stage 3**:
every setting and per-agent parameter in tables, how to run an evaluation, and how to read
a scorecard without being misled by it. What stays here is the *debugging* half — the
symptoms, and which knob caused them.

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
| Provision Pinecone | `python scripts/create_index.py [--dry-run]` |
| Provision Postgres | `python scripts/create_render_db.py [--dry-run]` |
| RAG end-to-end check | `backend/.venv/Scripts/python.exe scripts/slice_check.py` |
| Tear down that check | `backend/.venv/Scripts/python.exe scripts/slice_check.py --cleanup` |
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
| `GENERATION_TOP_K` | `64` | Sent to the Gemini family: no eligible provider, 404. `build_chat_model` drops it for `_NO_TOP_K_PREFIXES` |
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

This is not hypothetical: it has now been flattened and restored **twice**, most recently by
the freeze that added Ragas. Treat "re-add the marker" as the second half of the `pip freeze`
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

**Latency is dominated by generation, not by the cross-Pacific hop.** PRD §6 flags Cohere
as the only Singapore → US round trip. Measured: embed 365 ms, Pinecone k=20 394 ms,
Cohere rerank ~830 ms, **Gemma generation 13.2 s — 89% of the total**. The hop the PRD
worried about costs a twentieth of what generation does. Optimise there or nowhere.

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

**The consent screen shows the OAuth *brand* name, not the client name — and ours is
wrong.** PRD §8 records the client as `Agentic RAG Web`, which is accurate, but signing in
renders **"You're signing back in to Bedtime Story"**: the brand/app name on the GCP project
`dsai-mod-2-group-project` belongs to an unrelated earlier app, and a brand is per-project,
not per-client. Observed in Chrome 2026-08-15. This is worse than cosmetic — a user is being
asked to hand over their identity to an app whose name they do not recognise, which is
exactly the shape of a phishing prompt, and it is the first screen a workshop attendee sees.
Fix on the Branding page of the Google Auth Platform console; there is no API for it, same
as client creation.

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

**This repository is public.** Anything in a `VITE_*` variable is compiled into the bundle
and readable in devtools. The frontend gets exactly one config value: the backend URL.

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
