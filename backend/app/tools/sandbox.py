"""The parent half of the sandbox: static check, spawn, harvest, cap.

Read `new features/02-code-interpreter.md` section 5 before changing anything
here. It states exactly what this does and does not protect against, and a
change that quietly weakens one of those layers will not fail any test.

The short version of the design:

    static_check      reject in milliseconds, with a message the model can act on
    _minimal_env      the child inherits NO secrets -- the strongest control here
    subprocess.run    real process isolation, a kill that works, a wall clock
    _harvest          only known-safe file types, capped, all-or-nothing

`run()` never raises. Every failure -- refusal, syntax error, timeout, crash,
oversized output -- comes back as a `SandboxResult` with `ok=False` and an
`error_kind`, because the caller is a tool wrapper feeding a language model and
an exception there would abort a turn that could have self-corrected.
"""

from __future__ import annotations

import ast
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.config import settings

# The child is run as a script, so this is a path, never an import.
CHILD_PATH = Path(__file__).with_name("_sandbox_child.py")

# The user's source lands here, inside the work directory. Named with a leading
# underscore and skipped explicitly during harvest so it can never become a
# handout, whatever suffix rules change later.
CODE_FILENAME = "_handout_code.py"

# Top-level module names the code may import.
#
# `pathlib` is allowed because writing files is the point; `os` is not, because
# `os.system` and `os.environ` are. `io` is allowed for in-memory buffers.
# Nothing that opens a socket, starts a process, loads a shared library or
# reflects on the interpreter is on the list.
#
# DUPLICATED in `_sandbox_child.py`, deliberately -- the child cannot import
# from `app.*`. Both copies must change together.
ALLOWED_IMPORTS = frozenset(
    {
        # plotting
        "matplotlib",
        "mpl_toolkits",
        # decks and documents
        "pptx",
        # data
        "pandas",
        "numpy",
        # stdlib, the safe subset
        "math",
        "statistics",
        "random",
        "itertools",
        "functools",
        "collections",
        "datetime",
        "decimal",
        "fractions",
        "json",
        "csv",
        "io",
        "re",
        "textwrap",
        "string",
        "typing",
        "dataclasses",
        "enum",
        "pathlib",
        "base64",
        "colorsys",
    }
)

# Names that may not be called or attribute-accessed anywhere in the source.
# `getattr`/`setattr` are here because they turn the dunder-attribute rule below
# into a string comparison the checker cannot see.
DENIED_NAMES = frozenset(
    {
        "eval",
        "exec",
        "compile",
        "__import__",
        "globals",
        "locals",
        "vars",
        "getattr",
        "setattr",
        "delattr",
        "breakpoint",
        "input",
        "memoryview",
    }
)

# Module names that are blocked as ATTRIBUTES as well as imports, because an
# allowed library will happily hand them over.
#
# This was found by probing rather than by reasoning, and the probe is worth
# recording: `import matplotlib; matplotlib.os.environ` passes the import check
# cleanly. `matplotlib` is on the allowlist, `os` is never imported by the user's
# source, and `os` is not a dunder -- so all three of the rules above see nothing
# wrong. The binding exists because matplotlib imported `os` for its own use and
# module attributes are public. `numpy.ctypeslib.ctypes` is the same shape and is
# the worse one: `ctypes` is a route to arbitrary native code, not merely to the
# filesystem.
#
# Neither probe actually leaked anything, and WHY is the point. `os.environ` came
# back empty because `_minimal_env()` had already stripped every credential from
# the child. The cheap control failed and the strong one held, which is the
# layering working as designed -- but a layer that is known to be porous should
# be patched where patching is cheap, and one frozenset is cheap.
#
# These are attribute names, so `x.os` is refused whatever `x` is. False
# positives are conceivable (a DataFrame column named `sys`) and acceptable: the
# refusal names the attribute and the line, and the model rewrites around it.
DENIED_MODULE_ATTRS = frozenset(
    {
        "os",
        "sys",
        "ctypes",
        "cffi",
        "subprocess",
        "socket",
        "shutil",
        "importlib",
        "builtins",
        "pickle",
        "marshal",
        "multiprocessing",
        "threading",
        "signal",
        "resource",
        "platform",
        "sysconfig",
        "webbrowser",
        "urllib",
        "http",
        "ftplib",
        "smtplib",
        "telnetlib",
        "requests",
        "httpx",
        "tempfile",
        "glob",
        "fileinput",
        "runpy",
        "pty",
        "posix",
        "nt",
        "gc",
        "inspect",
        "traceback",
        "atexit",
        "site",
    }
)

# Checked together on attribute access; only DENIED_NAMES applies to a bare name
# load, because `os` as a bare name cannot resolve -- the import is refused first
# and the meta_path hook refuses it again at runtime.
_DENIED_ATTRS = DENIED_NAMES | DENIED_MODULE_ATTRS

# Suffix -> MIME. Anything not listed is not harvested, so a stray `.pyc` or a
# `__pycache__` directory never becomes a handout.
HARVEST_MIME = {
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".csv": "text/csv",
    ".md": "text/markdown",
    ".json": "application/json",
    ".txt": "text/plain",
}

_ALLOWED_LIST = ", ".join(sorted(ALLOWED_IMPORTS))
_DENIED_LIST = ", ".join(sorted(DENIED_NAMES))


@dataclass(frozen=True)
class SandboxArtifact:
    """One file the code wrote, read into memory before the workdir is removed."""

    filename: str
    mime_type: str
    content: bytes

    @property
    def byte_size(self) -> int:
        return len(self.content)


@dataclass(frozen=True)
class SandboxResult:
    ok: bool
    exit_code: int
    stdout: str
    stderr: str
    artifacts: list[SandboxArtifact] = field(default_factory=list)
    duration_ms: int = 0
    error: str | None = None
    # "import" | "syntax" | "timeout" | "runtime" | "output"
    error_kind: str | None = None


# --------------------------------------------------------------------------
# Environment
# --------------------------------------------------------------------------


def _minimal_env() -> dict[str, str]:
    """The child's entire environment.

    **This is the strongest control in the whole design, and the cheapest.**
    Every other layer -- the AST check, the import hook, the socket kill, the
    resource limits -- is a fence that a sufficiently clever piece of code might
    get over. This one is not a fence: the secrets are simply not there.

    Code that fully escapes the import allowlist, gets `os` back and reads
    `os.environ` finds no `OPENROUTER_API_KEY`, no `PINECONE_API_KEY`, no
    `GEMINI_API_KEY`, no `DATABASE_URL`, no `RENDER_API_KEY`, no
    `SESSION_SECRET`, no Google OAuth client secret. There is nothing to
    exfiltrate and -- combined with the socket kill -- nowhere to send it.

    So the rule for this function is: **only ever remove things from it.**
    Adding a passthrough for convenience ("the child needs XDG_CACHE_HOME for
    the font cache") is how an allowlist becomes a denylist, and the next
    variable added by inertia is the one with a key in it. Nothing here is read
    from `os.environ` by pattern; every name is spelled out.

    - `PATH` -- required to load DLLs on Windows and shared objects on Linux.
    - `LANG` / `LC_ALL` -- decode behaviour only; absent on most containers.
    - `HOME` (POSIX) / `SYSTEMROOT`, `TEMP`, `TMP` (Windows) -- without these
      the interpreter does not start reliably.
    - `MPLBACKEND=Agg` -- matplotlib must never look for a display.
    """
    env: dict[str, str] = {"MPLBACKEND": "Agg"}

    def passthrough(name: str) -> None:
        value = os.environ.get(name)
        if value:
            env[name] = value

    passthrough("PATH")
    passthrough("LANG")
    passthrough("LC_ALL")

    if os.name == "nt":
        # Windows will not start a process without SYSTEMROOT, and tempfile
        # inside the child resolves through TEMP/TMP.
        passthrough("SYSTEMROOT")
        passthrough("TEMP")
        passthrough("TMP")
    else:
        passthrough("HOME")

    return env


# --------------------------------------------------------------------------
# Static check
# --------------------------------------------------------------------------


def _is_dunder(name: str) -> bool:
    return len(name) > 4 and name.startswith("__") and name.endswith("__")


def _static_refusal(code: str) -> tuple[str, str] | None:
    """Return `(message, error_kind)` or None.

    Split out from `static_check` so that `run` can distinguish a syntax error
    from a refusal without re-parsing. The public function returns only the
    message, which is the half a caller ever needs.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        line = exc.lineno if exc.lineno is not None else 1
        detail = exc.msg or "invalid syntax"
        return (
            f"Python syntax error on line {line}: {detail}. "
            f"Nothing was run. Fix the code and try again.",
            "syntax",
        )
    except ValueError as exc:
        # Null bytes and a few other malformed-source cases raise ValueError
        # rather than SyntaxError.
        return (f"The code could not be parsed: {exc}. Nothing was run.", "syntax")
    except (MemoryError, RecursionError) as exc:
        # OBSERVED, not defensive. `scripts/agentic_check.py` S8 "recipe table"
        # failed with `MemoryError: Parser stack overflowed - Python source too
        # complex to parse`: the model emitted a deeply nested literal and
        # CPython's parser gave up on it.
        #
        # This is the failure `new features/loop.md` section 4 exists to
        # prevent, in the one function whose whole job is to prevent it. A
        # static check that RAISES instead of refusing takes the handout job
        # down with it -- the model never sees the problem, never gets the
        # chance to emit simpler code, and the row lands `failed` with a
        # traceback nobody can act on. Refusing hands it straight back as a
        # message it can fix, which is the single most valuable behaviour the
        # code interpreter has.
        #
        # `MemoryError` is caught NARROWLY, around `ast.parse` alone, and that
        # placement is the whole safety argument: CPython raises it here for a
        # parser stack overflow rather than for genuine exhaustion, so this
        # swallows a parse failure and not an out-of-memory condition anywhere
        # else in the process.
        return (
            "The code was too deeply nested for Python to parse "
            f"({type(exc).__name__}). Nothing was run. Rewrite it with less "
            "nesting -- build large tables from a loop or a literal list of "
            "rows rather than one deeply nested expression.",
            "syntax",
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".", 1)[0]
                if top not in ALLOWED_IMPORTS:
                    return (_import_refusal(top), "import")

        elif isinstance(node, ast.ImportFrom):
            if node.level:
                return (
                    "Relative imports are not permitted in the sandbox: the code "
                    "runs as a single standalone file with no package around it. "
                    f"Import one of the allowed modules instead. Allowed: {_ALLOWED_LIST}.",
                    "import",
                )
            top = (node.module or "").split(".", 1)[0]
            if top not in ALLOWED_IMPORTS:
                return (_import_refusal(top), "import")

        elif isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Load) and node.id in DENIED_NAMES:
                return (_name_refusal(node.id, node.lineno), "import")

        elif isinstance(node, ast.Attribute):
            if _is_dunder(node.attr):
                return (
                    f"Attribute access to '{node.attr}' is not permitted in the "
                    f"sandbox (line {node.lineno}). Names that both start and end "
                    "with '__' are blocked, because walking them "
                    "(`.__class__.__base__.__subclasses__()`) is the standard way "
                    "out of a restricted interpreter. Write ordinary code: import "
                    "an allowed module and call its public functions. "
                    f"Allowed modules: {_ALLOWED_LIST}.",
                    "import",
                )
            if node.attr in _DENIED_ATTRS:
                return (_attr_refusal(node.attr, node.lineno), "import")

    return None


def _import_refusal(module: str) -> str:
    shown = module or "<unknown>"
    return (
        f"Import of '{shown}' is not permitted in the sandbox. "
        f"Allowed modules: {_ALLOWED_LIST}. "
        "There is no network and no filesystem outside the current directory, so "
        "modules that reach either are not available. Write files to the current "
        "directory with `open()` or `pathlib.Path`."
    )


def _name_refusal(name: str, lineno: int) -> str:
    return (
        f"Use of '{name}' is not permitted in the sandbox (line {lineno}). "
        f"Blocked names: {_DENIED_LIST}. "
        "These are the routes to dynamic execution and interpreter reflection. "
        "Import an allowed module by name and call it directly instead."
    )


def _attr_refusal(name: str, lineno: int) -> str:
    """Refusal for an attribute, which needs different advice from a bare name.

    A model that writes `matplotlib.os.path.join(...)` is usually not probing the
    sandbox -- it is reaching for a stdlib helper through whatever module happens
    to be in scope. Saying so, and pointing at the allowed way to do the same
    thing, gets a corrected program on the next loop step instead of a second
    refusal.
    """
    return (
        f"Attribute access to '{name}' is not permitted in the sandbox "
        f"(line {lineno}). An allowed library may expose a blocked module as an "
        "attribute of itself (`matplotlib.os`, `numpy.ctypeslib.ctypes`); "
        "reaching a module that way is blocked exactly as importing it is. "
        "For paths and files, use `pathlib` or `open()` in the current "
        f"directory. Allowed modules: {_ALLOWED_LIST}."
    )


def static_check(code: str) -> str | None:
    """Return a human-readable refusal, or None if the code may run.

    The refusal is written for a language model, not a log: it names the
    offending module or name and lists what *is* allowed, because the next
    attempt then usually succeeds. A vague refusal costs a whole tool step.

    This is also a usability feature as much as a security one -- it fails in
    milliseconds rather than after a 1.5 s matplotlib import. It is the first
    line only; the import hook in the child is the second, because the static
    check reads source text and a lazy import inside a library function does not
    appear in it.
    """
    refusal = _static_refusal(code)
    return None if refusal is None else refusal[0]


# --------------------------------------------------------------------------
# Output caps and harvest
# --------------------------------------------------------------------------


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n... [truncated, {len(text) - limit} more characters]"


def _last_line(text: str) -> str:
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _harvest(workdir: str) -> tuple[list[SandboxArtifact], str | None]:
    """Collect the files the code wrote. Returns `(artifacts, error)`.

    All-or-nothing on the size caps: one oversized file means zero artifacts
    come back. A partial set would be reported to the model as a success, and a
    deck missing half its slides is worse than a deck that failed.
    """
    root = Path(workdir)
    candidates: list[tuple[str, Path, int]] = []

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.name == CODE_FILENAME:
            continue
        mime = HARVEST_MIME.get(path.suffix.lower())
        if mime is None:
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        name = path.relative_to(root).as_posix()
        candidates.append((name, path, size))

    # Deterministic order, so two identical runs produce identical handouts.
    candidates.sort(key=lambda item: item[0])

    total = 0
    for name, _path, size in candidates:
        if size > settings.sandbox_max_artifact_bytes:
            return [], (
                f"'{name}' is {size:,} bytes, over the {settings.sandbox_max_artifact_bytes:,} "
                "byte per-file limit. No files were kept. Write something smaller "
                "-- fewer rows, a lower figure DPI, or split the output."
            )
        total += size

    if total > settings.sandbox_max_total_bytes:
        return [], (
            f"The run produced {total:,} bytes across {len(candidates)} file(s), over the "
            f"{settings.sandbox_max_total_bytes:,} byte total limit. No files were kept."
        )

    artifacts: list[SandboxArtifact] = []
    for name, path, _size in candidates:
        try:
            content = path.read_bytes()
        except OSError as exc:
            return [], f"'{name}' could not be read back: {exc}. No files were kept."
        artifacts.append(
            SandboxArtifact(
                filename=name,
                mime_type=HARVEST_MIME[path.suffix.lower()],
                content=content,
            )
        )
    return artifacts, None


# --------------------------------------------------------------------------
# Run
# --------------------------------------------------------------------------


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _run_blocking(code: str, timeout_s: float) -> SandboxResult:
    started = time.perf_counter()

    refusal = _static_refusal(code)
    if refusal is not None:
        message, kind = refusal
        # Nothing is spawned and no work directory is created: a refusal costs
        # a few milliseconds, which is the whole point of checking here.
        return SandboxResult(
            ok=False,
            exit_code=-1,
            stdout="",
            stderr="",
            artifacts=[],
            duration_ms=_elapsed_ms(started),
            error=message,
            error_kind=kind,
        )

    limit = settings.sandbox_max_output_chars
    workdir = tempfile.mkdtemp(prefix="gw-sandbox-")
    try:
        code_path = Path(workdir) / CODE_FILENAME
        # Written to a file rather than passed with `-c`: a 4 KB program exceeds
        # Windows' command-line limits, and `-c` would put the source in the
        # process table where every user on the box can read it.
        code_path.write_text(code, encoding="utf-8")

        argv = [
            sys.executable,
            # -I (isolated) implies -E (ignore PYTHON* env vars) and -s (no user
            # site directory). The venv's own site-packages still loads, which is
            # why matplotlib is importable.
            "-I",
            str(CHILD_PATH),
            str(workdir),
            str(code_path),
            str(settings.sandbox_memory_mb),
            str(timeout_s),
        ]

        try:
            proc = subprocess.run(  # noqa: S603 - argv is built here, never from the model
                argv,
                cwd=workdir,
                env=_minimal_env(),
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            # subprocess has already killed the child. On Windows that is
            # TerminateProcess, which does not reap grandchildren -- but
            # `subprocess` is not on the import allowlist, so there are none.
            partial_out = exc.stdout if isinstance(exc.stdout, str) else ""
            partial_err = exc.stderr if isinstance(exc.stderr, str) else ""
            return SandboxResult(
                ok=False,
                exit_code=-1,
                stdout=_truncate(partial_out, limit),
                stderr=_truncate(partial_err, limit),
                artifacts=[],
                duration_ms=_elapsed_ms(started),
                error=(
                    f"The code ran for longer than {timeout_s:g}s and was stopped. "
                    "No files were kept. Do less work per run: fewer iterations, "
                    "a smaller dataset, no waiting."
                ),
                error_kind="timeout",
            )
        except OSError as exc:
            return SandboxResult(
                ok=False,
                exit_code=-1,
                stdout="",
                stderr="",
                artifacts=[],
                duration_ms=_elapsed_ms(started),
                error=f"The sandbox process could not be started: {exc}",
                error_kind="runtime",
            )

        stdout = _truncate(proc.stdout or "", limit)
        stderr = _truncate(proc.stderr or "", limit)

        if proc.returncode != 0:
            detail = _last_line(stderr) or f"the process exited with code {proc.returncode}"
            return SandboxResult(
                ok=False,
                exit_code=proc.returncode,
                stdout=stdout,
                stderr=stderr,
                artifacts=[],
                duration_ms=_elapsed_ms(started),
                error=f"The code raised an error: {detail}",
                error_kind="runtime",
            )

        artifacts, harvest_error = _harvest(workdir)
        if harvest_error is not None:
            return SandboxResult(
                ok=False,
                exit_code=proc.returncode,
                stdout=stdout,
                stderr=stderr,
                artifacts=[],
                duration_ms=_elapsed_ms(started),
                error=harvest_error,
                error_kind="output",
            )

        return SandboxResult(
            ok=True,
            exit_code=proc.returncode,
            stdout=stdout,
            stderr=stderr,
            artifacts=artifacts,
            duration_ms=_elapsed_ms(started),
        )
    except Exception as exc:  # pragma: no cover - defensive; run() must not raise
        return SandboxResult(
            ok=False,
            exit_code=-1,
            stdout="",
            stderr="",
            artifacts=[],
            duration_ms=_elapsed_ms(started),
            error=f"The sandbox itself failed: {type(exc).__name__}: {exc}",
            error_kind="runtime",
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def run(code: str, *, timeout_s: float | None = None) -> SandboxResult:
    """Run `code` in a hardened subprocess. Never raises.

    The whole call is blocking -- `subprocess.run` with a timeout -- so it goes
    on a worker thread. Everything that can go wrong comes back as
    `SandboxResult(ok=False, ...)` with an `error_kind`, because the caller is
    feeding a language model that can often fix the problem on the next step,
    and an exception would end the turn instead.
    """
    effective = settings.sandbox_timeout_s if timeout_s is None else timeout_s
    try:
        return await asyncio.to_thread(_run_blocking, code, effective)
    except Exception as exc:  # pragma: no cover - to_thread itself failing
        # asyncio.CancelledError is a BaseException and is deliberately allowed
        # through: a cancelled turn must stay cancelled.
        return SandboxResult(
            ok=False,
            exit_code=-1,
            stdout="",
            stderr="",
            artifacts=[],
            duration_ms=0,
            error=f"The sandbox could not be scheduled: {type(exc).__name__}: {exc}",
            error_kind="runtime",
        )
