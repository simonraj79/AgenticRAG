# Feature 5 — UI/UX overhaul, desktop and mobile

> Every item below came out of the frontend audit with a file and a line. Nothing here is a
> taste preference dressed as a defect; where something *is* taste, it says so.

---

## 1. The five real problems

### P1 — Chat has two nested scroll regions on a phone

`AgentChat.tsx:358` gives the thread pane `h-[70dvh]`. Above it sit the sticky nav (~60px),
the agent header (icon, wrapped title, two `<Reveal>` panels — 300-400px when open on a
narrow screen) and the tab strip. On a 390x844 device the document scrolls **and** the pane
inside it scrolls, and the composer starts below the fold. The user scrolls the outer
document to find the input, then scrolls the inner pane to read the answer.

**Fix.** Give the chat tab a real height context instead of a magic viewport fraction.

```
App.tsx        <main>                    -> no change (other views rely on document flow)
AgentDetail    chat tab wrapper           -> flex column, min-h-0
AgentChat      section                    -> flex-1 min-h-0, drop h-[70dvh]/md:h-[70vh]
               grid                       -> min-h-0
```

The agent header collapses to a single line on the chat tab below `sm`: icon + name, with
the description and parameter `<Reveal>`s hidden. They are reference material, not something
you read while asking a question, and they are still one tap away on the Overview tab.

Verified by measurement, not by eye: at 390x844 the composer's bounding box must be fully
within the viewport with the page scrolled to top.

### P2 — Nine tap targets under 44px, including two primary actions

`min-h-11` is the stated convention. These are the places it is not applied:

| File:line | Control | Now | Note |
|---|---|---|---|
| `Message.tsx:276` | **citation chip** | 24x24 | The most-tapped control in the product, inline in prose, `mx-0.5` from its neighbours |
| `AgentDocuments.tsx:292` | **"Upload and index"** | ~36px | The primary action of that tab |
| `AgentDocuments.tsx:329` | "Check again" | ~26px | |
| `AgentEvaluate.tsx:393` | inline action | ~26px | |
| `Scorecard.tsx:512` | inline action | ~26px | |
| `GoldenSetEditor.tsx:582` | inline action | ~26px | |
| `GoldenSetEditor.tsx:502,641` | inline inputs | ~30px | |
| `GoldenSetEditor.tsx:361,378,528,535,653,695` | buttons | ~32px | |
| `AgentDetail.tsx:119` | "Back to agents" (error state) | ~32px | |

The citation chip is the interesting one, because it cannot simply grow — a 44px box inline
in a paragraph would wreck the line height. **The visual stays 24px; the hit area grows to
44px via a pseudo-element:**

```css
/* index.css -- the gw- prefix is the slot for CSS a utility cannot express */
.gw-chip { position: relative; }
.gw-chip::after {
  content: ""; position: absolute; left: 50%; top: 50%;
  width: 44px; height: 44px; transform: translate(-50%, -50%);
}
```

This is the standard technique and it is invisible: the chip looks identical and the
tappable region overlaps its neighbours' *margins* rather than their hit areas, because
adjacent chips are separated by more than the overlap.

Everything else gets `min-h-11` (and `min-w-11` where the control is icon-only — the
codebase already notes at `CreateAgentWizard.tsx:1319` that "height alone is not a touch
target").

### P3 — Horizontal overflow at 320px

- **`AgentEvaluate.tsx:310` — `min-w-[18rem]`** (288px) on a flex child inside a `p-5` card
  inside `px-4` page padding. At 320px: 320 - 32 - 40 = 248px available. It overflows. The
  only hard `min-w-[...]` in the codebase; becomes `sm:min-w-[18rem]`.
- **`AgentDocuments.tsx:348-407` — a 6-column table.** It scrolls sideways rather than
  reflowing, and the delete button is in the last column, i.e. off-screen at 375px until you
  scroll. Becomes cards below `sm` and a table at `sm` and up — the same data, twice, which
  is the honest cost of a table on a phone.
- **`Message.tsx:110`** — the answer container has no `break-words`. `pre` and `table`
  children are guarded individually, but a 60-character URL in a paragraph pushes the bubble
  wide. Add `break-words` to the container.
- **`App.tsx:169-200` — the nav.** At 320px it is "Groundwork" + initials + admin pill +
  "Sign out" with `gap-3`, already flush. The email is `sm:inline` so it is not the problem;
  the admin pill is. It moves behind `sm:inline-flex`, and the row gets `flex-wrap` as a
  floor.

### P4 — Everything is capped at 1152px

`max-w-6xl` on every view. On a 1920px monitor two-thirds of the width is gutter. This is
what makes a docked Handouts panel impossible without a change, and it is a weakness on its
own. The chat tab goes to `xl:max-w-[90rem]` (1440px); other views stay at `max-w-6xl`
because a dashboard of cards genuinely does not benefit.

### P5 — There is no modal vocabulary

No focus trap, no Escape handling, no scroll lock, no portal. The Handouts drawer is the
first surface that needs all four, so it writes them once in `Drawer.tsx`
([04-handouts-panel.md §3.2](04-handouts-panel.md)) rather than inline.

---

## 2. New surfaces this feature owns

### 2.1 Trace panel — the three new event types

`TracePanel.tsx:28,37` hold two `Record<string, string>` maps with `??` fallbacks, so new
event types already degrade gracefully rather than crashing. They still need entries, and
`TOOL_CALL` needs more than a colour: its payload holds a Python program, and rendering that
as raw JSON in the existing `<pre>` is unreadable.

```ts
TOOL_CALL:   "border-cyan-800/60 bg-cyan-950/40 text-cyan-200"
TOOL_RESULT: "border-cyan-800/60 bg-cyan-950/40 text-cyan-200"
TOOL_ERROR:  "border-rose-800/60 bg-rose-950/40 text-rose-200"
```

Cyan is unused in the current palette (slate, emerald, sky, rose, amber, fuchsia, indigo,
violet, teal are all taken) and reads as "machine did something" beside them.

Descriptions, in the same plain register as the existing ones:

```ts
TOOL_CALL:   "The agent decided to use a tool and chose these arguments."
TOOL_RESULT: "What the tool returned."
TOOL_ERROR:  "The tool failed. The agent was shown this and could try again."
```

**Special-casing `TOOL_CALL` with `tool === "run_python"`**: render `payload.args.code` in
the mono block on its own, above the rest of the payload. Reading the code the agent wrote
is the single most interesting thing in the trace, and burying it inside a JSON dump with
`\n` escapes hides it.

### 2.2 Message — tool activity is visible without opening the trace

A turn that used tools currently looks identical to one that did not until you expand the
trace. The metadata row (`Message.tsx`, beside latency and model) gains a chip:

```
searched twice · made 1 handout
```

Built from `tool_steps` and `handouts.length` on the message. It is a summary, not a
control — clicking it opens the trace, which already exists.

### 2.3 The agentic toggle has to be findable

`agents.tools_enabled` and `max_tool_steps` are real behaviour changes and belong where the
other tunables are:

- **`CreateAgentWizard`** — the tuning step gains a labelled switch, default on, with a one
  line explanation of the cost ("lets the agent search again or write code; adds a few
  seconds when it does").
- **`AgentDetail`** — shown in the parameter grid and editable, so an agent created before
  this shipped can be switched on.

Note the wizard's existing trap: **native constraint validation aborts submit**, which is
why `noValidate` sits on the form and `required` stays on the input. A switch adds no
constraint, so nothing changes — but any new *required* field in that wizard would need the
same treatment.

---

## 3. What is explicitly not being changed

- **No design-system rewrite.** The palette, the surface formula
  (`rounded-xl border border-slate-800 bg-slate-900/50`), the pill formula and the button
  formulas are consistent and good. New components adopt them rather than introducing a
  parallel vocabulary.
- **No router.** `App.tsx` documents the `useState` union as a decision. A URL for the
  Handouts panel would be nice; adding react-router to get it is a change to how the whole
  app navigates and is not this feature.
- **No light theme.** `:root { color-scheme: dark }` and there is no `dark:` variant
  anywhere. Adding one means auditing every colour in the app.
- **No animation library.** The reduced-motion rule kills transitions globally with
  `!important`, so anything built on animation is decoration by definition.
- **No `@tailwindcss/typography`.** `Message.tsx` explains why: `package-lock.json` must
  stay consistent for Render's `npm ci`. The hand-rolled `Components` map is *extracted* to
  `lib/markdown.tsx` so the panel and the message share one copy, but the plugin still does
  not get added.

---

## 4. Files

| File | Change |
|---|---|
| `frontend/src/index.css` | `.gw-chip` hit-area rule |
| `frontend/src/App.tsx` | nav wrap; admin pill `sm:inline-flex` |
| `frontend/src/views/AgentDetail.tsx` | flex-column height context; `xl:max-w-[90rem]` on chat; header collapse below `sm`; P2 fix at :119 |
| `frontend/src/views/AgentChat.tsx` | drop fixed viewport heights; `min-h-0`; three-column grid; drawer toggle |
| `frontend/src/views/AgentDocuments.tsx` | cards below `sm`; P2 fixes at :292, :329 |
| `frontend/src/views/AgentEvaluate.tsx` | `sm:min-w-[18rem]`; P2 fix at :393 |
| `frontend/src/components/Message.tsx` | `.gw-chip`; `break-words`; tool-activity chip; import extracted markdown map |
| `frontend/src/components/TracePanel.tsx` | three event types, styles, descriptions, code rendering |
| `frontend/src/components/Scorecard.tsx` | P2 fix at :512 |
| `frontend/src/components/GoldenSetEditor.tsx` | P2 fixes (7 lines) |
| `frontend/src/components/CreateAgentWizard.tsx` | tools switch in the tuning step |
| `frontend/src/lib/markdown.tsx` | **new** — extracted `Components` map |

---

## 5. Acceptance

Measured in Playwright, not judged by eye. Three viewports: **1440x900**, **834x1112**, **390x844**.

1. `document.documentElement.scrollWidth <= clientWidth` on every view at **320px**. Zero horizontal scroll.
2. Every element matching `button, a[href], input, textarea, select, [role="button"]` has a bounding box with `height >= 44` — or, for `.gw-chip`, an `::after` hit area that does.
3. At 390x844 on the chat tab, the composer's bounding rect is inside the viewport with the page at `scrollTop = 0`, and there is exactly **one** scrollable region in the chat column.
4. At 1440x900 the Handouts panel is docked and the thread column is `>= 560px` wide.
5. At 834x1112 and 390x844 the panel is a drawer: opens, traps focus, Escape closes, focus returns to the toggle.
6. Focus ring is visible on every interactive element and is not clipped by an `overflow` ancestor — the global rule is `outline: 3px` with `outline-offset: 3px`, so a scroll container needs internal padding.
7. Zero console errors and zero React key warnings across all three runs.
8. With `prefers-reduced-motion: reduce` forced, the drawer still opens and closes correctly.
