# Building a change set — audit to shipped

The **outer** loop. [loop.md](loop.md) is the pattern for one feature where the *model*
decides something; this is the pattern for work that is bigger than one prompt — several
features, a new table, a new API surface — of any kind, model-decided or not.

The two compose in one direction:

```
build.md      audit -> plan -> decompose into feature files -> build -> verify -> ship
                                         |
                                         +-- each feature that is model-decided
                                             opens its session with loop-prompt.md
```

[build-prompt.md](build-prompt.md) is the companion that turns each phase below into a
prompt, the same way [loop-prompt.md](loop-prompt.md) does for one feature.

**Why the ceremony.** A first plan is architecture at high altitude. It reads as complete
and is not: hand it straight to a builder and the parts nobody wrote down get invented
independently, three times, differently. The evidence that this works is in this folder —
[00-IMPLEMENTATION-PLAN.md](00-IMPLEMENTATION-PLAN.md) carries the audit, the shared
contracts and the build sequence for the agentic-tools change set, and its §7a is titled
**"As built — where the plan was wrong."** A plan you can be wrong against is doing its job.
A plan you cannot check is decoration.

---

## 1. Routing — is this actually a change set?

Answer this first. Every wrong answer here costs a session.

| The change | Go to |
|---|---|
| Several features, or a new table / API surface / frontend view | **This document** |
| One feature, and the **model** decides something (tool, retry, detector) | [loop-prompt.md](loop-prompt.md) |
| One feature, fully deterministic | §9 short form below, or just build it |
| A threshold, or whether something should branch | [loop.md §1](loop.md) — if a number decides it, write the `if` |
| A bug in an existing tool | [loop.md §7](loop.md) — where the code lives |
| Pure UI, no new data | [05-ui-ux-overhaul.md](05-ui-ux-overhaul.md) reasoning, [07-workspace-shell.md](07-workspace-shell.md) shape, `scripts/ui_check.py` proof |
| Region, plan, index, migration strategy | [PRD.md](../PRD.md) §7 and [CLAUDE.md](../CLAUDE.md) — several are expensive to reverse |

**The failure this table prevents is over-applying the ceremony**, not under-applying it. A
detector tweak does not need a folder. [loop-prompt.md](loop-prompt.md)'s short form exists
for exactly that, and the four-time marker-list bug was four *small* changes.

---

## 2. Phase 1 — Audit, before any plan

**The audit's best outcome is a smaller change set, and that is not a consolation prize.**

[11-orchestrator-and-self-check.md](11-orchestrator-and-self-check.md) opened with a pattern
audit and concluded that three of the five catalogue patterns had already shipped here, and
that routing between *sources* was **architecturally closed** — one namespace per agent,
`Agent.namespace` derived, `SearchCorpusArgs` carrying exactly one field so a prompt-injected
model has nothing to name. Building it would have meant *adding back the parameter whose
omission is the security property*. The audit deleted more work than the plan added.

Three deliverables, and none of them is prose about intent:

1. **What exists**, at `file:line`. [00 §2.2](00-IMPLEMENTATION-PLAN.md) pastes the exact
   signatures the build writes against. Copying a signature into the plan is what makes the
   plan checkable later.
2. **What is closed**, and by which property. Not "we decided not to" — *which invariant
   forbids it*. Those are the entries that stop the same idea returning in six weeks.
3. **What the change reduces to** once 1 and 2 are subtracted.

Non-negotiable inputs to this phase:

- **Do not re-derive [PRD §7](../PRD.md) hard constraints or the §10 decisions table.** They
  were made deliberately and several are expensive to reverse.
- **Query the LangChain MCP servers first**, not after an import fails. 1.x moved symbols
  with no deprecation shim, so a stale import raises `ModuleNotFoundError` and reads like a
  missing dependency. Two relocations here were found the slow way; both were one query.
- **If the change touches a model request, check endpoints before assuming a parameter
  routes.** `supported_parameters` is a union across providers and tells you what will
  *route*, never what will *execute* — and neither tells you what langchain-openai added on
  the way out. That library has now injected an unrequested parameter three times.
- **Read [06-test-plan.md](06-test-plan.md) §1–4 now**, not at verification time. You are
  about to promise acceptance criteria, and §4 of this document requires each one to name a
  harness that already exists.

---

## 3. Phase 2 — The plan file

One file. It owns everything the feature files are allowed to assume, and the feature files
**do not repeat it** — that sentence is in [the folder README](README.md) and it is a
correctness rule, not tidiness. A contract stated twice drifts, and the copy that drifts is
never the one you are reading.

The proven section set, from [00-IMPLEMENTATION-PLAN.md](00-IMPLEMENTATION-PLAN.md):

| § | Holds | Why it is in the plan and not a feature file |
|---|---|---|
| Audit | Phase 1's output, with signatures | Every feature writes against it |
| Architecture after the change | The end state in one diagram | Otherwise each feature invents its own half |
| **Shared contracts** | New settings, **new trace event types**, schema + migration, API surface, frontend contract | These are the ones two features touch |
| Build sequence | Order, lowest layer first | Order is a decision; leaving it implicit means re-deciding it every session |
| Risk register | What could go wrong, and the tell | |
| Definition of done | The command list that must be green | |
| **What this deliberately does not do** | The audit's closed items | Stops the deleted work returning |

**Settle the migration in the plan, never in a feature file.** One migration for the whole
change set, written once. Two features each adding a column means two revisions racing for
the same `down_revision`.

**After it ships, add "As built — where the plan was wrong."** [00 §7a](00-IMPLEMENTATION-PLAN.md)
is the highest-value section in that document because it is the only one written with
hindsight, and it is what makes the next plan better rather than merely longer.

---

## 4. Phase 3 — Decompose into a folder

### The folder

A change set gets its own directory, numbered on from the last:

```
new features/
├── loop.md  loop-prompt.md  build.md  build-prompt.md    living references
├── 00-IMPLEMENTATION-PLAN.md ... 11-...md                change set 1 — shipped, flat
└── 12-<slug>/                                            change set 2
    ├── PLAN.md            audit, contracts, sequence, done
    ├── 01-<feature>.md    one file per feature
    ├── 02-<feature>.md
    └── ...
```

**Do not retro-fit `00`–`11` into a subfolder.** [CLAUDE.md](../CLAUDE.md),
[PRD.md](../PRD.md), [README.md](README.md) and [the root README](../README.md) all link them
by relative path, and there are dozens of those links. The flat set is change set 1; it stays
where it is. New work is where the structure changes.

**Un-numbered means living, numbered means a record of a change.** That is the existing
convention and it is the one thing that lets a reader tell instruction from archive at a
glance. Keep it.

### Each feature file

| Section | Content |
|---|---|
| What the user gets | Two sentences, plain terms. A goal stated plainly is a goal you can test against |
| Technical detail | Enough that a fresh session with only this file plus `PLAN.md` can build it |
| Contracts consumed | **By reference into `PLAN.md`.** Never restated |
| **Acceptance criteria** | See below — each names a harness case |
| **What must keep working** | The regression assertion. See below |

### Acceptance criteria — the rule that makes this different

> **An acceptance criterion names a harness file and a case id. If you cannot name one, it
> is a wish, and it will not be executed.**

This is not a stylistic preference. Feature 05's acceptance criteria were written in prose,
and [07-workspace-shell.md:385](07-workspace-shell.md) records that they were **"specified in
prose and never executed"** — for two documents, until `scripts/ui_check.py` was written and
finally ran them. Prose criteria feel like rigour and are inert.

So each criterion reads like `A4 — scripts/agentic_check.py S14: with tools off, the answer
is byte-identical to the classic path`. Pick the **lowest** layer that can prove it — the
first five harnesses need no database, no provider and no browser, and take seconds:

| Layer | Harness | Proves |
|---|---|---|
| 1 | `sandbox_check.py`, `ledger_check.py`, `refusal_check.py`, `route_specialist_check.py`, `llm_check.py` | Detectors, contracts, request shape. No DB, no network |
| 1.5 | `embed_check.py`, `goldenset_check.py`, `rewrite_check.py` | Network, no DB |
| 2 | `npm test` | Component behaviour |
| 3 | `agentic_check.py` | Live scenarios, DB + providers |
| 4 | `ui_check.py`, `mention_popup_check.py` | Browser, both servers up |

And the criterion must **make the feature necessary**, per [loop.md §5](loop.md). Scenario S3
passed twice while proving nothing — first because the fixtures chunked to one chunk per
file, then because `retrieve_k=3` over seven chunks still returned both topics. It only
became a real finding when the scenario starved retrieval to `k=1` **itself**. So a case owns
the conditions it needs and restores them in a `finally`; it does not rely on the fixture's
defaults happening to be hostile enough.

### What must keep working

Every feature file names it. [loop-prompt.md:97](loop-prompt.md) puts this well: a prompt
that only describes the addition gets a suite that only tests the addition. S1 and S7 exist
solely to check the new feature did not eat the old one, and the standing form of it is:
**with the feature off, output is byte-identical to today — assert it.**

---

## 5. Phase 4 — Write the harness case before the feature

Add the case, **run it, watch it fail**, then build. A case written after the code is a case
written to pass — which is how S3 got green twice.

Three things that must exist before the first line of feature code:

1. **New trace event types in `EVENT_TYPES`.** `TraceRecorder.record` raises on an unknown
   type and that guard is the only gate; there is no migration, no CHECK constraint. Add the
   frontend entries in `TracePanel`'s two maps too — a missing one degrades *silently*.
2. **Fixture discipline.** Reuse the harness's own fixture agent; never let a run mint a new
   one per case. `agentic_check.py --setup` / `--run` / **`--cleanup`**, and cleanup is not
   optional: a leaked Pinecone namespace is a real cost, and the Builder plan's
   1,000-namespace cap **is** the maximum number of agents this deployment can hold.
3. **A check that the fixture reaches the state you assert on.** `ui_check.py` passes with the
   `@mention` popup never rendering, because its fixture agent has no roster and nothing types
   `@`. That is why `mention_popup_check.py` is a second file. **A check that cannot fail
   reports success**, and it reports it in green, forever.

---

## 6. Phase 5 — Build, one feature per session

**Order comes from the plan's build sequence, lowest layer first.** A change in `app/tools/`
or `app/rag/` invalidates every layer above it, so building top-down means re-verifying the
top twice.

**Open each session with `PLAN.md` plus exactly one feature file, and clear between
features.** This is not ritual context hygiene — [CLAUDE.md](../CLAUDE.md) is 114 KB and
auto-loaded, [PRD.md](../PRD.md) is 77 KB. Loading five feature files means the model reasons
about five, and the interactions it invents between them are not in any plan.

**If the feature is model-decided, this is the hand-off point**: open that session with
[loop-prompt.md](loop-prompt.md)'s template instead of a plain build prompt. Its five
questions — is this a tool at all, what does it close over, what triggers it when the model
will not reach for it, what does a false positive cost, what makes it necessary — are cheap
on paper and expensive after the loop exists.

---

## 7. Phase 6 — Verify low to high, then look with your eyes

Run the ladder in order. **There is no CI; every one of these is run by hand**, so the
ordering is the protocol rather than a suggestion:

```bash
backend/.venv/Scripts/python.exe scripts/sandbox_check.py
backend/.venv/Scripts/python.exe scripts/ledger_check.py
backend/.venv/Scripts/python.exe scripts/refusal_check.py
backend/.venv/Scripts/python.exe scripts/route_specialist_check.py
backend/.venv/Scripts/python.exe scripts/llm_check.py
cd frontend && npm test
backend/.venv/Scripts/python.exe scripts/agentic_check.py --setup   # then --run, then --cleanup
cd frontend && npm run build
python scripts/ui_check.py                # global interpreter, both servers
python scripts/mention_popup_check.py     # global interpreter, both servers
```

Then the step that is not a command:

> ### Read one real output by eye. This is a phase, not advice.

**"Do not stop until everything is implemented and tested" is the single most expensive
sentence you can carry into this repo**, because a green suite here has been wrong five
separate times, in five different modules:

| What was green | What was actually true |
|---|---|
| `agentic_check.py` S3, twice | Retrieval returned the whole corpus; no question *could* need a second search |
| `ui_check.py`, zero console errors, zero overflow | The chat pane measured **24px with no visible thread** — the page rendered perfectly with no product on it |
| A concurrency measurement, n=9 | `gather` ran third in every trial, so it measured position, not concurrency. Running it first reversed the verdict |
| Every harness, after the DeepSeek swap | The model's raw `<｜DSML｜tool_calls>` markup was on screen, in the content channel, being read by the user |
| `refusal_pass`, four times | The agent refused correctly and the detector missed the phrasing — three-quarters of one scorecard blamed the agent for a marker list |

Every one of those was found by reading an answer, opening a page, or reordering a loop —
never by a passing assertion. The generalisation is [loop.md](loop.md) T2 and it is the whole
reason this repo writes things down: **an error-shaped check passes while the thing you
wanted silently did not happen.**

Distinguish environment failure from defect while you are here. `agentic_check.py` prints
`[rate]` rather than `[FAIL]` for an upstream refusal and does not exit non-zero, because a
suite that goes red when a provider says no teaches its reader to ignore red. Treat those
rows as **unmeasured**, never as passing.

---

## 8. Phase 7 — Ship

**A push to `main` is the deploy.** Both Render services build from it.

- **Apply the migration before the merge.** Then the start command's `alembic upgrade head`
  is a no-op rather than a first run against production traffic. That is how
  `bc307f5fc31f → d4e91c2a7b58` went out.
- **Re-check `pywin32` after any `pip freeze`.** The marker
  `pywin32==312; sys_platform == "win32"` has been flattened and restored **three times**,
  by three unrelated dependency additions. It is a property of `pip freeze`, not something to
  remember. `grep -n pywin32 backend/requirements.txt`.
- **If production behaves unlike local, compare env vars by VALUE, not by key.** Every
  required key was once present and two held different values from `.env`; the Cohere one was
  a stale *trial* key, rate-limited at ~10 calls/minute, surfacing as handouts stuck at
  `failed` — indistinguishable from a code defect. Hash to find candidates, then **test the
  key**; a hash flagged `PINECONE_API_KEY` as drift when both keys reached the same index.
- **When a deploy fails, hand back the log text verbatim.** Render's red entries name the
  cause far more often than a reconstruction from symptoms does.

---

## 9. Phase 8 — Fold the durable half out

The change shipped. The folder is now archive, and **this phase is what stops the folder
becoming sludge** — skipping it is precisely why a reader cannot tell instruction from record.

| What you learned | Where it goes |
|---|---|
| A gotcha that cost debugging time | [CLAUDE.md](../CLAUDE.md), under the failure it causes |
| An open item resolved, superseded or opened | [PRD.md](../PRD.md) §10 |
| Anything that changes how an evaluation is run or read | [EVAL.md](../EVAL.md) |
| The reusable shape of a model-decided feature | [loop.md](loop.md) — **edit it, never add a copy beside it** |
| The reusable shape of the *process* | This file — same rule |
| One row describing the change set | [README.md](README.md)'s table |

Then put the verification numbers in the change set's own `PLAN.md`, under **"As built — where
the plan was wrong"** (§3). They belong beside the plan they are evidence for, not in a
separate status file that goes stale the moment the next change lands.

**Watch which document a fresh clone actually sees.** `CLAUDE.md` is gitignored, so the
tracked entry point is [the root README](../README.md) — everything a newcomer must find has
to be reachable from there, and that includes this file. Check it after any change to the
folder's shape.

### Short form — a one or two feature change set

Skip the folder. Write one file, `new features/NN-<slug>.md`, holding the audit, the contracts
and the acceptance criteria together. Keep §4's criteria rule, §5's harness-first rule and
§7's read-it-by-eye step; those three are where the cost actually is. The folder is only
earned when features outnumber the sessions you want to spend.

---

## 10. The pattern in one paragraph

Audit before planning, and count a smaller change set as the best result. Put the shared
contracts — settings, trace event types, migration, API and frontend surface — in one plan
file that the feature files reference and never restate. Shatter the rest into one file per
feature in a numbered folder, each carrying acceptance criteria that **name a harness case**
rather than describing one, plus the thing that must keep working. Write those cases and watch
them fail before writing the feature. Build one feature per session, lowest layer first,
handing model-decided features to [loop-prompt.md](loop-prompt.md). Verify low to high, and
then — because a green suite in this repo has been wrong five times in five modules — open the
page and read one real answer. Ship with the migration already applied, then move the durable
half into CLAUDE.md, PRD.md, EVAL.md and loop.md, so the folder you just wrote can safely
become archive.
