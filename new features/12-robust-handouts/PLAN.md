# 12 — Robust handouts: the deck that opens

Change set 2. Written to [build.md](../build.md); the audit is §1, the shared contracts are §3,
and the feature files in this folder **reference §3 and never restate it**.

**The change in one sentence.** The handout pipeline already retries on *"the artefact I wanted
is absent"* — the [loop.md](../loop.md) T2 shape, and loop.md names this very retry as its
worked example. This change set deepens what *"the artefact I wanted"* means, from **a file with
the right extension exists** to **a file that opens and carries teaching material**, reusing the
trigger, the retry and the repair turn that are already built.

---

## 1. Audit

Five parallel audits, 2026-08-17: generation core, sandbox, API/storage/UI, prior-art docs,
harnesses. Everything below is cited; nothing is inferred.

### 1.1 The finding, measured rather than argued

Probed directly against `sandbox.run` in this repo, today, and **re-verified independently in a
second session** against `backend/.venv`'s python-pptx 1.0.2:

| Program | Result |
|---|---|
| `Presentation()` with **zero slides**, `prs.save("deck.pptx")` | `ok=True`, harvested `deck.pptx` **27,387 bytes**, reopens with **0 slides**, becomes a `ready` handout |
| `open("deck.pptx","wb").write(b"PK\x03\x04 this is not a real pptx")` | `ok=True`, harvested **28 bytes**, becomes a `ready` handout that downloads and raises **`zipfile.BadZipFile`** on open |

Both pass every assertion in the repository:

- `scripts/agentic_check.py:1632` — `ok = row["status"] == "ready" and row["byte_size"] > 0`
- `scripts/sandbox_check.py:161-173` — starts with `PK`, `>= 10_000` bytes, mime ends
  `presentationml.presentation`. The case under test writes **3 slides** and prints
  `"deck written with 3 slides"`; neither the slide count nor the stdout is read.

**Nothing between the model's `prs.save()` and a downloadable handout ever opens the bytes.**
`_harvest` (`sandbox.py:446-503`) checks suffix and size; `_primary_artifact`
(`jobs.py:249-274`) matches filename then extension; `_problem` (`jobs.py:277-310`) asks
"did the process fail" and "is there a file with the right extension"; `api/ask.py:1080-1111`
writes whatever arrived.

This is [build.md §7](../build.md)'s table gaining a **sixth** row: a green suite over a product
that is not there.

### 1.2 The criteria for this feature were already written — as prose, and never executed

- `new features/06-test-plan.md:183` promises S8 asserts *"the `.pptx` opens with
  `python-pptx`; the `.png` opens with `PIL`; the `.csv` parses; the `.md` is non-empty"*.
- `new features/04-handouts-panel.md:404` promises *"`recipe="deck"` -> a `.pptx` that
  PowerPoint opens without a repair prompt"*.

Neither is executed. That is build.md's own §4 rule — *"an acceptance criterion names a harness
file and a case id or it is a wish"* — failing for the **third** document, after feature 05's two.
**This change set's first job is to execute two criteria that already exist**, not to invent new ones.

### 1.3 What exists, with the signatures the build writes against

**The retry trigger is already correct.** `backend/app/handouts/jobs.py:277`:

```python
def _problem(
    recipe: Recipe,
    result: sandbox.SandboxResult,
    artifact: sandbox.SandboxArtifact | None,
) -> str | None:
```

body at `jobs.py:300-310`:

```python
    if not result.ok:
        return result.error or "The generated code did not run."
    if artifact is None:
        wrote = ", ".join(a.filename for a in result.artifacts) or "no files at all"
        return (...)
    return None
```

The second branch is the loop.md-compliant half — `result.ok is True` **and** no artefact, i.e.
code that computed the chart and forgot `savefig`. Measured at roughly **1 in 3** trials
(`09-deepseek-agentic.md:129-132`). **This change adds a third branch. It does not add a
mechanism.**

Everything the third branch needs already exists:

```python
# backend/app/handouts/jobs.py
async def _run_sandbox_recipe(model, messages: list[BaseMessage], recipe: Recipe
                              ) -> tuple[bytes, str, str | None, str, int]:   # :475
def _repair_message(code: str, result: sandbox.SandboxResult, problem: str
                    ) -> HumanMessage:                                        # :165
def _primary_artifact(...)                                                    # :249
async def run_handout_job(agent_id, handout_id, user_id, recipe_key, brief,
                          conversation_id) -> None:                           # :318
async def _settle(...)   # second session, finally, no-op unless still pending # :613

# backend/app/handouts/recipes.py
@dataclass class Recipe                                                       # :89-121
RECIPES: dict[str, Recipe]                                                    # :314-359
async def gather_material(db, agent, brief, conversation_id) -> Material:      # :521
    retrieval = await aretrieve(agent, brief)                                 # :552
def render(recipe, *, brief, material) -> str:                                # :362

# backend/app/tools/sandbox.py
async def run(code: str, *, timeout_s: float | None = None) -> SandboxResult:  # :650  never raises
@dataclass(frozen=True) class SandboxResult:                                   # :205
    ok, exit_code, stdout, stderr, artifacts, duration_ms, error, error_kind
@dataclass(frozen=True) class SandboxArtifact: filename, mime_type, content     # :192

# backend/app/rag/retriever.py — per-CALL overrides already exist, unwritten to the agent row
async def aretrieve(agent, question, *, k=None, top_n=None)                     # :278-303
```

### 1.4 Ten pure functions with zero test coverage

No file under `scripts/` imports anything from `app.handouts`. Uncovered and layer-1 decidable:
`_strip_fence` (`jobs.py:117`), `_primary_artifact` (`:249`), **`_problem` (`:277`)**,
`_join_attempts` (`:597`), `_repair_message` (`:165`), `render` (`recipes.py:362`),
`provisional_filename` (`:391`), `derive_title` (`:408`), `Material.is_empty` (`:440`),
`Material.conversation_block` (`:452`), and every prompt constant.

### 1.5 Nine harness checks that cannot fail

`build.md §5`: *"A check that cannot fail reports success, and it reports it in green, forever."*

| Check | The collapse |
|---|---|
| `sandbox_check.py` case 3 | `PK` + `>= 10_000` bytes — satisfied by a **zero-slide** deck (27,387 B) |
| `agentic_check.py` S8 ×4 | `status == "ready" and byte_size > 0` — satisfied by 28 junk bytes |
| S8b download/filename | wrapped in `if ready:` — appends **no Outcome at all** when nothing is ready. Not pass, not fail, not warn |
| S11 bytea leak | `ok = not leaked` with no floor on statements captured. Zero statements → green forever |
| S5 tool self-correction | `len(errors) == 0 or len(calls) > len(errors)` — no tool call short-circuits to `bool(out.answer)` |
| S13 gap trigger | `ok = bool(total)` — any search passes, including a self-initiated one |
| S10 citation integrity | zero citations passes: `[] == list(range(1,1))` |
| S23 shared markers | `all([])` is `True` |
| `mention_popup_check.py` overflow | `None` (element absent) is in the pass set |

Five of these would have surfaced immediately under `ui_check.py`'s three-state
`Results.unmeasured` (`ui_check.py:75-99`), which prints `[warn] ... <- NOT MEASURED` **even on a
green run** (`:565-569`). Neither `agentic_check.py` nor `sandbox_check.py` has that state.

### 1.6 The other three defects the audit surfaced

**a. The deck is written from three chunks.** `gather_material` is one unoverridden
`aretrieve(agent, brief)` (`recipes.py:552`) at `retrieve_k=20 → rerank_top_n=3`
(`models.py:127-129`). `DECK_PROMPT` asks for *"five to eight slides, one idea per slide"*
(`recipes.py:230`) and carries an honest-shrink rule (`:233-234`), so starvation converts into a
four-slide deck that **looks correct**. This is a retrieval defect wearing a prompt defect's
clothes.

**b. `stderr` on a successful run is seen by nobody.** `_render_success`
(`interpreter.py:90-118`) never touches `result.stderr`; the recipe caption is
`result.stdout.strip() or None` (`jobs.py:551`). A program that caught its own exception, printed
`"could not add slide 4"` to stderr, and saved a 3-slide deck reports as a clean 8-slide success.
The same blindness has hidden matplotlib's `MPLCONFIGDIR` warning on **every local run since day
one** (measured).

**c. Truncation is undetectable and un-retryable.** `CODE_MAX_TOKENS = 4_096` (`jobs.py:58`).
Grep confirms **nothing in the backend reads `finish_reason`** — three comments mention it
(`config.py:351`, `llm.py:378`, `eval/generate.py:667`), zero code reads it. A truncated deck
program is syntactically plausible until it stops, fails, retries **at the same cap**, produces
the same length, and fails identically. The deck is the longest of the four recipe programs.

### 1.7 The measurement that does not exist

**There is no recorded first-attempt success rate for the `deck` recipe, at all.** The
reasoning-off decision quoted throughout the repo — *"handouts drop to 5/6 first-attempt files at
3.7× the latency"* (`CLAUDE.md:100`, from `09-deepseek-agentic.md:112-127`) — used **6 chart
recipes per arm**. Charts only. The only `.pptx` evidence anywhere is `sandbox_check.py` case 3,
a **human-written** deck proving python-pptx imports.

Zero recorded deck failures means nobody has looked, not that none occurred. **Feature 01 exists
to make looking possible; the first number this change set produces is that rate.**

### 1.8 What is architecturally CLOSED, with the invariant named

| Closed | Invariant |
|---|---|
| Fetching a template, font, logo or image into the sandbox | **No network in the child.** Two independent mechanisms: absent from `ALLOWED_IMPORTS` + present in `DENIED_MODULE_ATTRS` (`sandbox.py:129-169`), and `disable_network()` rebinding `socket.socket.__init__`, `create_connection`, `socketpair`, `getaddrinfo` (`_sandbox_child.py:306-323`) |
| Passing anything to the child through the environment | **The empty environment is the strongest control, and it is remove-only.** `_minimal_env()` (`sandbox.py:223-269`) spells out eight names; its docstring names the exact temptation — *"the child needs XDG_CACHE_HOME for the font cache"* (`:239-241`) — in advance of §1.6b |
| Reusing the lecturer's own deck as a template | **No original files are stored.** The corpus is `chunks.text` in Postgres plus vectors. There is no source `.pptx` to lift from |
| A handout drawing on a second corpus | **One namespace per agent, derived.** `SearchCorpusArgs` has exactly one field; adding a source parameter re-opens what its omission guarantees (`loop.md:49-58`, `PRD.md:925-927`) |
| A handout retrieving differently from an answer | **The retriever is constructed in exactly one place** (`recipes.py:529-537`, `CLAUDE.md:170-172`) — *"a handout retrieving differently from an answer would make the two disagree about what the corpus says"*. Note this permits per-call `k`/`top_n`, which is a **budget**, not a second retriever |
| Sandboxing the `sheet` recipe | Deliberate: *"the panel still does something useful when the sandbox does not work"* (`recipes.py:23-36`) |
| A partial artefact set reported as success | **All-or-nothing harvest** (`sandbox.py:446-452`) — *"a deck missing half its slides is worse than a deck that failed"* |
| Client-supplied tenancy on any handout route | Every path carries `{agent_id}` through `OwnedAgent`; the job re-selects on the `(id, agent_id)` pair (`jobs.py:361-376`) |
| A second retry, and container isolation | Argued (`jobs.py:482-491`) and PRD open item 24 respectively |

### 1.9 What the change reduces to

Subtracting §1.3 and §1.8, "make handouts more robust, especially slides" is **not a new
subsystem**. It is six things, and two of them are executing criteria that already exist:

1. Make the harness able to see a bad deck **at layer 1** (§1.1, §1.2, §1.5).
2. Add a **third branch** to `_problem` that opens the bytes (§1.1).
3. Stop starving the deck of material (§1.6a).
4. Make failure legible — stderr, `error_kind`, truncation (§1.6b, §1.6c).
5. Let a user see the deck is empty without opening PowerPoint (`preview_text` already exists).
6. Ship the same validation through the **other door**, the `run_python` tool path.

**Zero migrations.** See §3.3.

---

## 2. Architecture after the change

```
  POST /api/agents/{id}/handouts            run_python tool (chat turn)
            |                                         |
            v                                         v
   run_handout_job (jobs.py:318)            interpreter._run (interpreter.py:142)
            |                                         |
            v                                         |
   gather_material  -- aretrieve(agent, brief,        |
                                 k=recipe.retrieve_k, |   << 03
                                 top_n=recipe.rerank_top_n)
            |                                         |
            v                                         |
       render(DECK_PROMPT, ...)                       |
            |                                         |
            v                                         v
   _generate -> _strip_fence -> sandbox.run(code) ----+
            |                                         |
            v                                         |
   _primary_artifact                                  |
            |                                         |
            v                                         |
   +--------------------------------------------+     |
   |  _problem(recipe, result, artifact)        |     |
   |    1. not result.ok            (today)     |     |
   |    2. artifact is None         (today)     |     |
   |    3. validate.check(recipe, artifact)  << 02    |
   +--------------------------------------------+     |
            |                                         |
      problem?  --yes--> _repair_message -> ONE retry -+   (tool path: ToolOutcome(ok=False)  << 06)
            |                                              worded to provoke a second call
            no
            |
            v
   handout.preview_text = outline(artifact)   << 05
   handout.meta["error_kind" | "validation"]  << 04
   status = "ready"
```

The only new module is `backend/app/handouts/validate.py`. `_problem` gains one branch; nothing
else changes shape.

---

## 3. Shared contracts

**Everything in this section is owned here. Feature files reference it by number and never
restate it** — a contract stated twice drifts, and the copy that drifted is never the one you
are reading ([README.md](../README.md), [build.md §3](../build.md)).

### 3.1 New settings — `backend/app/config.py`

Every one carries the measurement that chose it, per repo convention.

| Setting | Default | Why that value |
|---|---|---|
| `handout_validate_artifacts` | `true` | **The off switch the regression assertion needs.** Off, `_problem` must return byte-identical output to today for the same `(result, artifact)` — assertable as a pure function at layer 1 (§3.6) |
| `handout_deck_min_slides` | `3` | `DECK_PROMPT` asks for 5–8 (`recipes.py:230`) but carries an honest-shrink rule for thin material (`:233-234`). A floor of 3 fails an empty or single-slide deck without punishing an honest shrink. **Not 5** — that would fire on correct behaviour, the `refusal_pass = 0/2` defect |
| `handout_deck_max_bullet_chars` | **`400`** | Character count is the **only** honest fitting proxy available — see §5 risk R1. Proposed at 240 and **measured up to 400** once feature 03 widened retrieval and bullets grew with it: §8.3c |
| `handout_code_max_tokens` | `4096` | Promotes the existing `CODE_MAX_TOKENS` constant (`jobs.py:58`) to a setting so §1.6c's truncation retry can raise it on attempt 2. **This section listed it as shipped when it did not exist** — caught by the layer-2 wave, which watched R7 fire live (a floor raised high enough to inflate a deck truncated attempt 2 at exactly this cap). Built afterwards. |

**No per-agent columns.** Per-recipe retrieval lives on the `Recipe` dataclass (§3.2), which is
code, not configuration — recipes are already a closed `Literal` on the request schema
(`api/handouts.py:100`).

### 3.2 `Recipe` gains two fields — `backend/app/handouts/recipes.py:89-121`

```python
retrieve_k: int | None = None      # None -> agent.retrieve_k, today's behaviour exactly
rerank_top_n: int | None = None    # None -> agent.rerank_top_n
```

Passed straight through to the per-call overrides that already exist on
`aretrieve(agent, question, *, k=None, top_n=None)` (`retriever.py:278-303`). `None` on both is
the identity case, so the three non-deck recipes are untouched by construction.

### 3.3 Migration — **there is none, and that is a decision**

The alembic head is **`d4e91c2a7b58`** (`d4e91c2a7b58_orchestrator_and_self_check.py:61`); nothing
names it as a `down_revision`. This change set **adds no revision**, because every fact it wants
to record already has a home:

| Fact | Home | Why not a column |
|---|---|---|
| validation verdict, slide count | `handouts.meta` (JSONB, `models.py:831`) | `meta` already carries `{recipe, brief, chunk_ids, model, conversation_turns, attempts}` (`jobs.py:428-441`) |
| `error_kind` | `handouts.meta`, surfaced via a response field (§3.4) | The sandbox already computes it (`SandboxResult.error_kind`) and it is currently **discarded** |
| attempts | `meta["attempts"]` — **already written at `jobs.py:440` and read by nothing** | |
| deck outline | `handouts.preview_text` (`models.py:812`) — exists, already rendered by `HandoutCard` | |

If a feature file finds it needs a column, it must come back here first. **One migration per
change set, settled in the plan** — two features each adding a column means two revisions racing
for the same `down_revision`.

### 3.4 API surface

**No new routes.** Two additive response fields:

```python
class HandoutOut(BaseModel):        # backend/app/api/handouts.py:122-155
    ...
    error_kind: str | None = None   # "import"|"syntax"|"timeout"|"runtime"|"output"|"invalid"
    attempts: int | None = None     # from meta["attempts"]; 2 means the retry rescued it
```

Both are `| None` with a default, so an old client is unaffected and a row written before this
change reads `None`.

**CORRECTED 2026-08-17 — the snippet above is not sufficient, and getting it wrong ships a
feature that is not there.** `HandoutOut` is `from_attributes=True` against the ORM row, and
**neither `error_kind` nor `attempts` is an attribute of `Handout`** — both live inside the
`meta` JSONB. Declared as bare fields they serialise `None` forever, on every row, with no
error, no warning, valid JSON, and a chip that never renders. That is precisely the "green over
a product that is not there" failure this whole change set exists to correct, and the plan very
nearly reproduced it in its own contract section.

They must be **lifted out of `meta` explicitly** — `api/handouts.py` does it with a
`@model_validator(mode="wrap")` (`_lift_from_meta`) so `HandoutOut` and `HandoutDetail` cannot
disagree. Any future field that reads from `meta` goes through the same place.

One standing risk, documented at that validator: it reads `row.meta`, which is safe only because
`meta` is non-deferred and loaded by every `select(Handout)` in all four call sites. **If a
`load_only()` is ever added that drops `meta`, this raises `MissingGreenlet` from inside Pydantic
serialisation** — the same trap `create_handout`'s `db.refresh` comment already names.

**`"invalid"` is a new `error_kind` value**, minted by feature 02 — the sandbox's five are all
about the *process*; this one is about the *artefact*. It is a plain string, not an enum, and
`status`/`kind`/`origin` are `String(16)` with no CHECK by design (`models.py:777-798`), so a new
value costs nothing at the database. **`error_kind` must stay ≤ 16 chars** if it is ever promoted
to a column.

`meta` itself stays off both response models. Promoting it would re-open the
"the client can name the recipe" hole that `extra="forbid"` (`api/handouts.py:95`) closes.

### 3.5 Trace `EVENT_TYPES` — **unchanged, deliberately**

Recipe handouts write **zero** trace events (`jobs.py` never imports `TraceRecorder`) and carry
`query_id = NULL` by construction (`api/handouts.py:430-432`). `TracePanel` is mounted per
`queryId` (`TracePanel.tsx:84`), so giving recipe handouts a trace needs a **nullable anchor on
`trace_events` plus a new view** — a schema change and a frontend surface for the observability
of a background job. That is a different change set. See §7.

Consequence for feature files: **no feature in this folder may call `TraceRecorder.record`.**
`record` raises on an unknown type (`trace.py:200-204`) and that guard is the only gate, so this
is a rule rather than a preference.

### 3.6 The regression contract — the standing form, made executable

build.md: *"with the feature off, output is byte-identical to today — assert it."*

**Handout bytes cannot be byte-identical between runs** — generation is at temperature 1.0
(`jobs.py:107-114`) and a `.pptx` is a zip carrying timestamps. Asserting on bytes is a wish. The
executable form splits in two, and **both are layer 1**:

- **R-a.** With `handout_validate_artifacts=false`, `_problem(recipe, result, artifact)` returns
  a value **identical** to today's for a table of hand-constructed `(SandboxResult,
  SandboxArtifact | None)` pairs. `_problem` is a pure function; this needs no DB, no model, no
  subprocess.
- **R-b.** With every new field at its default, `render(recipe, brief=..., material=...)` returns
  a string **byte-identical** to a committed fixture. The prompt is what actually regresses, and
  `render` uses `.replace` rather than `str.format` for a reason (`recipes.py:362-381`).

Every feature file names R-a, R-b, or both.

### 3.7 Frontend contract

`HandoutCard.tsx` already renders `handout.error` verbatim with a fallback
(`:208-218`, `data-testid="handout-error"`) and already renders `preview_text` inside a `Reveal`
(`:276-336`). **The transport is sound; only the content is thin.** Features 04 and 05 therefore
write *better strings into fields that already reach the screen*, and touch the card only to add
an `error_kind` chip.

The two hard-coded copies of `TERMINAL_STATUSES` (`HandoutsPanel.tsx:69`, `HandoutCard.tsx:96`)
stay as they are — **no new status is introduced**, so the copies cannot drift on this change.

---

## 4. Build sequence — lowest layer first

`build.md §6`: a change in `app/tools/` or `app/rag/` invalidates every layer above it. And
`build.md §5`: **write the case, run it, watch it fail, then build.**

| # | Feature | Layer | Session opens with |
|---|---|---|---|
| **01** | [Deck harness floor](01-deck-harness-floor.md) — `scripts/deck_check.py`, `sandbox_check.py` case 3 deepened, the three-state `unmeasured` | harness only, no app code | `build-prompt.md` §4 |
| **02** | [Artefact validation](02-artefact-validation.md) — `app/handouts/validate.py`, the third `_problem` branch | `app/handouts/` | `build-prompt.md` §4 |
| **03** | [Deck material budget](03-deck-material-budget.md) — per-recipe `retrieve_k`/`rerank_top_n` | `app/handouts/`, reads `app/rag/retriever.py` | `build-prompt.md` §4 |
| **04** | [Failure legibility](04-failure-legibility.md) — stderr on success, artefacts kept on crash, `error_kind`, truncation retry | `app/tools/sandbox.py` + `app/handouts/` | `build-prompt.md` §4 |
| **05** | [Deck outline preview](05-deck-outline-preview.md) — slide titles into `preview_text` | `app/handouts/` + one frontend test | `build-prompt.md` §4 |
| **06** | [Tool-path parity](06-tool-path-parity.md) — the same validation through `run_python` | `app/tools/interpreter.py` | **[loop-prompt.md](../loop-prompt.md)** — model-decided |

**01 ships first and alone.** It is the only feature whose deliverable is the ability to watch the
others fail. Building 02 first means writing its case against code that already exists, which is
how `agentic_check.py` S3 went green twice while proving nothing.

**06 is the hand-off point.** Its trigger is a model choosing to call `run_python` a second time
after being told the deck is invalid — that is model judgement, and `loop-prompt.md`'s five
questions are cheap now and expensive after the loop exists.

---

## 5. Risk register — each with the tell

| # | Risk | The tell |
|---|---|---|
| **R1** | **`fit_text()` is the obvious way to check text fitting and it raises on Linux.** `pptx/text/fonts.py:41-50` returns font dirs for darwin and win32 and otherwise `raise OSError("unsupported operating system")`. Measured working locally. **Green on this dev box, dead on Render.** | Validation passes every local run and every deck fails in production with an `OSError` whose message says nothing about fonts. **Mitigation: character count only. Never import `fit_text`. Feature 01 asserts the absence of the symbol.** |
| **R2** | **`add_picture()` works or fails depending on whether the program also imported matplotlib.** Pillow registers decoders lazily; step 2 pre-imports only what the source *names* (`_sandbox_child.py:203-265`), so a pptx-only program has `"PNG" in PIL.Image.OPEN == False` and `add_picture` raises `UnidentifiedImageError`. Measured both ways. | Intermittent `UnidentifiedImageError` naming nothing about the sandbox. **Mitigation: `DECK_PROMPT`'s existing image prohibition (`recipes.py:216-218`) stays. Charts-on-slides is out of scope — §7.** |
| **R3** | **The reasoning-off decision was measured on charts only.** 6 chart recipes per arm (`09-deepseek-agentic.md:112-127`), quoted repo-wide as settled for all four recipes. The deck is the longest program. | A deck first-attempt rate that does not match the chart rate. **Mitigation: feature 01 produces the deck number; do not inherit the chart conclusion.** loop.md T1: *"re-run the table when the model changes"* — this is the same rule for a different axis. |
| **R4** | **Adding a parameter to a tool-bound request narrows routing.** Checked 2026-08-17: **28** endpoints serve `deepseek/deepseek-v4-flash-0731`; `tools`+`top_k` (today's shape) routes to **19**; adding `response_format`+`structured_outputs` leaves **15**; adding **`parallel_tool_calls` leaves 1**. | `404 No endpoints found that can handle the requested parameters`. **Mitigation: no feature here widens the request. `disabled_params={"parallel_tool_calls": None}` is far more load-bearing on DeepSeek than the Gemma 404 it was written for.** |
| **R5** | **A validator that is too strict deletes correct behaviour.** The precedent is exact: `refusal_pass = 0/2` was three-quarters a detector bug, and the scorecard *advised deleting the pedagogy*. `DECK_PROMPT`'s honest-shrink rule (`recipes.py:233-234`) means a thin corpus **should** produce a short deck. | A `failed` deck on a corpus that genuinely has three slides' worth of material. **Mitigation: `handout_deck_min_slides = 3`, not 5. Feature 01 case pairs — one asserting a thin-but-honest deck does NOT fire, one asserting an empty deck does. A deleted detector passes the first alone.** |
| **R6** | **A false positive costs one retry; a false negative costs the whole feature.** loop.md T3: strictness follows the cost of being wrong *in each direction*, and they are rarely symmetric. Here they are not. | Asymmetry ignored → a validator tuned like a safety control. **Mitigation: lean permissive. The validator feeds a retry, not a refusal.** |
| **R7** | **Truncation retries identically.** Nothing reads `finish_reason` anywhere in the backend. A cut-off deck program fails, retries at the same 4,096 cap, and fails the same way. | `meta["attempts"] == 2` with two syntactically-truncated programs in `source_code`. **Mitigation: feature 04 reads `finish_reason` and raises the cap on attempt 2.** |
| **R8** | **A measurement can be an artefact of its own loop order.** This repo has one recorded case: `asyncio.gather` measured third in every trial and reported confidently about position rather than concurrency; running it first reversed the verdict. | A deck-vs-chart or on-vs-off number that moves when the arms are reordered. **Mitigation: randomise or alternate arm order and say so in the write-up.** |
| **R9** | **`_settle` refuses to touch a row that is not `pending`** (`jobs.py:643`). Any new status between `pending` and `ready` silently breaks the stuck-row guarantee. | Rows stuck at the new status forever, invisibly. **Mitigation: §3.4 introduces no new status. If a feature file wants one, it comes back to this plan.** |
| **R10** | **Windows dev is measurably less protected than Linux prod.** `import resource` fails, so `RLIMIT_AS`/`CPU`/`FSIZE`/`NPROC` are all absent locally (`_sandbox_child.py:125-132`). | A validator that allocates freely passes locally and is OOM-killed on Render. **Mitigation: validation runs in the parent, not the child, and reads bytes already in memory.** |

---

## 6. Definition of done — the command list that must be green

There is no CI. Every one of these is run by hand, so the order is the protocol.

```bash
backend/.venv/Scripts/python.exe scripts/sandbox_check.py          # case 3 now opens the deck
backend/.venv/Scripts/python.exe scripts/deck_check.py             # NEW, layer 1
backend/.venv/Scripts/python.exe scripts/ledger_check.py
backend/.venv/Scripts/python.exe scripts/refusal_check.py
backend/.venv/Scripts/python.exe scripts/route_specialist_check.py
backend/.venv/Scripts/python.exe scripts/llm_check.py
cd frontend && npm test                                            # + HandoutCard.test.tsx
backend/.venv/Scripts/python.exe scripts/agentic_check.py --setup   # then --run, then --cleanup
cd frontend && npm run build
python scripts/ui_check.py                                         # global interpreter, both servers
python scripts/mention_popup_check.py                              # global interpreter, both servers
```

Then the step that is not a command:

> ### Generate one real deck and OPEN IT IN POWERPOINT.
>
> A green suite in this repo has been wrong five times in five modules, and §1.1 is the sixth.
> Every one was found by reading an answer, opening a page, or reordering a loop — never by a
> passing assertion. `04-handouts-panel.md:404` has promised *"a `.pptx` that PowerPoint opens
> without a repair prompt"* for two documents. **This is the run where somebody does it.**

Also required before ship:

- **A first-attempt deck success rate exists**, n ≥ 6, arm order randomised (R8). It goes in
  §8 and it is the number this change set is measured by.
- `[rate]` rows are treated as **unmeasured**, never as passing.
- `grep -n pywin32 backend/requirements.txt` — the marker survived. Only if anything was frozen;
  this change set adds **no dependency** (python-pptx 1.0.2 and Pillow 12.3.0 are already in the
  backend venv).

---

## 7. What this deliberately does NOT do

Every entry is a closed item from §1.8 or an audit finding that a plan would otherwise re-invent.
**They are here so they do not come back in six weeks.**

| Not doing | Why |
|---|---|
| **Rendering a slide to an image for preview** | Needs LibreOffice or a rendering service. No network in the child (§1.8), and a parent-side renderer is a new heavy dependency on a starter dyno. Feature 05's **text** outline gets most of the value for none of it |
| **Charts on slides** | R2. Works today only by accident of import order. Fixing it means granting `PIL` in `ALLOWED_IMPORTS` (a large C-extension surface with its own file IO) or unconditionally pre-importing `PIL.PngImagePlugin` + `zlib` regardless of what the code names. The second is narrower and defensible — **and it is a sandbox change, not a handout change.** Its own change set |
| **`fit_text()` / real text metrics** | R1. Raises on Linux. Character count is the honest proxy |
| **Company templates, brand fonts, logos** | §1.8 — no network, no stored originals, and the empty environment is remove-only. The only design that does not open a road is the parent placing a template file into the workdir before spawn, which is a **new capability** for the child, not a relaxation. Out of scope |
| **A second retry** | `jobs.py:482-491` argues the curve: what survives the first retry is usually a model that misunderstood the task rather than mistyped it. **Feature 01 produces the number that would justify revisiting this.** Do not pre-empt it |
| **Trace events for recipe handouts** | §3.5. Needs a nullable anchor on `trace_events` plus a new view, because `TracePanel` is per-`queryId` and a recipe handout has no query |
| **A server-side retry-in-place route** | `_settle`'s `pending` guard (`jobs.py:643`) is what makes the terminal-status guarantee work, and re-entering `pending` breaks it (R9). The client-side "Try again" already works when the session created the row |
| **Fixing the tool path's quota bypass** | `api/ask.py:1080-1111` inserts handouts with no `handout_max_per_agent` check. Real, and orthogonal — it is a quota bug, not a robustness bug |
| **`python-docx` / `openpyxl` recipes** | Neither is installed. A dependency addition + two `ALLOWED_IMPORTS` + two `HARVEST_MIME` + a `_handout_kind` branch, and it re-triggers the `pywin32` marker rule |
| **Sandboxing the `sheet` recipe, or giving it a retry** | Deliberate (`recipes.py:23-36`): the panel must still do something useful when the sandbox does not |
| **Running the handout brief through the question rewriter** | `gather_material` never calls `contextualize_question` (`pipeline.py:494`). Wiring it is a few lines — **and a brief is not a question**, and the rewriter's prompt is measured on questions (`rewrite_check.py`). It needs its own measurement, so it needs its own feature file, and it is not this change set's problem. **Recorded as a new PRD open item (§8) rather than built** |
| **Source routing between corpora for handouts** | §1.8. Architecturally closed, and it was already closed once in [11](../11-orchestrator-and-self-check.md) |
| **Container isolation for the sandbox** | PRD open item 24, open deliberately |
| **Object storage for handout bytes** | PRD open items 10 and 25, one apart on purpose |

---

## 8. As built — where the plan was wrong

*Written after the change set ships. This section is the highest-value one in the document,
because it is the only one written with hindsight (`build.md` §3, `00-IMPLEMENTATION-PLAN.md`
§7a).*

### 8.1 A11 — the deck measurement that did not exist. Run 2026-08-17.

`scripts/deck_rate_check.py`, two arms, pair order alternated (R8), against the
`agentic_check.py` fixture agent at its **hostile** budget (`retrieve_k=3, rerank_top_n=2`, seven
chunks total). **The run wedged on trial 11 of 12 and was killed; the completed sample is 10,
balanced 5/5 by arm and by position**, so it is usable as it stands.

Measured with validation OFF — `validate.py` landed at 14:27 and the process had imported
`jobs.py` at 14:06, so no artefact was ever gated. That is the number wanted: measuring the
distribution *through* the validator you are trying to calibrate would be circular.

| | reasoning off | reasoning on | both |
|---|---|---|---|
| **first-attempt success** | **4/5** | **4/5** | **8/10 (80%)** |
| slide counts | 6, 6, 6, 7, 8 | 5, 5, 6, 7, 9 | median **6** |
| wall clock, median (approx, from file mtimes) | **~54 s** | **~161 s** | — |

Distribution over all 10 decks: slides `[5,5,6,6,6,6,7,7,8,9]`, **min 5**; bullet lengths
n=296, p50 **69**, p90 92, p99 109, **max 114**; **zero** untitled slides in any deck.

**Both provisional thresholds survive, and are now measured rather than guessed.**

- `handout_deck_min_slides = 3` — the minimum honest deck observed is **5**, so the floor sits
  two slides below the worst real case at the most starved budget in the repo. Confirmed.
  Deliberately not raised to 4: `loop.md` T3, a false positive costs a retry and a false
  negative costs one bad deck.
- `handout_deck_max_bullet_chars` — **superseded within the hour; see §8.3c.** This arm measured
  a maximum of 114 and concluded 240 was safe with 2.1× headroom. That conclusion was drawn from
  the *narrow* budget and feature 03 then made it the wrong budget: at `rerank_top_n=10` the
  observed maximum is **235**, two percent under the threshold. The shipped value is **400**.
  Left here as written, rather than quietly corrected, because the sequence is the lesson — a
  threshold calibrated under one configuration is not calibrated under another.

### 8.2 R3 is half true, and the half that transfers is not the half that was quoted

`CLAUDE.md:100` and `09-deepseek-agentic.md` record reasoning-off winning **on both axes** for
charts (6/6 vs 5/6, 8.1 s vs 30.4 s). For decks:

- **First-attempt rate is a TIE — 4/5 both ways.** The quality half of the chart finding does
  **not** transfer.
- **Latency is a rout — roughly 3× (≈54 s vs ≈161 s median).** The cost half does transfer, and
  more sharply than the chart arm's 3.7× suggested at the low end.

So `GENERATION_REASONING=false` remains right for decks, **for a different reason than the one
on record.** Anyone re-deciding it from the chart numbers would be reasoning from a result that
does not hold here. Two further marks against `on`, both n=1 and both worth noting: the first
reasoning-on trial in the smoke test returned an **empty generation** (feature 04 §E), and the
run that wedged was also reasoning-on.

### 8.3 THE PREMISE OF FEATURE 03 IS WRONG AS STATED

`PLAN.md` §1.6a and `03-deck-material-budget.md` both assert that a deck asked for five to eight
slides from two or three chunks is *"structurally starved"*, and that the honest-shrink rule
converts that into **"a four-slide deck that looks correct"**.

**Measured, at `rerank_top_n=2` — the most starved configuration in the repo — the model produces
5 to 9 slides, median 6, and never fewer than 5.** That is squarely inside `DECK_PROMPT`'s own
ask. **The deck is not slide-count-starved, and the symptom the feature was written to fix does
not occur.**

What this does *not* establish: slide **count** is not slide **quality**. Six slides from two
chunks means each slide is backed by roughly a third of a chunk, and whether that is dense
material or padding is **unmeasured**. The widening may well still be right — but it is now
right for a reason nobody has evidence for, and its stated symptom is disproved.

### 8.3b — RESOLVED. Feature 03 is KEPT, on evidence it did not originally have

The grounding measurement §8.3 called for was run the same day. `DECK_PROMPT` asks for a
`[filename]` citation on each bullet, so the share of bullets carrying one is a grounding proxy
computable straight from the bytes — no judge, no Ragas, no cost. Comparing **like with like**,
reasoning-off only, validation off in both arms:

| | narrow (`rerank_top_n=2`) | wide (`rerank_top_n=10`) |
|---|---|---|
| decks | 5 | 6 |
| pooled cited bullets | **52.9%** | **75.1%** |
| per-deck spread | 19, 75, 74, **15**, 76 | 73, 74, 75, 75, 76, 77 |
| **decks under 50% cited** | **2 of 5** | **0 of 6** |
| distinct sources cited | `comms-subsystem.md` **only** | **both documents** |

**The average improving is the boring half. The variance collapsing is the finding.** Narrow
produced two decks in five that were ~80% uncited prose; wide produced none, inside a 4-point
band.

And the mechanism is visible rather than inferred: **across all ten narrow decks, not one ever
cited `power-subsystem.md`** — while one of them carried a slide titled *"Power Allocation"*. At
`rerank_top_n=2` the power document was never retrieved, so the model wrote about power out of
nothing, and the honest-shrink rule had nothing to shrink *to*. At `rerank_top_n=10` the whole
seven-chunk fixture fits and both documents get cited.

So the feature stands and **§8.3's disproof of its premise also stands.** Both are true: the deck
was never slide-count-starved, and it *was* source-starved. A feature can be right and its stated
reason wrong, and this folder is the argument for measuring rather than reasoning about which.

Costs, accepted knowingly: prompt input roughly doubles (~5k → ~11k tokens), and an agent with
`rerank_enabled=False` now receives 3× the material.

### 8.3c — the interaction neither feature's plan anticipated

Widening the budget made bullets **longer**, and that collided with feature 02's threshold:

| budget | bullets | p50 | p95/p99 | **max** |
|---|---|---|---|---|
| `rerank_top_n=2` | 296 | 69 | 109 (p99) | **114** |
| `rerank_top_n=10` | 181 | 72 | 127 (p95) | **235** |

`handout_deck_max_bullet_chars` was 240. **One real bullet landed within 2% of it** — and the
threshold had been calibrated in §8.1 against the *narrow* arm, i.e. against a configuration that
no longer ships. At any volume that fires on a legitimate deck, twice, and then fails the handout
outright: `loop.md` T3's asymmetry pointing the wrong way.

Raised to **400**, 1.7× the observed maximum. **The true overflow point remains unmeasured and
cannot be measured here** — that needs rendering, which needs LibreOffice or a font stack the
sandbox deliberately lacks (`fit_text` is R1). So 400 bounds the model's observed behaviour, not
the geometry, and it must be re-read whenever the retrieval budget moves.

The transferable lesson: **a threshold calibrated under one configuration is not calibrated under
another, and two features of one change set can invalidate each other's measurements silently.**
Only measuring the two budgets separately made it visible.

### 8.4 The cap that would have made feature 03 a no-op

Found by the feature 03 build, not by the plan. `MAX_CONTEXT_CHARS = 12_000` against
`chunk_size=800` **tokens** — measured at p50 ≈ 2,400 and max ≈ 3,300 *characters* per chunk on
this repo's own markdown. So at the new `rerank_top_n=10` the old cap delivered **3.7 chunks of
10**, i.e. the feature would have moved the deck from 3 chunks to 3.7 while paying in full for a
k=40 query and a ten-document rerank — **a widening that widens nothing, under a green harness.**
Cap raised to 36,000, and `deck_check.py` case 32 now reads the width off the recipe so raising
`rerank_top_n` again without raising the cap goes red rather than going quiet.

The root confusion is worth carrying out of this folder: **`chunk_size` is in tokens and
`MAX_CONTEXT_CHARS` is in characters**, and reading the first as the second is what made 12,000
look roomy.

### 8.5 A stuck row, reproduced live

Trial 11 hung and its handout row sat at `pending` for 26 minutes until the process was killed —
where it remains. `_settle` only runs inside the job's own `finally`, so a hung (rather than
crashed) job never reaches it, and **there is no server-side sweeper**. The API audit predicted
this exactly; the measurement reproduced it by accident. Not fixed, not in scope, and now a
documented open item rather than a hypothesis.

### 8.6 Verification ledger, 2026-08-17

| Layer | Result |
|---|---|
| `sandbox_check.py` | **22 cases** (was 15 + env) |
| `deck_check.py` | **46 cases** — a new file; nothing under `scripts/` imported `app.handouts` before |
| `ledger_check` · `refusal_check` · `route_specialist_check` · `llm_check` | green |
| `npm test` | **46** (was 39) |
| `npm run build` · `tsc -b` | clean |
| `agentic_check.py` | S8/S8b/**S8c**/S11/S12/S17 + **S28-S33**, each seen failing first, run targeted |
| Measurement | `deck_rate_check.py`, 16 decks over two budgets |
| By eye | a deck made through the real UI; its outline read on the card |

**`--only` now reaches the HTTP block.** It was gated on `if not only:`, so every handout
assertion was all-or-nothing with the ~20-minute suite — which is *why* these criteria kept not
being executed. `--only s11_` now runs in **0.5 s**.

### 8.7 Four defects the layer-2 wave found that no layer-1 case could

1. **`error_kind` was lying.** `jobs._attempt` called `static_check` (which discards the kind) and
   hard-coded `"import"`, while `sandbox.py` had the correct kind all along. A **truncated
   program** — a syntax error — was reported to the user as *"blocked import"*, sending them to
   the allowlist to explain a program that stopped mid-string. Invisible until feature 04 put
   `error_kind` on the card.
2. **§3.1 listed `handout_code_max_tokens` as shipped and it did not exist**, so feature 04 §D
   never shipped, and **R7 fired live**: a floor raised high enough to inflate a deck truncated
   attempt 2 at exactly that cap, then retried at the same cap and failed identically.
3. **A `failed` handout stored no `source_code` at all** — measured `len == 0`. `_run_sandbox_recipe`
   raised before the caller assigned it, so the code a reader most needs was kept only on rows
   that did not need it.
4. **And fixing (3) shipped invisible**, because `HandoutCard` gated the disclosure on
   `status === "ready"`. Stored, returned by `HandoutDetail`, never rendered. Caught by the agent
   that could not edit the frontend saying so plainly.

### 8.8 Two acceptance criteria of mine that were structurally impossible

Worth keeping, because both were written confidently and both are wrong in ways prose hides:

- **04 A8** named `Material.is_empty` as the lever for forcing a failure. It raises a bare
  `ValueError`, and `error_kind` is recorded only for `HandoutFailure` — deliberately, with a
  comment saying so. The scenario would have been red forever.
- **02 A10 / 06 A7** said to raise `handout_deck_min_slides` above what the corpus supports. **The
  repair turn is told the threshold**, so the model just complies: floor 12 → attempt 2 succeeds;
  floor 40 → attempt 2 truncates. *A slide floor is either satisfiable or inflationary.* The
  working lever is `handout_deck_max_bullet_chars = 0` — un-satisfiable by construction, and it
  makes the retry **shorter**.

### 8.9 The full suite, run end to end — 2026-08-17

`--setup` (corpus already ingested, no-op) → `--run` → **`--cleanup`**, the last of which is not
optional: it deleted the Pinecone namespace first, and the Builder plan's 1,000-namespace cap
**is** the maximum number of agents this deployment can hold.

```
  37 / 38 passed
  1 NOT MEASURED -- treat as unknown, never as passing:
    [warn] S5 tool failure is recoverable  calls=1 errors=0 answered=True
           -- no tool error was provoked, so recovery was not exercised
```

**Zero failures. Zero `[rate]` rows.** Every feature in this change set confirmed on a live turn:

| Scenario | What it proved |
|---|---|
| **S28** | an invalid deck ends `failed` with `error_kind=invalid` — feature 02's validator is reached in production, which nine layer-1 cases could not establish |
| **S30** | `deck_chunks=7 (expected 7)` against `table_chunks=1 (expected 1)` — feature 03's per-recipe budget landed, with the untouched recipe as the control |
| **S31** | `error_kind=timeout attempts=2` — feature 04 carrying the kind to the API |
| **S8c** | the stored `preview_text` names the deck's real slide count — feature 05 |
| **S32** | an invalid chat deck is rejected and **not persisted** — feature 06 |

**The `[warn]` is the machinery working, and it is a finding.** S5's assertion was
`bool(out.answer) and (len(errors) == 0 or len(calls) > len(errors))`; with no tool error
provoked, the disjunction short-circuits and the whole check collapses to "did a turn come
back". It would have printed green on this very run. Feature 01's three-state reporting turned
that into `[warn] NOT MEASURED`, excluded from the exit code — so **S5 has plausibly been passing
vacuously for some time**, in the scenario whose own docstring calls self-correction "the single
most valuable behaviour a code interpreter has". Making it *reliably* provoke a tool error is
owed work, not a defect in this change set.

### 8.10 Still to fill in



- The deck first-attempt success rate (§1.7 — the number that did not exist).
- ~~Whether the two thresholds survived contact with the measured distribution.~~ **Answered in
  §8.1 and §8.3c**: `min_slides = 3` survived with a two-slide margin; `max_bullet_chars` did NOT —
  it was 240 by instinct, feature 03 pushed real bullets to 235, and it ships at **400**.
- Whether the third `_problem` branch fired at a rate that justifies a second retry.
- Which of R1–R10 actually happened.
- Which assertion would have caught §1.1 if it had been present, and whether it now is.
- New PRD §10 open items opened (next free number is **33**; follow the dated-subsection pattern
  of items 26–32).

---

## 9. Folding out, when it ships

Per [build.md §9](../build.md). Known already:

| What | Where |
|---|---|
| The empty-deck / 28-byte measurements, and the rule they teach | `CLAUDE.md`, under the failure they cause — a new subsection beside "The code-interpreter sandbox" |
| `fit_text()` raising on Linux (R1) | `CLAUDE.md` — it belongs beside the `resource`-does-not-import note as a **second** Windows/Linux divergence, running the opposite way |
| `add_picture` / lazy PIL decoder registration (R2) | `CLAUDE.md` — it generalises the `matplotlib.os` finding from the opposite direction: there, an allowed library handed over a module it should not have; here, an allowed library is **denied** one it needs, silently |
| `parallel_tool_calls` = 1/28 endpoints on DeepSeek (R4) | `CLAUDE.md`, OpenRouter section |
| **`CLAUDE.md:1081` says "Two things now run off the request thread"** | It is **three** — the handout job is not listed. Fix while folding |
| **`EVAL.md` has zero occurrences of handout/slide/pptx** | Decide whether handout quality is an eval concern or deliberately outside it, and say which |
| **`02-code-interpreter.md:203` documents `RLIMIT_NPROC` as "current + 0"** | The code is `+ 64` (`_sandbox_child.py:170`) and has been for some time |
| The deck first-attempt rate, and the three-state `unmeasured` pattern | §8 here, and `06-test-plan.md` |
| The rewriter-for-briefs question | PRD §10, new dated subsection, item 33 |
| One row for the change set | [`new features/README.md`](../README.md)'s table |
| Check the **root README** still reaches everything a fresh clone needs | `CLAUDE.md` is gitignored; the root README is the tracked entry point |
