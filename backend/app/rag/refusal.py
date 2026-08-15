"""Refusal and gap detection -- one marker list, two questions.

Split out of `app/api/ask.py` when the agent loop needed the same phrases, and
kept in `app/rag/` rather than `app/api/` because it is domain logic about model
output, not about HTTP. The direction matters: `agent_loop` may not import from
`app.api`, and a second copy of these tuples is precisely the failure this module
exists to prevent -- the list has already been wrong three times (`"does not
say"` after eval run 1, `"does not cover"` after run 2, `"does not state"` after
the agentic check), each time producing a scorecard that blamed the agent for
the detector's gap.

**Two functions, two different questions, and confusing them is the bug this
file is shaped to avoid.**

`detect_refusal` asks *"did this turn decline?"*. It is position-sensitive: a
caveat phrase counts only before the model has answered anything, because "The
lesson covers X and Y. The text does not say when it was published." is a
qualified answer, not a refusal. It writes `queries.refused`, which is a Stage 3
success metric, so a false positive there corrupts the measurement.

`detect_gap` asks *"did this text admit something was missing?"* -- anywhere,
regardless of position, regardless of how much was answered first. It is a
strictly weaker test and it must never write `refused`. Its only job is to
trigger a search: if the model says it does not know, and it holds a tool for
finding out, it should look before that answer is accepted. A false positive
there costs one retrieval; a false negative costs the whole agentic behaviour,
which is exactly the asymmetry that makes the weaker test the right one.
"""

from __future__ import annotations

import re

# How much of an answer may go by before a refusal phrase stops counting as a
# refusal.
#
# A model that declines says so before it says anything else. A model that
# answers and *then* notes a gap -- "...the lesson covers X, Y and Z. The context
# does not say when the method was first published." -- is qualifying an answer,
# not refusing, and counting that turn as a refusal corrupts the one metric
# Stage 3 uses to check that grounding works.
#
# Position alone does not separate the two, because a real answer's first
# sentence can run past any fixed character window. What separates them is how
# much substance precedes the phrase: scanning stops once the model has produced
# ~200 characters of actual content, so a marker in the opening (however
# long-winded the refusal that follows) counts and a marker after two sentences
# of real answer does not. The budget is charged per sentence and only after that
# sentence has been checked, so a leading apology barely dents it and a single
# long opening sentence is still examined in full.
REFUSAL_LEAD_CHARS = 200

# How much preamble may precede a CAVEAT_MARKERS phrase and still count. Room
# for an apology or a hedge ("I'm sorry.", "Let me check the context.") and
# nothing more.
REFUSAL_PREAMBLE_CHARS = 40

# Substring markers, lowercased and whitespace-normalised before matching, in
# two tiers -- because the phrases split cleanly into two kinds and treating
# them alike is what produces false positives.
#
# REFUSAL_MARKERS are phrasings a model reaches for only when declining. They
# count anywhere in the lead.
#
# CAVEAT_MARKERS are ordinary qualifications. "The material does not include a
# glossary" is a perfectly good sentence inside a real answer, and a flat
# substring test over the opening would score that turn as a refusal. They count
# only when said before the model has answered anything -- a refusal leads with
# the reason it is refusing; an answer earns its caveats first.
#
# This is still a heuristic over natural language and it is still fallible in
# both directions. It is spelled out rather than inferred because
# `queries.refused` is a Stage 3 success metric (PRD section 4.4: a correct
# refusal is a correct outcome), and `golden_questions.expected_behaviour =
# 'refuse'` is what will actually calibrate these lists -- ten questions whose
# right answer is "I don't know" will show which phrasings Gemma really reaches
# for and which entries here never fire once.
REFUSAL_MARKERS = (
    "does not contain",
    "doesn't contain",
    "do not contain",
    "don't contain",
    "no information",
    "not enough information",
    "insufficient information",
    "no relevant information",
    "cannot answer",
    "can't answer",
    "cannot be answered",
    "unable to answer",
    "i do not know",
    "i don't know",
    "not found in the",
    "not covered in the",
    "outside the scope",
)

# "does not say" / "doesn't say" were added after the first two eval runs, which
# both reported `refusal_pass = 0 / 2`. Half of that was the detector, not the
# agent: Gemma answered "Which of the fourteen launches took place in 2040?" with
#
#     "The provided text does not say which of the fourteen launches took
#      place in 2040 [1]."
#
# -- a textbook refusal, scored as a failure, because the phrase it reached for
# was in neither tier. The scorecard blamed the agent for the measurement's gap,
# which is the same failure class as `strictness=3` and worse than a crash for
# the same reason: it still renders, and it still points confidently at the wrong
# thing.
#
# **The CAVEAT tier, not the refusal tier, and the distinction is the whole
# point.** REFUSAL_MARKERS match anywhere in the lead, so putting it there would
# score "The lesson covers X and Y. The text does not say when it was published."
# as a refusal of a question it had just answered. "does not say" is an ordinary
# qualification -- structurally identical to "does not mention" and "does not
# specify" already here -- and only means refusal when nothing has been answered
# yet. The failing row's phrase starts at character 0, so it matches; the
# answer-then-caveat row that CLAUDE.md flags as a REAL persona finding lands at
# consumed=198, stays outside the 40-character preamble window, and is still
# correctly counted as an answer. Fixing the detector must not delete that
# finding.
CAVEAT_MARKERS = (
    "context does not",
    "does not mention",
    "doesn't mention",
    "does not provide",
    "doesn't provide",
    "does not include",
    "does not specify",
    "does not say",
    "doesn't say",
    # "does not cover" is here for the same reason, and it settles a question
    # CLAUDE.md left open. That note warned that adding this phrase would score
    # the Feynman answer-then-caveat turn as a refusal and "quietly delete the
    # finding" -- true of the HARD tier, and not true here. Replayed against the
    # actual stored answers, the two turns separate cleanly by position:
    #
    #   run 1  "The provided text does not cover the specific duties..."
    #          consumed=0    -> inside the window  -> refusal, correctly
    #   run 2  "...states that there are eleven permanent crew members [1]. It
    #           also mentions ... but it does not cover their specific duties."
    #          consumed=198  -> outside the window -> answer, correctly
    #
    # The tier split was built for exactly this and needed no adjudication. Worth
    # noting the record it corrects: CLAUDE.md described the second refusal row
    # as answer-then-caveat in BOTH runs, but only run 2 was -- run 1's was a
    # clean leading refusal. Three of the four refusal rows were detector
    # failures, not two.
    "does not cover",
    "doesn't cover",
    "does not appear in",
    # "does not state" is the THIRD recurrence of this exact gap, and the third
    # one is the one that should change the approach rather than the list.
    # Found by `scripts/agentic_check.py` scenario S7, on a corpus that RAISES
    # the modulation scheme and says it is held elsewhere:
    #
    #     "The provided text does not state which modulation and coding scheme
    #      the Ka-band downlink uses; it notes that modulation..."
    #
    # A perfect refusal, `refused=False`. Run 1 taught this list "does not say",
    # run 2 taught it "does not cover", and each time the finding arrived as a
    # scorecard confidently blaming the agent.
    #
    # So the entries below are added by PATTERN, not by observation: the family
    # is "does not <reporting verb>", and every member of it fails the hard-tier
    # test ("would a model never write this while answering?") for the same
    # reason. Adding the rest of the family costs nothing, because the caveat
    # tier is position-gated -- a phrase here only counts as a refusal when
    # nothing has been answered before it -- and NOT adding them costs another
    # eval run misattributed to the agent.
    #
    # The real lesson is the one CLAUDE.md already states and this confirms
    # twice over: a refusal metric measures the detector and the agent at once,
    # and the two failures look identical on the card. Read the answers first.
    "does not state",
    "doesn't state",
    "does not describe",
    "does not indicate",
    "does not detail",
)


def sentences(text: str) -> list[str]:
    """Split into sentences, for the refusal scan only.

    Splits on a terminator followed by whitespace, and on blank lines. It does
    NOT split on a lone newline, which is the case that matters: generated text
    soft-wraps mid-sentence, so treating every newline as a boundary would cut
    "does not\\ncontain" into two fragments and the marker would match neither.
    """
    return [s for s in re.split(r"(?<=[.!?])\s+|\n{2,}", text) if s.strip()]


def detect_refusal(answer: str) -> str | None:
    """The matched refusal phrase, or None. See REFUSAL_MARKERS.

    **Refusal is detected from the answer, never from the score.** CLAUDE.md
    records the measurement that settles this: on `3.1-lesson-gist.md`,
    on-topic questions scored 0.61-0.67 and off-topic ones 0.49-0.58 -- an
    overlapping band, not a separation -- and the plainly off-topic "What is the
    refund policy for this course?" scored **0.5765**, comfortably *above* the
    0.5 threshold. It was refused anyway, correctly, because the system prompt
    forbids answering outside the context. The threshold governs *rewriting*;
    the prompt governs *refusing*. Wiring `refused` to the threshold would have
    marked that turn as answered.

    Sentence by sentence until the content budget runs out -- see
    REFUSAL_LEAD_CHARS for why a fixed character window is not enough, and
    CAVEAT_MARKERS for why the markers are in two tiers. Whitespace is normalised
    inside each sentence because generated text wraps, and "does not  contain" is
    the same refusal as "does not contain".
    """
    consumed = 0
    for sentence in sentences(answer):
        lowered = " ".join(sentence.lower().split())
        for marker in REFUSAL_MARKERS:
            if marker in lowered:
                return marker
        if consumed <= REFUSAL_PREAMBLE_CHARS:
            for marker in CAVEAT_MARKERS:
                if marker in lowered:
                    return marker
        # Charged after the checks, so the sentence that blows the budget is
        # still examined. A single long opening sentence that refuses is a
        # refusal.
        consumed += len(lowered)
        if consumed >= REFUSAL_LEAD_CHARS:
            return None
    return None


# All markers, both tiers, for the position-insensitive test below. Built from
# the tuples rather than typed out, so a phrase added to either tier is picked up
# here automatically -- the whole point of one module.
_ALL_MARKERS = REFUSAL_MARKERS + CAVEAT_MARKERS


def detect_gap(answer: str) -> str | None:
    """The phrase in which the answer admitted something was missing, or None.

    Deliberately NOT `detect_refusal`, and the difference is the reason this
    module has two functions. Measured 2026-08-16, `agentic_check.py` S3, with
    one chunk of context and a two-part question:

        "The platform carries twenty-four lithium-ion battery modules [1]. The
         provided text does not contain information regarding the onboard
         storage for science instruments."

    `detect_refusal` returns None for that, correctly -- the turn answered, and
    scoring it as a refusal would corrupt `refusal_pass`. But it is exactly the
    turn that should have searched, and the admission sits in the second
    sentence where every position rule this module has is designed to ignore it.

    So: no sentence budget, no preamble window, both tiers, anywhere in the text.
    The two tiers exist to protect a metric; this function feeds a retry, and a
    retry that fires once too often costs a search while one that fires too
    rarely costs the feature.
    """
    lowered = " ".join(answer.lower().split())
    for marker in _ALL_MARKERS:
        if marker in lowered:
            return marker
    return None
