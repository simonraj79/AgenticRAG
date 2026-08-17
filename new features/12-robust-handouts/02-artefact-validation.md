# 02 — Artefact validation: open the file

The centre of the change set. One new module, **one new branch** in a function that already
exists.

---

## What the user gets

A slide deck that opens. Today a deck that is empty, or is 28 bytes of junk, is stored, marked
`ready`, and downloaded — and the user finds out in PowerPoint. After this, the pipeline notices
before the user does, tells the model what was wrong, and lets the retry that is already built fix
it.

---

## The shape, and why it is small

`PLAN.md` §1.3: `_problem` (`jobs.py:277-310`) already has two branches, and the second is the
[loop.md](../loop.md) T2 shape — `result.ok is True` **and** no artefact, i.e. code that computed
the chart and forgot `savefig`, measured at ~1 in 3 trials. loop.md names this very retry as its
worked example of *"trigger on the absence of the outcome you wanted, never on the presence of an
error."*

This feature adds the **third rung of that same ladder**:

```
absent                  -> _problem branch 2   (today)
present but not a deck  -> _problem branch 3   (this feature)
```

It reuses the trigger, the one retry (`jobs.py:510-531`), the repair turn
(`_repair_message`, `:165-208`), the `failed` path and `_settle`. **No new job shape, no new
status, no migration** — `PLAN.md` §3.3.

---

## Technical detail

### A. `backend/app/handouts/validate.py` — new module, pure functions

```python
def check(recipe: Recipe, artifact: sandbox.SandboxArtifact) -> str | None:
    """Return a model-actionable problem string, or None if the artefact is usable.

    Pure. Reads bytes already in memory. Never raises -- a validator that raises
    would take the handout job with it, which is the failure `loop.md` section 4
    exists to prevent, and it has happened here once already inside the one
    function whose whole job was to prevent it (`08-streaming-and-followups.md:163-194`).
    """
```

Per-recipe validators, dispatched off `recipe.key`:

| recipe | rejects |
|---|---|
| `deck` | does not open with `python-pptx`; `len(prs.slides) < handout_deck_min_slides`; any slide with no title text; any slide whose only text is a template placeholder (`"Click to edit Master text styles"`); any run longer than `handout_deck_max_bullet_chars` |

**Catch broadly on the deck open.** Verified 2026-08-17: `Presentation(io.BytesIO(junk))` raises
**`zipfile.BadZipFile`**, not a `pptx` exception — a `.pptx` is a zip, so the failure surfaces from
the standard library one layer below the one you are calling. Catching only `pptx`'s own errors
would let it escape and take `run_handout_job` with it. `except Exception` and return a string.
| `chart` | not a PNG signature; `PIL.Image.open` raises; either dimension ≤ 1 |
| `table` | `csv.reader` raises; fewer than 2 rows |
| `sheet` | empty after strip; no `#` heading |

`chart`/`table`/`sheet` are here because they cost four lines each and because
`06-test-plan.md:183` promised all four. The deck is the one with a measured failure.

**Return the string in the register `_repair_message` already speaks.** The existing branch-2
message names the expected filename and lists what the code actually wrote; branch 3 must name the
defect and what to do:

> `The file deck.pptx opened but has 0 slides. Add slides with prs.slides.add_slide(prs.slide_layouts[1]) and put a title and bullets on each one before prs.save().`

A refusal the model cannot act on wastes a step (`loop.md:236-238`).

### B. `_problem` gains one branch

```python
    if artifact is None:
        ...                                             # today
    if settings.handout_validate_artifacts:             # << new
        invalid = validate.check(recipe, artifact)
        if invalid is not None:
            return invalid
    return None
```

The flag is `PLAN.md` §3.1, and it exists **for the regression assertion**, not as a product
option — with it off, `_problem` must return byte-identical output to today for the same inputs.

### C. `error_kind = "invalid"`

`PLAN.md` §3.4. The sandbox's five kinds are about the *process*; this one is about the
*artefact*. `jobs.py` records it into `meta` when the second attempt also fails, and it reaches the
card through feature 04's `HandoutOut.error_kind`. ≤ 16 chars, so it stays promotable to a column.

### D. Where it does NOT run

**Not inside the sandbox child.** Validation reads bytes the parent already holds, after the child
has exited. Running it in the child would need `pptx` re-imported under the allowlist, would be
subject to `RLIMIT_AS` — absent on Windows (`PLAN.md` R10) — and would let a hostile program lie
about its own output. Parent-side costs nothing and weakens no control.

**Not in `_harvest`.** `sandbox.py`'s contract is "the sandbox and its controls"; artefact
*quality* is a handout concern. `_harvest`'s all-or-nothing size rule stays exactly as it is.

---

## The two traps this feature must not walk into

**R1 — `fit_text()`.** It is python-pptx's only text-fitting API and the obvious way to answer
"does this bullet fit". `pptx/text/fonts.py:41-50` returns font directories for darwin and win32
and otherwise `raise OSError("unsupported operating system")`. Measured working on this Windows
box. **It would pass every local run and fail every deck on Render**, with a message that says
nothing about fonts. Character count is the honest proxy and the only one available. Feature 01's
case A8 asserts the symbol never appears.

**R5/R6 — strictness.** `loop.md` T3: set strictness from what being wrong costs *in each
direction*, and they are rarely symmetric. Here a false positive costs **one retry**; a false
negative costs **the whole feature**. So lean permissive. Concretely: `handout_deck_min_slides = 3`,
not 5 — `DECK_PROMPT`'s honest-shrink rule (`recipes.py:233-234`) means a thin corpus *should*
produce a short deck, and firing on that is the `refusal_pass = 0/2` defect, where a measurement
punished correct behaviour and then **advised deleting the thing that produced it**.

---

## Contracts consumed

By reference into [`PLAN.md`](PLAN.md): §3.1 (`handout_validate_artifacts`,
`handout_deck_min_slides`, `handout_deck_max_bullet_chars`), §3.3 (no migration), §3.4
(`error_kind` string, `"invalid"`), §3.5 (**no `TraceRecorder` call** — `record` raises on an
unknown type and that guard is the only gate), §3.6 (R-a), §5 R1/R5/R6/R10.

---

## Acceptance criteria

All layer 1. Fixtures from feature 01 §D.

| # | Criterion |
|---|---|
| **A1** | `scripts/deck_check.py` **case 20**: `validate.check(RECIPES["deck"], empty.pptx)` returns a non-None string naming zero slides |
| **A2** | `scripts/deck_check.py` **case 21**: `validate.check(RECIPES["deck"], junk.pptx)` returns a non-None string and **does not raise** |
| **A3** | `scripts/deck_check.py` **case 22**: `validate.check(RECIPES["deck"], thin-honest.pptx)` returns **None** — the R5 control. A deleted validator passes A1 and A2; only A3 separates it |
| **A4** | `scripts/deck_check.py` **case 23**: `validate.check(RECIPES["deck"], overflow.pptx)` returns a string naming the over-long bullet |
| **A5** | `scripts/deck_check.py` **case 24**: `_problem(deck_recipe, ok_result, empty_artifact)` returns non-None — **the branch is wired**, not merely written |
| **A6** | `scripts/deck_check.py` **case 25**: with `handout_validate_artifacts=false`, `_problem` returns **identical** values to today across a table of hand-constructed `(SandboxResult, SandboxArtifact\|None)` pairs — `PLAN.md` §3.6 **R-a** |
| **A7** | `scripts/deck_check.py` **case 26**: `validate.check` returns `None` for every valid chart / table / sheet fixture, and a string for a 1×1 PNG, a header-only CSV and an empty `.md` |
| **A8** | `scripts/deck_check.py` **case 27**: every string `validate.check` can return is non-empty, ASCII, and contains at least one imperative — a message the model cannot act on wastes a step |
| **A9** | `scripts/agentic_check.py` **S8 `deck`**: with a live model, the stored deck opens and clears `handout_deck_min_slides`. Reports **unmeasured**, not green, if the recipe never reached `ready` |
| **A10** | `scripts/agentic_check.py` **S28** (new): with `handout_deck_min_slides` temporarily raised **above** what the fixture corpus can support, the deck handout ends `failed` with an `error` naming slides — and the scenario **restores the setting in a `finally`**. This is the case that makes the branch necessary; without it, A1–A8 all pass over a validator nothing ever calls in production |

**A5, A6 and A10 are the three that matter.** A1–A4 test a function; A5 tests that it is reached;
A6 tests that turning it off restores today; A10 tests that a real job actually fails when it
should. `agentic_check.py` S3 passed twice because it tested a function and not a path.

---

## What must keep working

- **`PLAN.md` §3.6 R-a, asserted as A6.** With the flag off, `_problem` is byte-identical to
  today.
- **`sandbox.run()` never raises** (`sandbox.py:14-17, 650-674`), and neither does
  `run_handout_job` or `_settle`. `validate.check` inherits that contract — a validator that
  raises takes the whole job with it, which is exactly what happened once when `_static_refusal`
  caught `SyntaxError` and `ValueError` and a `MemoryError` escaped
  (`08-streaming-and-followups.md:163-194`). **Catch broadly inside `check`, return a string.**
- **`_primary_artifact`'s three tiers stay** (`jobs.py:249-274`). Tier 2 — any file with the
  recipe's extension — is a deliberate forgiveness (`figure.png` instead of `chart.png`) and
  burning a retry to correct a filename would be worse. Validation happens *after* the tier
  match, never instead of it.
- **`handout.mime_type` is the recipe's, not the artefact's** (`jobs.py:425`). Safe only because
  tier 2 matches on `recipe.extension`. Do not loosen the matcher — the download route serves that
  mime with `Content-Disposition` attached.
- **`_settle`'s `pending` guard** (`jobs.py:643`). No new status (`PLAN.md` R9).
- The four existing `sandbox_check.py` security cases and case 15 are untouched — this feature
  adds no import to the child and does not go near `ALLOWED_IMPORTS`.
- `Material.is_empty` still refuses before generating (`recipes.py:440-450`, `jobs.py:394-405`). A
  regression there produces a beautifully formatted artefact out of parametric memory,
  indistinguishable from a grounded one — and it would now also pass validation.
