# Prompt structure — building a change set

Companion to [build.md](build.md). That file is the procedure; this one is how to open each
session that follows it — the same relationship [loop-prompt.md](loop-prompt.md) has to
[loop.md](loop.md).

**Why one prompt per phase rather than one big one.** The phases have different failure
modes, and the expensive ones fail *quietly*. An audit that skips straight to designing
produces a plan for work that is already done — the source-routing audit in
[11](11-orchestrator-and-self-check.md) deleted a whole feature by asking first. A decompose
step that writes acceptance criteria in prose produces criteria nobody executes, which is
what happened to feature 05 for two documents running. Neither error announces itself, and
both are cheap to prevent by ending the session before the next one starts.

**Clear the conversation between phases.** [CLAUDE.md](../CLAUDE.md) is 114 KB and
auto-loaded; [PRD.md](../PRD.md) is 77 KB. Carrying phase 1's exploration into phase 4 buys
nothing and costs the context the build needs.

---

## The six sessions

| # | Session | Produces | The failure it prevents |
|---|---|---|---|
| 1 | **Audit** | What exists, what is closed, what remains | Planning work that already shipped |
| 2 | **Plan** | `PLAN.md` — contracts, sequence, done | Two features inventing the same contract differently |
| 3 | **Decompose** | The folder, one file per feature | Acceptance criteria that are prose, and therefore inert |
| 4 | **Build** (×N) | One feature | Five features' interactions invented in one context |
| 5 | **Verify** | Green ladder, and one answer read by eye | A suite that passes while the product is broken |
| 6 | **Ship & fold** | Deploy, then durable half moved out | The folder becoming indistinguishable from instruction |

---

## 1. Audit

```
Read `new features/build.md` section 2, then PRD.md section 7 and section 10.
Do not plan anything yet, and do not write code.

WHAT I'M CONSIDERING
<one or two sentences, plain terms, what the user would get>

AUDIT AND REPORT BACK ONLY
1. What already exists that does part of this? Cite file:line, and paste the
   exact signatures anything new would write against.
2. What part of this is architecturally CLOSED here -- not "we chose not to"
   but which invariant forbids it? Name the invariant.
3. What does the change reduce to once 1 and 2 are subtracted?
4. Does it touch a model request? If so check the OpenRouter endpoints
   before assuming any new parameter routes.
5. Does it touch LangChain? Query the docs/reference MCP servers first.

A smaller change set is the best outcome here, not a disappointing one.
Tell me what NOT to build before you tell me what to build.
```

The last two lines matter more than they look. Left implicit, a model reads "audit" as
"survey, then propose" and proposes the whole thing back.

---

## 2. Plan

```
Read `new features/build.md` section 3 and `new features/00-IMPLEMENTATION-PLAN.md`
for the shape -- match its section set, not its content.

From the audit, write `new features/<NN>-<slug>/PLAN.md` covering:
- the audit, with the signatures
- architecture after the change
- SHARED CONTRACTS: new settings, new trace EVENT_TYPES, the migration
  (ONE for the whole change set), API surface, frontend contract
- build sequence, lowest layer first
- risk register, with the tell for each
- definition of done, as the command list that must be green
- what this deliberately does NOT do, from the audit's closed items

The feature files will REFERENCE these contracts and must never restate them.
Anything two features both touch belongs here or it will drift.
```

---

## 3. Decompose

```
Read `new features/build.md` section 4.

Break PLAN.md into one file per feature in the same folder, numbered.
Each file: what the user gets, technical detail sufficient for a session
holding only PLAN.md plus that file, contracts consumed BY REFERENCE,
acceptance criteria, and what must keep working.

ACCEPTANCE CRITERIA -- the hard rule
Every criterion names a harness FILE and a CASE ID, e.g.
  "A3 -- scripts/agentic_check.py S14: with tools off, byte-identical output"
If you cannot name one, say so and stop rather than writing prose.
Feature 05's criteria were prose and went unexecuted for two documents.
Pick the LOWEST layer that can prove it (build.md section 4 has the table).

Each criterion must make the feature NECESSARY. A case owns the conditions
it needs and restores them in a finally -- do not rely on the fixture's
defaults happening to be hostile enough (loop.md section 5, scenario S3).

Every file also names the regression: what must keep working, and the
standing form is "with the feature off, output is byte-identical -- assert it".
```

---

## 4. Build one feature

Open with `PLAN.md` and **exactly one** feature file. Clear the conversation afterwards.

```
Read `new features/<NN>-<slug>/PLAN.md` and `<NN>-<slug>/0K-<feature>.md`.
Nothing else from that folder.

Write the acceptance-criteria harness cases FIRST. Run them. Show me them
FAILING. Then build the feature.

BEFORE THE FIRST LINE OF FEATURE CODE
- new trace event types into EVENT_TYPES (TraceRecorder.record raises on an
  unknown type, and that guard is the only gate)
- the matching TracePanel map entries -- a missing one degrades silently
- reuse the harness fixture agent; never mint a new one per case

NON-NEGOTIABLE
- with the feature off, output is byte-identical to today -- assert it
- tool failures come back as ToolMessages, never exceptions
- do not add any parameter to a tool-bound request (check endpoints first)
- ASCII in print(); extensions on TS imports; min-h-11 on controls
- one migration for the change set, already settled in PLAN.md
```

**If the feature is model-decided — a tool, a retry, a detector over model output — use
[loop-prompt.md](loop-prompt.md)'s template for this session instead.** That is the hand-off
point, and its five questions are the ones that get expensive after the loop exists.

---

## 5. Verify

```
Read `new features/build.md` section 7.

Run the ladder lowest layer first and show me the output, not a summary.
Then do the step that is not a command: open the page, or print a real
answer, and READ IT.

Do not tell me it works. A green suite here has been wrong five times in
five different modules -- S3 passed twice proving nothing, ui_check.py was
green on a 24px chat pane with no visible thread, a concurrency measurement
was an artefact of loop order, every harness was green while raw tool markup
was on screen, and refusal_pass blamed the agent for a marker list four times.

Show me the measurements. Tell me which assertion would have caught each
failure if it had been present, and whether it now is.
Treat any [rate] row as UNMEASURED, never as passing.
```

---

## 6. Ship and fold out

```
Read `new features/build.md` sections 8 and 9.

Apply the migration BEFORE the merge so the start command's
`alembic upgrade head` is a no-op rather than a first run against
production traffic. Then push to main -- that is the deploy.
Check pywin32's marker survived any pip freeze.

Then fold the durable half out, and tell me what went where:
- gotchas -> CLAUDE.md, filed under the failure they cause
- open items resolved/superseded/opened -> PRD.md section 10
- anything changing how an evaluation is read -> EVAL.md
- reusable shape of a model-decided feature -> loop.md (EDIT it, do not
  add a second copy)
- reusable shape of the process -> build.md, same rule
- one row -> new features/README.md's table
- verification numbers -> the change set's PLAN.md, "As built" section
- check the ROOT README still reaches everything a fresh clone needs
  (CLAUDE.md is gitignored; the root README is the tracked entry point)

If build.md turned out to be wrong, edit it. Do not add a doc beside it.
```

---

## Worked example

A change set this repo has actually deferred: **pinning eval contexts so a scorecard survives
its documents.** [PRD open item 18](../PRD.md) — `query_chunks.chunk_id` cascades from
`chunks`, which cascades from `documents`, so deleting one source file silently empties the
contexts that `context_precision` and `context_recall` read. The scores still render.

```
Read `new features/build.md` section 2, then PRD.md section 7 and section 10,
then EVAL.md.

WHAT I'M CONSIDERING
An eval run should stay reproducible after a document it cited is deleted --
today the scores survive and the evidence does not.

AUDIT AND REPORT BACK ONLY
1-5 as above. In particular: is the honest fix pinning contexts by copying
   the text, or refusing to delete a document an eval_run depends on? Those
   are different products, and PRD 18 records both without choosing.
2. (closed check) does anything already make this detectable -- is there a
   reason the cascade was chosen rather than inherited?

Tell me what NOT to build first.
```

**Two things worth copying out of that.** The audit question is *specific and names the fork
in the road* rather than asking for a survey — PRD 18 records two candidate designs and the
audit's job is to kill one. And it asks whether the current behaviour was **chosen**: a
cascade that was deliberate is a constraint, and one that was inherited from a default is a
bug. Those need opposite plans, and only the audit can tell them apart.

---

## Short form — a one or two feature change set

```
Read `new features/build.md` sections 4, 5 and 7.
<the change>

Write it as ONE file, new features/NN-<slug>.md -- no folder.
Acceptance criteria name a harness file and case id or they don't count.
Write the cases first and show me them failing.
Then verify low to high and read one real output by eye.
```

Short does not mean skipping the criteria rule. The folder is the only part that is optional;
§4, §5 and §7 of [build.md](build.md) are where the cost actually lands.

---

## When NOT to use this

This template front-loads questions about *scope and contracts*. If the change has neither —
one file, one behaviour, no new surface — the questions have no answers and asking them
produces confident noise.

- **One feature where the model decides something** — go straight to
  [loop-prompt.md](loop-prompt.md). Its questions are about model judgement, which is the
  harder problem and the one this template does not ask about.
- **A detector tweak, a retry condition, one more marker** — [loop-prompt.md](loop-prompt.md)'s
  own short form. The marker-list bug recurred four times as four *small* changes.
- **A threshold or a branch** — [loop.md §1](loop.md). If a number decides it, write the `if`.
- **Pure UI with no new data** — [07-workspace-shell.md](07-workspace-shell.md) and
  `scripts/ui_check.py`. There is nothing to decompose.
- **Infrastructure** — region, plan, index, migration strategy. [PRD §7](../PRD.md) and
  [CLAUDE.md](../CLAUDE.md); several of those are expensive to reverse and none of them
  decompose into features.
