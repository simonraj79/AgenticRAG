"""`run_python` -- the LangChain wrapper around the sandbox.

`sandbox.py` does the dangerous half: static check, spawn with an empty
environment, resource limits, kill, harvest, cap. **Read
`new features/02-code-interpreter.md` section 5 before changing anything there**;
this file is the thin part that turns a `SandboxResult` into something a model
can read.

Two properties of that translation are the whole job.

**The tool returns text, never bytes.** Artifacts go onto `ctx.artifacts`, which
`ask.run_turn` persists as Handout rows. A `ToolMessage` carrying a base64 PNG
would blow the context window in a single call -- a 40 KB chart is ~55 KB of
base64, which is more than the entire retrieved context of a normal turn.

**A failure returns the error and the tail of the traceback.** That is what makes
self-correction work: a model that wrote `plt.bar(labels, values)` with mismatched
lengths reads the `ValueError`, fixes it, and succeeds on the next step. It is
the single most valuable behaviour a code interpreter has, and every layer here
is arranged so that nothing can turn a failure into an exception that ends the
turn instead.

**A file this tool keeps is a handout, so it is opened before it is kept.**
Added 2026-08-17 (`new features/12-robust-handouts/06-tool-path-parity.md`), and
the reason is an asymmetry rather than a new idea: the panel button and the chat
turn produce handouts through completely separate code, sharing no prompt, no
grounding rules and -- until this -- no validation. Feature 02 taught
`jobs._problem` to open a `.pptx` before calling it ready; the identical
27,387-byte zero-slide deck went on shipping through THIS door, which is the one
the workshop actually demonstrates.

Two things about that validation differ from the recipe path on purpose.

**It is per artefact, and a run may keep the good files while rejecting the bad
one.** The recipe path keeps exactly one file, so its verdict is all-or-nothing;
this path keeps every file a program wrote, and throwing away a correct CSV
because the deck beside it was empty punishes the half that worked.

**A rejected artefact is not persisted at all.** Keeping it and warning about it
is the wrong trade: the Handouts panel would show a `ready` row for a file the
model has just been told is broken, and the user would download it. So the
rejection reaches the model instead, as a `ToolMessage` naming the defect and
the fix -- which is the same shape as every other failure here, and the reason
nothing on this path raises.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from app.config import settings
from app.tools import sandbox
from app.tools.sandbox import SandboxArtifact, SandboxResult

if TYPE_CHECKING:  # pragma: no cover - import cycle, types only
    from app.tools.registry import ToolContext

log = logging.getLogger("uvicorn.error")

RUN_PYTHON = "run_python"

# How much of stderr goes back to the model on a failure.
#
# A traceback's useful part is at the bottom -- the exception type, its message,
# and the line that raised. The frames above it are matplotlib's internals more
# often than they are the model's code. Thirty lines keeps the raising frame and
# a little context while bounding what a deeply nested library error can spend of
# the next step's prompt.
TRACEBACK_TAIL_LINES = 30

# The prefix the child stamps on its own housekeeping lines
# (`_sandbox_child.py:104-106`). Duplicated across that boundary for the same
# reason `ALLOWED_IMPORTS` is: the child runs as a standalone script under `-I`
# and cannot import from `app.*`. Both copies must change together.
#
# It is filtered out of the SUCCESS render only. On a failure the whole tail of
# stderr goes back verbatim, because a note saying a resource limit could not be
# set may be the explanation for what happened next.
SANDBOX_NOTE_PREFIX = "[sandbox]"


class RunPythonArgs(BaseModel):
    """Two fields, and the second one is not decoration."""

    code: str = Field(
        description=(
            "Python source. Write files to the current directory. You have no "
            "network and no filesystem outside it."
        )
    )
    # `purpose` is what the Handout is titled with, what the trace shows, and --
    # the reason it is required rather than optional -- what measurably improves
    # the code. A model that has to state the goal writes to it.
    purpose: str = Field(description="One line: what this produces and why.")


# The deck sentences are condensed from `DECK_PROMPT` (`handouts/recipes.py`),
# which is ~50 lines the panel path gets and this one never did. They are here
# and not only in `TOOL_GUIDANCE` because a description travels with the tool
# schema: a model choosing to call `run_python` reads this at the moment it
# decides what to write.
#
# **They are FORMAT, and they are worded so they cannot be read as grounding.**
# Every persona prompt in this project opens with "GROUNDING COMES FIRST. It
# outranks every instruction below", and a format rule phrased with the same
# force competes with it -- CLAUDE.md records what happens when two instructions
# compete and the wrong one wins.
#
# The layout restriction is measured rather than cautious: the default template
# carries exactly 11 layouts, so `prs.slide_layouts[11]` raises `IndexError`
# (verified 2026-08-17, python-pptx 1.0.2) and the indices between 2 and 10 are
# real but vary in what placeholders they expose. `[0]` and `[1]` are the two a
# deck can be built from without knowing the template.
#
# The image prohibition is a crash rather than a downgrade, and PLAN.md R2 has
# the mechanism: Pillow registers its decoders lazily and the sandbox child
# pre-imports only what the source NAMES, so in a pptx-only program
# `"PNG" in PIL.Image.OPEN` is False and `add_picture` raises
# `UnidentifiedImageError` naming nothing about the sandbox. Measured both ways.
TOOL_DESCRIPTION = (
    "Write and run Python in a sandbox, and keep the files it writes. Use it "
    "when the user wants a chart, a slide deck, a table or a data file. "
    "matplotlib, python-pptx, pandas and numpy are available; there is no "
    "network and no filesystem outside the working directory. Put the numbers "
    "in the code as literals. "
    "For a slide deck, python-pptx has to be driven a particular way or it "
    "raises: set 16:9 through prs.slide_width and prs.slide_height in Inches, "
    "use prs.slide_layouts[0] for the title slide and prs.slide_layouts[1] for "
    "every content slide and no other index, give each content slide a title "
    "and three to five bullets, and add no images, icons, charts, custom fonts "
    "or template files."
)


def _human_size(byte_size: int) -> str:
    """Bytes as the model should read them. ASCII only, one decimal place."""
    if byte_size < 1024:
        return f"{byte_size} B"
    if byte_size < 1024 * 1024:
        return f"{byte_size / 1024:.1f} KB"
    return f"{byte_size / (1024 * 1024):.1f} MB"


def _tail(text: str, lines: int) -> str:
    """The last `lines` non-empty-ish lines, oldest first."""
    rows = text.splitlines()
    if len(rows) <= lines:
        return text.strip()
    return "\n".join(rows[-lines:]).strip()


def program_stderr(text: str) -> str:
    """What the PROGRAM said on stderr, bounded, with the child's notes removed.

    Public on purpose. The recipe caption in `app/handouts/jobs.py` wants exactly
    this filter, and a second copy of it would drift -- this repo has a marker
    list that was corrected five times in five places to prove it.

    Two things are dropped and each one is a decision:

    - **Lines the child wrote about itself.** On Windows `import resource` fails
      and `_sandbox_child.py` says so on stderr of *every single run*. Surfacing
      stderr without this filter means every local run gains a line that is
      always there and never actionable, and by day two the reader is skipping
      the whole block -- which puts the real signal back where it started.
    - **Everything but the tail.** `sandbox_max_output_chars` already bounds
      stderr in the parent; `TRACEBACK_TAIL_LINES` bounds it again here in
      lines, because a successful-but-chatty program (a hundred pandas
      `SettingWithCopyWarning`s) would otherwise spend the next step's prompt
      budget on warnings. The tail is kept rather than the head for the same
      reason `_render_failure` keeps it: the last thing said is the useful one.
    """
    kept = [
        line
        for line in text.splitlines()
        if not line.lstrip().startswith(SANDBOX_NOTE_PREFIX)
    ]
    return _tail("\n".join(kept), TRACEBACK_TAIL_LINES)


def _validate_artifacts(
    artifacts: Sequence[SandboxArtifact],
) -> tuple[list[SandboxArtifact], list[tuple[str, str]]]:
    """Split what the run harvested into `(kept, [(filename, problem), ...])`.

    **Never raises, and every unexpected outcome resolves to "keep the file".**
    Three separate guards say the same thing, because they fail differently: the
    import can fail, `check` can raise, and `check` can return something that is
    not a usable string. A bug in validation is evidence about the validator and
    none at all about the artefact, so the permissive resolution is the honest
    one -- and the alternative is a validator defect that silently deletes every
    handout the chat path produces, for as long as it lives.

    That asymmetry is `loop.md` T3 with the numbers pointing harder than on the
    recipe path. A false positive here costs a step out of `max_tool_steps` in a
    turn a user is watching, where on the background job it costs one retry
    nobody sees. So this path is at least as permissive as that one, never
    stricter -- and `handout_deck_min_slides` is shared rather than re-tuned
    locally (PLAN.md 3.1).

    Whitespace-only and non-string verdicts are treated as "no problem" rather
    than as a rejection with no message. A refusal the model cannot act on
    wastes a step (`loop.md` section 4), and a rejection with nothing to read is
    exactly that.
    """
    if not settings.handout_validate_artifacts:
        # The off switch, and the only reason it exists: with it off this
        # function is the identity, so `_run` below is provably the code that
        # shipped before validation existed. `deck_check.py` case 64.
        return list(artifacts), []

    try:
        # LOCAL, AND NOT FOR TIDINESS. `app.handouts.validate` imports
        # `app.handouts.recipes` -> `app.rag.pipeline` -> `app.rag.agent_loop` ->
        # `app.tools.registry` -> THIS module. Measured 2026-08-17 by making the
        # import module-scope and importing from three entry points:
        #
        #     import app.tools.registry   ImportError: cannot import name
        #                                 'ToolArtifact' from partially
        #                                 initialized module
        #     import app.rag.agent_loop   ImportError: 'ContextLedger', same
        #     import app.main             imports fine
        #
        # The third row is why this is a comment and not a footnote. The server
        # would start, every route would work, and only a direct import of
        # `registry` or `agent_loop` -- which is what every layer-1 harness under
        # `scripts/` does -- would fail, on a line naming neither handouts nor
        # validation.
        from app.handouts import validate
    except Exception:  # noqa: BLE001
        log.exception("Artefact validation unavailable; keeping every file")
        return list(artifacts), []

    kept: list[SandboxArtifact] = []
    rejected: list[tuple[str, str]] = []
    for artifact in artifacts:
        problem: object = None
        try:
            # There is no `Recipe` on this path and there never will be -- the
            # model decided to make this file, so all that exists is the name it
            # chose and the MIME type `HARVEST_MIME` stamped on it. `kind_for_mime`
            # returns None for the formats no reader reads exactly, which is the
            # ordinary answer for `.svg`, `.json` and `.txt`.
            kind = validate.kind_for_mime(artifact.mime_type)
            if kind:
                problem = validate.check(kind, artifact)
        except Exception:  # noqa: BLE001
            log.exception("Artefact validation crashed for %s", artifact.filename)
            problem = None

        if isinstance(problem, str) and problem.strip():
            rejected.append((artifact.filename, problem.strip()))
        else:
            kept.append(artifact)
    return kept, rejected


def _render_success(
    result: SandboxResult,
    kept: list[SandboxArtifact] | None = None,
    rejected: Sequence[tuple[str, str]] = (),
) -> str:
    """What the model reads after a run that exited 0.

    `kept is None` with no rejections is the shape this function had before
    validation existed, and it reproduces that output exactly -- which is what
    makes `handout_validate_artifacts=false` assertable as byte-identical rather
    than merely similar (PLAN.md 3.6 R-a, `deck_check.py` case 64).
    """
    kept = list(result.artifacts) if kept is None else kept
    parts = [f"Exit {result.exit_code} in {result.duration_ms / 1000:.1f}s."]

    if result.stdout.strip():
        parts.append(f"stdout:\n{result.stdout.strip()}")

    # An exit code of 0 does not mean nothing went wrong, and until 2026-08-17
    # this function never looked at `result.stderr` at all. A program that
    # wrapped a slide in try/except, lost slide 4, printed "could not add slide
    # 4" and saved the other three reported here as a clean success -- with the
    # model then describing the deck it *meant* to write. The same blindness hid
    # matplotlib's MPLCONFIGDIR warning on every local run since day one.
    #
    # Labelled rather than dumped, because raw stderr under a success reads as a
    # failure to a model, and a turn abandoned over a warning is a worse outcome
    # than the warning.
    #
    # Note what this deliberately does NOT do: matplotlib's warning names
    # `MPLCONFIGDIR` and asks for it to be set, and passing it to the child is
    # the one move `sandbox.py:_minimal_env` warns against by name. The
    # environment is remove-only. Make the warning visible; do not act on it.
    warnings_text = program_stderr(result.stderr)
    if warnings_text:
        parts.append(
            "stderr (the process still exited 0, so this is a warning rather "
            f"than a failure -- read it before describing the result):\n{warnings_text}"
        )

    if kept:
        listing = "\n".join(
            f"  {a.filename}  ({a.mime_type}, {_human_size(a.byte_size)})"
            for a in kept
        )
        parts.append(f"Files produced:\n{listing}")
        # The closing instruction is aimed at one specific failure: a model that
        # ran code producing `chart.png` and then tells the user it also made a
        # summary table it never wrote.
        parts.append(
            "These are saved as handouts. Refer to them by name; do not claim "
            "any others exist."
        )
    elif not rejected:
        # Said explicitly rather than left as an absence. A model reading "Exit
        # 0" with no file list will otherwise describe the chart it meant to
        # make.
        #
        # Guarded on `rejected` because a run whose only file was thrown out DID
        # produce a file, and telling it otherwise would be a second untruth on
        # top of the one being corrected. The block below covers that case.
        parts.append(
            "No files were produced. Nothing has been saved as a handout, so do "
            "not tell the user a file exists."
        )

    if rejected:
        # Each `why` already names the defect AND the fix -- that is what
        # `validate.check` is written to produce, in the same register as the
        # static-check refusals which "name the offending module and list what
        # is allowed, so the retry is usually correct" (`loop.md:236-238`). It is
        # passed through verbatim; paraphrasing it would throw away the slide
        # number and the call to make.
        listing = "\n".join(f"  {name}: {why}" for name, why in rejected)
        # The verb agrees with the count, and this is in here because reading one
        # real message is the only thing that catches it: the first draft said
        # "These files ... They were NOT saved" over a single rejected deck,
        # which is ASCII, non-empty, imperative and contains every substring
        # `deck_check.py` cases 61 and 62 assert on. `validate.py` shipped the
        # identical defect a day earlier and its case 28 exists because of it.
        # A repo that has now made this mistake twice writes the branch.
        one = len(rejected) == 1
        parts.append(
            (
                "This file was opened and checked, and is not usable. It was "
                "NOT saved as a handout and the user cannot see it:\n"
                if one
                else "These files were opened and checked, and are not usable. "
                "They were NOT saved as handouts and the user cannot see "
                "them:\n"
            )
            + listing
        )
        # Worded to provoke a second call rather than to close the subject. A
        # rejection the model reads as final costs the user the artefact they
        # asked for AND the step that produced it.
        parts.append(
            f"Fix what {'the line' if one else 'each line'} above asks for and "
            "call run_python again. Do not describe a rejected file to the user "
            "and do not count it among the files you made."
        )

    return "\n\n".join(parts)


def _render_failure(result: SandboxResult) -> str:
    # "The code did not run" was true of a static refusal and never quite true of
    # a crash, and once the sandbox started returning files from a crashed run it
    # became self-contradictory: that sentence sat one paragraph above "Written
    # before it failed: deck.pptx" in the same message to the same model. "The
    # run failed" is accurate for all five error kinds, and the specific error
    # right after it says which.
    parts = [f"The run failed. {result.error or 'Unknown failure.'}"]

    if result.stdout.strip():
        # Printed output from before the exception is often exactly the clue --
        # it says how far the code got.
        parts.append(f"stdout before the failure:\n{result.stdout.strip()}")

    tail = _tail(result.stderr, TRACEBACK_TAIL_LINES)
    if tail:
        parts.append(f"Last {TRACEBACK_TAIL_LINES} lines of the traceback:\n{tail}")

    # `sandbox.run` harvests before it reads the exit code, so a crash that came
    # after a successful save now arrives here with the file attached. Saying so
    # is the whole value of that change on this path: it turns "your code raised
    # an error" into "your code raised an error AFTER writing the deck", which
    # are different problems with different fixes.
    #
    # The disclaimer is not optional. `_run` deliberately does not append these
    # to `ctx.artifacts` -- a failed run must not silently produce a handout --
    # so without a sentence saying they were not kept, this block becomes the
    # thing `_render_success`'s closing instruction exists to prevent: a model
    # telling the user about a file that does not exist.
    if result.artifacts:
        names = ", ".join(a.filename for a in result.artifacts)
        parts.append(
            f"Written before it failed: {names}. These were NOT saved as "
            "handouts, so do not tell the user they exist -- they are here to "
            "show how far the code got. Keep the part that worked."
        )

    parts.append("Fix the code and call run_python again, or answer without it.")
    return "\n\n".join(parts)


def build_python_tool(ctx: ToolContext) -> BaseTool:
    """The code interpreter, bound to one turn's artifact list."""

    from app.tools.registry import ToolArtifact, ToolOutcome

    async def _run(code: str, purpose: str) -> tuple[str, ToolOutcome]:
        # `sandbox.run` never raises -- every failure mode, including the sandbox
        # failing to start at all, comes back as `SandboxResult(ok=False)` with
        # an `error_kind`. That contract is what lets this function have no
        # try/except: there is nothing to catch that would not already be a bug
        # in the sandbox.
        result = await sandbox.run(code)

        def detail_for(artifacts: Sequence[SandboxArtifact]) -> dict:
            return {
                "purpose": purpose,
                "exit_code": result.exit_code,
                "stdout_chars": len(result.stdout),
                # The sandbox's own measurement, named apart from the
                # `duration_ms` column on `trace_events`. The two differ by the
                # wrapper's overhead and by `asyncio.to_thread` scheduling, and a
                # payload key that shadows a column name is how a trace panel
                # comes to show one number under two labels.
                "sandbox_ms": result.duration_ms,
                "artifacts": [
                    {
                        "filename": a.filename,
                        "mime_type": a.mime_type,
                        "byte_size": a.byte_size,
                    }
                    for a in artifacts
                ],
            }

        if not result.ok:
            # A failed run's files are never persisted (see `_render_failure`),
            # so there is nothing here to validate. `artifacts` still lists what
            # the program managed to write, because "how far did it get" is the
            # useful thing to put in the trace beside a traceback.
            detail = detail_for(result.artifacts)
            detail["error_kind"] = result.error_kind
            return _render_failure(result), ToolOutcome(
                ok=False,
                summary=f"{result.error_kind or 'error'}: {purpose}"[:200],
                detail=detail,
                error=result.error,
            )

        # OPENED BEFORE KEPT. Per artefact, so a run that wrote a good table and
        # an empty deck keeps the table -- see the module docstring for why that
        # asymmetry with the recipe path is correct rather than an oversight.
        kept, rejected = _validate_artifacts(result.artifacts)

        # `artifacts` in the payload means "became a handout", which is what a
        # reader of the trace panel is looking for. With the flag off `kept` IS
        # `result.artifacts`, so this is the identical dict it always was.
        detail = detail_for(kept)

        # Artifacts are appended, never replaced: a turn can run code twice, and
        # the second chart does not delete the first. `ask.run_turn` writes one
        # Handout row per entry after the commit boundary.
        names = [a.filename for a in kept]
        for artifact in kept:
            ctx.artifacts.append(
                ToolArtifact(
                    artifact=artifact,
                    title=purpose,
                    source_code=code,
                    step=ctx.step,
                )
            )

        if rejected:
            detail["rejected"] = [
                {"filename": name, "problem": why} for name, why in rejected
            ]
            # PLAN.md 3.4 mints "invalid" for exactly this: the sandbox's five
            # kinds are all about the PROCESS, and this one is about the
            # artefact. A plain string, and inside the 16 characters the column
            # would allow if it were ever promoted to one.
            detail["error_kind"] = "invalid"
            # `ok=False`, so `agent_loop._execute` stamps the ToolMessage
            # `status="error"` and the turn records TOOL_ERROR -- an EXISTING
            # trace type, which is why this feature needs no `EVENT_TYPES` entry
            # and no `TracePanel` map entry (PLAN.md 3.5). It is False even when
            # some files survived: something the model asked for did not happen,
            # and a step that reports success is a step it will not revisit.
            return _render_success(result, kept, rejected), ToolOutcome(
                ok=False,
                summary=(
                    f"{len(kept)} saved, {len(rejected)} rejected: "
                    + ", ".join(name for name, _ in rejected)
                )[:200],
                detail=detail,
                error="; ".join(why for _, why in rejected),
            )

        return _render_success(result, kept, rejected), ToolOutcome(
            ok=True,
            summary=(
                f"{len(kept)} file(s): {', '.join(names)}"
                if names
                else "ran, no files produced"
            )[:200],
            detail=detail,
        )

    return StructuredTool.from_function(
        coroutine=_run,
        name=RUN_PYTHON,
        description=TOOL_DESCRIPTION,
        args_schema=RunPythonArgs,
        response_format="content_and_artifact",
    )
