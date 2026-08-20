# 05 — Embeddings and rerank

Contracts consumed: [PLAN.md](PLAN.md) §3.2 schema, §3.3 the metering contract.
**Nothing here restates them.**

Added after 01–04 shipped, because the audit's conclusion that embedding cost must be
*estimated* was measured and found wrong. The correction is in
[00-AUDIT.md](00-AUDIT.md) §3.5.

## What the user gets

The console stops under-reporting. A turn's line items now include the retrievals it paid
for, not only the answers it generated.

## Technical detail

**Embedding cost is REPORTED, exactly like chat cost.** Measured through this repo's own
`get_embeddings()`:

```
usage     Usage(prompt_tokens=7, total_tokens=7, cost=1.4e-06, cost_details={...})
provider  "Google AI Studio"
id        "gen-emb-1787199865-2n8YI6ISF4fmdg3MBDvM"
```

`cost` arrives on `usage.model_extra`, `provider` and `id` on the response's. The `openai`
SDK preserves all three. The audit's error was inferring from "LangChain has no embedding
callback" that there was no *seam* — there was, one layer down.

**Both wrappers wrap the CLIENT, not a method**, and that is the reusable decision:

| | why the method would have been wrong |
|---|---|
| `OpenAIEmbeddings` | `check_embedding_ctx_length=False` routes through a plain batching loop, `True` routes through `_get_len_safe_embeddings`. **Both** call `self.client.create(...)`. CLAUDE.md records that flag as one 400 away from reconsideration, so a method override would un-meter the system on a config change |
| `CohereRerank` | reached two ways here — through `ContextualCompressionRetriever` and directly from `aretrieve`. One seam covers both |

Both classes set `model_config extra="forbid"`, so assignment to the declared `client` /
`async_client` fields is **verified, not assumed** — `metering_check.py` case 9e constructs
a real `OpenAIEmbeddings` and asserts the wrapper survives.

**The call kind overrides the ambient scope.** An `embed_query` inside a turn runs under
`run_turn`'s `generation` scope; filing it there would hide the retrieval half of the bill
inside the answer half. `meter_embeddings` always writes `embedding`, and inherits only the
user, agent and query.

**Rerank is counted, never priced.** Cohere returns `meta.billed_units.search_units`
(measured `1.0` per call) and no cost. Units go in `api_usage.units` as a measurement; a
dollar figure would be arithmetic over a published rate, so it is opt-in via
`cohere_search_unit_usd` (default `0.0` = do not estimate) and lands in `estimated_cost`,
never `cost_usd`. Off by default because a hardcoded price is a number nobody re-checks,
and this repository already has that failure on this provider — a Cohere key silently
downgraded to a trial tier, discovered only under load.

**Ingest is scoped too**, and it is where embedding spend actually lives: one question
embeds one string, one document embeds every chunk of it. `run_ingest_job` opens
`collect_usage()` with `inherit=False` and persists in a `finally`, so a document that
failed at chunk 40 of 50 still records the 40 it paid for.

## Acceptance criteria

| id | Harness | Asserts |
|---|---|---|
| **E1** | `scripts/metering_check.py` case 9 | An embeddings response yields a record with **reported** cost, `cost_is_estimated=False` |
| **E2** | `scripts/metering_check.py` case 9b | Kind is `embedding` and the served provider is recovered |
| **E3** | `scripts/metering_check.py` case 9d | The kind **overrides** an ambient `generation` scope while attribution is inherited |
| **E4** | `scripts/metering_check.py` case 9e | A real `OpenAIEmbeddings` still accepts a wrapped client under `extra="forbid"` |
| **E5** | `scripts/metering_check.py` case 10 | Rerank records measured search units |
| **E6** | `scripts/metering_check.py` case 10b/10c | Rerank never claims a reported cost, and invents no estimate when the price is unset |
| **E7** | `scripts/metering_check.py` case 10d/10e | With a price set, the estimate lands in `estimated_cost` and **never** in `cost_usd` |
| **E8** | manual, PLAN §5 | One real turn records embedding **and** rerank rows beside the chat rows |

**E8 as measured, 2026-08-20** — the same turn that previously produced 3 rows now produces
**9**, across four kinds:

```
rewrite     google/gemma-4-31b-it            CoreWeave    493/12     $5.338e-05
embedding   google/gemini-embedding-2        Google         6/-      $1.2e-06
rerank      rerank-v3.5                      cohere         -/-      units=1
generation  deepseek/deepseek-v4-flash-0731  Relace      1823/107    $0.00014259
embedding   google/gemini-embedding-2        Google         5/-      $1e-06
rerank      rerank-v3.5                      cohere         -/-      units=1
embedding   google/gemini-embedding-2        Google         3/-      $6e-07
rerank      rerank-v3.5                      cohere         -/-      units=1
generation  deepseek/deepseek-v4-flash-0731  Relace      2833/689    $0.00029477
```

**Read the shape, not just the total.** Three embeddings and three reranks for one question
is the agent loop searching twice more after its initial retrieval — CLAUDE.md's note that
*"the retrieval budget quietly doubled"* and that `max_tool_steps` bounds steps rather than
calls, now visible as line items instead of as a warning in prose.

## What must keep working

- **`scripts/embed_check.py` must stay green.** It is the measurement that both embedding
  routes land on one vector, and the wrapper must not perturb the returned values — it
  records and returns the response untouched.
- **`EMBEDDING_ROUTE=google` must still work.** `meter_embeddings` returns the embedder
  unchanged when there is no `client` to wrap: losing metering on the rollback route is
  correct, crashing on it is not.
- **`metering_enabled=false` still means untouched.** Both wrappers are attached behind that
  flag in `retriever.py`, and the imports are local so the metering package never becomes a
  hard dependency of retrieval.

## As built — where this was wrong

**Inserting the `_metered` helper above `get_embeddings` stole its `@lru_cache` decorator.**
The text landed between the decorator and the function it belonged to, so `_metered` became
the cached one and `lru_cache` tried to hash an `OpenAIEmbeddings` — surfacing as
`TypeError: unhashable type: 'OpenAIEmbeddings'` at the *call site*, naming neither the
decorator nor the caching. Worth knowing generally: **inserting a function immediately
before another silently rebinds any decorator sitting above it**, and the failure appears
somewhere else entirely.
