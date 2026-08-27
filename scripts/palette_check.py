"""
Does any component still name a COLOUR instead of a ROLE?

Run it with the global interpreter, from the repo root, with nothing running:

    python scripts/palette_check.py
    python scripts/palette_check.py --verbose     # list every hit, not just a count

Why this exists
---------------
The frontend's design system lives in two files: the token layer in
`frontend/src/index.css` and the shared class strings in `frontend/src/lib/styles.ts`.
A component is supposed to say WHAT a colour is for -- `bg-surface`, `text-muted`,
`border-line` -- and never WHICH colour it is. That indirection is the only reason
light and dark are one codebase rather than two.

A raw palette utility silently opts out of it. `bg-slate-900` renders a dark panel in
BOTH themes: correct in the dark, unreadable on paper, and nothing raises. That is the
failure shape this repo keeps rediscovering -- the check passes and the thing you wanted
did not happen -- so it gets a check that can actually see it.

This is the frontend sibling of `metering_check.py` case 12, and it is built the same
way and for the same reason. That case walks the application's call graph with `ast`
rather than testing a call site it wrote itself, because:

    A harness cannot prove instrumentation is COMPLETE, only that the instrumentation
    it was handed works.

The same is true here one storey down. A vitest case can prove a component renders; it
cannot prove no component anywhere still hardcodes a colour, because jsdom never loads
Tailwind and `toBeVisible()` is inert without it -- the audit confirmed exactly one of
the 62 unit cases asserts on a class name at all. Coverage is a property of the SOURCE,
so the check reads the source.

Direction of error
------------------
The regex is deliberately broad on prefixes and strict on palette names, which means it
can raise a false ALARM (a `text-red-500` inside a string that is not a className) and
cannot give a false ALL-CLEAR for any real utility. That is the correct direction to be
wrong in: a false alarm costs one `# palette-check: ignore` comment, a false all-clear
costs an unreadable page in the theme nobody was looking at.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "frontend" / "src"

# Every hue Tailwind ships. A component may name none of them.
STOCK_HUES = (
    "slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|"
    "teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose"
)

# Every utility prefix that takes a colour. `shadow-` is included because
# `shadow-emerald-950/30` is a colour; the sized `shadow-lg` has no hue after it
# and therefore cannot match.
COLOUR_PREFIXES = (
    "bg|text|border|ring|outline|divide|from|via|to|decoration|accent|fill|stroke|"
    "shadow|placeholder|caret|selection"
)

BANNED_UTILITY = re.compile(
    rf"\b(?:{COLOUR_PREFIXES})-(?:{STOCK_HUES})-\d{{2,3}}(?:/\d{{1,3}})?\b"
)

# `white` and `black` take no numeric shade, so they need their own pattern. They
# are banned for the same reason: `text-white` is correct in exactly one theme.
BANNED_ABSOLUTE = re.compile(
    rf"\b(?:{COLOUR_PREFIXES})-(?:white|black)(?:/\d{{1,3}})?\b"
)

# A literal colour written into JSX -- an inline style, an SVG fill, a gradient.
# Reported but NOT failed, because a few are legitimate: a third party's brand
# mark has one correct colour and it is not ours to theme.
LITERAL_COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(")

# A line carrying this marker is exempt. Kept deliberately awkward to type, so
# reaching for it is a decision rather than a reflex.
IGNORE_MARKER = "palette-check: ignore"


def source_files() -> list[Path]:
    """Every TS/TSX file under src/, in a stable order."""
    return sorted(
        p for p in SRC.rglob("*") if p.suffix in {".ts", ".tsx"} and p.is_file()
    )


def scan(path: Path) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    """Returns (banned, literals) as (line number, matched text) pairs."""
    banned: list[tuple[int, str]] = []
    literals: list[tuple[int, str]] = []

    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if IGNORE_MARKER in line:
            continue
        for pattern in (BANNED_UTILITY, BANNED_ABSOLUTE):
            for match in pattern.finditer(line):
                banned.append((number, match.group(0)))
        for match in LITERAL_COLOUR.finditer(line):
            literals.append((number, match.group(0)))

    return banned, literals


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="list every hit rather than the first few per file",
    )
    args = parser.parse_args()

    if not SRC.is_dir():
        print(f"[FAIL] no source directory at {SRC}")
        return 2

    files = source_files()
    total_banned = 0
    total_literals = 0
    dirty_files = 0

    print(f"scanning {len(files)} files under frontend/src")
    print()

    for path in files:
        banned, literals = scan(path)
        total_literals += len(literals)

        if not banned:
            continue

        dirty_files += 1
        total_banned += len(banned)
        relative = path.relative_to(REPO_ROOT).as_posix()
        print(f"[FAIL] {relative} -- {len(banned)} hardcoded colour(s)")

        shown = banned if args.verbose else banned[:6]
        for number, text in shown:
            print(f"         {relative}:{number}  {text}")
        if len(banned) > len(shown):
            print(f"         ... and {len(banned) - len(shown)} more (--verbose)")

    print()
    print("-" * 68)

    if total_literals:
        # Not a failure. A brand mark's hex is correct exactly as written, and
        # this exists so a NEW one is noticed rather than blocked.
        print(
            f"[note] {total_literals} literal colour value(s) in JSX "
            f"(inline styles, SVG fills). Legitimate for third-party marks; "
            f"check any others belong."
        )

    if total_banned:
        print(
            f"[FAIL] {total_banned} hardcoded colour utilit(ies) across "
            f"{dirty_files} file(s)."
        )
        print()
        print("       A component names a ROLE, never a colour. Map each one onto a")
        print("       token from frontend/src/lib/styles.ts -- bg-surface, text-muted,")
        print("       border-line, and the PILL/BTN/FIELD strings.")
        print()
        print("       If a hit is genuinely not a class name, put")
        print(f"       `{IGNORE_MARKER}` on that line with a reason.")
        return 1

    print("[PASS] every colour in the frontend comes from a token.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
