# 06 — Tool-path parity

**The model-decided feature in this change set.** Open its build session with
[loop-prompt.md](../loop-prompt.md)'s template, not a plain build prompt — `build.md` §6 names
this as the hand-off point, and its five questions are cheap now and expensive after the loop
exists.

---

## What the user gets

A deck asked for **in the chat** — *"make me a slide deck about the Ka-band link budget"* — gets
the same validation, the same grounding rules and the same second chance as a deck made from the
Handouts panel button. Today it gets none of them.

---

## The asymmetry

There are two origins and they share **no prompt, no grounding rules and no validation code**.

| | `origin="recipe"` (panel button) | `origin="tool"` (chat) |
|---|---|---|
| Decided by | a code path — `Literal["chart","deck","sheet","table"]` on the request | **the model**, calling `run_python` |
| Prompt | `SYSTEM_PREAMBLE` + `_SANDBOX_RULES` + `_GROUNDING_RULES` + `DECK_PROMPT` — ~50 lines (`recipes.py:131-243`) | `TOOL_DESCRIPTION` (`interpreter.py:64-70`) + **one bullet** of `TOOL_GUIDANCE` (`agent_loop.py:132-134`) |
| Deck rules | 16:9, `slide_layouts[0]`/`[1]` only, 5–8 slides, ≤ 8-word headings, 3–5 bullets, `[filename]` citations, no images/icons/custom fonts/template files | none |
| Material | one `aretrieve` (feature 03 widens it) | whatever the turn happened to retrieve |
| Retry on a bad artefact | `_problem` + `_repair_message`, exactly one | **none of its own** |
| Validation after this change set | features 02, 04, 05 | **nothing, unless this feature ships** |

Feature 02 landing alone means the same defect ships through the other door, and the door it ships
through is the one the workshop actually demonstrates.

---

## The five loop.md questions, answered in advance

**1. Is this a tool?** *(loop.md §1, checklist 1)* — **No, and that matters.** The validation runs
every time `run_python` produces a `.pptx`. loop.md: *"If it must run every time, call it
yourself"*, and *"a tool the model must always call is a tool you should call yourself."* This is
a code path inside `interpreter._run`, not a `validate_deck` tool. Making it a tool is
[loop.md](../loop.md) T1 in advance — a tool the model declines to call.

**2. What does it close over?** Nothing new. `interpreter._run` already closes over `ToolContext`
(`registry.py:41-81`) and the artefacts it produced. `validate.check` from feature 02 is a pure
function of `(recipe-ish spec, artifact)`. **The tool path has no `Recipe`** — it has a MIME type
from `HARVEST_MIME`. So `check` must be reachable by artefact kind, not only by recipe key. That
is a small refactor in feature 02's module and it belongs in *this* file's build session, so
feature 02 does not guess at it.

**3. What triggers it, given the model will not reach for it?** The trigger is not the model at
all — it fires whenever a harvested artefact fails validation. What the **model** must then decide
is whether to call `run_python` again, and that is where loop.md's real lesson lands:

- The result comes back as a **`ToolMessage`, never an exception** (`agent_loop.py:436-442`,
  `sandbox.py:650` — both documented "never raises"). `loop.md:225-226`: *"This is the single most
  valuable behaviour a code interpreter has, and it is lost the moment an exception escapes."*
- The message must be **worded to provoke a second call**, naming the defect and the fix, in the
  same register as the static-check refusals which *"name the offending module and list what is
  allowed, so the retry is usually correct"* (`loop.md:236-238`).
- **A refusal the model cannot act on wastes a step**, and steps are bounded:
  `agent.max_tool_steps` and `MAX_CONSECUTIVE_FAILED_STEPS = 2` (`agent_loop.py:172`).

**4. What does a false positive cost, versus a false negative?** *(loop.md T3 — they are rarely
symmetric, and here they are not.)*

- **False positive** — a good deck rejected: one wasted tool step out of `max_tool_steps`, and a
  turn that may run out of budget before answering. **More expensive than on the recipe path**,
  where a false positive costs one retry on a background job nobody is watching in real time.
- **False negative** — a bad deck accepted: the user downloads a broken file, which is today's
  behaviour.

So the tool path should be **at least as permissive as the recipe path, and arguably more**.
Do not tighten thresholds here. `handout_deck_min_slides` is shared; if it turns out to need to
differ, that is a `PLAN.md` §3.1 change, not a local constant.

**5. What makes it necessary?** *(loop.md §5 — a scenario that passes without exercising anything
is worse than none.)* The scenario must produce a chat-requested deck that is **invalid**, and
prove the model was told and acted. Relying on a live model to spontaneously write a bad deck is
the intermittency `agentic_check.py` S24's docstring refuses. So the necessary shape is: assert
the **wiring** at layer 1 with a hand-constructed artefact, and assert the **behaviour** at layer 2
against a corpus deliberately too thin to fill a deck.

---

## Technical detail

### A. `interpreter._run` validates before persisting

`interpreter.py:183-191` puts every harvested artefact on `ctx.artifacts` as a `ToolArtifact`.
Between harvest and that append: run `validate.check` per artefact kind. An artefact that fails is
**not persisted**, and `_render_success` says why.

**Persisting a known-bad file and warning about it is the wrong trade** — the panel would show a
`ready` handout the model has already been told is broken, and `PLAN.md` §1.8's all-or-nothing
principle applies: *"a deck missing half its slides is worse than a deck that failed."*

Note the tool path keeps **all** artefacts (`interpreter.py:183-191`), unlike the recipe path
which keeps one (`jobs.py:268-274`). So validation is per-artefact and a run can persist the good
files while rejecting the bad one — that is a genuine difference from the recipe path and it is
correct.

### B. `TOOL_DESCRIPTION` and `TOOL_GUIDANCE` gain the deck rules

The four load-bearing sentences from `DECK_PROMPT` (`recipes.py:214-237`), condensed: 16:9,
`slide_layouts[0]` and `[1]` only (higher indices vary and `[11]` raises `IndexError` — measured),
a title and 3–5 bullets per slide, and **no images, icons, charts, custom fonts or template
files** — *each one is a crash, not a downgrade*.

**Two cautions.** `TOOL_GUIDANCE`'s final paragraph is Gemma-era and looks like dead weight;
`agentic_check.py` **S16** asserts it is load-bearing — either it or `GENERATION_REASONING` alone
holds tool use at 6/6, removing both drops it to 2/6. **Do not edit or reorder it.** And every
persona prompt opens with `GROUNDING COMES FIRST. It outranks every instruction below` — deck
rules are *format*, not grounding, and must not be phrased in a way that competes.

### C. What must NOT change about the request

`PLAN.md` R4. Checked 2026-08-17, **28** endpoints serve `deepseek/deepseek-v4-flash-0731`:

| Request carries | Eligible |
|---|---|
| `tools` + `top_k` — today's generation shape | **19 / 28** |
| + `response_format` + `structured_outputs` | 15 / 28 |
| + **`parallel_tool_calls`** | **1 / 28** |

This feature adds **nothing** to the request. And `disabled_params={"parallel_tool_calls": None}`
is far more load-bearing on DeepSeek than on the Gemma 404 it was written for — one endpoint, not
a partial loss. loop.md T5: check endpoints *before* adding to a tool-bound request, not after a
404.

### D. Charts on slides stay forbidden

`DECK_PROMPT`'s image prohibition (`recipes.py:216-218`) is carried across, and the reason is now
measured (`PLAN.md` R2): `add_picture()` works or fails depending on whether the same program also
imported matplotlib, because Pillow registers decoders lazily and the child pre-imports only what
the source *names* (`_sandbox_child.py:203-265`). With `pptx` alone, `"PNG" in PIL.Image.OPEN` is
**False** and `add_picture` raises `UnidentifiedImageError` naming nothing about the sandbox.
Telling the chat path it may put charts on slides would make that intermittent failure the
default. `PLAN.md` §7 keeps the fix out of this change set.

---

## Contracts consumed

By reference into [`PLAN.md`](PLAN.md): §3.1 (`handout_validate_artifacts`,
`handout_deck_min_slides` — **shared with the recipe path, not duplicated**), §3.3 (no migration),
§3.4 (`error_kind`), §3.5 (**no `TraceRecorder` call** — but note the tool path *already* writes
`TOOL_RESULT`/`TOOL_ERROR` via `agent_loop.py:577`, and a rejected artefact must land in
`TOOL_ERROR`, an **existing** type), §5 R2/R4/R6.

Depends on feature 02's `validate.py`, reachable by artefact kind rather than only by recipe key
(question 2 above).

---

## Acceptance criteria

| # | Criterion |
|---|---|
| **A1** | `scripts/deck_check.py` **case 60**: `validate.check` is callable with an artefact kind and no `Recipe` — the tool path has no recipe, and this is the refactor question 2 names |
| **A2** | `scripts/deck_check.py` **case 61**: a rejected artefact produces a `ToolOutcome` with `ok=False` and a message naming the defect **and** an action. Not a bare "invalid" |
| **A3** | `scripts/deck_check.py` **case 62**: `interpreter._run` given a run that harvested `good.csv` + `empty.pptx` persists **only** `good.csv` — per-artefact rejection, the genuine difference from the recipe path |
| **A4** | `scripts/deck_check.py` **case 63**: `_run` **never raises** for any validator outcome, including a validator that itself throws. `loop.md` §4 — the one behaviour a code interpreter cannot afford to lose |
| **A5** | `scripts/deck_check.py` **case 64**: with `handout_validate_artifacts=false`, `_run`'s rendered output and persisted artefacts are **byte-identical** to today — `PLAN.md` §3.6 R-a, applied to this path |
| **A6** | `scripts/llm_check.py` **case 30** (new): the tool-bound request carries no `parallel_tool_calls`, no `response_format` and no `structured_outputs` after this feature — R4, asserted structurally |
| **A7** | `scripts/agentic_check.py` **S32** (new): with `retrieve_k` starved so a chat-requested deck cannot fill `handout_deck_min_slides` — the scenario **owns and restores it in a `finally`** — a `TOOL_ERROR` event is recorded and **no invalid handout row is persisted**. This is question 5's necessity case |
| **A8** | `scripts/agentic_check.py` **S33** (new): the regression — with `agent.tools_enabled = False` the turn's output is byte-identical to the classic path, and no handout is created. The standing form, and the scenario owns the flag |
| **A9** | `scripts/agentic_check.py` **S16 still passes unchanged.** `TOOL_GUIDANCE`'s final paragraph is load-bearing and this feature edits that constant |

**A7 is written the `loop.md` §5 way**: it starves retrieval **itself** rather than hoping the
model writes a bad deck. A scenario that waits for spontaneous failure is intermittent, and an
intermittent scenario gets re-run until it passes.

**A9 is the trap.** This feature is the only one in the folder that edits `TOOL_GUIDANCE`, and
`CLAUDE.md` records that constant's last paragraph as redundant-looking and load-bearing —
removing it and `GENERATION_REASONING` together drops tool use from 6/6 to 2/6.

---

## What must keep working

- **`agent_loop._execute` catches everything and returns `ok=False` plus a `ToolMessage`**
  (`agent_loop.py:436-442`, "Never raises"). `sandbox.run` likewise (`sandbox.py:650`). A
  validator on this path inherits both — **A4**.
- **`MAX_CONSECUTIVE_FAILED_STEPS = 2`** (`agent_loop.py:172`) and `agent.max_tool_steps` still
  bound the turn. Rejecting artefacts consumes steps; the budget does not grow.
- **`S16`'s disjunction** — A9.
- **`S1`'s regression shape** — with tools off, byte-identical output — A8.
- **The tool path still persists every good artefact**, not just one. That asymmetry with the
  recipe path is deliberate (`interpreter.py:183-191` vs `jobs.py:268-274`) — A3.
- **`_handout_kind`** (`ask.py:369-391`) still maps a MIME to a panel row, with `"file"` as the
  fallback (`:391`). Validation must not change what kind a good artefact becomes.
- **The request is not widened** — A6, and R4's 1-in-28.
- **No new trace event type.** A rejected artefact uses `TOOL_ERROR`, which exists and already has
  entries in both `TracePanel` maps (`TracePanel.tsx:49-81`). Adding a type would need
  `EVENT_TYPES` **and** both maps in the same commit, and a missing map entry degrades silently.
