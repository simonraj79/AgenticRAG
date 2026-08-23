# 02 — Trajectory capture

> Contracts consumed: [PLAN.md](PLAN.md) §4.1 (settings), §4.2 (payload keys), §4.3 (the
> trajectory contract). Not restated here.

## What the user gets

The trace panel starts showing what a tool actually returned, not just a one-line summary of it.
Everything downstream of this feature — the rubric, the admin tab — depends on a turn being
reconstructable after the fact.

## Why the missing half matters

Ragas' judged agent metrics read the trajectory through `MultiTurnSample.pretty_repr()`, which
renders every `ToolOutput` in full. Today the tool's returned content is discarded, so a
trajectory assembled from what is stored would show the judge `"3 results, 2 new for \"q\""`
where the model saw a numbered block of retrieved snippets.

That is not a smaller input, it is a **different** one, and a verdict reached on it would be a
verdict about a conversation that never happened.

## Technical detail

**`ToolInvocation` gains two fields** (`backend/app/rag/agent_loop.py:379-405`):

```python
content: str = ""          # ToolMessage.content, truncated
assistant_text: str = ""   # the text the AI emitted alongside the call
```

**`content` is stamped in `_execute`, one place.** That function has three exits — `failed()` for
an unknown tool or a pydantic rejection, the `except` for a raising tool, and the success path —
and all three already build the `ToolMessage`. Stamping at the single return point covers a
sandbox traceback exactly as it covers a search result, which is the case a reader most wants.

**`assistant_text` is stamped in the loop**, from the `ai` message of the step that produced the
call, after `_message_text()` — so the DeepSeek `U+FF5C` markup CLAUDE.md records is stripped
before it can reach a judge prompt.

**Truncation happens at the loop boundary**, not at the reader, so the cap bounds the JSONB write
*and* the judge prompt with one constant.

**`app/eval/trajectory.py`** is new and holds a pure function plus a thin async wrapper, per
PLAN.md §4.3. Two rules decide its correctness:

- **Pair by `call_id`, never by adjacency.** Rows are ordered by `step_index`, but a step can
  emit several calls and the loop dispatches them sequentially — adjacency is a coincidence of
  the current implementation and pairing on it would be a bug waiting for a parallel dispatch.
- **A missing result row is synthesised, never skipped.** Ragas' `field_validator` requires a
  `ToolMessage` to follow an `AIMessage` with non-empty `tool_calls`; dropping the result would
  leave the *call* in place and shorten the trajectory silently.

## Acceptance criteria

| # | Case | Asserts |
|---|---|---|
| B1 | `agent_loop_check.py` 10 | A successful tool call carries `invocation.content` equal to the `ToolMessage` content |
| B2 | `agent_loop_check.py` 11 | A **failed** tool call carries the error text as `content`. Written first and watched failing — the `failed()` path is a separate return |
| B3 | `agent_loop_check.py` 12 | `content` is truncated to `TRAJECTORY_MAX_TOOL_CONTENT_CHARS`, asserted with an over-long stub return |
| B4 | `agent_loop_check.py` 13 | `assistant_text` carries the step's assistant text, and is `""` when the model emitted none |
| B5 | `agent_metrics_check.py` 11 | A `MultiTurnSample` built from real fixture events constructs without raising |
| B6 | `agent_metrics_check.py` 12 | A trajectory with a missing `TOOL_RESULT` still constructs |
| B7 | `agent_metrics_check.py` 13 | Two calls in one step pair to the **right** results when their `call_id`s are interleaved in `step_index` order. **This is the case that fails if pairing is done by adjacency** |
| B8 | `agentic_check.py` S34 | A real turn's `TOOL_RESULT` payload carries a non-empty `content`. Layer 1 cannot prove this — it proves the key is written, not that a real tool run fills it |

## What must keep working

- **`agentic_check.py` S1** — with tools off, the answer and its trace are byte-identical to the
  classic path. This feature adds keys only inside the three tool events, which a tools-off turn
  never emits.
- **`agent_loop_check.py` cases 1–2** — the `LoopResult.steps` fix from PR #10. `ToolInvocation`
  gains fields; nothing about step counting moves.
- **`ledger_check.py`** — the citation-marker contract is untouched. `content` is recorded beside
  the markers, never instead of them.
