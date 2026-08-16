"""Layer 1 harness for specialist routing and the self-check trigger.

No database, no network, no model -- seconds. Layer 2 is
`scripts/agentic_check.py` S20-S27, which needs a live corpus and a live model
and costs provider quota; everything below is the half that can be true or false
without either.

    backend/.venv/Scripts/python.exe scripts/route_specialist_check.py

WHY THIS FILE EXISTS, IN THE SHAPE `new features/loop.md` T2 DESCRIBES.

Three components land at once and **not one of them fails by raising**:

- `parse_mentions` over-firing turns `simon@groundwork.dev` into a routing event.
  The turn still answers. It answers in the wrong voice, having silently deleted
  an email address out of the question before the embedder ever saw it.
- `parse_mentions` under-firing leaves `@feynman` in the text. The turn answers,
  the mention is embedded as noise, and the trace records a router decision that
  reads perfectly reasonable.
- `self_check_signal` over-firing discards a draft the user watched stream --
  and if it over-fires on a TEACHING answer it discards it for teaching, which
  is PRD open item 20 rebuilt inside the live turn.

So every case here names an OUTCOME. There is no exception to test for in any of
the three, which is exactly why the components needed a harness before they
needed a scenario.

**CASES 25 AND 26 ARE THE PAIR TO READ FIRST.** 25 is the EVAL run 3 answer that
faithfulness scored 0.571 for containing an analogy and a comprehension check;
26 is the same answer with its citations stripped, which MUST fire. Without 26,
case 25 would pass on a `self_check_signal` that had been deleted -- `loop.md`
section 5's rule that a test which cannot fail is worse than no test, applied to
the one case in this file whose failure would be invisible in production.

ASCII in print(), exit 1 on any failure -- `new features/06-test-plan.md`. The
specialist icons ARE emoji and are never printed; `ascii()` where one has to be
shown.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

# The import itself is case 13: `specialists.py` raises at module scope if two
# specialists claim one alias, so reaching the next line is the assertion.
from app.db.personas import PERSONA_TEMPLATES  # noqa: E402
from app.db.specialists import (  # noqa: E402
    BY_SLUG,
    DEFAULT_ROSTER,
    SPECIALISTS,
    Specialist,
    resolve,
    roster,
)
from app.rag.route import parse_mentions  # noqa: E402
from app.rag.selfcheck import (  # noqa: E402
    CRITIC_SYSTEM_PROMPT,
    MIN_SUBSTANTIVE_CHARS,
    SIGNAL_NO_CITATIONS,
    SIGNAL_PHANTOM,
    markers_in,
    self_check_signal,
)

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "[ok]  " if condition else "[FAIL]"
    print(f"{status} {name}" + (f" -- {detail}" if detail else ""))
    if not condition:
        failures.append(name)


def slugs_of(parse) -> list[str]:
    return [s.slug for s in parse.specialists]


ALL = SPECIALISTS  # the full roster, for the cases that are not about gating


print("=" * 74)
print("route.py + specialists.py + selfcheck.py -- layer 1")
print("=" * 74)

# ---------------------------------------------------------------------------
# 1-12. Mention parsing. Both directions, because the two failures are
#       symmetric in shape and opposite in cost -- see the module docstring.
# ---------------------------------------------------------------------------
print("\n-- mention parsing --")

p1 = parse_mentions("@feynman explain the Ka-band budget", ALL)
check(
    "1. one mention is extracted",
    slugs_of(p1) == ["feynman-explainer"],
    f"specialists={slugs_of(p1)} matched={list(p1.matched)}",
)
check(
    "2. ...and stripped from the text that reaches the embedder",
    p1.question == "explain the Ka-band budget",
    repr(p1.question),
)

p3 = parse_mentions("@feynman @polya how do I size the link?", ALL)
check(
    "3. two mentions extract two, in the order they were typed",
    slugs_of(p3) == ["feynman-explainer", "polya-coach"]
    and p3.question == "how do I size the link?",
    f"specialists={slugs_of(p3)} question={p3.question!r}",
)

# 4-6 are the over-firing guard, and 5 is the case this whole regex exists for.
RISK = "what is @risk in this design?"
p4 = parse_mentions(RISK, ALL)
check(
    "4. '@risk' is prose, not a route -- text byte-identical",
    not p4.specialists and p4.question == RISK,
    f"specialists={slugs_of(p4)} question={p4.question!r}",
)

# **THE ONE THAT MATTERS.** A mention is STRIPPED before the embedder, so a
# false positive here does not merely mis-route -- it silently deletes part of
# the user's question. An address is the likeliest `@` in ordinary text.
EMAIL = "mail the link budget to simon@groundwork.dev before Friday"
p5 = parse_mentions(EMAIL, ALL)
check(
    "5. an email address never routes, and is never edited",
    not p5.specialists and p5.question == EMAIL,
    f"specialists={slugs_of(p5)} question={p5.question!r}",
)

# The hostile version of 5, and **the one that actually bites.** Measured by
# replacing `_MENTION` with the naive `@{1,2}([A-Za-z][\w-]*)`: case 5 still
# passes, because `groundwork` is nobody's alias, while this one routes to TWO
# specialists and hands the embedder `"forward it to notes.example.com and to
# me.org"` -- mis-routed AND silently edited. Case 5 states the intent; this is
# the assertion that fails when the lookbehind goes.
EMAIL_ALIAS = "forward it to notes@quiz.example.com and to me@feynman.org"
p6 = parse_mentions(EMAIL_ALIAS, ALL)
check(
    "6. an email whose DOMAIN is an alias still never routes",
    not p6.specialists and p6.question == EMAIL_ALIAS,
    f"specialists={slugs_of(p6)} question={p6.question!r}",
)

p7 = parse_mentions("@@feynman explain the link budget", ALL)
check(
    "7. '@@' is accepted -- a doubled sigil is a habit, not a syntax error",
    slugs_of(p7) == ["feynman-explainer"] and p7.question == "explain the link budget",
    f"specialists={slugs_of(p7)} question={p7.question!r}",
)

# 8. A mention with nothing after it must still leave the turn something to
# retrieve. Embedding an empty string is a worse outcome than embedding the
# mention -- the mention is honoured either way.
p8 = parse_mentions("@feynman", ALL)
check(
    "8. a bare mention keeps the raw text, so retrieval has an input",
    slugs_of(p8) == ["feynman-explainer"] and p8.question == "@feynman",
    f"specialists={slugs_of(p8)} question={p8.question!r}",
)

p9 = parse_mentions("@feynman @explain @feynman what is the link margin?", ALL)
check(
    "9. duplicates and co-aliases dedupe to one specialist",
    slugs_of(p9) == ["feynman-explainer"] and len(p9.matched) == 3,
    f"specialists={slugs_of(p9)} matched={list(p9.matched)}",
)

p10 = parse_mentions("@FEYNMAN @Polya how do I work this out?", ALL)
check(
    "10. matching is case-insensitive",
    slugs_of(p10) == ["feynman-explainer", "polya-coach"],
    f"specialists={slugs_of(p10)}",
)

# 11. The roster is a permission boundary, not a convenience. An owner who
# disabled the Quiz Writer must not have one summoned by typing its name.
ONE = roster(["feynman-explainer"])
QUIZ_ME = "@quiz test me on the power allocation"
p11 = parse_mentions(QUIZ_ME, ONE)
check(
    "11. a specialist outside THIS agent's roster stays literal text",
    not p11.specialists and p11.question == QUIZ_ME,
    f"specialists={slugs_of(p11)} question={p11.question!r}",
)

# 12. The classic path. `agents.specialists IS NULL` is the whole off switch, so
# an empty roster must cost nothing and change nothing.
p12 = parse_mentions("@feynman explain the link budget", ())
check(
    "12. an empty roster is a no-op -- the classic path parses nothing",
    not p12.specialists and p12.question == "@feynman explain the link budget",
    f"specialists={slugs_of(p12)} question={p12.question!r}",
)

# ---------------------------------------------------------------------------
# 13-16. The alias table.
# ---------------------------------------------------------------------------
print("\n-- the alias table --")

# 13. `specialists.py` raises at import if two specialists claim one alias, so
# this file having got this far IS the assertion. Stated explicitly because a
# guard nobody names is a guard somebody deletes.
check(
    "13. the module imported, so no alias is claimed by two specialists",
    len(SPECIALISTS) == 5 and len(BY_SLUG) == 5,
    f"specialists={len(SPECIALISTS)} by_slug={len(BY_SLUG)}",
)

bad_alias = [
    (s.slug, alias)
    for s in SPECIALISTS
    for alias in s.aliases
    if resolve(alias) is not s
]
check(
    "14. every alias resolves to its own specialist",
    not bad_alias,
    f"aliases={sum(len(s.aliases) for s in SPECIALISTS)} broken={bad_alias}",
)

bad_slug = [s.slug for s in SPECIALISTS if resolve(s.slug) is not s]
check(
    "15. every slug resolves to itself",
    not bad_slug,
    f"broken={bad_slug}",
)

# 16. Returning None rather than guessing is what keeps `@risk` out of the
# routing path. A "closest match" here would make case 4 unfixable.
unknown = ["risk", "groundwork.dev", "", "   ", "explainer-2", "feyman"]
guessed = [t for t in unknown if resolve(t) is not None]
check(
    "16. an unknown token resolves to None rather than to a near miss",
    not guessed,
    f"guessed={guessed}",
)

# ---------------------------------------------------------------------------
# 17-24. The self-check trigger. Free, deterministic, and gated per specialist.
# ---------------------------------------------------------------------------
print("\n-- self_check_signal --")

PHANTOM_DRAFT = (
    "The Ka-band high-rate downlink is budgeted at 220 Mbps [1] and achieves an "
    "operational average of 164 Mbps [2]. The relay constellation is visible for "
    "74 percent of each orbit [7], which is what the shortfall comes from."
)

missed = [
    s.slug
    for s in SPECIALISTS
    if (self_check_signal(
        PHANTOM_DRAFT, ledger_size=3, expects_citations=s.expects_citations
    ) or None) is None
]
check(
    "17. a phantom marker fires for EVERY specialist, exemptions included",
    not missed,
    f"exempt_specialists={[s.slug for s in SPECIALISTS if not s.expects_citations]} "
    f"missed={missed}",
)

sig18 = self_check_signal(PHANTOM_DRAFT, ledger_size=3, expects_citations=False)
check(
    "18. ...and it names the offending marker, not just the failure",
    sig18 is not None
    and sig18.name == SIGNAL_PHANTOM
    and sig18.phantom_markers == (7,),
    f"signal={getattr(sig18, 'name', None)} phantom={getattr(sig18, 'phantom_markers', None)}",
)

CITED_DRAFT = (
    "The Ka-band high-rate downlink is budgeted at 220 Mbps [1] and achieves an "
    "operational average of 164 Mbps [2]. The S-band command and telemetry link "
    "runs at 2.1 Mbps downlink [3], and it is the link that must never fail [3]."
)
check(
    "19. an answer whose markers are all legal does not fire",
    self_check_signal(CITED_DRAFT, ledger_size=3, expects_citations=True) is None,
    f"markers={sorted(markers_in(CITED_DRAFT))} ledger=3",
)

# 20/21 are the gate, in both directions. The same text, two answers, on purpose
# -- the same asymmetry `refusal.py` draws between its two functions.
UNCITED_DRAFT = (
    "The communications allocation is 4.2 kW, split between the Ka-band "
    "transmitter, the S-band chain and the UHF proximity link. Because the "
    "Ka-band link only transmits for part of each orbit, the measured average "
    "draw is lower than the allocation, and the difference is what the battery "
    "store has to cover during a load shed."
)
sig20 = self_check_signal(UNCITED_DRAFT, ledger_size=3, expects_citations=True)
check(
    "20. a substantive answer that anchored nothing fires when citations are expected",
    sig20 is not None and sig20.name == SIGNAL_NO_CITATIONS,
    f"chars={len(UNCITED_DRAFT)} signal={getattr(sig20, 'name', None)}",
)
check(
    "21. ...and does NOT when the specialist is designed to cite nothing",
    self_check_signal(UNCITED_DRAFT, ledger_size=3, expects_citations=False) is None,
    "socratic-tutor, polya-coach and reflective-coach answer with questions",
)

# 22. "The material does not cover this" is a correct answer with nothing to
# cite. Grading it would repeat `refusal_pass = 0/2` -- a measurement penalising
# the behaviour the system exists to produce.
REFUSAL_DRAFT = (
    "The provided material does not state which modulation and coding scheme the "
    "Ka-band downlink uses. It describes the link budget, the operational average "
    "and the availability profile, and it says explicitly that the modulation and "
    "coding schemes are held in a separate document."
)
check(
    "22. a correct refusal does not fire, however long it is",
    self_check_signal(REFUSAL_DRAFT, ledger_size=3, expects_citations=True) is None,
    f"chars={len(REFUSAL_DRAFT)} (over the {MIN_SUBSTANTIVE_CHARS}-char floor)",
)

SHORT_DRAFT = "Which part of the link budget are you asking about?"
check(
    "23. an answer under the length floor does not fire",
    len(SHORT_DRAFT) < MIN_SUBSTANTIVE_CHARS
    and self_check_signal(SHORT_DRAFT, ledger_size=3, expects_citations=True) is None,
    f"chars={len(SHORT_DRAFT)} floor={MIN_SUBSTANTIVE_CHARS}",
)

check(
    "24. markers_in reads exactly what the answer cited",
    markers_in("a [1] b [12] c [1] d [999] e [] f [abc]") == {1, 12, 999},
    str(sorted(markers_in("a [1] b [12] c [1] d [999] e [] f [abc]"))),
)

# ---------------------------------------------------------------------------
# 25-26. THE CRITICAL CASE, and the proof that it is not passing for free.
# ---------------------------------------------------------------------------
print("\n-- the teaching answer (PRD open item 20) --")

# The shape of the real 0.571 answer from EVAL run 3: four cited factual
# sentences, then a labelled analogy, then a comprehension check.
#
# **PRD OPEN ITEM 20.** Ragas scored that answer 0.571 on faithfulness. Its
# sentences 1-4 were four correct figures straight from the context; the
# deductions were the analogy and "restate this in your own words", both
# unsupported by construction and both exactly what `feynman-explainer` exists
# to produce. The scorecard then named faithfulness as the weakest metric and
# advised tightening the grounding clause and reducing persona verbosity -- that
# is, deleting the pedagogy.
#
# A regression here is that instrument rebuilt INSIDE the live turn, where it
# does not merely recommend deleting the teaching: the draft is discarded and
# the model is told to write a duller one, on a turn the user watched stream.
# Nothing raises. The product just gets worse.
TEACHING_ANSWER = (
    "The Ka-band high-rate downlink is budgeted at 220 Mbps [1]. It achieves an "
    "operational average of 164 Mbps [1], because the relay constellation is "
    "visible for only 74 percent of each orbit [2]. Science instruments generate "
    "about 1.9 TB per day [2], and the link clears about 1.48 TB per day at that "
    "average [3].\n\n"
    "**Analogy:** the Ka-band link is like a high-speed motorway that is closed "
    "in several sections -- the posted speed limit is not what decides how much "
    "traffic gets through in a day.\n\n"
    "Please restate this idea in your own words to ensure you have understood it."
)

check(
    "25. the EVAL run 3 teaching answer does NOT fire the self-check",
    self_check_signal(TEACHING_ANSWER, ledger_size=3, expects_citations=True) is None,
    f"markers={sorted(markers_in(TEACHING_ANSWER))} ledger=3 "
    f"chars={len(TEACHING_ANSWER)} -- an analogy and a comprehension check are "
    "not ungrounded claims",
)

# 26. Case 25 on its own would pass over a `self_check_signal` that had been
# deleted, or one whose gate had been inverted -- `loop.md` section 5: a test
# that cannot fail is worse than no test, because it reports success. The same
# answer with its citations stripped is the control, and it MUST fire.
TEACHING_ANSWER_UNCITED = TEACHING_ANSWER.replace("[1]", "").replace(
    "[2]", ""
).replace("[3]", "")
sig26 = self_check_signal(
    TEACHING_ANSWER_UNCITED, ledger_size=3, expects_citations=True
)
check(
    "26. ...and the SAME answer with its citations stripped DOES fire",
    sig26 is not None and sig26.name == SIGNAL_NO_CITATIONS,
    f"signal={getattr(sig26, 'name', None)} -- this is what makes case 25 a finding",
)

# ---------------------------------------------------------------------------
# 27-31. The critic's carve-out. Structural, because the failure is a WORDING
#        edit and a wording edit raises nothing.
# ---------------------------------------------------------------------------
print("\n-- the critic prompt's carve-out --")

_critic = CRITIC_SYSTEM_PROMPT.lower()
for number, label, needle in (
    (27, "an analogy is exempt", "analogy"),
    (28, "a question put to the learner is exempt", "question put to the learner"),
    (29, "an instruction to the learner is exempt", "restate this in your own words"),
    (30, "'the material does not cover it' is exempt", "does not cover something"),
    # The half that must survive the carve-out: exempting pedagogy must not
    # exempt everything. A prompt that judges nothing strictly is a critic that
    # always says grounded, which is the same as no critic and costs a call.
    (31, "...and the strict half survives the exemptions", "unsupported unless"),
):
    check(f"{number}. {label}", needle in _critic, f"needle={needle!r}")

# ---------------------------------------------------------------------------
# 32-34. Roster gating.
# ---------------------------------------------------------------------------
print("\n-- roster gating --")

check(
    "32. roster(None) is empty -- NULL is the classic path, not 'all five'",
    roster(None) == (),
    f"got={[s.slug for s in roster(None)]}",
)
check(
    "33. roster([]) is empty too",
    roster([]) == (),
    f"got={[s.slug for s in roster([])]}",
)
# Registry order, not the caller's order: two mentions render as two sections,
# and a roster that reordered itself per request would make the section order a
# property of how the owner happened to type the column.
picked = roster(["quiz-generator", "feynman-explainer"])
check(
    "34. roster filters, and preserves REGISTRY order rather than argument order",
    [s.slug for s in picked] == ["feynman-explainer", "quiz-generator"],
    f"asked=['quiz-generator', 'feynman-explainer'] got={[s.slug for s in picked]}",
)

# ---------------------------------------------------------------------------
# 35-40. Specialist invariants.
# ---------------------------------------------------------------------------
print("\n-- specialist invariants --")

check(
    "35. every specialist has a non-empty when_to_use (the router sees only this)",
    all(s.when_to_use.strip() for s in SPECIALISTS),
    f"empty={[s.slug for s in SPECIALISTS if not s.when_to_use.strip()]}",
)

headings = [s.heading for s in SPECIALISTS]
check(
    "36. headings are distinct -- two mentions produce two '## ' sections",
    len(set(headings)) == len(headings),
    f"headings={headings}",
)

# `ascii()` rather than the glyph: these are emoji, and a Windows console
# codepage turns one into a UnicodeEncodeError several layers from this line.
icons = [s.icon for s in SPECIALISTS]
check(
    "37. icons are distinct -- the route pill must not read as the wrong persona",
    len(set(icons)) == len(icons),
    f"icons={ascii(''.join(icons))}",
)

# 38. **A drift here quietly retunes retrieval.** The routed specialist's
# `retrieve_k` / `rerank_top_n` are passed to `aretrieve` as overrides, so if
# they stop matching the seeded template the same persona retrieves one way when
# it is an agent and another way when it is routed to -- and every EVAL.md
# number was measured on the template's values.
seeded = {t["slug"]: t for t in PERSONA_TEMPLATES}
drift = [
    (
        s.slug,
        (s.retrieve_k, s.rerank_top_n),
        (seeded[s.slug]["retrieve_k"], seeded[s.slug]["rerank_top_n"]),
    )
    for s in SPECIALISTS
    if s.slug in seeded
    and (s.retrieve_k, s.rerank_top_n)
    != (seeded[s.slug]["retrieve_k"], seeded[s.slug]["rerank_top_n"])
]
check(
    "38. retrieve_k / rerank_top_n match the seeded persona templates",
    not drift and len(seeded) >= len(SPECIALISTS),
    f"personas={len(seeded)} specialists={len(SPECIALISTS)} drift={drift}",
)

# 39. The prompts are IMPORTED, never re-typed. An identity check rather than an
# equality one: a copy that happens to be equal today is a copy that drifts the
# first time either is edited, and nothing would report it.
prompt_copies = [
    s.slug
    for s in SPECIALISTS
    if s.slug in seeded and s.system_prompt is not seeded[s.slug]["system_prompt"]
]
check(
    "39. each specialist holds the SAME prompt object as its template, not a copy",
    not prompt_copies,
    f"copied={prompt_copies}",
)

check(
    "40. DEFAULT_ROSTER is the whole registry, in registry order",
    DEFAULT_ROSTER == [s.slug for s in SPECIALISTS]
    and all(isinstance(BY_SLUG[slug], Specialist) for slug in DEFAULT_ROSTER),
    f"default_roster={DEFAULT_ROSTER}",
)

print("\n" + "=" * 74)
if failures:
    print(f"{len(failures)} FAILED:")
    for name in failures:
        print(f"  - {name}")
    sys.exit(1)
print("all checks passed")
