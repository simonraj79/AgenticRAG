# 17 — The create-agent flow

**Status:** SHIPPED, 2026-08-27.
**Closes:** the reported complaint that the flow is "a small overlay [that] makes it
difficult to view the entire process", plus two user-facing statements that are false.

---

## 1. What is actually wrong

Measured in a real browser at 1440x900 against the running dev servers, before any change.
Full table in [MEASUREMENTS.md](MEASUREMENTS.md).

| | measured |
|---|---|
| Content width | **511px of a 1440px viewport** (35%) |
| Persona cards | 9 cards at **159px** each |
| Descriptions truncated by `line-clamp-3` | **6 of 9** |
| Horizontal overflow inside the panel | **40px** (step 2), **49px** (step 3) |
| Step 3 in Customize | **2.5 screens** of scrolling |
| Longest help string | 296 chars in a **239px** column = 10 wrapped lines |
| Chunk-size input vs Overlap label | **31px collision** — renders as `800Overlap` |
| Overlap's number input | **37px outside** the panel's right edge |

**The root cause is a container/viewport mismatch, and it is one line of reasoning.**
Every responsive class inside the wizard is keyed to the VIEWPORT while the box is a
fixed 544px. `lg:grid-cols-3` asks "is the window at least 1024px?", gets yes, and
lays three cards into 511px. `sm:grid-cols-4` does the same with four parameter
columns. So the **phone layout is the correct one** — 342px, one honest column — and
the desktop layout is the degraded one. That is the inversion of the rule this repo
already records under *"When a fix is written for a phone, check what it does at 1440."*

A 2560px monitor gets exactly the same 511px.

**The second half of "cannot view the entire process" is vertical.** Step 3 is ~2,125px
of scroll in a 900px window, and the step rail — the only thing saying where you are and
the only way back — is inside the scroller, so it is the first thing to leave.

**And the copy compounds it.** All ten parameters are labelled with their database column
names; in the mode the user actually lands in there is no explanation of any of them; and
two of the ten describe behaviour that does not exist (§4).

## 2. The surface decision

**Keep the `Drawer` primitive; give it `placement="center"` and an `xl` width.**
960px wide, capped at `min(56rem, 90dvh)` tall, three regions: a fixed header carrying
the title, the close button and the step rail; a scrolling `@container` body; the child's
own sticky footer. `Dashboard.tsx` passes `placement="center" width="xl"`;
`AgentSettingsSheet` passes nothing and keeps the 34rem right-hand drawer it has today.

Result: **928px of content**, and the rail and close button never scroll away.

Four alternatives lose:

- **Widen the right-hand drawer.** Fixes width only — step 3 still scrolls the rail away,
  and a 56rem right-edge panel on a 1024px laptop covers 87% of the screen: a modal
  wearing a drawer's animation. It also cannot be done by editing `lg` in place, because
  `AgentSettingsSheet` shares that token and its `grid-cols-2` was tuned for 511px. A new
  token is needed either way — and if a new token is needed anyway, centre it.
- **A `/agents/new` route.** Refused by the codebase's own recorded decision (`App.tsx:4`,
  "No router"), and it discards the focus-restore-to-the-opener contract `Dashboard` relies on.
- **Flatten the four steps into one page.** This is what the wizard *replaced*. The single
  form pushed the required name field 1113px down a desktop page and made requiredness
  discoverable only by failing. "I cannot see the whole process" reads as an argument for
  this layout; it is the layout that was removed for cause. **Step 1 stays name-only.**
- **Container queries alone, no width change.** Correct and insufficient — honestly re-keyed
  at 511px the persona step becomes one column of nine cards, ~1,876px of scroll. It is not
  an alternative, it is WS2, and it must ship *with* the width change: a centred dialog's
  width is still not the viewport's, so leaving viewport breakpoints inside merely moves the
  same defect from 511px to 928px.

**This does not reverse the measurement in `05-ui-ux-overhaul.md`.** What 05 measured was
inline-page versus overlay. Centring is still an overlay, with the same focus trap, Escape,
scroll lock, inert background and sticky actions. Below `sm` the panel stays full-bleed, so
the phone case 05 fixed is bit-identical.

## 3. The shared contract

Stated here once, and never restated in a workstream — a contract written twice drifts, and
the copy that drifted is never the one you are reading.

`frontend/src/lib/tunables.ts` is the single source of user-facing copy for the ten
parameters, read by **both** the wizard and the settings sheet.

```ts
export type TunableKey =
  | "chunk_size" | "chunk_overlap" | "splitter"
  | "retrieve_k" | "rerank_enabled" | "rerank_top_n"
  | "score_threshold" | "max_rewrites"
  | "tools_enabled" | "max_tool_steps";

/** WHEN a parameter takes effect. The grouping is the teaching. */
export type TunableGroup = "answer" | "upload" | "inert";

export type TunableCopy = {
  key: TunableKey;
  /** Plain English, 2-4 words. What the user reads. */
  label: string;
  /** The real column name, kept as a quiet mono tag. This app teaches RAG;
   *  hiding the vocabulary is a loss, not a simplification. */
  tag: string;
  group: TunableGroup;
  /** ONE sentence, hard cap 110 characters. Sits under the control. */
  help: ReactNode;
  /** Every measured fact that used to be crammed into `help`. Nothing is
   *  deleted; it moves down one tier, behind "Why this matters". */
  detail: ReactNode;
  /** Display-only. Never changes the stored value. */
  format?: (value: string | number | boolean) => string;
};

export const TUNABLES: Record<TunableKey, TunableCopy>;
export const GROUPS: { id: TunableGroup; title: string; blurb: string }[];
```

`GROUPS` titles, taken verbatim from `AgentSettingsSheet`'s existing sections so the two
surfaces speak with one voice:

| id | title |
|---|---|
| `answer` | Takes effect on the next answer |
| `upload` | Takes effect on the next upload |
| `inert` | Recorded, but changes nothing today |

**Stored values never change.** `splitter` stays `"markdown"` / `"recursive"` on the wire;
only the label becomes *At headings* / *At paragraphs*. `format` is display-only.

## 4. The two false statements

Both verified against the backend, not inferred.

**`score_threshold`.** The wizard says *"Below this top similarity score the question
becomes a candidate for rewriting."* `backend/app/api/ask.py:1159` is headed
`# 4. Score check -- OBSERVABILITY ONLY`; `below_threshold` is computed at :1174 and
written to a trace at :1195, and read nowhere else. The trace's own `action` field says
`"advisory -- the agent loop decides whether to search again"`.

**`max_rewrites`.** The wizard says *"0 turns rewriting off."* Its only consumer in
`backend/app/` outside schemas and seeds is `ask.py:1197` — a trace payload key. The real
gate is `settings.rewrite_every_turn`, which is global and has no per-agent path. A user
who follows that instruction believes they disabled a subsystem that is still running.

**Neither slider is deleted.** `07-workspace-shell.md` already decided this for the settings
sheet: *"Hiding them would be tidier and worse: the trace panel prints `score_threshold` on
every turn."* They move into the `inert` group, relabelled honestly.

## 5. Workstreams

All eight landed. Results in [ACCEPTANCE.md](ACCEPTANCE.md).

| id | title | depends on |
|---|---|---|
| WS0 | `scripts/wizard_check.py`, written first and watched failing | — |
| WS1 | `Drawer` gains `placement="center"`, `xl`, and three regions | WS0 |
| WS2 | Re-key all breakpoints to the container scale; lift the rail into the header | WS1 |
| WS3 | `lib/tunables.ts`; `ui.tsx` primitives gain `detail` / `help` slots | WS0 |
| WS4 | Regroup step 3 by when it takes effect; fix the two false statements | WS3 |
| WS5 | Make the preset legible — the persona IS the preset, so say so | WS3 |
| WS6 | The four accessibility defects a wider grid would multiply | WS2 |
| WS7 | Verify, mutate, fold back | all |

**No backend change.** Every value the wizard sends is already accepted by `AgentCreate`.
No migration, no new column, no API change.

## 6. Acceptance criteria

Every criterion names a harness file and a case id, or it is a wish. Full list in
[ACCEPTANCE.md](ACCEPTANCE.md). The six written before any implementation:

| id | file | asserts |
|---|---|---|
| W1 | `scripts/wizard_check.py` | wizard content width >= 720px at 1440 (was 511) |
| W2 | `scripts/wizard_check.py` | every resolved grid track >= 260px (persona) / 200px (sliders), measured in px |
| W3 | `scripts/wizard_check.py` | every step has clientHeight > 0 and its footer on screen |
| W4 | `scripts/wizard_check.py` | zero horizontal overflow at 320px, at BOTH document and panel level |
| W7 | `scripts/wizard_check.py` | in the DEFAULT mode, all ten parameters have a visible explanation >= 40 chars |
| W10 | `scripts/wizard_check.py` | the ten values shown on Review are the ten values STORED on the row |

W7 must fail ten times over against current code. W2 and W4 must fail. If any is green
today it is a case written to pass.

## 7. Out of scope

- A `/agents/new` route, or any router dependency.
- Widening the shared `lg` drawer token in place.
- **A second Fast/Balanced/Thorough preset axis.** Every value set such a preset would carry
  is already a shipped `agent_templates` row, so it is a rename of an existing persona held
  in a second, client-side, drifting copy of numbers the server owns. And it cannot deliver
  the one intent anyone would want from it: a "Fastest" preset is unbuildable here, because
  retrieval is ~11% of a turn (embed 365ms, Pinecone 394ms, Cohere ~830ms) against generation
  at 13.2s. Latency is decided on the **persona** step. If the workshop wants named intents
  later, they are new rows in `agent_templates`, built server-side.
- Deleting `score_threshold` or `max_rewrites` from either surface.
- Making the system prompt editable during creation.
- Backend changes of any kind.
- Reworking `AgentSettingsSheet`'s layout beyond wiring it to `tunables.ts`. It is the file
  that got 511px *right* — zero breakpoint classes, one honest column — which is the
  empirical evidence that the wizard's grids are the outlier, not the baseline.


---

## 8. What shipped, measured

| | before | after |
|---|---|---|
| Content width at 1440 | 511px (35% of screen) | **926px** |
| Persona card width | 159px | **297–378px** |
| Horizontal overflow | 40–49px at 1440 | **0 at every viewport incl. 320px** |
| Parameters explained in the mode users land in | **0 of 10** | **10 of 10**, 62–89 chars each |
| Step rail while scrolled to the bottom | scrolled away | pinned, always reachable |
| Browser cases over this flow | **0** | 43 |
| Unit tests | 68 | 73 |

## 9. Four defects found that were not in the brief

Each was found by measuring or by looking, not by reading source, and each is now pinned.

1. **`${FIELD} w-24` renders `w-full`.** Two width utilities tied on specificity; the number
   input was 242px instead of 96px and hung 152px past its column. This is the original cause
   of the `800Overlap` text collision in the opening screenshot — the input was never 96px, so
   it always ran into its neighbour. Fixed by removing the conflict (a `w-24` wrapper), not by
   out-ranking it. `wizard_check.py` W4.
2. **The dialog could be pushed off its own screen.** `overflow-hidden` clips *and* creates a
   scrollport, so focusing a control low in the panel scrolled the root and dragged the panel
   to top −25 with its title clipped. `overflow-hidden` on the panel fixed the root and moved
   the bug one level down; `overflow-clip` removes it at both. `wizard_check.py` W6,
   mutation-tested.
3. **The step-rail buttons were 36px wide below `sm`** — the flow's primary navigation, on
   phones, against this app's own 44px contract, with a code comment claiming it was fixed
   (the height had been raised; the width had not). `wizard_check.py` W12.
4. **The persona cards and every `Segmented` option had no visible focus ring.** Both wrap an
   `sr-only` radio, so the global `:focus-visible` outline was painted on a 1px clipped box —
   and the code comment asserted this worked. `FOCUS_PROXY` in `lib/styles.ts` moves the ring
   to the element you can see.

## 10. Honest remainders

- **`ui_check.py` A10 fails intermittently** with a 502 on `/api/auth/*`. Environmental: the
  Better Auth Node service on `:3000` is not running in local development
  (`curl localhost:5173/api/auth/session` → 502 while the backend answers 200). Seen both
  passing and failing on unchanged code. `wizard_check.py` buckets this as NOT MEASURED naming
  the service; `ui_check.py` predates that distinction. **Not fixed here on purpose** — editing
  a shared regression harness so this branch reads greener is the wrong instinct.
- **`Drawer`'s `subheader` region is unused.** It was built for the step rail, and the rail
  ended up `sticky` inside the scrolling body instead, because using the region would have
  meant lifting `step`, `furthest` and `goTo` out of the wizard and into the dashboard so a
  generic layout primitive could own a wizard's navigation state. Kept rather than deleted: it
  is a reasonable affordance and the next drawer with fixed chrome will want it. If nothing
  claims it, delete it — an unused prop named for a purpose solved another way misleads.
- **`topNWarning` exists in the wizard and not in the settings sheet.** That is behaviour, not
  copy, so wiring the sheet to `tunables.ts` did not change it. Worth a decision; not taken here.
- **The persona grid still clamps 6 of 9 pedagogy paragraphs to three lines.** Now deliberate
  rather than incidental: the clamp lifts on the selected card, so it is progressive disclosure
  at 297px rather than truncation at 159px.
