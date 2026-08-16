"""Layer 1.5 harness for `app/eval/generate.py`. Network, no DB, no golden rows.

WHY THIS FILE EXISTS.

`golden_set_model` moved from `google/gemini-3.7-flash` to `minimax/minimax-m3`
on 2026-08-16, to buy judge independence: Flash is also `ragas_judge_model`, so
while it drafted the set the judge was grading against reference answers it had
written itself. The swap costs one measured regression, and it is the silent
kind. Pooled over 8 runs on the same corpus and prompt:

                                 MiniMax M3    Flash
    reference answer, median        24 chars   95 chars
    references under 20 chars            43%         0%
    shortest seen               '14 knots', '31 hours'

A reference answer of '14 knots' is not wrong. It validates, it persists, it
renders in the editor, and `LLMContextRecall` -- which decomposes that field
into claims and attributes each to the retrieved contexts -- gets nothing to
decompose. Two of the four metrics read this field. So the failure shape is the
one this repo keeps meeting: the scorecard still prints a number, and the number
is about nothing. `config.py` rejected Gemma as the drafting model for exactly
this ("Nineteen", 8 characters); the mitigation is the `reference_answer` field
description in `generate.py`, and a mitigation that lives in a prompt string can
be undone by an edit that raises nothing.

WHAT THIS ASSERTS, AND WHY IT IS A DISTRIBUTION RATHER THAN A PASS/FAIL.

`new features/loop.md`: trigger on the ABSENCE of the outcome you wanted, never
on the presence of an error. The wanted outcome here is "reference answers that
decompose into claims", so the assertion is a length distribution over several
runs -- never "did it parse" (48/48 parsed while the median sat at 24) and never
"did it raise" (nothing raises). The numbers are printed in full, because the
gate is a floor and the interesting question when it is close is which way the
population is moving.

Cases 4 and 5 guard the other half of the swap: MiniMax has reasoning
default-ON with `mandatory=false`, measured at 93-99.98% of completion tokens on
this prompt, and three consecutive calls at production counts came back
`finish_reason=length` against MAX_OUTPUT_TOKENS. A truncated golden set is a
short golden set, which looks like a thin corpus.

WHAT IT FOUND ON THE WAY IN, 2026-08-16, both of them intermittent.

**The field description alone was not enough.** It took the median from 24 to 68
characters and still left 34% of references under 20 -- better, and still
failing. The rule had to be in `SUGGEST_SYSTEM_PROMPT` as well before the median
reached 132-157 with zero short references across five samples. A schema field
description is read as documentation about a field; the system prompt is read as
the job.

**And the first version of that system-prompt rule cost refusal probes.** Ninety
words of rationale added above the refusal section -- the longest and
most-emphasised part of that prompt -- and 3 of 12 populated runs came back with
no refusal question at all. Cut to a worked example and one clause, the length
result held and the rate fell to 1 in 16. Prompt real estate is zero-sum, and
case 3 is what says so out loud.

**MiniMax returns a well-formed EMPTY set in about one call in six** (5 of 30,
`finish_reason=stop`, 46-386 output tokens, no error). That is a property of the
model, present before any of this work; `suggest_golden_questions` already turns
it into `GoldenSetSuggestionError`. Case 1b measures the rate rather than
failing on any single occurrence, for the reason recorded at MAX_EMPTY_SHARE.

    backend/.venv/Scripts/python.exe scripts/goldenset_check.py
    backend/.venv/Scripts/python.exe scripts/goldenset_check.py --runs 3

Costs `--runs` real OpenRouter calls (5 by default, ~10 s each with reasoning
off). Touches no database, creates no `golden_questions` rows, and reads no
agent -- the corpus is the inline fixture below, so this is safe to run against
production credentials.

KNOWN LIMIT, recorded rather than hidden: the fixture is ~2.6 kB against
`MAX_CONTEXT_CHARS = 60000`, i.e. more than twenty times smaller than a real
agent corpus. Truncation pressure gets WORSE with a real one, not better, so a
green case 5 here is necessary and not sufficient -- re-run the suggestion
against a real agent before trusting a production golden set.

ASCII in print(), exit 1 on any failure -- `new features/06-test-plan.md`.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from langchain_core.callbacks.base import BaseCallbackHandler  # noqa: E402
from langchain_core.outputs import LLMResult  # noqa: E402

from app.config import settings  # noqa: E402
from app.eval.generate import (  # noqa: E402
    MAX_OUTPUT_TOKENS,
    PASSAGE_SEPARATOR,
    STRUCTURED_OUTPUT_METHOD,
    SUGGEST_SYSTEM_PROMPT,
    SuggestedGoldenSet,
    SuggestedQuestion,
    _accept,
    _get_suggester,
)
from app.rag.llm import openrouter_slug  # noqa: E402

# --------------------------------------------------------------------------
# Thresholds
# --------------------------------------------------------------------------

# **The gate, and the two populations it sits between.** Measured 2026-08-16,
# pooled over 8 runs each on one corpus with one prompt:
#
#   google/gemini-3.7-flash                        median 95 chars,  0% under 20
#   minimax/minimax-m3, old field description      median 24 chars, 43% under 20
#
# 40 characters is above the ENTIRE broken population's median and comfortably
# below the working one's, so it separates them rather than sitting inside
# either -- the mistake `score_threshold` records for retrieval, where 0.5 lands
# in the overlap of the on-topic and off-topic bands and therefore decides
# nothing. A reference at 40 characters is a short sentence naming a subject and
# a value, which is the smallest thing `LLMContextRecall` can decompose into
# more than zero claims.
#
# If a future model or a reworded field description cannot clear these, the
# honest response is to record the number and reconsider the model. Lowering the
# threshold to make the harness green deletes the only measurement that made the
# swap decidable.
#
# **RAISED 40 -> 60 after the first green run, on the arithmetic rather than on
# taste.** 40 was chosen before the mitigation was measured. With it in place the
# median came in at 119 / 144 / 144 / 150 / 157 over five independent samples, so
# a gate at 40 sat roughly a hundred characters below the floor of the working
# population and would have gone green on a model less than half as good. 60 is
# still comfortably under the observed floor and still well clear of the broken
# populations it has to separate -- 24 for MiniMax before the fix, and 68 for the
# intermediate state where only the field description was strengthened and the
# system prompt was not. That intermediate number is the one that matters: it is
# a REAL configuration this repo passed through, it FAILED the short-share gate,
# and a median gate of 40 would have waved it through.
MEDIAN_MIN_CHARS = 60

# A reference this short is a bare figure or a bare noun phrase. '14 knots' is
# 8, '31 hours' is 8, 'Nineteen' is 8 -- all three are real, and all three
# decompose into no attributable claim.
SHORT_REFERENCE_CHARS = 20

# Not zero, and not one-in-ten either. The argument for a nonzero tolerance is
# real: a single terse reference in a ten-question set is a drafting wobble a
# human editor fixes in one keystroke, whereas 43% is a systematic defect in what
# the model thinks the field is for, and a gate that flakes gets re-run instead of
# read. The argument against 0.10 is arithmetic: at the n=40 this harness actually
# samples, one-in-ten admits FOUR bare-figure references, and four is not a
# wobble -- it is a quarter of the way back to the defect the gate exists to
# catch, passing green.
#
# 0.05 comes off the measurement rather than off a feeling. Zero references under
# 20 chars were observed in 168 draws with the mitigation in place; the 95%
# binomial upper bound on a true rate given 0/168 is about 1.8%, so 5% sits
# roughly three times above the plausible real rate -- far enough out to absorb a
# genuine wobble -- while allowing at most TWO short references at n=40 instead of
# four. Tighten it toward zero as the sample grows, never loosen it.
MAX_SHORT_SHARE = 0.05

# **The empty-set rate, which is a MEASUREMENT rather than a pass/fail because
# the defect is intermittent and not ours.** `minimax/minimax-m3` returns a
# perfectly well-formed `SuggestedGoldenSet` with `questions: []` in a minority
# of calls -- 46 to 386 output tokens, `finish_reason=stop`, no error anywhere.
# Measured 2026-08-16 over 6 independent 5-run samples on this fixture: **5 empty
# in 30 runs, 17%**, present before and after the prompt work and therefore a
# property of the model rather than of anything in `generate.py`.
#
# `suggest_golden_questions` already turns this into
# `GoldenSetSuggestionError("well-formed but empty set")`, so production shows an
# error rather than an empty golden set. What it means operationally is that
# roughly one in six presses of Suggest questions fails and wants a retry.
#
# The gate is 40% and not 0%, deliberately. At 17% a strict 5/5 rule goes red on
# two thirds of runs, and CLAUDE.md's own lesson from the `[rate]` rows is that a
# suite which goes red because a provider said no teaches its reader to ignore
# red. 40% is more than twice the measured rate, so ordinary variance does not
# flip it and a model or prompt change that doubles the rate does. **Do not raise
# it to accommodate a worse model** -- the number to change then is
# `golden_set_model`.
MAX_EMPTY_SHARE = 0.40

# **The zero-refusal rate, gated the same way and for the same reason.** A
# populated set with no refusal probe is the failure
# `suggest_golden_questions` names where it caps the two buckets independently:
# it "always scores well and never catches ungrounded answering". It is worth
# being red about, and it is intermittent, so it is a rate.
#
# Measured 2026-08-16 on this fixture, counting POPULATED runs only:
#
#   system-prompt rule at ~90 words   3 of 12 runs with no refusal probe   25%
#   same rule cut to one clause       1 of 16                              6%
#
# Every occurrence looked the same from outside: the model overproduced
# answerable questions (10, 15, once 36 against the 8 asked for) and simply
# never got to the refusal section. So the tell is overproduction, and the first
# place to look is what was most recently added ABOVE that section.
#
# The gate is 20%: below the 25% the long prompt produced, so a regression of
# that size fails, and above the 6% the current one produces, so ordinary
# variance does not. It is a share rather than a count because a count large
# enough not to flake is too large to detect the regression -- at these rates,
# "at most one bad run in five" would miss the long-prompt state nearly two
# thirds of the time.
#
# A red here still deserves a second run before it is believed: with five runs
# this flags roughly one invocation in ten while nothing is wrong, which is the
# price of a gate tight enough to see 25%. Read the per-run counts printed
# beside it, not just the verdict.
MAX_NO_REFUSAL_SHARE = 0.20

# The sample below which case 3b reports instead of asserting. A rate gate needs
# enough draws to tell its two populations apart, and 5 cannot: with the current
# prompt's measured 6% it goes red about 3% of the time by luck, while with the
# 25% regression it is meant to catch it still passes about 37% of the time. 12
# is the smallest round n where the regression is caught more often than not and
# a false red is rare -- and it is what the 3-of-12 measurement above was taken
# at, so it is the sample this gate's own numbers came from.
#
# Not raised to 12 as the DEFAULT `--runs`, deliberately: every run is a real
# OpenRouter call at roughly ten seconds, and a layer-1.5 harness someone
# hesitates to run is worse than one that reports an honest "unmeasured".
MIN_RATE_SAMPLE = 12

# Production counts, from `suggest_golden_questions`' defaults (count=10,
# refusal_count=2). The truncation measured on this swap was at these counts and
# not at smaller ones, so a harness that asked for four questions would pass
# while production failed.
ANSWER_COUNT = 8
REFUSAL_COUNT = 2

DEFAULT_RUNS = 5

# --------------------------------------------------------------------------
# The fixture corpus
# --------------------------------------------------------------------------

AGENT_NAME = "Golden Set Check"

# **Every passage plants a gap that is RAISED and not COMPLETED**, because PRD
# 3.6.1 calls that "the single largest determinant of whether the set measures
# anything": a refusal probe about something the corpus never mentions is
# refused by every model on earth and therefore tests nothing. The planted ones,
# pinned here so a future edit to the fixture does not quietly remove them:
#
#   1. the coolant is described by its properties, never named
#   2. the Tier 2 load shed is referred to and its procedure is "in the
#      operations manual", i.e. not here
#   3. the radiator boom has "a documented single-point failure" and the
#      document does not say which mechanism
#   4. pass scheduling conflicts are resolved "by the operations centre" under
#      rules that are not given
#   5. the two ground stations are compared on availability but never on cost,
#      which the text sets up and does not deliver
#
# The numbers are dense on purpose: a value-shaped question is exactly where a
# reference answer collapses to '14 knots', so a fixture without numbers would
# not exercise the regression this harness exists for.
FIXTURE: list[tuple[str, int, str]] = [
    (
        "thermal-control.md",
        0,
        """## Heat rejection

The platform rejects waste heat through two independent single-phase coolant
loops feeding a shared radiator boom. Loop A serves the pressurised modules and
is sized for 22.4 kW; Loop B serves the payload bay and is sized for 17.9 kW.
The combined design rejection capacity is therefore 40.3 kW, against a measured
average thermal load of 33.1 kW.

The coolant was selected for its freezing point rather than its heat capacity,
because a loop that freezes in eclipse cannot be recovered on orbit. That choice
costs roughly 8% in pumping power against the alternative.

Radiator effectiveness falls with beta angle. Above 60 degrees the boom sees the
sun for the whole orbit and rejection capacity drops to 31.6 kW, which is below
the average load, so high-beta periods are managed by deferring payload
operations rather than by shedding pressurised-module cooling.""",
    ),
    (
        "thermal-control.md",
        3,
        """## Degradation and contingencies

Radiator surface degradation from atomic oxygen and micrometeoroid pitting is
measured at 0.9% of rejection capacity per year. After the fifteen-year service
life the end-of-life rejection capacity is 35.1 kW, still above the average load
but below the 40.3 kW nominal.

Loop A and Loop B can be cross-connected through a single valve assembly, which
lets either loop carry a reduced version of both duties at 26.8 kW total. The
cross-connect is a Tier 2 load shed action and is not commanded autonomously;
the procedure is held in the operations manual.

The radiator boom deployment mechanism carries one documented single-point
failure, and it is the reason the boom is deployed during the commissioning
window while a visiting vehicle is still attached.""",
    ),
    (
        "ground-segment.md",
        1,
        """## Station coverage

Two ground stations carry the S-band command and telemetry link. The northern
station supports 9 passes per day at an average of 11.4 minutes each; the
southern station supports 6 passes per day at an average of 8.2 minutes each.
Combined, that is 15 passes and 151 minutes of contact per day.

Northern station availability is 99.1% and southern station availability is
97.4%, the difference being weather outages at Ka-band frequencies that also
affect the co-located S-band feed. The southern station is the cheaper of the
two to operate, which is why it is retained despite the lower availability.

When both stations are visible in the same window the pass is assigned to one of
them by the mission operations centre. Commands are never uplinked through both
stations simultaneously, because the command counter is not shared between
them.""",
    ),
]


def render_fixture() -> str:
    """The passages, labelled exactly as `generate._render_passage` labels them."""
    return PASSAGE_SEPARATOR.join(
        f"[{filename} #{index}]\n{text.strip()}" for filename, index, text in FIXTURE
    )


# --------------------------------------------------------------------------
# Console
# --------------------------------------------------------------------------

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "[ok]  " if condition else "[FAIL]"
    print(f"{status} {name}" + (f" -- {detail}" if detail else ""))
    if not condition:
        failures.append(name)


def ascii_safe(text: str) -> str:
    """Force text to ASCII for the Windows console.

    Not optional here. Everything printed below the fixture line came out of a
    model: MiniMax emits typographic dashes and quotes freely, and CLAUDE.md
    records this exact crash breaking three throwaway scripts. A harness that
    dies on a UnicodeEncodeError while printing its own evidence reads as a
    broken harness.
    """
    return text.encode("ascii", "replace").decode("ascii")


def rule(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


# --------------------------------------------------------------------------
# Usage probe
# --------------------------------------------------------------------------

class UsageProbe(BaseCallbackHandler):
    """Reads token usage and `finish_reason` off the real chain.

    A callback rather than a parallel `include_raw=True` construction, because a
    second construction is a second set of parameters that can drift from
    `_get_suggester`'s -- and what case 4 is trying to prove is precisely that
    the flag `_get_suggester` passes reached the wire. Measuring a model this
    script built itself would prove nothing about the one production uses.
    """

    def __init__(self) -> None:
        self.usages: list[dict[str, Any]] = []
        self.finish_reasons: list[str | None] = []

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        for generations in response.generations:
            for generation in generations:
                message = getattr(generation, "message", None)
                if message is None:
                    continue
                self.usages.append(dict(getattr(message, "usage_metadata", None) or {}))
                metadata = getattr(message, "response_metadata", None) or {}
                self.finish_reasons.append(metadata.get("finish_reason"))

    @property
    def reasoning_tokens(self) -> int | None:
        """Reasoning tokens summed, or None when the provider reported none.

        None and 0 are different facts and are kept apart: 0 is "the provider
        counted and it was zero", None is "the provider did not report the
        field". Only the first is evidence that `reasoning=False` was honoured.
        """
        reported = [
            usage.get("output_token_details", {}).get("reasoning")
            for usage in self.usages
        ]
        counted = [value for value in reported if value is not None]
        return sum(counted) if counted else None

    @property
    def output_tokens(self) -> int:
        return sum(int(usage.get("output_tokens") or 0) for usage in self.usages)


# --------------------------------------------------------------------------
# Finding the chat model inside the chain
# --------------------------------------------------------------------------

def find_chat_model(runnable: Any, depth: int = 0) -> Any | None:
    """The `ChatOpenAI` buried in `prompt | model.with_structured_output(...)`.

    `with_structured_output` wraps the model in a `RunnableBinding` and then in
    a sequence with a parser, so the model is two or three layers down and the
    exact shape is a langchain internal. Walking the documented attributes
    (`steps`, `bound`, `first`, `middle`, `last`) rather than indexing a
    position keeps this working across a minor version bump; failing to find it
    is reported as a failure rather than skipped, because a silent skip here
    would turn case 0b into a check that can only pass.
    """
    if depth > 6 or runnable is None:
        return None
    if hasattr(runnable, "extra_body") and hasattr(runnable, "model_name"):
        return runnable
    for attribute in ("bound", "first", "last"):
        found = find_chat_model(getattr(runnable, attribute, None), depth + 1)
        if found is not None:
            return found
    for attribute in ("steps", "steps__", "middle"):
        children = getattr(runnable, attribute, None)
        if isinstance(children, dict):
            children = list(children.values())
        if isinstance(children, (list, tuple)):
            for child in children:
                found = find_chat_model(child, depth + 1)
                if found is not None:
                    return found
    return None


# --------------------------------------------------------------------------
# One run
# --------------------------------------------------------------------------

class RunResult:
    def __init__(self, index: int) -> None:
        self.index = index
        self.parsed: SuggestedGoldenSet | None = None
        self.error: str | None = None
        self.seconds: float = 0.0
        self.probe = UsageProbe()

    @property
    def ok(self) -> bool:
        return isinstance(self.parsed, SuggestedGoldenSet)


def persistable(parsed: SuggestedGoldenSet) -> list[tuple[SuggestedQuestion, str | None]]:
    """What `suggest_golden_questions` would actually keep, in its own order.

    The raw reply is not the measurable population. MiniMax routinely
    overproduces answerable questions (asked for 8, returned 10-18) and
    `suggest_golden_questions` caps each bucket independently and drops the
    surplus, so measuring the raw list would grade text no user will ever see.
    This mirrors that split with the real `_accept`, so the distribution below
    is the distribution of what reaches the database.
    """
    answerable: list[tuple[SuggestedQuestion, str | None]] = []
    refusals: list[tuple[SuggestedQuestion, str | None]] = []
    seen: list[frozenset[str]] = []
    for suggestion in parsed.questions:
        accepted = _accept(suggestion, seen)
        if accepted is None:
            continue
        _, reference = accepted
        refusing = suggestion.expected_behaviour == "refuse"
        bucket = refusals if refusing else answerable
        limit = REFUSAL_COUNT if refusing else ANSWER_COUNT
        if len(bucket) >= limit:
            continue
        bucket.append((suggestion, reference))
    return answerable + refusals


async def one_run(index: int, passages: str) -> RunResult:
    result = RunResult(index)
    started = time.perf_counter()
    try:
        result.parsed = await _get_suggester().ainvoke(
            {
                "answer_count": ANSWER_COUNT,
                "refusal_count": REFUSAL_COUNT,
                "agent_name": AGENT_NAME,
                "filenames": "thermal-control.md, ground-segment.md",
                "passages": passages,
            },
            config={"callbacks": [result.probe]},
        )
    except Exception as exc:  # noqa: BLE001 - recorded, then asserted on
        result.error = f"{type(exc).__name__}: {exc}"
    result.seconds = time.perf_counter() - started
    return result


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

async def main(runs: int) -> None:
    passages = render_fixture()

    rule("golden set -- configuration")
    print(f"  golden_set_model  : {settings.golden_set_model}")
    print(f"  resolved slug     : {openrouter_slug(settings.golden_set_model)}")
    print(f"  structured output : {STRUCTURED_OUTPUT_METHOD}")
    print(f"  max output tokens : {MAX_OUTPUT_TOKENS}")
    print(f"  fixture           : {len(passages)} chars, {len(FIXTURE)} passages")
    print(f"  runs              : {runs} at {ANSWER_COUNT}+{REFUSAL_COUNT}")

    # -- 0. The mitigation, checked without the network -----------------------
    # Both of these are structural: they fire if someone reverts the field
    # description or drops the reasoning flag, WITHOUT waiting for a model to
    # misbehave. A prompt string is code that nothing type-checks.
    rule("0. the mitigations are present in the code")
    description = SuggestedQuestion.model_fields["reference_answer"].description or ""
    check(
        "0a. reference_answer demands a complete sentence, with a worked example",
        "COMPLETE SENTENCE" in description and "14 knots" in description,
        f"{len(description)} chars",
    )
    # **The system prompt carries the same rule, and it is the half that moved
    # the number.** Measured 2026-08-16 on this fixture, 5 runs each: the field
    # description alone took the median from 24 to 68 chars and left 11/32
    # references under 20 (34%, against 43% before it) -- an improvement that
    # still fails the gate. Adding the rule to SUGGEST_SYSTEM_PROMPT as well
    # took it to a median of 132-157 and 0/168 short across five samples. A
    # schema field description is read as documentation for a field; the system
    # prompt is read as the job. Reverting either is a silent regression, so
    # both are asserted.
    check(
        "0b. SUGGEST_SYSTEM_PROMPT carries the same rule, with its own example",
        "COMPLETE SENTENCE" in SUGGEST_SYSTEM_PROMPT
        and "40.3 kW" in SUGGEST_SYSTEM_PROMPT,
        f"{len(SUGGEST_SYSTEM_PROMPT)} chars",
    )
    model = find_chat_model(_get_suggester())
    extra_body = getattr(model, "extra_body", None) or {}
    check(
        "0c. the chain's model carries reasoning: {'enabled': False}",
        extra_body.get("reasoning") == {"enabled": False},
        f"extra_body keys {sorted(extra_body)}" if model is not None
        else "no ChatOpenAI found in the chain",
    )

    # -- the runs ------------------------------------------------------------
    rule(f"generating {runs} golden sets (network)")
    results: list[RunResult] = []
    for index in range(1, runs + 1):
        result = await one_run(index, passages)
        results.append(result)
        if result.ok:
            kept = persistable(result.parsed)
            refusals = sum(1 for s, _ in kept if s.expected_behaviour == "refuse")
            print(
                f"  run {index}: {result.seconds:5.1f}s  "
                f"raw {len(result.parsed.questions):2d} questions  "
                f"kept {len(kept):2d} ({refusals} refusal)  "
                f"output {result.probe.output_tokens} tok "
                f"(reasoning {result.probe.reasoning_tokens})  "
                f"finish {result.probe.finish_reasons}"
            )
        else:
            print(f"  run {index}: {result.seconds:5.1f}s  FAILED -- {ascii_safe(result.error or '')}")

    # -- 1. every run parses --------------------------------------------------
    rule("1. every run parses to SuggestedGoldenSet")
    # Parsed and POPULATED are two different failures and are asserted
    # differently, because only one of them is under this repo's control.
    # Parsing is strict: a `None`, a wrong type or a transport error is a
    # configuration fault and must never be tolerated. Emptiness is a rate, for
    # the reasons at MAX_EMPTY_SHARE. Counting them as one check would double
    # count a single bad run and, worse, would let a real parse regression hide
    # inside a tolerance that exists for something else entirely.
    parsed_ok = sum(1 for result in results if result.ok)
    populated = [result for result in results if result.ok and result.parsed.questions]
    empty_share = (runs - len(populated)) / runs
    check(
        "1a. every run parsed to a SuggestedGoldenSet (never None, never a fault)",
        parsed_ok == runs,
        f"{parsed_ok}/{runs} parsed",
    )
    check(
        f"1b. well-formed-but-empty rate is at most {MAX_EMPTY_SHARE * 100:.0f}%",
        empty_share <= MAX_EMPTY_SHARE,
        f"{runs - len(populated)}/{runs} empty = {empty_share * 100:.0f}% "
        f"(measured baseline 5/30 = 17%)",
    )
    usable = populated
    if not usable:
        rule("nothing to measure")
        print("Every run failed or came back empty, so there is no distribution to")
        print("report. That is a routing, credential or model-availability failure,")
        print("NOT a finding about reference-answer length. Do not read it as one.")
        finish("aborted before the distribution could be measured")

    # -- 2. THE ONE ASSERTION: the reference-length distribution --------------
    rule("2. reference-answer length (THE ONE ASSERTION)")
    # Measured over the ANSWERABLE references only, and that is deliberate. A
    # refusal reference is "the passages do not state X", a full sentence by
    # construction and long whatever the model does, so pooling the two inflates
    # the median and hides the regression in the half that carries it. It is
    # also the half the metrics read: EVAL.md scores refusal rows separately as
    # pass/fail on `behaviour_ok` and excludes them from the metric means, so
    # `LLMContextRecall` never sees a refusal reference at all.
    answerable_lengths: list[int] = []
    refusal_lengths: list[int] = []
    shortest: list[tuple[int, str]] = []
    for result in usable:
        for suggestion, reference in persistable(result.parsed):
            length = len(reference or "")
            if suggestion.expected_behaviour == "refuse":
                refusal_lengths.append(length)
            else:
                answerable_lengths.append(length)
                shortest.append((length, reference or ""))

    shortest.sort(key=lambda item: item[0])
    median = statistics.median(answerable_lengths) if answerable_lengths else 0
    short_count = sum(1 for n in answerable_lengths if n < SHORT_REFERENCE_CHARS)
    short_share = short_count / len(answerable_lengths) if answerable_lengths else 1.0
    pooled = answerable_lengths + refusal_lengths
    pooled_median = statistics.median(pooled) if pooled else 0

    print(f"  answerable references : n={len(answerable_lengths)}")
    print(f"    median              : {median:.0f} chars   (gate >= {MEDIAN_MIN_CHARS})")
    print(f"    mean                : {statistics.fmean(answerable_lengths):.0f} chars"
          if answerable_lengths else "    mean                : n/a")
    print(f"    min / max           : {min(answerable_lengths)} / {max(answerable_lengths)}"
          if answerable_lengths else "    min / max           : n/a")
    print(f"    under {SHORT_REFERENCE_CHARS} chars       : {short_count}/{len(answerable_lengths)}"
          f" = {short_share * 100:.0f}%   (gate <= {MAX_SHORT_SHARE * 100:.0f}%)")
    print(f"  refusal references    : n={len(refusal_lengths)}, "
          f"median {statistics.median(refusal_lengths):.0f}"
          if refusal_lengths else "  refusal references    : n=0")
    print(f"  pooled median         : {pooled_median:.0f} chars "
          f"(reported for comparison with the 24 / 95 baselines; NOT the gate)")
    print("  the five shortest answerable references, verbatim:")
    for length, reference in shortest[:5]:
        print(f"    {length:3d}  {ascii_safe(reference)}")

    check(
        f"2a. median answerable reference >= {MEDIAN_MIN_CHARS} chars",
        median >= MEDIAN_MIN_CHARS,
        f"median {median:.0f} over {len(answerable_lengths)} references "
        f"(MiniMax before the fix: 24; Flash: 95)",
    )
    check(
        f"2b. under {SHORT_REFERENCE_CHARS} chars is at most {MAX_SHORT_SHARE * 100:.0f}%",
        short_share <= MAX_SHORT_SHARE,
        f"{short_count}/{len(answerable_lengths)} = {short_share * 100:.0f}% "
        f"(MiniMax before the fix: 43%; Flash: 0%)",
    )

    # -- 3. every run yields a refusal probe ----------------------------------
    rule("3. every populated run yields at least one refusal question")
    # A set with no refusal probe "always scores well and never catches
    # ungrounded answering" -- `suggest_golden_questions` says so where it caps
    # the two buckets independently.
    #
    # **Populated runs only.** An empty set has no refusal probe by arithmetic,
    # and letting that turn this case red as well would report one bad run as
    # two failures; case 1b owns emptiness.
    #
    # This case earned its keep during the swap, and what it caught was a PROMPT
    # LENGTH effect rather than a model one. The reference-answer rule added to
    # SUGGEST_SYSTEM_PROMPT's answerable section was first written as ~90 words
    # of rationale, and the refusal section that follows it -- the longest part
    # of that prompt, and labelled the most important -- started losing:
    # **3 of 12 populated runs came back with zero refusal probes**, having
    # overproduced answerable ones instead. Cut to the worked example and one
    # clause, the same rule held the reference length (median 119-157, 0% short)
    # and the rate fell to **1 of 16**. See MAX_NO_REFUSAL_SHARE for the gate.
    #
    # It is not zero, and the residue is worth knowing: the surviving failure
    # came in a run that returned **36 questions against the 8 asked for** and
    # never reached the refusal section at all. If this goes red, read the raw
    # question count on that run first, then what was most recently added ABOVE
    # the refusal section -- and only then suspect the model.

    per_run_refusals = [
        sum(
            1
            for suggestion, _ in persistable(result.parsed)
            if suggestion.expected_behaviour == "refuse"
        )
        for result in usable
    ]
    # Two checks, because "the refusal section stopped working" and "one run in
    # sixteen wanders off" are different failures with different responses, and
    # a single per-run assertion reports the second with the alarm the first
    # deserves. 3a can only go red if EVERY run missed, which is not variance.
    without = sum(1 for count in per_run_refusals if count == 0)
    no_refusal_share = without / len(per_run_refusals) if per_run_refusals else 1.0
    check(
        "3a. the refusal section still works at all (some run produced a probe)",
        any(count >= 1 for count in per_run_refusals),
        f"per populated run: {per_run_refusals} (asked for {REFUSAL_COUNT} each)",
    )
    # **3b IS A RATE, AND A RATE NEEDS A SAMPLE THAT CAN RESOLVE IT.** This case
    # went red on a default 5-run invocation and green on the very next one with
    # no code change between, which is the shape `new features/loop.md` warns
    # about most plainly: a red that means "re-run me" teaches its reader to
    # ignore red, and the next real regression is read as variance.
    #
    # The arithmetic says the gate was being asked a question the sample cannot
    # answer. The measured true rate is 1 in 16 (~6%) with the current prompt and
    # 3 in 12 (25%) with the long one -- the regression this case exists to
    # catch. At n=5, a 20% gate fails the moment TWO runs miss, and with a true
    # rate of 6% that happens about 3% of the time, while with the 25% regression
    # in place a 5-run sample still passes about 37% of the time. So at n=5 the
    # case is simultaneously flaky AND unable to catch what it is for.
    #
    # Below MIN_RATE_SAMPLE it therefore REPORTS rather than asserts, and says
    # "unmeasured" in as many words -- the same distinction EVAL.md draws between
    # a metric that scored badly and one that never ran. Run with `--runs 12` to
    # arm it. Widening the threshold instead would have kept a green harness that
    # could not see the 25% case at all.
    if len(per_run_refusals) >= MIN_RATE_SAMPLE:
        check(
            f"3b. runs with no refusal probe at most {MAX_NO_REFUSAL_SHARE * 100:.0f}%",
            no_refusal_share <= MAX_NO_REFUSAL_SHARE,
            f"{without}/{len(per_run_refusals)} = {no_refusal_share * 100:.0f}% "
            f"(long system prompt: 25%; current: 6%)",
        )
    else:
        print(
            f"[----]  3b. no-refusal rate UNMEASURED at n={len(per_run_refusals)} "
            f"-- observed {without}/{len(per_run_refusals)}, need n>={MIN_RATE_SAMPLE} "
            f"to assert (re-run with --runs {MIN_RATE_SAMPLE})"
        )

    # -- 4. reasoning reached the wire ----------------------------------------
    rule("4. reasoning tokens are zero")
    # **Evidence is required, not merely an absence of contrary evidence.** This
    # read `all(value == 0 for value in counted) if counted else True` -- green
    # when `counted` was EMPTY, i.e. when no provider reported a reasoning count
    # at all and therefore nothing had been measured. That is the mirror image of
    # `new features/loop.md` T2 landing in the harness: passing on the absence of
    # the evidence rather than on the presence of the wanted outcome. The failure
    # it would have waved through is the expensive one -- `reasoning=False`
    # silently dropped, 93-99.98% of completion tokens spent thinking, and
    # `finish_reason=length` truncating the set -- while the run still printed
    # `[ok] 4. no run spent tokens thinking`.
    #
    # Case 0c is the structural half and stays: it proves the flag is on the
    # model this chain builds. This case is the wire half, and a wire check with
    # no wire reading is unmeasured, not passed. If it goes red on "no usage
    # reported", suspect the provider's usage accounting before suspecting the
    # flag -- and read 0c to tell the two apart.
    reported = [result.probe.reasoning_tokens for result in usable]
    counted = [value for value in reported if value is not None]
    check(
        "4. no run spent tokens thinking",
        bool(counted) and all(value == 0 for value in counted),
        f"per run: {reported}"
        + (
            f" ({len(counted)}/{len(reported)} runs reported a count)"
            if counted
            else " -- UNMEASURED: no usage reported, so no run's reasoning count "
            "was read. Not evidence that thinking was off; case 0c is the only "
            "surviving evidence, and it is structural rather than from the wire."
        ),
    )

    # -- 5. nothing truncated -------------------------------------------------
    rule("5. no run hit the output ceiling")
    # `finish_reason=length` was 3/3 with reasoning left at its default. A
    # truncated reply usually fails to parse -- but not always, and the case
    # that parses is the dangerous one: a short golden set that reads like a
    # thin corpus.
    #
    # **Same vacuous-pass defect as case 4, and it reads even more innocently.**
    # `"length" not in reasons` is trivially true of an EMPTY list, so a run in
    # which no `finish_reason` was reported at all went green on a check whose
    # whole job is to read `finish_reason`. Truncation is exactly the failure
    # that parses: a short golden set looks like a thin corpus, so this is the
    # last thing that should certify itself on no data.
    reasons = [reason for result in usable for reason in result.probe.finish_reasons]
    check(
        "5. no finish_reason=length against MAX_OUTPUT_TOKENS",
        bool(reasons) and "length" not in reasons,
        f"finish reasons: {reasons}"
        if reasons
        else "UNMEASURED: no usage reported, so no finish_reason was read. Not "
        "evidence that nothing truncated.",
    )

    # -- 6. read the refusal probes by eye ------------------------------------
    rule("6. the refusal probes, for a human to read (not asserted)")
    print("PRD 3.6.1: a probe about something the corpus NEVER mentions tests")
    print("nothing. Each of these should name a detail the fixture RAISES and")
    print("does not complete -- the coolant's identity, the Tier 2 procedure,")
    print("the single-point failure, the scheduling rules, the cost comparison.")
    for result in usable:
        for suggestion, reference in persistable(result.parsed):
            if suggestion.expected_behaviour != "refuse":
                continue
            print(f"  run {result.index}: {ascii_safe(suggestion.question)}")
            print(f"           ref: {ascii_safe(reference or '(none)')}")

    finish(None)


def finish(note: str | None) -> None:
    print("\n" + "=" * 74)
    if note:
        print(note)
    if failures:
        print(f"{len(failures)} FAILED:")
        for name in failures:
            print(f"  - {name}")
        sys.exit(1)
    print("all checks passed")
    sys.exit(0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runs",
        type=int,
        default=DEFAULT_RUNS,
        help=f"how many golden sets to generate (default {DEFAULT_RUNS})",
    )
    args = parser.parse_args()
    asyncio.run(main(args.runs))
