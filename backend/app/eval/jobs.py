"""One eval run, executed after the response has already gone out.

`app/rag/jobs.py` solved these problems first for ingest and this module follows
its shape deliberately rather than inventing a second one: take ids, open your
own session, let nothing escape, and make the row say what happened.

**Why this is a background job and not a request.** CLAUDE.md measures a single
persona-flavoured turn at 30-45 s, of which generation is 89%. A ten-question
golden set therefore costs ten of those, plus up to four judged metric calls per
question on top -- several minutes, comfortably past any proxy timeout and far
past a user's patience for a spinner. So the route stages an `eval_runs` row,
returns it, and the client polls `progress_done`/`progress_total`. Those two
columns are the entire reason this is bearable: without them a five-minute run
is indistinguishable from a hung one.

**What one question costs, in rows:** a `conversations` row, a `queries` row,
its `query_chunks`, its `trace_events`, and one `eval_results` row. That is not
incidental -- an eval turn is a real turn, so it is traceable in exactly the same
Trace view as a turn a human asked, and the answer that was scored can be read
back later next to the contexts it was scored against.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import func, select

from app.config import settings
from app.db.models import (
    Agent,
    Chunk,
    Conversation,
    EvalResult,
    EvalRun,
    GoldenQuestion,
    QueryChunk,
    User,
)
from app.db.session import SessionLocal
from app.eval.metrics_guide import METRIC_KEYS, summarise
from app.eval.ragas_runner import EvalTurn, score_samples
from app.metering.context import collect_usage, meter_as
from app.metering import store as metering_store

log = logging.getLogger("uvicorn.error")


async def _contexts_for_query(db, query_id: uuid.UUID) -> list[str]:
    """The chunk text that was actually in the prompt, in rank order.

    Read back from `query_chunks` rather than carried out of the pipeline in
    memory, for two reasons. The scored contexts are then literally the stored
    evidence, so a scorecard and the Trace view can never disagree about what
    grounded an answer. And `AskOut.citations` only carries a 240-character
    preview, which would silently truncate every context the judge sees.

    `query_chunks` holds the FINAL set -- post-rerank -- which is the right input
    for context precision. Handing Ragas the pre-rerank twenty would collapse
    precision by construction and measure the retriever's recall instead of the
    answer's grounding.

    Worth knowing (CLAUDE.md, Ragas section): `query_chunks.chunk_id` cascades
    from `chunks` and thence from `documents`, so deleting a source document
    later empties the contexts behind past runs. This read happens seconds after
    the write, so it is unaffected -- but a re-scoring feature would be.
    """
    rows = await db.execute(
        select(Chunk.text)
        .join(QueryChunk, QueryChunk.chunk_id == Chunk.id)
        .where(QueryChunk.query_id == query_id)
        .order_by(QueryChunk.rank.asc())
    )
    return [text for (text,) in rows.all()]


async def run_eval_job(
    agent_id: uuid.UUID,
    eval_run_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    """Score one agent's golden set into one `eval_runs` row. Never raises.

    Takes ids only -- no ORM objects, no session. Both halves matter and the
    reasoning is the same as `app/rag/jobs.py`'s, restated because getting it
    wrong here fails in a way that looks like a database bug.
    """
    # ------------------------------------------------------------------
    # THIS FUNCTION OPENS ITS OWN SESSION. It cannot be given one.
    #
    # A FastAPI BackgroundTask runs AFTER the response has been sent, and
    # `get_db` is a generator dependency that FastAPI closes as the request
    # finishes. A session captured from the request is therefore already closed
    # by the time this line runs, its connection already back in the pool and
    # possibly already lent to another request -- producing `MissingGreenlet`,
    # "attached to a different loop", or a silent write against somebody else's
    # connection. None of those errors mentions background tasks.
    #
    # The same reasoning forbids passing an `Agent` or an `EvalRun`: an ORM
    # object belongs to the session that loaded it. Everything is re-loaded
    # below, inside the session that will actually use it.
    # ------------------------------------------------------------------
    # **The most expensive thing this system does, and it was the easiest to
    # leave unmetered.** A ten-question run is 23-25 minutes: ten full agent
    # turns plus four judged metric calls each, on a THIRD vendor's judge. None
    # of it belongs to a `queries` row an admin would find, which is exactly why
    # the metering unit is a call rather than a turn -- a turn-shaped unit would
    # have shown a complete-looking total with the eval spend silently missing.
    #
    # `inherit=False`: this task may run inside a request's context, and eval
    # spend belongs to the run, not to whatever turn happened to be in flight.
    with collect_usage() as usage_records, meter_as(
        user_id=user_id, agent_id=agent_id, call_kind="judge", inherit=False
    ):
      try:
          async with SessionLocal() as db:
              agent = await db.get(Agent, agent_id)
              run = await db.scalar(
                  select(EvalRun).where(
                      EvalRun.id == eval_run_id,
                      # Selected on the pair. The two ids arrive as separate
                      # arguments and only the agent has been through the
                      # ownership check in `app/api/deps.py`, so matching on both
                      # means a mismatched pair fetches nothing rather than
                      # scoring one tenant's corpus into another tenant's run.
                      EvalRun.agent_id == agent_id,
                  )
              )
              user = await db.get(User, user_id)

              if run is None:
                  log.warning(
                      "Eval job: run %s not found under agent %s", eval_run_id, agent_id
                  )
                  return
              if agent is None or user is None:
                  # Deleted between the response and this task starting. `eval_runs`
                  # cascades from `agents`, so when the agent is gone there is
                  # probably no row left to mark either -- but when only the user
                  # is gone the run survives (`user_id` is SET NULL) and must be
                  # closed out rather than left at `running` forever.
                  await _fail_run(eval_run_id, "Agent or user no longer exists.")
                  return

              # `is_active` alone is NOT enough. `golden_questions.agent_id` is
              # nullable, so rows written before golden sets belonged to an agent
              # match any unscoped filter -- and a question about lecture
              # transcripts scored against a policy corpus still returns numbers,
              # it simply returns numbers about the wrong corpus. That silent
              # mixing is the exact failure the column was added to prevent, and
              # it is reintroduced by a query that forgets it.
              questions = list(
                  await db.scalars(
                      select(GoldenQuestion)
                      .where(
                          GoldenQuestion.agent_id == agent_id,
                          GoldenQuestion.is_active.is_(True),
                      )
                      # Display order, with a stable tie-break: `order_index`
                      # defaults to 0, so a set that has never been reordered is
                      # entirely tied and would otherwise come back in whatever
                      # order the scan produced -- making two runs of the same set
                      # score its questions in different orders.
                      .order_by(
                          GoldenQuestion.order_index.asc(),
                          GoldenQuestion.created_at.asc(),
                          GoldenQuestion.id.asc(),
                      )
                  )
              )

              # ----------------------------------------------------------
              # Open the run.
              # ----------------------------------------------------------
              # `generation_model` is written HERE, at run start, and never read
              # back from `agents.generation_model` at display time. The agent's
              # setting can change after a run, and reading it live would attribute
              # a score to a model that never produced the answer.
              run.status = "running"
              run.started_at = func.now()
              run.judge_model = settings.ragas_judge_model
              run.generation_model = agent.generation_model or settings.generation_model
              run.progress_total = len(questions)
              run.progress_done = 0
              # `func.now()` keeps every timestamp on the database's clock, matching
              # the server defaults on `created_at`. The cost is that the attribute
              # holds a SQL expression until refetched, so do NOT read
              # `run.started_at` below -- on an async session the implicit reload
              # raises MissingGreenlet.
              await db.commit()

              self_judged = run.judge_model == run.generation_model
              if self_judged:
                  # Not an error, and not hidden either. See `ragas_judge_model` in
                  # app/config.py: the model is grading its own output, the number
                  # is still measured against the retrieved contexts, and the
                  # scorecard says so.
                  log.info(
                      "Eval run %s is self-judged: %s answers and grades",
                      eval_run_id,
                      run.judge_model,
                  )

              scored_rows: list[dict[str, Any]] = []

              for index, question in enumerate(questions, start=1):
                  try:
                      row = await _run_one_question(db, agent, user, run, question)
                  except Exception as exc:  # noqa: BLE001 - see below
                      # ONE QUESTION FAILING MUST NOT ABORT THE RUN. That is the
                      # whole reason `eval_results.error` exists next to
                      # `eval_runs.error`: a per-question timeout belongs in the
                      # former, and putting it in the latter would void a
                      # scorecard for a single bad row.
                      log.exception(
                          "Eval run %s: question %s failed", eval_run_id, question.id
                      )
                      # The failed turn may have left a half-written `queries` row
                      # in this transaction. Roll it back before recording the
                      # failure, or the commit below carries the wreckage with it.
                      await db.rollback()
                      # Rollback expires every object in the session, and touching
                      # an expired attribute on an async session raises
                      # MissingGreenlet. Re-load the three we keep using.
                      agent = await db.get(Agent, agent_id)
                      user = await db.get(User, user_id)
                      run = await db.get(EvalRun, eval_run_id)
                      if agent is None or user is None or run is None:
                          await _fail_run(
                              eval_run_id,
                              "Agent, user or run disappeared mid-run.",
                          )
                          return

                      message = str(exc) or exc.__class__.__name__
                      db.add(
                          EvalResult(
                              id=uuid.uuid4(),
                              eval_run_id=eval_run_id,
                              golden_question_id=question.id,
                              error=message,
                          )
                      )
                      row = {
                          "golden_question_id": question.id,
                          "expected_behaviour": question.expected_behaviour,
                          # Unknown rather than False: the agent never got to
                          # answer, so nothing was observed about its behaviour and
                          # claiming a behaviour failure would be a fabricated
                          # measurement.
                          "behaviour_ok": None,
                          "scored": False,
                          "error": message,
                          **{key: None for key in METRIC_KEYS},
                      }

                  scored_rows.append(row)

                  # Committed per question, not once at the end. A run that dies
                  # halfway then keeps the answers it already paid for, and the
                  # progress the UI is polling actually moves.
                  run.progress_done = index
                  await db.commit()

              # ----------------------------------------------------------
              # Close the run.
              # ----------------------------------------------------------
              # `completed` even when every question errored. The alternative --
              # calling that `failed` -- would put the reason in `eval_runs.error`,
              # which is defined as "the run ended without a summary", and this run
              # has one: `error_count`, `scored_count` and the summary's `note`
              # say exactly what happened. `failed` is reserved for the run
              # machinery breaking, not for the agent or the judge scoring badly.
              run.summary = summarise(scored_rows, self_judged=self_judged)
              run.status = "completed"
              run.finished_at = func.now()
              await db.commit()

              log.info(
                  "Eval run %s finished: %s of %s scored, weakest %s",
                  eval_run_id,
                  run.summary.get("scored_count"),
                  run.summary.get("total_count"),
                  run.summary.get("weakest_metric"),
              )

      except Exception as exc:  # noqa: BLE001
          # NOTHING ESCAPES THIS FUNCTION. An exception raised out of a
          # BackgroundTask is returned to nobody -- the response went out minutes
          # ago -- so at best it lands in the log and at worst the task machinery
          # swallows it. Either way the run stays at `running` forever, which looks
          # like progress and is the single most confusing state available.
          log.exception("Eval run %s failed", eval_run_id)
          await _fail_run(eval_run_id, str(exc) or exc.__class__.__name__)


      finally:
          # Written whether the run completed or failed. A run that died after
          # eight of ten questions still spent the money for eight, and a
          # scorecard that is missing is not a bill that is missing.
          if usage_records:
              try:
                  async with SessionLocal() as meter_db:
                      metering_store.persist(meter_db, usage_records)
                      await meter_db.commit()
              except Exception:  # noqa: BLE001 -- accounting never breaks a run
                  log.warning("could not persist eval usage", exc_info=True)

async def _run_one_question(
    db,
    agent: Agent,
    user: User,
    run: EvalRun,
    question: GoldenQuestion,
) -> dict[str, Any]:
    """Ask one golden question through the real pipeline, then judge the answer."""
    # Imported here rather than at module scope. `app.api.ask` is a route module
    # that pulls in FastAPI dependencies and the whole RAG stack; importing it at
    # the top would make `app.eval.jobs` unimportable from anything that merely
    # wants a type, and would invite a cycle the day an eval route imports this
    # module (which is exactly what task #18 does).
    from app.api.ask import derive_conversation_title, run_turn

    # ------------------------------------------------------------------
    # ONE CONVERSATION PER QUESTION, NOT ONE PER RUN.
    #
    # `run_turn` reads the thread's recent turns and hands them to the
    # contextualiser, which rewrites a question in light of what came before.
    # Put ten golden questions in one thread and question five is no longer the
    # question that was written down -- it is question five as reinterpreted
    # through questions one to four, and the pronouns it never had get resolved
    # against a conversation that only exists because of how the eval was run.
    # The scorecard would then be measuring a different set of questions than the
    # editor shows, and the difference would not appear anywhere.
    #
    # A fresh thread has no history, so no pronoun is resolved against a
    # conversation that only exists because of how the eval was run.
    #
    # **That used to be the whole of the guarantee and is not any more.** Until
    # 2026-08-16 "no history" also meant "no rewrite at all", so a fresh thread
    # embedded the question verbatim by accident of its emptiness. The rewriter
    # now runs on first turns too, and `rewrite=` below is what buys the verbatim
    # embedding back deliberately rather than as a side effect.
    #
    # `is_archived=True` keeps ten machine-generated threads out of the user's
    # chat sidebar while preserving every trace hanging off them -- the column
    # exists precisely because there is no delete path that would keep those.
    # ------------------------------------------------------------------
    conversation = Conversation(
        id=uuid.uuid4(),
        agent_id=agent.id,
        user_id=user.id,
        # ASCII only: this string reaches a Windows console via the scripts that
        # print run summaries, where the codepage mangles anything else.
        title=f"[eval] {derive_conversation_title(question.question)}",
        is_archived=True,
    )
    db.add(conversation)
    await db.flush()

    # The real pipeline, with the agent's own configuration -- its retrieve_k,
    # its reranker setting, its system prompt. Anything overridden here would be
    # scoring a system the user cannot get back to from the agent editor.
    # `session=None`: a background job has no browser session, and
    # `queries.session_id` is nullable for exactly this.
    answer = await run_turn(
        db,
        agent=agent,
        user=user,
        session=None,
        conversation=conversation,
        question=question.question,
        # **The rewriter is skipped for eval turns, and that is a measurement
        # decision rather than a default.** A fresh thread used to guarantee
        # verbatim embedding by accident of having no history; with the rewriter
        # on every turn that guarantee is gone, every EVAL.md baseline silently
        # stops being comparable, and the golden-question editor keeps showing a
        # question that is not the one that was asked.
        # `settings.eval_rewrite_questions` turns it back on for anyone willing
        # to re-run the baselines and build an editor that shows the rewrite.
        rewrite=settings.eval_rewrite_questions,
    )

    contexts = await _contexts_for_query(db, answer.query_id)

    turn = EvalTurn(
        question=question.question,
        answer=answer.answer,
        contexts=contexts,
        reference=question.reference_answer,
        expected_behaviour=question.expected_behaviour,
        refused=answer.refused,
        golden_question_id=question.id,
    )
    scored = (await score_samples([turn], judge_model=run.judge_model))[0]

    db.add(
        EvalResult(
            id=uuid.uuid4(),
            eval_run_id=run.id,
            golden_question_id=question.id,
            # The join back to the answer, its citations and its trace. SET NULL
            # on delete, so a purged query leaves the score with its provenance
            # gone rather than taking the score with it.
            query_id=answer.query_id,
            faithfulness=scored["faithfulness"],
            answer_relevance=scored["answer_relevance"],
            context_precision=scored["context_precision"],
            context_recall=scored["context_recall"],
            behaviour_ok=scored["behaviour_ok"],
            error=scored["error"],
        )
    )
    return scored


async def _fail_run(eval_run_id: uuid.UUID, message: str) -> None:
    """Last resort: force a run to `failed`, from a session of its own.

    A SECOND session, deliberately, and not the one the caller was using -- the
    reason we are here may be that the first session is the thing that broke, and
    every write attempted on a session whose connection dropped mid-commit fails
    too, turning a recorded failure into an unrecorded one.

    Does nothing to a run that already finished. A run can fail after its summary
    was written (a stray error on the way out), and overwriting `completed` with
    `failed` would discard a scorecard that is perfectly good.

    Swallows its own errors. A failure to record a failure must not become the
    exception that escapes the background task.
    """
    try:
        async with SessionLocal() as db:
            run = await db.get(EvalRun, eval_run_id)
            if run is None or run.status in ("completed", "failed"):
                return
            run.status = "failed"
            run.error = message
            run.finished_at = func.now()
            await db.commit()
    except Exception:
        log.exception("Could not mark eval run %s failed", eval_run_id)
