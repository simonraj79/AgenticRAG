"""One handout, generated after the response has already gone out.

`app/rag/jobs.py` solved these problems first for ingest and `app/eval/jobs.py`
followed it; this module follows both rather than inventing a third shape. Take
ids and plain values, open your own session, let nothing escape, and make the
row say what happened.

**Why this is a background job and not a request.** A recipe handout costs one
generation call over a prompt carrying the whole retrieved context, plus -- for
three of the four recipes -- a subprocess that imports matplotlib or python-pptx
and runs the model's code, plus, when that fails, a second generation call and a
second subprocess. CLAUDE.md measures generation alone at 13-45 s depending on
the persona. So the route stages a `pending` row, answers 202, and the panel
polls it, exactly as document upload does.

**The bytes live in this process until they are committed.** A `.pptx` is
harvested into memory by the sandbox, carried here, and written to a bytea
column. `settings.sandbox_max_total_bytes` (15 MB) is therefore the per-job
memory ceiling, and N concurrent handout jobs cost N times that -- the same
property `app/rag/jobs.py` records for upload `data`, and worth knowing before
either cap is raised.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from sqlalchemy import select

from app import storage
from app.config import settings
from app.db.models import Agent, Handout
from app.db.session import SessionLocal
from app.handouts import validate
from app.handouts.recipes import (
    RECIPES,
    SYSTEM_PREAMBLE,
    Recipe,
    gather_material,
    provisional_filename,
    render,
)
from app.rag.llm import build_chat_model
from app.tools import sandbox

log = logging.getLogger("uvicorn.error")

# `error_kind` for a run that produced a file which is not the thing it claims
# to be -- a .pptx with no slides, a .png that is 1x1. The sandbox's five kinds
# ("import", "syntax", "timeout", "runtime", "output") are all about the
# PROCESS; this one is about the ARTEFACT, which is why it is minted here rather
# than there.
#
# A plain string, not an enum: `handouts.status`/`kind`/`origin` are `String(16)`
# with no CHECK by design, so a new value costs nothing at the database. Keep it
# at 16 characters or fewer so it stays promotable to a column if it is ever
# worth one.
ERROR_KIND_INVALID = "invalid"

# THE GENERATION CAP IS `settings.handout_code_max_tokens` (4,096). It was the
# module constant `CODE_MAX_TOKENS` here until this feature, and it moved for one
# reason: the retry has to be able to RAISE it.
#
# What the cap is multiplied by on a retry that was triggered by a truncated
# reply -- and only then.
#
# **TWO, and the obvious justification for a bigger number is wrong.** The reflex
# is that a provider output ceiling bounds this; it does not. Checked 2026-08-17
# against the two models `agents.generation_model` can name in practice:
# `max_completion_tokens` is 393,216 on `deepseek/deepseek-v4-flash-0731` and
# 262,144 on `google/gemma-4-31b-it`. Four times the cap, or forty, would route
# and execute perfectly well. So the discipline has to come from somewhere else.
#
# It comes from what a truncation MEANS here. A correct six-slide deck program is
# ~2,000-3,000 characters, roughly 600-900 tokens, so 4,096 is already four to
# six times the honest program: a run that exhausts it is not a long deck, it is
# an inflating one. Both live observations bear that out -- each happened only
# with the slide floor forced to 40, i.e. with the model asked for something
# absurd. Doubling gives any honest deck room it cannot plausibly need, while
# keeping the second attempt's latency and token spend bounded on a job the user
# is already watching a spinner for.
#
# **The multiplier is half of the fix and the smaller half.** The other half is
# `TRUNCATION_NOTE`, which tells the model it was cut off and asks for a shorter
# program. A raised cap on its own buys a longer version of the same wrong
# program; the note on its own asks the model to fit into a budget that already
# defeated it. Neither works alone, which is why they ship together and why
# `scripts/deck_check.py` case 41 asserts both in one case.
TRUNCATION_RETRY_MULTIPLIER = 2

# `finish_reason` values that mean "the model was still writing when it stopped".
#
# **READ DEFENSIVELY, AND AN UNRECOGNISED VALUE MEANS NOT TRUNCATED.**
# langchain-openai puts the field in `response_metadata["finish_reason"]`, but
# OpenRouter passes through whatever the routed provider said and providers do
# not agree: `"length"` is the OpenAI spelling, `"MAX_TOKENS"` the Google-native
# one, `"max_tokens"` Anthropic's. Comparison is on the lower-cased string so a
# spelling difference is not a capability difference.
#
# The asymmetry that sets the default (`loop.md` T3, and it is not symmetric
# here): a FALSE POSITIVE raises the cap and rewords the repair turn on a run
# that was never cut off -- one slightly larger retry, on a turn that was failing
# anyway. A FALSE NEGATIVE is exactly today's behaviour, which is the defect
# being fixed. So an unknown string reads as "finished", and the cost of being
# wrong about a provider nobody has seen yet is one ordinary retry.
_TRUNCATED_FINISH_REASONS = frozenset({"length", "max_tokens", "max_output_tokens"})

# The separator between attempts in `handouts.source_code`. A Python comment, so
# that the stored field is still a valid-looking Python file a user can copy out
# and read top to bottom -- which is the point of storing it at all.
ATTEMPT_SEPARATOR = "\n\n# " + "-" * 66 + "\n# ATTEMPT {n}\n# {note}\n# " + "-" * 66 + "\n\n"


class HandoutFailure(RuntimeError):
    """A handout failure that still knows what KIND of failure it was.

    The sandbox computes `SandboxResult.error_kind` on every run and, until
    this change, **nothing ever read it**: `_run_sandbox_recipe` raised a bare
    `RuntimeError` carrying only the message, so by the time the row was marked
    `failed` the distinction between "the code raised", "it ran and saved
    nothing" and "it saved something that will not open" had been thrown away.
    All three then read as one flat `error` string in the panel.

    A `RuntimeError` subclass rather than a new control-flow shape, so the
    existing `except Exception` in `run_handout_job` catches it unchanged and
    nothing about the raise-to-`_settle` path moves.

    **`source_code` is carried on the exception because there is nowhere else
    for it to go.** `run_handout_job` assigns `handout.source_code` only on the
    success path -- the raise happens first, so a FAILED row stored nothing, and
    `len(source_code) == 0` was measured on two of them. That is the code a user
    most needs to read: `04-handouts-panel.md` promises "both attempts are joined
    by `ATTEMPT_SEPARATOR`; that is what a user reads to see the retry", and it
    was true only of rows that did not need the retry. The attempts have to
    survive the raise to reach `_settle`, and an attribute on the exception is
    the only path between the two that already exists.
    """

    def __init__(
        self,
        message: str,
        *,
        error_kind: str | None = None,
        attempts: int = 0,
        source_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.error_kind = error_kind
        self.attempts = attempts
        self.source_code = source_code


# --------------------------------------------------------------------------
# Model plumbing
# --------------------------------------------------------------------------


def _model_for(agent: Agent, *, max_tokens: int | None = None):
    """The chat model for this agent, with the generation sampling config.

    `build_chat_model` rather than `pipeline.get_chat_model`, and the difference
    is `max_tokens`: this call writes a program, not an answer (see
    `settings.handout_code_max_tokens`). Everything else is deliberately
    identical to a generation
    turn -- the same model, and Gemma 4's card values for temperature, top_p and
    top_k, which CLAUDE.md is emphatic are ONE standardized configuration rather
    than three knobs. The reflex here is to drop temperature to 0 "because it is
    code"; Gemma is not calibrated for that, and grounding is what stops a
    handout inventing figures, not determinism.

    `top_k` is passed and may be dropped: `build_chat_model` strips it for the
    Gemini family, because under `provider.require_parameters` a `top_k` that no
    Gemini endpoint advertises leaves zero eligible providers and 404s -- and now
    for DeepSeek too, where it does NOT 404 but silently routes around the only
    endpoint with a 10x cheaper cache. Agents can be pointed at any of the three,
    so this call site must not decide.

    `reasoning` reads the same setting the chat path does, and that shared value
    is a measurement rather than an economy. The expectation was that this call
    site -- which writes a program -- would want thinking ON where a generation
    turn does not. Measured 2026-08-16 on `deepseek/deepseek-v4-flash-0731`, 6
    chart recipes per arm, scoring the outcome this module already triggers on
    (`chart.png` present on the FIRST attempt, not "the call succeeded"):

        reasoning on   ->  5/6 first try,  p50 30.4s
        reasoning off  ->  6/6 first try,  p50  8.1s

    Off won on both axes, so there is no second setting. Note WHAT was scored: a
    first-attempt miss is invisible to an "did it error" check, because `_problem`
    catches it and the retry usually succeeds -- the run still ends `ready`. It
    costs a whole extra model call and sandbox run, and only the artefact-absence
    question surfaces it. That is `new features/loop.md` T2 applied to a config
    choice rather than to a feature.

    **`max_tokens` is a parameter because the truncation retry moves it.** It
    defaults to `settings.handout_code_max_tokens` so every existing call site
    reads the same number it always did; the only caller that passes anything
    else is the second attempt of a run whose first attempt was cut off. Note
    that `build_chat_model` sends it through `extra_body` rather than as
    `ChatOpenAI(max_tokens=...)`, which is the difference between a working
    request and a 404 -- the reasoning is at that call site and is not repeated
    here.
    """
    return build_chat_model(
        agent.generation_model or settings.generation_model,
        temperature=settings.generation_temperature,
        top_p=settings.generation_top_p,
        top_k=settings.generation_top_k,
        max_tokens=max_tokens or settings.handout_code_max_tokens,
        reasoning=settings.generation_reasoning,
    )


def _strip_fence(text: str) -> str:
    """Unwrap a whole-reply markdown fence, if there is one.

    Gemma fences things. CLAUDE.md records this as the failure that made
    `with_structured_output(method="function_calling")` mandatory for the
    rewrite decision -- the model emits perfectly correct output wrapped in
    ```` ```python ````, and a strict consumer sees garbage. There is no
    structured-output channel to hide behind here, because the payload is source
    code, so the fence has to be stripped by hand.

    **Only a fence that wraps the ENTIRE reply is removed.** A study sheet may
    legitimately contain fenced code blocks of its own, and eating those would
    silently corrupt the artefact -- so the count of fence markers has to be
    exactly two, opening the first line and closing the last.
    """
    stripped = text.strip()
    if not stripped.startswith("```") or not stripped.endswith("```"):
        return stripped
    if stripped.count("```") != 2:
        return stripped

    body = stripped[3:-3]
    # ```python / ```py / ```markdown -- drop the language tag, which is on the
    # opening line and is never part of the payload.
    first_newline = body.find("\n")
    if first_newline == -1:
        return body.strip()
    first_line = body[:first_newline].strip()
    if first_line and " " not in first_line:
        body = body[first_newline + 1 :]
    return body.strip()


@dataclass(frozen=True)
class Generation:
    """One model reply: the text, and whether the model got to FINISH writing it.

    A record rather than a bare `str`, because "the model stopped" and "the
    model was stopped" are different facts that produce identical-looking code.
    A truncated program is syntactically plausible right up to the point it
    ends, so the sandbox reports a `SyntaxError` on the line where the string
    ran out -- and the model, told only that its code raised, goes and debugs a
    line that is fine.

    `finish_reason` is kept beside the flag rather than discarded once it has
    been interpreted, so a log line can name the value that was actually
    received. `_TRUNCATED_FINISH_REASONS` is a guess about other people's
    providers, and a guess that cannot be seen is a guess that cannot be
    corrected.
    """

    text: str
    truncated: bool = False
    finish_reason: str | None = None


def _finish_reason(response) -> str | None:
    """The provider's stop reason, lower-cased, or None if it did not say.

    Every access is defended, because none of this is ours: `response_metadata`
    is populated from whatever OpenRouter passed through from whichever provider
    routing landed on, and this function is on the path of every handout. It
    returns None rather than raising on any shape it does not recognise --
    "the provider did not tell us" is a legitimate answer and is the same answer
    a missing key gives.
    """
    meta = getattr(response, "response_metadata", None)
    if not isinstance(meta, dict):
        return None
    raw = meta.get("finish_reason")
    if raw is None:
        # The other spelling that exists in the wild. OpenAI shipped
        # `finish_details: {"type": ...}` on one model family and langchain
        # passes the whole metadata dict through untouched, so a provider
        # imitating that shape reaches here with `finish_reason` absent.
        details = meta.get("finish_details")
        if isinstance(details, dict):
            raw = details.get("type")
    if raw is None:
        return None
    return str(raw).strip().lower() or None


async def _generate(model, messages: list[BaseMessage]) -> Generation:
    """One model call: the text with any fence removed, and whether it was cut off."""
    response = await model.ainvoke(messages)
    content = response.content
    # `content` is a str for every model this project uses, but the type is
    # `str | list[...]` on the base class and a content-block list would
    # stringify into something unrunnable rather than failing.
    if not isinstance(content, str):
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        )
    reason = _finish_reason(response)
    return Generation(
        text=_strip_fence(content),
        truncated=reason in _TRUNCATED_FINISH_REASONS,
        finish_reason=reason,
    )


# WHAT THE MODEL IS TOLD WHEN IT RETURNED NOTHING TO RUN.
#
# **Neither string may mention a save call or a filename**, and that prohibition
# is the entire point of the branch rather than a style note. Measured on a live
# run, read back out of `handouts.source_code`: attempt 1 was the EMPTY STRING,
# `static_check("")` accepted it (an empty program is a valid, empty AST), a
# subprocess was spawned to run nothing, `_problem`'s branch 2 fired perfectly
# correctly -- and `_repair_message` then told the model to "check that the save
# call is actually reached and that the filename matches", about a program it had
# never written. The trigger was right and the diagnosis was wrong, which is the
# worse of the two failures: it sends the model to debug a line that does not
# exist. `scripts/deck_check.py` case 43 asserts the absence of those words in
# the message AND in the turn the model actually reads.
#
# The run recovered on attempt 2, so the row ended `ready` and the whole episode
# was invisible to every error-shaped check in the repository. Its only witness
# was `meta["attempts"] == 2`, which nothing reads -- `loop.md` T2 again.
# These are the DIAGNOSIS only; `_no_code_message` adds the instruction once.
# Splitting them that way is not tidiness -- the same string is also written to
# `handouts.error` and read by a PERSON on a failed card, where "reply with the
# complete Python file" is an instruction addressed to nobody. Found by printing
# the assembled turn and reading it, which is `build.md`'s last verification
# step: the first draft carried the instruction in both halves and said "reply
# with the complete Python file and nothing else" twice in four lines. Every
# assertion in `deck_check.py` case 43 passed over it.
NO_CODE_PROBLEM = (
    "You replied with NO CODE AT ALL. The reply was empty, so nothing ran and "
    "nothing could have been produced -- this is not a bug in a program, "
    "because there was no program."
)

# The same absence, arriving the other way. `finish_reason == "length"` with
# empty content is a truncation at zero tokens -- the model spent its whole
# budget on something that was not code, most often a reasoning preamble -- so it
# gets the plain-empty message's diagnosis and section D's raised cap both.
NO_CODE_TRUNCATED_PROBLEM = (
    "You replied with NO CODE AT ALL, because your reply was CUT OFF at the "
    "output token limit before any code arrived. Nothing ran and nothing could "
    "have been produced. Begin with the code itself this time -- no preamble, "
    "no plan, no explanation -- and keep the program as short as the brief "
    "allows."
)

# Appended to whatever the sandbox said, when the program that failed was one the
# model never finished writing.
#
# **Without this the raised cap is wasted.** The model is otherwise told only
# that its code raised, so it reads a `SyntaxError` on the line where the string
# ran out and looks for a bug there. Naming the cut-off is what turns the extra
# budget into a different program rather than a longer identical one. Observed
# live twice: "Python syntax error on line 322: unterminated string literal",
# both times on a deck the model was inflating.
TRUNCATION_NOTE = (
    "AND YOUR REPLY WAS CUT OFF. You reached the output token limit before you "
    "finished writing the program, so it stops part way through -- the error "
    "above is WHERE IT STOPPED, not what is wrong with it, and there is no bug "
    "on that line to find. You have more room this time. Use it to finish, and "
    "write a shorter program so that finishing is easy: fewer slides and fewer "
    "bullets beat a program that never ends."
)


def _generation_problem(generation: Generation) -> str | None:
    """Is there anything to run at all? Asked BEFORE a subprocess is paid for.

    **Deliberately not part of `_problem`, and the layer is the point.**
    `_problem` answers "what went wrong with one attempt", and by the time it
    runs the subprocess has already been spawned, the static check has already
    passed an empty AST, and the retry is already going to be told about a file
    that was never going to be written. This is a check on the GENERATION, one
    layer up, and merging the two would keep every symptom while moving the
    code.

    Feature 02's validator and this are both `loop.md` T2 -- trigger on the
    absence of the outcome you wanted -- but they are absences of different
    things at different layers, and they need different sentences.
    """
    if generation.text.strip():
        return None
    return NO_CODE_TRUNCATED_PROBLEM if generation.truncated else NO_CODE_PROBLEM


def _nothing_ran(problem: str) -> sandbox.SandboxResult:
    """The result of not running anything, so the retry path has one shape.

    A hand-built `SandboxResult` rather than a `None` threaded through every
    caller. There is precedent one function down: `_attempt` returns exactly
    this shape for a statically-refused program, which also never reaches a
    process. `exit_code=-1` is that same "no process existed" marker.

    `error_kind="output"` is the sandbox's own word for "it ran and produced
    nothing usable", and `_run_direct_recipe` already uses it for an empty study
    sheet. A new kind would be more precise and would cost a `String(16)` value
    with no label on the card (`PLAN.md` 3.4) for a distinction the message
    already makes in words.
    """
    return sandbox.SandboxResult(
        ok=False,
        exit_code=-1,
        stdout="",
        stderr="",
        artifacts=[],
        duration_ms=0,
        error=problem,
        error_kind="output",
    )


def _no_code_message(problem: str) -> HumanMessage:
    """The retry turn when there is no code to quote back.

    `_repair_message` opens with "here is exactly what you wrote" and a fenced
    block; over an empty reply that renders an empty fence, which reads as a
    program the model wrote and cannot see anything wrong with. So this is a
    separate message rather than a branch inside that one -- and keeping
    `_repair_message` unchanged also keeps `deck_check.py` case 15's assertion
    about verbatim tracebacks measuring what it was written to measure.
    """
    return HumanMessage(
        content="\n".join(
            [
                problem,
                "",
                "Reply with the complete Python file and nothing else: no "
                "prose, no explanation, and not an empty reply. The grounding "
                "rules and the MATERIAL from the first message still apply in "
                "full. This is your last attempt, so prefer the simplest thing "
                "that works over the best thing that might not.",
            ]
        )
    )


def _repair_message(
    code: str, result: sandbox.SandboxResult, problem: str
) -> HumanMessage:
    """The retry turn: here is what you wrote, here is what happened, fix it.

    **This is the single most valuable property of a code interpreter** -- the
    model reads its own `NameError` and corrects it -- and it is why one retry
    is worth having at all. The traceback goes in verbatim, not summarised: the
    line number and the exception type are the whole of the signal, and a
    paraphrase throws away the part that makes the fix mechanical.

    `problem` rather than `result.error`, because the two are not the same set.
    A run that raised has an error; a run that succeeded and wrote no file does
    not, and that is the failure the model is most able to fix -- see
    `_problem`.

    stderr is capped by the sandbox at `settings.sandbox_max_output_chars`
    before it reaches here, so this cannot blow the prompt out.
    """
    parts = [
        "That code did not work. Here is exactly what you wrote:",
        "",
        "```python",
        code,
        "```",
        "",
        f"And here is what happened ({result.error_kind or 'no output'}):",
        "",
        problem,
    ]
    if result.stderr.strip():
        parts += ["", "stderr:", result.stderr.strip()]
    if result.stdout.strip():
        parts += ["", "stdout:", result.stdout.strip()]
    parts += [
        "",
        "Fix it and reply with the corrected complete file. Reply with code and "
        "nothing else. Do not apologise, do not explain the fix, and do not "
        "change what the artefact contains -- the grounding rules and the "
        "MATERIAL from the first message still apply in full. This is your last "
        "attempt, so prefer the simplest thing that works over the best thing "
        "that might not.",
    ]
    return HumanMessage(content="\n".join(parts))


# --------------------------------------------------------------------------
# Sandbox
# --------------------------------------------------------------------------


async def _attempt(code: str) -> sandbox.SandboxResult:
    """Static check, then run. Never raises.

    `sandbox.run` performs the same static check internally and would produce
    the same message, so this is not a second guard -- it is a *distinction*.
    A refusal means the model wrote a disallowed import or a blocked name, which
    is a prompt problem worth logging as one, and it comes back without a
    workdir or a process ever existing. A `run()` failure means the code was
    permitted and something went wrong while it executed. Both feed the same
    retry, and the log line tells them apart afterwards.

    The blocking half -- `subprocess.run` with a wall clock -- is already pushed
    onto a worker thread inside `sandbox.run`, so this await does not pin the
    event loop. That matters more here than almost anywhere: Render's starter
    plan runs a single uvicorn worker, and a background task that blocks the
    loop stalls every request the service is meanwhile trying to serve.
    """
    # `_static_refusal` returns (message, kind) and the kind is already correct --
    # "import" for a blocked module, "syntax" for a program that will not parse.
    # This used to call `static_check`, which discards the kind, and then
    # hard-coded "import" for both.
    #
    # That was invisible until `error_kind` reached the user (feature 04), and
    # then it was actively misleading: a TRUNCATED program -- the failure
    # `settings.handout_code_max_tokens` exists to prevent, which is a syntax
    # error by the time it
    # gets here -- was reported on the card as "blocked import", i.e. "the model
    # reached for something unavailable". It sent the reader to the allowlist to
    # explain a program that simply stopped mid-string.
    #
    # Observed twice while writing the layer-2 scenarios, on a floor deliberately
    # raised high enough to make the model inflate the deck until it ran out of
    # tokens. Both halves of that are real and this is only the reporting half.
    refusal = sandbox._static_refusal(code)
    if refusal is not None:
        message, kind = refusal
        log.info("Handout code refused by the static check: %s", message.split(".")[0])
        return sandbox.SandboxResult(
            ok=False,
            exit_code=-1,
            stdout="",
            stderr="",
            artifacts=[],
            duration_ms=0,
            error=message,
            error_kind=kind,
        )
    return await sandbox.run(code)


def _primary_artifact(
    recipe: Recipe, artifacts: list[sandbox.SandboxArtifact]
) -> sandbox.SandboxArtifact | None:
    """Pick the file the handout IS, out of everything the run wrote.

    Three tiers, deliberately forgiving in that order:

    1. The exact name the prompt asked for.
    2. Anything with the recipe's extension. A model that writes `figure.png`
       instead of `chart.png` has produced a perfectly good chart, and burning a
       20-second retry to correct a filename would be a worse outcome than the
       one it fixes.
    3. Nothing. A `.csv` where a `.png` was asked for is not a chart, and
       storing it under `image/png` would produce a handout that downloads and
       then fails to open -- which is harder to diagnose than a failed row.

    The sandbox already refuses to harvest anything outside its own suffix
    allowlist, so tier 2 cannot pick up a `.pyc` or a `__pycache__` entry.
    """
    for artifact in artifacts:
        if artifact.filename == recipe.output_filename:
            return artifact
    for artifact in artifacts:
        if artifact.filename.lower().endswith(recipe.extension):
            return artifact
    return None


def _problem(
    recipe: Recipe,
    result: sandbox.SandboxResult,
    artifact: sandbox.SandboxArtifact | None,
) -> str | None:
    """What went wrong with one attempt, or None if nothing did.

    **The second branch is the one worth having, and it is easy to leave out.**
    `SandboxResult.ok` answers "did the process exit cleanly", which is not the
    question this job is asking -- code that computes a chart perfectly and then
    forgets `plt.savefig` exits 0, harvests nothing, and is a total failure of
    the handout. Keying the retry on `ok` alone gives that case no second
    attempt at all, which is backwards: a forgotten save is among the MOST
    recoverable failures there is, far more so than a genuine crash, because the
    fix is one line the model already knows how to write.

    So the retry trigger is "the attempt did not produce the artefact", and a
    crash is only one of the two ways to fail that.

    The message is written for the model, in the same register as the sandbox's
    own refusals: name what happened, name what was expected, and be specific
    enough that the next attempt is a correction rather than a re-roll.

    **The THIRD branch is the same ladder one rung further up.** Branch 2 asks
    "is the artefact absent"; branch 3 asks "is the present one actually the
    thing". Measured 2026-08-17: a `Presentation()` with zero slides saves as
    27,387 bytes starting `PK`, and 28 bytes of junk starting `PK` also harvest
    -- both became `ready` handouts that nothing in the repository could tell
    from a real deck. `app/handouts/validate.py` opens them.

    The flag is not a product option. It exists so that the regression
    assertion can be executed: with it off this function returns exactly what it
    returned before the branch was added, for every input, which is
    `scripts/deck_check.py` case 25 and `PLAN.md` 3.6 R-a. Handout bytes cannot
    be compared between runs -- temperature 1.0, and a .pptx is a zip carrying
    timestamps -- so a pure function is the only place "identical with the
    feature off" can actually be asserted.
    """
    if not result.ok:
        return result.error or "The generated code did not run."
    if artifact is None:
        wrote = ", ".join(a.filename for a in result.artifacts) or "no files at all"
        return (
            f"The code ran without error but wrote no {recipe.extension} file. "
            f"It wrote: {wrote}. The handout needs exactly one file named "
            f"`{recipe.output_filename}` in the current directory -- check that "
            "the save call is actually reached and that the filename matches."
        )
    if settings.handout_validate_artifacts:
        invalid = validate.check(recipe, artifact)
        if invalid is not None:
            return invalid
    return None


def _failure_kind(
    result: sandbox.SandboxResult, artifact: sandbox.SandboxArtifact | None
) -> str:
    """Which of `_problem`'s three branches produced the failure.

    Kept beside `_problem` and reading the same two arguments, so the two cannot
    disagree about which branch fired. It is a separate function rather than a
    second return value because `_problem`'s signature is asserted directly by
    `scripts/deck_check.py` cases 4, 5, 6 and 25 -- the regression contract is
    written against `str | None`, and widening it to a tuple would rewrite the
    baseline this feature exists to preserve.

    Called only when `_problem` returned a string, so the fall-through is
    unreachable in practice; it returns the artefact verdict rather than raising,
    because nothing in this module may raise.
    """
    if not result.ok:
        return result.error_kind or "runtime"
    if artifact is None:
        # The sandbox's own word for "ran, produced nothing usable".
        return "output"
    return ERROR_KIND_INVALID


def _preview_for(
    recipe: Recipe,
    result: sandbox.SandboxResult,
    artifact: sandbox.SandboxArtifact,
) -> str | None:
    """What the panel shows inline for a sandbox recipe: an outline, else stdout.

    **The outline wins and the caption is the fallback, in that order.** Both
    describe the same artefact and only one of them was read off it: every
    sandbox prompt invites the model to `print()` one short line, so the caption
    is the model's CLAIM about what it made, while `validate.outline` is what the
    bytes actually contain. On the measured failure this change set exists for --
    a `Presentation()` with zero slides -- the claim reads "deck written with 6
    slides" and the outline reads "0 slides". Preferring the claim would put the
    lie on the card.

    **The caption is not discarded, and that half is deliberate too.** Two of the
    four recipes have no outline function at all (`chart` renders its own PNG as
    a thumbnail; `sheet` never reaches here), so for them this returns exactly
    what this line returned before the outline existed -- `.strip()`, and `None`
    rather than `""` so the panel renders no caption block instead of an empty
    one. `scripts/deck_check.py` case 56 pins that byte for byte.

    **Not gated on `handout_validate_artifacts`.** That flag is the regression
    switch for `_problem` (`PLAN.md` 3.6 R-a); the preview is a product feature.
    Wiring one to the other would delete every preview the moment somebody
    reproduced a baseline -- `deck_check.py` case 55.

    Nothing here raises, because nothing in this module may. `outline` carries
    the same contract and is defended behind it anyway: a preview must never be
    the reason a finished handout is lost.
    """
    caption = result.stdout.strip() or None

    summary = validate.outline(recipe, artifact)
    # `isinstance` rather than a bare truth test. `outline` promises `str | None`
    # and this is the one call site where a broken promise would be written to
    # the database and rendered -- the same defence `interpreter._validate_artifacts`
    # makes against `check`, and for the same reason: the promise has been broken
    # once before in this repo, by the one function whose whole job was to keep it.
    if isinstance(summary, str) and summary.strip():
        return summary
    return caption


# --------------------------------------------------------------------------
# The job
# --------------------------------------------------------------------------


async def run_handout_job(
    agent_id: uuid.UUID,
    handout_id: uuid.UUID,
    user_id: uuid.UUID | None,
    recipe_key: str,
    brief: str,
    conversation_id: uuid.UUID | None,
) -> None:
    """Fill in one already-staged handout. NEVER raises.

    Takes ids and plain values -- no ORM objects, no session. Both halves are
    load-bearing and are explained where they are enforced below.
    """
    # ------------------------------------------------------------------
    # THIS FUNCTION OPENS ITS OWN SESSION. It cannot be given one.
    #
    # A FastAPI BackgroundTask runs AFTER the response has been sent, and
    # `get_db` is a generator dependency FastAPI closes as the request finishes.
    # A session captured from the request is therefore already closed by the
    # time this line runs, its connection already back in the pool and possibly
    # already lent to another request -- producing `MissingGreenlet`, "attached
    # to a different loop", or a silent write against somebody else's
    # connection. None of those errors mentions background tasks, and all of
    # them surface at a line that has nothing wrong with it.
    #
    # The same reasoning forbids passing an `Agent` or a `Handout`: an ORM
    # object belongs to the session that loaded it, and carrying one across is
    # the same bug wearing the shape of an argument. Everything is re-loaded
    # below, inside the session that will actually use it.
    # ------------------------------------------------------------------
    failure: str | None = None
    # Carried out of the `except` and into the `finally` below, because the row
    # is marked terminal in `_settle` and the exception does not reach it.
    failure_kind: str | None = None
    failure_attempts = 0
    failure_source: str | None = None
    try:
        async with SessionLocal() as db:
            agent = await db.get(Agent, agent_id)
            if agent is None:
                # Deleted between the 202 and this task starting. `handouts`
                # cascades from `agents`, so there is very likely no row left to
                # mark either. `_settle` below will no-op on a missing row.
                log.warning(
                    "Handout job %s: agent %s no longer exists", handout_id, agent_id
                )
                return

            handout = await db.scalar(
                select(Handout).where(
                    Handout.id == handout_id,
                    # Selected on the PAIR. These two ids arrive as separate
                    # arguments and only the agent has been through the
                    # ownership check in `app/api/deps.py`; matching on both
                    # means a mismatched pair fetches nothing rather than
                    # writing one tenant's file into another tenant's panel.
                    #
                    # `content` stays deferred through this load, which is what
                    # keeps a retry cheap: re-running a failed handout does not
                    # drag the previous attempt's megabytes back out of Postgres
                    # in order to overwrite them.
                    Handout.agent_id == agent_id,
                )
            )
            if handout is None:
                log.warning(
                    "Handout job: handout %s not found under agent %s",
                    handout_id,
                    agent_id,
                )
                return

            recipe = RECIPES.get(recipe_key)
            if recipe is None:
                # Unreachable through the API -- `HandoutRequest.recipe` is a
                # `Literal` -- but reachable after a recipe is removed while a
                # row is queued behind it. Fail the row rather than raise, so
                # the panel shows a cause.
                raise ValueError(f"Unknown recipe {recipe_key!r}")

            # `recipe=` supplies the per-call retrieval budget. It is resolved
            # above rather than passed down as `recipe_key`, so the widening is
            # a property of the recipe object the rest of this job already
            # holds and there is no second place for a key to be looked up.
            material = await gather_material(
                db, agent, brief, conversation_id, recipe=recipe
            )
            if material.is_empty:
                # REFUSED, not generated. See `Material.is_empty`: a model given
                # a brief and no material produces a beautifully formatted
                # artefact out of parametric memory, and nothing downstream can
                # tell it from a grounded one. An empty corpus is the state a
                # brand-new agent is in, so this is the common first experience
                # of the panel and it has to say why.
                raise ValueError(
                    "Nothing in this agent's corpus matches that brief, and "
                    "there is no conversation to draw on. Upload a document or "
                    "ask the agent a question first."
                )

            # A FACTORY rather than a model, because the sandbox path may need a
            # second one at a bigger output cap when the first reply was cut off
            # -- see `_run_sandbox_recipe`. `agent` is captured here, inside the
            # session that loaded it, so the factory can be called later without
            # any of the detached-ORM-object hazards this module opens with.
            def build_model(max_tokens: int):
                return _model_for(agent, max_tokens=max_tokens)

            messages: list[BaseMessage] = [
                SystemMessage(content=SYSTEM_PREAMBLE),
                HumanMessage(content=render(recipe, brief=brief, material=material)),
            ]

            if recipe.uses_sandbox:
                content, filename, preview, source_code, attempts = await _run_sandbox_recipe(
                    build_model, messages, recipe
                )
            else:
                # One model at the ordinary cap. The direct path has no retry to
                # raise a cap for, so it takes a model rather than a factory --
                # see `_run_direct_recipe` for why truncation is not actionable
                # there.
                content, filename, preview, source_code, attempts = await _run_direct_recipe(
                    build_model(settings.handout_code_max_tokens),
                    messages,
                    recipe,
                    brief,
                )

            # OBJECT FIRST, ROW SECOND. The key is derivable before the row is
            # written because it is built from `agent_id` and this row's own id,
            # both of which already exist -- which is the whole reason the
            # ordering can be this way round. If the put fails it raises here,
            # the `except` below marks the row `failed`, and no key was ever
            # recorded; if the put succeeds and the commit does not, the
            # `except` deletes what was written.
            #
            # `to_thread` because boto3 is synchronous, exactly like the Pinecone
            # and Cohere clients this module already wraps. Render runs one
            # uvicorn worker, so a blocking call here stalls every other request.
            storage_key: str | None = None
            if storage.enabled():
                storage_key = storage.handout_key(agent.id, handout.id, recipe.mime_type)
                await asyncio.to_thread(
                    storage.put_object, storage_key, content, recipe.mime_type
                )

            # `content` is still written on the R2 road, and that is deliberate
            # rather than leftover. The change set keeps `storage_route=postgres`
            # a working rollback, and a rollback whose bytes were never written
            # is not one. The column is dropped in a later change set, once R2
            # has been trusted in production for a while -- the same blue/green
            # discipline `migrate_index.py` applies to a Pinecone index.
            handout.content = content
            handout.storage_key = storage_key
            handout.byte_size = len(content)
            handout.filename = filename
            handout.mime_type = recipe.mime_type
            handout.preview_text = preview
            handout.source_code = source_code
            handout.meta = {
                # Provenance, per section 2.4: a handout can be traced back to
                # the chunks that produced it the way an answer can.
                "chunk_ids": material.chunk_ids,
                "recipe": recipe.key,
                "brief": brief,
                "model": agent.generation_model or settings.generation_model,
                "conversation_turns": material.turn_count,
                # 1 or 2. Recorded because "it worked first time" and "it worked
                # after reading its own traceback" are different facts about the
                # model, and `source_code` alone makes them look the same to
                # anything that is not a human reading it.
                "attempts": attempts,
            }
            handout.error = None
            handout.status = "ready"
            await db.commit()

            log.info(
                "Handout %s ready: %s, %s bytes, %s attempt(s)",
                handout_id,
                filename,
                len(content),
                attempts,
            )

    except Exception as exc:
        # NOTHING ESCAPES THIS FUNCTION. An exception raised out of a
        # BackgroundTask is returned to nobody -- the response went out minutes
        # ago -- so at best it lands in the log and at worst the task machinery
        # swallows it. Either way the row stays `pending` forever, which reads
        # as progress and never gets investigated.
        log.exception("Handout job failed for handout %s (%s)", handout_id, recipe_key)

        # The fourth step of the write ordering. `storage_key` is only bound
        # once a put has SUCCEEDED, so reaching here with it set means the
        # bytes landed and something after that did not -- most likely the
        # commit. That object is unreferenced by anything and its key is
        # derived from a row that may be about to be marked `failed`, so
        # nothing would ever find it again.
        #
        # `delete_quietly` cannot raise, which matters more here than anywhere
        # else in the codebase: this is an `except` block whose entire job is to
        # leave the row in a terminal state, and an exception thrown while
        # cleaning up would skip `_settle` and strand the row at `pending`
        # forever -- trading a leaked object for the exact failure this
        # try/except exists to prevent.
        if locals().get("storage_key"):
            storage.delete_quietly(locals()["storage_key"])

        failure = str(exc) or exc.__class__.__name__
        if isinstance(exc, HandoutFailure):
            # Everything else that lands here -- an empty corpus, a removed
            # recipe, a dropped connection -- has no sandbox run behind it and
            # therefore no kind to record. `None` is written as absence rather
            # than guessed at, so a `meta` with no `error_kind` means "this did
            # not fail in the sandbox", which is itself the useful fact.
            failure_kind = exc.error_kind
            failure_attempts = exc.attempts
            # The code both attempts tried, which the success path assigns
            # directly and the failure path used to LOSE -- the raise happens
            # before `handout.source_code = ...` is ever reached, so a `failed`
            # row stored nothing. Measured `len == 0` on two of them.
            failure_source = exc.source_code
    finally:
        # THE TERMINAL STATUS, in a `finally` rather than in the `except`.
        #
        # The `except` covers the failures that raise. This covers the ones that
        # do not: an early `return` above, a `sys.exit` from something exotic,
        # and -- the one that actually happens -- a cancellation, because
        # `CancelledError` is a BaseException and would sail straight past
        # `except Exception`. All of those leave a row at `pending` with nothing
        # coming to move it, which is the single most confusing state available:
        # a spinner that never stops looks like a slow job, not a dead one.
        await _settle(
            agent_id,
            handout_id,
            failure,
            error_kind=failure_kind,
            attempts=failure_attempts,
            source_code=failure_source,
        )


def _retry_note(generation: Generation, result: sandbox.SandboxResult) -> str:
    """The banner above attempt 2 in `source_code`: why there was a second one.

    Read by a person opening "Code" on a two-attempt handout, so it names the
    reason in a reader's terms rather than echoing `error_kind`. The two facts
    `error_kind` cannot express are exactly the two this feature added -- nothing
    came back at all, and the reply was cut off -- and both of them arrive as the
    generic `"output"` otherwise.
    """
    if not generation.text.strip():
        return "cut off before any code" if generation.truncated else "no code returned"
    if generation.truncated:
        return f"{result.error_kind or 'no artefact produced'}, cut off"
    return result.error_kind or "no artefact produced"


async def _evaluate(
    recipe: Recipe, generation: Generation
) -> tuple[sandbox.SandboxResult, sandbox.SandboxArtifact | None, str | None]:
    """Judge one generation -- WITHOUT spending a subprocess on nothing.

    Three questions in the order their answers cost money:

    1. Is there any code? (free, and until this feature nobody asked)
    2. Did running it produce the artefact? (`_attempt` + `_problem`)
    3. If it failed, was the model even finished writing? (free, and it changes
       what the model is told about (2))

    Question 3 comes last and MODIFIES rather than replaces, because a truncated
    program still fails for a real reason and the sandbox's own message is still
    the most useful half of the signal -- it names the line and the exception.
    The note says which of those two facts to act on.
    """
    problem = _generation_problem(generation)
    if problem is not None:
        # No process, no static check, no workdir. The measured defect this
        # replaces spawned one to discover that an empty string writes no files.
        log.info(
            "Handout %s: the model returned no code (finish_reason=%s)",
            recipe.key,
            generation.finish_reason,
        )
        return _nothing_ran(problem), None, problem

    result = await _attempt(generation.text)
    artifact = _primary_artifact(recipe, result.artifacts)
    problem = _problem(recipe, result, artifact)
    if problem is not None and generation.truncated:
        problem = problem + "\n\n" + TRUNCATION_NOTE
    return result, artifact, problem


async def _run_sandbox_recipe(
    build_model,
    messages: list[BaseMessage],
    recipe: Recipe,
) -> tuple[bytes, str, str | None, str, int]:
    """Write code, run it, and on failure fix it once. Returns the artefact.

    **A model FACTORY, not a model**, and that is the whole of section D's second
    half. `build_model(max_tokens)` returns a chat model at that cap, so this
    function can ask for a bigger one when -- and only when -- the reason the
    first attempt failed was that the model ran out of room. A retry at the same
    budget produces the same length and fails identically: `PLAN.md` R7, observed
    live twice. Passing a pre-built model instead would put the cap out of this
    function's reach and leave the retry unable to succeed, which is the state
    this replaces.

    **ONE retry, and one only.** The reasoning is in section 2.5 and it is a
    curve rather than a principle: the first retry recovers most recoverable
    failures, because the overwhelmingly common ones -- a typo, a wrong keyword
    argument, a `NameError` on a variable the model renamed halfway through --
    are exactly the ones a traceback makes mechanical to fix. The second retry
    recovers far less, because what survives the first is usually a model that
    has misunderstood the task rather than mistyped it, and re-reading the same
    traceback does not help with that. Meanwhile every attempt costs a full
    generation call plus a subprocess, on a job the user is already watching a
    spinner for.

    **Both attempts' code is kept, joined, in `source_code`.** A user opening
    "Code" on a handout that took two goes should see the correction, not just
    the version that worked. It is the clearest demonstration in the product of
    what the tool loop actually does -- and when a handout is subtly wrong, the
    first attempt is often where the misunderstanding is legible.

    **The retry fires on "no artefact", not on "the process failed".** See
    `_problem`: code that exits 0 and forgets to save is a total failure of the
    handout and one of the most recoverable ones there is, and keying on
    `SandboxResult.ok` would give it no second attempt at all.
    """
    cap = settings.handout_code_max_tokens
    generation = await _generate(build_model(cap), messages)
    attempts = [(generation.text, "first attempt")]
    result, artifact, problem = await _evaluate(recipe, generation)

    if problem is not None:
        log.info(
            "Handout %s attempt 1 failed (%s%s); retrying once",
            recipe.key,
            result.error_kind or "no artefact",
            ", TRUNCATED" if generation.truncated else "",
        )
        if generation.truncated:
            # THE RETRY THAT CAN ACTUALLY SUCCEED. Everything else about this
            # retry -- the traceback, the note, the framing -- is advice, and
            # advice cannot fit a program into a budget that already defeated it.
            cap = cap * TRUNCATION_RETRY_MULTIPLIER
        if generation.text.strip():
            messages = messages + [
                # The failed code goes back as the AI turn it actually was, not
                # quoted inside the human turn. The model is then correcting its
                # own last message rather than editing a snippet somebody handed
                # it, which is the framing it follows most reliably -- and it
                # keeps the transcript honest for anything that later reads it.
                AIMessage(content=generation.text),
                _repair_message(generation.text, result, problem),
            ]
        else:
            # NO AIMessage, because there was no AI turn worth replaying: an
            # empty assistant message is a turn that says nothing, and some
            # providers reject one outright. `_no_code_message` also avoids
            # `_repair_message`'s "here is exactly what you wrote" plus an empty
            # fence, which reads as a program the model cannot find the fault in.
            messages = messages + [_no_code_message(problem)]
        # Computed BEFORE `generation` is reassigned -- the banner describes the
        # attempt that failed, not the one about to run.
        note = _retry_note(generation, result)
        generation = await _generate(build_model(cap), messages)
        attempts.append((generation.text, f"retry after: {note}"))
        result, artifact, problem = await _evaluate(recipe, generation)

    source_code = _join_attempts(attempts)

    if problem is not None or artifact is None:
        # Both attempts failed. The row goes to `failed` carrying the SECOND
        # problem, and it has to be self-contained enough to act on. It is the
        # sandbox's own text where the code raised, which is written for a model
        # and reads perfectly well to a person: it names the module, the line or
        # the limit.
        #
        # **`source_code` travels ON THE EXCEPTION.** It used to be written by
        # the caller on the success path only, so a `failed` handout stored
        # nothing at all -- measured `len == 0` on two failed rows, which is
        # exactly when somebody wants to read what was tried. `_settle` writes it
        # from its second session; see `HandoutFailure`.
        #
        # `artifact is None` is re-tested rather than assumed from `problem`,
        # because the type checker cannot see that one implies the other and a
        # future third failure mode might break that implication silently.
        raise HandoutFailure(
            problem or "The generated code produced no file.",
            error_kind=_failure_kind(result, artifact),
            attempts=len(attempts),
            source_code=source_code,
        )

    # What the card shows without a download. An outline of the artefact when
    # one can be written -- the deck's slide titles, the table's header -- and
    # otherwise the caption the model printed, which is what this line was
    # before feature 05 and is still all there is for a chart. See
    # `_preview_for`.
    preview = _preview_for(recipe, result, artifact)

    # `artifact.filename` is MODEL-WRITTEN: the model chose the name when its
    # code called `savefig`/`save`/`to_csv`, and the sandbox harvested whatever
    # was on disk. It goes into a `Content-Disposition` header at download time,
    # which is why `app/api/handouts.py` sanitises it there -- at the boundary
    # it escapes through, not here.
    return artifact.content, artifact.filename, preview, source_code, len(attempts)


async def _run_direct_recipe(
    model,
    messages: list[BaseMessage],
    recipe: Recipe,
    brief: str,
) -> tuple[bytes, str, str | None, str | None, int]:
    """The no-sandbox path: the model's markdown IS the artefact.

    The markdown goes into `content` AND `preview_text` -- see the module
    docstring in `recipes.py`. `content` is the downloadable file, encoded UTF-8
    once here so the download route never has to guess an encoding; and
    `preview_text` is what the panel renders inline, because `content` is
    deferred and a preview must not be a reason to load bytea.

    `source_code` is None: there was no code. Storing the prompt there instead
    would be a lie in a column whose whole value is that it shows exactly what
    produced the file.

    There is no retry. Nothing ran, so there is no traceback to feed back, and
    "the model wrote prose I did not like" is not a failure this job can detect
    -- only an empty reply is, and that is a failure a second identical call is
    unlikely to fix.

    **Truncation is READ here and deliberately not acted on.** `_generate`
    reports it for every call, but a cut-off study sheet is still a study sheet:
    the markdown simply stops, and the honest thing is to keep it rather than
    fail a handout the user can read most of. Raising the cap is section D's fix
    for a PROGRAM, where a missing last line is the difference between a file and
    no file. It is recorded in the log so a run of short sheets has somewhere to
    be traced to, and `handout_code_max_tokens` is the knob if it ever matters.
    """
    generation = await _generate(model, messages)
    text = generation.text
    if generation.truncated:
        log.info(
            "Handout %s: the study sheet was cut off at %s tokens; keeping it",
            recipe.key,
            settings.handout_code_max_tokens,
        )
    if not text.strip():
        # `error_kind="output"` -- the sandbox's own word for "it ran and
        # produced nothing usable". There is no sandbox here, but the fact is
        # the same one and giving it a second name would make the two paths
        # unreadable together.
        raise HandoutFailure(
            "The model returned an empty study sheet.",
            error_kind="output",
            attempts=1,
        )

    return (
        text.encode("utf-8"),
        provisional_filename(recipe, brief),
        text,
        None,
        1,
    )


def _join_attempts(attempts: list[tuple[str, str]]) -> str:
    """Both attempts in one field, each under a comment saying which it is.

    A single attempt is stored bare -- no banner -- because the overwhelming
    majority of handouts succeed first time and a lone "ATTEMPT 1" header on
    every one of them is noise that makes the two-attempt case less visible, not
    more.
    """
    if len(attempts) == 1:
        return attempts[0][0]
    return "".join(
        ATTEMPT_SEPARATOR.format(n=index, note=note) + code
        for index, (code, note) in enumerate(attempts, start=1)
    ).lstrip("\n")


async def _settle(
    agent_id: uuid.UUID,
    handout_id: uuid.UUID,
    message: str | None,
    *,
    error_kind: str | None = None,
    attempts: int = 0,
    source_code: str | None = None,
) -> None:
    """Last resort: force a still-`pending` handout to `failed`.

    A SECOND session, deliberately, and not the one the caller was using. The
    reason we are here may be that the first session is the thing that broke --
    a connection dropped mid-commit leaves it unusable and every write attempted
    on it afterwards fails too, turning a recorded failure into an unrecorded
    one. The same guard `app/rag/jobs.py::_mark_failed` and
    `app/eval/jobs.py::_fail_run` both carry.

    Does nothing to a row that already reached a terminal status, which is the
    common case: the job commits `ready` itself, and this then finds `ready` and
    leaves it alone. That check is the whole reason this can live in a `finally`
    rather than only in the `except`.

    Loaded on the `(id, agent_id)` pair for the same reason the job body is.

    Swallows its own errors. A failure to record a failure must not become the
    exception that escapes the background task.
    """
    try:
        async with SessionLocal() as db:
            handout = await db.scalar(
                select(Handout).where(
                    Handout.id == handout_id,
                    Handout.agent_id == agent_id,
                )
            )
            if handout is None or handout.status != "pending":
                return

            handout.status = "failed"
            handout.error = message or (
                "The handout job stopped before it finished, and did not say "
                "why. Try again."
            )

            # THE CODE THAT WAS TRIED, on a row that failed. Until this feature
            # a `failed` handout stored none -- `_run_sandbox_recipe` raises
            # before the caller reaches `handout.source_code = ...`, so both
            # attempts were discarded at exactly the moment somebody wants to
            # read them. Measured `len == 0` on two failed rows.
            #
            # Guarded rather than assigned unconditionally: most callers of
            # `_settle` have no code behind them at all (an empty corpus, a
            # removed recipe, a cancellation), and writing `None` over a value
            # would make the failure path lose it a second way. The `pending`
            # check above already means a row that settled `ready` keeps its own.
            if source_code:
                handout.source_code = source_code

            # `meta` is JSONB and needs no migration -- PLAN.md 3.3. It is
            # REBUILT rather than mutated in place: SQLAlchemy tracks JSONB by
            # identity, so `handout.meta["x"] = y` on the loaded dict is a write
            # the session never notices and never flushes.
            #
            # The success path writes `attempts` at the end of the job; this is
            # the only place a FAILED row gets it, and the pair
            # `(error_kind, attempts)` is what separates "crashed twice", "ran
            # twice and saved nothing", and "produced a file that will not open"
            # -- three different problems that today read as one flat string.
            meta = dict(handout.meta or {})
            if error_kind:
                meta["error_kind"] = error_kind
            if attempts:
                meta["attempts"] = attempts
            handout.meta = meta

            await db.commit()
    except Exception:
        log.exception("Could not mark handout %s failed", handout_id)
