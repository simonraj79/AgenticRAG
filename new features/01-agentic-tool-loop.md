# Feature 1 — the agentic tool loop

> Shared contracts live in [00-IMPLEMENTATION-PLAN.md §4](00-IMPLEMENTATION-PLAN.md).
> This document covers the loop itself: where it slots in, how it terminates, how citations
> survive it, and what it writes to the trace.

---

## 1. The problem it solves

The pipeline is a straight line: contextualise, retrieve once, generate once. That shape
cannot answer a question needing two lookups. Ask *"how does the propulsion budget compare
with the comms budget?"* and one embedding of one question retrieves whichever of the two
sections is closer in vector space, then the model answers confidently about half the
question. No amount of reranking fixes it — the missing text was never a candidate.

PRD open item 7 specifies a score-triggered rewrite loop as the Stage 2 answer. That loop
retries the *same* question when the top score is low. It would not help here: both
retrievals score fine, they are just each half an answer.

**Letting the model call the retriever solves both.** It rewrites because it decided to, and
it searches twice because it noticed there were two things to look up. The score-triggered
loop becomes a special case of a general mechanism, which is why this supersedes item 7
rather than sitting beside it.

---

## 2. Where it goes

`app/rag/agent_loop.py` — a new module. `pipeline.answer_question` grows one branch at its
generation step and nothing else changes:

```python
# app/rag/pipeline.py, inside answer_question, replacing the single chain.ainvoke

ledger = ContextLedger.seed(retrieval)

if _tools_active(agent):
    loop = await run_agent_loop(
        agent=agent,
        question=search_query,
        ledger=ledger,
        system_prompt=agent.system_prompt or DEFAULT_SYSTEM_PROMPT,
        model=model,
        max_steps=agent.max_tool_steps,
    )
    text = loop.text
    tool_calls, artifacts = loop.tool_calls, loop.artifacts
    tool_ms, tool_steps, stopped_reason = loop.tool_ms, loop.steps, loop.stopped_reason
    generation_ms = loop.generation_ms
else:
    text = await chain.ainvoke({"context": format_context(retrieval.documents),
                                "question": search_query})
    # ... unchanged
```

```python
def _tools_active(agent: Agent) -> bool:
    """Both gates must pass. The setting is an operator kill switch; the column is the
    per-agent choice. Either one off means the classic path, which is what keeps an
    already-measured agent reproducible."""
    return settings.agent_tools_enabled and bool(agent.tools_enabled)
```

**Retrieval still runs before the loop.** It is not replaced by the tool. Three reasons:
the first search is unconditional in every real turn, so making the model ask for it wastes
a round trip; the `RETRIEVE` trace event and its `SCORE_CHECK` keep working unchanged; and
an agent that calls no tools produces exactly today's answer from exactly today's context.
The tool is for the *second* search onward.

---

## 3. `ContextLedger` — why citations survive

Citations are `[n]` markers resolved against `AskOut.citations`, which `run_turn` builds
1-based from `result.documents`. If a tool adds documents mid-turn, three things must hold
or the markers lie:

1. A chunk retrieved twice must keep **one** marker, not two.
2. A marker shown to the model inside a tool result must be the **same** marker the user
   sees in the citation list.
3. Order must be stable, because position *is* the marker.

```python
@dataclass
class LedgerEntry:
    marker: int                 # 1-based, assigned once, never reassigned
    document: Document
    chunk_id: str
    similarity: float | None
    rerank: float | None
    source: str                 # "initial" | "tool"

class ContextLedger:
    """Ordered, deduped by chunk_id. Owns the marker numbering for the whole turn."""

    @classmethod
    def seed(cls, retrieval: Retrieval) -> ContextLedger: ...

    def merge(self, retrieval: Retrieval) -> list[int]:
        """Add anything new; return the markers of everything in `retrieval`,
        existing entries included. Callers render those markers to the model."""

    def format_context(self) -> str:
        """`[n] filename#chunk_index\\n<text>` blocks -- same shape as
        pipeline.format_context, so the prompt does not change form mid-turn."""

    @property
    def documents(self) -> list[Document]: ...      # marker order == list order
```

`merge` returning markers for *existing* entries too is the load-bearing detail. When the
model searches for something it already has, the tool result says "these are chunks [2] and
[5]" rather than presenting them as new — so the model learns it is going in circles, and
the marker the user sees stays correct.

`pipeline.format_context` stays where it is and keeps its signature; `ContextLedger.format_context`
delegates to it so there is one renderer, not two.

---

## 4. The loop

```python
@dataclass
class LoopResult:
    text: str
    tool_calls: list[ToolInvocation]
    artifacts: list[SandboxArtifact]
    steps: int
    tool_ms: int
    generation_ms: int
    stopped_reason: str | None      # "max_steps" | "tool_error" | None

async def run_agent_loop(*, agent, question, ledger, system_prompt,
                         model, max_steps) -> LoopResult
```

```
messages = [SystemMessage(system_prompt + TOOL_GUIDANCE),
            HumanMessage(USER_TEMPLATE.format(context=ledger.format_context(),
                                              question=question))]

bound = model.bind_tools(tools)          # tools = registry.build_tools(agent, ctx)

for step in 1..max_steps:
    ai = await bound.ainvoke(messages)          # time -> generation_ms
    messages.append(ai)
    if not ai.tool_calls:
        return LoopResult(text=ai.text(), ...)  # normal exit

    for call in ai.tool_calls:                  # sequential, not gathered -- see below
        result = await execute(call)            # time -> tool_ms
        messages.append(ToolMessage(content=result.payload_for_model,
                                    tool_call_id=call["id"]))
    # a search may have added context; refresh the human turn so the model
    # sees new chunks in the same shape as the originals
    messages[1] = HumanMessage(USER_TEMPLATE.format(context=ledger.format_context(),
                                                    question=question))

# budget spent -- force an answer
final = await model.bind_tools(tools, tool_choice="none").ainvoke(messages)
return LoopResult(text=final.text(), stopped_reason="max_steps", ...)
```

### Four decisions in that sketch

**Tool calls execute sequentially, not with `asyncio.gather`.** `disabled_params={"parallel_tool_calls": None}` means langchain-openai never asks for parallel calls, so a batch of more than one is already unusual; and `run_python` spawns a subprocess on a single-worker deployment. Sequential is both correct and the safe shape.

**The final invoke uses `tool_choice="none"`, not a bare model.** `tool_choice` is a parameter OpenRouter routes on; dropping `tools` entirely on the last call would change the routing constraint mid-turn and risk a different provider answering than the one that made the calls. Keeping the tools bound and forbidding their use is one parameter set for the whole turn.

**The human message is rebuilt, not appended to.** Appending a second context block leaves the model with two, one stale. Rebuilding keeps exactly one context block that always reflects the ledger.

**A tool that raises never propagates.** `execute` catches everything, returns
`ok=False`, and feeds the model a `ToolMessage` describing the failure. A model that wrote
bad Python gets to see the traceback and fix it on the next step — which is the single most
valuable behaviour a code interpreter has, and it is lost the moment an exception escapes.
`stopped_reason="tool_error"` is set only when the *loop* gives up, not per failed call.

### `TOOL_GUIDANCE`

Appended to the persona prompt, never replacing it. Persona text is user-editable and
`SystemMessage(content=...)` is used rather than the `("system", ...)` tuple form precisely
so braces in it are not parsed as template variables — the guidance is concatenated after
the same way.

```
You have tools. Use them only when they earn their cost.

- search_corpus: run another search when the question has more than one part, when the
  context above does not cover something you were asked, or when a term in the question
  does not appear in the context. Search for the missing thing, not the whole question again.
- run_python: write and run Python when the user wants a chart, a slide deck, a table or a
  file. Put the numbers in the code as literals -- you have no filesystem and no network.

Rules that do not change: answer only from the context, cite with [n] markers, and say so
plainly when the context does not cover something. A tool result is context too, and is
cited the same way. Never claim to have made a file unless run_python returned one.
```

That last sentence exists because a model asked for a chart will happily *describe* the
chart it did not make.

---

## 5. Trace events

`run_turn` emits these from `result.tool_calls`, **after** `RETRIEVE`/`SCORE_CHECK`/`RERANK`
and **before** `GENERATE`. The recorder is a plain counter, so inserting into the middle of
the sequence is safe.

```python
for inv in result.tool_calls:
    trace.record(TOOL_CALL, payload={"step": inv.step, "tool": inv.tool,
                                     "call_id": inv.call_id, "args": inv.args})
    if inv.ok:
        trace.record(TOOL_RESULT, payload={"step": inv.step, "tool": inv.tool,
                                           "call_id": inv.call_id, "ok": True,
                                           "summary": inv.summary, **inv.detail},
                     duration_ms=inv.duration_ms)
    else:
        trace.record(TOOL_ERROR, payload={"step": inv.step, "tool": inv.tool,
                                          "call_id": inv.call_id, "ok": False,
                                          "error": inv.error},
                     duration_ms=inv.duration_ms)
```

`args` goes through `_jsonable`, which coerces anything unserialisable to `str` and never
raises — so a tool argument holding a 4 KB Python program lands in the payload intact and
is exactly what the trace panel should show.

`GENERATE`'s payload gains `{"tool_steps": n, "stopped_reason": ...}` and its `duration_ms`
stays `result.generation_ms` — model time only. The turn's total is
`contextualize_ms + retrieval_ms + tool_ms + generation_ms`, which now adds up instead of
overlapping.

---

## 6. Failure modes and what each does

| Failure | Behaviour |
|---|---|
| Model returns text and no tool call on step 1 | Normal exit. Identical to today's answer for an agent that had no reason to search |
| Model calls a tool that does not exist | `TOOL_ERROR`, `ToolMessage` naming the real tools, loop continues |
| Tool arguments fail schema validation | Same — pydantic's error text goes to the model, which is usually enough for it to retry correctly |
| `run_python` times out | `TOOL_ERROR` with `error_kind="timeout"`; model told the code took too long and to simplify |
| `search_corpus` returns nothing | `ok=True`, `summary="no matches"`, zero new markers. Not an error — "the corpus does not cover this" is a real answer |
| `max_steps` exhausted | Forced final invoke, `stopped_reason="max_steps"`. The user always gets an answer |
| OpenRouter 404 on the bound call | Propagates. This is a configuration fault, not a turn fault, and swallowing it would hide exactly the failure CLAUDE.md warns about three times |

---

## 7. Files

| File | Change |
|---|---|
| `app/rag/agent_loop.py` | **new** — `ContextLedger`, `ToolInvocation`, `LoopResult`, `run_agent_loop`, `TOOL_GUIDANCE` |
| `app/rag/pipeline.py` | branch at generation; `AnswerResult` gains 5 fields; `_tools_active` |
| `app/rag/trace.py` | `TOOL_CALL`, `TOOL_RESULT`, `TOOL_ERROR` + `EVENT_TYPES` |
| `app/api/ask.py` | emit tool events; `AskOut.tool_steps`; persist artifacts as handouts |
| `app/config.py` | `agent_tools_enabled`, `agent_max_tool_steps` |
| `app/db/models.py` | `agents.tools_enabled`, `agents.max_tool_steps` |
| `app/api/agents.py` | both columns in `AgentOut` / `AgentTunables` |

---

## 8. Acceptance

1. `tools_enabled=false` -> trace has exactly the six event types it has today, in the same order.
2. `tools_enabled=true`, simple question -> zero `TOOL_CALL` events, answer unchanged in shape.
3. Two-part question -> at least one `TOOL_CALL` for `search_corpus`; `TOOL_RESULT.new_chunks > 0`; the answer cites a marker that did not exist before the tool ran.
4. Deliberately broken Python -> `TOOL_ERROR`, then a `TOOL_CALL` on the next step with corrected code, then an answer. The self-correction is the test.
5. `max_tool_steps=1` with a question that wants two searches -> `stopped_reason="max_steps"` and an answer is still returned.
6. `contextualize_ms + retrieval_ms + tool_ms + generation_ms` is within 15% of `latency_ms`.
