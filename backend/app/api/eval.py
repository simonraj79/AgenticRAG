"""Evaluation: golden sets, their JSON round trip, and the runs that score them.

PRD section 4.4. Two resources live here and they are deliberately different
shapes: a golden set is edited constantly and cheap to write, an eval run is
expensive, asynchronous and immutable once finished.

**Four routes address a row by its own id and have no `agent_id` to bind
`OwnedAgent` to** -- `PATCH`/`DELETE /api/golden-questions/{id}` and
`GET`/`DELETE /api/eval-runs/{id}`. `app/api/deps.py` explains why that matters
more here than the missing dependency suggests: an agent's namespace is the
tenancy boundary and a wrong one fails silently. So the chain is rebuilt by hand,
in two dependencies rather than in four handlers, and it runs in one direction
only:

    question_id -> golden_questions row -> agent_id -> agents row
                                                    -> owner_user_id == session user

    run_id      -> eval_runs row        -> agent_id -> agents row
                                                    -> owner_user_id == session user

`app/api/conversations.py` does the same thing for threads and says the same
thing about why: a check copied into four handlers is a check missing from the
fifth. Both columns are NULLABLE, and a row with no `agent_id` has no owner to
check against, so it is treated as absent rather than guessed at -- see
`_owned_question`.

**Every agent-scoped read filters on `agent_id` explicitly.** `is_active` alone
is not enough: `golden_questions.agent_id` is nullable, so unscoped legacy rows
match any filter that forgets it and would be silently mixed into whichever
agent's set is being listed, exported or scored.

**Refusal questions are a feature of this API, not an edge case.** A golden set
deliberately contains questions the corpus cannot answer, and PRD 4.4 counts
refusing them as a correct outcome. The metric means exclude those rows entirely
-- `app/eval/metrics_guide.summarise` holds that rule and explains it -- so
nothing here should ever be tempted to "fix" a scorecard by averaging them in.

Three things are slow and therefore asynchronous: suggestion (one LLM call over
the corpus), an eval run (one real turn per question, plus judged metrics), and
nothing else. Both return 202 with something the client can poll.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Body,
    Depends,
    HTTPException,
    Query,
    Response,
    status,
)
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError
from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, DbSession, OwnedAgent
from app.config import settings
from app.db.models import (
    Agent,
    AuditLog,
    Chunk,
    Document,
    EvalResult,
    EvalRun,
    GoldenQuestion,
)
# `Query` is already taken in this module by `fastapi.Query`, which is used for
# the suggestion parameters. Aliasing the model rather than the FastAPI helper
# because only one of the two appears in a signature, where a wrong name is a
# silent 422 rather than an import error.
from app.db.models import Query as QueryRow
from app.db.session import SessionLocal
from app.eval.jobs import run_eval_job
from app.eval.metrics_guide import RunSummary

log = logging.getLogger("uvicorn.error")

router = APIRouter(prefix="/api", tags=["eval"])


# --------------------------------------------------------------------------
# Vocabularies and bounds
# --------------------------------------------------------------------------

# `golden_questions.source` is a plain String(16), not an enum, so adding a
# provenance costs no migration (see the column comment in `db/models.py`). The
# price of that is this tuple: it is the only place the vocabulary is written
# down, and a value outside it degrades to "manual" on the way out rather than
# raising. If a fifth provenance is ever added, add it HERE too -- otherwise it
# will be stored correctly and rendered as "manual", which is the quiet kind of
# wrong.
SOURCES: tuple[str, ...] = ("ai_suggested", "edited", "manual", "imported")

# `expected_behaviour` is the same shape of column and is validated strictly on
# the way IN, because unlike `source` it changes what a row MEANS: a question
# mislabelled "refusal" marks a correct answer as a failure, and a mislabelled
# "answer" drags refusal rows into the metric means the whole design excludes
# them from.
BEHAVIOURS: tuple[str, ...] = ("answer", "refuse")

# Sanity bounds, not platform limits. `question` and `reference_answer` are Text
# columns and would take anything; a 10,000-character "question" is a pasted
# document, and it would be discovered at run time as a bizarre embedding rather
# than here as a 422.
MAX_QUESTION_CHARS = 2_000
MAX_REFERENCE_CHARS = 4_000
MAX_NOTES_CHARS = 2_000

# A hand-edited file with a runaway loop in it must not become an hours-long,
# billable eval run. Each question costs one real generated turn (measured at
# 30-45 s with a persona, CLAUDE.md) plus up to four judged metric calls.
MAX_IMPORT_QUESTIONS = 500

# How long an eval run may sit at `pending`/`running` before DELETE stops
# protecting it. Same trade, and the same reasoning, as
# `documents.PROCESSING_STALE_AFTER`: a run can be abandoned mid-flight by a
# deploy, a restart or an OOM kill, and nothing will ever move it off `running`.
# Refusing forever would leave an undeletable row and, worse, an agent whose
# every future run is blocked by the 409 in `create_eval_run`. A ten-question
# run is minutes; an hour is not a run that is still going.
RUN_STALE_AFTER = timedelta(hours=1)

# Statuses that mean "this run is not finished". Written once because two routes
# branch on it and they must agree: `create_eval_run` refuses to start a second
# one, `delete_eval_run` refuses to delete underneath it.
LIVE_RUN_STATUSES: tuple[str, ...] = ("pending", "running")

QuestionText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_QUESTION_CHARS)
]
ReferenceText = Annotated[
    str, StringConstraints(strip_whitespace=True, max_length=MAX_REFERENCE_CHARS)
]
BehaviourText = Annotated[str, StringConstraints(strip_whitespace=True)]
NotesText = Annotated[
    str, StringConstraints(strip_whitespace=True, max_length=MAX_NOTES_CHARS)
]
# `golden_questions.order_index` is a plain `Integer`, i.e. Postgres int4. An
# unbounded value from a client is not merely silly, it is a 500: asyncpg raises
# a DataError on anything past 2^31, from inside the driver, naming a column
# rather than the request that carried it. The ceiling is arbitrary and generous
# -- it only has to be far below int4 and far above any set a person will write.
OrderIndex = Annotated[int, Field(ge=0, le=1_000_000)]


# --------------------------------------------------------------------------
# The tenancy boundary, by hand
# --------------------------------------------------------------------------

async def owned_question(
    question_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> GoldenQuestion:
    """Load a golden question the caller owns, or 404.

    ------------------------------------------------------------------
    THIS AND `owned_run` ARE THE HIGHEST-RISK CODE IN THIS MODULE. They are the
    only authorisation in the app that is not `OwnedAgent`, because
    `/api/golden-questions/{id}` has no `agent_id` for that dependency to bind
    to. Everything they protect is reached through `question.agent_id`, and the
    hop must be made server-side, from the stored row -- never from anything the
    request carries.
    ------------------------------------------------------------------

    **A row whose `agent_id` is NULL is treated as absent.** That column is
    nullable because it was added to a populated table (see `db/models.py`), so
    rows can exist that belong to no agent -- and a row with no agent has no
    owner to compare against. The only alternatives are to guess an owner or to
    let anyone edit it; both are worse than 404.

    404 rather than the 403 `owned_agent` returns, matching
    `conversations.owned_conversation`. An agent id is a handle the UI holds
    across sessions, so "wrong owner" there is a diagnosable stale-handle case. A
    question id is only ever obtained from a list this same caller just fetched;
    there is nothing to diagnose, and collapsing "not yours" into "not found"
    gives nothing away.
    """
    question = await db.get(GoldenQuestion, question_id)
    if question is None or question.agent_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Golden question not found",
        )

    agent = await db.get(Agent, question.agent_id)
    if agent is None or agent.owner_user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Golden question not found",
        )
    return question


OwnedQuestion = Annotated[GoldenQuestion, Depends(owned_question)]


async def owned_run(run_id: uuid.UUID, user: CurrentUser, db: DbSession) -> EvalRun:
    """Load an eval run the caller owns, or 404. See `owned_question`.

    Ownership goes through `eval_runs.agent_id`, NOT through `eval_runs.user_id`,
    even though the latter is right here on the row and would save a query. A
    scorecard is a property of the agent -- it is the number the agent's owner
    tunes against -- whereas `user_id` records who pressed the button, and those
    two stop being the same person the moment PRD 4.2's sharing arrives. Keying
    on the agent means this route says the same thing then as it does now.
    `user_id` is also SET NULL on user delete, so it is not a boundary that
    survives.
    """
    run = await db.get(EvalRun, run_id)
    if run is None or run.agent_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Eval run not found",
        )

    agent = await db.get(Agent, run.agent_id)
    if agent is None or agent.owner_user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Eval run not found",
        )
    return run


OwnedRun = Annotated[EvalRun, Depends(owned_run)]


# --------------------------------------------------------------------------
# Wire shapes
# --------------------------------------------------------------------------

class GoldenQuestionOut(BaseModel):
    """One test question as the editor sees it."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    # Non-optional even though the column is nullable. Every row this module
    # returns has been either written with an `agent_id` or filtered on one, and
    # the unscoped legacy rows the nullable column exists for are unreachable
    # through every route here (`owned_question` 404s them). Typing it non-null
    # is therefore a true statement about the API rather than a hopeful one.
    agent_id: uuid.UUID
    question: str
    reference_answer: str | None = None
    expected_behaviour: str
    is_active: bool
    source: str
    order_index: int
    created_at: datetime

    @classmethod
    def from_row(cls, row: GoldenQuestion) -> GoldenQuestionOut:
        out = cls.model_validate(row)
        # See SOURCES. An unrecognised provenance renders as "manual" instead of
        # failing the whole list: a scorecard editor that will not open because
        # somebody inserted a row by hand is a worse outcome than one label being
        # imprecise.
        if out.source not in SOURCES:
            out = out.model_copy(update={"source": "manual"})
        return out


class GoldenQuestionCreate(BaseModel):
    """Body for POST /api/agents/{agent_id}/golden-questions.

    `extra="forbid"`, for the reason `AgentUpdate` gives: an ignored field on a
    write is a UI that lies. In particular `source` is NOT accepted -- provenance
    is derived from how a row got here (this route means "a human typed it", so
    "manual"), and a client that could set it could claim a machine-written set
    had been human-reviewed, which is the one thing provenance exists to record.
    """

    model_config = ConfigDict(extra="forbid")

    question: QuestionText
    reference_answer: ReferenceText | None = None
    expected_behaviour: BehaviourText = "answer"
    is_active: bool = True
    # Omitted means "append to the end of the set". Accepted because the editor
    # supports drag-to-reorder and has to be able to insert at a position.
    order_index: OrderIndex | None = None


class GoldenQuestionPatch(BaseModel):
    """Body for PATCH /api/golden-questions/{id}. Only the fields SENT are applied.

    `reference_answer` can legitimately be set to null -- a refusal question needs
    none -- so "not sent" and "sent as null" must stay distinguishable, which is
    what `model_fields_set` (via `exclude_unset`) is read for below.
    """

    model_config = ConfigDict(extra="forbid")

    question: QuestionText | None = None
    reference_answer: ReferenceText | None = None
    expected_behaviour: BehaviourText | None = None
    is_active: bool | None = None
    order_index: OrderIndex | None = None

    # Declared ONLY so it can be refused with an explanation instead of a generic
    # "extra fields not permitted", exactly as `AgentUpdate.embedding_model` is.
    # A client PATCHing a whole `GoldenQuestionOut` back will send this, and the
    # 422 should say why it cannot be set rather than implying a typo.
    source: str | None = None


class ProgressOut(BaseModel):
    """"3 of 12", as two numbers.

    Nested rather than flattened into `done`/`total` on the run so that a client
    polling a long run reads one object and cannot render a done without its
    total. Both columns are NOT NULL with a server default of 0, so a queued run
    honestly reports 0 of 0 rather than forcing every reader to decide what a
    missing count means.
    """

    done: int
    total: int


# The pinned contract calls this `ScoreSummary`; `app/eval/metrics_guide.py`
# calls it `RunSummary` and OWNS it. Aliased rather than redefined, because
# `eval_runs.summary` is JSONB with no enforced shape -- the model IS the schema,
# and a second copy of it here would be a second place for a key to drift, with
# nothing in the database to catch the difference. The alias carries a few keys
# the contract does not list (`total_count`, `error_count`, `self_judged`,
# `investment`, `note`); those are additive, and the scorecard needs them.
ScoreSummary = RunSummary


class EvalRunOut(BaseModel):
    """One scorecard's header: status, models, progress and the roll-up."""

    id: uuid.UUID
    agent_id: uuid.UUID
    status: str
    judge_model: str | None = None
    generation_model: str | None = None
    notes: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    # Run-level failure only -- the reason a run ended without a summary. One
    # question failing lands in `EvalResultOut.error` instead, and conflating the
    # two would void a whole scorecard for a single bad row.
    error: str | None = None
    progress: ProgressOut
    summary: ScoreSummary | None = None
    # COMPUTED, never stored. When the judge and the generator are the same
    # model, faithfulness is self-assessment (CLAUDE.md, Ragas section) and the
    # scorecard has to say so. Derived here rather than read from
    # `summary.self_judged` so that it is available on a run that has not
    # finished -- and so a pending run already tells the user what it is about to
    # measure.
    judge_is_generator: bool


class EvalResultOut(BaseModel):
    """One question's row in a scorecard.

    All four metrics are nullable and None means "not measured", which is a
    different fact from 0.0 and must survive to the UI as a different one. Two
    quite different rows are None across the board: a correct refusal, which
    Ragas has nothing to grade, and a question whose judge call failed. Read
    `behaviour_ok` and `error` to tell them apart -- that is exactly why
    `behaviour_ok` is a column and not derived from the floats.

    `answer_relevance` is a cosine mean and CAN BE NEGATIVE. It is the only one
    of the four not bounded below at zero, and it is deliberately not clamped:
    clamping would turn "actively off-topic" into "merely unrelated". Anything
    drawing a bar from this must not assume a 0-1 domain.
    """

    id: uuid.UUID
    golden_question_id: uuid.UUID
    # Joined from `golden_questions`, so a scorecard read months later shows the
    # question as it stands now -- the row is the same row the editor holds.
    question: str
    expected_behaviour: str
    # The join back to the answer, its citations and its Trace view. NULL when
    # the query row has since been purged (`eval_results.query_id` is SET NULL),
    # which is also why `answer` and `refused` below can be absent.
    query_id: uuid.UUID | None = None
    answer: str | None = None
    refused: bool | None = None
    behaviour_ok: bool | None = None
    faithfulness: float | None = None
    answer_relevance: float | None = None
    context_precision: float | None = None
    context_recall: float | None = None
    error: str | None = None


class EvalRunDetail(EvalRunOut):
    results: list[EvalResultOut]


class EvalRunCreate(BaseModel):
    """Body for POST /api/agents/{agent_id}/eval-runs. Optional in full.

    `notes` is the point of eval-driven development and the only field: "raised
    rerank_top_n to 5" is what turns two scorecards into a measurement rather
    than two numbers. No model overrides -- the judge comes from settings and the
    generator from the agent, so a run always scores a system the user can get
    back to from the agent editor.
    """

    model_config = ConfigDict(extra="forbid")

    notes: NotesText | None = None


class SuggestAccepted(BaseModel):
    """202 body for the suggestion route.

    There is no suggestion RESOURCE to poll -- the job's only output is rows in
    the golden set -- so this says what was accepted and the client re-reads
    `GET .../golden-questions` until it changes.
    """

    status: str
    count: int
    refusal_count: int
    message: str


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _audit(
    # `AsyncSession`, not the `DbSession` alias: that alias carries a
    # `Depends(get_db)` inside it, which means something to FastAPI and nothing
    # to a plain helper. Same call as `documents._audit`.
    db: AsyncSession,
    user_id: uuid.UUID | None,
    action: str,
    resource_type: str,
    resource_id: uuid.UUID,
    **metadata: Any,
) -> None:
    """Stage one `audit_log` row. Adds only -- the caller owns the commit.

    Written AFTER the operation it records, never before: a log that claims an
    import which then failed is worse than one that under-reports, because a
    missing line sends someone to look and a false line stops them.

    Takes a user id rather than a `User`, because the background jobs below have
    only an id -- an ORM object belongs to the session that loaded it, and theirs
    is long closed. `audit_log.user_id` is nullable and SET NULL on user delete.

    The attribute is `audit_metadata`; the COLUMN is `metadata`, which is
    reserved on a SQLAlchemy declarative class. Everything inside must be
    JSON-native for JSONB, which is why ids are stringified at the call sites.
    """
    db.add(
        AuditLog(
            user_id=user_id,
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id),
            audit_metadata=metadata,
        )
    )


def _active_set_query(agent_id: uuid.UUID):
    """One agent's ACTIVE golden set, in display order.

    ------------------------------------------------------------------
    `agent_id` IS FILTERED EXPLICITLY, AND `is_active` ALONE IS NOT ENOUGH.
    `golden_questions.agent_id` is nullable, so a bare
    `where(GoldenQuestion.is_active)` silently mixes unscoped legacy rows into
    whichever agent's set is being read -- which is precisely the failure that
    column was added to prevent, reintroduced at the query layer. It still
    returns numbers; they are simply numbers about the wrong corpus.
    ------------------------------------------------------------------

    Three sort keys, matching `app/eval/jobs.py` exactly. `order_index` defaults
    to 0, so a set that has never been reordered is entirely tied and would come
    back in scan order -- making the editor's order and the run's order disagree,
    and two runs of the same set differ from each other.
    """
    return (
        select(GoldenQuestion)
        .where(
            GoldenQuestion.agent_id == agent_id,
            GoldenQuestion.is_active.is_(True),
        )
        .order_by(
            GoldenQuestion.order_index.asc(),
            GoldenQuestion.created_at.asc(),
            GoldenQuestion.id.asc(),
        )
    )


async def _next_order_index(db: AsyncSession, agent_id: uuid.UUID) -> int:
    """One past the highest `order_index` in this agent's set, or 0.

    Over ALL of the agent's questions, active or not: a deactivated question is
    still shown in the editor, so appending on top of its index would drop the
    new row into the middle of the visible list.
    """
    highest = await db.scalar(
        select(func.max(GoldenQuestion.order_index)).where(
            GoldenQuestion.agent_id == agent_id
        )
    )
    return 0 if highest is None else int(highest) + 1


async def _retire_questions(
    db: AsyncSession, questions: list[GoldenQuestion]
) -> tuple[int, int]:
    """Supersede questions without destroying the scorecards that scored them.

    Returns `(deleted, deactivated)`. Adds and deletes only -- the caller commits.

    ------------------------------------------------------------------
    `eval_results.golden_question_id` is ON DELETE CASCADE, so deleting a
    question DELETES ITS ROW FROM EVERY PAST SCORECARD. The stored
    `eval_runs.summary` keeps its means and its `scored_count`, computed over
    rows that no longer exist -- a scorecard that keeps its scores and loses its
    evidence, which CLAUDE.md flags as worse than losing both, because the
    numbers still render and nothing signals that they are no longer
    reproducible.

    That is an acceptable price for an explicit DELETE, where a human asked for
    it. It is not an acceptable price for pressing "Suggest" again. So a question
    some run has already scored is DEACTIVATED (it stays in the editor, greyed,
    and stays out of future runs) and only a never-scored question is actually
    deleted.
    ------------------------------------------------------------------
    """
    if not questions:
        return 0, 0

    ids = [question.id for question in questions]
    scored = set(
        await db.scalars(
            select(EvalResult.golden_question_id)
            .where(EvalResult.golden_question_id.in_(ids))
            .distinct()
        )
    )

    deactivated = 0
    for question in questions:
        if question.id in scored:
            question.is_active = False
            deactivated += 1

    doomed = [row_id for row_id in ids if row_id not in scored]
    if doomed:
        await db.execute(sql_delete(GoldenQuestion).where(GoldenQuestion.id.in_(doomed)))
    return len(doomed), deactivated


def _summary_of(run: EvalRun) -> ScoreSummary | None:
    """`eval_runs.summary` as a typed object, or None.

    Read back THROUGH `RunSummary` rather than handed out as the raw JSONB dict.
    The column has no enforced shape, so this is the only thing standing between
    a renamed key in the runner and a scorecard that renders silent blanks.

    A summary that will not validate degrades to None with a log line rather than
    500-ing the request: the run's status, models and per-question rows are still
    worth showing, and a scorecard that cannot be opened at all tells the user
    strictly less than one missing its roll-up.
    """
    if not run.summary:
        return None
    try:
        return RunSummary.model_validate(run.summary)
    except ValidationError:
        log.exception("Eval run %s has an unreadable summary", run.id)
        return None


def _run_out(run: EvalRun) -> EvalRunOut:
    """The single construction site for an EvalRunOut.

    `judge_is_generator` and `progress` are not columns, so every route building
    this shape by hand would be a route that can disagree with the others about
    it -- the kind of difference a frontend finds at runtime and nothing else
    catches.

    The two model names are read off the RUN, never from
    `agents.generation_model`. The agent's setting can change after a run, and
    reading it live would attribute a score to a model that never produced the
    answer.
    """
    return EvalRunOut(
        id=run.id,
        # Non-null by construction: `owned_run` and the list route both refuse or
        # filter out a run with no agent.
        agent_id=run.agent_id,  # type: ignore[arg-type]
        status=run.status,
        judge_model=run.judge_model,
        generation_model=run.generation_model,
        notes=run.notes,
        started_at=run.started_at,
        finished_at=run.finished_at,
        error=run.error,
        progress=ProgressOut(done=run.progress_done, total=run.progress_total),
        summary=_summary_of(run),
        # `bool(judge_model) and ...` rather than a bare equality. Both columns
        # are nullable, and `None == None` is True -- which would claim a run
        # with no models recorded at all had graded its own output.
        judge_is_generator=bool(run.judge_model)
        and run.judge_model == run.generation_model,
    )


def _filename_slug(name: str) -> str:
    """An agent name reduced to something safe inside a header.

    Two separate reasons, and only the second is obvious. `Content-Disposition`
    values must be latin-1 encodable or the ASGI server raises when it writes the
    header -- after the whole body has been built -- so an agent named in Chinese
    would 500 on export and nowhere near a line mentioning its name. And the
    header has a grammar: an unescaped quote or semicolon in a filename lets a
    user-chosen agent name inject header parameters.

    Stripping to `[A-Za-z0-9-]` solves both at once, at the cost of a
    transliterated name being reduced to "agent". A downloaded file with a dull
    name beats a download that fails.
    """
    slug = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower()
    return slug[:60] or "agent"


def _header_safe(text: str) -> str:
    """Collapse to printable ASCII for a response header. See `_filename_slug`."""
    return re.sub(r"[^\x20-\x7e]+", " ", text).replace("\\", " ").strip()


# --------------------------------------------------------------------------
# Golden questions: read and write one
# --------------------------------------------------------------------------

@router.get("/agents/{agent_id}/golden-questions")
async def list_golden_questions(
    agent: OwnedAgent, db: DbSession
) -> list[GoldenQuestionOut]:
    """This agent's golden set, in display order. Inactive rows included.

    Inactive questions are RETURNED, not filtered. `is_active` is the editor's
    "include this in the next run" toggle, so hiding a switched-off question
    would make the toggle a one-way trip -- and `_retire_questions` deactivates
    superseded rows rather than deleting them, which would make those vanish with
    no trace of why the set shrank.

    The ordering is the same three keys the runner uses; `_active_set_query`
    explains why the tie-break is load-bearing.
    """
    rows = await db.scalars(
        select(GoldenQuestion)
        # The tenancy filter, and it is not optional even with `OwnedAgent`
        # upstream: `agent_id` is nullable, so omitting it lists legacy unscoped
        # rows alongside this agent's set.
        .where(GoldenQuestion.agent_id == agent.id)
        .order_by(
            GoldenQuestion.order_index.asc(),
            GoldenQuestion.created_at.asc(),
            GoldenQuestion.id.asc(),
        )
    )
    return [GoldenQuestionOut.from_row(row) for row in rows.all()]


@router.post(
    "/agents/{agent_id}/golden-questions",
    status_code=status.HTTP_201_CREATED,
)
async def create_golden_question(
    body: GoldenQuestionCreate, agent: OwnedAgent, db: DbSession
) -> GoldenQuestionOut:
    """Write one question by hand.

    `source="manual"` is set here and cannot be sent -- see
    `GoldenQuestionCreate`. It is what tells a later reader that a human wrote
    this row, and it is also what protects it: `_retire_questions` is only ever
    aimed at `ai_suggested` rows, so re-running Suggest can never touch this one.

    An answerable question with no `reference_answer` is ACCEPTED, deliberately.
    `context_recall` is computed against the reference and silently abstains
    without one, so such a row scores three metrics out of four -- but refusing
    it would block a user who types the question first and the answer second,
    which is how anybody actually writes one. The editor is the right place to
    warn.
    """
    behaviour = _validated_behaviour(body.expected_behaviour)

    question = GoldenQuestion(
        id=uuid.uuid4(),
        agent_id=agent.id,
        question=body.question,
        reference_answer=body.reference_answer or None,
        expected_behaviour=behaviour,
        is_active=body.is_active,
        source="manual",
        order_index=(
            body.order_index
            if body.order_index is not None
            else await _next_order_index(db, agent.id)
        ),
    )
    db.add(question)
    await db.commit()
    # `created_at` is a server default. An unloaded attribute on an async session
    # refreshes itself with implicit IO, which raises MissingGreenlet from inside
    # the serialiser -- a 500 whose traceback points at Pydantic rather than at
    # the column.
    await db.refresh(question)

    return GoldenQuestionOut.from_row(question)


def _validated_behaviour(value: str) -> str:
    """`expected_behaviour`, or a 422 naming what was sent.

    A free function rather than a Pydantic `Literal` so that the import path can
    reuse the same rule and report it per row instead of rejecting a whole file.
    """
    behaviour = (value or "").strip().lower()
    if behaviour not in BEHAVIOURS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"expected_behaviour must be one of {', '.join(BEHAVIOURS)} "
                f"-- got {value!r}."
            ),
        )
    return behaviour


@router.patch("/golden-questions/{question_id}")
async def update_golden_question(
    body: GoldenQuestionPatch, question: OwnedQuestion, db: DbSession
) -> GoldenQuestionOut:
    """Edit one question. Only the fields actually sent are applied.

    **Editing the CONTENT flips `source` to "edited"; toggling `is_active` or
    dragging the row does not.** That distinction is the whole value of the
    column: provenance records whether a human reviewed the WORDING, and a set
    the model wrote for itself is a weaker test than the same set after somebody
    corrected it. Switching a question off is not review, and if it counted as
    review then "ai_suggested" would decay to "edited" through ordinary use and
    stop meaning anything. Only a question that was `ai_suggested` moves -- a
    `manual` or `imported` row is already human-authored and stays that way.
    """
    if "source" in body.model_fields_set:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "source is derived, not set: it records how a question got here "
                "and flips to 'edited' by itself when the wording is changed."
            ),
        )

    # `exclude_unset` is the whole reason this is not a loop over the model's
    # fields: `reference_answer=None` sent explicitly means "clear it" (a refusal
    # question needs none), and omitted means "leave it alone". A plain
    # `model_dump()` cannot tell those apart and would wipe the reference answer
    # off every question the editor PATCHes for an unrelated reason.
    changes = body.model_dump(exclude_unset=True)

    # VALIDATED IN FULL BEFORE ANYTHING IS ASSIGNED. Raising halfway through a
    # loop that has already called `setattr` leaves a mutated ORM object in the
    # session, and it is only harmless because `get_db` closes the session
    # without committing -- a guarantee living in another module, which is
    # exactly the kind of thing an edit here would not know it was relying on.
    for field, value in changes.items():
        # ONLY `reference_answer` IS NULLABLE IN THE TABLE. The optional typing on
        # the patch model means "may be omitted", not "may be null" -- and a
        # client sending `"question": null` (a form field cleared, most likely)
        # would otherwise validate here and fail at the driver as an
        # IntegrityError: a 500 for what is plainly a request error, with a
        # message naming a column instead of a field.
        if value is None and field != "reference_answer":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"{field} cannot be null. Omit it to leave it unchanged.",
            )
        if field == "expected_behaviour":
            changes[field] = _validated_behaviour(value)
        if field == "reference_answer":
            # "" and NULL are the same fact here and must not become two. An
            # empty reference answer disables `context_recall` for the row just
            # as a missing one does, and storing the empty string would make the
            # editor's "has a reference answer" check disagree with the runner's.
            changes[field] = value or None

    content_changed = False
    for field, value in changes.items():
        if field in ("question", "reference_answer", "expected_behaviour"):
            # Compared before assigning: a PATCH that re-sends identical wording
            # (the editor saving an untouched form) is not an edit, and marking it
            # as one would launder an unreviewed set into a reviewed-looking one.
            content_changed = content_changed or getattr(question, field) != value
        setattr(question, field, value)

    if content_changed and question.source == "ai_suggested":
        question.source = "edited"

    await db.commit()
    await db.refresh(question)
    return GoldenQuestionOut.from_row(question)


@router.delete("/golden-questions/{question_id}")
async def delete_golden_question(
    question: OwnedQuestion, user: CurrentUser, db: DbSession
) -> dict[str, bool]:
    """Delete one question, and with it its row in every past scorecard.

    **This cascades.** `eval_results.golden_question_id` is ON DELETE CASCADE, so
    the per-question rows this question contributed to earlier runs go with it,
    while those runs keep the `summary` that was computed over them. A scorecard
    that keeps its scores and loses its evidence is the failure CLAUDE.md warns
    about, and it is allowed here only because a DELETE is an explicit act by the
    row's owner.

    The audit row is the compensation: after the cascade this is the only record
    that the question existed, so it carries the text and the count of scorecard
    rows that went with it. Deactivating instead (PATCH `is_active`) is the
    non-destructive option and the one the editor should offer first.
    """
    scored_rows = int(
        await db.scalar(
            select(func.count())
            .select_from(EvalResult)
            .where(EvalResult.golden_question_id == question.id)
        )
        or 0
    )

    _audit(
        db,
        user.id,
        "golden_question.delete",
        "golden_question",
        question.id,
        agent_id=str(question.agent_id),
        # The text is copied into the log deliberately: once the row is gone this
        # is the only place it still exists.
        question=question.question,
        source=question.source,
        eval_results_deleted=scored_rows,
    )
    await db.delete(question)
    await db.commit()
    return {"ok": True}


# --------------------------------------------------------------------------
# Suggestion (202, background)
# --------------------------------------------------------------------------

@router.post(
    "/agents/{agent_id}/golden-questions/suggest",
    status_code=status.HTTP_202_ACCEPTED,
)
async def suggest_golden_questions_route(
    agent: OwnedAgent,
    user: CurrentUser,
    db: DbSession,
    background: BackgroundTasks,
    count: Annotated[int, Query(ge=1, le=25)] = 10,
    refusal_count: Annotated[int, Query(ge=0, le=25)] = 2,
) -> SuggestAccepted:
    """Draft a golden set from this agent's own indexed chunks. 202, not 201.

    One LLM call over a sample of the corpus, which is several seconds and
    occasionally more -- past a spinner's welcome and squarely into background
    territory, like ingest. There is nothing to poll but the set itself, so the
    client re-reads `GET .../golden-questions`.

    **The empty-corpus case is caught HERE, synchronously, before the handoff.**
    `suggest_golden_questions` raises `EmptyCorpusError` for it, but raised inside
    a background task that message reaches a log and nobody else -- and "you have
    not uploaded anything yet" is the single most likely reason this route is
    called wrongly. One COUNT turns it into an immediate 409 the user can act on.
    """
    if refusal_count > count:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="refusal_count cannot exceed count.",
        )

    # `chunks` has no agent column, so the join through `documents` IS the
    # tenancy scope -- the same join `app/eval/generate.py` uses to read the
    # corpus, asked here only for its size.
    chunk_total = int(
        await db.scalar(
            select(func.count(Chunk.id))
            .join(Document, Document.id == Chunk.document_id)
            .where(Document.agent_id == agent.id)
        )
        or 0
    )
    if chunk_total == 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This agent has no indexed text to write questions about. "
                "Upload a document and wait for it to finish indexing first."
            ),
        )

    # IDS ONLY, and scheduled with nothing from this session. A FastAPI
    # BackgroundTask runs after the response has gone out, by which point
    # `get_db` has closed this session and returned its connection to the pool --
    # so an `Agent` or an `AsyncSession` captured here is already dead when the
    # task starts. `app/rag/jobs.py` explains the failure modes; none of their
    # error messages mentions background tasks.
    background.add_task(
        _suggest_job,
        agent.id,
        user.id,
        count,
        refusal_count,
    )

    return SuggestAccepted(
        status="generating",
        count=count,
        refusal_count=refusal_count,
        message=(
            "Drafting questions from the corpus. Re-read the golden set in a "
            "few seconds; suggestions replace earlier AI-suggested questions "
            "and never touch ones you wrote or edited."
        ),
    )


async def _suggest_job(
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    count: int,
    refusal_count: int,
) -> None:
    """Draft a set and swap it in. Opens its own session. Never raises.

    Same contract as `app/rag/jobs.py` and `app/eval/jobs.py`: ids in, a session
    of its own, nothing escapes. An exception raised out of a BackgroundTask is
    returned to nobody -- the response went out seconds ago -- so at best it lands
    in a log and at worst the task machinery swallows it.
    """
    # Imported here rather than at module scope. `app/eval/generate.py` pulls in
    # LangChain and the Gemini client, and `app/eval/__init__.py` is deliberately
    # empty of imports for the same reason: nothing that merely wants a response
    # shape out of this module should pay for the model stack.
    from app.eval.generate import (
        EmptyCorpusError,
        GoldenSetSuggestionError,
        suggest_golden_questions,
    )

    try:
        async with SessionLocal() as db:
            agent = await db.get(Agent, agent_id)
            if agent is None:
                # Deleted between the response and this task starting.
                log.warning("Suggest job: agent %s no longer exists", agent_id)
                return

            try:
                drafted = await suggest_golden_questions(
                    db, agent, count=count, refusal_count=refusal_count
                )
            except (EmptyCorpusError, GoldenSetSuggestionError) as exc:
                # THE EXISTING SET IS STILL INTACT, because nothing has been
                # retired yet -- see the ordering below. A failed suggestion
                # leaves the user exactly where they were.
                log.warning("Suggest job for agent %s failed: %s", agent_id, exc)
                _audit(
                    db,
                    user_id,
                    "golden_questions.suggest_failed",
                    "agent",
                    agent_id,
                    error=str(exc),
                )
                await db.commit()
                return

            # --------------------------------------------------------------
            # THE SWAP HAPPENS AFTER GENERATION SUCCEEDED, NEVER BEFORE.
            # Clearing first and drafting second would leave a user with an
            # empty golden set every time the model was rate limited.
            #
            # The predicate is POSITIVE -- `source == "ai_suggested"` -- and not
            # the negative `source NOT IN ("edited", "manual")` it could have
            # been. They are the same set today and they diverge the moment a
            # provenance is added: a negative predicate would silently start
            # deleting the new kind, and an imported, hand-written golden
            # question destroyed by pressing Suggest is not a mistake anybody
            # would forgive. Only rows this feature itself created are
            # replaceable.
            # --------------------------------------------------------------
            superseded = list(
                await db.scalars(
                    select(GoldenQuestion).where(
                        GoldenQuestion.agent_id == agent_id,
                        GoldenQuestion.source == "ai_suggested",
                    )
                )
            )
            deleted, deactivated = await _retire_questions(db, superseded)

            base = await _next_order_index(db, agent_id)
            for row in drafted:
                db.add(
                    GoldenQuestion(
                        id=uuid.uuid4(),
                        agent_id=agent_id,
                        question=row["question"],
                        reference_answer=row["reference_answer"],
                        expected_behaviour=row["expected_behaviour"],
                        is_active=True,
                        source=row["source"],
                        # `suggest_golden_questions` numbers its output from 0
                        # and says so; a caller appending to an existing set has
                        # to offset it or the new rows interleave with whatever
                        # survived the retirement.
                        order_index=base + int(row["order_index"]),
                    )
                )

            _audit(
                db,
                user_id,
                "golden_questions.suggest",
                "agent",
                agent_id,
                suggested=len(drafted),
                requested=count,
                refusal_count=refusal_count,
                replaced_deleted=deleted,
                replaced_deactivated=deactivated,
            )
            await db.commit()

            log.info(
                "Suggest job for agent %s wrote %s questions (replaced %s, "
                "kept %s already scored)",
                agent_id,
                len(drafted),
                deleted,
                deactivated,
            )

    except Exception:  # noqa: BLE001 - nothing escapes a background task
        log.exception("Suggest job for agent %s failed", agent_id)


# --------------------------------------------------------------------------
# The plain-JSON round trip
# --------------------------------------------------------------------------

@router.get("/agents/{agent_id}/golden-questions/export")
async def export_golden_questions(agent: OwnedAgent, db: DbSession) -> Response:
    """Download this agent's golden set as a file meant to be hand-edited.

    Not a `list[GoldenQuestionOut]`. The export is a THIRD shape -- question,
    reference answer, expected behaviour and nothing else -- because the point is
    a file somebody opens in a text editor, and ids, timestamps and provenance
    are noise there that also cannot survive a round trip through another agent.

    **Active questions only.** The format carries no `is_active` field, so a
    deactivated question written into the file would come back switched on: the
    round trip would quietly resurrect exactly the questions its owner turned
    off. Exporting only what is live keeps import able to default it to true.

    Built with `json.dumps(indent=2)` rather than returned as a model, because
    `JSONResponse` emits one long line and this file's whole purpose is to be
    read and edited by a person.
    """
    rows = list(await db.scalars(_active_set_query(agent.id)))

    payload = {
        "agent_name": agent.name,
        # Second-resolution UTC with a literal Z, matching the pinned format.
        # `isoformat()` on an aware datetime would render "+00:00" and carry
        # microseconds, which is the same instant and a different string -- and
        # this string is compared by eye across exports.
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "questions": [
            {
                "question": row.question,
                "reference_answer": row.reference_answer,
                "expected_behaviour": row.expected_behaviour,
            }
            for row in rows
        ],
    }

    filename = f"golden-set-{_filename_slug(agent.name)}.json"
    return Response(
        # `ensure_ascii=False` so a corpus in any language reads as itself in the
        # file. The BODY is UTF-8 and unrestricted; only the HEADER below has to
        # be ASCII, which is what `_filename_slug` is for.
        content=json.dumps(payload, ensure_ascii=False, indent=2),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _parse_import_rows(payload: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """Turn a hand-edited file into rows plus a list of per-row complaints.

    ------------------------------------------------------------------
    ONE BAD ROW MUST NOT REJECT THE FILE. This file is edited by hand, in a text
    editor, usually at the point where somebody is already frustrated with a
    scorecard. Refusing all twelve questions because the fourth has a typo in
    `expected_behaviour` is the behaviour that makes people give up on the
    feature; importing eleven and saying what happened to the twelfth is not.
    ------------------------------------------------------------------

    Accepts the wrapped object OR a bare list, and ignores unknown keys at both
    levels, so a file carrying extra fields from a future version -- or one a
    person added notes to -- still imports.
    """
    problems: list[str] = []

    if isinstance(payload, dict):
        # Unknown top-level keys (`agent_name`, `exported_at`, anything a person
        # added) are ignored by simply not being read.
        raw = payload.get("questions")
        if raw is None:
            return [], ["The object has no 'questions' key."]
    else:
        raw = payload

    if not isinstance(raw, list):
        return [], ["Expected a list of questions, or an object with a 'questions' list."]

    rows: list[dict[str, Any]] = []
    # Normalised question text already accepted, so a file containing the same
    # question twice does not become two rows that will both be asked, judged and
    # billed for. Cheap and lexical on purpose: this catches copy-paste, not
    # paraphrase, and the human editing the file is the real filter.
    seen: set[str] = set()

    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            problems.append(f"row {index}: skipped, not an object.")
            continue

        question = item.get("question")
        if not isinstance(question, str) or not question.strip():
            problems.append(f"row {index}: skipped, 'question' is missing or empty.")
            continue
        question = question.strip()
        if len(question) > MAX_QUESTION_CHARS:
            problems.append(
                f"row {index}: skipped, 'question' is longer than "
                f"{MAX_QUESTION_CHARS} characters."
            )
            continue

        key = " ".join(question.lower().split())
        if key in seen:
            problems.append(f"row {index}: skipped, duplicate of an earlier row.")
            continue

        behaviour = item.get("expected_behaviour", "answer")
        if not isinstance(behaviour, str) or behaviour.strip().lower() not in BEHAVIOURS:
            problems.append(
                f"row {index}: skipped, 'expected_behaviour' must be one of "
                f"{', '.join(BEHAVIOURS)}."
            )
            continue
        behaviour = behaviour.strip().lower()

        reference = item.get("reference_answer")
        if reference is not None and not isinstance(reference, str):
            problems.append(
                f"row {index}: skipped, 'reference_answer' must be text or null."
            )
            continue
        reference = (reference or "").strip()[:MAX_REFERENCE_CHARS] or None

        if behaviour == "answer" and reference is None:
            # KEPT, not skipped, and the wording says so. `context_recall` is
            # computed against the reference answer and silently abstains without
            # one, so this row will score three metrics out of four -- worth
            # saying, not worth discarding somebody's question over.
            problems.append(
                f"row {index}: imported, but it has no 'reference_answer', so "
                "context_recall cannot be scored for it."
            )

        seen.add(key)
        rows.append(
            {
                "question": question,
                "reference_answer": reference,
                "expected_behaviour": behaviour,
            }
        )

        if len(rows) >= MAX_IMPORT_QUESTIONS:
            problems.append(
                f"Stopped at {MAX_IMPORT_QUESTIONS} questions; the rest of the "
                "file was ignored."
            )
            break

    return rows, problems


@router.post("/agents/{agent_id}/golden-questions/import")
async def import_golden_questions(
    agent: OwnedAgent,
    user: CurrentUser,
    db: DbSession,
    response: Response,
    payload: Annotated[Any, Body(...)],
    replace: bool = False,
) -> list[GoldenQuestionOut]:
    """Load questions from a hand-edited file. Appends by default.

    **Append, not replace, unless `?replace=true` is asked for.** The destructive
    default is the tempting one -- export, edit, import, and the set is what the
    file says -- but it means a mis-clicked import silently destroys questions
    that are not in the file, and there is no undo. Duplicates are dropped
    instead (see `_parse_import_rows`), so re-importing an unchanged export is a
    no-op rather than a doubled set, which is what makes appending workable.
    `?replace=true` routes through `_retire_questions`, so even then a question a
    past run scored is deactivated rather than deleted.

    **Per-row problems travel in headers, not in the body.** The pinned contract
    makes this route return a bare `[GoldenQuestionOut]` with nowhere to put a
    warning, and the alternative -- failing the whole file so the message fits in
    a 422 -- is exactly the behaviour the per-row handling exists to avoid. The
    full list is also written to `audit_log`, which is durable and does not have
    a length limit.
    """
    rows, problems = _parse_import_rows(payload)

    if not rows:
        # Nothing usable at all IS a 422, and it carries every complaint. This is
        # the branch that tells someone their file is a JSON array of strings, or
        # that they exported from a different tool.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "message": "No questions could be read from this file.",
                "problems": problems[:20],
            },
        )

    retired_deleted = 0
    retired_deactivated = 0
    if replace:
        existing = list(
            await db.scalars(
                select(GoldenQuestion).where(GoldenQuestion.agent_id == agent.id)
            )
        )
        retired_deleted, retired_deactivated = await _retire_questions(db, existing)
        # Flushed before the next index is computed: `_next_order_index` reads
        # MAX(order_index) from the database, and rows deleted only in this
        # session's identity map are still there as far as that query is
        # concerned.
        await db.flush()

    base = await _next_order_index(db, agent.id)
    created: list[GoldenQuestion] = []
    for offset, row in enumerate(rows):
        question = GoldenQuestion(
            id=uuid.uuid4(),
            agent_id=agent.id,
            question=row["question"],
            reference_answer=row["reference_answer"],
            expected_behaviour=row["expected_behaviour"],
            is_active=True,
            # "imported" and not "manual". Both are human-authored, and the
            # distinction still earns its place: an imported set came from
            # somewhere else, possibly from another agent's corpus, and that is
            # worth knowing when its recall scores look strange.
            source="imported",
            # THE FILE'S ORDER IS THE SET'S ORDER. `order_index` is deliberately
            # not read from the file even if it is there -- reordering questions
            # by moving lines in a text editor is the obvious gesture, and it
            # would do nothing if a stale index in each object overrode it.
            order_index=base + offset,
        )
        db.add(question)
        created.append(question)

    _audit(
        db,
        user.id,
        "golden_questions.import",
        "agent",
        agent.id,
        imported=len(created),
        replaced=replace,
        replaced_deleted=retired_deleted,
        replaced_deactivated=retired_deactivated,
        problems=problems,
    )
    await db.commit()

    for question in created:
        # `created_at` is a server default; see `create_golden_question`.
        await db.refresh(question)

    response.headers["X-Import-Imported"] = str(len(created))
    if problems:
        response.headers["X-Import-Problems"] = str(len(problems))
        # Truncated hard. Header size is bounded by every proxy in the path, and
        # a 500-row file with 500 complaints would otherwise produce a header
        # nothing will forward. The audit row has all of them.
        response.headers["X-Import-Detail"] = _header_safe("; ".join(problems))[:500]

    return [GoldenQuestionOut.from_row(row) for row in created]


# --------------------------------------------------------------------------
# Eval runs
# --------------------------------------------------------------------------

@router.post(
    "/agents/{agent_id}/eval-runs",
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_eval_run(
    agent: OwnedAgent,
    user: CurrentUser,
    db: DbSession,
    background: BackgroundTasks,
    body: EvalRunCreate | None = None,
) -> EvalRunOut:
    """Stage a scorecard and start scoring it in the background. 202.

    The row exists, is committed and is addressable before any model is called --
    the same contract as document upload, and for the same reason: the client is
    handed an id it can poll, and `run_eval_job` ADOPTS this row rather than
    creating one, so there is never a `pending` row nothing will touch sitting
    beside a second row quietly filling in.

    Both model names are written HERE and again by the runner at start. Writing
    them now is what lets a queued run say what it is about to measure -- and in
    particular say `judge_is_generator` before it has produced a single answer.

    **A second concurrent run is refused with 409.** Two runs over the same
    golden set would double the API spend, race on `progress_done`, and produce
    two scorecards for one configuration with no way to tell which is which.
    """
    # SELECT ... FOR UPDATE on the agent row, and it closes a race rather than
    # narrowing one. Without it, two clicks a millisecond apart both read "no
    # live run" and both insert. There is no existing eval_runs row to lock --
    # the whole point is that none should exist -- so the agent is the only thing
    # both requests are guaranteed to contend on. `documents.remove_document`
    # takes the same approach for the same reason.
    await db.execute(select(Agent.id).where(Agent.id == agent.id).with_for_update())

    live = await db.scalar(
        select(EvalRun).where(
            EvalRun.agent_id == agent.id,
            EvalRun.status.in_(LIVE_RUN_STATUSES),
        )
    )
    if live is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "An evaluation is already running for this agent. Wait for it to "
                "finish, or delete it if it has stalled."
            ),
        )

    # The count and the run must agree, so both come from the same predicate --
    # `_active_set_query`, which is also what `app/eval/jobs.py` re-derives. A
    # `progress_total` computed from a different filter than the loop uses is a
    # progress bar that stops at 8 of 10 forever.
    questions = list(await db.scalars(_active_set_query(agent.id)))
    if not questions:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "This agent has no active golden questions. Add or suggest some "
                "before running an evaluation."
            ),
        )

    run = EvalRun(
        id=uuid.uuid4(),
        agent_id=agent.id,
        user_id=user.id,
        judge_model=settings.ragas_judge_model,
        # The agent's effective model, resolved the same way the pipeline
        # resolves it. `agents.generation_model` is nullable and null means "the
        # service default", so storing the null would record a run against a
        # model name nobody can look up later.
        generation_model=agent.generation_model or settings.generation_model,
        # `pending`, NOT `running`: nothing has started. The runner writes
        # `running` when it actually begins, and the two states are kept apart
        # because "queued" and "on question 3 of 10" are different answers to a
        # user watching a bar for five minutes.
        status="pending",
        progress_done=0,
        progress_total=len(questions),
        notes=body.notes if body else None,
    )
    db.add(run)

    _audit(
        db,
        user.id,
        "eval_run.create",
        "eval_run",
        run.id,
        agent_id=str(agent.id),
        question_count=len(questions),
        judge_model=run.judge_model,
        generation_model=run.generation_model,
        self_judged=run.judge_model == run.generation_model,
    )
    await db.commit()
    await db.refresh(run)

    # Scheduled AFTER the commit, so a task can never be queued against a row
    # that was rolled back. Ids only -- see `_suggest_job` and `app/rag/jobs.py`.
    background.add_task(run_eval_job, agent.id, run.id, user.id)

    return _run_out(run)


@router.get("/agents/{agent_id}/eval-runs")
async def list_eval_runs(agent: OwnedAgent, db: DbSession) -> list[EvalRunOut]:
    """This agent's runs, newest first.

    Newest first because the history view opens on the most recent scorecard and
    reads the one below it for comparison -- that pairing is the eval-driven loop.
    `ix_eval_runs_agent_created` indexes `(agent_id, created_at DESC)` so this is
    a forward scan; the `id` tie-break keeps two runs created in the same
    microsecond from swapping places between renders.

    Results are deliberately not included. A run carries one row per question and
    a history list is a list of headers -- `GET /api/eval-runs/{id}` is where the
    detail lives.
    """
    rows = await db.scalars(
        select(EvalRun)
        .where(EvalRun.agent_id == agent.id)
        .order_by(EvalRun.created_at.desc(), EvalRun.id.desc())
    )
    return [_run_out(row) for row in rows.all()]


@router.get("/eval-runs/{run_id}")
async def get_eval_run(run: OwnedRun, db: DbSession) -> EvalRunDetail:
    """One scorecard, with every question's row.

    Three tables in one statement rather than a walk over the results asking for
    each question and each answer: that shape is an N+1, and on an async session
    every one of those lazy loads is an implicit-IO refresh that raises
    MissingGreenlet rather than merely being slow.

    INNER JOIN to `golden_questions`, LEFT JOIN to `queries`. The first is safe
    because `eval_results.golden_question_id` is NOT NULL and cascades -- a
    result cannot outlive its question. The second must be outer:
    `eval_results.query_id` is SET NULL, so a purged query leaves a score with
    its provenance gone rather than taking the score with it, and that row still
    belongs in the scorecard.

    Ordered by the question's display order, so the scorecard and the editor list
    the same questions in the same sequence. `order_index` ties at 0 for a set
    nobody has reordered, hence the same three keys used everywhere else.
    """
    rows = await db.execute(
        select(EvalResult, GoldenQuestion, QueryRow)
        .join(GoldenQuestion, GoldenQuestion.id == EvalResult.golden_question_id)
        .outerjoin(QueryRow, QueryRow.id == EvalResult.query_id)
        .where(EvalResult.eval_run_id == run.id)
        .order_by(
            GoldenQuestion.order_index.asc(),
            GoldenQuestion.created_at.asc(),
            GoldenQuestion.id.asc(),
        )
    )

    results = [
        EvalResultOut(
            id=result.id,
            golden_question_id=question.id,
            question=question.question,
            expected_behaviour=question.expected_behaviour,
            query_id=result.query_id,
            answer=query.answer if query is not None else None,
            refused=query.refused if query is not None else None,
            behaviour_ok=result.behaviour_ok,
            faithfulness=result.faithfulness,
            answer_relevance=result.answer_relevance,
            context_precision=result.context_precision,
            context_recall=result.context_recall,
            error=result.error,
        )
        for result, question, query in rows.all()
    ]

    header = _run_out(run)
    return EvalRunDetail(**header.model_dump(), results=results)


@router.delete("/eval-runs/{run_id}")
async def delete_eval_run(
    run: OwnedRun, user: CurrentUser, db: DbSession
) -> dict[str, bool]:
    """Delete one scorecard and its per-question rows.

    **A run that is still going is refused**, because deleting the row out from
    under `run_eval_job` leaves it committing progress against nothing: SQLAlchemy
    raises `StaleDataError` on the next update, the job's own handler tries to
    mark a run failed that no longer exists, and the user sees a generic 500 in
    the log for what was actually a deliberate delete.

    The staleness escape hatch is not a hedge. A run can be abandoned at
    `running` by a deploy or an OOM kill and nothing will ever move it -- and
    while it sits there, `create_eval_run` refuses every new run for this agent.
    Refusing to delete it as well would leave the agent permanently unable to be
    evaluated. Past `RUN_STALE_AFTER` we take the trade openly, exactly as
    `documents.remove_document` does.

    **The eval turns are NOT deleted.** Each scored question wrote an archived
    conversation, a `queries` row, its citations and its trace, and those stay:
    they are the evidence, they are visible in the Trace view, and `query_id` is
    SET NULL rather than CASCADE precisely so that deleting a scorecard does not
    destroy the answers it was computed from.
    """
    if run.status in LIVE_RUN_STATUSES and (
        datetime.now(timezone.utc) - run.created_at < RUN_STALE_AFTER
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This evaluation is still running. Wait for it to finish or "
                "fail, then delete it."
            ),
        )

    _audit(
        db,
        user.id,
        "eval_run.delete",
        "eval_run",
        run.id,
        agent_id=str(run.agent_id),
        status_at_delete=run.status,
        judge_model=run.judge_model,
        generation_model=run.generation_model,
        # Copied into the log because the cascade takes the scores with the run,
        # and afterwards this is the only record of what it measured.
        summary=run.summary,
    )
    await db.delete(run)
    await db.commit()
    return {"ok": True}
