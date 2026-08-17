# 05 — The deck outline preview

The smallest feature in the folder and the one that changes the product most, because it is the
only one the user sees on every successful deck rather than only on a bad one.

---

## What the user gets

The Handouts panel shows the deck's slide titles. Today a `.pptx` has **no preview whatsoever** —
the card renders a filename, a size, and the Python that generated it. A chart at least renders
itself as a thumbnail (`HandoutCard.tsx:161-170`); a deck does not. **A user must download the
file and open PowerPoint to learn that the deck is empty, one slide, or wrong.**

---

## Why it is nearly free

`handouts.preview_text` already exists (`models.py:812`), is already on `HandoutDetail`
(`api/handouts.py:154`), and is already rendered inside the card's `Reveal`
(`HandoutCard.tsx:276-336`). For a sandbox recipe it currently holds **the model's own single
`print()` line** (`jobs.py:551`) — a caption, where an outline would fit.

So: no column, no route, no migration, no new UI component. `PLAN.md` §3.3 and §3.7.

Feature 02's validator already opens the deck and walks its slides. This feature is that walk
returning its titles instead of discarding them.

---

## Technical detail

### A. `validate.py` gains an outline function

```python
def outline(recipe: Recipe, artifact: sandbox.SandboxArtifact) -> str | None:
    """A short human-readable summary of what the artefact contains, or None.

    Pure, never raises, ASCII-safe. Called only after `check` has returned None.
    """
```

For `deck`, a numbered list of slide titles plus a count:

```
6 slides
1. Ka-band downlink budget
2. Modulation and coding
3. Ground station handover
...
```

For `table`, the header row plus the row count. For `chart`, nothing — it already has a
thumbnail. For `sheet`, `preview_text` is already the markdown itself and must not be replaced.

### B. `jobs.py` writes it

`jobs.py:551` currently sets the caption from `result.stdout.strip() or None`. It becomes: the
outline if there is one, **falling back to the caption**. Do not discard the caption — a model
that printed something useful should keep saying it, and on a recipe with no outline function it
is all there is.

### C. Cap it

`preview_text` is `Text` and unbounded, but a 40-slide deck's outline is prompt-sized and this
field is returned on a detail fetch. Cap at ~2,000 characters with a `... (N more slides)` tail.

### D. ASCII

`derive_title` and `provisional_filename` already handle model-written text; the outline is model
-written slide titles going into a field that a Python script may print. **`ascii_safe` it** —
the Windows console codepage has broken three throwaway scripts in this repo, most recently on a
`§` and a `│` copied out of a repo file. This is exactly that class of text.

---

## Contracts consumed

By reference into [`PLAN.md`](PLAN.md): §3.3 (no migration — `preview_text` exists), §3.4 (no new
response field — `preview_text` is already on `HandoutDetail`), §3.7 (the card already renders it;
this feature does not touch `HandoutCard` except for one test).

Depends on feature 02's `validate.py` module existing. **It does not depend on validation being
on** — `outline` must work with `handout_validate_artifacts=false`, because the preview is a
product feature and the flag is a regression switch, not a product option.

---

## Acceptance criteria

| # | Criterion |
|---|---|
| **A1** | `scripts/deck_check.py` **case 50**: `outline(deck, thin-honest.pptx)` returns a string containing `"3 slides"` and all three slide titles, in order |
| **A2** | `scripts/deck_check.py` **case 51**: `outline(deck, empty.pptx)` returns a string saying zero slides, and **does not raise**. A preview that crashes on the artefact it exists to warn about is worse than none |
| **A3** | `scripts/deck_check.py` **case 52**: `outline(deck, junk.pptx)` returns `None` and does not raise |
| **A4** | `scripts/deck_check.py` **case 53**: a 40-slide fixture's outline is ≤ 2,000 chars and ends with a `more slides` tail |
| **A5** | `scripts/deck_check.py` **case 54**: `outline` output is pure ASCII for a fixture whose slide titles contain an em-dash, a `§` and an emoji |
| **A6** | `scripts/deck_check.py` **case 55**: with `handout_validate_artifacts=false`, `outline` still returns the outline — the preview is not gated on the regression flag |
| **A7** | `scripts/deck_check.py` **case 56**: for a recipe with no outline function, `preview_text` falls back to the stdout caption **byte-identically to today** — `PLAN.md` §3.6, applied to this field |
| **A8** | `frontend` `npm test`, `HandoutCard.test.tsx`: a `ready` deck with multi-line `preview_text` renders it inside the `Reveal`; with `preview_text: null` the `Reveal` renders exactly as today |
| **A9** | `scripts/agentic_check.py` **S8 `deck`**: the stored handout's `preview_text` names a slide count matching the deck's actual slide count. Reports **unmeasured** if the recipe never reached `ready` |

**A9 is the one that closes the loop with `build.md` §7.** The definition of done requires
somebody to open a real deck in PowerPoint; A9 is what makes the *next* person able to check the
same thing from the card, in one second, without PowerPoint. That is the difference between a
one-off verification and a standing one.

---

## What must keep working

- **`preview_text` for `sheet` is still the study-sheet markdown** (`jobs.py:588-594`), not an
  outline. `sheet` is the direct recipe — no sandbox, no artefact to open — and replacing its
  preview with a summary would delete the only recipe whose preview is the product.
- **The caption fallback** — A7. A recipe with no outline function behaves exactly as today.
- **`HandoutCard`'s chart thumbnail path** (`:161-170`) is untouched. Charts have a real preview
  already; adding a text one beside it is noise.
- **The `Reveal` renders `preview_text` and `source_code` as it does now** (`:276-336`). Both
  attempts are still joined by `ATTEMPT_SEPARATOR` in `source_code` (`jobs.py:597-610`) — that is
  what a user reads to see the retry, and this feature must not crowd it out.
- **No new response field, no new route, no migration.** If the outline turns out to want its own
  field, that decision comes back to `PLAN.md` §3.3 rather than being made here.
- **`outline` never raises**, same contract as `check` — `run_handout_job` must not be taken down
  by a preview.

---

## As built — 2026-08-17

Built by a sub-agent, then **verified through the real UI on a real agent**, which is the step
that mattered. `deck_check` cases 50-56 green, `npm test` 44 passed, `npm run build` clean.

### The feature worked and was unusable, and only the browser showed it

Made a deck through the actual panel on a real corpus. The stored `preview_text` was exactly
right:

```
7 slides
1. Power Subsystem: Generation, Storage, Load Shedding
2. Generation: Four Arrays, 37.6 kW Nominal
3. Storage: 98.4 kWh Across Six Strings
...
```

Written, fetched, rendered, reachable. **And it sat behind a disclosure labelled "CODE"** — the
last place a user looks for slide titles. This feature exists so that somebody can see a deck is
empty *without* downloading it and opening PowerPoint; shipping the outline behind the word
"Code" leaves them doing exactly that.

Every assertion passed. `deck_check` proved the outline was correct; `npm test` proved it
rendered; the API proved it was stored. **Not one of them could see that the control was named
after the wrong thing**, because none of them was a person looking at a card. `build.md` §7's
last step, earning its place for the third time in this change set — after the validator's verb
agreement and the tool path's rejection block, both also found by reading output.

Fixed with `REVEAL_SUMMARY`: a deck and a table say **"Outline and code"**, a study sheet keeps
"Preview", and a chart keeps **"Code"** — honest, because its preview is the thumbnail already
above and the disclosure genuinely only adds matplotlib. Verified in the browser after the change:
deck `"Outline and code"`, chart `"Code"`.

### Two traps for anyone writing browser assertions about handouts

1. **A collapsed `<details>` gives a non-zero `getBoundingClientRect` and an empty `innerText`.**
   A handout card measured 641x192 px, `visible: true`, with `innerText.length === 0` — which is
   the exact signature of the "24px chat pane" failure in `CLAUDE.md`. It was not a defect: the
   card was inside a shut `ALL HANDOUTS` disclosure. **`textContent` (150 chars) versus
   `innerText` (0) is what separates "not rendered" from "not present"**, and a screenshot settles
   it in one look. I nearly reported a false bug from the first reading.
2. **`preview_text` and `source_code` are on `HandoutDetail`, not `HandoutOut`.** The card fetches
   detail on first open, so setting `details.open = true` in JavaScript shows an EMPTY disclosure —
   the click handler is what triggers the fetch. Any assertion must click, not set the property.

### Owed

- **A9** (`agentic_check.py` S8: the stored `preview_text` names the live deck's real slide count)
  is written by a later wave, not here.
- **The outline still does not reach the tool door.** `api/ask.py` never sets `preview_text` when
  persisting a `run_python` artefact, so a deck asked for in chat has no preview. Same asymmetry
  feature 06 exists to close, in a place feature 06 did not cover. Not fixed.
