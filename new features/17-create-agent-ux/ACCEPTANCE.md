# 17 — acceptance criteria, and what each one caught

Every row names a harness file and a case id. Cases marked **caught a real defect** were
watched failing before the code that satisfies them existed; that is the difference between
a criterion and a wish.

**Final state, 2026-08-28:** `scripts/wizard_check.py --live` **50 passed, 0 failed, 1 not
measured**. `npm test` **79 passed** (was 68). `scripts/ui_check.py` **14 passed, 0 failed, 1 not
measured**. `scripts/palette_check.py` PASS. `tsc --noEmit` and `npm run build` clean.

---

## Browser cases — `scripts/wizard_check.py`

| id | asserts | outcome |
|---|---|---|
| W1 | wizard content width >= 720px at 1440, >= 620px at 834 | **926px** of 1440 (was 511) |
| W2 | every RESOLVED grid track >= 260px (persona) / 200px (controls), measured in pixels at 1440/834/390/320 | persona 297–378px (was 159); narrowest control track 320px |
| W3 | every step's scrolling body has clientHeight > 0 and its footer action is on screen | shortest body 553px; lowest footer 838 of 900 |
| W4 | zero horizontal overflow at 320px, at BOTH document and panel level, all four steps and both tuning modes | **caught a real defect** — 59px, then 108px, then 119px of overhang across three iterations before the cause was found |
| W6 | the dialog cannot be pushed off its own screen: heading does not move, neither root nor panel is a scrollport, close and rail stay reachable — driven by `scrollIntoView` AND by focusing a control low in the tallest step | **caught a real defect found by eye, not by a case.** Mutation-tested: reverting the fix fails all three assertions (heading moved −71px, close button off screen) |
| W7 | in the DEFAULT tuning mode, all ten parameters have a visible explanation >= 40 chars, reported per parameter BY NAME | **failed ten times over** against the starting code; now 62–89 chars each |
| W10 | the ten values shown on Review are the ten values STORED on the row, read back over the API | 10/10 columns agree |
| W12 | every control >= 44px tall, >= 44px wide when icon-only, at 1440/390/320 on all four steps | **caught a real defect** — the step-rail buttons were 36px wide below `sm`, on the flow's primary navigation, on phones |
| W13 | zero console errors across the whole drive | 0 errors |
| W14 | the selected persona is filled AND outlined differently from an unselected one | **caught a real defect** — `${CARD} border-accent bg-accent-soft` tied on specificity with `CARD`'s own `border-line` and `bg-surface`, so NEITHER rendered and choosing a persona changed nothing visible. Asserting the two halves **separately** then caught a half-fix: it went green on the fill while still red on the border |
| W15 | a live region is mounted in the wizard BEFORE the reset notice has text, and the same element later carries it | **failed twice** against the old code — no region existed until the notice did, so it arrived already containing its text and would never have announced |
| W16 | full-bleed sheet below `sm`, centred card at 1440 | **failed at 390 and 320.** Its 1440 row is green today and is a guard, not evidence: without it, "make every viewport full-bleed" satisfies the case and deletes the centred dialog |

W2's limitation is recorded rather than papered over: **a green W2 is not evidence that
container queries shipped.** The width change alone lifted every track over the floor. The
residual viewport-keying was visible in the numbers (a 20% wider panel gaining 50% more
columns) and was fixed on that evidence, not on the case.

## jsdom cases — `frontend/src/components/CreateAgentWizard.test.tsx`

These are deliberately about copy and state, never layout. **jsdom has no layout engine**, so
every width is zero — the 511px content column, the 159px cards and the 31px text collision
were all invisible to a green suite in this very file.

| id | asserts | note |
|---|---|---|
| CW1 | the required name is focused and gates step 1 | pre-existing |
| CW2 | every parameter has a >= 40-char explanation in the mode the user LANDS in, plus its plain label and its column tag | mutation-tested: removing one `help` prop turns it red |
| CW3 | the persona card's summary is DERIVED from that template's row | fixture uses 4-of-17-at-640, a combination no seeded persona has, so a hardcoded summary cannot pass |
| CW4 | the splitter displays *At headings* / *At paragraphs* while storing `markdown` / `recursive` | a relabel reaching the wire would change how every future upload is split |
| CW5 | the two inert parameters stay visible, and no longer claim to do anything | asserts the absence of the two false sentences specifically |
| CW6 | Review's `data-value` carries the unformatted stored value | asserts raw, not formatted text — a case matching "640 tokens" breaks the next time a unit is pluralised, and fails as though data were lost |

## A case that was written, went green, and was then DELETED

**W17** asserted that the "recorded, but changes nothing today" group arrived collapsed. That
behaviour shipped and was reversed within one run — see [PLAN.md §11](PLAN.md). W7 and a collapsed
group are not both satisfiable, because collapsing is precisely the act of taking explanations off
the arrival screen.

It is deleted rather than skipped or left red: a case that cannot pass is noise, and a skipped one
claims the behaviour is unmeasured when it was decided against. Two findings from its short life
are kept in `scripts/wizard_check.py` where it stood, because both are about harnesses generally:

- **Its first draft went green against code with no group disclosure at all**, by finding the first
  per-parameter "Why this matters" `Reveal` and opening that instead. A structural assertion must
  resolve the SPECIFIC element it means, never the first of its kind in the subtree.
- **Its visibility predicate used a bounding rect**, on the premise that closed `<details>` content
  measures 0x0. In the Chromium bundled here it reported 60x1280 with `checkVisibility()` false,
  while W7's `innerText` gate got the same page right in the same drive. Use `checkVisibility()` or
  text, never a rect.

## Cases that were considered and NOT written

- **A case asserting `overflow-clip` on the panel.** It would pass a future refactor that kept
  the property and lost the behaviour, and fail one that reached the same outcome another way.
  W6 asserts the outcome — did the heading move, is the close button reachable — which is the
  thing anyone actually cares about.
- **A case counting how many times `score_threshold` appears in `ask.py`.** The plan named two
  trace sites; there are three (`:1174`, `:1195`, `:1614`). A count would go red for the wrong
  reason. The copy says "printed on every turn" rather than counting.

## Known not-measured, with the reason

- **W13, the Better Auth proxy.** (`ui_check.py` A10 now buckets this the same way — fixed
  2026-08-28, and proven in both directions: NOT MEASURED on an injected 502, still FAIL on an
  injected `console.error`.) `vite.config.ts` forwards `/api/auth/*`
  to the Node service on `:3000`, which is not running in local development, so Vite answers
  502 and the SPA logs it. Verified environmental, not a regression:
  `curl localhost:5173/api/auth/session` → **502** while `curl localhost:8000/api/health` →
  **200**. `wizard_check.py` buckets it as NOT MEASURED naming the service;
  `ui_check.py` predates that distinction and reports it as a FAIL **intermittently**,
  depending on whether the token probe fires during its drive — it was seen both ways on
  unchanged code. Left alone deliberately: editing a shared regression harness so this branch
  reads greener is the wrong instinct, and a suite that goes red because a service nobody
  started said no teaches its reader to ignore red.
- **`ui_check.py` A8, citation chips.** "No chips in this thread." Pre-existing.

## Database safety

`wizard_check.py` owns its subject: its own identity `wizard-check@groundwork.local`, agents
named `wizard-check <hex>` with the name printed **before** the POST so a row orphaned by a
kill is findable, deletion by id and by prefix sweep in a `finally`, plus a `--cleanup` mode
runnable independently because a `finally` does not cover a killed process. Agent count is
compared before and after and fails the run if it moved. `DATABASE_URL` points at the live
Render Postgres holding real users. Verified across every run: **0 → 0**.
