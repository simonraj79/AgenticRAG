"""Layer 1 harness for `app/rag/refusal.py`. No DB, no API, no model -- seconds.

WHY THIS FILE EXISTS, AND WHY IT DID NOT UNTIL NOW.

`refusal.py` is the most-corrected module in this repository. Its own docstrings
record the marker list being wrong three separate times -- `"does not say"`,
`"does not cover"`, `"does not state"` -- each discovered by reading a scorecard
that had blamed the agent for the detector. `new features/loop.md` T3 turned that
into a rule: when a list has been wrong three times, add the SHAPE rather than the
string. A fourth arrived with the DeepSeek swap and was not a phrase at all --
`"The material does **not** mention ..."`, where the marker is present, the
meaning is present, and the substring match cannot see it because the negation was
bolded.

Four corrections, and not one of them could have been caught by anything in
`scripts/`. `sandbox_check.py` guards the sandbox, `ledger_check.py` guards the
ledger, `agentic_check.py` needs a database and a live model and twenty minutes.
The component with the worst track record in the repo had no harness at all, and
every one of its regressions is SILENT: `detect_gap` failing means a search
quietly does not happen, and `detect_refusal` failing means `queries.refused`
quietly reports the wrong number. Nothing raises in either case.

WHAT THIS ASSERTS, DELIBERATELY IN BOTH DIRECTIONS.

`loop.md` T3: strictness follows the cost of being wrong in EACH direction, and
the two functions here are tuned oppositely on purpose. So the cases come in
pairs -- for every "this must be detected" there is a "this must NOT be", because
a detector that has been widened four times is far likelier to fail next by
over-firing than by under-firing. Cases 20-24 are that guard, and they are the
ones to read first if a future widening turns them red.

    backend/.venv/Scripts/python.exe scripts/refusal_check.py

ASCII in print(), exit 1 on any failure -- `new features/06-test-plan.md`.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.rag.refusal import (  # noqa: E402
    CAVEAT_MARKERS,
    REFUSAL_LEAD_CHARS,
    REFUSAL_MARKERS,
    detect_gap,
    detect_refusal,
    sentences,
    strip_emphasis,
)

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "[ok]  " if condition else "[FAIL]"
    print(f"{status} {name}" + (f" -- {detail}" if detail else ""))
    if not condition:
        failures.append(name)


def gap(name: str, text: str, *, expect: bool) -> None:
    """detect_gap fires (or does not) on `text`."""
    got = detect_gap(text)
    ok = bool(got) == expect
    check(
        name,
        ok,
        f"expected {'a marker' if expect else 'None'}, got {got!r}",
    )


def refusal(name: str, text: str, *, expect: bool) -> None:
    """detect_refusal fires (or does not) on `text`."""
    got = detect_refusal(text)
    ok = bool(got) == expect
    check(
        name,
        ok,
        f"expected {'a marker' if expect else 'None'}, got {got!r}",
    )


print("=" * 74)
print("refusal.py -- detectors, both directions")
print("=" * 74)

# ---------------------------------------------------------------------------
# 1-4. The three historical misses. Each of these shipped broken once.
# ---------------------------------------------------------------------------
print("\n-- the phrases this list has already been wrong about --")
gap("1. 'does not say'", "The provided text does not say which launch took place.", expect=True)
gap("2. 'does not cover'", "The provided text does not cover the crew duties.", expect=True)
gap("3. 'does not state'", "The text does not state which modulation scheme is used.", expect=True)
gap(
    "4. the rest of the reporting-verb family",
    "The context does not describe the antenna, does not indicate the gain, "
    "and does not detail the feed.",
    expect=True,
)

# 4b-4e. The FIFTH correction, and the only one caused by a marker being too
# SPECIFIC rather than missing. "not covered in the" matched one determiner;
# every real refusal observed used a different one.
print("\n-- determiner-independence (the fifth correction) --")
refusal(
    "4b. 'not covered in this briefing' -- the S7 answer that went red",
    "The corpus explicitly states that the modulation and coding schemes for the "
    "communications subsystem are held in a separate document and are not "
    "covered in this briefing.",
    expect=True,
)
gap(
    "4c. 'not covered in this material' -- the browser answer that missed twice",
    "The Ka-band downlink is described, but its modulation and coding scheme is "
    "not covered in this material.",
    expect=True,
)
refusal(
    "4d. the original determiner still matches",
    "That is not covered in the supplied material.",
    expect=True,
)
refusal(
    "4e. 'not found in' is determiner-free too",
    "That figure is not found in any of these documents.",
    expect=True,
)

# ---------------------------------------------------------------------------
# 5-9. The FOURTH miss: markdown emphasis. Measured against
#      deepseek/deepseek-v4-flash-0731, 2026-08-16.
# ---------------------------------------------------------------------------
print("\n-- markdown emphasis, the fourth miss (real deepseek output) --")
gap(
    "5. bolded negation, verbatim from the probe",
    "According to the provided material, there are **twenty-four lithium-ion "
    "battery modules** arranged in three strings of eight. The material does "
    "**not** mention the cell chemistry vendor. (Source: power-subsystem.md)",
    expect=True,
)
gap(
    "6. bolded negation, second form",
    "The context does **not** provide the degradation rate.",
    expect=True,
)
gap("7. underscore emphasis", "The context does _not_ mention the vendor.", expect=True)
gap("8. backticked negation", "The context does `not` specify the limit.", expect=True)
refusal(
    "9. emphasis fix reaches detect_refusal too",
    "The provided material does **not** contain that information.",
    expect=True,
)

# ---------------------------------------------------------------------------
# 10-13. strip_emphasis itself -- it must not damage ordinary text.
# ---------------------------------------------------------------------------
print("\n-- strip_emphasis --")
check(
    "10. removes emphasis, keeps words separated",
    strip_emphasis("does **not** mention") == "does not mention",
    repr(strip_emphasis("does **not** mention")),
)
check(
    "11. preserves newlines, because sentences() splits on blank lines",
    "\n\n" in strip_emphasis("One **sentence**.\n\nAnother."),
    repr(strip_emphasis("One **sentence**.\n\nAnother.")),
)
check(
    "12. leaves unemphasised text byte-identical",
    strip_emphasis("The array generates 4.2 kW [1].") == "The array generates 4.2 kW [1].",
)
check(
    "13. blank-line split still works after stripping",
    len(sentences(strip_emphasis("**A.**\n\n**B.**"))) == 2,
    f"{sentences(strip_emphasis('**A.**' + chr(10) * 2 + '**B.**'))}",
)

# ---------------------------------------------------------------------------
# 14-19. The asymmetry between the two functions -- loop.md T3.
#        This is the property that makes them two functions and not one.
# ---------------------------------------------------------------------------
print("\n-- the asymmetry: same text, two answers, on purpose --")
ANSWER_THEN_CAVEAT = (
    "The platform carries twenty-four lithium-ion battery modules [1]. The "
    "provided text does not cover the onboard storage for science instruments."
)
refusal(
    "14. answer-then-CAVEAT-tier phrase is NOT a refusal (protects refusal_pass)",
    ANSWER_THEN_CAVEAT,
    expect=False,
)
gap(
    "15. ...but IS a gap (this is the turn that should have searched)",
    ANSWER_THEN_CAVEAT,
    expect=True,
)
# 15b pins the tier distinction itself, because this is where `refusal.py`'s
# docstring was wrong until 2026-08-16: it illustrated the asymmetry with
# "does not contain", a HARD-tier phrase, which matches in any sentence within
# the lead and therefore does NOT demonstrate it. Asserting the actual behaviour
# here means the next person to reword that paragraph finds out immediately.
#
# This is not an endorsement. CLAUDE.md's rule is that the hard tier is for
# phrases a model would never write while answering, and this text is a
# counter-example to its own marker. Moving it would change what
# `queries.refused` means and break comparability with every scorecard in
# EVAL.md, so it is pinned and flagged rather than quietly changed.
refusal(
    "15b. answer-then-HARD-tier phrase DOES score as a refusal (pinned, see PRD)",
    "The platform carries twenty-four lithium-ion battery modules [1]. The "
    "provided text does not contain information regarding the onboard storage.",
    expect=True,
)
refusal(
    "16. a lead refusal IS a refusal",
    "The provided text does not contain that information.",
    expect=True,
)
gap(
    "17. bolded answer-then-caveat, the deepseek shape of case 14",
    "Based on the provided context: the solar array generates **4.2 kW**. The "
    "context does **not** provide the degradation rate.",
    expect=True,
)
refusal(
    "18. ...and that one is still not a refusal",
    "Based on the provided context: the solar array generates **4.2 kW**. The "
    "context does **not** provide the degradation rate.",
    expect=False,
)
check(
    "19. every caveat marker is absent from the hard tier",
    not (set(CAVEAT_MARKERS) & set(REFUSAL_MARKERS)),
    f"{len(REFUSAL_MARKERS)} hard, {len(CAVEAT_MARKERS)} caveat",
)

# ---------------------------------------------------------------------------
# 20-24. THE OVER-FIRING GUARD. Read these first if a widening turns them red.
# ---------------------------------------------------------------------------
print("\n-- must NOT fire: a detector widened four times fails next by over-firing --")
gap(
    "20. a clean grounded answer is not a gap",
    "The solar array generates 4.2 kW at end of life [1]. The communications "
    "subsystem is allocated 310 W of that [1].",
    expect=False,
)
gap(
    "21. emphasis on ordinary words must not invent a gap",
    "The array generates **4.2 kW** and the batteries retain **78 percent** of "
    "capacity [1].",
    expect=False,
)
gap(
    "22. a negation that is not an admission",
    "The batteries do not exceed 78 percent depth of discharge [1], and the "
    "array does better than its allocation [1].",
    expect=False,
)
refusal(
    "23. a long answer that caveats late is not a refusal",
    ("The platform carries twenty-four modules in three strings [1]. " * 4)
    + "The context does not mention the vendor.",
    expect=False,
)
gap(
    "24. an empty answer is not a gap",
    "",
    expect=False,
)

# ---------------------------------------------------------------------------
# 25-26. The budget, which the emphasis change touches.
# ---------------------------------------------------------------------------
print("\n-- the content budget --")
check(
    "25. REFUSAL_LEAD_CHARS is still the documented 200",
    REFUSAL_LEAD_CHARS == 200,
    str(REFUSAL_LEAD_CHARS),
)
refusal(
    "26. markdown syntax is not charged against the budget as content",
    "**" * 60 + "The provided text does not contain that information.",
    expect=True,
)

# ---------------------------------------------------------------------------
# 27-33. Leaked tool-call markup. Not a detector, but the same failure class and
#        the same blast radius: text the model produced reaching the user
#        unexamined, with nothing raising.
# ---------------------------------------------------------------------------
print("\n-- leaked tool-call markup (agent_loop) --")
from app.rag.agent_loop import (  # noqa: E402
    _emit_until_markup,
    _strip_leaked_tool_markup,
)

LEAK = (
    "Let me try searching for the thermal rejection budget document.\n\n"
    '<｜DSML｜tool_calls> <｜DSML｜invoke name="search_corpus">'
)
check(
    "27. leaked markup is cut, prose before it is kept",
    _strip_leaked_tool_markup(LEAK)
    == "Let me try searching for the thermal rejection budget document.",
    repr(_strip_leaked_tool_markup(LEAK)),
)
check(
    "28. a clean answer is returned byte-identical",
    _strip_leaked_tool_markup("The array generates 4.2 kW [1].")
    == "The array generates 4.2 kW [1].",
)
check(
    "29. an answer discussing ASCII markup is untouched",
    _strip_leaked_tool_markup("Use <tool> tags, or the | pipe character.")
    == "Use <tool> tags, or the | pipe character.",
)
check(
    "30. streaming gate opens on clean text",
    _emit_until_markup("The array ", []) is True,
)
check(
    "31. streaming gate closes on the sentinel",
    _emit_until_markup("<｜DSML", ["Some answer. "]) is False,
)
check(
    "32. gate LATCHES -- prose after the leak stays suppressed",
    _emit_until_markup(" more prose", ["ok <｜DSML｜tool_calls>"]) is False,
)
check(
    "33. sentinel split across a chunk boundary is still caught",
    _emit_until_markup("｜DSML", ["Some answer. <"]) is False,
    "the half-sentinel case a per-chunk test misses",
)

print("\n" + "=" * 74)
if failures:
    print(f"{len(failures)} FAILED:")
    for name in failures:
        print(f"  - {name}")
    sys.exit(1)
print("all checks passed")
