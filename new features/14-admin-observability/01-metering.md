# 01 — The meter

Contracts consumed: [PLAN.md](PLAN.md) §3.1 settings, §3.3 metering contract, §6 risks.
**Nothing here restates them.**

## What the user gets

Nothing visible. Every model call in the project starts reporting what it cost, and the
number is OpenRouter's own rather than one this repo computed.

## Technical detail

Three pieces, `app/metering/`.

### `chat.py` — `MeteredChatOpenAI(ChatOpenAI)`

Overrides exactly one method, `_convert_chunk_to_generation_chunk` (langchain-openai 1.5.1,
`chat_models/base.py:1408`). In the `len(choices) == 0` branch — the usage-only frame — it
attaches what `_create_usage_metadata` (base.py:4175) discards:

```python
generation_info["openrouter_usage"] = [{"usage": chunk["usage"],
                                        "provider": chunk.get("provider"),
                                        "id": chunk.get("id")}]
```

**A single-element list, never a bare dict or float** — PLAN §6 R2. `merge_dicts`
concatenates lists and *raises* on two unequal scalars, and streaming merges
`generation_info` across chunks (measured: `finish_reason` merges to `"stopstop"`).

**It changes parsing only.** No field is added to the request body; `extra_body`,
`disabled_params` and `model_kwargs` are untouched. That is R3, and case A6 asserts it.

`build_chat_model` (`llm.py:241`) constructs `MeteredChatOpenAI` instead of `ChatOpenAI`
when `settings.metering_enabled`, and `ChatOpenAI` otherwise. **The `else` branch is the
regression guarantee**, not a courtesy: with the flag off the object is the one that shipped.

### `context.py` — `meter_as(...)`

A `ContextVar` holding `MeterScope(user_id, agent_id, query_id, call_kind)`. Async-task-local,
so `RAGAS_MAX_CONCURRENCY=2` cannot cross-attribute. Reentrant: an inner `meter_as` overrides
and restores on exit, because `pipeline` opens a `generation` scope inside which the loop
may open a `critic` one.

### `meter.py` — `UsageMeter(AsyncCallbackHandler)`

`on_llm_end(response: LLMResult)` reads, in order of preference:

| Source | Gives | Path |
|---|---|---|
| `response.llm_output["token_usage"]` | tokens + `cost` + `cost_details` | `ainvoke` |
| `generations[0][0].generation_info["openrouter_usage"][-1]` | tokens + `cost` + `provider` + `gen-` id | `astream`, via the subclass |
| `generations[0][0].message.usage_metadata` | tokens only | fallback, both paths |

Emits a `UsageRecord` to a sink. **This feature ships the logging sink only**; feature 02
swaps in the database sink. That split is deliberate — it lets the riskiest piece run in
production for a day before anything writes.

**Every callback body is wrapped** (R1). A metering failure logs at WARNING and returns; it
never propagates into a turn. `settings.metering_strict` re-raises, and harnesses set it.

## Acceptance criteria

| id | Harness | Asserts |
|---|---|---|
| **A1** | `scripts/metering_check.py` case 1 | `MeteredChatOpenAI` attaches `openrouter_usage` as a **list of length 1** on a synthetic usage-only chunk |
| **A2** | `scripts/metering_check.py` case 2 | Two synthetic usage frames merge to a list of **2** and **do not raise** — R2 directly |
| **A3** | `scripts/metering_check.py` case 3 | `UsageMeter` extracts cost from an `ainvoke`-shaped `LLMResult` |
| **A4** | `scripts/metering_check.py` case 4 | `UsageMeter` extracts cost + `served_provider` from an `astream`-shaped `LLMResult` |
| **A5** | `scripts/metering_check.py` case 5 | Two concurrent `asyncio` tasks in different `meter_as` scopes produce records with **different** `user_id` — the contextvar claim in PLAN §3.3 |
| **A6** | `scripts/llm_check.py` case 31 | `extra_body`, `disabled_params` and `model_kwargs` are **byte-identical** with metering on and off, for all four model families — R3 |
| **A7** | `scripts/metering_check.py` case 6 | A meter whose sink raises does **not** propagate when `metering_strict=false`, and **does** when true — R1 |
| **A8** | `scripts/route_check.py --live` | A real streamed call yields `cost > 0` **and** a non-empty `served_provider`. **Not "no exception"** — R5 |

A1–A7 are layer 1: no DB, no network, seconds.

## What must keep working

- **`metering_enabled=false` ⇒ the request is byte-identical to today.** A6 asserts it for
  the request body; `llm_check.py`'s existing 30 cases must stay green with the flag on.
- `scripts/agentic_check.py` S1 (tools off ⇒ classic path unchanged) and S16 (the
  reasoning/prompt disjunction) must be unaffected. Metering touches neither.
- `refusal_check.py` and `ledger_check.py` must be untouched — they do not construct models.
