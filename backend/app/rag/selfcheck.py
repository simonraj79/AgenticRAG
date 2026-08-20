"""Self-evaluation -- does the draft actually rest on what was retrieved?

`new features/loop.md` T2 in its purest available form. The question is "did the
outcome I wanted occur?", never "did an error occur?", and an ungrounded answer
is the exact shape T2 is about: it raises nothing, renders perfectly, and reads
better than the refusal it should have been.

TWO STAGES, AND THE FIRST ONE IS FREE

`ContextLedger` assigns a marker once and never reassigns it, so at the moment a
draft lands the set of LEGAL citations is known exactly. That makes the trigger a
set operation rather than a model call or a string heuristic -- which is what
makes it affordable to run on every turn.

The critic only runs when that free test fires. In the common case a turn pays
nothing at all, which is the whole reason this is a trigger rather than an
unconditional grading pass. An always-on critic would add a model call to a
system where generation is already 89% of turn latency.

STRICTNESS FOLLOWS THE COST OF BEING WRONG, IN EACH DIRECTION (loop.md T3)

The two signals have opposite asymmetries, which is why they are two signals:

    phantom marker   FP: nothing. No teaching method has a reason to cite a
                         passage that does not exist.
                     FN: a fabricated citation ships -- worse than a fabricated
                         sentence, because the [n] chip ASSERTS provenance the
                         user can click.
                     => strict. Always fire. No persona exemption.

    no citations     FP: one critic call, and possibly a discarded draft the
                         user watched stream. Visible and annoying.
                     FN: an unanchored answer in a confident teaching voice --
                         PRD 4.2's "most likely place in this system for
                         hallucination to start".
                     => gated: persona flag, length floor, and not a refusal.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Sequence

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable
from pydantic import BaseModel, Field

from app.config import settings
from app.rag.llm import build_chat_model
from app.rag.refusal import detect_refusal
from app.metering.context import meter_as

log = logging.getLogger(__name__)

# Matches the frontend's `MARKER_PATTERN` in `Message.tsx`. Bounded at three
# digits for the same reason it is there: an unbounded run of digits turns a
# footnote-shaped string in a code block into a citation.
MARKER_PATTERN = re.compile(r"\[(\d{1,3})\]")

# Below this, an uncited answer is far more likely to be a refusal, a clarifying
# question, or a one-line acknowledgement than an unanchored claim. Set from the
# shape of the failure rather than measured: the answers PRD open item 20 is
# about run 495-1551 characters, and the refusals in CLAUDE.md's refusal table
# are all well under 200. Raising it trades recall for quiet; lowering it starts
# grading pleasantries.
MIN_SUBSTANTIVE_CHARS = 200

SIGNAL_PHANTOM = "phantom_marker"
SIGNAL_NO_CITATIONS = "no_citations"

VERDICT_GROUNDED = "grounded"
VERDICT_UNGROUNDED = "ungrounded"
VERDICT_FAILED = "failed"


@dataclass(frozen=True)
class SelfCheckSignal:
    """Why the check fired. `None` from `self_check_signal` means it did not."""

    name: str
    phantom_markers: tuple[int, ...] = ()
    markers_used: tuple[int, ...] = ()


def markers_in(text: str) -> set[int]:
    """Every citation marker the draft used."""
    return {int(m) for m in MARKER_PATTERN.findall(text or "")}


def self_check_signal(
    text: str,
    *,
    ledger_size: int,
    expects_citations: bool,
) -> SelfCheckSignal | None:
    """The free pre-check. No model call, no network, no string heuristics.

    `expects_citations` comes off the routed specialist and is the flag that
    stops this punishing a teaching method for teaching. A Socratic turn is a
    question put back to the learner and a Polya phase-1 turn is "what are you
    given?" -- neither asserts anything, so neither has anything to cite, and
    both are correct when they cite nothing.

    Returns None when the answer looks anchored, which must be the overwhelmingly
    common case or the trigger is miscalibrated rather than the model.
    """
    used = markers_in(text)

    # Ungated, and first: this one is evidence rather than inference.
    legal = set(range(1, ledger_size + 1))
    phantom = sorted(used - legal)
    if phantom:
        return SelfCheckSignal(
            name=SIGNAL_PHANTOM,
            phantom_markers=tuple(phantom),
            markers_used=tuple(sorted(used)),
        )

    if not expects_citations:
        return None
    if used:
        return None
    if len(text or "") < MIN_SUBSTANTIVE_CHARS:
        return None
    if detect_refusal(text):
        # "The material does not cover this" is a correct answer with nothing to
        # cite. Grading it would repeat `refusal_pass = 0/2` -- a measurement
        # penalising the behaviour the system exists to produce.
        return None

    return SelfCheckSignal(name=SIGNAL_NO_CITATIONS, markers_used=())


# --------------------------------------------------------------------------
# The critic
# --------------------------------------------------------------------------


class GroundingVerdict(BaseModel):
    """What the critic returns."""

    grounded: bool = Field(
        description=(
            "True if every factual claim about the material is carried by a "
            "cited passage. Exempt material does not make this false."
        )
    )
    unsupported: list[str] = Field(
        default_factory=list,
        description=(
            "The offending factual claims, quoted from the answer. Empty when "
            "grounded is true."
        ),
    )
    suggested_query: str | None = Field(
        default=None,
        description=(
            "If a corpus search would settle the unsupported claims, the search "
            "to run. Null if searching would not help."
        ),
    )


# THIS PROMPT'S CARVE-OUT IS LOAD-BEARING. DO NOT TRIM IT.
#
# EVAL run 3 scored an answer 0.571 on faithfulness whose sentences 1-4 were four
# correct figures straight from the context. The deductions were sentence 5, a
# labelled analogy, and sentence 6, "restate this in your own words". Both are
# unsupported by construction. Both are precisely what `feynman-explainer` exists
# to produce. The scorecard then named faithfulness as the weakest metric and
# advised tightening the grounding clause and reducing persona verbosity -- that
# is, deleting the pedagogy. It is PRD open item 20.
#
# A groundedness critic without this carve-out rebuilds that instrument INSIDE
# the live turn, where it would not merely recommend deleting the teaching. It
# would discard the draft and instruct the model to write a duller one.
#
# `scripts/agentic_check.py` S26 pins this with that actual 0.571 answer. A
# wording edit that drops the exemptions will not raise anything; it will just
# quietly start deleting teaching.
CRITIC_SYSTEM_PROMPT = """\
You check whether an answer rests on the passages it was given. You are not \
grading the answer's teaching, its tone, or its usefulness.

Judge ONLY FACTUAL CLAIMS ABOUT THE MATERIAL -- statements asserting that \
something is the case in the subject being taught.

These are NOT factual claims about the material, and are NEVER unsupported:
- an analogy, comparison or illustration the answer presents as such
- a question put to the learner
- an instruction or invitation to the learner ("restate this in your own words", \
"try the next step", "what do you think happens if")
- a statement that the material does not cover something, or covers only part
- a description of what the answer itself is doing ("let's start with", "first")

Judge everything else strictly. A factual claim is UNSUPPORTED unless one of the \
numbered passages actually carries it. Close enough is not carried. A claim that \
generalises beyond what a passage says is not carried.

If a search of the material would settle the unsupported claims, put that search \
in `suggested_query`. If the claims are simply invented, or the material plainly \
has nothing on them, leave it null.

Return grounded=true when there are no unsupported factual claims.\
"""

CRITIC_USER_TEMPLATE = """\
PASSAGES:
{context}

ANSWER TO CHECK:
{answer}\
"""


@lru_cache(maxsize=1)
def get_critic() -> Runnable:
    """The critic chain. One instance, shared.

    On `settings.decision_model` rather than the generation model, for the reason
    `config.py` gives about the Ragas judge: a model grading its own output in
    the same voice that produced it is self-assessment. Here it is also the
    cheaper call, which is what lets the trigger stay affordable.

    `method="function_calling"` for the reason recorded on `get_contextualizer`:
    a fenced JSON answer comes back from a strict parser as `None` rather than as
    an exception, and this is a value the caller branches on.
    """
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", CRITIC_SYSTEM_PROMPT),
            ("human", CRITIC_USER_TEMPLATE),
        ]
    )
    model = build_chat_model(
        settings.decision_model,
        temperature=settings.generation_temperature,
        top_p=settings.generation_top_p,
        top_k=settings.generation_top_k,
    )
    return prompt | model.with_structured_output(
        GroundingVerdict, method=settings.structured_output_method
    )


@dataclass(frozen=True)
class SelfCheckResult:
    """What the turn records, whether or not the critic ran."""

    signal: str
    verdict: str
    unsupported: tuple[str, ...] = ()
    suggested_query: str | None = None
    phantom_markers: tuple[int, ...] = ()
    duration_ms: int = 0
    acted: bool = False


async def run_grounding_critic(
    *, answer: str, context: str, signal: SelfCheckSignal
) -> SelfCheckResult:
    """Ask whether the draft is carried by its passages. Never raises.

    A failed critic returns `verdict="failed"` and the turn KEEPS THE DRAFT. That
    is the honest outcome: the check is a quality control, and a quality control
    that cannot run has not found a problem. Failing the turn instead would let a
    transport error delete an answer that was probably fine.
    """
    import time

    started = time.perf_counter()
    try:
        # The critic is the call most likely to be questioned on cost grounds --
        # it is a second model call on a turn that already has an answer. Naming
        # its kind is what lets that question be settled with a number.
        with meter_as(call_kind="critic"):
            verdict = await get_critic().ainvoke(
                {"context": context, "answer": answer}
            )
    except Exception as exc:  # noqa: BLE001 - see the docstring
        log.warning("Grounding critic failed, keeping the draft: %s", exc)
        return SelfCheckResult(
            signal=signal.name,
            verdict=VERDICT_FAILED,
            phantom_markers=signal.phantom_markers,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )

    duration_ms = int((time.perf_counter() - started) * 1000)

    if verdict is None:
        log.warning("Grounding critic returned no structured output")
        return SelfCheckResult(
            signal=signal.name,
            verdict=VERDICT_FAILED,
            phantom_markers=signal.phantom_markers,
            duration_ms=duration_ms,
        )

    # A phantom marker is proof the answer cited something that does not exist,
    # and no critic verdict can make that true. If the critic says grounded
    # anyway, the marker still has to go -- so the signal overrides the verdict
    # in exactly one direction, and never the other.
    grounded = bool(verdict.grounded) and not signal.phantom_markers

    query = (verdict.suggested_query or "").strip() or None
    return SelfCheckResult(
        signal=signal.name,
        verdict=VERDICT_GROUNDED if grounded else VERDICT_UNGROUNDED,
        unsupported=tuple(c.strip() for c in verdict.unsupported if c and c.strip()),
        suggested_query=query,
        phantom_markers=signal.phantom_markers,
        duration_ms=duration_ms,
    )


def correction_message(result: SelfCheckResult) -> str:
    """The nudge appended before a redraft.

    Names what was wrong rather than restating the grounding rule. The rule is
    already at the top of every persona prompt, stated more forcefully than
    anything that could be added here -- repeating it is what loop.md T1
    describes as the instruction that competes and loses.
    """
    lines: list[str] = []
    if result.phantom_markers:
        markers = ", ".join(f"[{m}]" for m in result.phantom_markers)
        lines.append(
            f"Your answer cited {markers}, which do not exist. Only the numbered "
            "passages above are citable."
        )
    if result.unsupported:
        lines.append("These statements are not carried by any passage above:")
        lines.extend(f"  - {claim}" for claim in result.unsupported[:5])
    lines.append(
        "Rewrite the answer. Keep what the passages support, cite it, and say "
        "plainly which part the material does not cover. Do not restate a claim "
        "you cannot cite."
    )
    return "\n".join(lines)
