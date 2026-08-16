# new features

Planning documents for one change set: **agentic tools + Handouts**.

> **[09-deepseek-agentic.md](09-deepseek-agentic.md) is the one to read next after
> [loop.md](loop.md).** It is the first change here that made loop.md *wrong* rather
> than merely applying it: T1's "assume the model will not call your tool" is a fact
> about `google/gemma-4-31b-it`, and the model driving the loop today calls it 6/6
> unprompted. loop.md has been amended rather than duplicated, per the rule at the
> bottom of this file — but 09 carries the measurements and the machinery that had to
> become conditional as a result.
>
> **[10-routing-and-embeddings.md](10-routing-and-embeddings.md) is where the model
> layer finished moving.** Every model call in the project now goes through
> OpenRouter, embeddings included, with no re-ingest — and its central finding is
> about a prompt rather than a provider: a bullet telling the rewriter to "expand
> acronyms" contradicted the do-not-invent bullet four lines below it, and the model
> resolved the conflict by inventing. That is [loop.md](loop.md) T1's mechanism
> appearing in a module with no tool in it, which is why 10 is worth reading even if
> the routing half is irrelevant to you.
>
> **That bullet no longer exists.** It was rewritten as a conditional — expand only
> when the question or the conversation spells the term out — which measured 5/5 in
> both directions and was **removed anyway**: the feature's value was the first-turn
> case, where nothing has spelled anything out, so the gate that made it safe also
> made it fire almost never. The rewriter still repairs typos and shorthand and still
> resolves coreference; acronyms now pass through untouched under a flat prohibition.
> **A feature can pass its own harness and still be the wrong thing to ship** — §5.2
> is that record, and §6.1 keeps the version worth building instead.

**[loop.md](loop.md) and [loop-prompt.md](loop-prompt.md) are the living documents here.**
Everything else records a change that has shipped. `loop.md` is the design pattern extracted
from it — read before adding a tool, a retry, or any feature where the model decides
something rather than the code deciding it. `loop-prompt.md` is the prompt structure for
starting that work, and the two are a pair: the pattern says what to get right, the prompt
gets those questions asked before the code exists rather than after. CLAUDE.md points at
both.

For the history: read [00-IMPLEMENTATION-PLAN.md](00-IMPLEMENTATION-PLAN.md) first. It holds
the audit, the shared contracts every other document depends on, and the build sequence. The
feature documents assume it and do not repeat it.

**[07-workspace-shell.md](07-workspace-shell.md) supersedes the layout half of
[05](05-ui-ux-overhaul.md)** — 05's five defects are fixed and are now regression assertions,
but its three-column chat tab was the thing that read as NotebookLM, and its acceptance
criteria were written in prose and never executed. 07 replaces the layout and adds the
harness. Read 05 for the tap-target and overflow reasoning, which still stands; read 07 for
the shape of the screen.

| Document | What it covers |
|---|---|
| **[loop.md](loop.md)** | **The agent loop as a reusable pattern — living reference, read before adding a tool** |
| **[loop-prompt.md](loop-prompt.md)** | **How to open a session that follows it — prompt structure, worked example, and when not to use it** |
| [00-IMPLEMENTATION-PLAN.md](00-IMPLEMENTATION-PLAN.md) | Audit, contracts, sequencing, risks, definition of done |
| [01-agentic-tool-loop.md](01-agentic-tool-loop.md) | The bounded loop, `ContextLedger`, trace events, termination |
| [02-code-interpreter.md](02-code-interpreter.md) | `run_python`, the sandbox, and **what it does not protect against** |
| [03-corpus-search-tool.md](03-corpus-search-tool.md) | `search_corpus`, and why it supersedes PRD open item 7 |
| [04-handouts-panel.md](04-handouts-panel.md) | The `handouts` table, four recipes, the job, the routes, the panel |
| [05-ui-ux-overhaul.md](05-ui-ux-overhaul.md) | Five real layout and tap-target defects, with file:line |
| [06-test-plan.md](06-test-plan.md) | Three test layers and the iteration protocol between them |
| [07-workspace-shell.md](07-workspace-shell.md) | The workspace shell, the editable settings sheet, and the de-NotebookLM pass — plus `scripts/ui_check.py`, which finally executes 05's acceptance criteria |
| [08-streaming-and-followups.md](08-streaming-and-followups.md) | SSE streaming, and the two defects that came from how the work was divided rather than how it was written — including why an unadvertised parameter sometimes 404s and sometimes does not |
| **[09-deepseek-agentic.md](09-deepseek-agentic.md)** | The move to `deepseek/deepseek-v4-flash-0731` — **the change that inverted [loop.md](loop.md) T1**, why turning reasoning off is only safe because a Gemma-era paragraph survives, and the two new layer-1 harnesses |
| **[10-routing-and-embeddings.md](10-routing-and-embeddings.md)** | Embeddings move to OpenRouter with **no re-ingest** (same model, same space, cosine 1.000000) and the four kwargs that are each a different 400; the golden set moves to a third vendor for judge independence; the DeepSeek provider pin recorded as a **NO_GO with evidence**; and the rewriter losing its trigger — including the acronym bullet that **fabricated**, was fixed into a conditional that measured 5/5, and was **removed anyway**, with the grounded version deferred rather than guessed at |

---

**Relationship to the repository's other documents.** [PRD.md](../PRD.md) remains the
specification and [CLAUDE.md](../CLAUDE.md) the operational companion; the numbered files
here are neither. They are a plan for one change, and once it shipped the durable half moved
out:

- gotchas discovered while building -> `CLAUDE.md`
- open items resolved or superseded (7) and opened (21-25) -> `PRD.md` §10
- anything that changes how an evaluation is run or read -> `EVAL.md`
- **the reusable shape of the change itself -> [loop.md](loop.md)**, which stays here

That last line is why this folder is not purely an archive. The numbered documents are a
record of how one change was sequenced; `loop.md` is what that change taught, written for the
next one. If a future build makes it wrong, edit it — do not add a second copy beside it.
