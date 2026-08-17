# 04 — Failure legibility

Three signals the system already computes and then throws away, and one it has never looked at.

---

## What the user gets

When a handout fails, the card names *what kind* of failure it was rather than only quoting a
sentence. When a handout succeeds while something went wrong inside it, that no longer vanishes.
And a deck that was cut off mid-program gets a retry that can actually succeed.

---

## The four defects

### A. `stderr` on a successful run is seen by nobody

`_render_success` (`interpreter.py:90-118`) renders exit code, stdout and the file listing, and
**never touches `result.stderr`**. The recipe caption is `result.stdout.strip() or None`
(`jobs.py:551`). So a program that caught its own exception, printed `"could not add slide 4"` to
stderr, and saved a 3-slide deck reports as a clean 8-slide success.

The same blindness has hidden matplotlib's own warning on **every local run since day one**
(measured):

```
Matplotlib created a temporary cache directory ... because there was an issue with resolving
the home directory; it is highly recommended to set the MPLCONFIGDIR environment variable ...
```

That is on the stderr of a *successful* run. Nobody has ever seen it, which is the whole point.

**Fix:** append a trimmed `stderr` to the success render and to the recipe caption, behind a
filter that drops the `[sandbox]` note the child writes on Windows when `resource` fails to import
(`_sandbox_child.py:125-132`) — otherwise every local run gains noise and the signal is buried
again on day two.

### B. Artefacts are dropped on a non-zero exit

`sandbox.py:601-612` returns `artifacts=[]` on a crash. A deck that saved successfully and then
raised on the `print()` line comes back with nothing, and `_problem`'s branch 1 fires with a
runtime error rather than branch 2's more useful "you wrote no file".

**Fix:** harvest first, return the artefacts **alongside** `ok=False`. The size caps run
identically, so this weakens nothing — it gives the retry a "you got as far as this" signal, and
it gives feature 02's validator something to inspect on a partially-successful run.

**The timeout path (`sandbox.py:566-585`) stays as it is**, returning `artifacts=[]` explicitly:
the workdir is deleted at `:646-647` and a file written by a program that was killed mid-write is
not a file. Say so in a comment, or someone will "fix" the asymmetry.

### C. `SandboxResult.error_kind` is computed and discarded

Five values already exist — `"import" | "syntax" | "timeout" | "runtime" | "output"` — computed
at every failure site and never stored. Only the prose message reaches `handouts.error`
(`jobs.py:461`, `_settle` `:646-650`).

**Fix:** record it into `handouts.meta`, surface it via `HandoutOut.error_kind`
(`PLAN.md` §3.4), render it as a small chip on the failed card. `"invalid"` from feature 02 joins
the set.

This is what turns *"the job failed without recording a reason"* and a wall of prose into a
class the user can act on: `timeout` means ask for less; `import` means the model reached for
something unavailable; `invalid` means the file was produced and is not usable.

### D. Truncation is undetectable and retries identically

`PLAN.md` §1.6c. `CODE_MAX_TOKENS = 4_096` (`jobs.py:58`). Grep confirms **nothing in the backend
reads `finish_reason`** — three comments mention it (`config.py:351`, `llm.py:378`,
`eval/generate.py:667`), zero code reads it.

A truncated deck program is syntactically plausible right up to where it stops. It fails with a
`SyntaxError`, the model is told its code raised, it regenerates **at the same 4,096 cap**,
produces the same length, and fails identically. Both attempts land in `source_code` joined by
`ATTEMPT_SEPARATOR` and look like a model that cannot write Python.

The deck is the longest of the four recipe programs — 8 slides × 4 bullets × a full sentence plus
citations. Anyone raising the slide count or bullet richness (feature 03 does exactly that) makes
this more likely, not less.

**Fix, in two halves:**

1. Read `finish_reason` off the response in `_generate` (`jobs.py:150-162`) and return it
   alongside the text. On `"length"`, the problem string says so explicitly — *the model is
   otherwise never told it was cut off*, which is the whole reason the retry cannot work.
2. On a retry triggered by truncation, raise the cap for attempt 2 via
   `handout_code_max_tokens` (`PLAN.md` §3.1). A retry at the same budget is a retry that cannot
   succeed.

**`finish_reason` lives in different places depending on the provider.** langchain-openai puts it
in `response_metadata["finish_reason"]`, and OpenRouter may report `"length"` or a
provider-specific string. Read defensively, treat an unrecognised value as "not truncated", and
**pin the shape in a `deck_check.py` case using a hand-constructed `AIMessage`** rather than a
live call.

### E. An EMPTY generation is run in a subprocess, and the retry is told the wrong thing

**Found by measurement on 2026-08-17, not by reading the code.** Same root cause as §D —
nothing checks that the generation produced anything — but it arrives as *empty* rather than
*truncated*, and it is worse, because the repair turn actively misdirects the model.

Observed on the very first `reasoning=True` deck run (`scripts/deck_rate_check.py`), read back
out of `handouts.source_code`:

```
segment 0: 0 chars of code, lines=0
segment 1: 0 chars of code, lines=0      <- ATTEMPT 1
segment 2: 2028 chars of code, lines=52  <- ATTEMPT 2, correct
ATTEMPT 1 CODE, repr: ''
```

Attempt 1 was **the empty string**. What then happened, in order:

1. `_generate` returned `""` and nobody looked.
2. `static_check("")` accepted it — an empty program is a valid, empty AST.
3. **A subprocess was spawned to run nothing.**
4. `_harvest` found no files, `result.ok` was `True`.
5. `_problem` branch 2 fired correctly: *"The code ran without error but wrote no .pptx file."*
6. `_repair_message` told the model to *"check that the save call is actually reached and that
   the filename matches"* — **a save call in a program it never wrote.**

The trigger was right; the **diagnosis** was wrong. And the run recovered on attempt 2, so the
row ended `ready` and the whole episode is invisible to every error-shaped check. The only
signal is `meta["attempts"] == 2`, which nothing reads — `loop.md` T2 in a third place, and the
third distinct thing in this change set whose only witness is that unread field.

**Fix:** check the generated text before `_attempt` is called at all.

- Empty (or whitespace-only) after `_strip_fence` is its own problem, with its own message —
  *"you returned no code"*, not *"your save call did not run"* — and it must **not** spend a
  subprocess to discover that.
- It composes with §D: `finish_reason == "length"` plus empty content is a truncation at zero
  tokens, and both want the cap raised on attempt 2.
- Do **not** merge it into `_problem`. `_problem` answers "what went wrong with one *attempt*",
  and by then the subprocess has already been paid for. This is a check on the *generation*,
  one layer up, in `_run_sandbox_recipe` between `_generate` and `_attempt`.

**Why this belongs in 04 and not 02.** Feature 02 validates the *artefact*. There is no artefact
here and never was a program. Both are `loop.md` T2 — trigger on the absence of the outcome —
but they are absences of different things, at different layers, needing different sentences.

---

## Contracts consumed

By reference into [`PLAN.md`](PLAN.md): §3.1 (`handout_code_max_tokens`), §3.3 (no migration —
`error_kind` and the truncation flag live in `handouts.meta`), §3.4 (`HandoutOut.error_kind` and
`attempts`, both `| None` with defaults so an old row and an old client both read `None`), §3.5
(**no `TraceRecorder` call**), §3.7 (the card already renders `handout.error` verbatim with
`data-testid="handout-error"`; this feature adds a chip beside it, nothing more).

---

## Acceptance criteria

| # | Criterion |
|---|---|
| **A1** | `scripts/sandbox_check.py` **case 18**: a program that writes `deck.pptx` then raises returns `ok=False` **and** `len(result.artifacts) == 1`. Fails today |
| **A2** | `scripts/sandbox_check.py` **case 19**: the timeout path still returns `artifacts == []` — the deliberate asymmetry, asserted so it is not "tidied" |
| **A3** | `scripts/sandbox_check.py` **case 20**: a program that prints to stderr and exits 0 has that text present in `interpreter._render_success` output, and the `[sandbox]` Windows note is **not** in it |
| **A4** | `scripts/deck_check.py` **case 40**: `_generate` on a hand-constructed `AIMessage` with `response_metadata={"finish_reason": "length"}` reports truncation; with `"stop"` it does not; with the key **absent** it does not |
| **A5** | `scripts/deck_check.py` **case 41**: a truncation-triggered retry requests a **higher** `max_tokens` than attempt 1 — a retry at the same budget cannot succeed |
| **A6** | `scripts/deck_check.py` **case 42**: every `SandboxResult.error_kind` value, plus `"invalid"`, is ≤ 16 chars and maps to a label in the frontend — `PLAN.md` §3.4 keeps it promotable to a column |
| **A7** | `frontend` `npm test`, **`HandoutCard.test.tsx`** (new file): a `failed` handout with `error_kind="timeout"` renders the chip; with `error_kind: null` renders no chip and the row is unchanged; with `error: null` still renders the existing fallback string |
| **A9** | `scripts/deck_check.py` **case 43**: an empty (and a whitespace-only) generation is detected **before** `_attempt` is called — assert the subprocess is never reached, not merely that the run failed. §E |
| **A10** | `scripts/deck_check.py` **case 44**: the empty-generation problem string does **not** mention a save call or a filename, and does name the absence of code. §E's whole point is that the old message misdirects |
| **A8** | `scripts/agentic_check.py` **S31** (new): a handout forced to fail — the cheapest lever is the `Material.is_empty` refusal (`jobs.py:396-405`), reachable by POSTing a brief against an agent with no corpus — returns `error` **and** `error_kind` on `GET .../handouts`. The scenario owns and restores the corpus condition in a `finally` |

**A1 is the one to write first and watch fail**, because it is a change to `sandbox.py`, the
lowest layer in this feature, and everything else reads what it returns.

---

## What must keep working

- **`sandbox.run()` never raises** (`sandbox.py:14-17, 650-674`). Harvesting on the error path
  must not introduce one — `_harvest` already returns `[], error` rather than raising, so wrap
  nothing and change nothing about its contract.
- **The all-or-nothing harvest rule survives** (`sandbox.py:446-452`) — *"a deck missing half its
  slides is worse than a deck that failed"*. Returning artefacts on a crash does **not** relax the
  per-file 5 MB or per-run 15 MB caps; A1's program writes one small file.
- **Output truncation still applies.** `sandbox_max_output_chars = 8_000` in the parent
  (`sandbox.py:534`) and `TRACEBACK_TAIL_LINES = 30` in the wrapper (`interpreter.py:46`). Adding
  stderr to the *success* render must respect the same caps, or a chatty program blows the prompt
  budget on the next turn.
- **No new environment variable reaches the child.** `MPLCONFIGDIR` is the obvious response to
  §A's warning and it is **the specific move `sandbox.py:239-241` warns against by name** — the
  empty environment is remove-only and is the strongest control in the sandbox. This feature makes
  the warning *visible*; it does not act on it. If it is ever acted on,
  `scripts/sandbox_check.py:351-370` asserts the whole key set and must change in the same commit,
  which is the point.
- **`error_kind` is additive and nullable.** Every handout row written before this change reads
  `None`, and `HandoutCard` must render that identically to today — A7's second case.
- **`_settle` still writes a terminal status in a `finally` from a second session**
  (`jobs.py:462-472, 613-653`), and still no-ops unless the row is `pending` (`:643`). No new
  status (`PLAN.md` R9).
- **`content` stays off both response models** and stays `deferred()`. `agentic_check.py` S11 asserts
  no emitted statement selects `handouts.content`; adding fields to `HandoutOut` must not disturb
  it — and feature 01 gave S11 a floor so it can now actually fail.
