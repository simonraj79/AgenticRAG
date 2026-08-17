# 01 — The deck harness floor

**Ships first, and alone.** Its deliverable is the ability to watch every other feature in this
folder fail. Building anything else first means writing a case against code that already exists,
which is how `agentic_check.py` S3 went green twice while proving nothing
([loop.md §5](../loop.md)).

---

## What the user gets

Nothing, directly — this feature is entirely test infrastructure. What the *project* gets is the
first honest answer to "does a model-written deck actually open", and a place to put the five
acceptance criteria the other features need.

---

## Why this is a feature and not a chore

`PLAN.md` §1.2: the acceptance criteria for deck robustness were written **twice, in prose, and
never executed** — `06-test-plan.md:183` and `04-handouts-panel.md:404`. `PLAN.md` §1.1 shows the
two artefacts that pass every assertion in the repository today. `PLAN.md` §1.5 lists nine checks
that cannot fail.

`build.md` §5 is unambiguous: **add the case, run it, watch it fail, then build.** This feature is
the "add the case" half for the whole change set.

---

## Technical detail

### A. `scripts/sandbox_check.py` case 3 — deepen it, and watch it fail

Today (`:161-173`) it asserts: exactly 1 artifact, mime ends `presentationml.presentation`,
content starts `b"PK"`, `len >= 10_000`. `CASE_3` (`:91-105`) writes **3 slides** and prints
`"deck written with 3 slides"`. Neither the slide count nor the stdout is read.

Add, in the same case, reading the harvested bytes already in `result.artifacts[0].content`:

```python
from pptx import Presentation          # python-pptx 1.0.2, already in backend/.venv
prs = Presentation(io.BytesIO(art.content))
```

- `len(prs.slides) == 3` — the number `CASE_3`'s own source writes, and the number its own
  `print()` claims. Two things the harness has been ignoring.
- every slide has a non-empty title.
- `"deck written with 3 slides"` appears in `result.stdout`.

**Run it before changing `CASE_3`.** It must pass — this is a deepening of a *true* case, not a
new one, and if it fails the source has drifted from the assertion.

### B. Two new `sandbox_check.py` cases — 16 and 17 — that must FAIL first

These are the two artefacts measured in `PLAN.md` §1.1. Add them as cases whose *code under test*
produces a deliberately bad deck, and assert the harness now notices:

- **16** — `Presentation(); prs.save("deck.pptx")`, zero slides. Harvest succeeds
  (`ok=True`, ~27,387 bytes, starts `PK`), and the case asserts `len(prs.slides) == 0` **is
  detected**, i.e. it is the input feature 02's validator must reject.
- **17** — `open("deck.pptx","wb").write(b"PK\x03\x04 this is not a real pptx")`, 28 bytes.
  Harvest succeeds; `Presentation(io.BytesIO(...))` raises. The case asserts the raise is caught
  and named, never propagated.

Both are **fixture producers** for `deck_check.py`, not just assertions — see D.

### C. `scripts/deck_check.py` — a NEW layer-1 harness

No file under `scripts/` imports anything from `app.handouts` today. This is that file.

- **Invocation:** `backend/.venv/Scripts/python.exe scripts/deck_check.py`
- **Needs:** the venv only. **No DB, no network, no model, no subprocess.** Target < 2 s.
- **Shape:** copy `refusal_check.py:58-62` exactly — a `check(name, condition, detail)` helper, a
  `failures` list, `sys.exit(1)` at the end. Case ids are plain numbers `1`–`N`.
- **ASCII in `print()`.** The Windows console codepage mangles em-dashes, and this has broken
  three throwaway scripts in this repo. `ascii(value)` when you need to see what is there.

It owns two families of case:

**C1 — the ten uncovered pure functions** (`PLAN.md` §1.4). Cases 1–10, one per function, at
minimum the branch each function's own docstring says is easy to get wrong:
`_problem`'s second branch, `_primary_artifact`'s three tiers, `_strip_fence`'s
`count("```") != 2` guard, `_join_attempts`' one-vs-two, `render`'s `.replace` (a prompt gaining
an f-string turns into `KeyError: 'brief'` **at generation time**, inside the 20-minute suite),
`Material.is_empty`, `provisional_filename`'s 200-char truncation and the hostile brief
`'a"; rm -rf /'` that `04-handouts-panel.md:405` names in prose.

**C2 — the deck cases**, which are the acceptance criteria for features 02–05. Numbered from 11.

### D. Committed fixture decks — `scripts/fixtures/decks/`

**This is what makes every deck criterion executable without a model.** Four `.pptx` files,
generated once by B's cases and committed:

| File | Property |
|---|---|
| `empty.pptx` | zero slides, 27,387 bytes, starts `PK` — the §1.1 finding, frozen |
| `junk.pptx` | 28 bytes of `b"PK\x03\x04 ..."` — opens as a zip header and nothing else |
| `thin-honest.pptx` | **3 slides**, titles, short bullets — a correct honest-shrink deck. **The R5 control** |
| `overflow.pptx` | 6 slides, one bullet of ~900 characters | 

Round-tripping committed bytes decouples every deck assertion from the model, so a validator
change is tested against a **known-bad** deck rather than hoping a live model produces one. It is
also what lets feature 02's case run in milliseconds.

`thin-honest.pptx` is not optional. A validator that was **deleted** passes every assertion that
only checks bad decks are rejected; only a case asserting a *good* deck is accepted can tell a
working detector from an absent one. That pairing is the `route_specialist_check.py` cases 25/26
pattern, and it exists because the same defect shipped once already.

### E. The three-state result — `unmeasured`

`ui_check.py:75-99` has `Results.check` / `Results.unmeasured`, prints `[warn] ... <- NOT
MEASURED`, counts separately, and **prints the unmeasured list even on a green run** (`:565-569`).
Neither `agentic_check.py` nor `sandbox_check.py` has it. Five of `PLAN.md` §1.5's nine
cannot-fail checks would have surfaced immediately if they did.

Port it to both, then apply it to the five that need it:

| Check | Change |
|---|---|
| `agentic_check.py` S8b (`:1640`) | the `if ready:` guard reports **unmeasured** instead of appending nothing |
| `agentic_check.py` S11 (`:1672-1675`) | assert `statements > 0` first; zero captured statements is **unmeasured**, not green |
| `agentic_check.py` S5 (`:509`) | `len(calls) == 0` is **unmeasured** — the disjunction currently collapses to `bool(out.answer)` |
| `agentic_check.py` S10 (`:612-615`) | zero markers is **unmeasured**, not a pass |
| `agentic_check.py` S23 (`:1281-1283`) | `all([])` over an empty dict is **unmeasured** (already documented as deliberate at `:1247-1250`; this makes it visible rather than folded into `ok`) |

S13 (`:694`) is left alone but **gains a note**: `ok = bool(total)` cannot fail on its named
property when the model self-initiates a search, so `PLAN.md` and any future document must not
cite S13 as proof of the gap trigger.

### F. `agentic_check.py` S8 — the criterion that has been passing junk

`:1632` is `ok = row["status"] == "ready" and row["byte_size"] > 0`. Replace with a per-recipe
content check — the four `06-test-plan.md:183` promised, which is why this is *executing an
existing criterion*, not adding one:

| recipe | assertion |
|---|---|
| `deck` | `Presentation(io.BytesIO(body))` opens, `len(prs.slides) >= handout_deck_min_slides` |
| `chart` | starts `\x89PNG` **and** `PIL.Image.open` succeeds with both dimensions > 1 |
| `table` | `csv.reader` parses, `>= 2` rows |
| `sheet` | non-empty after strip, contains at least one `#` heading |

The body comes from the **download route**, which S8b already calls (`:1640-1650`) — so this also
removes S8b's dependence on a separately-successful poll.

Note `--only` never runs the HTTP block (`:1836` gates `http_scenarios` on `if not only:`), so
every handout assertion is all-or-nothing with the ~20-minute suite. **That is the argument for
pushing everything possible down to `deck_check.py`**, and it is why F is the smallest part of
this feature rather than the largest.

### G. The first measurement

`PLAN.md` §1.7: there is **no** recorded first-attempt success rate for the `deck` recipe. Produce
it. A small script or an `agentic_check.py --only` variant that POSTs `recipe="deck"` n ≥ 6 times
and records, per run: `meta["attempts"]`, slide count, whether the first attempt produced a valid
deck, and wall clock.

**Alternate or randomise arm order** (`PLAN.md` R8) — this repo has one recorded measurement that
reported confidently about loop position rather than about its variable.

The number goes in `PLAN.md` §8. It is what `handout_deck_min_slides` and
`handout_deck_max_bullet_chars` get set from, instead of instinct.

---

## Contracts consumed

By reference into [`PLAN.md`](PLAN.md), never restated: §3.1 settings (`handout_deck_min_slides`
is read by F), §3.6 the regression contract (R-a and R-b both land in `deck_check.py`), §5 R1
(`fit_text` must not appear), R5 (the `thin-honest.pptx` pairing), R8 (arm order).

---

## Acceptance criteria

| # | Criterion |
|---|---|
| **A1** | `scripts/sandbox_check.py` **case 3**: the harvested deck opens with `python-pptx`, `len(prs.slides) == 3`, every slide has a non-empty title, and `"deck written with 3 slides"` is in stdout. **Passes on the unmodified `CASE_3` source** |
| **A2** | `scripts/sandbox_check.py` **case 16**: a zero-slide deck harvests `ok=True` at ~27 KB starting `PK`, and is detected as zero-slide. **Must fail before A1 lands** |
| **A3** | `scripts/sandbox_check.py` **case 17**: 28 junk bytes harvest `ok=True`, and `Presentation()` raising on them is caught and named rather than propagated |
| **A4** | `scripts/deck_check.py` **case 11**: `scripts/fixtures/decks/empty.pptx` opens and reports `len(prs.slides) == 0` |
| **A5** | `scripts/deck_check.py` **case 12**: `scripts/fixtures/decks/junk.pptx` raises on open, and the raise is caught |
| **A6** | `scripts/deck_check.py` **case 13**: `scripts/fixtures/decks/thin-honest.pptx` opens with **3** slides, every slide titled, every bullet under 240 chars — **the R5 control that a deleted validator fails** |
| **A7** | `scripts/deck_check.py` **cases 1–10**: one per uncovered pure function in `PLAN.md` §1.4, each exercising the branch its own docstring calls easy to omit |
| **A8** | `scripts/deck_check.py` **case 14**: `grep`-equivalent assertion that neither `app/handouts/` nor `scripts/deck_check.py` imports or references `fit_text` — **R1, asserted structurally rather than remembered** |
| **A9** | `scripts/agentic_check.py` **S8**: per-recipe content checks replace `byte_size > 0`; a zero-slide deck fails S8 |
| **A10** | `scripts/agentic_check.py` **S8b, S11, S5, S10, S23**: each reports `unmeasured` rather than green when its precondition is absent, and the unmeasured list prints on a green run |
| **A11** | A deck first-attempt success rate exists, n ≥ 6, arm order randomised, recorded in `PLAN.md` §8 |

**A2 and A3 must be seen failing before A1's code exists.** A case written after the code is a
case written to pass.

---

## What must keep working

- **R-a and R-b (`PLAN.md` §3.6) both live in this file from now on.** This feature adds them; it
  does not yet have a flag to turn off, so they assert today's behaviour and become the regression
  baseline for features 02–05.
- **`sandbox_check.py`'s security contract is untouched.** Cases 8–14 and the `env` case are the
  reason that file is read (`SECURITY_CASES`, `:58`, drives the banner at `:414-422`). New cases
  are numbered from 16 and go in the behaviour block, never renumbering the security block.
- **Case 15 keeps passing** — the anti-over-blocking control (`matplotlib`+`pathlib` still runs,
  artifacts exactly `["ok.csv","ok.png"]`). It is the case that stops a denylist growing until the
  tool quietly stops working.
- `agentic_check.py --cleanup` still removes the Pinecone namespace **first** (`:282-286`). A
  leaked namespace burns one of the Builder plan's 1,000, and that cap **is** the maximum number
  of agents this deployment can hold.
- `--setup` remains a no-op when documents already exist (`:257-262`).
- The fixture's hostile settings (`chunk_size=250, retrieve_k=3, rerank_top_n=2`, `:206-243`) are
  **not** relaxed. Feature 03 changes retrieval, and it must own its own conditions in a
  `finally`, per `loop.md` §5.

---

## As built — 2026-08-17

**Status: A1–A8 built and verified. A9–A11 written but UNVERIFIED** — they need
`agentic_check.py --setup / --run / --cleanup`, i.e. a live database, OpenRouter, Pinecone,
Cohere and roughly twenty minutes, which has not been run. Treat them as unmeasured, not as
passing.

Layer-1 ladder after the change: `sandbox_check` **18/18**, `deck_check` **17/17** (new),
`ledger_check`, `refusal_check`, `route_specialist_check`, `llm_check` all green, all exit 0.

### Where the plan was wrong

**1. The fixture decks are BUILT at runtime, not committed.** §D said to commit four `.pptx`
files. Building them inside `deck_check.py` instead: large binaries in git are permanent
(`CLAUDE.md`, on the workshop PDFs); a committed `empty.pptx` would freeze *one* python-pptx
version's output while the interesting fact is what **this** library produces; and a reviewer can
read `deck_thin_honest()` and see three titled slides where a binary tells them nothing. The
27,387-byte fact is asserted as a **range** (`20_000 < n < 40_000`) for the same reason. No
`scripts/fixtures/decks/` directory exists.

**2. Case 14 failed on itself, first run.** A check that greps for `fit_text` across a file list
including itself matches its own source line. The needle is now assembled from parts
(`"." + "fit" + "_text("`) with a comment saying why. **A self-scanning check has to be written
so that looking for the thing is not doing the thing** — worth carrying out of this folder. Then
proved it can still fail: planting a real `.fit_text(` call under `app/handouts/` turned it red,
removing it turned it green. `build.md` §5's rule applied to the checker itself.

**3. Case 17's junk deck must be LARGER than 10,000 bytes.** The 28-byte probe from the audit is
caught here by the existing `byte_size < 10_000` floor and accepted only by `agentic_check.py`'s
`byte_size > 0`. So the small version proves nothing about *this* file, and case 17 writes a PK
header plus 20 KB of padding — the realistic truncated-zip shape, which defeats all three old
assertions at once. The 28-byte case lives in `deck_check.py` case 12, where it belongs.

**4. S23 was NOT converted, and A10 was wrong to list it.** The other four collapse *entirely*
when their precondition is absent. S23 has five sub-conditions of which four are always real;
only `shared_ok`'s `all([])` is vacuous. Flagging the whole scenario `[warn]` would hide four
passing assertions behind one that could not run — worse than the defect. The vacuity is spelled
out in the detail line instead (`shared_markers=none <- that half NOT MEASURED`). **The rule:
`unmeasured` is for a check that could not run, never for a check with one vacuous clause.**

**5. A defect the plan did not know about, found while adding the third state.**
`agentic_check.py`'s summary spelled the two-state version out inline
(`'[ok]  ' if o.ok else '[FAIL]'`) instead of calling `_flag`, so a `[rate]` row printed as
`[rate]` during the run and `[FAIL]` in the summary — the one line a reader screenshots
disagreed with the run. Worse, the exit code was `not o.ok`, so **a rate-limited row exited
non-zero**, the exact opposite of what the file's own comment and `CLAUDE.md` both describe:
*"it prints instead of `[FAIL]` for anything matching a rate-limit phrase and does not exit
non-zero."* A documented behaviour that was never implemented — and it meant a Cohere 429 was
indistinguishable from a broken handout job, which `CLAUDE.md` records happening in production.
Both fixed: `_is_failure` excludes `rate_limited` and `unmeasured`, and the summary calls `_flag`.

**6. Three extra cases beyond the criteria**, each cheap and each guarding something feature 02's
validator will trip over:
- **15** — `_repair_message` carries code, problem and stderr verbatim.
- **16** — a Blank-layout slide (`slide_layouts[6]`) has `shapes.title` **None**, not empty. A
  naive `.title.text` walk raises there, inside a function whose whole contract is not raising.
- **17** — the overflow fixture must overflow *by a margin* (measured 830 characters against a
  240 threshold), so re-tuning the threshold from the measured distribution cannot quietly make
  the fixture valid.

### What is still owed

- **A9, A10** — one clean `agentic_check.py --setup && --run && --cleanup`. Cleanup is not
  optional: a leaked Pinecone namespace burns one of the Builder plan's 1,000.
- **A11** — the deck first-attempt success rate, n ≥ 6, arm order randomised. It does not exist
  yet, and `handout_deck_min_slides` / `handout_deck_max_bullet_chars` stay placeholders until it
  does. `DECK_MIN_SLIDES = 3` is currently hard-coded in `agentic_check.py` and moves to
  `PLAN.md` §3.1's setting in feature 02.
- **Opening a real deck in PowerPoint** (`PLAN.md` §6). Not done. Nothing in this feature makes
  it unnecessary — it makes it *checkable a second time*, which is not the same thing.
