# new features

Planning documents for one change set: **agentic tools + Handouts**.

Read [00-IMPLEMENTATION-PLAN.md](00-IMPLEMENTATION-PLAN.md) first. It holds the audit, the
shared contracts every other document depends on, and the build sequence. The five feature
documents assume it and do not repeat it.

| Document | What it covers |
|---|---|
| [00-IMPLEMENTATION-PLAN.md](00-IMPLEMENTATION-PLAN.md) | Audit, contracts, sequencing, risks, definition of done |
| [01-agentic-tool-loop.md](01-agentic-tool-loop.md) | The bounded loop, `ContextLedger`, trace events, termination |
| [02-code-interpreter.md](02-code-interpreter.md) | `run_python`, the sandbox, and **what it does not protect against** |
| [03-corpus-search-tool.md](03-corpus-search-tool.md) | `search_corpus`, and why it supersedes PRD open item 7 |
| [04-handouts-panel.md](04-handouts-panel.md) | The `handouts` table, four recipes, the job, the routes, the panel |
| [05-ui-ux-overhaul.md](05-ui-ux-overhaul.md) | Five real layout and tap-target defects, with file:line |
| [06-test-plan.md](06-test-plan.md) | Three test layers and the iteration protocol between them |

---

**Relationship to the repository's other documents.** [PRD.md](../PRD.md) remains the
specification and [CLAUDE.md](../CLAUDE.md) the operational companion; these are neither.
They are a plan for one change, and once it has shipped the durable half moves out:

- gotchas discovered while building -> `CLAUDE.md`
- open items resolved or superseded (7, 13) -> `PRD.md` §10
- anything that changes how an evaluation is run or read -> `EVAL.md`

This folder is then a record of how the change was sequenced, not a live reference.
