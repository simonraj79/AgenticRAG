# 18 — The Google ADK runtime, beside the hand-rolled loop

**Status:** in build. Branch `feat/adk-runtime`.
**Shape:** a second agent runtime selected by `AGENT_RUNTIME`, exactly as `EMBEDDING_ROUTE`
and `STORAGE_ROUTE` select an embedder and an object store. Default stays `langchain`.

---

## 1. The seam, and why the whole migration is one function

`grep` settles the architecture before any design does: **only one file in `scripts/` calls
`run_agent_loop`, and only one line in `backend/app/` does** — `pipeline.py`'s generation
branch. Everything anyone thinks of as "the agent" — the SSE contract, `ask.run_turn`, the
trace rows, `api_usage`, the eval job, the Ragas scorecard — sits *above* that call and reads
a `LoopResult`.

So the migration is not a rewrite. It is:

```python
loop_fn = select_runtime(agent)          # app/rag/runtime.py -- one function, one `if`
result  = await loop_fn(...)             # -> LoopResult, identical either way
```

`run_agent_loop_adk` has the **identical signature and return type**. Nothing above the seam
is edited, so nothing above the seam can regress — and that is a structural claim, not a
careful one. `AGENT_RUNTIME=langchain` re-executes the pre-migration path modulo one `if`.

**The rule that keeps it reversible: nothing outside `app/adk/` may `import google.adk`.**
Asserted by `adk_model_check.py` A11, not left to discipline.

---

## 2. The model road — measured, not assumed

The documented ADK road to a non-Gemini provider is `google.adk.models.lite_llm.LiteLlm`.
**We do not take it.** A live spike (`scratchpad/adk_live_spike.py`, 14/14) established that a
custom `BaseLlm` delegating to this repo's existing `build_chat_model()` is strictly better,
and it erases five of the eight risks a docs-only audit had identified:

| Risk on the LiteLlm road | On the `BaseLlm`-over-`build_chat_model` road |
|---|---|
| ADK hard-maps `max_output_tokens` -> `max_completion_tokens`; CLAUDE.md records that as a guaranteed 404 at routing under `require_parameters` | Never reached. Body carries `max_tokens` in `extra_body`, **measured** |
| `stream_options={"include_usage": true}` hardcoded after `_additional_args`, unremovable, and deprecated by OpenRouter | No litellm in the path at all |
| `http_options.extra_body` clobbers the constructor's `provider` block by assignment | No `http_options` anywhere |
| litellm maps `FunctionCallingConfigMode.ANY` -> `"required"` and **discards `allowed_function_names`**, so named-tool forcing — the gap trigger's whole mechanism — becomes inexpressible | The adapter reads `allowed_function_names` itself and emits `tool_choice=<name>`. **Measured PASS** |
| Cost and served-provider lost at the client seam, and *entirely* lost on streamed calls; needs a metered client subclass plus a stream-wrapper plus an `estimated_cost` policy | **Free.** `build_chat_model` already attaches `MeteredChatOpenAI` + `UsageMeter`. Measured: 2 records, `cost_usd` `[3.15e-05, 3.171e-05]`, `served_provider` `['Relace','Relace']`, `call_kind='generation'` — with no metering code written |

That last row is the one that decides it. `metering_check.py` case 12 walks the call graph
seeded on the literal callee name `build_chat_model`; an ADK path that reaches it **stays
covered by construction**. A LiteLlm path would not mention the name, `_leaks` would stay
empty, and the case would print green over an unmetered runtime — this repo's seventh
green-suite failure, reproduced exactly.

**Consequence: `litellm` is not a dependency of this change set.** `google-adk==2.8.0` alone
adds 8 packages and bumps exactly one (`google-genai` 2.18.1 -> 2.20.0). Verified: all six
layer-1 harnesses stayed green after the install.

### 2.1 What the adapter must inherit rather than re-derive

`build_chat_model` is the one chokepoint carrying seven measured facts. The adapter imports
the policy; it never restates it.

| Fact | Where it lives |
|---|---|
| `provider.require_parameters: true` | `llm.py`, gated on the setting |
| `max_tokens` via `extra_body`, never `max_completion_tokens` | `llm.py` |
| `top_k` dropped for `_NO_TOP_K_PREFIXES` = `("google/gemini-", "deepseek/", "minimax/")` | `llm.py` |
| `reasoning` withheld for `_REASONING_ALWAYS_ON_PREFIXES` | `llm.py` |
| `disabled_params={"parallel_tool_calls": None}` | `llm.py` |
| never `provider.order`, never `provider.sort` | `llm.py`, pinned by `llm_check` 27-29 |
| metering attach | `llm.py` |

> **Open question 6.1 from the audit is closed by measurement.** The brief claimed `top_k` is
> sent for the DeepSeek family. It is not: `deepseek/` is in `_NO_TOP_K_PREFIXES`, and the
> live spike confirmed `top_k` absent from the body. The code was right and the brief was
> wrong. `adk_model_check` A3 reads the tuple off `app.rag.llm` at runtime so it follows the
> policy rather than a copy of it.

---

## 3. What ADK genuinely buys

Three things, and they are the justification — the shim is not smaller than `agent_loop.py`.

1. **A deterministic forcer.** Today's `tool_choice="search_corpus"` is honoured by the
   provider roughly one time in three. ADK lets `after_model_callback` return an
   `LlmResponse` carrying a *synthetic* `function_call`, which ADK dispatches itself without
   asking the provider anything. A fired gap trigger then **always** executes its search.
2. **A structural tenancy boundary.** A `BaseTool` subclass with a hand-written
   `_get_declaration()` has no code path that could introspect the closed-over `Agent` into
   the schema — where `FunctionTool` derives the schema from the signature and pushes hard
   toward `def search_corpus(query, agent_id)`.
3. **The streaming path finally acquires a harness.** `agent_loop_check` runs entirely with
   `emit=None`, so chunk accumulation, the tool-step token suppression, the DSML latch and
   the `steps+1` final numbering have **zero coverage today**. `adk_stream_check.py` is a
   deliverable in its own right.

Behaviour change to record rather than gloss: **a fired gap trigger now always searches.**
That is more correct and it is not the same system, so EVAL baselines are re-measured under
ADK before any cross-runtime comparison (§6).

---

## 4. Module layout

**As built.**

```
backend/app/
  rag/
    textguard.py       NEW   MOVED out of agent_loop.py: the U+FF5C sentinel,
                             _strip_leaked_tool_markup, _emit_until_markup.
                             ONE owner; both runtimes import it. Never retyped.
    runtime.py         NEW   select_runtime(runtime=None) -> the loop callable.
                             One function, one `if`, and a LAZY import.
    agent_loop.py      EDIT  Re-exports the three textguard names for compat, so
                             refusal_check 27-33 keep resolving. Otherwise unchanged.
    pipeline.py        EDIT  Two call sites: `loop_fn = select_runtime()`.
  adk/                 NEW   Nothing outside this package imports google.adk (A11).
    __init__.py              Deliberately EMPTY -- a convenience re-export here would
                             destroy the lazy-import rollback while every test passed.
    model.py                 OpenRouterAdkLlm(BaseLlm) -> delegates to build_chat_model.
                             build_adk_model() is the named seed metering_check 12b needs.
    context.py               AdkTurnContext: agent, ledger, step, gap phase, timings.
                             WRAPS app.tools.registry.ToolContext rather than replacing it.
    tools.py                 SearchCorpusTool / RunPythonTool, hand-declared schemas,
                             wrapping the EXISTING langchain tools. Tool ORDER. Semaphore.
    loop.py                  run_agent_loop_adk(...) -> LoopResult. Identical signature.
                             Owns the two-invocation gap structure and the SSE translation.
    plugins.py               step_budget, context_block, text_guard, gap_trigger, in the
                             one order that is correct.
  config.py            EDIT  agent_runtime ("langchain" | "adk"), validated at load.
```

**Three things the audit's layout proposed and this build deliberately does not have.**

- `adk/client.py` — existed only to recover cost from litellm. There is no litellm.
- `adk/events.py` — the ADK-event-to-`Emit` translation is ~40 lines inside `loop._drive`,
  and splitting it out would separate the streaming decisions from the loop state they read
  (`ctx.step`, `ctx.gap_fired`) for no reader's benefit.
- `rag/trace_payloads.py` — declaring the four payload key sets in one place is a real and
  separate improvement, and it belongs to `ask.run_turn`, which this change set does not
  touch. Deferred rather than done badly. **Recorded here so it is a decision and not an
  omission.**

`plugins.py` is a module rather than the proposed `plugins/` package: five callbacks, and
their ORDER is the load-bearing fact, which one file states once at the bottom.

---

## 5. Acceptance criteria

> An acceptance criterion names a harness file and a case id, or it is a wish.

Cases marked ★ are watched failing before the code exists.

| # | Criterion | File | Case |
|---|---|---|---|
| 1 | `SearchCorpusArgs` exposes exactly one field | `scripts/tenancy_check.py` | T1 |
| 2 | The langchain bound-tool schema names no tenancy parameter | `scripts/tenancy_check.py` | T2 |
| 3 | The **serialised** ADK declaration names no tenancy parameter | `scripts/tenancy_check.py` | T3 ★ |
| 4 | `agent.namespace` appears nowhere in the serialised request body | `scripts/tenancy_check.py` | T4 ★ |
| 5 | No tool callable in `backend/app/` takes `agent_id`/`namespace`/`corpus` (AST) | `scripts/tenancy_check.py` | T5 ★ |
| 6 | `AgentTool` imported nowhere in `backend/app/` | `scripts/tenancy_check.py` | T6 |
| 7 | Body carries `max_tokens`, **not** `max_completion_tokens` | `scripts/adk_model_check.py` | A1 ★ |
| 8 | `provider.require_parameters` true, by exact dict equality | `scripts/adk_model_check.py` | A2 |
| 9 | `top_k` presence matches `_NO_TOP_K_PREFIXES` read at runtime | `scripts/adk_model_check.py` | A3 ★ |
| 10 | `reasoning` presence matches `_REASONING_ALWAYS_ON_PREFIXES` | `scripts/adk_model_check.py` | A4 |
| 11 | `parallel_tool_calls` absent from the body | `scripts/adk_model_check.py` | A5 |
| 12 | No `provider.order`, no `provider.sort`, every family | `scripts/adk_model_check.py` | A6 |
| 13 | Forced mode ANY + `allowed_function_names` -> `tool_choice=<name>` | `scripts/adk_model_check.py` | A7 ★ |
| 14 | Mode NONE -> `tool_choice="none"` **with `tools` still populated** | `scripts/adk_model_check.py` | A8 ★ |
| 15 | Metering on vs off -> byte-identical request | `scripts/adk_model_check.py` | A9 |
| 16 | Nothing outside `app/adk/` imports `google.adk` | `scripts/adk_model_check.py` | A11 |
| 17 | Tool order is `[search_corpus, run_python]`, unsorted | `scripts/adk_model_check.py` | A12 |
| 18 | No tool call -> immediate return, `stopped_reason is None` | `scripts/adk_loop_check.py` | 3b ★ |
| 19 | Gap trigger fires at most once per turn | `scripts/adk_loop_check.py` | 16 ★ |
| 20 | Gap trigger suppressed when `corpus_searched` | `scripts/adk_loop_check.py` | 17 ★ |
| 21 | Model declines the forced call -> a synthetic call still executes | `scripts/adk_loop_check.py` | 1b ★ |
| 22 | Budget exhausted -> one further call, `stopped_reason=="max_steps"`, answer non-empty | `scripts/adk_loop_check.py` | 15 ★ |
| 23 | A raising tool does not abort the invocation | `scripts/adk_loop_check.py` | 18 ★ |
| 24 | `ToolInvocation.content` is the string the model read, clipped by the SETTING | `scripts/adk_loop_check.py` | 10 |
| 25 | Two calls in one step do not overlap in time | `scripts/adk_loop_check.py` | 14 ★ |
| 26 | A tool step emits zero TOKEN frames | `scripts/adk_stream_check.py` | S1 ★ |
| 27 | The `partial=False` aggregate is not re-emitted as tokens | `scripts/adk_stream_check.py` | S8 ★ |
| 28 | A sentinel split across two fragments is caught; the latch holds | `scripts/adk_stream_check.py` | S5 ★ |
| 29 | `emit=None` -> zero frames and a `LoopResult` equal to the streamed one | `scripts/adk_stream_check.py` | S9 ★ |
| 30 | Metering coverage seed is a SET including the ADK builder | `scripts/metering_check.py` | 12b ★ |
| 31 | `AGENT_RUNTIME=langchain` reproduces the pre-migration path | `scripts/agentic_check.py` | S1, S33 |
| 32 | The full agentic suite is green on **both** runtimes | `scripts/agentic_check.py --runtime adk` | S1-S35 |
| 33 | `pywin32` marker survives the `google-adk` freeze | `grep -n pywin32 backend/requirements.txt` | manual |

**What must keep working:** with `AGENT_RUNTIME=langchain`, every existing harness is green and
unmodified. That is criterion 31 and it is the standing form of *"with the feature off, output
is byte-identical to today — assert it."*

---

## 6. Evaluation

Ragas, unchanged — `app/eval/ragas_runner.py` reads a stored turn and never knows which runtime
produced it, which is exactly why the seam was drawn at `run_agent_loop`.

**ADK's own eval stack is deliberately rejected.** `tool_trajectory_avg_score` compares tool
arguments byte-exactly, which is the identical trap already documented for ragas
`ToolCallAccuracy`: `REWRITE_EVERY_TURN=true` at temperature 1.0 means the one meaningful
argument is designed never to be the same string twice. `num_runs` defaults to 2, doubling
spend. `response_evaluation_score` needs GCP. And `google-adk[eval]` would pull
`google-cloud-aiplatform[evaluation]`, `rouge-score`, `nltk`, `openpyxl` and `gepa` into a repo
already carrying ragas.

The comparison is A/B on one agent, same golden set, one run per runtime, scorecards diffed.
`eval_runs.agent_runtime` records which produced which; **NULL means "predates the column",
which is not `"langchain"`.**

---

## 6.1 Measured, 2026-08-28

**Verification ladder, all green.**

| Harness | Result |
|---|---|
| `sandbox_check` | 22 checks |
| `ledger_check`, `refusal_check`, `route_specialist_check`, `llm_check` | all pass, unmodified |
| `agent_loop_check` | all pass -- the langchain runtime is byte-unchanged |
| **`tenancy_check`** (new) | **14 checks**, and mutation-verified: adding `agent_id` to the ADK declaration turns T3/T4 red |
| **`adk_loop_check`** (new) | **36 checks**, three mutations verified red |
| `metering_check` (+12b, 12c) | all pass |
| `npm test` | 79 / 79 |
| **`adk_parity_check`** (new, live) | **15 checks** |
| **`adk_ragas_check`** (new, live) | **7 checks** |

**Mutation tests, run and verified RED:**

| Line deleted | Case that caught it |
|---|---|
| the `not corpus_searched` gate | `adk_loop_check` 17 |
| the `gap_fired` gate | `adk_loop_check` 16c |
| `config.tools = []` on budget exhaustion (the "obvious ADK move") | `adk_loop_check` 15 |
| `agent_id` added to the ADK declaration | `tenancy_check` T3/T4 |

**Ragas A/B**, `Kestrel Feynman`, **all 10 golden questions**, judge `google/gemini-3.7-flash`,
generation `deepseek/deepseek-v4-flash-0731`, golden set drafted by `minimax/minimax-m3`, so
`self_judged` is False in both directions:

| | langchain | adk |
|---|---|---|
| faithfulness | **0.836** (n=8) | **0.758** (n=8) |
| answer relevance | 0.925 (n=8) | 0.831 (n=8) |
| context precision | 1.000 (n=8) | 1.000 (n=8) |
| context recall | 1.000 (n=8) | 1.000 (n=8) |
| `scored_count` / `total_count` | 8 / 10 | 8 / 10 |
| **`refusal_pass`** | **1 / 2** | **1 / 2** |
| `error_count` | 0 | 0 |
| `self_judged` | False | False |
| tool calls | 4 | 5 |
| cost | $0.00095725 | $0.00091298 |
| wall | 83.5 s | **56.2 s** |

(An earlier 4-question run gave 0.908/0.908 against 0.784/0.660 -- a much wider gap on a
much smaller sample, which is itself the argument for not quoting either as a ranking.)

**How to read that, honestly.**

- **`refusal_pass` is 1 / 2 on BOTH runtimes, and that is the strongest single line on the
  card.** The remaining failure is the one CLAUDE.md already attributes to the persona rather
  than the agent: `feynman-explainer` is *designed* to name the gap rather than decline, which
  is pedagogically right and structurally an answer (PRD open item 16). A new runtime landing
  on the same idiosyncratic result as the old one is better evidence of parity than any mean.
- **Every metric's `n` equals `scored_count`**, 8 of 10, with the two refusal rows correctly
  excluded from all four means. The footnote is true -- which CLAUDE.md records as the
  exception rather than the rule, since run 1's answer relevance was a mean over a SINGLE
  value while the card claimed 8.
- **ADK was 33% faster** (56.2 s against 83.5 s for the same ten questions) and marginally
  cheaper, on one more tool call. Not a claim that ADK is faster in general -- one run, and
  provider routing alone swings an identical request by ~3.5x -- but it rules out the obvious
  fear that a framework loop costs latency.
- **It is not a ranking, and must not be quoted as one.** Generation runs at temperature 1.0,
  every run re-asks the questions, and n is 8 scored rows. What this supports is *"the ADK
  runtime is in the same band, not broken"* -- which is the assertion the harness actually
  makes (faithfulness within 0.25; measured delta -0.078).
- **ADK made one more tool call than langchain over the same ten questions** (5 against 4),
  which is the likeliest driver of the faithfulness delta rather than worse grounding: more
  retrieved material means more statements for faithfulness to decompose, and a longer answer
  is a bigger denominator. It is also the deterministic-forcer behaviour change working as
  designed -- and CLAUDE.md's standing finding is that faithfulness structurally punishes a
  teaching persona for the analogy and the comprehension check it exists to produce.
- **Context precision and recall are both exactly 1.000 on both engines**, which CLAUDE.md
  says to read as *not yet measured* rather than as excellent -- this corpus is one document
  and retrieval cannot fail on it.

A real ranking needs the full eval run on a re-baselined golden set, per section 6.

---

## 7. Ship

Merge with `AGENT_RUNTIME=langchain` as the default. Flipping the default is a **separate**
config edit, and deliberately not part of this change set — the whole point of the ROUTE
convention is that the flip is one line and reversible.
