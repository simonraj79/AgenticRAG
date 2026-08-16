"""Harness for the question rewriter -- structure first, then behaviour.

    backend/.venv/Scripts/python.exe scripts/rewrite_check.py
    backend/.venv/Scripts/python.exe scripts/rewrite_check.py --models

**TWO LAYERS IN ONE FILE, AND THE ORDER IS THE POINT.** Cases 1-5 are structural:
no network, no database, no model, milliseconds. Cases 6-11 make real OpenRouter
calls through `pipeline.contextualize_question` and cost a minute. The structural
ones run first so that a red one EXPLAINS a red behavioural one instead of the
reader debugging a model over a prompt that no longer says what they think it
says -- the same S16-before-S15 ordering `scripts/agentic_check.py` uses, arrived
at for the same reason.

**WHY THIS FILE EXISTS.** The rewriter runs on every turn as of 2026-08-16, and
`contextualize_question` swallows every exception by design and degrades to the
raw question. So every way this component can break is SILENT: no exception, no
failed request, a turn that answers, and marginally worse retrieval. That is
`new features/loop.md` T2 exactly -- the assertion has to name the outcome
("did the typo get repaired", "did the clean question survive"), because there is
no error to test for.

**CASE 8 IS AS IMPORTANT AS THE REPAIR CASES AND IS EASY TO READ AS FILLER.** The
asymmetry recorded above `CONTEXTUALIZE_SYSTEM_PROMPT` is that a false positive
is the expensive direction: rewriting a question that was already fine risks
changing what was asked, while a missed typo costs slightly worse retrieval on a
question the corpus may answer anyway. Case 8 is the only assertion protecting
the first of those, so it is held to 5/5 where the repair cases are held to 4/5.

Trials, not single samples: the rewriter runs at `generation_temperature` (1.0,
from Gemma's card), so one call is an anecdote. `get_contextualizer.cache_clear()`
is called before anything that changes the prompt or the settings the chain was
built from -- the chain is `@lru_cache(maxsize=1)`, so without it this file would
happily test the previously-built chain and report green.

ASCII in print(), exit 1 on any failure -- `new features/06-test-plan.md`.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.config import settings  # noqa: E402
from app.rag.pipeline import (  # noqa: E402
    CONTEXTUALIZE_SYSTEM_PROMPT,
    StandaloneQuestion,
    contextualize_question,
    get_contextualizer,
)

failures: list[str] = []

# Repair cases tolerate one bad sample in five; the over-firing guard does not.
# Not superstition and not a fudge: at temperature 1.0 a single miss on a repair
# is model variance and costs one slightly worse retrieval, while a single
# unprompted rewrite of a clean question is the failure this whole prompt is
# biased against. The thresholds are the asymmetry, in numbers.
TRIALS = 5
REPAIR_PASS_MARK = 4
STRICT_PASS_MARK = 5


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "[ok]  " if condition else "[FAIL]"
    print(f"{status} {name}" + (f" -- {detail}" if detail else ""))
    if not condition:
        failures.append(name)


def ascii_safe(text: str) -> str:
    """The model's output reaches a Windows console, where cp1252 raises on an
    em-dash several layers from anything this file wrote. Three throwaway scripts
    in this repo have died of it."""
    return text.encode("ascii", "replace").decode("ascii")


def has(text: str, needle: str) -> bool:
    """Case-insensitive containment.

    Case is not what any of these cases is about -- "Ka-band" and "ka-band"
    embed to nearly the same place and neither is a defect -- so testing it would
    make the harness fail for a reason it is not measuring.
    """
    return needle.lower() in text.lower()


# --------------------------------------------------------------------------
# 1-5. Structural. No network.
# --------------------------------------------------------------------------

def structural() -> None:
    print("\n-- structural (no network) " + "-" * 45)

    # 1-3 pin the three instructions that must survive the widening. Each was
    # load-bearing before this change and none of them is what the change was
    # for, which is exactly the sort of line a later edit drops by accident.
    check(
        "1  prompt still forbids answering",
        has(CONTEXTUALIZE_SYSTEM_PROMPT, "Do not answer the question"),
        "an answer embedded as a search query retrieves documents that LOOK like answers",
    )
    check(
        "2  prompt still forbids invention",
        has(CONTEXTUALIZE_SYSTEM_PROMPT, "not in the question or in the conversation"),
        "a repair must be recoverable from the question plus the conversation, never the corpus",
    )
    check(
        "3  prompt still resolves coreference",
        has(CONTEXTUALIZE_SYSTEM_PROMPT, "Resolve pronouns"),
        "the original job, and the one a widened prompt is likeliest to crowd out",
    )

    # 4. One required field, still. A second one is a second thing the model must
    # fill, and `config.decision_model`'s parsed 9/9 was measured against exactly
    # this schema.
    required = sorted(
        name for name, f in StandaloneQuestion.model_fields.items() if f.is_required()
    )
    check(
        "4  StandaloneQuestion has exactly one required field",
        required == ["question"],
        f"required={required}",
    )

    # 5. The two settings the feature is. Asserted here rather than assumed by
    # the behavioural cases below, so that "the rewriter did nothing" is
    # attributed to configuration on the line where it is true.
    check(
        "5  rewrite_every_turn on, eval_rewrite_questions off",
        settings.rewrite_every_turn is True and settings.eval_rewrite_questions is False,
        f"rewrite_every_turn={settings.rewrite_every_turn} "
        f"eval_rewrite_questions={settings.eval_rewrite_questions}",
    )


# --------------------------------------------------------------------------
# 6-11. Behavioural. Real model calls.
# --------------------------------------------------------------------------

# A one-turn conversation about the Ka-band link, for the coreference case. The
# facts are `scripts/fixtures/comms-subsystem.md`'s, so this file and
# `agentic_check.py` are talking about the same corpus.
KA_BAND_HISTORY = [
    (
        "What is the Ka-band high-rate downlink budgeted at?",
        "The Ka-band high-rate downlink is budgeted at 220 Mbps [comms-subsystem.md].",
    )
]

# A conversation that SPELLS OUT the acronym, for case 7b -- the hardest case
# for an unconditional prohibition. "command and telemetry" is already on the
# table, so an expansion here would invent nothing and would still be wrong: the
# rule is that the acronym is the corpus's own wording and passes through. If a
# future prompt edit reintroduces conditional expansion, 7b is the case that
# goes red first, before anyone has to notice it in a trace.
CT_HISTORY = [
    (
        "What does the S-band command and telemetry link carry?",
        "The S-band command and telemetry link carries the uplink and the "
        "housekeeping downlink [comms-subsystem.md].",
    )
]


# Words that carry subject matter, for the groundedness assertion in case 7b.
# Function words and the question scaffolding are excluded because the rewriter
# is SUPPOSED to add those -- "what is the ... rate?" is a well-formed question
# built from a fragment, and penalising it would assert against the feature. What
# must be grounded is the NOUNS: every content word in the rewrite has to come
# from the question or from the conversation, never from the model's priors.
_STOPWORDS = frozenset(
    """a an and are as at be by can do does for from has have how in is it its many
    much of on or that the their there these this to was were what when where which
    who why will with would you your please tell me about""".split()
)


def _content_words(text: str) -> list[str]:
    """Lowercased alphabetic tokens that are not scaffolding.

    Punctuation is stripped rather than split on, so "C&T" and "Ka-band" survive
    as single tokens -- splitting them would compare fragments that appear
    nowhere and fail every case for the wrong reason.
    """
    out = []
    for raw in text.lower().split():
        word = raw.strip("?.,;:!()[]\"'")
        if word and word not in _STOPWORDS and any(c.isalpha() for c in word):
            out.append(word)
    return out


async def run_case(
    name: str,
    question: str,
    predicate,
    *,
    history=(),
    trials: int = TRIALS,
    pass_mark: int = REPAIR_PASS_MARK,
) -> None:
    """`trials` samples, printed individually, scored against `pass_mark`."""
    passed = 0
    samples: list[str] = []
    for _ in range(trials):
        try:
            got = await contextualize_question(question, history)
        except Exception as exc:  # contextualize_question swallows its own; be safe
            got = None
            samples.append(f"RAISED {type(exc).__name__}")
            continue
        ok = got is not None and predicate(got)
        passed += int(ok)
        samples.append(("+" if ok else "-") + " " + ascii_safe(repr(got)))
    check(f"{name}  [{passed}/{trials}]", passed >= pass_mark)
    for sample in samples:
        print(f"          {sample}")


async def behavioural() -> None:
    print("\n-- behavioural (real model calls) " + "-" * 38)
    print(f"   decision_model={settings.decision_model}")

    # The behavioural cases exercise `contextualize_question`, whose empty-history
    # early-out reads `settings.rewrite_every_turn`. Case 5 asserts the shipped
    # value; this forces it regardless, so a locally-overridden .env produces a
    # single red structural case rather than six red behavioural ones blaming the
    # model for a flag.
    prior = settings.rewrite_every_turn
    settings.rewrite_every_turn = True
    get_contextualizer.cache_clear()
    try:
        # 6. Typo repair. Three misspellings at once, which is the realistic
        # shape -- a hurried question is not wrong in one place.
        # The hyphen is deliberately NOT asserted. "ka bnd" repaired to "Ka band"
        # and to "Ka-band" are both correct repairs of the same typo, and which
        # one the model picks moved when the acronym bullet was narrowed --
        # asserting the hyphen made this case red while every substantive repair
        # in it was 5/5. The two words that MATTER are the ones the corpus writes
        # and the raw question does not.
        await run_case(
            "6  typo repair",
            "wats the thruput on the ka bnd dwnlink",
            lambda r: has(r, "throughput")
            and has(r, "downlink")
            and (has(r, "Ka-band") or has(r, "Ka band")),
        )

        # 7a. **The acronym prohibition, and the typo repair beside it.** The
        # prompt once said "expand acronyms and initialisms", contradicting its
        # own do-not-invent rule, and the model invented: "Ka-band (Kurtz-band)",
        # "Ka-band (Kurzwellen-band)", and -- measured moving retrieval to the
        # wrong file -- "LS&T" -> "Link System and Telemetry". A wrong expansion
        # is the expensive direction: the acronym is the DOCUMENT'S OWN wording
        # and matches it, while a guess does not.
        #
        # The typo half of the same assertion matters: the acronym passing
        # through untouched must not come at the cost of the repair still
        # happening around it. "uplnk" -> "uplink" in the same string.
        await run_case(
            "7a acronym untouched, typo beside it still fixed",
            "whats the C&T uplnk rate",
            lambda r: has(r, "C&T")
            and has(r, "uplink")
            and not has(r, "command and telemetry"),
        )

        # 7b. **The line the prohibition is actually drawn on, and it is not
        # where the first version of this case put it.**
        #
        # Measured: with no history, "C&T" and "LS&T" both pass through untouched
        # 5/5 (7a, 7c). With a history that SPELLS THE TERM OUT, the rewrite comes
        # back as "S-band command and telemetry uplink rate" 5/5. The only
        # variable is the conversation, so that expansion is the COREFERENCE
        # bullet resolving a reference into the words it stands for -- the words
        # being ones the user's own thread supplied. It is not the acronym
        # feature, which was removed from the prompt; and an unconditional ban
        # could only beat it by damaging coreference, which is measured working
        # (case 9) and is the rewriter's oldest job.
        #
        # So the invariant worth asserting is GROUNDEDNESS, not silence: whatever
        # expansion appears must be recoverable from the conversation. That is
        # exactly the property whose absence produced "Kurtz-band" and "Link
        # System and Telemetry", and it is the one a future prompt edit would
        # break first.
        history_text = " ".join(q + " " + a for q, a in CT_HISTORY).lower()
        await run_case(
            "7b any expansion is grounded in the conversation, never invented",
            "and whats the C&T uplnk rate",
            lambda r: has(r, "uplink")
            and (has(r, "C&T") or "command and telemetry" in history_text)
            and not has(r, "Link System")
            and all(
                w in history_text or w in "and whats the c&t uplnk rate"
                for w in _content_words(r)
            ),
            history=CT_HISTORY,
        )

        # 7c. The regression that was measured doing actual harm, pinned with the
        # exact string that did it. "LS&T" appears in no fixture; the invented
        # expansion sent retrieval to a different file entirely.
        await run_case(
            "7c a fabricated expansion is not invented for an unknown acronym",
            "wats the LS&T alloc",
            lambda r: has(r, "LS&T") and not has(r, "Link System"),
        )

        # 8. **The over-firing guard.** Byte-identical, not "close enough": every
        # number in EVAL.md was measured on unrewritten text, and the argument
        # that invented words move the vector away from the user's intent is
        # unchanged by this widening. 5/5 -- see REPAIR_PASS_MARK.
        clean = "How much power do the solar arrays generate?"
        await run_case(
            "8  clean question comes back byte-identical",
            clean,
            lambda r: r == clean,
            pass_mark=STRICT_PASS_MARK,
        )

        # 9. Coreference still works. The regression the widening most plausibly
        # causes: a prompt now talking about spelling could quietly stop
        # dereferencing "its".
        await run_case(
            "9  coreference still resolves",
            "what is its power draw?",
            lambda r: has(r, "Ka-band"),
            history=KA_BAND_HISTORY,
        )

        # 10. The rewriter must not ANSWER. A rewrite carrying "220 Mbps"
        # retrieves documents that look like answers rather than documents that
        # contain one -- and on a corpus it guessed wrong about, retrieves
        # nothing at all.
        await run_case(
            "10 the rewriter does not answer",
            "how fast is the Ka-band downlink?",
            lambda r: not any(ch.isdigit() for ch in r) and not has(r, "Mbps"),
        )

        # 11. The change's whole premise, in one assertion: empty history now
        # returns a STRING. Before 2026-08-16 this returned None by construction.
        got = await contextualize_question("What is the S-band uplink rate?", ())
        check(
            "11 empty history returns a string, not None",
            isinstance(got, str) and bool(got),
            ascii_safe(repr(got)),
        )
    finally:
        settings.rewrite_every_turn = prior
        get_contextualizer.cache_clear()


# --------------------------------------------------------------------------
# --models: the decision-model head-to-head, re-run because the prompt changed
# --------------------------------------------------------------------------

# `config.py` records gemma parsed 9/9, correct 9/9, p50 1.02s against deepseek
# 9/9, 9/9, 1.58s, and says in as many words not to move `decision_model` without
# re-running that table. **A prompt change invalidates it exactly as a model
# change would**, so this mode re-runs it.
#
# Two departures from the original, both forced and both worth stating. The case
# mix is one coreference plus one repair plus one clean question, because the job
# itself widened and a table of three coreference cases no longer measures what
# this call site does. And the "reasoning default" arm is absent: reasoning is
# not a parameter `get_contextualizer` passes, so the only arms expressible
# through the real chain are the two model ids.
MODEL_ARMS = ("google/gemma-4-31b-it", "deepseek/deepseek-v4-flash-0731")
MODEL_REPS = 3
MODEL_CASES = [
    (
        "coreference",
        "what is its power draw?",
        KA_BAND_HISTORY,
        lambda r: has(r, "Ka-band"),
    ),
    (
        "repair",
        "whats the C&T uplnk rate",
        (),
        lambda r: has(r, "command and telemetry") and has(r, "uplink"),
    ),
    (
        "leave alone",
        "How much power do the solar arrays generate?",
        (),
        lambda r: r == "How much power do the solar arrays generate?",
    ),
]


async def model_table() -> None:
    print("\n-- decision-model head-to-head " + "-" * 41)
    print(f"   {MODEL_REPS} reps x {len(MODEL_CASES)} cases = "
          f"{MODEL_REPS * len(MODEL_CASES)} trials per model")
    prior_model = settings.decision_model
    prior_flag = settings.rewrite_every_turn
    settings.rewrite_every_turn = True
    try:
        for model in MODEL_ARMS:
            settings.decision_model = model
            # **Without this the second arm silently measures the first model.**
            # The chain is built once and cached on nothing.
            get_contextualizer.cache_clear()
            parsed = correct = 0
            total = 0
            latencies: list[float] = []
            for label, question, history, predicate in MODEL_CASES:
                for _ in range(MODEL_REPS):
                    total += 1
                    t0 = time.perf_counter()
                    got = await contextualize_question(question, history)
                    latencies.append(time.perf_counter() - t0)
                    if got is None:
                        continue
                    parsed += 1
                    if predicate(got):
                        correct += 1
                    else:
                        print(f"          {label} MISS: {ascii_safe(repr(got))}")
            p50 = statistics.median(latencies) if latencies else 0.0
            print(f"   {model:34s} parsed {parsed}/{total}  "
                  f"correct {correct}/{total}  p50 {p50:.2f}s")
    finally:
        settings.decision_model = prior_model
        settings.rewrite_every_turn = prior_flag
        get_contextualizer.cache_clear()


# --------------------------------------------------------------------------

async def main_async(args) -> int:
    structural()
    if args.models:
        await model_table()
    elif not args.structural_only:
        await behavioural()

    print("\n" + "=" * 72)
    if failures:
        print(f"{len(failures)} FAILED: " + ", ".join(failures))
        return 1
    print("all cases passed")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Harness for the question rewriter.")
    p.add_argument(
        "--structural-only",
        action="store_true",
        help="Cases 1-5 only. No network, no key, milliseconds.",
    )
    p.add_argument(
        "--models",
        action="store_true",
        help="Re-run the decision-model head-to-head instead of cases 6-11.",
    )
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
