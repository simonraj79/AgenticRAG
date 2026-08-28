# insights.md — what this project learned that outlives it

**This file is the transferable half.** [CLAUDE.md](CLAUDE.md) is indexed by *the failure it
causes* — a symptom, and the knob behind it — which is the right shape when you already have a
traceback and the wrong shape when you are about to make a decision and do not yet know what to
fear. [PRD.md](PRD.md) is the specification, [EVAL.md](EVAL.md) the operator's guide to Stage 3,
[new features/build.md](new%20features/build.md) and [loop.md](new%20features/loop.md) the two
process patterns. What is here is the residue: the claims that stay true after you delete this
codebase.

**Two reasons it is a separate file rather than a section.**

1. **`CLAUDE.md` is gitignored** ([.gitignore:46](.gitignore)), by request, so on GitHub it 404s
   and in a fresh clone it does not exist. Every durable lesson this project paid for lives in a
   file a new reader cannot see and a lost laptop takes with it. This file is tracked.
2. **These rules are not about this repo.** None of them mentions Pinecone's region lock or
   asyncpg's `sslmode`. Each is a claim about building software with a model in the loop, and
   each is here because it cost time here first.

**Every entry carries its evidence.** A rule with no incident behind it is an opinion, and this
project has a documented history of confident opinions that measurement reversed. Where the
detail lives is named at the end of each entry.

---

## I. Checks, harnesses and the things they let through

### 1. Trigger on the ABSENCE of the outcome you wanted, never on the PRESENCE of an error

The most transferable idea the project produced. An error-shaped check — *did it throw? did the
request fail? is the exit code zero?* — passes while the thing you wanted silently did not
happen. It has now fired in six modules, three of which contain no model decision at all:

| Where | The error-shaped check | What it let through |
|---|---|---|
| Agent loop | "did the turn refuse?" | The turn that answered **half** and gave up on the rest — precisely the turn needing a search |
| Handout retry | "did the code crash?" | Code that computes the chart correctly and forgets `savefig`. Exit 0, no file |
| Handout retry **again** | "is the artefact absent?" | A `.pptx` that is **present**, 27,387 bytes, opens fine, and has **zero slides** |
| Layout | console errors, failed requests, overflow | The chat pane collapsed to **24 px with 0 px of thread**. Rendered perfectly, threw nothing |
| Forced upload | "did the POST return 202?" | `force` dropped at the background handoff; the job re-deduplicated and wrote `failed` a minute later |
| Deploy | `GET /api/health` → **200** | The service had failed its last **three** deploys and could not start. The old instance keeps serving while the new one crash-loops |

**It is a ladder, not a step.** Row 3 is row 2 catching up with itself: *absent* is itself a proxy
for *not the thing I wanted*, and a file can be present and worthless. Every answer to "did the
goal occur?" is a proxy for a goal one level further out. **The tell is an assertion that talks
about an artefact's existence rather than its contents.**

The last row is the sharpest, because a health check is the most error-shaped check there is, and
it is the one everybody trusts.

→ [loop.md §3 T2](new%20features/loop.md), [build.md §7](new%20features/build.md)

### 2. A green suite is not evidence. This one has been wrong nine times, in nine modules

Not nine instances of one bug — nine different subsystems, nine different mechanisms. The full
list is in [build.md §7](new%20features/build.md) and is worth reading before trusting any suite.
Three that generalise hardest:

- **A zero-slide deck and 28 bytes of junk both passed** `starts with PK` + `>= 10,000 bytes`.
  Nothing between `prs.save()` and the download had ever *opened* the file. → **Assert the
  artefact, never the byte count.**
- **A refusal metric read `0/2`** while the agent refused correctly. Three of the four rows were a
  detector gap; the scorecard blamed the agent, then advised changing the prompt.
- **A schemaless column was written through one model and read through another.** The key was
  stored in the database and absent from every API response, because the reading model's
  serialiser ignores unknown fields by default. **Both sides succeeded and nothing raised.**
  A live scenario went green over it, because it read the column — the one place the
  defect is invisible. Where a schemaless column has two models, the assertion must be a
  **round trip**.
- **An admin endpoint returned 500 on every request** while an offline harness that reads source
  and introspects routes reported every case passing. In the browser it surfaced as a **CORS
  error**, naming the wrong subsystem entirely.

**Every one was found by reading an answer, opening a page, or reordering a loop — never by a
passing assertion.** So verification ends with a step that is not a command: *open the page and
read one real output by eye.*

**Corollary: "do not stop until everything is tested" is a failure mode, not a goal.** It
optimises for the suite being green, which is the state that has been wrong nine times.

### 3. Then mutate. A passing suite tells you nothing about what it would let through

The question at the end of verification is not *does the suite pass* — you already know that — it
is **what could I break without the suite noticing?**

Delete the three lines whose loss would be worst, one at a time, and re-run. One change set was
built harness-first with every case watched failing: 280 backend assertions and 56 frontend tests,
all green. Deletion then found **three lines no case guarded**:

| Delete | Suite says | Actually |
|---|---|---|
| `receipt.committed = True` | green | every successful turn writes its usage **twice** |
| the fourth revert gate | green | the revert fires on a thread the user has since left |
| the `else:` on a download fallthrough | green | every object-storage download reads the bytea it exists to avoid |

Each had a comment explaining why it was load-bearing. Every reviewer who *read* the code agreed.
Only deleting it revealed that nothing would notice if it went. **A line that survives its own
deletion is undefended, whatever its comment claims.** Restore it, write the case that goes red,
and put the mutation in the case's comment so the next reader can repeat it.

This is the first method here that caught defects *before* they shipped rather than explaining
them afterwards.

→ [build.md §7](new%20features/build.md)

### 4. A test that cannot fail reports success — in green, forever

Three independent instances, and none of them looked wrong:

- A scenario proving the agent searches twice passed **twice while proving nothing**: first
  because the fixtures chunked to one chunk per file, then because a `k=3` retrieval over seven
  chunks still returned both topics. Only when the scenario starved retrieval *itself* did the
  absence of a second search become a finding.
- **Context precision of 1.000 on a single-chunk corpus is not excellent retrieval. It is
  retrieval that cannot fail.** Treat a perfect score on a tiny corpus as *not yet measured*.
- A browser harness passed with a `@mention` popup **that never rendered**, because its fixture
  agent had no roster and nothing typed `@`.

So **a case owns the conditions it needs and restores them in a `finally`.** Do not rely on the
fixture's defaults happening to be hostile enough. And a check that reports three states —
pass / fail / **NOT MEASURED** — is worth the extra branch, because the third is otherwise
indistinguishable from the first.

**A fourth instance, caught in the act rather than in hindsight.** A case asserting that one
migration added four columns concatenated *every* file in the migrations directory and grepped
for the four column names. It reported two of them already satisfied — because columns with
those names existed on a **different table**, from an unrelated revision written months
earlier. It would have passed with the new migration absent entirely.

> **A check that searches everywhere can pass on the wrong thing.** Name the file, and name
> the table. *"Does this string appear somewhere in the project"* is almost never the
> assertion you meant.

→ [loop.md §5](new%20features/loop.md)

### 4b. A check over source TEXT cannot tell code from prose about code

A case asserted that a module imported ragas' message classes and never langchain's
same-named ones — a real hazard, because importing the wrong pair raises nothing and yields a
sample every metric reads as empty. It was written as a substring search and went red against
a **correct** file: the module's own docstring *explains the collision*, and the check matched
its own explanation.

The same shape had already been recorded once here, when a check that a symbol appeared
nowhere under a directory scanned its own source and matched its own search string. Twice is a
pattern:

> **Looking for the thing must not be doing the thing.** When the subject of an assertion is
> code, parse it — an `Import` node can only ever be an import. A grep over source cannot
> distinguish a call from a comment about the call, and the file most likely to discuss a
> hazard is the file that handles it.

### 4c. A feature change can invalidate a distant case WITHOUT failing it

A case focused a control near the bottom of a tall panel, to prove that focusing something low
cannot displace a dialog. A later change put that control inside a collapsed `<details>`. The
content of a closed `<details>` is in the DOM but **not rendered**, so `.focus()` silently becomes
a no-op — and the case went on passing while no longer exercising its own premise. Nothing went
red. Nothing could have.

This is §4 with the arrow reversed: not a test written unable to fail, but a **working test
disarmed from a distance**, by an edit in a different file that never mentions it. The two share
one remedy. **A case must own the conditions it needs**, and where it cannot own them it must
assert them — "the control I am about to focus is on screen" is one line, and it is the line that
turns this from silence into a red row.

The general form, worth asking on any change that hides, defers, virtualises or lazily mounts
something: **which existing assertions reach into what I just made unreachable?** Grep the
selectors, not the feature.

### 4d. Never ask a rect whether something is visible

A case decided a collapsed section was closed by reading
`getBoundingClientRect().height > 0`, on the reasonable-sounding premise that closed `<details>`
content measures 0x0. Measured in the browser actually running the suite, it does not — closed
content reported **60x1280** with `checkVisibility()` false. The harness then contradicted itself
on the same nodes in the same drive: a sibling case gated on `innerText`, which *is*
rendering-aware, and got the same page right.

A bounding rect answers "what box does layout give this", which is not the same question as "can a
person see it", and the two diverge for `content-visibility`, closed disclosures, `visibility:
hidden` subtrees and clipped ancestors. Ask `checkVisibility()`, or ask for the text.

**And note which way this fails.** The rect said *visible* when nothing was: a false GREEN on the
"it is expanded" reading and a false RED on the "it is collapsed" one, in the same predicate,
depending only on which way the case was phrased. A predicate whose error direction flips with
the phrasing is not a predicate you can reason about — replace it, do not tune it.

### 5. A harness proves the instrumentation it was HANDED works — never that it is COMPLETE

Two levels of this, learned separately:

> **A layer-1 harness cannot prove a query RUNS, only that it was WRITTEN.** An offline check
> reads source and introspects routes; a query that compiles and does not run is invisible to it.
> Anything emitting SQL, a request body or a file needs a case that **executes** it — hence
> `--live` modes rather than more introspection.

> **A harness cannot prove instrumentation is COMPLETE, only that the instrumentation it was
> handed works.** Ten metering cases passed while one call site was unmetered — because each case
> opened its *own* scope and then asserted attribution survived. **The harness only ever tested
> call sites it wrote itself.**

Coverage is a property of the application's call graph, so a case asserting it must read the
application's **source**, never a shape the harness invented. Forty lines of `ast` that walk the
call graph and fail on any entry point reaching the instrumented function outside a scope is the
answer. Write it whenever a feature is *cross-cutting* — metering, auth, audit logging, tracing.
For those, "every case passes" and "every call site is covered" are unrelated statements.

**Bias such a check toward false alarms.** The call-graph walk resolves callee names bare,
ignoring the module, so two same-named functions merge into one node — that can invent an edge and
never lose one, so it can raise a false alarm but not a false all-clear. That is the correct
direction to be wrong in.

### 6. An acceptance criterion names a harness file and a case id, or it is a wish

Prose criteria feel like rigour and are inert. One change set's criteria were written as prose and
went **unexecuted across two documents** until someone wrote the harness that finally ran them. A
criterion reads `S14 — with the feature off, the answer is byte-identical to the classic path`,
and it names the file it lives in.

Two rules that ride along:

- **Write the case first and watch it fail.** A case added after the code is a case written to
  pass. That is how the same scenario went green twice while proving nothing.
- **The criterion must make the feature necessary.** A scenario that passes without exercising
  anything is the same defect as an assertion that cannot fail.

→ [build.md §4–5](new%20features/build.md)

### 7. A harness that writes OWNS its subject, or it does not write

When the development database is production, a write mode is one line from corrupting real data.
`select(User).limit(1)` in a write path is the defect this rule exists to prevent: no `ORDER BY`,
so *which* real person receives a test fixture is whatever the database returns first, and it can
differ between runs. Two harnesses shipped with exactly that line; it was caught in review, not by
a test.

A harness creates its own user with its own address, attaches everything to it, and removes it.
Three properties worth copying:

- **A `--cleanup` mode runnable on its own**, because a `finally` covers an exception and not a
  killed process or a dropped connection.
- **A printed marker BEFORE the write, not after** — so a row orphaned by a kill between the
  commit and the sweep is findable by name.
- **Verify by counting, before and after.** A cleanup that silently failed is invisible any other
  way.

### 30. A stub silently replaced by the real thing is an offline test that goes to the network

An offline harness handed a scripted model to the code under test. The code narrowed with
`isinstance(model, OurConcreteClass)` and, finding a plain base-class stub, **discarded it and
built a live client instead**. The harness made a real, billed API call, got a fluent answer
back, and nothing raised.

It was caught by one assertion and would have been invisible to the obvious ones. *"An answer was
produced"* passed. *"No exception"* passed. What failed was **"the answer is the exact string I
scripted"** and **"the stub was called exactly once"** — assertions about the outcome wanted,
never about the absence of an error.

So: **a substitution point is a place a test double can be silently rejected.** Widen the type
check to the seam's own abstraction, and have at least one case assert the double was *used* —
a call count, or an exact scripted string. A harness that can quietly fall through to production
is worse than no harness, because it bills you for the privilege of testing nothing.

### 31. A gate can be masked by another gate, so mutating it changes nothing

Mutation testing is supposed to answer *"would this suite notice if I deleted the line?"*. On a
guard with four conditions, the honest answer can be **no, and the guard is still correct** —
because a second condition suppresses the same behaviour on the path the test happens to drive.

Deleting a once-per-turn flag changed no end-to-end outcome twice running: the first scenario was
masked by a *different* gate downstream, and the second by the **loop structure itself**, which
entered the guarded block once per turn regardless. Two rewrites of the same case, both green,
both proving nothing about the line they named.

Three things follow. **A green mutation is a finding, not a failure of the tooling** — it says the
line is unreachable-as-tested, which is worth knowing before someone deletes it as dead. **Assert
at the level where the property is decidable**: driving the callback directly turned the mutation
red immediately, where no end-to-end scenario could. And **say in the harness which mechanism
actually guarantees the behaviour**, because "the gate does it" and "the structure does it, the
gate is defence in depth" are different facts, and only one of them survives a refactor of the
structure.

---

## II. Instruments, and the ways a measurement lies

### 8. A measurement can be wrong in exactly the same silent way a test can pass

A concurrency probe reported `asyncio.gather` costing *more* than the slower of its two calls — a
plausible result about connection contention, and an artefact: `gather` ran third in every trial,
so it measured **position** as much as concurrency. Running it first reversed the verdict.

The question to ask of any number before quoting it: **could this be an artefact of my loop order,
my sample, or what ran before it?** A measurement is a test whose assertion is implicit, and it
inherits every failure mode of one.

### 9. An instrument can punish the behaviour the system exists to produce — and then advise its removal

The most dangerous failure here, because the output still renders and still points confidently.

- A refusal metric read `0/2` on an agent refusing **perfectly**. The detector's marker list was
  missing the phrasing.
- Faithfulness scores a teaching persona's **analogy** and its **comprehension check** as
  unsupported claims. They are, by construction — they are not in the retrieved context, and they
  are the two things the persona exists to produce. The scorecard then names faithfulness as the
  weakest metric and advises *tightening the grounding clause and reducing persona verbosity*,
  i.e. deleting the pedagogy.

**A metric that scores a correct behaviour as a failure is not a strict metric. It is the wrong
metric.** Before acting on a weak score, read the underlying outputs and ask whether the
instrument can express the thing you built.

**Corollary — a refusal metric measures the detector and the agent at once, and both failures look
identical on the card.**

### 10. Validate a judge against a known case before believing anything it says

One judge scored an answer **copied word-for-word out of its own context** at **0.000**. Another,
on the identical stored answer, scored it **1.000** — and scored that same answer plus two invented
facts at **0.250**. That is the difference between an instrument and a random number generator, and
nothing on the scorecard distinguished them.

So: **a metric that cannot discriminate is worse than no metric, because the scorecard still
renders.** The controlled test is cheap — one known-good case, one known-bad case, identical
inputs, and confirm the number moves in the right direction. Do it before the metric is trusted,
not after a result surprises you.

**And assert that the two verdicts DIFFER, as its own check.** A known-good case scoring 1.0
and a known-bad case scoring 0.0 are two assertions that a *constant* also satisfies: a metric
stuck at 1.0 passes the first alone, one stuck at 0.0 passes the second alone. Only the
comparison rules out a constant, and it is the assertion that would have caught the 0.000 judge
above on the day it was configured rather than a scorecard later.

The same pairing discipline appears wherever a detector is asserted *not* to fire — that
assertion is passed just as well by a detector somebody deleted. Write the pair, always.

**And do not read a before/after delta as the judge delta** if the answers were regenerated in
between. Temperature is not zero; judge change and answer variation are confounded. The clean
evidence is the *same stored answer* scored twice.

### 11. Each metric's mean has its own denominator, and the summary line does not

A scorecard reported faithfulness as a mean over **6** values while its footnote said "means rest
on 8 scored questions" — because a row counts as scored if *any* metric survived. **The metric most
likely to fail therefore has the smallest sample, and it is the one the weakest-metric pointer
selects and sends you to act on.** Read the count next to the number, never the count at the bottom
of the card.

### 12. A silent decline to measure is worse than a crash

One metric requested three candidates in a single call; the provider rejected it, and the run still
reported `status=completed` with that metric almost entirely null and a confident pointer. A crash
sends you to the cause. A null renders.

Relatedly: **do not let one timeout constant mean two things.** A per-metric ceiling that doubles
as a quota-retry budget makes a rate limit and a hang print the identical string — and they need
opposite fixes (wait, versus raise the ceiling).

### 13. Distinguish an environment failure from a defect, in the output itself

A suite that goes red because a provider said no teaches its reader to ignore red. So an upstream
rate limit prints `[rate]`, not `[FAIL]`, and does not exit non-zero. **Treat those rows as
unmeasured, never as passing.**

The same conflation in reverse: a provider 429 surfaced as a job stuck at `failed` and a scenario
throwing — indistinguishable, on the console, from a code defect. It has happened three times here,
and each time made a working system look broken.

---

## III. Detectors, prompts, and getting a model to act

### 14. Assume the model will not call your tool — but re-run the table when the model changes

Binding a tool is twenty lines and works first time. The work is designing the **trigger** for when
the model then declines. One model self-initiated a search **0/6** on a probe; its replacement
scored **6/6** on the identical probe.

**The structural explanation outlived the measurement and is what predicts the next model:** a
model drilled to treat a missing fact as a cue to **decline** will not spontaneously treat it as a
cue to **search**. Every well-grounded RAG system states the grounding rule before it establishes
voice — which is exactly why it can be trusted to say "I don't know", and exactly why it will not
go looking. The two instructions compete and the earlier, more forceful one wins. Weakening the
grounding rule trades a hallucination-free system for a tool-happy one: the wrong trade.

**When you invert an assumption, look for the test you never wrote.** A year of assuming
*under*-calling left the suite with no assertion that could see **over**-calling — and the new
model emitted 1.5–2.0 calls per step against a budget that bounds *steps*, so the retrieval cost
silently doubled with every harness green.

→ [loop.md §3](new%20features/loop.md)

### 15. Strictness follows the cost of being wrong — in each direction, separately

One marker list feeds two functions with deliberately different rigour. One writes a success
metric, where a false positive **corrupts a measurement**, so it is position-sensitive. The other
drives a retry, where a false positive **costs one retrieval** and a false negative costs the
entire feature, so it is position-insensitive.

One source of truth for the phrases, two tests over it, and the asymmetry stated at each. **Ask
what being wrong costs in each direction before choosing strictness. The two are rarely
symmetric.**

### 16. When a pattern-match has been wrong three times, add the SHAPE, not the string

The list was corrected five times. Three were the same gap discovered independently —
`"does not say"`, then `"does not cover"`, then `"does not state"`. The family is
`does not <reporting verb>`; adding the family cost nothing and ended the sequence.

The other two are the failure from opposite sides, and together they are the real rule:

| # | The output | Why the match failed |
|---|---|---|
| 4 | `The material does **not** mention the vendor.` | **Markdown emphasis.** The phrase *was* in the list. Whitespace was normalised; `**` was not — all 34 markers were equally blind |
| 5 | `... are not covered in this briefing.` | **A hard-coded determiner.** The marker was `"not covered in the"` — not missing, too *specific* |

> **A marker carries the shape and nothing else** — not a determiner, and not the formatting a
> model happened to wrap it in.

Fixes at that level are free, because every string the marker matched before, it still matches.

**And a flaky detector reads as model variance and gets re-run.** At temperature 1.0 the model
picks a different phrasing each time, so the fifth defect passed once and failed the next run with
no code change between. Read the output before believing "just flaky".

### 17. A prohibition written to stop one behaviour will stop its neighbours unless its edge is stated

A rewriter prompt said *"leave acronyms, initialisms and product names exactly as the user wrote
them"* — and typo repair fell **5/5 → 3/5**, because a repaired term now read as a product name to
preserve. Narrowing the prohibition and adding an explicit carve-out for ordinary misspellings
restored 5/5.

**It was only caught because the case asserted the repair HAPPENED**, rather than asserting nothing
was fabricated. A test that checks only for the bad outcome cannot see a good one going missing —
§1 again, arriving inside a prompt.

The companion failure: a bullet telling the model to *expand* acronyms contradicted a
do-not-invent bullet four lines below it, and **the model resolved the conflict by inventing** —
including an expansion that moved retrieval to the wrong file. Two instructions in one prompt that
can conflict *will* conflict, and the model picks.

### 18. A feature can pass its own harness and still be the wrong thing to ship

The conditional version of that acronym rule measured **5/5 in both directions** — and was removed
anyway. Its value was the first-turn case, where nothing has spelled the term out; the gate that
made it safe also made it fire almost never, while leaving a standing invitation on the page.

**Passing is necessary and not sufficient. Ask what fraction of the cases you built it for it
actually reaches.**

### 18b. A correct, well-tested measurement can be structurally unfit for YOUR system

An off-the-shelf metric library shipped two metrics for exactly the thing that needed
measuring — whether an agent called the right tools. Both were correct, documented and
maintained. Both were unusable here, and the reason had nothing to do with the library.

They compare tool **arguments** byte-exactly. This system rewrites its search query on *every*
turn, by design, at a sampling temperature that is not zero — so the one meaningful argument
is guaranteed never to be the same string twice. Measured: **0.0** for a differently worded
query with identical intent; **0.0** for a deliberately empty reference (the documented
escape hatch, which returns zero exactly where it would be used); **0.0** for two calls
against a one-call reference, which is what the model normally emits.

An instrument reading zero while the subject behaves perfectly is not a strict instrument —
it is the wrong one, and it would have rendered a confident column of zeros forever.

Three things generalise:

- **The blocking property is often in YOUR system, not the tool.** Nothing in the library's
  documentation could have warned about a rewriter it has never heard of. The question is not
  *"is this metric good?"* but *"what does this metric assume is stable, and is it stable
  here?"*
- **Measure before adopting, and it is usually cheap.** Both metrics need no model at all, so
  the entire calibration was seven assertions and ran offline in milliseconds. The decision to
  reject them cost less than the argument about it would have.
- **Write the rejection down as an executable check, not a note.** The pinning case reads the
  installed library's own source, so a future release that changes the behaviour turns the
  build red and forces the decision to be re-opened deliberately. A note would simply have
  become quietly false — §24.

---

## IV. Platforms, libraries and environments

### 19. What ROUTES is not what EXECUTES, and neither is what your client library added

Five distinct mechanisms by which one parameter can be wrong, on one gateway, each producing a
different failure and only one of them the failure you would predict:

| # | Mechanism | Symptom |
|---|---|---|
| 1 | Unadvertised, and routing consults it | 404 on a model that plainly exists |
| 2 | Unadvertised, and routing does **not** consult it | works fine — the list is silent in both directions |
| 3 | Advertised, routed, then rejected by the provider | 400 at execution |
| 4 | **Injected by the client library, unasked** | 400 on a parameter absent from your call site |
| 5 | Sent fine, parsed fine, then **dropped by the client's whitelist** | no error at all; the data is simply gone |

**So the diagnostic step is: print the request body. Do not read the call site** — two of these are
invisible there by construction. And do not read the capability list either; it describes one
surface and is consulted for one purpose. **Probe.**

**Count the client-library injections, because the count is the finding.** Three unrequested
parameters across three unrelated features is not three coincidences — it is a property of the
library, in the same way a packaging tool silently flattening an environment marker **three times,
in three unrelated dependency additions**, is a property of that tool. Treat the fix as the second
half of the command, not as a thing to remember.

### 20. Presence is not correctness — compare by VALUE, and then test the thing

Deployed configuration drifted from local. Every required key was **present**; two held different
values, and one was a stale trial-tier credential rate-limited at ~10 calls/minute — surfacing as
jobs stuck at `failed`, indistinguishable from a code defect.

**Two keys that look identical can differ only under load.** A hash comparison finds candidates; it
does not decide. Twelve rapid calls separated the trial key from the production one. **Test the
credential, do not trust its shape.**

### 21. Two environments diverge in OPPOSITE directions, so neither "works locally" nor "works in production" is safe alone

The pair is the point:

- A resource limit does not exist on the development OS, so **the dev box is measurably less
  protected than production** — the reverse of the usual arrangement.
- A text-fitting API returns font directories for macOS and Windows and otherwise raises
  `OSError("unsupported operating system")`. It works locally, passes every local test, and would
  kill every job in production with a message naming nothing about fonts.

One divergence makes the dev box weaker; the other makes it *capable of things production cannot
do*. Check both directions.

### 22. An allowlist over IMPORTS does not stop an allowed library handing you a blocked module

`import matplotlib` then `matplotlib.os.environ` passes an import allowlist, a name denylist and a
dunder rule — the user's source never names `os`. The fix is applying the denylist to **attribute
access**, not only to imports.

**The instructive part is that neither probe leaked anything.** The child process is spawned with
an empty environment, so the read returned `None`. **The strongest control in a sandbox is not a
limit, it is the empty environment** — any change that starts passing variables through removes
more protection than any change to the allowlist could.

**And write the test for the opposite failure**, which is likelier over time: a denylist wide enough
to block `matplotlib.os` must not block `matplotlib.pyplot`. Without that case, the safe-looking
response to any future scare is to keep adding names until the tool quietly stops working.

### 22b. A responsive rule keyed to the WINDOW is the wrong question inside a fixed-width box — and the DESKTOP is the case that breaks

A component lived in a 544px panel and carried "three columns at >= 1024px" on a card grid. On
every desktop that fired inside a **511px** content column: nine cards at **159px each**, most of
their text clamped, a badge pushed outside its own border, and 40-49px of horizontal scroll **at
1440px** against a hard requirement of zero at 320px.

Three things generalise, and the second is the one that inverts a rule people already hold.

- **The breakpoint answered a question nobody asked.** `min-width` media queries measure the
  viewport; the constraint was the container. Container queries ask the right question — and their
  scale is a *different set of numbers*, not the viewport names renamed, so a mechanical rename
  produces a class that silently never fires.
- **The phone layout was the correct one and the desktop layout was the degraded one.** The usual
  warning is "a fix written for a phone must be checked at 1440". This is its mirror: a component
  can be right at 375px and wrong at 1440px *for the same reason*. Check both directions.
- **A bigger monitor revealed nothing** — 2560px produced exactly the same 511px. A defect that
  does not respond to the obvious diagnostic gesture will survive every casual look.

**So assert the RESOLVED track width in pixels, never the class.** The class list was correct and
the box was wrong, so a className assertion and a viewport-only assertion are both blind to it.

### 22c. When two styling rules tie on specificity, the winner is emitted order — so restructure instead of out-ranking

A shared field style carried "full width"; a call site added "6rem wide". Both are width utilities
of equal specificity, so the winner was whichever the framework emitted later — and it was the
shared one. The input rendered **242px instead of 96px** and hung 152px past its column, which is
what put a value on top of its neighbour's label in a shipped build.

**The tempting fix is a more specific selector, and it is the wrong one**: it wins today, is
invisible in the class list, and is one refactor from silently reverting with no way for the next
reader to know a tie exists. Remove the conflict instead — inside a 6rem wrapper, "full width" *is*
6rem.

The same mechanism had already been recorded in this codebase for two `display` utilities. **A trap
that recurs on a second CSS property is a property of the tool, not an incident** — treat any
shared-string-plus-local-override of the same property as a coin flip until one of them is gone.

### 22d. `overflow: hidden` clips AND creates a scrollport — and the second half will move your dialog

`overflow: hidden` boxes are unscrollable by the *user* and fully scrollable by *script*, including
the script every browser runs to reveal a focused element. A centred dialog's ancestor accumulated
2,012px of scrollable overflow, reached `scrollTop: 70`, and dragged an 810px panel to **top -25
with its own title clipped off the screen**. Nothing threw and nothing logged.

**The trigger is ordinary keyboard use.** Moving focus to a control low in a tall panel does it,
because the browser scrolls every ancestor scrollport to reveal the focused element.

**And clipping the inner box fixed the outer one while MOVING the bug one level down** — the panel
then accepted a scroll and slid its own heading out of view. `overflow: clip` clips *without*
establishing a scroll container, so there is nothing left to scroll at either level.

Two transferable pieces. **A fix that relocates a defect one level in is indistinguishable from a
fix, until you re-measure at the new level.** And **assert the outcome, never the property**: a case
asserting `overflow-clip` passes a refactor that keeps the property and loses the behaviour, and
fails one that reaches the same outcome another way. Ask instead whether the heading moved and
whether the close button is still reachable.

### 22e. A label that names the implementation is a label only its author can read — and two of them were lying

Ten controls were named after their database columns. Renaming them to plain English is the obvious
half. Three less obvious things came out of doing it:

- **Keep the technical name beside the plain one, quietly.** If the system's own logs, traces and
  docs use `retrieve_k`, replacing it with "Shortlist size" hands the reader two vocabularies and
  tells them about one. The tag is the join. This matters most in a product whose purpose is to
  *teach* the domain.
- **Explanations belong on the surface people LAND on.** The good help text existed — behind an
  "advanced" mode nobody had to open. The default view had a value and a column name and nothing
  else, so the one surface everybody read was the one surface with nothing to read.
- **Two labels described behaviour no code performed.** Both parameters reached exactly one
  consumer: a log payload. A label a user can act on that does nothing is *worse* than an opaque
  one, because they believe they changed the system. Neither control was deleted — one of them is
  printed in the trace every turn, so hiding it would leave the reader meeting the number there
  with no explanation at all. They were relabelled honestly and grouped under "recorded, but
  changes nothing today".

**Group by WHEN a setting takes effect.** Ten controls in one flat list assert that all ten are the
same kind of thing. Three did nothing until the next upload, two did nothing at all, and the rest
were read on the next request — which is the most useful thing anyone learns from the screen, and
the flat list hid it.

### 23. A count written in prose goes stale. Grep the mechanism instead

A note read "two things run off the request thread", then three, then **four** — and the fourth had
been there the whole time. It was missed by every count *including the risk-register entry written
to catch it*, because it was the only one that did not live in a file named for the pattern.

**If you are enumerating, grep the call, not the filenames.** The same applies to any "there are N
of these" sentence in any document: it is a measurement, it has a date, and nothing raises when it
expires.

### 24. A deferral recorded as a risk needs a re-check date the way a measurement does

A decision not to probe something was correct when written and quietly stopped being true. Nothing
in the codebase could have told you — the note read as a standing conclusion rather than as a dated
observation. **Write deferrals with the condition that would reverse them.**

---

## V. Process

### 25. The audit's best outcome is a SMALLER change set, and that is not a consolation prize

One audit concluded that three of five planned patterns had already shipped, and that a fourth was
**architecturally closed** — building it meant *adding back the parameter whose omission was the
security property*. The audit deleted more work than the plan added.

An audit's deliverables are not prose about intent. They are: **what exists**, at `file:line`, with
signatures copied in so the plan is checkable later; **what is closed, and by which invariant** —
not "we decided not to", because that is the entry that stops the idea returning in six weeks; and
**what the change reduces to** once the first two are subtracted.

### 25b. Let the acceptance criteria overrule you, including when you are the one they are overruling

A judgement call — collapse the section of settings that no code path reads, because the screen is
long — shipped, and the criterion written earlier in the same change set went red **by name** on the
two parameters it hid. The criterion asserted that every parameter carries a visible explanation *in
the mode the user lands in*. Collapsing a group is precisely the act of removing explanations from
the arrival screen. The two were never both satisfiable.

**The criterion encoded the request; the change was polish about length.** That is the whole test,
and it is worth applying explicitly rather than by feel: when a criterion and a later idea conflict,
ask which of them is the thing that was asked for. Polish loses.

Two properties made this work, and neither is automatic:

- **It asserted the OUTCOME the user asked for, not the implementation that delivered it.** A case
  pinning "the ten parameters render in three groups" would have stayed green through the collapse
  and caught nothing.
- **It was written before the code and watched failing.** It had already demonstrated it could go
  red, so a red row was information rather than a suspected harness fault — which is the state most
  green-by-construction cases can never reach.

There is also a disposal rule. When a criterion wins, the case for the reverted behaviour has no
subject left: **delete it, do not skip it.** A case that cannot pass is noise; a skipped one claims
the behaviour is merely unmeasured when it was in fact decided against. Move whatever it taught to
where it stood.

### 26. A contract stated twice drifts, and the copy that drifted is never the one you are reading

Shared contracts — settings, event types, schema, API surface — live in **one** plan file that the
feature files reference and never restate. Settle the migration there too: two features each adding
a column means two revisions racing for the same parent.

### 27. Build one feature per session, and clear between them

Not context hygiene as ritual. Loading five feature documents means the model reasons about five,
and **the interactions it invents between them are in no plan.**

### 28. Ship the durable half back out, or the folder becomes sludge

The change shipped; the plan folder is now archive. What was learned goes to the file indexed by
the failure it causes, the spec's open-items table, the operator's guide, or the pattern documents
— **edited, never copied beside them.** Skipping this is precisely why a reader cannot tell
instruction from record.

**Un-numbered means living; numbered means a record of one change.** Carrying that distinction in
the *filename* is the only thing that lets a reader tell a rule from a diary entry at a glance.

### 29. Applying a migration before the merge opens a window — keep it short

Applying it first makes the start-up migration a no-op rather than a first run against traffic,
which is right. The cost, unrecorded for months: between the apply and the merge, the database
claims a revision the deployed code does not contain, so the start command exits non-zero. **Any
restart inside that window crash-loops the service**, and the platform restarts on its own
schedule. See §I.1 for why nothing noticed.

### 32. The documented path is not always the correct one — spike before you plan

A twelve-agent research pass, every one of them reading the installed package source rather than
recalling documentation, unanimously recommended the framework's *documented* adapter for
reaching a third-party model provider. It was the right answer to "what is supported" and the
wrong answer to "what should this codebase do".

A forty-line spike, run before any planning, found the alternative: implement the framework's own
one-method model interface and delegate to the chokepoint the project already had. That erased
**five of the eight risks** the research had identified — three of which failed silently — and
removed a dependency entirely. None of it was discoverable by reading: every fact came from
running the thing and printing what came out.

The rule is not "don't research". The research produced the audit that made the spike's result
legible, and it named the five risks that were then measured away. The rule is about ORDER:
**docs tell you the supported path; running code tells you the correct one, and it costs less
than the plan you would otherwise write against the wrong one.** Spike the riskiest integration
before the plan hardens around it — the same argument as writing the harness case first, applied
one level up, to the architecture.

---

## VI. Adding to this file

**Earn the entry.** A rule goes in when it has cost something — a debugging session, a wrong
scorecard, a shipped defect — and when it would still be true in a different codebase. If it
mentions a specific library's quirk, it belongs in [CLAUDE.md](CLAUDE.md) under the failure it
causes, not here.

**Keep the evidence attached.** Numbers, counts, and the incident. An entry that has lost its
evidence has become an opinion and should be deleted rather than trusted.

**Amend rather than append.** When a new incident sharpens an existing rule, edit that rule — the
way [loop.md](new%20features/loop.md) T1 was amended when the model changed, rather than being
duplicated beside itself. A file that only grows is a file nobody rereads.
