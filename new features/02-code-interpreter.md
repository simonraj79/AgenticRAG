# Feature 2 — `run_python`, the sandboxed code interpreter

> Shared contracts: [00-IMPLEMENTATION-PLAN.md §4](00-IMPLEMENTATION-PLAN.md).
> **Read §5 of this document before changing anything in `app/tools/`.** It states exactly
> what the sandbox does and does not protect against, and a change that quietly weakens one
> of those layers will not fail any test.

---

## 1. What it is

A tool that takes Python source written by the model, runs it in a separate process with no
credentials and no network, and returns whatever it printed plus whatever files it wrote.
Those files become Handouts.

```python
class RunPythonArgs(BaseModel):
    code: str = Field(description="Python source. Write files to the current directory. "
                                  "You have no network and no filesystem outside it.")
    purpose: str = Field(description="One line: what this produces and why.")
```

`purpose` is not decoration. It is what the Handout is titled with, it is what the trace
shows, and asking for it measurably improves the code — a model that has to state the goal
writes to it.

---

## 2. Why a subprocess, and what was rejected

| Option | Verdict |
|---|---|
| `exec()` in the FastAPI process | No. Shares the interpreter, the event loop, `settings`, and every API key in `os.environ` |
| Restricted-eval (RestrictedPython, `__builtins__` stripping) | No. In-process sandboxes in CPython have a long history of escapes via attribute chains; and matplotlib will not run under one |
| Docker-per-call | No. Render's starter plan has no Docker-in-Docker; adds a runtime dependency the workshop cannot assume |
| Hosted sandbox (E2B, Modal) | Correct at scale; rejected here for a new vendor, a new key, a paid dependency and a Singapore-to-elsewhere hop in a demo |
| **Separate process, hardened** | **Chosen.** Real process isolation for memory and crashes, an empty environment, OS resource limits on Linux, and a kill that actually works |

---

## 3. Layout

```
app/tools/
  __init__.py
  sandbox.py            # parent side: static check, spawn, harvest, cap
  _sandbox_child.py     # child side: import hook, socket kill, rlimits, exec
  interpreter.py        # the LangChain tool wrapping sandbox.run
```

`_sandbox_child.py` is executed as a **script**, never imported by the app. It must have no
imports from `app.*` — it runs in an interpreter that cannot see the project.

---

## 4. The parent — `sandbox.py`

```python
@dataclass(frozen=True)
class SandboxArtifact:
    filename: str
    mime_type: str
    content: bytes
    @property
    def byte_size(self) -> int: ...

@dataclass(frozen=True)
class SandboxResult:
    ok: bool
    exit_code: int
    stdout: str
    stderr: str
    artifacts: list[SandboxArtifact]
    duration_ms: int
    error: str | None = None
    error_kind: str | None = None   # "import" | "syntax" | "timeout" | "runtime" | "output"

def static_check(code: str) -> str | None:
    """Return a human-readable refusal, or None if the code may run."""

async def run(code: str, *, timeout_s: float | None = None) -> SandboxResult:
    """asyncio.to_thread around _run_blocking. Never raises."""
```

### 4.1 Static check — first line, and the one the model can learn from

`ast.parse` the source, then walk it:

- Every `import X` / `from X import ...` top-level name must be in `ALLOWED_IMPORTS`.
  A refusal names the offending module and lists what is allowed, so the model's next
  attempt is usually correct.
- Reject these names anywhere they are *called* or attribute-accessed:
  `eval`, `exec`, `compile`, `__import__`, `globals`, `locals`, `vars`, `getattr`, `setattr`,
  `delattr`, `breakpoint`, `input`, `memoryview`.
- Reject any attribute access whose name starts and ends with `__` (`__class__`,
  `__subclasses__`, `__globals__`, `__builtins__`) — the standard escape chain.
- Syntax errors return `error_kind="syntax"` with the line number, without spawning anything.

```python
ALLOWED_IMPORTS = frozenset({
    # plotting
    "matplotlib", "mpl_toolkits",
    # decks and documents
    "pptx",
    # data
    "pandas", "numpy",
    # stdlib, the safe subset
    "math", "statistics", "random", "itertools", "functools", "collections",
    "datetime", "decimal", "fractions", "json", "csv", "io", "re", "textwrap",
    "string", "typing", "dataclasses", "enum", "pathlib", "base64", "colorsys",
})
```

`pathlib` is allowed because writing files is the point; `os` is not, because `os.system`
and `os.environ` are. `io` is allowed for in-memory buffers. Nothing that opens a socket,
starts a process, loads a shared library or reflects on the interpreter is on the list.

**The static check is a usability feature as much as a security one.** It fails in
milliseconds with a message the model can act on, instead of after a 1.5 s matplotlib import.

### 4.2 Spawn

```python
argv = [sys.executable, "-I", str(CHILD), str(workdir), code_path, str(memory_mb)]
proc = subprocess.run(argv, cwd=workdir, env=_minimal_env(),
                      capture_output=True, timeout=timeout_s, text=True,
                      errors="replace")
```

- **`-I` (isolated).** Implies `-E` (ignore `PYTHON*` env vars) and `-s` (no user site
  directory). The venv's `site-packages` still loads, which is why matplotlib works.
- **`env=_minimal_env()`** — this is the strongest control in the whole design and the
  cheapest. The child gets `PATH`, `SYSTEMROOT`/`TEMP` on Windows, `LANG`, `HOME`, and
  `MPLBACKEND=Agg`. **Nothing else.** No `OPENROUTER_API_KEY`, no `PINECONE_API_KEY`, no
  `DATABASE_URL`, no `GEMINI_API_KEY`, no `RENDER_API_KEY`. Code that fully escapes the
  import allowlist still finds an environment with no secrets in it.
- **`cwd=workdir`** — a fresh `tempfile.mkdtemp()` per call, removed in a `finally`.
- `code` is written to a file rather than passed on argv; a 4 KB program exceeds Windows'
  command-line limits and `-c` would put the source in the process table.
- `errors="replace"` on decode. The Windows console codepage mangles non-ASCII, and a
  UnicodeDecodeError while *reading a traceback* is the worst possible place to hit it.
- `text=True` with `capture_output` reads both pipes concurrently, so a child writing a lot
  to stderr cannot deadlock on a full pipe buffer.

### 4.3 Timeout

`subprocess.run(timeout=...)` raises `TimeoutExpired` after killing the child. On Windows the
kill is `TerminateProcess`, which does not reap grandchildren — but `subprocess` is not on
the import allowlist, so there are none. Returns `error_kind="timeout"`.

### 4.4 Harvest

After a clean exit, walk `workdir` (excluding the code file):

- Skip anything not in `HARVEST_MIME`, so a stray `.pyc` or `__pycache__` never becomes a handout.
- Any single file over `sandbox_max_artifact_bytes` (5 MB) -> `error_kind="output"`,
  a message naming the file and its size, and **no** artifacts returned. Partial success
  here would be worse than failure: the model would think it succeeded.
- Total over `sandbox_max_total_bytes` (15 MB) -> same.
- Deterministic order: sorted by filename, so two identical runs produce identical handouts.

```python
HARVEST_MIME = {
    ".png":  "image/png",
    ".svg":  "image/svg+xml",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".csv":  "text/csv",
    ".md":   "text/markdown",
    ".json": "application/json",
    ".txt":  "text/plain",
}
```

### 4.5 Output caps

`stdout` and `stderr` are each truncated to `sandbox_max_output_chars` (8,000) with a
`... [truncated, N more characters]` suffix. A runaway `print` in a loop is a real failure
mode and an untruncated one would blow the model's context on the next step.

---

## 5. The child — `_sandbox_child.py`, and exactly what it guarantees

Order matters here; each step depends on the previous one having finished.

```
1. resource limits          (POSIX only)
2. pre-import the modules the code actually names
3. install the deny-everything-else import hook
4. neuter sockets
5. exec the user code with a trimmed __builtins__
```

**Step 1 — `resource.setrlimit`, POSIX only.** Render is Linux, so production gets these;
Windows development does not, and the module logs that it is running without them rather
than pretending otherwise.

| Limit | Value | Stops |
|---|---|---|
| `RLIMIT_CPU` | `ceil(timeout_s) + 1` | A busy loop burning a core past the wall clock |
| `RLIMIT_AS` | `sandbox_memory_mb` (768 MB) | `[0] * 10**10` |
| `RLIMIT_FSIZE` | `sandbox_max_total_bytes` | Filling the disk |
| `RLIMIT_NPROC` | current + 0 | `fork()` bombs |
| `RLIMIT_CORE` | 0 | Core dumps |

**Step 2 — pre-import, driven by the code's own AST.** The deny hook in step 3 blocks
anything not already in `sys.modules`, and matplotlib's transitive imports run into the
hundreds. Rather than allowlisting those by hand — a list that rots — the child re-parses
the code, intersects its imports with `ALLOWED_IMPORTS`, and imports exactly those first.
A chart job never pays matplotlib's 1.5 s *and* python-pptx's.

**Step 3 — the import hook.** A `sys.meta_path` finder at position 0 that raises
`ImportError` for any module not already in `sys.modules` and not in `ALLOWED_IMPORTS`. This
is the second line; the static check in the parent is the first. Both exist because the
static check reads source text and a lazy import inside a library function does not appear
in it.

**Step 4 — sockets.** `socket.socket.__init__`, `socket.create_connection`,
`socket.socketpair` and `socket.getaddrinfo` are replaced with functions that raise
`PermissionError`. This runs *after* the pre-imports because matplotlib touches `socket` at
import time on some platforms and would fail to load otherwise.

**Step 5 — exec.** `exec(compile(code, "<handout>", "exec"), {"__builtins__": SAFE_BUILTINS,
"__name__": "__main__"})`. `SAFE_BUILTINS` is a copy of `builtins` with `eval`, `exec`,
`compile`, `__import__`, `open`... — no. `open` **stays**, because writing a file is the
entire purpose, and the confinement of writes is the empty cwd, not the builtin.

### What this stops

- Reading any credential the application holds — there are none in the environment
- Making any network request — no sockets, no `urllib`, no `requests` in the allowlist
- Reading or writing the database — no driver importable, no URL to connect to
- Starting a process — no `os`, no `subprocess`, `RLIMIT_NPROC` on Linux
- Crashing, hanging or exhausting the API process — separate process, hard kill, rlimits
- Filling the disk or returning gigabytes — `RLIMIT_FSIZE` plus harvest caps

### What this does NOT stop, stated plainly

- **It is not a container.** The child runs as the same OS user as the API process. Code
  that defeats both the AST check and the import hook can read any file that user can read.
  On Render that is the application container; on a developer laptop it is the developer's
  home directory.
- **The import allowlist is a denylist in disguise.** `ALLOWED_IMPORTS` grants matplotlib,
  numpy and pandas — each of which is a large C-extension surface, and numpy in particular
  can read and write arbitrary files through its own IO functions.
- **No syscall filtering.** No seccomp, no AppArmor.
- **Windows gets no resource limits at all.** Development is measurably less protected than
  production, which is the reverse of the usual arrangement and worth knowing.

**Threat model this is calibrated for**: the code's author is a language model working on a
user's own corpus, not an attacker with a shell. It defends against a confused model, a
prompt-injected corpus nudging it toward exfiltration (no network, no secrets), and runaway
resource use. It is not calibrated for a determined attacker with arbitrary input, and if
this ever accepts user-authored Python directly, §5 is the section that has to change first.

---

## 6. The tool wrapper — `interpreter.py`

```python
def build_python_tool(ctx: ToolContext) -> BaseTool
```

Returns a `StructuredTool` over `RunPythonArgs`. On each call it:

1. `sandbox.run(code)`
2. Appends every artifact to `ctx.artifacts` with `source_code=code`, `title=purpose`
3. Returns a **string** to the model:

```
Exit 0 in 2.4s.

stdout:
Chart written with 4 bars.

Files produced:
  budget.png  (image/png, 41.2 KB)

These are saved as handouts. Refer to them by name; do not claim any others exist.
```

On failure the string is the error plus the last 30 lines of the traceback. That is what
makes step-4 self-correction work.

**The tool returns text, never bytes.** Bytes go on `ctx.artifacts`, which `run_turn`
persists. A `ToolMessage` holding a base64 PNG would blow the context window in one call.

---

## 7. Dependencies

```
# requirements.in
matplotlib          # charts. Agg backend only -- MPLBACKEND=Agg is set for the child
python-pptx         # .pptx decks
```

`pandas` 3.0.5 and `numpy` 2.5.2 are already installed (Ragas transitives) and are promoted
to direct dependencies, because the sandbox now imports them by name and a change to Ragas'
dependency tree must not silently remove them.

**After `pip freeze`, restore the `pywin32` marker.** It has been flattened twice.

```bash
grep -n 'pywin32' backend/requirements.txt   # must read: pywin32==312; sys_platform == "win32"
```

Cost: roughly 55 MB added to the Render build. Accepted and recorded.

---

## 8. Test harness — `scripts/sandbox_check.py`

Runs standalone, no database, no API. Twelve cases.

| # | Case | Expected |
|---|---|---|
| 1 | `print("hello")` | `ok`, stdout `hello`, no artifacts |
| 2 | matplotlib bar chart -> `chart.png` | `ok`, one `image/png` artifact, non-trivial size |
| 3 | python-pptx 3-slide deck -> `deck.pptx` | `ok`, one pptx artifact |
| 4 | pandas -> `data.csv` | `ok`, one `text/csv` artifact |
| 5 | `1/0` | `ok=False`, `error_kind="runtime"`, `ZeroDivisionError` in stderr |
| 6 | `def f(:` | `ok=False`, `error_kind="syntax"`, **no process spawned** |
| 7 | `while True: pass` | `ok=False`, `error_kind="timeout"`, returns within `timeout_s + 2` |
| 8 | **`import os; os.environ`** | refused by static check, `error_kind="import"` |
| 9 | **`import socket`** | refused by static check |
| 10 | **`__import__("os").system("echo pwned")`** | refused — `__import__` is on the name denylist |
| 11 | **`().__class__.__base__.__subclasses__()`** | refused — dunder attribute access |
| 12 | **writes a 9 MB file** | `ok=False`, `error_kind="output"`, **zero** artifacts returned |
| 13 | **`import matplotlib; matplotlib.os.environ`** | refused — attribute reach through an *allowed* library |
| 14 | **`import numpy; numpy.ctypeslib.ctypes`** | refused — same shape, and ctypes reaches native code |
| 15 | matplotlib + pathlib writing two real files | `ok`, two artifacts — the denylist must not be too wide |

Cases 8-14 are the ones that matter. A change to `sandbox.py` that leaves 1-7 green and
turns any of 8-14 red has removed a control.

**Cases 13-15 were added after the first twelve passed**, and how they were found is the
useful part. Reasoning about the design produced §4.1's import allowlist; *probing* the
built sandbox produced these. `import matplotlib` is legitimate and allowed, `matplotlib.os`
is then a public attribute of an allowed module, and `os` is not a dunder — so the import
check, the name denylist and the dunder rule all saw a clean program.

Neither probe actually leaked anything: `matplotlib.os.environ.get("OPENROUTER_API_KEY")`
returned `None`, because `_minimal_env()` had already stripped it. **The cheap layer failed
and the strong one held**, which is the layered model working exactly as §5 claims. It is
still worth closing, because `numpy.ctypeslib.ctypes` is an escape to arbitrary native code
rather than to the filesystem, and the fix is one frozenset (`DENIED_MODULE_ATTRS`), applied
to attribute access rather than only to imports.

Case 15 guards the opposite failure, which is the likelier one over time: a denylist wide
enough to block `matplotlib.os` must not block `matplotlib.pyplot`. Without it, the safe
response to any future scare is to keep adding names until the tool quietly stops working.

Additionally, and asserted rather than eyeballed: **case 1 run with a fake
`OPENROUTER_API_KEY` in the parent's environment must not find it in the child.** The test
prints `os.environ` — which is refused — so it is asserted from the parent instead, by
checking `_minimal_env()` contains no key whose name matches `KEY|SECRET|TOKEN|PASSWORD|DATABASE_URL`.
