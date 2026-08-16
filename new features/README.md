# new features

Planning documents for one change set: **agentic tools + Handouts**.

**[loop.md](loop.md) and [loop-prompt.md](loop-prompt.md) are the living documents here.**
Everything else records a change that has shipped. `loop.md` is the design pattern extracted
from it — read before adding a tool, a retry, or any feature where the model decides
something rather than the code deciding it. `loop-prompt.md` is the prompt structure for
starting that work, and the two are a pair: the pattern says what to get right, the prompt
gets those questions asked before the code exists rather than after. CLAUDE.md points at
both.

For the history: read [00-IMPLEMENTATION-PLAN.md](00-IMPLEMENTATION-PLAN.md) first. It holds
the audit, the shared contracts every other document depends on, and the build sequence. The
five feature documents assume it and do not repeat it.

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
