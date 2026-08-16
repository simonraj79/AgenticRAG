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

import logging
import uuid

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from sqlalchemy import select

from app.config import settings
from app.db.models import Agent, Handout
from app.db.session import SessionLocal
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

# The generation cap for a code-writing call, deliberately above
# `settings.generation_max_tokens` (2,048).
#
# That default sizes an ANSWER -- a few paragraphs with citations. A python-pptx
# script for an eight-slide deck is a long, repetitive program, and a truncated
# one is the worst possible failure shape here: it is syntactically plausible
# right up to the point it stops, so it fails inside the sandbox with an error
# the model cannot fix on the retry (the model is not told it was cut off, only
# that its code raised). Paying for the headroom is far cheaper than paying for
# a wasted retry.
CODE_MAX_TOKENS = 4_096

# The separator between attempts in `handouts.source_code`. A Python comment, so
# that the stored field is still a valid-looking Python file a user can copy out
# and read top to bottom -- which is the point of storing it at all.
ATTEMPT_SEPARATOR = "\n\n# " + "-" * 66 + "\n# ATTEMPT {n}\n# {note}\n# " + "-" * 66 + "\n\n"


# --------------------------------------------------------------------------
# Model plumbing
# --------------------------------------------------------------------------


def _model_for(agent: Agent):
    """The chat model for this agent, with the generation sampling config.

    `build_chat_model` rather than `pipeline.get_chat_model`, and the difference
    is `max_tokens`: this call writes a program, not an answer (see
    CODE_MAX_TOKENS). Everything else is deliberately identical to a generation
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
    """
    return build_chat_model(
        agent.generation_model or settings.generation_model,
        temperature=settings.generation_temperature,
        top_p=settings.generation_top_p,
        top_k=settings.generation_top_k,
        max_tokens=CODE_MAX_TOKENS,
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


async def _generate(model, messages: list[BaseMessage]) -> str:
    """One model call, returned as plain text with any fence removed."""
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
    return _strip_fence(content)


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
    refusal = sandbox.static_check(code)
    if refusal is not None:
        log.info("Handout code refused by the static check: %s", refusal.split(".")[0])
        return sandbox.SandboxResult(
            ok=False,
            exit_code=-1,
            stdout="",
            stderr="",
            artifacts=[],
            duration_ms=0,
            error=refusal,
            error_kind="import",
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
    return None


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

            material = await gather_material(db, agent, brief, conversation_id)
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

            model = _model_for(agent)
            messages: list[BaseMessage] = [
                SystemMessage(content=SYSTEM_PREAMBLE),
                HumanMessage(content=render(recipe, brief=brief, material=material)),
            ]

            if recipe.uses_sandbox:
                content, filename, preview, source_code, attempts = await _run_sandbox_recipe(
                    model, messages, recipe
                )
            else:
                content, filename, preview, source_code, attempts = await _run_direct_recipe(
                    model, messages, recipe, brief
                )

            handout.content = content
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
        failure = str(exc) or exc.__class__.__name__
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
        await _settle(agent_id, handout_id, failure)


async def _run_sandbox_recipe(
    model,
    messages: list[BaseMessage],
    recipe: Recipe,
) -> tuple[bytes, str, str | None, str, int]:
    """Write code, run it, and on failure fix it once. Returns the artefact.

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
    code = await _generate(model, messages)
    attempts = [(code, "first attempt")]
    result = await _attempt(code)
    artifact = _primary_artifact(recipe, result.artifacts)
    problem = _problem(recipe, result, artifact)

    if problem is not None:
        log.info(
            "Handout %s attempt 1 failed (%s); retrying once with the traceback",
            recipe.key,
            result.error_kind or "no artefact",
        )
        messages = messages + [
            # The failed code goes back as the AI turn it actually was, not
            # quoted inside the human turn. The model is then correcting its own
            # last message rather than editing a snippet somebody handed it,
            # which is the framing it follows most reliably -- and it keeps the
            # transcript honest for anything that later reads it.
            AIMessage(content=code),
            _repair_message(code, result, problem),
        ]
        code = await _generate(model, messages)
        attempts.append(
            (code, f"retry after: {result.error_kind or 'no artefact produced'}")
        )
        result = await _attempt(code)
        artifact = _primary_artifact(recipe, result.artifacts)
        problem = _problem(recipe, result, artifact)

    source_code = _join_attempts(attempts)

    if problem is not None or artifact is None:
        # Both attempts failed. The row goes to `failed` carrying the SECOND
        # problem, and `source_code` is written by the caller only on success --
        # so this message has to be self-contained enough to act on. It is the
        # sandbox's own text where the code raised, which is written for a model
        # and reads perfectly well to a person: it names the module, the line or
        # the limit.
        #
        # `artifact is None` is re-tested rather than assumed from `problem`,
        # because the type checker cannot see that one implies the other and a
        # future third failure mode might break that implication silently.
        raise RuntimeError(problem or "The generated code produced no file.")

    # The caption. `stdout` when the code printed one -- every sandbox prompt
    # invites exactly one short line for this -- and None otherwise, so the
    # panel renders no caption rather than an empty one.
    preview = result.stdout.strip() or None

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
    """
    text = await _generate(model, messages)
    if not text.strip():
        raise RuntimeError("The model returned an empty study sheet.")

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
    agent_id: uuid.UUID, handout_id: uuid.UUID, message: str | None
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
            await db.commit()
    except Exception:
        log.exception("Could not mark handout %s failed", handout_id)
