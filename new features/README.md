# new features

Planning documents for three change sets: **agentic tools + Handouts** (`00`–`11`, flat),
**[12 — robust handouts](12-robust-handouts/PLAN.md)** and
**[13 — object storage](13-object-storage/PLAN.md)**. All three have shipped.

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

**Four documents here are living; every numbered one records a change.**
That distinction is the only thing letting a reader tell instruction from archive, so it is
carried in the filename: **un-numbered means living.**

**A numbered entry that has not shipped yet says so in the table, and nowhere else.** `00`–`11`,
`12-robust-handouts/` and `13-object-storage/` have all shipped. The convention used to read
"numbered means shipped", which had no way to describe a change set between decompose and ship —
[build.md §4](build.md) numbers the folder when it is created, not when it lands. The status
column is the fix.

They are two pairs, one nested inside the other. Each pair is a pattern plus the prompt that
opens a session following it — the pattern says what to get right, the prompt gets those
questions asked before the code exists rather than after.

```
build.md / build-prompt.md    the OUTER loop -- a change set, any kind
   audit -> plan -> decompose into a folder -> build -> verify -> ship -> fold out
        |
        +-- loop.md / loop-prompt.md    the INNER loop -- one feature the MODEL decides
```

Start at [build.md](build.md) for anything bigger than one prompt; it hands each
model-decided feature to [loop-prompt.md](loop-prompt.md), so the pairs compose rather than
compete. Go straight to [loop-prompt.md](loop-prompt.md) when the change is one feature and
the hard part is getting a model to act. If a *number* decides it, [loop.md §1](loop.md) says
write the `if` and skip both. CLAUDE.md points at all four.

**`build.md` was extracted after the fact**, from the change set numbered `00`–`11` below —
which is why `00-IMPLEMENTATION-PLAN.md` is the worked example of its §3 and the twelve files
beside it are the worked example of its §4. The procedure existed as an artifact long before
it existed as a procedure, and its two sharpest rules are corrections to how that went:
acceptance criteria must name a harness case (05's were prose and went unexecuted until 07),
and a green suite is not evidence (five modules, five times).

For the history: read [00-IMPLEMENTATION-PLAN.md](00-IMPLEMENTATION-PLAN.md) first. It holds
the audit, the shared contracts every other document depends on, and the build sequence. The
feature documents assume it and do not repeat it.

**[07-workspace-shell.md](07-workspace-shell.md) supersedes the layout half of
[05](05-ui-ux-overhaul.md)** — 05's five defects are fixed and are now regression assertions,
but its three-column chat tab was the thing that read as NotebookLM, and its acceptance
criteria were written in prose and never executed. 07 replaces the layout and adds the
harness. Read 05 for the tap-target and overflow reasoning, which still stands; read 07 for
the shape of the screen.

| Document | Status | What it covers |
|---|---|---|
| **[build.md](build.md)** | living | **The outer loop — living reference, START HERE for any new feature bigger than one prompt.** Audit before planning, shared contracts in one plan file, one feature file per feature with criteria that name a harness case, harness-first, verify low to high, then read one answer by eye |
| **[build-prompt.md](build-prompt.md)** | living | **How to open each of its six sessions** — one prompt per phase, a worked example on PRD open item 18, and when not to use it |
| **[loop.md](loop.md)** | living | **The agent loop as a reusable pattern — living reference, read before adding a tool** |
| **[loop-prompt.md](loop-prompt.md)** | living | **How to open a session that follows it — prompt structure, worked example, and when not to use it** |
| [00-IMPLEMENTATION-PLAN.md](00-IMPLEMENTATION-PLAN.md) | shipped | Audit, contracts, sequencing, risks, definition of done |
| [01-agentic-tool-loop.md](01-agentic-tool-loop.md) | shipped | The bounded loop, `ContextLedger`, trace events, termination |
| [02-code-interpreter.md](02-code-interpreter.md) | shipped | `run_python`, the sandbox, and **what it does not protect against** |
| [03-corpus-search-tool.md](03-corpus-search-tool.md) | shipped | `search_corpus`, and why it supersedes PRD open item 7 |
| [04-handouts-panel.md](04-handouts-panel.md) | shipped | The `handouts` table, four recipes, the job, the routes, the panel |
| [05-ui-ux-overhaul.md](05-ui-ux-overhaul.md) | shipped | Five real layout and tap-target defects, with file:line |
| [06-test-plan.md](06-test-plan.md) | shipped | Three test layers and the iteration protocol between them |
| [07-workspace-shell.md](07-workspace-shell.md) | shipped | The workspace shell, the editable settings sheet, and the de-NotebookLM pass — plus `scripts/ui_check.py`, which finally executes 05's acceptance criteria |
| [08-streaming-and-followups.md](08-streaming-and-followups.md) | shipped | SSE streaming, and the two defects that came from how the work was divided rather than how it was written — including why an unadvertised parameter sometimes 404s and sometimes does not |
| **[09-deepseek-agentic.md](09-deepseek-agentic.md)** | shipped | The move to `deepseek/deepseek-v4-flash-0731` — **the change that inverted [loop.md](loop.md) T1**, why turning reasoning off is only safe because a Gemma-era paragraph survives, and the two new layer-1 harnesses |
| **[11-orchestrator-and-self-check.md](11-orchestrator-and-self-check.md)** | shipped | The `adaptive-tutor` template, `@mentions`, and self-evaluation — **the first change here that applies [loop.md](loop.md) by NOT building three of the four mechanisms as tools.** Also the pattern audit: three of the five catalogue patterns already shipped, source routing is architecturally closed and building it would undo a security property, and the honest weighting that says this system's measured errors are generation-side rather than retrieval-side |
| **[10-routing-and-embeddings.md](10-routing-and-embeddings.md)** | shipped | Embeddings move to OpenRouter with **no re-ingest** (same model, same space, cosine 1.000000) and the four kwargs that are each a different 400; the golden set moves to a third vendor for judge independence; the DeepSeek provider pin recorded as a **NO_GO with evidence**; and the rewriter losing its trigger — including the acronym bullet that **fabricated**, was fixed into a conditional that measured 5/5, and was **removed anyway**, with the grounded version deferred rather than guessed at |
| **[12-robust-handouts/](12-robust-handouts/PLAN.md)** | shipped | **The first change set to use [build.md](build.md) as a procedure rather than produce it.** Nothing between the model's `prs.save()` and a downloadable handout ever opens the bytes — measured, a zero-slide deck is 27,387 bytes and 28 bytes of `PK` junk is a `ready` handout, and both pass every assertion in the repository. The fix is a **third branch** on a retry trigger that is already [loop.md](loop.md) T2-correct, plus the three defects the audit turned up beside it: a deck written from **three chunks**, `stderr` on a successful run that nobody has ever seen, and truncation that retries at the same cap. Its audit deleted more than its plan adds, and **two of its acceptance criteria already existed in prose and had never been executed** |

---

**Relationship to the repository's other documents.** [PRD.md](../PRD.md) remains the
specification and [CLAUDE.md](../CLAUDE.md) the operational companion; the numbered files
here are neither. They are a plan for one change, and once it shipped the durable half moved
out:

- gotchas discovered while building -> `CLAUDE.md`
- open items resolved or superseded (7) and opened (21-25) -> `PRD.md` §10
- anything that changes how an evaluation is run or read -> `EVAL.md`
- **the reusable shape of a model-decided feature -> [loop.md](loop.md)**, which stays here
- **the reusable shape of the process that produced it -> [build.md](build.md)**, likewise

That last line is why this folder is not purely an archive. The numbered documents are a
record of how one change was sequenced; `loop.md` is what that change taught, written for the
next one. If a future build makes it wrong, edit it — do not add a second copy beside it.

---

**[13-object-storage/](13-object-storage/PLAN.md)** | shipped | Bytes leave Postgres for a
private Cloudflare R2 bucket -- handout files, and original uploads for the first time -- with
the download route answering 302 to a presigned URL so the bytes never enter the single uvicorn
worker. Three things make it worth reading past the plumbing. Its audit found the constraint it
was relaxing was **misattributed**: four code sites cite "PRD section 7" for *"original uploads
are never stored"* and section 7 never contained it, while the one place that did discuss it
over-claimed that `chunks.text` makes re-CHUNKING possible -- which it does not, because chunk
boundaries are lossy. It deliberately does **not** drop `handouts.content`, because that column
is simultaneously the rollback, the reason `agentic_check` S11 still has a subject, and the
blue/green discipline `migrate_index.py` already applies to a Pinecone index. And its harness
found a real defect before the code had a caller -- `delete_quietly`, documented as never
raising, caught only `StorageError` while its callee wrapped only `ClientError` -- alongside
**two false reds of its own**, both substring assertions standing in for semantic ones, one of
which was tripped by the docstring of the guard it was testing.

---

**[14-admin-observability/](14-admin-observability/PLAN.md)** | shipped | An admin console —
users, agents, full conversations, eval runs, and what all of it cost. **Its audit is the
reason it is small.** The brief asked whether LiteLLM should be deployed to track token
spend; the measurement said no, and not for the reason anyone expected. OpenRouter has
always returned the real cost of every call, and `langchain-openai` discards it on the
streaming path (`_create_usage_metadata`, base.py:4175) — so the 1.7 MB price table LiteLLM
ships to *estimate* cost is redundant against a number already on the wire. The disqualifying
argument was not the second container or the second Postgres: **LiteLLM builds its own
request body**, which re-opens all five documented OpenRouter parameter traps, two of which
fail silently. Beside that, the audit found `require_admin`, `AdminUser`, `api_usage` and
both `queries.*_tokens` columns already built with **zero callers and zero rows** — so the
change set reduced to a callback on the one factory every model call passes through, and a
view. Three findings generalise. **A fifth parameter shape**: not a parameter wrongly sent,
but one that became *unnecessary* — `stream_usage` was declined as unprobed and measured
byte-identical to leaving it off, so **a deferral written down as a risk needs a re-check
date the way a measurement does**. **One person is two user rows**, because the dev-login
shim keys on `dev|<email>` precisely so it cannot collide with a real Google `sub` — which
makes "grant admin to this email" ambiguous, and granting by `sub` would make the console
reachable only by a human at a consent screen. And the **seventh** green-suite failure:
`admin_check.py` passed every case while a route returned 500 on every request, because a
layer-1 harness cannot prove a query *runs*, only that it was *written*. An **eighth** landed
after that sentence was written and is the one that changes practice: `metering_check.py`'s
ten cases were green while the golden-set drafter's spend was logged and never recorded,
because every case opened its *own* scope and then asserted attribution survived — so the
harness only ever tested call sites it wrote itself. **A harness cannot prove instrumentation
is COMPLETE, only that the instrumentation it was handed works**, and `--live` does not fix it:
a live case exercises what it invokes, and this was a call site nobody invoked. The answer is a
case that reads the application's own source — case 12 walks the call graph with `ast` and
fails on any entry point reaching `build_chat_model` outside a `meter_as`
