# Feature 7 — the agent workspace: a shell, a settings sheet, and less NotebookLM

> Every number below was measured in Playwright against the running app on 2026-08-16,
> agent *Orbital Platform* (Explainer persona, 2 documents, tools on, one conversation
> carrying one handout). Nothing here is a taste preference dressed as a defect; where
> something **is** taste, it says so.

---

## 0. First: this is not a loop.md feature, and saying so is the point

[loop.md §6.1](loop.md) asks the gating question before anything else — *is it a tool? does
it change what the agent can **find** or what it can **output**?* This changes neither. The
model decides nothing here; every branch is code reading a viewport width or a click. So the
loop pattern does not apply, and forcing it would produce a trigger with nothing to trigger
on.

Four of loop.md's rules do transfer, and they are the ones this feature is held to:

| loop.md rule | How it lands here |
|---|---|
| **S4** — off is byte-identical | There is no "off" for a layout, and adding a flag would mean maintaining two. The honest translation is **the API contract is untouched**: no route, request or response shape changes. Asserted twice in §7.5 — structurally (`git status backend/` is empty) and empirically (`agentic_check.py` 16/16, with S1 "classic path unchanged" being the row that matters). |
| **§5** — a test must make the feature necessary | A screenshot proves nothing. `scripts/ui_check.py` asserts the *numbers* — chrome height, workspace height, one scroll region, 44px targets — and **§7.1 records what each assertion would have caught**, because an assertion that cannot fail is worse than none. |
| **T2** — trigger on the absence of the outcome | The `calc()` bug below was invisible to every error-shaped check. Nothing threw. The page rendered. The outcome — *a usable chat pane* — was simply absent. §1.1. |
| **T3** — strictness from the asymmetry | Applied to the settings sheet's warnings: §4.3. |

---

## 1. What the audit found

### 1.1 The headline: opening the parameters deletes the chat

| Viewport | State | Chrome above chat | % of viewport | Chat panel | Thread visible |
|---|---|---|---|---|---|
| 1440x900 | disclosures closed | **576 px** | **64.0%** | 324 px | 215 px holding 904 px |
| 1440x900 | **disclosures open** | **1092 px** | **121.3%** | **24 px** | **0 px** |
| 834x1112 | closed | 576 px | 51.8% | 536 px | 442 px |
| 390x844 | compact header | 289 px | 34.2% | 555 px | fine |

[`AgentDetail.tsx:122`](../frontend/src/views/AgentDetail.tsx) sizes the chat panel as the
complement of its own chrome:

```ts
setChatHeight(`calc(100dvh - ${Math.round(top)}px)`);
```

That is a good idea — it measures instead of subtracting a constant, and the comment above it
correctly lists the three reasons a constant would be wrong. **It has no floor.** When the
chrome grows past the viewport the complement is negative, CSS clamps it, and the panel
collapses to its own padding.

And the chrome grows past the viewport in the ordinary case, because
[`compactHeader`](../frontend/src/views/AgentDetail.tsx#L224) hides the reference material
with `hidden sm:block` — **the collapse applies only below 640px.** Above it, the description,
the pedagogy note, the parameter summary and both `<Reveal>` panels are always rendered at
full height. Hence the shape of the table: **desktop is worse than mobile**, 576 px against
289 px, which is the reverse of how the responsive work was framed.

The interaction that triggers it is the one the workshop exists to teach. CLAUDE.md:
*"this is a workshop artifact and 'change chunk_size, watch the answer change' is the
exercise."* Opening `Retrieval parameters` to read `chunk_size` removes the answer.

**This is loop.md T2 in a module that has never seen a tool.** Every error-shaped check
passes: no exception, no console error, no failed request, no layout warning, zero horizontal
overflow. The page renders. What is absent is the outcome. The assertion that catches it is
not *"did anything throw?"* but *"is the thread taller than zero?"*

### 1.2 Horizontal waste

| Viewport | Tab | Content width | Gutter each side |
|---|---|---|---|
| 1440x900 | Documents / Evaluate | 1152 px (`max-w-6xl`) | **144 px** |
| 1440x900 | Chat | 1408 px (`xl:max-w-[90rem]`) | 16 px |

The chat tab was already widened by feature 05. The other tabs were not, so the corpus table
— the densest grid in the app — renders in 1152 px while 288 px sits empty beside it.

### 1.3 The corpus is below the fold with two documents in it

Documents tab at 1440x900: panel top 576 px, upload dropzone 634 px, corpus heading 800 px,
table 833 px. **One of two rows is below the fold.** The upload control, used once per file,
outranks the corpus list, which is the reason you opened the tab.

### 1.4 The NotebookLM read, itemised

Four decisions, and the palette is not among them.

| Signal | Where | Why it reads as NotebookLM |
|---|---|---|
| **S1** the silhouette | `AgentChat.tsx:378` — `xl:grid-cols-[15rem_minmax(0,1fr)_22rem]` | Narrow fixed left rail, elastic middle, wider fixed right rail. That *is* Sources / Chat / Studio, at the same proportion, with the same dock-to-drawer responsive switch. The single strongest signal in the codebase. |
| **S2** the Studio cards | `HandoutsPanel.tsx:379-409` | Four fixed generators as a 2x2 grid of emoji-over-label-over-blurb cards, each firing a background job that returns a row below. Audio Overview / Mind Map / Reports / Study guide, rebuilt. |
| **S3** the badge toggle | `AgentChat.tsx:523-540` | A right-panel button with a count pill in a thin bar above the thread. |
| **S4** evidence on request | `Message.tsx:47` — `sourcesOpen` defaults `false` | Citations are a thing you go and fetch. That posture is NotebookLM's, and it is the opposite of this product's actual claim. |

**What is not the problem, and must not be churned:** the thread-plus-composer with
Enter-to-send (convergent by function — there is no second correct answer); the conversations
rail (that ancestor is ChatGPT, and NotebookLM's left panel is Sources, which this app does
not have); the palette and the dark-only choice (already further from NotebookLM than a
recolour could take it); the measured-height mechanism and the `flex-1 min-h-0` chain
(hard-won, and unrelated to the read); the 44px convention and `.gw-chip`.

### 1.5 What feature 05 already got right — do not re-litigate

- **Zero horizontal overflow at 320 px.** `scrollWidth` 305 == `clientWidth` 305, zero
  offending elements.
- **Composer inside the viewport at 390x844** with the page at `scrollTop = 0`.
- **Exactly one scrollable region** in the chat column at 390.
- **Every tap target >= 44px** except the six citation chips, which are 24x24 by design and
  carry the `.gw-chip::after` hit area.

All four become regression assertions in §7 rather than work.

---

## 2. The shape

```
                        BEFORE                                 AFTER
        +--------------------------------+      +--------------------------------+
   69px | nav                            |      | nav                            | 69px
        +--------------------------------+      +--------------------------------+
        | back link                      |      | < (*) Orbital Platform [ready] |
        | icon  NAME  [ready] [EXPLAIN]  |      |   250-tok k=3 rr2 tools  [...] | 56px
        | Explainer - 2 documents        |      +--------------------------------+
  576px | description                    |      |                                |
   ..   | 250-token chunks - k=3 - ...   |      |  SOURCES | Threads |  thread    |
  1092  | Rests on: <pedagogy>           |      |  --------+---------+           |
        | > RETRIEVAL PARAMETERS         |      |  + Add   |         |  [1] .67  | 775px
        | > SYSTEM PROMPT                |      |  power.md|         |           |
        +--------------------------------+      |  comms.md|         | +-------+ |
        | Chat | Documents | Evaluate    |      |          |         | | Ask   | |
        +--------------------------------+      |          |         | +-------+ |
   324  | rail | thread | studio         |      |          +---------------------+
   ..0  |      |        |                |      |          | ^ HANDOUTS (1)      |
        +--------------------------------+      +--------------------------------+
```

Three moves, in the order they matter.

### M1 — the page becomes a shell

`AgentDetail` stops being a document-flow page with a tall header above a measured panel, and
becomes a **fixed-height workspace**: one `AgentBar` row, then the workspace filling the rest.

The measurement stays — it is still the only thing that survives the nav wrapping at 320 px —
but it now measures the top of the **shell**, which sits directly under the nav, instead of
the top of a panel that sits under 500 px of header. **The bar is a single row and cannot
grow**, so the complement cannot go negative. A `Math.max` floor goes in anyway, with a
comment naming this bug, because the guard is one expression and the failure is silent.

Chrome: **576 -> 125 px at 1440. Workspace: 324 -> 775 px, a 2.4x increase**, and it no longer
depends on what is open.

### M2 — parameters and the system prompt move behind `[...]`

The bar carries an overflow button opening an **agent settings sheet**. Not a hamburger (that
means global navigation) and not `(i)` (that means read-only information, and this is
editable). Three dots is the standing convention for *"more actions on this specific object"*,
and it is what the request asked for.

The sheet reuses `Drawer` — already the only surface in the codebase with a focus trap,
Escape handling, scroll lock and correct `inert` semantics. Building a positioned popover
instead would mean writing anchoring logic that exists nowhere in this app, to hold a form
that wants the room a sheet already has.

**It is editable**, which is new: `PATCH /api/agents/{agent_id}` is complete on the server
(`agents.py:803`) and **no frontend code has ever called it**. The wizard's own header says
so — *"there is no agent-settings UI anywhere, so an agent created with the wrong parameters
could not be corrected from the browser."* §4 is the design.

### M3 — the third column goes, and Sources takes the rail

The rail becomes a two-segment switch, **Sources | Threads**.

This is what makes the layout stop being NotebookLM's rather than merely differ from it. In
NotebookLM, Sources and Chat are two simultaneous fixed panels and the Studio is a third; here
there are **two regions**, and the rail's *content* is switchable. S1 dies with the third grid
track, S3 dies with the toggle it belonged to, and S2 dies in §5 where the 2x2 recipe grid
becomes one control.

It also answers the actual request — the corpus becomes visible **while you are talking to the
agent**, which is where knowing what it is grounded in is worth something, instead of behind a
tab you must leave the conversation to reach.

Handouts moves to a **dock under the composer**, collapsed to 44 px, expanding upward. That
keeps a handout adjacent to the turn that produced it — the property that would have been lost
by moving it to a tab, and the reason the `seed` path in `AgentChat.tsx:279` exists at all.

---

## 3. The workspace, precisely

```
md and up:   grid-cols-[17rem_minmax(0,1fr)]  grid-rows-[minmax(0,1fr)]
below md:    flex column; the rail is a disclosure above the thread, capped max-h-56
```

Two grid tracks at every width from `md` up. **No `xl` variant, so no third column at any
width** — S1 cannot come back by widening the window.

`17rem` rather than `15rem`: the rail now holds filenames, which are longer than conversation
titles and were already truncating.

The thread column is a flex column: thread (`flex-1 min-h-0 overflow-y-auto`), composer, dock.
The dock is `shrink-0`, so it takes from the thread and never from the composer — the composer
staying inside the viewport at 390x844 is a feature-05 acceptance criterion this must not
break, and §7 asserts it.

---

## 4. The settings sheet

### 4.1 Grouped by when the change takes effect, not by what it is

This grouping is the whole design, and it comes from reading the backend rather than from
taste. Verified against `app/rag/`:

| Group | Fields | Read at | Copy |
|---|---|---|---|
| **Takes effect on the next answer** | `retrieve_k`, `rerank_enabled`, `rerank_top_n`, `tools_enabled`, `max_tool_steps`, `system_prompt` | query time — `retriever.py:111/128/131`, `pipeline.py:432/490/527` | "Ask a question after saving and the change is in the answer." |
| **Takes effect on the next upload** | `chunk_size`, `chunk_overlap`, `splitter` | ingest time — `ingest.py:422-424` | "Documents already indexed keep the chunking they were ingested with. Re-upload to apply this." |
| **Recorded, but changes nothing today** | `score_threshold`, `max_rewrites` | see 4.2 | see 4.2 |
| **Fixed for the life of the agent** | `embedding_model`, and the persona fields | see 4.4 | read-only, with the reason |

A sheet that presents ten sliders as ten equal controls tells the user that changing
`chunk_size` will change the next answer. It will not, and the silence afterwards reads as a
broken app.

### 4.2 The two inert parameters are shown, and labelled inert

Both were confirmed by exhaustive grep over `app/` and `scripts/`:

- **`max_rewrites` is read by no code at all.** It appears only in schemas, seeds, a column
  default and one trace payload (`ask.py:675`). PRD 3.5's Stage 2 rewrite loop is not
  implemented; the only rewriter is history contextualisation, which is not score-triggered
  and not bounded by this number.
- **`score_threshold` is observability-only.** `ask.py:652` computes `below_threshold` purely
  to record it, and `ask.py:677-681` writes the literal action `"advisory..."`. It governs
  neither refusing nor rewriting.

Both stay visible with an explicit note. Hiding them would be tidier and worse: the trace
panel prints `score_threshold` on every turn, so a reader who meets it there would have
nowhere to find out what it is. This also matches what CLAUDE.md already says out loud —
*"`score_threshold` governs rewriting, not refusing. Not a safety control"* — and recording
what is not yet true is this repository's habit.

### 4.3 Validation strictness, from the asymmetry (loop.md T3)

The server is the authority and returns a usable `detail` on every rejection. The client
duplicates exactly one check and no more:

- **`chunk_overlap >= chunk_size` is checked client-side.** It is the one rule evaluated
  against the **merged** config rather than the request body, so a user can trip it by editing
  one field while the other keeps its saved value — and the wizard already computes this
  warning, so a second copy of the *rule* is not being written, only a second call to it.
  A false positive costs a warning under a field the user is mid-edit on; a false negative
  costs a 422 after a save. Warn, do not block.
- **Everything else is left to the server**: 409 on a colliding name, 400 on an
  `embedding_model` change, 422 on an explicit null at a NOT NULL column. Re-implementing
  these client-side means two sources of truth for a bound that only the server enforces, and
  the failure mode of drift is a form that silently refuses a value the API accepts.

### 4.4 What the sheet must render read-only, and why

`AgentUpdate` sets `extra="forbid"`, so **any unknown key is a 422 rather than an ignored
field.** `icon`, `persona_role`, `pedagogy`, `category`, `template_id`, `status` and
`visibility` are copied at creation and are not patchable. `embedding_model` is declared on
the model *only to be refused*: a same-value round trip is allowed so read-modify-write works,
and a change returns 400.

The client type `AgentPatch` therefore lists only the patchable fields. That is deliberate
type-level enforcement — adding `icon` to the sheet would otherwise be a runtime 422 in the
browser instead of a build error on the machine that wrote it.

`generation_model` is a real column (`db/models.py:181`) that appears in neither `AgentOut`
nor `AgentUpdate`, so it is invisible to the API and cannot be shown at all. Noted here so the
next person does not go looking.

---

## 5. Handouts: the dock, and killing the Studio grid

`HandoutsPanel` already owns no scroll container, heading or width — its frame is supplied by
whatever holds it. That is why it can move from an `<aside>` to a dock without being rewritten.

One change inside it: **the 2x2 recipe card grid becomes a single labelled control** — a
`<select>` for the recipe beside the brief textarea and the create button. The four recipes
themselves are fine and stay. It is the *shape* — four emoji cards in two columns, each a
generator firing a background job — that is Studio, and it is the cheapest of the four signals
to remove because nothing depends on the cards being cards.

The dock:

```
collapsed   [ ^ Handouts (2)      power-by-subsystem.png  41 KB ]     44px, shrink-0
expanded    the same bar, plus the panel at max-h-[45%] of the workspace
```

`aria-expanded` / `aria-controls` on the bar, and it is **never opened by an effect**. The
constraint documented at `AgentChat.tsx:104-118` still holds and still bites: the conversation
rename input cancels on blur by design, so any code path that moves focus on the user's behalf
silently discards a rename in progress. A turn that produces a handout increments the count;
it does not open the dock.

---

## 6. Files

| File | Change |
|---|---|
| `lib/useFocusTrap.ts` | **new** — Drawer's trap/Escape/restore/scroll-lock, extracted so the sheet reuses it rather than copying it |
| `lib/useAgentDocuments.ts` | **new** — fetch/poll/upload/delete, so the rail and the full view share one backoff timer |
| `lib/api.ts` | `agents.get`, `agents.update` — the first caller of `PATCH /api/agents/{id}` |
| `lib/types.ts` | `AgentPatch`, patchable fields only |
| `components/ui.tsx` | `ParamSlider` and `Segmented` lifted out of the wizard so the sheet shares them |
| `components/Drawer.tsx` | consumes the hook; gains `width?: "md" \| "lg"` |
| `components/AgentBar.tsx` | **new** — the 56px row, and the `[...]` button |
| `components/AgentSettingsSheet.tsx` | **new** — the editable sheet |
| `components/SourceRail.tsx` | **new** — the corpus, in the rail |
| `components/HandoutDock.tsx` | **new** — the collapsed bar and its expanded panel |
| `components/HandoutsPanel.tsx` | recipe cards -> one control |
| `components/CreateAgentWizard.tsx` | imports the lifted controls |
| `views/AgentDetail.tsx` | the shell; the header's content moves to the bar and the sheet |
| `views/AgentChat.tsx` | two grid tracks, switchable rail, dock; the `xl` third column and its drawer branch are deleted |
| `views/AgentDocuments.tsx` | consumes the hook; unchanged output |
| `index.css` | only if a rule cannot be a utility — the `gw-` bar |
| `scripts/ui_check.py` | **new** — the proof |

---

## 7. Proof — run, and falsified

`scripts/ui_check.py`, Python Playwright on the **global** interpreter — the same split
CLAUDE.md already records, where `scripts/` runs outside the backend venv. Four viewports:
1440x900, 834x1112, 390x844 and a 320x844 overflow pass.

```
python scripts/ui_check.py          # backend on :8000, frontend on :5173
passed 15   failed 0
```

**Port 5173 is a requirement, not a default.** `cors_origins` is an allow-list, so a second
dev server on 5174 is a different origin and every request fails CORS before it is answered.

### 7.1 Measured, before and after

| Viewport | | Before | After |
|---|---|---|---|
| 1440x900 | chrome above the workspace | **576 px (64.0%)** | **122 px (13.6%)** |
| | workspace | 324 px | **831 px** |
| | thread visible | 215 px | **616 px** |
| | **with parameters open** | chrome 1092 px, thread **0 px** | chrome 122 px, thread **616 px** |
| | grid tracks | 3 | **2** |
| | content width, corpus view | 1152 px (144 px gutter each side) | 1280 px |
| 390x844 | chrome | 289 px | **170 px** |
| | workspace | 555 px | **775 px** |
| | page scrolls | no | no |

Desktop workspace **2.6x**; phone workspace **+40%**; and the number that is not a ratio is
the one that matters — **0 px of thread becomes 616 px** in the state the workshop exercise
puts the screen in.

### 7.2 The assertions were falsified against the real pre-change code

A suite that has only ever been green proves nothing, so the tracked frontend was stashed to
`HEAD` and the same quantities measured on the old build:

| Assertion | Old, disclosures closed | Old, disclosures OPEN | New |
|---|---|---|---|
| A1 chrome <= 140 px | **FAIL** 576 | **FAIL** 1092 | ok 122 |
| A3 workspace >= 700 px | **FAIL** 324 | **FAIL** 24 | ok 831 |
| **A2 thread > 0 px** | ok 230 | **FAIL 0** | ok 616 |
| A4 exactly two tracks | **FAIL** 3 | **FAIL** 3 | ok 2 |

**A2 is the assertion worth copying, and this table is why.** It passes on the old code with
the disclosures closed and fails only when they are opened — which is exactly the shape of the
defect, and exactly what a screenshot or a smoke test would have missed. Nothing threw in
either state.

- **A4** stops S1 returning: re-adding an `xl:` variant is a one-token edit.
- **A5–A8** are feature 05's acceptance criteria, specified in prose and never executed until
  now. They pass, and their job is to keep passing.
- **A1 and A3** are the same fact from both directions, so a regression that moves the
  boundary is attributed to the side that moved.

### 7.3 Two assertions were wrong before the code was

Both failed on a correct layout, and both are recorded because the tempting fix in each case
was to loosen the assertion until it went green.

- **A6 counted three scroll regions in a column that has one.** The selector was scoped to
  `section`, and a `<textarea>` carries `overflow-y: auto` intrinsically — so it was counting
  the composer, and the settings sheet's prompt field, because that sheet renders `<section>`
  too. Fixed by scoping to `[data-testid="chat-column"]` and excluding form controls.
- **Then A6 counted zero,** because the second version also required a region to be
  *currently overflowing*. That is a fact about the fixture, not the layout: the check agent's
  thread was empty. The criterion means "the user is never handed two nested things to
  scroll", which is structural and true or false whether or not the thread is full today.
- **A10 failed on a 401** from `/api/auth/me` on the landing page — the app correctly
  discovering there is no session yet. Fixed by clearing the console buffer *after* sign-in:
  scoping by time rather than by matching the message text, which would also have hidden a
  real 401 later.
- The harness itself had the same class of bug. It waited for
  `agent-open OR create-agent-toggle`, and the New agent button is always present, so the wait
  resolved before the list rendered and every run concluded the account was empty. It then
  tried to create the agent it had made a moment earlier and got a 409. It asks the API now.

**And the fixture had to be made hostile,** exactly as loop.md §5 requires: the check
provisions its own agent (the dev-login shim stores `google_sub = "dev|<email>"`, so it is a
*separate* user row from the same address signed in through Google, and it starts empty) and
uploads one document. A Sources rail with nothing in it cannot fail a tap-target assertion, so
it would report a success it never earned.

### 7.4 Two real bugs, both caught by the same trigger

Both were found by asking *"did the outcome occur?"* rather than *"did an error occur?"*, and
neither threw anything.

- **The handout dock unmounted its panel while closed**, as an optimisation. The count is
  reported *up* by the panel's own list request, so unmounting the panel unmounted the thing
  that produces the number — the toggle read `Handouts 0` on a conversation whose answer said
  "made 1 handout". The optimisation deleted precisely the state it was protecting, and its
  premise was wrong too: the panel stops polling on its own once nothing is pending, so
  staying mounted costs one request, not one every three seconds. The same reasoning applies
  to `SourceRail`, which is hidden rather than unmounted behind the Threads tab because it
  owns the poll that watches an ingest finish and moves `document_count` in the bar.
- **The settings sheet re-seeded its draft from the whole `agent` object.** The visible
  symptom was cosmetic — saving replaced the record, a new object identity re-ran the effect,
  and "Saved." was wiped in the frame it appeared. The bug underneath was not: the owner
  refetches the agent whenever the corpus changes, so an upload finishing while the sheet was
  open would have silently discarded whatever the user had typed. Keyed on `agent.id` now.

A third, smaller, in the same family: `sticky bottom-0` on the sheet's Save bar resolves
against the scroll container's **padding** box, and the Drawer panel carries `p-4` — so the
bar parked 16 px short of the panel edge and the form scrolled through the gap underneath it.
Negative margins do not move where sticky comes to rest; only the offset does. `-bottom-4`.

### 7.5 The API contract is untouched — asserted twice

Structurally, and cheaply: **`git status backend/` is empty.** No route, request or response
shape changes in this feature. `PATCH /api/agents/{id}` is a route the server already served
and nothing called; this is its first caller.

And empirically, once the Cohere trial key was replaced with a production one:

```
scripts/agentic_check.py --setup --run       16 / 16 passed, zero [rate] rows
```

Every scenario green — S1 classic path unchanged, S2 no reflex tool use, S3 multi-hop search,
S4 chart handout from chat, S5 tool failure recoverable, S6 step budget, S7 refusal survives
tools, S8 all four recipes plus download, S9 phase timings, S10 citation integrity, S11 no
bytea in the list query, S12 quota refuses without evicting.

**This is the first clean full pass that suite has ever had**, and the reason is worth
separating from this feature: it is the key, not the code. CLAUDE.md recorded that the suite
makes ~20 rerank calls against a trial key allowing 10 a minute, so a full run reliably
produced `[rate]` rows that said nothing about correctness. Twelve consecutive `rerank-v3.5`
calls now succeed where a trial key fails on the eleventh.

**S1 is the one that matters here** — "classic path unchanged", `events=['GENERATE',
'RERANK', 'RETRIEVE', 'SCORE_CHECK']` in 10.1 s. That is the byte-identical-with-tools-off
guarantee from `loop.md` S4, still holding after a frontend rewrite, which is exactly what it
was written to protect.

`npm run build` is clean: 290 modules, 493 kB / 144 kB gzipped.

### 7.6 The vacuous assertion, closed and then falsified

A8's citation-chip row was reported in §7.1 of the first draft of this document as a pass. It
was not one. The code read:

```python
all(c["w"] == "44px" ... for c in chips) if chips else True
```

so a thread with no citations in it returned `True` on a rule that had never been evaluated
against anything — the same trap `loop.md` §5 records for context precision scoring 1.000 on
a single-chunk corpus. It is *the* trap this document spends §7.2 and §7.3 congratulating
itself on avoiding, and it was sitting in the harness the whole time.

The chip is the only control in the product allowed to be under 44 px: 24x24 with a
transparent 44 px `::after`, because a 44 px inline box would wreck the line height of any
paragraph containing it. An exception that specific is exactly what a suite must keep honest,
and it could not.

Two changes:

1. **The fixture produces the input.** `ui_check.py` now waits for ingest to reach a terminal
   status, then asks one question through the API, so the thread holds a real answer with
   citations. Idempotent — skipped when a conversation already exists — so the model call is
   paid once per fixture rather than once per run. Measured: answered in 12.2 s, 1 citation.
2. **A third outcome exists.** `[warn] ... NOT MEASURED`, counted separately and reprinted in
   the summary, because an unmeasured assertion that scrolls past in silence is
   indistinguishable from one that passed. It does not fail the run, for the same reason
   `agentic_check.py` prints `[rate]` rather than `[FAIL]`: a red row meaning "the fixture did
   not produce the input" sends its reader to debug working code.

**And the new branch was itself falsified before being believed**, because a third state that
has never fired is no better than the `else True` it replaced. Pointing the chip selector at
a class that matches nothing:

```
passed 14   failed 0   not measured 1
  NOT MEASURED A8 citation chips carry a 44px ::after hit area: ... not evaluated
EXIT=0
```

Restored, the run reads `1 chips, 1 at 44x44` and **15 passed / 0 failed / 0 not measured**.
