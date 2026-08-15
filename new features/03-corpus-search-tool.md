# Feature 3 — `search_corpus`, retrieval the model drives

> Shared contracts: [00-IMPLEMENTATION-PLAN.md §4](00-IMPLEMENTATION-PLAN.md).
> The loop that calls this is [01-agentic-tool-loop.md](01-agentic-tool-loop.md).

---

## 1. What it is

The retriever, handed to the model as a tool. One argument: a search query. The model
decides when a second lookup is worth its latency, and what to look up.

```python
class SearchCorpusArgs(BaseModel):
    query: str = Field(description=
        "What to look up. Search for the specific missing thing, not the whole question "
        "again -- the context above already holds the first search's results.")
```

There is deliberately **no `k` argument, and no filename or namespace argument.** Each is
worth a sentence:

- **`k`** — the model has no calibrated sense of how many chunks it needs and would pick a
  number for the wrong reasons. `agent.retrieve_k` is an operator-tuned parameter that
  Stage 3 measures ([EVAL.md §5](../EVAL.md)); letting the model overwrite it per call
  would make retrieval parameters unmeasurable.
- **A namespace or agent argument** — PRD §7: *"the namespace comes from the session, never
  from the request body."* A model that can be prompt-injected by a retrieved document must
  not have a parameter that names another tenant's corpus. The tool closes over the `Agent`
  object; there is no argument through which a namespace could arrive.
- **A filename filter** — plausible, and out of scope. It would need metadata filtering in
  `aretrieve`, which is a change to the one place the retriever is built.

---

## 2. Implementation

```python
# app/tools/corpus.py

def build_corpus_tool(ctx: ToolContext) -> BaseTool:
    """`ctx` carries the Agent and the ContextLedger. Neither is a tool argument."""

    async def _search(query: str) -> str:
        retrieval = await aretrieve(ctx.agent, query)      # rerank per agent config
        markers = ctx.ledger.merge(retrieval)
        ...
        return rendered
```

**It goes through `aretrieve`, not `similarity_search`.** `retriever.py` is the one place a
retriever is constructed, and that is what keeps the Stage 1 -> Stage 2 change a one-liner.
A tool calling `similarity_search` directly would bypass reranking and punch through the
seam the whole codebase is organised around.

### The string returned to the model

```
Searched: "Ka-band downlink budget"
6 results, 3 new. Top similarity 0.71.

[7] 4-comms-lecture.md#12
    The Ka-band downlink is budgeted at 220 Mbps across ...

[8] 4-comms-lecture.md#13
    ... allocation is revisited during handover weeks, when ...

[2] 3-power-lecture.md#4   (already in context)
    ...
```

Three properties of that rendering matter:

1. **Markers come from the ledger**, so `[7]` is the same `[7]` the user will see in the
   citation list. This is the whole reason `ContextLedger` exists.
2. **Chunks already in context are labelled and shown anyway.** The model needs to see that
   its new query returned old material — that is the signal to stop searching. Hiding them
   produces a loop that searches three times for the same thing.
3. **The header carries the counts and the top score.** `3 new` and `Top similarity 0.71`
   are the numbers a model reasons about when deciding whether to search again.

Each snippet is truncated to ~400 characters. The full text is already in the ledger and
therefore in the rebuilt context block; repeating it in the tool result would double the
token cost of every search.

### Empty results are a success

```
Searched: "refund policy"
0 results above the retrieval floor. The corpus does not appear to cover this.
```

`ok=True`, `new_chunks=0`. This is not an error, and treating it as one would be a
correctness bug: *"the corpus does not cover this"* is one of the most valuable things this
system can determine, and the refusal behaviour in the system prompt depends on the model
being able to reach that conclusion.

---

## 3. Relationship to PRD open item 7

Item 7 specifies a **score-triggered rewrite loop**: if `top_score < agent.score_threshold`,
rewrite the question and retrieve again, up to `agent.max_rewrites` times. It has never been
built; `SCORE_CHECK` records `"action": "none -- no score-triggered rewrite loop yet"`.

This tool is a strictly more general mechanism, and the measurement in CLAUDE.md is the
reason to prefer it. On `3.1-lesson-gist.md`, on-topic questions scored 0.61-0.67 and
off-topic ones 0.49-0.58 — **overlapping bands**. A 0.5 threshold sits inside the noise, so
a score-triggered loop fires late on genuinely bad retrievals and early on good ones. The
model reading the retrieved text can tell "this is about the wrong thing" far more reliably
than a threshold sitting in a distribution with no separation.

So:

- `score_threshold` stays on the agent, stays in `SCORE_CHECK`, and stays **advisory**. Its
  `"governs": "rewrite"` note stays true — it just now describes a signal the model is shown
  rather than a branch the code takes.
- `SCORE_CHECK`'s payload changes `action` from the "no loop yet" string to
  `"advisory -- the agent loop decides"` when tools are on, and keeps the old string when
  they are off. **A stale trace string that says a feature does not exist, after it does, is
  a documentation bug in the most-read part of the product.**
- PRD item 7 is updated to record that the score-triggered loop was superseded, with the
  overlapping-bands measurement as the reason, rather than left open.

`agent.max_rewrites` is untouched and still governs the history-aware contextualiser.

---

## 4. Cost

A search is one embedding call plus one Pinecone query plus, when reranking is on, one
Cohere call. Measured previously on this corpus: embed 365 ms, Pinecone k=20 394 ms, Cohere
rerank ~830 ms — so roughly **1.6 s per tool call** with reranking, 0.8 s without.

Against a persona turn measured at 6.3 s since the move to OpenRouter, one extra search is a
~25% increase and two is ~50%. That is the honest cost, and it is why `max_tool_steps`
defaults to 3 rather than 8: the model gets enough room for a genuine multi-hop question and
not enough to explore.

The model is told the cost in `TOOL_GUIDANCE` ("use them only when they earn their cost")
because a model that does not know a tool is expensive will call it every turn.

---

## 5. Files

| File | Change |
|---|---|
| `app/tools/corpus.py` | **new** — `SearchCorpusArgs`, `build_corpus_tool` |
| `app/tools/registry.py` | **new** — `ToolContext`, `build_tools(agent, ctx)` |
| `app/api/ask.py` | `SCORE_CHECK` payload `action` reflects whether tools are on |

```python
# app/tools/registry.py
@dataclass
class ToolContext:
    agent: Agent
    ledger: ContextLedger
    artifacts: list[SandboxArtifact] = field(default_factory=list)

def build_tools(ctx: ToolContext) -> list[BaseTool]:
    """Order is stable: search first, then python. Some models weight the first tool in
    the list more heavily, and search is the cheaper one to be biased toward."""
    return [build_corpus_tool(ctx), build_python_tool(ctx)]
```

---

## 6. Acceptance

1. `"compare the power budget with the comms budget"` on a two-document corpus -> at least one `TOOL_CALL`, `new_chunks > 0`, and the answer cites markers from both documents.
2. A search returning only chunks already in context -> `new_chunks: 0`, the markers still render as `(already in context)`, and the model stops searching rather than repeating.
3. An off-corpus search -> `ok=True`, `0 results`, and the turn ends in a refusal rather than an error.
4. `SearchCorpusArgs` has exactly one field. Adding `namespace`, `agent_id` or `k` fails review.
5. `AskOut.citations` markers are contiguous 1..N and every `[n]` in the answer resolves, after a turn with two searches.
