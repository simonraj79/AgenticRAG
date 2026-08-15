"""The child half of the sandbox. Executed as a script, never imported.

    python -I _sandbox_child.py <workdir> <code_path> <memory_mb> <timeout_s>

**This module must not import anything from `app.*`.** It runs under `-I` in an
interpreter whose working directory is a throwaway temp folder; the project is
not on its path and never should be. That is why `ALLOWED_IMPORTS` below is a
verbatim copy of the one in `sandbox.py` rather than an import -- the
duplication is deliberate, and **both copies must change together.** The same
goes for `MAX_FILE_BYTES`, which mirrors `settings.sandbox_max_total_bytes`.

The order of the five steps in `main()` is load-bearing; each depends on the
previous one having finished. See `new features/02-code-interpreter.md` §5.

    1. resource limits          (POSIX only)
    2. pre-import the modules the code actually names
    3. install the deny-everything-else import hook
    4. neuter sockets
    5. exec the user code with a trimmed __builtins__
"""

from __future__ import annotations

import ast
import builtins
import importlib
import linecache
import math
import os
import sys
import traceback

# Imported HERE, at module scope, because step 3 installs an import hook that
# would block it -- and step 4 must run *after* the pre-imports (matplotlib
# touches socket at import time on some platforms), so it cannot be imported
# then either.
import socket

# --- duplicated from app/tools/sandbox.py; keep in sync -------------------
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

# Mirrors settings.sandbox_max_total_bytes (15 MB). The child is not given this
# on argv, and it cannot read the app's config, so it carries its own copy.
MAX_FILE_BYTES = 15 * 1024 * 1024

# Interpreter-internal machinery the import hook must never block. Codec modules
# are imported *lazily*, by name, the first time text is encoded or decoded in
# an encoding not already loaded -- so blocking them turns an ordinary
# `open(path, "w")` into a LookupError that reads like a sandbox bug. They
# expose no capability: `encodings.cp1252` is a lookup table.
_BOOTSTRAP_ALLOWED = frozenset({"encodings", "codecs", "_codecs"})

# Removed from the user's `__builtins__`. `open` deliberately STAYS -- writing a
# file is the entire purpose of this tool, and writes are confined by the empty
# temp cwd, not by the builtin.
_DENIED_BUILTINS = (
    "eval",
    "exec",
    "compile",
    "__import__",
    "breakpoint",
    "input",
    "memoryview",
    "globals",
    "locals",
    "vars",
    "help",
)


def _note(message: str) -> None:
    """One line to stderr. The parent surfaces stderr to the model verbatim."""
    print(f"[sandbox] {message}", file=sys.stderr)


# --------------------------------------------------------------------------
# Step 1 -- resource limits, POSIX only
# --------------------------------------------------------------------------


def apply_resource_limits(memory_mb: int, timeout_s: float) -> None:
    """Set rlimits where the platform has them.

    Render is Linux, so production gets these. Windows development does not, and
    this says so rather than pretending otherwise -- development is measurably
    less protected than production here, which is the reverse of the usual
    arrangement and worth knowing.

    Each limit is guarded separately: some are unsettable inside containers, and
    one refusal must not cost the others.
    """
    try:
        import resource
    except ImportError:
        _note(
            "resource limits are unavailable on this platform (Windows); "
            "running with process isolation and the import hook only"
        )
        return

    def set_limit(name: str, soft: int, hard: int | None = None) -> None:
        which = getattr(resource, name, None)
        if which is None:
            return
        try:
            current_soft, current_hard = resource.getrlimit(which)
        except (ValueError, OSError):
            return
        target_hard = current_hard if hard is None else hard
        if current_hard != resource.RLIM_INFINITY:
            # Never try to raise the hard limit -- that is refused, and the
            # refusal would take the soft limit down with it.
            target_hard = min(target_hard, current_hard)
            soft = min(soft, current_hard)
        try:
            resource.setrlimit(which, (soft, target_hard))
        except (ValueError, OSError) as exc:
            _note(f"{name} could not be set: {exc}")

    # A busy loop burning a core past the wall clock. The parent's timeout is
    # the real stop; this catches the case where the parent is itself wedged.
    set_limit("RLIMIT_CPU", int(math.ceil(timeout_s)) + 1)

    # `[0] * 10**10`.
    set_limit("RLIMIT_AS", memory_mb * 1024 * 1024)

    # Filling the disk. The harvest caps in the parent are the policy; this is
    # the floor under them, and it stops the write rather than the handout.
    set_limit("RLIMIT_FSIZE", MAX_FILE_BYTES)

    # Fork bombs. On Linux the soft limit is compared, at clone() time, against
    # the number of tasks already owned by this real UID -- and a *thread* is a
    # task, so numpy's OpenBLAS pool counts against it. Setting it to zero would
    # therefore break the pre-imports in step 2 rather than the fork bomb. The
    # goal is a low finite ceiling, not zero: current usage plus enough headroom
    # for library thread pools.
    set_limit("RLIMIT_NPROC", _current_process_count() + 64)

    # Core dumps.
    set_limit("RLIMIT_CORE", 0, 0)


def _current_process_count() -> int:
    """Best-effort count of this UID's processes. 64 if the kernel won't say."""
    try:
        uid = os.getuid()
    except AttributeError:
        return 64
    try:
        entries = os.listdir("/proc")
    except OSError:
        return 64
    count = 0
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            if os.stat("/proc/" + entry).st_uid == uid:
                count += 1
        except OSError:
            continue
    return count if count else 64


# --------------------------------------------------------------------------
# Step 2 -- pre-import, driven by the code's own AST
# --------------------------------------------------------------------------


def preimport_from_source(source: str) -> None:
    """Import exactly the modules the code names, before the hook goes up.

    The deny hook in step 3 blocks anything not already in `sys.modules`, and
    matplotlib's transitive imports run into the hundreds -- `dateutil`,
    `cycler`, `kiwisolver`, `pyparsing`, `packaging`, `PIL` and the rest.
    Allowlisting those by hand is a list that rots with every upgrade, and each
    entry is a genuine grant. So instead the child re-parses the code, takes the
    module names it actually mentions, intersects them with `ALLOWED_IMPORTS`,
    and imports those *first* -- at which point every transitive dependency is
    in `sys.modules` and the hook lets it through by identity rather than by
    name.

    The dotted name matters: `import matplotlib.pyplot` must pre-import
    `matplotlib.pyplot`, not just `matplotlib`, or the transitives never load
    and the hook blocks them at runtime.

    It is also why a chart job does not pay python-pptx's import cost and a deck
    job does not pay matplotlib's -- only what the code names is loaded.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # The parent's static check already rejected this; nothing to pre-import.
        return

    required: list[str] = []
    speculative: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                required.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level or not node.module:
                continue
            required.append(node.module)
            for alias in node.names:
                # `from pptx import util` -- `util` may be a submodule or may be
                # a name inside the module. Try it; failure is not an error.
                if alias.name != "*":
                    speculative.append(node.module + "." + alias.name)

    seen: set[str] = set()

    def attempt(name: str, loud: bool) -> None:
        if name in seen:
            return
        seen.add(name)
        if name.split(".", 1)[0] not in ALLOWED_IMPORTS:
            return
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 - any import failure is survivable here
            if loud:
                # Let the user code's own import statement raise the real error
                # a moment later; this is a breadcrumb, not the failure.
                _note(f"pre-import of {name!r} failed: {type(exc).__name__}: {exc}")

    for name in required:
        attempt(name, loud=True)
    for name in speculative:
        attempt(name, loud=False)


# --------------------------------------------------------------------------
# Step 3 -- the deny-everything-else import hook
# --------------------------------------------------------------------------


class _DenyUnlistedImports:
    """A `sys.meta_path` finder at position 0 that refuses unknown modules.

    This is the second line; the static check in the parent is the first. Both
    exist because the static check reads source text, and a lazy `import
    requests` inside a library function does not appear in it.

    A module already in `sys.modules` is allowed through: Python resolves those
    before consulting `meta_path` anyway, so refusing them here would buy
    nothing and would break every submodule of a package pre-imported in step 2.
    """

    def find_spec(self, fullname, path=None, target=None):  # noqa: ANN001, ANN201
        top = fullname.split(".", 1)[0]
        if top in ALLOWED_IMPORTS or top in _BOOTSTRAP_ALLOWED:
            return None  # defer to the real finders
        if fullname in sys.modules or top in sys.modules:
            return None
        raise ImportError(
            f"{fullname!r} is not available in this sandbox. "
            "Allowed modules: " + ", ".join(sorted(ALLOWED_IMPORTS))
        )


def install_import_hook() -> None:
    sys.meta_path.insert(0, _DenyUnlistedImports())


# --------------------------------------------------------------------------
# Step 4 -- neuter sockets
# --------------------------------------------------------------------------


def disable_network() -> None:
    """Make every socket constructor raise.

    Runs *after* the pre-imports: matplotlib touches `socket` at import time on
    some platforms and would fail to load otherwise.

    `socket` is in `sys.modules` by the time user code runs, so the import hook
    cannot stop `import socket` -- the parent's static check does, and this makes
    the module useless even if something reaches it by another route.
    """

    def blocked(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise PermissionError("network access is not available in this sandbox")

    socket.socket.__init__ = blocked  # type: ignore[method-assign]
    socket.create_connection = blocked  # type: ignore[assignment]
    socket.socketpair = blocked  # type: ignore[assignment]
    socket.getaddrinfo = blocked  # type: ignore[assignment]


# --------------------------------------------------------------------------
# Step 5 -- exec
# --------------------------------------------------------------------------


def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):  # noqa: A002, ANN001, ANN201
    """The `__import__` the user's code gets.

    The choice, stated because the alternatives look equivalent and are not:
    the raw builtin `__import__` is REMOVED, and this delegate takes its place.

    Removing `__import__` outright and stopping there does not work -- an
    `import` statement inside the executed source looks it up in
    `__builtins__` at runtime, so every `import matplotlib.pyplot` in a chart
    job would fail with a message about `__import__` rather than about
    matplotlib. Pre-binding the pre-imported modules into the globals dict
    instead would work for top-level imports and quietly break `import x.y as z`
    and every import inside a function body.

    So: keep the *capability*, drop the *object*. This wrapper re-checks the
    allowlist and then delegates, which makes an unlisted import fail with the
    same wording the parent's static check uses instead of a stack of importlib
    frames. It is a usability layer, not the control -- the control is the
    `sys.meta_path` hook from step 3, which also covers imports made by library
    code that never touches these globals.
    """
    # Strictly the allowlist -- note there is deliberately no "or it is already
    # in sys.modules" escape here, unlike the meta_path hook. The hook has to
    # allow loaded modules because Python resolves them before ever calling it;
    # this delegate does not, so `import os` fails in user code even though `os`
    # has been in sys.modules since interpreter start.
    top = name.split(".", 1)[0] if name else name
    if top and top not in ALLOWED_IMPORTS and top not in _BOOTSTRAP_ALLOWED:
        raise ImportError(
            f"{name!r} is not available in this sandbox. "
            "Allowed modules: " + ", ".join(sorted(ALLOWED_IMPORTS))
        )
    return builtins.__import__(name, globals, locals, fromlist, level)


def build_safe_builtins() -> dict:
    safe = dict(builtins.__dict__)
    for name in _DENIED_BUILTINS:
        safe.pop(name, None)
    safe["__import__"] = _guarded_import
    return safe


def execute(source: str) -> int:
    """Exec the user code. Returns the process exit code."""
    # Prime linecache so a traceback shows the offending source line without
    # putting the temp path in front of the model.
    linecache.cache["<handout>"] = (
        len(source),
        None,
        source.splitlines(keepends=True),
        "<handout>",
    )

    sandbox_globals = {"__builtins__": build_safe_builtins(), "__name__": "__main__"}

    try:
        exec(compile(source, "<handout>", "exec"), sandbox_globals)  # noqa: S102
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        print(code, file=sys.stderr)
        return 1
    except BaseException:  # noqa: BLE001 - the child's job is to report, not to survive
        exc_type, exc, tb = sys.exc_info()
        # Drop this function's own frame so the traceback starts at <handout>.
        if tb is not None and tb.tb_next is not None:
            tb = tb.tb_next
        sys.stderr.write("".join(traceback.format_exception(exc_type, exc, tb)))
        return 1
    return 0


# --------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        _note("usage: _sandbox_child.py <workdir> <code_path> <memory_mb> <timeout_s>")
        return 2

    workdir, code_path, memory_raw, timeout_raw = argv
    try:
        memory_mb = int(memory_raw)
    except ValueError:
        memory_mb = 768
    try:
        timeout_s = float(timeout_raw)
    except ValueError:
        timeout_s = 30.0

    try:
        with open(code_path, "r", encoding="utf-8") as handle:
            source = handle.read()
    except OSError as exc:
        _note(f"the code file could not be read: {exc}")
        return 2

    try:
        os.chdir(workdir)
    except OSError:
        # The parent already set cwd; this is belt and braces.
        pass

    apply_resource_limits(memory_mb, timeout_s)  # 1
    preimport_from_source(source)  # 2
    install_import_hook()  # 3
    disable_network()  # 4
    code = execute(source)  # 5

    sys.stdout.flush()
    sys.stderr.flush()
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
