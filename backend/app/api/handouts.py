"""Handouts: make a file from the corpus, list them, download one, delete one.

`new features/04-handouts-panel.md` section 2.6, and section 4.5 of the
implementation plan for the frozen contract.

**Every path carries `{agent_id}`, and that is the whole authorisation story.**
`app/api/documents.py` explains at length why routes are nested under the agent
rather than flat: the Pinecone namespace is the tenancy boundary, a wrong one is
a successful cross-tenant read rather than an error, and a flat route would have
to carry the agent id in a body or a query param -- exactly the client-supplied
scoping PRD section 7 forbids. Handouts have no namespace, but they have bytes
and prompts made out of one agent's corpus, so the same rule applies for the
same reason: all five routes below take `OwnedAgent` and not one of them
contains a hand-written ownership check.

**`conversation_id` is the one client-supplied id that is NOT covered by that**,
and it is checked explicitly in `create_handout`. It arrives in a request body,
it is not an agent, and the job reads that thread's stored answers into the
prompt -- so an unchecked value would put another user's conversation into this
user's slide deck. That check is the highest-risk line in this module and it
says so where it sits.

------------------------------------------------------------------
THREE THINGS THIS MODULE IS CAREFUL ABOUT.

**`content` is never loaded except by the download route.** Two independent
guards: the column is `deferred()` on the model, so no ordinary `select(Handout)`
emits it, and `HandoutOut` has no `content` field at all, so nothing can
serialise it by accident either. The panel lists up to
`settings.handout_max_per_agent` (200) rows; eagerly loading bytea there returns
tens of megabytes for a list that renders filenames and sizes, and the symptom
is a slow network rather than anything pointing at this file.

**The filename is sanitised on the way out, not on the way in.** It is written
by a MODEL -- the sandbox harvests whatever name the generated code passed to
`savefig` -- and it reaches a `Content-Disposition` header. See `_safe`.

**The quota refuses; it never evicts.** A panel that deletes the user's oldest
slide deck to make room for a chart is worse than one that says no.
------------------------------------------------------------------
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Response, status
from fastapi.responses import RedirectResponse
from pydantic import (
    BaseModel,
    ConfigDict,
    ModelWrapValidatorHandler,
    StringConstraints,
    model_validator,
)
from sqlalchemy import func, inspect as sa_inspect, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from app import storage
from app.api.deps import CurrentUser, DbSession, OwnedAgent
from app.config import settings
from app.db.models import Agent, Conversation, Handout
from app.handouts.jobs import run_handout_job
from app.handouts.recipes import RECIPES, derive_title, provisional_filename

log = logging.getLogger("uvicorn.error")

# Carries the `/api/agents` prefix, so every path below is written
# `"/{agent_id}/handouts..."` -- the same shape as `app/api/documents.py`, and
# the reason `main.py` includes this router with no arguments.
router = APIRouter(prefix="/api/agents", tags=["handouts"])

# Anything outside this class is replaced in a downloaded filename. See `_safe`.
_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")

# Long enough that a descriptive name survives intact; short enough that no
# header this service emits can be pushed past a proxy's line limit by a model
# that decided to name a file after the whole brief.
MAX_FILENAME_CHARS = 120


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------


class HandoutRequest(BaseModel):
    """What the panel sends when the user presses "Make it".

    `extra="forbid"`, so a frontend that invents a field -- `title`, `filename`,
    `kind` -- is told immediately rather than having it silently ignored. Those
    three in particular are SERVER-derived on purpose: `kind` and `mime_type`
    come from the recipe, and letting a client name the mime type of a file this
    service will later serve back with `Content-Disposition` is how a download
    endpoint becomes a content-type confusion.
    """

    model_config = ConfigDict(extra="forbid")

    # A `Literal` rather than a lookup against `RECIPES`, so an unknown recipe is
    # a 422 from the schema -- with the four valid values in the error body --
    # rather than a 404 from a handler.
    recipe: Literal["chart", "deck", "sheet", "table"]
    # 1,000 characters. Long enough for a real brief with a list of what to
    # cover; short enough that it cannot become a prompt-injection surface the
    # size of a document. It is `strip_whitespace`d because a brief of three
    # spaces would otherwise pass `min_length` and produce a titleless handout.
    brief: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=1000),
    ]
    # Optional, and checked against the agent before anything is inserted. See
    # the module docstring.
    conversation_id: uuid.UUID | None = None


class HandoutOut(BaseModel):
    """One handout as the panel sees it.

    **There is deliberately no `content` field, and there must never be one.**
    That is the second of the two guards described in the module docstring -- the
    first being `deferred()` on the column. Either alone would be enough today;
    both together mean the mistake has to be made twice, in two files, to leak
    megabytes into a list response.

    `preview_text` and `source_code` are absent for a milder version of the same
    reason: a study sheet's markdown and a python-pptx script are each a few
    kilobytes, times 200 rows. They live on `HandoutDetail`, fetched when a user
    opens one.

    **`meta` is absent too, and that is a third guard rather than an oversight.**
    It is a free-form JSONB blob holding the brief, the chunk ids and the recipe
    key; putting it on the wire would hand a client the vocabulary that
    `extra="forbid"` on `HandoutRequest` exists to withhold. The two facts a
    panel actually needs are lifted out of it as flat, typed, nullable fields --
    see `_lift_from_meta`.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str
    title: str
    filename: str
    mime_type: str
    byte_size: int
    status: str
    origin: str
    error: str | None = None
    # WHAT KIND of failure that was, from `meta`: "import" | "syntax" |
    # "timeout" | "runtime" | "output" | "invalid". The first five are the
    # sandbox's own classification of the PROCESS; "invalid" is about the
    # ARTEFACT -- a file was produced and it does not open.
    #
    # A `str` and not an enum, matching `status`/`kind`/`origin` above and the
    # `String(16)` columns behind them: the set has already grown once, and a
    # closed type here would make this the file that breaks when it grows
    # again. It stays under 16 characters so it remains promotable to a column
    # if it is ever worth one.
    error_kind: str | None = None
    # 1, or 2 when the retry rescued the run. Recorded because "it worked first
    # time" and "it worked after reading its own traceback" are different facts
    # about the model, and `source_code` -- which holds both attempts joined --
    # makes them look the same to anything that is not a human reading it.
    attempts: int | None = None
    conversation_id: uuid.UUID | None = None
    query_id: uuid.UUID | None = None
    created_at: datetime

    @model_validator(mode="wrap")
    @classmethod
    def _lift_from_meta(
        cls, data: Any, handler: ModelWrapValidatorHandler["HandoutOut"]
    ) -> "HandoutOut":
        """Carry two values across from `meta`, and leave `meta` behind.

        A `wrap` validator rather than a `before` one, because the ordinary
        `from_attributes` path already does all the work: this runs after it and
        moves two values across, instead of re-listing every field in order to
        rebuild the input as a dict. It is inherited, so `HandoutDetail` gets it
        for free and the two shapes cannot disagree.

        **This reads `meta`, which is an ordinary column loaded by every
        `select(Handout)` in this module.** Unlike `content` it is not
        `deferred()`, so nothing here triggers the implicit IO that raises
        `MissingGreenlet` from inside Pydantic serialisation -- the same trap
        the `db.refresh` in `create_handout` guards against. A `load_only()`
        added later that dropped `meta` would break that, which is the one
        change to watch for.

        `getattr` with a default, so validating a plain dict still works and
        keeps whatever that dict supplied: the ORM row is the only input that
        has a `meta` to lift from.

        Both keys are missing from most rows and none of that is an error. A job
        that failed before the sandbox ran has no `error_kind` to record, a job
        that failed at all writes no `meta` today, and every row written before
        this change has neither. `None` is the honest answer in all three cases,
        which is why both fields carry a default -- an old row and an old client
        read the same thing.
        """
        model = handler(data)

        meta = getattr(data, "meta", None)
        if isinstance(meta, dict):
            kind = meta.get("error_kind")
            model.error_kind = kind if isinstance(kind, str) else None

            attempts = meta.get("attempts")
            # `isinstance(True, int)` is True, so bools are excluded by hand.
            # Nothing validates JSONB on the way in, and this reads back
            # whatever a job wrote -- including, one day, something that is not
            # a count.
            model.attempts = (
                attempts
                if isinstance(attempts, int) and not isinstance(attempts, bool)
                else None
            )

        return model


class HandoutDetail(HandoutOut):
    """One handout, opened. Adds the two big text columns and nothing else.

    Inherits rather than repeats, so a field added to the list shape cannot go
    missing from the detail shape -- and `content` stays absent from both by
    construction.
    """

    preview_text: str | None = None
    source_code: str | None = None


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _safe(filename: str) -> str:
    """A filename safe to put inside a `Content-Disposition` header.

    **The input is model-written.** The sandbox harvests whatever name the
    generated code passed to `savefig` or `prs.save`, and that name is stored on
    the row and comes back out here. A model asked to produce a chart "about the
    Q3 shortfall" may well name the file after the brief, and a brief is
    user-written -- so this string is attacker-influenced from two directions at
    once.

    Unescaped, that is header injection: a `\\r\\n` in the value ends the header
    and starts a new one, and a `"` ends the quoted filename and lets the rest be
    read as `Content-Disposition` parameters. Both are exactly the kind of thing
    a fluent model produces without meaning to.

    So the rule is an ALLOWLIST -- `[A-Za-z0-9._-]`, everything else collapsed to
    a single underscore -- and not a denylist of the characters that are known to
    be dangerous. A denylist here has to be right about every encoding a proxy
    might normalise; an allowlist only has to be right about what a filename
    needs.

        `a"; rm -rf /`   ->  `a_rm_-rf_`

    Two further properties fall out of the same rule and are worth naming
    because they are the reason no path handling is needed downstream: `/` and
    `\\` are not in the class, so nothing here can express a directory; and
    leading dots are stripped, so neither `..` nor a dotfile survives.

    The extension is preserved across truncation. Cutting `.pptx` off a
    150-character name would produce a file the operating system refuses to open
    with anything, which reads to the user as a corrupt handout rather than as a
    long name.
    """
    cleaned = _UNSAFE_FILENAME.sub("_", (filename or "").strip())
    cleaned = cleaned.lstrip(".")

    if len(cleaned) > MAX_FILENAME_CHARS:
        stem, dot, ext = cleaned.rpartition(".")
        # `1 <= len(ext) <= 8` is what distinguishes a real extension from a
        # name that merely contains a dot near the end.
        if dot and 1 <= len(ext) <= 8:
            cleaned = f"{stem[: MAX_FILENAME_CHARS - len(ext) - 1]}.{ext}"
        else:
            cleaned = cleaned[:MAX_FILENAME_CHARS]

    # Everything can legitimately be stripped away -- a filename of "..." or of
    # pure CJK leaves nothing behind -- and an empty `filename=""` is a header a
    # browser handles unpredictably.
    return cleaned or "handout"


async def _load_owned(
    # `AsyncSession`, not the `DbSession` alias: that alias carries a
    # `Depends(get_db)` inside it, which means something to FastAPI and nothing
    # to a plain helper.
    db: AsyncSession,
    agent: Agent,
    handout_id: uuid.UUID,
    *,
    with_content: bool = False,
) -> Handout:
    """One handout belonging to this agent, or 404.

    **The agent filter is in the WHERE clause, not in an `if` after the load.**
    The path pair is two client-supplied ids and only one of them has been
    authorised; loading by `handout_id` alone and comparing afterwards would
    work, and it would put a check where a check can be deleted. Selecting on
    both means a handout belonging to another agent is not fetched at all, so
    there is nothing for a later edit to forget to compare. `documents.py` makes
    the same argument for the same reason.

    404 rather than 403 on a cross-agent id: this route addresses `agent`'s
    handouts, and within that set the row does not exist. That the caller may
    own the *other* agent changes nothing -- a handout reachable through an
    agent that does not hold it means the route is not agent-scoped and the
    boundary is decorative.

    `with_content=True` is the ONLY place `content` is ever UNDEFERRED, and it
    is reached from one route. Everything else in this module loads the row
    without it.

    It is not, however, the only place the column is ever FETCHED, and that
    distinction is worth a sentence rather than a discovery. `download_handout`'s
    Postgres fallthrough selects `Handout.content` by hand, in a second
    statement, because whether it needs the bytes at all is a fact about the ROW
    -- `storage_key IS NULL` -- and that fact is not known until this query has
    already returned. Reading those two as one thing is the defect change set 15
    feature 03 fixed; the comment block at that fallthrough carries the whole
    argument.
    """
    query = select(Handout).where(
        Handout.id == handout_id,
        Handout.agent_id == agent.id,
    )
    if with_content:
        query = query.options(undefer(Handout.content))

    handout = await db.scalar(query)
    if handout is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Handout not found",
        )
    return handout


# --------------------------------------------------------------------------
# List
# --------------------------------------------------------------------------


@router.get("/{agent_id}/handouts")
async def list_handouts(
    agent: OwnedAgent,
    db: DbSession,
    conversation_id: uuid.UUID | None = None,
    kind: str | None = None,
    limit: int = Query(
        default=settings.handout_max_per_agent,
        ge=1,
        # The quota is the natural ceiling: there can never be more than that
        # many rows under one agent, so a larger limit could only ever return
        # the same set while inviting a client to ask for a page size the table
        # cannot produce.
        le=settings.handout_max_per_agent,
    ),
) -> list[HandoutOut]:
    """This agent's handouts, newest first.

    **THIS IS THE POLLING ENDPOINT.** Create answers 202 with `status="pending"`
    and the panel watches this list until nothing is pending any more -- so it is
    called every few seconds while anything is moving, and it must stay one
    round trip that touches no bytea.

    That is what the plain `select(Handout)` below buys: `content` is
    `deferred()`, so it is simply not in the emitted column list. Nothing here
    undefers it, nothing here reads `.content`, and `HandoutOut` has no field
    that could pull it in during serialisation. The one route that does want the
    bytes asks for them by name.

    `conversation_id` is a FILTER, not a scope, and needs no ownership check of
    its own: the `agent_id` predicate is already the boundary, so a
    conversation belonging to somebody else simply matches nothing. This is the
    opposite of the create route, where the same id decides what goes INTO a
    prompt -- worth noticing that the same parameter is dangerous in one place
    and inert in the other.

    Ordering matches `ix_handouts_agent_created`, which is declared DESC on
    `created_at` precisely so that the newest-first read -- which is every read
    -- is a forward index scan. The id tiebreak makes it deterministic: several
    handouts from one turn share a `created_at` to the microsecond, and without
    it the panel reshuffles them on every poll.
    """
    query = select(Handout).where(Handout.agent_id == agent.id)
    if conversation_id is not None:
        query = query.where(Handout.conversation_id == conversation_id)
    if kind is not None:
        query = query.where(Handout.kind == kind)

    rows = await db.scalars(
        query.order_by(Handout.created_at.desc(), Handout.id).limit(limit)
    )
    return [HandoutOut.model_validate(row) for row in rows.all()]


# --------------------------------------------------------------------------
# Create
# --------------------------------------------------------------------------


@router.post("/{agent_id}/handouts", status_code=status.HTTP_202_ACCEPTED)
async def create_handout(
    agent: OwnedAgent,
    user: CurrentUser,
    db: DbSession,
    background: BackgroundTasks,
    body: HandoutRequest,
) -> HandoutOut:
    """Stage a handout and hand the work off. 202, not 201.

    The shape is `documents.py`'s upload handoff, deliberately: everything cheap
    happens now -- validate the conversation, check the quota, insert a
    `pending` row, commit -- and everything slow happens in
    `app/handouts/jobs.py` afterwards, in a session of its own. The response
    body is the row itself, so the panel has an id to poll before the model has
    written a line of code.

    `user` is requested alongside `agent` even though `owned_agent` already
    depends on it. FastAPI caches a dependency per request, so this is a
    dictionary lookup rather than a second session resolution, and it keeps
    `created_by_user_id` reading from the same object the authorisation check
    used.
    """
    recipe = RECIPES[body.recipe]

    # ------------------------------------------------------------------
    # THE ONE CLIENT-SUPPLIED ID THAT `OwnedAgent` DOES NOT COVER.
    #
    # `conversation_id` arrives in the request body. The job passes it to
    # `gather_material`, which reads that thread's stored questions and answers
    # straight into the generation prompt -- so an unvalidated value is not a
    # broken foreign key, it is another user's conversation rendered into this
    # user's slide deck, downloaded and kept. The failure is silent and the
    # artefact looks perfectly normal.
    #
    # Checked on the PAIR, exactly as `_load_owned` checks a handout: the
    # conversation must exist AND belong to this agent, which has already been
    # through `owned_agent`. 404 rather than 403 for the same reason as there.
    #
    # `recipes._recent_answers` filters on `agent_id` a second time, in the job.
    # That is not redundancy for its own sake -- the job is reachable with two
    # bare ids and no route in front of it, so it cannot rely on this check
    # having happened.
    # ------------------------------------------------------------------
    if body.conversation_id is not None:
        owns_conversation = await db.scalar(
            select(func.count())
            .select_from(Conversation)
            .where(
                Conversation.id == body.conversation_id,
                Conversation.agent_id == agent.id,
            )
        )
        if not owns_conversation:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Conversation not found for this agent",
            )

    # ------------------------------------------------------------------
    # QUOTA, BEFORE THE INSERT. Refused, never silently evicted.
    #
    # The alternative -- drop the oldest row to make room -- is worse than a
    # refusal in the way that matters: a user who is told "no" can delete
    # something and try again, while a user whose oldest slide deck vanished to
    # make room for a chart has lost work and has not been told. Handout bytes
    # live in Postgres and nowhere else (PRD open item 10 tracks object storage),
    # so an eviction here is a permanent deletion.
    #
    # Counted over ALL of this agent's handouts, both origins. The quota is a
    # bound on what one agent stores, and a tool-produced chart costs exactly as
    # much bytea as a recipe-produced one.
    #
    # This is checked, not enforced: two requests racing past it can both insert
    # and leave the agent one over. That is the right trade -- the alternative is
    # a table lock on a read-mostly path to defend a soft limit whose only
    # consequence of being exceeded by one is that the next request is refused.
    # ------------------------------------------------------------------
    existing = await db.scalar(
        select(func.count()).select_from(Handout).where(Handout.agent_id == agent.id)
    )
    if (existing or 0) >= settings.handout_max_per_agent:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"This agent already has {existing} handouts, which is the limit "
                f"of {settings.handout_max_per_agent}. Delete one and try again. "
                "Nothing is removed automatically."
            ),
        )

    # THE STAGED ROW. It exists, is committed and is addressable before any slow
    # work starts, which is the entire contract that makes the handover work.
    # `run_handout_job` ADOPTS this row rather than creating one, so there is
    # never a `pending` row nothing will touch sitting beside a second row
    # quietly going `ready`.
    #
    # `filename` and `mime_type` are NOT NULL and the panel renders the row
    # while it is still pending, so both are filled in now from the recipe and
    # the brief. The job overwrites `filename` with whatever the generated code
    # actually wrote.
    handout = Handout(
        # Explicit rather than left to the column default, because
        # `background.add_task` below needs the id and a Python-side default is
        # only populated at flush.
        id=uuid.uuid4(),
        agent_id=agent.id,
        conversation_id=body.conversation_id,
        # No `query_id`: a recipe handout is made outside any turn, which is
        # exactly what a NULL here means. See the model.
        created_by_user_id=user.id,
        kind=recipe.kind,
        title=derive_title(body.brief),
        filename=provisional_filename(recipe, body.brief),
        mime_type=recipe.mime_type,
        byte_size=0,
        status="pending",
        origin="recipe",
    )
    db.add(handout)
    await db.commit()

    # Insurance, not bookkeeping. `handouts.created_at` is a server default, and
    # an unloaded attribute on an async session refreshes itself with implicit
    # IO -- which raises `MissingGreenlet` from inside Pydantic serialisation,
    # a 500 whose traceback points at the serialiser rather than at the column.
    #
    # `content` stays deferred through this refresh, so this is not a bytea read
    # -- and at this point the column is NULL anyway.
    await db.refresh(handout)

    # IDS AND PLAIN VALUES ONLY. Passing `agent`, `handout` or `db` would hand a
    # background task objects belonging to a session FastAPI closes as this
    # request finishes -- see the comment block at the top of
    # `app/handouts/jobs.py`, where that trap is explained and guarded against.
    #
    # Scheduled AFTER the commit, so a task can never be queued against a row
    # that was rolled back. A job that started against an uncommitted row would
    # find nothing, log, and return -- leaving the user polling a row that will
    # never move.
    background.add_task(
        run_handout_job,
        agent.id,
        handout.id,
        user.id,
        body.recipe,
        body.brief,
        body.conversation_id,
    )
    return HandoutOut.model_validate(handout)


# --------------------------------------------------------------------------
# Detail
# --------------------------------------------------------------------------


@router.get("/{agent_id}/handouts/{handout_id}")
async def get_handout(
    agent: OwnedAgent, db: DbSession, handout_id: uuid.UUID
) -> HandoutDetail:
    """One handout, with its preview text and the code that made it.

    Fetched when the user opens a card or presses "Code", never as part of a
    list -- which is why `preview_text` and `source_code` are on this shape and
    not on `HandoutOut`.

    **`source_code` is returned to the user on purpose.** NotebookLM hides the
    generation step; for a product whose whole purpose is making a pipeline
    inspectable, that would be the one place it stopped practising what it
    teaches. It is also the fastest way for somebody to see why a chart is
    wrong. When a run took two attempts, both are here, joined -- so the
    correction is visible too.

    `content` is not undeferred: `HandoutDetail` has no field for it, and
    nothing below reads `.content`.
    """
    handout = await _load_owned(db, agent, handout_id)
    return HandoutDetail.model_validate(handout)


# --------------------------------------------------------------------------
# Download
# --------------------------------------------------------------------------


@router.get("/{agent_id}/handouts/{handout_id}/download")
async def download_handout(
    agent: OwnedAgent, db: DbSession, handout_id: uuid.UUID
) -> Response:
    """The bytes. The only route in this module that loads `content`.

    **A cookie-authenticated GET, deliberately.** That is what lets the frontend
    use a plain `<a href download>` instead of fetching into a blob, holding a
    whole `.pptx` in browser memory and synthesising an object URL. The
    codebase-wide `credentials: "include"` default and the same-site `none`
    cookie configuration are already in place, so the anchor simply works.

    409 rather than 404 on a row that is not `ready`, and the distinction is
    real: the handout exists and the user can see it in the panel, it just has
    no bytes yet. A 404 would read as "this was deleted" for a row that is
    visibly sitting there with a spinner on it.

    `content is None` is checked alongside the status rather than trusting the
    status alone. They are written in the same transaction so they cannot
    disagree today -- but this is the one place a `None` would reach
    `Response(content=...)`, which serialises it as the four bytes `None`,
    producing a downloaded `.png` that is not a PNG.

    **On the R2 route this answers 302 and the bytes never enter this process.**
    That is the point of the move rather than a side effect: Render's starter
    plan runs a single uvicorn worker, and the previous shape read up to
    `sandbox_max_artifact_bytes` into the heap of the only worker there is, on
    every download. Authorisation still happens HERE -- `_load_owned` has already
    proved the row belongs to this agent, and the presigned URL is minted only
    after it has. The bucket is private; an unsigned GET returns 400.

    **A `ready` row with no `storage_key` falls through to Postgres even when
    the route is R2**, and fetching its bytes then costs a second statement --
    see the comment block at that fallthrough. That row shape is what makes the
    documented `STORAGE_ROUTE=postgres` rollback survivable, and until change
    set 15 feature 03 it answered 500.

    Three things are deliberately preserved across the two roads, because each
    has a consumer that would break silently:

    - **The 409 on a row that is not ready.** `HandoutCard` gates the chart
      thumbnail on `status === "ready"` precisely because this route answers 409
      otherwise, and `types.ts` encodes it.
    - **`Content-Disposition` with a sanitised filename.** On the R2 road it is
      emitted by Cloudflare, from the `response-content-disposition` parameter
      `app/storage.presigned_get_url` signs into the URL. `_safe` is applied here
      as before AND again inside the seam, so the guard has to be missed twice.
    - **The mime type**, which comes from the column either way and never from
      whatever R2 has stored on the object.

    What is NOT preserved is `Cache-Control: private, no-store`. A presigned URL
    IS the capability, so the only control left is how long it lives
    (`r2_presign_ttl_s`, five minutes). Recorded as an accepted loss rather than
    solved -- see the change set's risk register.
    """
    # `with_content` is False on the R2 road: the bytes are not needed to build a
    # redirect, and undeferring them would reintroduce exactly the cost this
    # route exists to remove.
    #
    # THIS IS AN OPTIMISATION AND NO LONGER A CORRECTNESS DECISION, which is the
    # half of change set 15 feature 03 that is easiest to read past.
    # `storage.enabled()` is a fact about the DEPLOYMENT; whether this particular
    # download ends up needing bytes out of Postgres is a fact about the ROW, and
    # the two disagree for exactly one shape -- a `ready` row with
    # `storage_key IS NULL`, on the R2 road. Reading the deferred attribute in
    # that gap was a 500. The fallthrough below no longer depends on this line
    # having guessed right; all it decides now is whether the bytes arrive in
    # THIS round trip or in a second one. On the Postgres road every ready row
    # needs them, so fetching them here saves a statement; on the R2 road almost
    # no row does.
    handout = await _load_owned(db, agent, handout_id, with_content=not storage.enabled())

    if handout.status != "ready":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"This handout is not ready yet (status: {handout.status})."
                if handout.status != "failed"
                else f"This handout failed: {handout.error or 'no reason recorded'}"
            ),
        )

    if storage.enabled() and handout.storage_key:
        try:
            url = storage.presigned_get_url(
                handout.storage_key,
                filename=_safe(handout.filename),
                mime_type=handout.mime_type,
            )
        except storage.StorageError as exc:
            # 503, not 500, and it names the store. This is the shape an expired
            # API token takes -- every download failing at once while the
            # application is provably unchanged and every offline harness stays
            # green. A generic 500 would send the reader into the wrong module.
            log.error("Could not sign a download URL for %s: %s", handout.id, exc)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Object storage is unavailable, so this file cannot be served.",
            ) from exc

        # 302, not 307. A permanent or method-preserving redirect would be wrong
        # for a URL that expires in five minutes, and 302 is what browsers,
        # `<img>` and httpx all follow for a GET without argument.
        return RedirectResponse(url, status_code=status.HTTP_302_FOUND)

    # ------------------------------------------------------------------
    # THE POSTGRES ROAD -- reachable two ways: `storage_route` is "postgres", or
    # the route is R2 and this row has no key. The second is why this is a
    # fallthrough rather than an error: during the blue/green window both kinds
    # of row exist and both must download.
    #
    # **`content` IS NOT NECESSARILY LOADED HERE, and reading it when it is not
    # was an unconditional 500.** That is the defect this block exists to fix.
    # On the R2 road the load above leaves the column `deferred()`, so the old
    # `handout.content` was implicit IO on an `AsyncSession`: `MissingGreenlet`,
    # raised from inside greenlet adaptation, naming neither this line nor the
    # column nor the table. It did not arrive legibly at the browser either --
    # the handler raises before `CORSMiddleware` can add its headers, so the
    # failure presents as a CORS error and sends the reader into the wrong
    # module entirely. `admin_check.py` records the identical misdirection for
    # `GET /api/admin/spend`.
    #
    # NOBODY IS BROKEN BY IT TODAY, and that is the argument for fixing it
    # rather than filing it. Counted against production, 2026-08-23: **ZERO**
    # keyless rows are `ready` -- every one of them is `failed`, so no download
    # that exists takes this branch on the R2 road. The absolute count of
    # keyless rows is deliberately NOT quoted here: a live row count in a
    # comment is perishable by construction and stops being true without anybody
    # editing the file, and only the zero is load-bearing.
    #
    # What it breaks is the documented
    # `STORAGE_ROUTE=postgres` ROLLBACK -- switch to postgres, write handouts,
    # switch back, and every one of those rows 500s forever. A rollback that
    # does not work is worse than no rollback, because it is only ever exercised
    # in an emergency, by somebody already handling one.
    #
    # **The condition asks the OBJECT, not the deployment**, and that is the
    # defect restated in reverse rather than fussiness. The bug was a fact about
    # the deployment standing in for a fact about the row, so writing
    # `if storage.enabled():` here would reproduce the same mistake in a new
    # place and stay correct only for as long as nothing upstream changed its
    # mind. `sa_inspect(handout).unloaded` is set arithmetic over the instance's
    # own attribute dict -- no query, no IO, nothing that can raise -- and it
    # cannot disagree with the load that actually happened. It stays right if
    # the `with_content` line above is changed, or deleted.
    #
    # **What this must NOT become is an unconditional undefer.** That is the
    # whole cost the R2 route exists to remove: Render's starter plan runs a
    # single uvicorn worker, and undeferring at the load would put up to
    # `sandbox_max_artifact_bytes` back through that one heap on EVERY download,
    # redirects included. It would also pass every case written for this defect,
    # which is why the harness measures the negative directly rather than
    # trusting the positive.
    #
    # Pinned by `scripts/storage_check.py --live`. Case 78: a `ready` keyless row
    # answers 200 with byte-identical content and a quoted disposition. Case 78b:
    # the bytea is read on the keyless road AND NOT on the keyed one -- that
    # second half is the guard against the unconditional fix, and nothing else in
    # the repository catches it, because S8, S8b, S8c, S28 and S29 all follow
    # redirects and stay green against a route that stopped emitting any. Case
    # 79b is the second latent 500 at this same line, and the one that survives a
    # careless repair: the `content is None` test below IS the read that raised,
    # so the row with neither a key nor bytes was precisely the row that never
    # reached the 409 written for it.
    #
    # **AND CASE 78d IS THE ONLY THING PINNING THE `if`/`else` DIRECTLY BELOW.**
    # It is named here because the three cases above are not enough and an
    # earlier version of this comment listed only those three -- an invitation to
    # read the named guards, delete the `else`, refetch unconditionally, and
    # watch every one of them pass. MEASURED against exactly that edit,
    # 2026-08-23: 78 stayed 200 at 52/52 bytes, 78b stayed keyless=1/keyed=0, 79b
    # stayed 409, and 78d alone went red at 2 statements mentioning the column
    # where 1 is correct. A comment that names a subset of the guards is worse
    # than one that names none, because it tells an editor when to stop looking.
    #
    # `agent_id` is repeated on this second statement even though `_load_owned`
    # has already proved it. That docstring makes the argument -- a scoping check
    # belongs in a WHERE clause, where an unrelated-looking edit cannot delete it
    # -- and the property worth being able to establish by grep is that no
    # statement in this module fetches a handout without naming its agent. It
    # costs nothing; this is the same primary-key lookup either way.
    #
    # **That property is now ASSERTED, by case 77c, and it was not before.**
    # Deleting `Handout.agent_id == agent.id` below left both modes of
    # `storage_check.py` fully green (measured 2026-08-23) -- which is what a
    # defence-in-depth check looks like by definition: redundant, therefore
    # invisible to any assertion about a response, therefore deletable by a tidy
    # edit with nothing going red. 77c parses this module and requires every
    # `select(...)` chain that mentions `Handout` to mention `Handout.agent_id`.
    # A paragraph claiming a property is greppable is not a grep.
    # ------------------------------------------------------------------
    if "content" in sa_inspect(handout).unloaded:
        content = await db.scalar(
            select(Handout.content).where(
                Handout.id == handout.id,
                Handout.agent_id == agent.id,
            )
        )
    else:
        content = handout.content

    # `content is None` is now two facts arriving at one answer: the row never
    # had bytes, or it was deleted between the two statements above. A 409 saying
    # there is no stored file is honest about both, and a download racing a
    # delete is a real sequence -- the panel offers both actions on one card.
    if content is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This handout has no stored file.",
        )

    return Response(
        content=content,
        media_type=handout.mime_type,
        headers={
            # `_safe` is what stands between a model-written filename and this
            # header. Read its docstring before changing the quoting here: the
            # value is quoted AND sanitised, and neither alone is sufficient.
            "Content-Disposition": f'attachment; filename="{_safe(handout.filename)}"',
            # The bytes are user data behind a session cookie. Nothing should
            # cache them, least of all a shared proxy.
            "Cache-Control": "private, no-store",
        },
    )


# --------------------------------------------------------------------------
# Delete
# --------------------------------------------------------------------------


@router.delete("/{agent_id}/handouts/{handout_id}")
async def remove_handout(
    agent: OwnedAgent, db: DbSession, handout_id: uuid.UUID
) -> dict:
    """Delete one handout, and its object if it has one.

    **This docstring used to say "Row only -- there is nothing else to clean
    up", and that sentence is why it is being rewritten rather than extended.**
    It was true and load-bearing: a handout's bytes lived in its own row, so
    deleting the row deleted them, atomically and for free. With bytes in R2 it
    is false, and a comment asserting that no external store exists is worse
    than no comment at all -- it actively tells the next reader not to look.

    **Object first, row second**, which is the ordering `app/rag/delete.py`
    already argues for Pinecone and adopts here for the same asymmetry: a row
    with no object is visible in the panel and can be deleted again, while an
    object with no row is unreachable forever, because the key is derived from
    an id that no longer exists anywhere. Given a choice of which orphan to
    risk, take the one a human can still see.

    A failed object delete does NOT block the row delete. The user asked for the
    handout to be gone; refusing because a bucket was briefly unreachable would
    leave them staring at a file they have already deleted twice. The leak is
    logged and reconcilable by prefix; the alternative is not recoverable by the
    user at all.

    Worth contrasting with `documents.py`, which refuses to delete a document
    that is mid-ingest: there, the row is the only record of which vectors exist
    in Pinecone, so deleting under a running job strands them permanently. Here
    a `pending` handout stays deletable on purpose -- the job's commit finds no
    row, its own handler catches it, and `_settle` finds nothing to mark. That
    remains the escape hatch for a row abandoned by a restart mid-job.
    """
    handout = await _load_owned(db, agent, handout_id)

    if handout.storage_key:
        try:
            await asyncio.to_thread(storage.delete_object, handout.storage_key)
        except storage.StorageError as exc:
            log.warning(
                "Handout %s deleted but its object was not: %s", handout.id, exc
            )

    await db.delete(handout)
    await db.commit()
    return {"ok": True}
