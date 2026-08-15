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
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from app.tools import sandbox
from app.tools.sandbox import SandboxResult

if TYPE_CHECKING:  # pragma: no cover - import cycle, types only
    from app.tools.registry import ToolContext

RUN_PYTHON = "run_python"

# How much of stderr goes back to the model on a failure.
#
# A traceback's useful part is at the bottom -- the exception type, its message,
# and the line that raised. The frames above it are matplotlib's internals more
# often than they are the model's code. Thirty lines keeps the raising frame and
# a little context while bounding what a deeply nested library error can spend of
# the next step's prompt.
TRACEBACK_TAIL_LINES = 30


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


TOOL_DESCRIPTION = (
    "Write and run Python in a sandbox, and keep the files it writes. Use it "
    "when the user wants a chart, a slide deck, a table or a data file. "
    "matplotlib, python-pptx, pandas and numpy are available; there is no "
    "network and no filesystem outside the working directory. Put the numbers "
    "in the code as literals."
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


def _render_success(result: SandboxResult) -> str:
    parts = [f"Exit {result.exit_code} in {result.duration_ms / 1000:.1f}s."]

    if result.stdout.strip():
        parts.append(f"stdout:\n{result.stdout.strip()}")

    if result.artifacts:
        listing = "\n".join(
            f"  {a.filename}  ({a.mime_type}, {_human_size(a.byte_size)})"
            for a in result.artifacts
        )
        parts.append(f"Files produced:\n{listing}")
        # The closing instruction is aimed at one specific failure: a model that
        # ran code producing `chart.png` and then tells the user it also made a
        # summary table it never wrote.
        parts.append(
            "These are saved as handouts. Refer to them by name; do not claim "
            "any others exist."
        )
    else:
        # Said explicitly rather than left as an absence. A model reading "Exit
        # 0" with no file list will otherwise describe the chart it meant to
        # make.
        parts.append(
            "No files were produced. Nothing has been saved as a handout, so do "
            "not tell the user a file exists."
        )

    return "\n\n".join(parts)


def _render_failure(result: SandboxResult) -> str:
    parts = [f"The code did not run. {result.error or 'Unknown failure.'}"]

    if result.stdout.strip():
        # Printed output from before the exception is often exactly the clue --
        # it says how far the code got.
        parts.append(f"stdout before the failure:\n{result.stdout.strip()}")

    tail = _tail(result.stderr, TRACEBACK_TAIL_LINES)
    if tail:
        parts.append(f"Last {TRACEBACK_TAIL_LINES} lines of the traceback:\n{tail}")

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

        detail = {
            "purpose": purpose,
            "exit_code": result.exit_code,
            "stdout_chars": len(result.stdout),
            # The sandbox's own measurement, named apart from the `duration_ms`
            # column on `trace_events`. The two differ by the wrapper's overhead
            # and by `asyncio.to_thread` scheduling, and a payload key that
            # shadows a column name is how a trace panel comes to show one number
            # under two labels.
            "sandbox_ms": result.duration_ms,
            "artifacts": [
                {
                    "filename": a.filename,
                    "mime_type": a.mime_type,
                    "byte_size": a.byte_size,
                }
                for a in result.artifacts
            ],
        }

        if not result.ok:
            detail["error_kind"] = result.error_kind
            return _render_failure(result), ToolOutcome(
                ok=False,
                summary=f"{result.error_kind or 'error'}: {purpose}"[:200],
                detail=detail,
                error=result.error,
            )

        # Artifacts are appended, never replaced: a turn can run code twice, and
        # the second chart does not delete the first. `ask.run_turn` writes one
        # Handout row per entry after the commit boundary.
        names = [a.filename for a in result.artifacts]
        for artifact in result.artifacts:
            ctx.artifacts.append(
                ToolArtifact(
                    artifact=artifact,
                    title=purpose,
                    source_code=code,
                    step=ctx.step,
                )
            )

        return _render_success(result), ToolOutcome(
            ok=True,
            summary=(
                f"{len(result.artifacts)} file(s): {', '.join(names)}"
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
