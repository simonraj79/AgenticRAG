"""Standalone check of the code-interpreter sandbox. No database, no API.

The twelve cases from `new features/02-code-interpreter.md` section 8, three more
found by probing afterwards, plus one assertion about the child's environment
that cannot be made from inside the sandbox (the test would have to print
`os.environ`, which is refused).

Cases 1-7 are behaviour. **Cases 8-14 are the security contract**: a change to
`sandbox.py` that leaves 1-7 green and turns any of 8-14 red has removed a
control, and this script says so as loudly as a terminal allows.

Cases 13-15 exist because the first twelve all passed and the sandbox was still
porous. `import matplotlib` is allowed; `matplotlib.os` is then a public
attribute of an allowed module, and the import allowlist, the dunder rule and
the name denylist all saw nothing wrong with it. `numpy.ctypeslib.ctypes` is the
same shape and reaches native code rather than the filesystem. Neither leaked
anything -- `_minimal_env()` had already removed what the first one went looking
for -- which is the layered model behaving as designed and not a reason to leave
the outer layer open. Case 15 guards the other direction: the denylist that
blocks `matplotlib.os` must not block `matplotlib.pyplot`.

Usage:
    backend/.venv/Scripts/python.exe scripts/sandbox_check.py

Exits 1 if anything fails.

ASCII only in print(). The Windows console codepage mangles em-dashes, section
signs and emoji, and this has broken three throwaway scripts in this repo
already. Anything echoed back from the sandbox goes through ascii().
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.tools.sandbox import (  # noqa: E402
    SandboxResult,
    _minimal_env,
    run,
    static_check,
)

# A fake secret planted in the parent BEFORE the child is ever spawned. If any
# of this leaks into _minimal_env(), the environment assertion at the end fails.
os.environ["OPENROUTER_API_KEY"] = "sk-or-v1-FAKE-CANARY-FOR-SANDBOX-CHECK"
os.environ["DATABASE_URL"] = "postgresql://canary:canary@example.invalid/canary"

SECRET_PATTERN = re.compile(r"KEY|SECRET|TOKEN|PASSWORD|DATABASE_URL", re.IGNORECASE)

SECURITY_CASES = {8, 9, 10, 11, 12, 13, 14}


def short(text: str, limit: int = 160) -> str:
    """ASCII-safe, single-line, bounded. Everything from the sandbox goes here."""
    flat = " ".join(text.split())
    if len(flat) > limit:
        flat = flat[:limit] + "..."
    # ascii() quotes the result and escapes anything the console cannot draw.
    return ascii(flat)


# --------------------------------------------------------------------------
# The cases
# --------------------------------------------------------------------------

CASE_1 = 'print("hello")\n'

CASE_2 = """
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

labels = ["alpha", "beta", "gamma", "delta"]
values = [3, 1, 4, 1]
fig, ax = plt.subplots(figsize=(4, 3))
ax.bar(labels, values)
ax.set_title("Sandbox chart")
fig.tight_layout()
fig.savefig("chart.png", dpi=100)
print("chart written with", len(labels), "bars")
"""

CASE_3 = """
from pptx import Presentation
from pptx.util import Pt

prs = Presentation()
layout = prs.slide_layouts[5]
count = 0
for heading in ("One", "Two", "Three"):
    slide = prs.slides.add_slide(layout)
    slide.shapes.title.text = heading
    slide.shapes.title.text_frame.paragraphs[0].font.size = Pt(36)
    count += 1
prs.save("deck.pptx")
print("deck written with", count, "slides")
"""

CASE_4 = """
import pandas as pd

frame = pd.DataFrame({"n": [1, 2, 3, 4], "square": [1, 4, 9, 16]})
frame.to_csv("data.csv", index=False)
print("rows:", len(frame))
"""

CASE_5 = "value = 1 / 0\n"

CASE_6 = "def f(:\n    pass\n"

CASE_7 = "while True:\n    pass\n"

CASE_8 = "import os\nprint(os.environ)\n"

CASE_9 = "import socket\ns = socket.socket()\n"

CASE_10 = '__import__("os").system("echo pwned")\n'

CASE_11 = "print(().__class__.__base__.__subclasses__())\n"

CASE_12 = """
with open("big.txt", "w", encoding="utf-8") as handle:
    handle.write("x" * 9_000_000)
print("wrote a big file")
"""


def check_1(result: SandboxResult) -> str | None:
    if not result.ok:
        return f"expected ok, got error_kind={result.error_kind} error={short(result.error or '')}"
    if "hello" not in result.stdout:
        return f"stdout did not contain hello: {short(result.stdout)}"
    if result.artifacts:
        return f"expected no artifacts, got {len(result.artifacts)}"
    return None


def check_2(result: SandboxResult) -> str | None:
    if not result.ok:
        return f"expected ok, got error_kind={result.error_kind} error={short(result.error or '')} stderr={short(result.stderr)}"
    if len(result.artifacts) != 1:
        return f"expected 1 artifact, got {[a.filename for a in result.artifacts]}"
    art = result.artifacts[0]
    if art.mime_type != "image/png":
        return f"expected image/png, got {art.mime_type}"
    if art.byte_size < 2000:
        return f"png is implausibly small: {art.byte_size} bytes"
    if not art.content.startswith(b"\x89PNG"):
        return "artifact does not carry a PNG signature"
    return None


def check_3(result: SandboxResult) -> str | None:
    if not result.ok:
        return f"expected ok, got error_kind={result.error_kind} error={short(result.error or '')} stderr={short(result.stderr)}"
    if len(result.artifacts) != 1:
        return f"expected 1 artifact, got {[a.filename for a in result.artifacts]}"
    art = result.artifacts[0]
    if not art.mime_type.endswith("presentationml.presentation"):
        return f"unexpected mime {art.mime_type}"
    if not art.content.startswith(b"PK"):
        return "pptx is not a zip container"
    if art.byte_size < 10_000:
        return f"pptx is implausibly small: {art.byte_size} bytes"
    return None


def check_4(result: SandboxResult) -> str | None:
    if not result.ok:
        return f"expected ok, got error_kind={result.error_kind} error={short(result.error or '')} stderr={short(result.stderr)}"
    if len(result.artifacts) != 1:
        return f"expected 1 artifact, got {[a.filename for a in result.artifacts]}"
    art = result.artifacts[0]
    if art.mime_type != "text/csv":
        return f"expected text/csv, got {art.mime_type}"
    if b"square" not in art.content:
        return f"csv body looks wrong: {short(art.content.decode('utf-8', 'replace'))}"
    return None


def check_5(result: SandboxResult) -> str | None:
    if result.ok:
        return "expected failure, got ok"
    if result.error_kind != "runtime":
        return f"expected error_kind=runtime, got {result.error_kind}"
    if "ZeroDivisionError" not in result.stderr:
        return f"ZeroDivisionError missing from stderr: {short(result.stderr)}"
    return None


def check_6(result: SandboxResult) -> str | None:
    if result.ok:
        return "expected failure, got ok"
    if result.error_kind != "syntax":
        return f"expected error_kind=syntax, got {result.error_kind}"
    if static_check(CASE_6) is None:
        return "static_check accepted a syntax error, so a process would be spawned"
    # A refusal costs a parse, not a process. 500 ms is generous for one.
    if result.duration_ms > 500:
        return f"took {result.duration_ms} ms, which suggests a process was spawned"
    if result.exit_code != -1:
        return f"expected the no-process sentinel exit_code=-1, got {result.exit_code}"
    return None


def check_7(result: SandboxResult) -> str | None:
    if result.ok:
        return "expected failure, got ok"
    if result.error_kind != "timeout":
        return f"expected error_kind=timeout, got {result.error_kind}"
    return None


def refusal_check(code: str, must_mention: tuple[str, ...]):
    """Cases 8-11: refused by the static check, before anything is spawned."""

    def check(result: SandboxResult) -> str | None:
        if result.ok:
            return "THE CODE RAN. The static check let it through."
        if result.error_kind != "import":
            return f"expected error_kind=import, got {result.error_kind}"
        message = static_check(code)
        if message is None:
            return "static_check returned None, so this reached a subprocess"
        for token in must_mention:
            if token not in message:
                return f"refusal does not name {token!r}: {short(message)}"
        if "Allowed" not in message and "Blocked" not in message:
            return f"refusal lists no alternatives, so the model cannot retry: {short(message)}"
        if result.duration_ms > 500:
            return f"took {result.duration_ms} ms, which suggests a process was spawned"
        return None

    return check


# Cases 13-15 were added after the first twelve passed, because probing found a
# hole reasoning had missed. `import matplotlib` is allowed and legitimate;
# `matplotlib.os` is then a public attribute of an allowed module, so the import
# allowlist, the dunder rule and the name denylist all saw nothing wrong.
# `numpy.ctypeslib.ctypes` is the same shape and worse -- ctypes reaches native
# code rather than merely the filesystem.
#
# Neither probe leaked anything, because `_minimal_env()` had already stripped
# the credentials the first one went looking for. That is the layered model
# working. It is still worth a regression test, because the fix (attribute names
# in `DENIED_MODULE_ATTRS`) is one frozenset that a later edit could trim without
# failing any of cases 1-12.
#
# Case 15 is the other half and matters just as much: a denylist wide enough to
# block `matplotlib.os` must not block `matplotlib.pyplot`. Without it, the safe
# response to any future scare is to add names until the tool stops working.
CASE_13 = 'import matplotlib\nprint(matplotlib.os.environ.get("PATH"))\n'
CASE_14 = "import numpy\nprint(numpy.ctypeslib.ctypes)\n"
CASE_15 = (
    "import matplotlib\n"
    "matplotlib.use('Agg')\n"
    "import matplotlib.pyplot as plt\n"
    "import pathlib\n"
    "fig, ax = plt.subplots()\n"
    "ax.bar(['a', 'b'], [1, 2])\n"
    "fig.savefig('ok.png')\n"
    "pathlib.Path('ok.csv').write_text('a,b\\n1,2\\n')\n"
    "print('both wrote')\n"
)


def check_15(result: SandboxResult) -> str | None:
    if not result.ok:
        return (
            "legitimate matplotlib + pathlib code was refused; the attribute "
            f"denylist is too wide: {short(result.error or result.stderr)}"
        )
    names = sorted(a.filename for a in result.artifacts)
    if names != ["ok.csv", "ok.png"]:
        return f"expected ok.csv and ok.png, got {names}"
    return None


def check_12(result: SandboxResult) -> str | None:
    if result.ok:
        return "a 9 MB file was accepted; the per-file cap is not enforced"
    if result.error_kind != "output":
        return f"expected error_kind=output, got {result.error_kind} ({short(result.error or '')})"
    if result.artifacts:
        return (
            f"returned {len(result.artifacts)} artifact(s) alongside the failure; "
            "a partial set would be reported to the model as success"
        )
    if "big.txt" not in (result.error or ""):
        return f"the error does not name the offending file: {short(result.error or '')}"
    return None


CASES = [
    (1, 'print("hello")', CASE_1, None, check_1),
    (2, "matplotlib bar chart -> chart.png", CASE_2, None, check_2),
    (3, "python-pptx 3-slide deck -> deck.pptx", CASE_3, None, check_3),
    (4, "pandas -> data.csv", CASE_4, None, check_4),
    (5, "1/0", CASE_5, None, check_5),
    (6, "def f(:", CASE_6, None, check_6),
    (7, "while True: pass", CASE_7, 5.0, check_7),
    (8, "import os; os.environ", CASE_8, None, refusal_check(CASE_8, ("'os'",))),
    (9, "import socket", CASE_9, None, refusal_check(CASE_9, ("'socket'",))),
    (
        10,
        '__import__("os").system(...)',
        CASE_10,
        None,
        refusal_check(CASE_10, ("__import__",)),
    ),
    (
        11,
        "().__class__.__base__.__subclasses__()",
        CASE_11,
        None,
        refusal_check(CASE_11, ("__subclasses__",)),
    ),
    (12, "writes a 9 MB file", CASE_12, None, check_12),
    (
        13,
        "matplotlib.os.environ (module reached via an ALLOWED library)",
        CASE_13,
        None,
        refusal_check(CASE_13, ("'os'",)),
    ),
    (
        14,
        "numpy.ctypeslib.ctypes (native code via an ALLOWED library)",
        CASE_14,
        None,
        refusal_check(CASE_14, ("'ctypes'",)),
    ),
    (15, "legitimate code still runs after the attribute rules", CASE_15, None, check_15),
]


# --------------------------------------------------------------------------
# The environment assertion
# --------------------------------------------------------------------------


def check_env() -> str | None:
    """The child must inherit no credential-shaped variable. Asserted here.

    It cannot be asserted from inside the sandbox: the test would have to print
    `os.environ`, and `import os` is refused (case 8). So the parent checks the
    environment it is about to hand over instead.
    """
    env = _minimal_env()
    offenders = sorted(name for name in env if SECRET_PATTERN.search(name))
    if offenders:
        return f"credential-shaped variables would reach the child: {offenders}"
    if env.get("MPLBACKEND") != "Agg":
        return f"MPLBACKEND is {env.get('MPLBACKEND')!r}, not 'Agg'"
    unexpected = sorted(
        set(env)
        - {"PATH", "LANG", "LC_ALL", "HOME", "SYSTEMROOT", "TEMP", "TMP", "MPLBACKEND"}
    )
    if unexpected:
        return f"unexpected variables in the child environment: {unexpected}"
    return None


# --------------------------------------------------------------------------


async def main() -> int:
    print("=" * 78)
    print("sandbox_check -- code interpreter, 12 cases plus the environment assertion")
    print("=" * 78)
    print(f"python:  {sys.executable}")
    print(f"platform:{os.name}")
    print()

    rows: list[tuple[str, str, str, str]] = []
    failures: list[tuple[str, str]] = []

    for number, label, code, timeout_s, check in CASES:
        started = time.perf_counter()
        result = await run(code, timeout_s=timeout_s)
        wall = time.perf_counter() - started

        problem = check(result)
        if problem is None and number == 7:
            budget = (timeout_s or 30.0) + 2.0
            if wall > budget:
                problem = f"returned after {wall:.1f}s, past the {budget:.0f}s budget"

        status = "[ok]" if problem is None else "[FAIL]"
        detail = "" if problem is None else problem
        rows.append((str(number), label, status, f"{wall:.1f}s"))

        marker = " " if problem is None else "!"
        print(f"{marker} {status:6} {number:>2}. {label}")
        print(
            f"           ok={result.ok} exit={result.exit_code} kind={result.error_kind} "
            f"artifacts={[a.filename for a in result.artifacts]} {wall:.1f}s"
        )
        if result.error:
            print(f"           error: {short(result.error)}")
        if result.stdout.strip():
            print(f"           stdout: {short(result.stdout)}")
        if problem is not None:
            failures.append((f"case {number} ({label})", problem))
            if number in SECURITY_CASES:
                print()
                print("  " + "!" * 74)
                print("  !!! SECURITY CASE FAILED -- A CONTROL HAS BEEN REMOVED !!!")
                print(f"  !!! case {number}: {label}")
                print(f"  !!! {problem}")
                print("  !!! Read new features/02-code-interpreter.md section 5 before")
                print("  !!! changing anything else in app/tools/.")
                print("  " + "!" * 74)
            else:
                print(f"           FAILED: {problem}")
        print()

    env_problem = check_env()
    env_status = "[ok]" if env_problem is None else "[FAIL]"
    marker = " " if env_problem is None else "!"
    print(f"{marker} {env_status:6} env. _minimal_env() carries no credentials")
    print(f"           keys: {sorted(_minimal_env())}")
    if env_problem is not None:
        failures.append(("environment assertion", env_problem))
        print()
        print("  " + "!" * 74)
        print("  !!! SECURITY CASE FAILED -- THE CHILD WOULD INHERIT A SECRET !!!")
        print(f"  !!! {env_problem}")
        print("  !!! _minimal_env() is the strongest control in the design. Only ever")
        print("  !!! remove names from it.")
        print("  " + "!" * 74)
    rows.append(("env", "_minimal_env() carries no credentials", env_status, "-"))
    print()

    print("=" * 78)
    print("summary")
    print("=" * 78)
    print(f"{'#':>4}  {'result':<7} {'time':>6}  case")
    print(f"{'-' * 4}  {'-' * 7} {'-' * 6}  {'-' * 46}")
    for number, label, status, wall in rows:
        print(f"{number:>4}  {status:<7} {wall:>6}  {label}")
    print()

    if failures:
        print(f"{len(failures)} FAILED:")
        for name, problem in failures:
            print(f"  - {name}: {problem}")
        return 1

    print(f"all {len(rows)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
