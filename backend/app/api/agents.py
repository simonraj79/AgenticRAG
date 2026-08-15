"""Agents and templates: create, list, tune, delete.

PRD sections 3.7 and 4.2. Four things in this module are decisions rather than
plumbing, and each one is invisible in the code unless it is pointed at:

**1. Creating from a template COPIES the parameters onto the agent row.** It
does not store a reference and read through it. This is the single most
misreadable line in the file, because a foreign key called `template_id` sitting
next to a set of duplicated columns reads exactly like a normalisation mistake
somebody should clean up. It is not: PRD 4.2 requires that editing a template
never silently re-tune agents somebody already built and evaluated. An agent's
parameters are part of its measured behaviour, so an agent whose config could
change under it -- because an admin edited a shared preset -- would invalidate
every eval run recorded against it, with nothing in the trace to show why the
numbers moved. `template_id` survives creation for provenance only: it answers
"where did this start", never "what is this now".

**2. Every single-agent route resolves through `OwnedAgent`.** Not one of them
filters by owner inline. See `app/api/deps.py`: a forgotten `.where()` is not a
bug that raises, it is a successful cross-tenant read.

**3. `embedding_model` is not editable.** A config PATCH is exactly the shape of
request that would let it through silently, so it is refused explicitly -- see
`update_agent`.

**4. Deleting an agent deletes its vectors first.** Same ordering argument as
`app/rag/delete.py`: orphaned rows are visible and re-deletable, orphaned vectors
are unreachable and permanent.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal, NoReturn

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import case, delete as sa_delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, DbSession, OwnedAgent
from app.db.models import Agent, AgentTemplate, AuditLog, Document, User
from app.db.seed import TEMPLATE_SLUGS
from app.rag.delete import delete_agent_namespace

router = APIRouter(prefix="/api", tags=["agents"])


# The parameters a template supplies to a new agent. Spelled out as one tuple
# rather than copied field by field so that adding a tunable column cannot leave
# it copied here and missing from the editor, or the reverse. Note what is NOT
# in it: `slug`, `name`, `description` and `is_active` describe the template, not
# the agent, and `id` is the provenance link that stays in `template_id`.
TEMPLATE_PARAMETERS: tuple[str, ...] = (
    "chunk_size",
    "chunk_overlap",
    "splitter",
    "retrieve_k",
    "rerank_enabled",
    "rerank_top_n",
    "score_threshold",
    "max_rewrites",
    "system_prompt",
)

# Postgres' SQLSTATE for a unique violation. Used to make sure a 409 is only
# ever claimed for a genuine duplicate -- see `_conflict_or_reraise`.
_UNIQUE_VIOLATION = "23505"

# Template display order. `created_at` cannot supply it (all three are seeded in
# one migration, in one transaction, with one timestamp) and alphabetical order
# would put "From scratch" at the top of the picker -- the worst possible first
# option for a user who came here to be handed a starting point. The seed's
# declaration order is the intended one: the PRD default first, the blank canvas
# last. Anything not seeded by this repo sorts after the three, by name.
_TEMPLATE_ORDER = case(
    {slug: index for index, slug in enumerate(TEMPLATE_SLUGS)},
    value=AgentTemplate.slug,
    else_=len(TEMPLATE_SLUGS),
)

# Stripped and length-checked at the edge. `agents.name` is String(128), so an
# over-long name is a 422 here rather than a DataError from the driver, and a
# name of pure whitespace collapses to "" and fails `min_length` instead of
# becoming an agent nobody can identify in a list.
AgentName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)
]

# `ingest._build_splitter` branches on exactly one value: "markdown" selects the
# heading-aware separators, anything else takes the plain recursive path. The
# enumeration exists so that a typo ("markdwon") is a 422 instead of a silent
# downgrade to a splitter the user did not choose and cannot see they got.
SplitterName = Literal["markdown", "recursive"]


# --------------------------------------------------------------------------
# Response and request models
# --------------------------------------------------------------------------

class TemplateOut(BaseModel):
    """A preset, as the create-agent picker sees it.

    `system_prompt` is deliberately absent. It is long, it is the load-bearing
    safety control (see `app/db/seed.py`), and it is not a choice the picker
    offers -- the copy lands on the agent, where `AgentOut.system_prompt` exposes
    it for editing against the agent that will actually use it.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    slug: str
    name: str
    description: str | None = None
    chunk_size: int
    chunk_overlap: int
    splitter: str
    retrieve_k: int
    rerank_enabled: bool
    rerank_top_n: int
    score_threshold: float
    max_rewrites: int


class AgentOut(BaseModel):
    """One agent and its effective configuration.

    Every parameter here is the agent's own column, never read through
    `template_id`. What the UI shows is what the next ingest and the next query
    will actually use.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None = None
    status: str
    visibility: str
    template_id: uuid.UUID | None = None
    embedding_model: str | None = None
    chunk_size: int
    chunk_overlap: int
    splitter: str
    retrieve_k: int
    rerank_enabled: bool
    rerank_top_n: int
    score_threshold: float
    max_rewrites: int
    system_prompt: str | None = None
    # Not a column on `agents`: an aggregate over `documents`, supplied by the
    # route. It carries a default so `model_validate` against the ORM object
    # succeeds, and `from_agent` is the only sanctioned way to fill it -- a
    # response built any other way silently reports an empty corpus.
    document_count: int = 0
    created_at: datetime

    @classmethod
    def from_agent(cls, agent: Agent, document_count: int) -> AgentOut:
        return cls.model_validate(agent).model_copy(
            update={"document_count": document_count}
        )


class AgentCreate(BaseModel):
    """Body for POST /api/agents.

    Three fields, and deliberately no tunables: parameters come from the
    template (or from the model defaults), never from the request, which is what
    makes "create from a template" and "create your own" one code path rather
    than two. Unknown keys are ignored rather than rejected -- a client that
    posts `chunk_size` here gets a working agent carrying the template's value
    and can PATCH it a moment later, which is a far smaller harm than a failed
    creation. `AgentUpdate` takes the opposite line, and the asymmetry is
    explained there.
    """

    name: AgentName
    description: str | None = None
    # Omitted means "from scratch": model defaults, and `template_id` stays
    # null. There is a `from-scratch` TEMPLATE too, and picking it is a
    # different, equally valid route to nearly the same agent -- it supplies the
    # same values plus a minimal grounding prompt, and records the provenance.
    template_id: uuid.UUID | None = None


class AgentUpdate(BaseModel):
    """Partial config update. Only the fields actually SENT are applied.

    `extra="forbid"`, unlike `AgentCreate`. The failure modes are not symmetric:
    an ignored extra field on create costs a PATCH, while an ignored extra field
    on update is a tuning UI that lies -- the user types `rerank_topn: 5`, gets a
    200, and then debugs an agent that is still reranking to 3 with nothing
    anywhere to say the value never landed. A 422 naming the unknown field is the
    only outcome that cannot mislead.

    The bounds are cheap insurance against configurations that fail somewhere
    else, later, in a message that points nowhere near this request.
    """

    model_config = ConfigDict(extra="forbid")

    name: AgentName | None = None
    description: str | None = None

    # Upper bound is gemini-embedding-2's 8,192-token input ceiling: a larger
    # chunk is truncated at embed time and the tail is lost silently, with the
    # `chunks.text` row still showing the full text. Lower bound is the point
    # below which a chunk carries no context to retrieve on.
    chunk_size: int | None = Field(default=None, ge=64, le=8192)
    chunk_overlap: int | None = Field(default=None, ge=0, le=4096)
    splitter: SplitterName | None = None

    # k is the reranker's input size, and rerank was measured at ~830 ms of the
    # request. This is a sanity bound, not a platform limit.
    retrieve_k: int | None = Field(default=None, ge=1, le=100)
    rerank_enabled: bool | None = None
    rerank_top_n: int | None = Field(default=None, ge=1, le=100)

    # Pinecone's cosine metric is higher-is-closer (see `search_with_scores`), so
    # this is a floor, not a distance. A negative value would only encode "never
    # rewrite", which `max_rewrites=0` says far more clearly.
    score_threshold: float | None = Field(default=None, ge=0.0, le=1.0)

    # The workshop names cost blowout as one of four agentic failure modes, and
    # an unbounded rewrite loop is precisely how it happens. PRD 3.5 sets 2; the
    # ceiling is here so no config can turn the loop into a spiral.
    max_rewrites: int | None = Field(default=None, ge=0, le=5)
    system_prompt: str | None = None

    # Declared ONLY so it can be refused with an explanation rather than with a
    # generic "extra fields not permitted". See `update_agent`.
    embedding_model: str | None = None


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _conflict_or_reraise(exc: IntegrityError, name: str) -> NoReturn:
    """Turn a duplicate-name violation into a 409, and nothing else into one.

    `agents` carries `UniqueConstraint("owner_user_id", "name")`, and the 409 is
    driven off the constraint failing rather than off a pre-flight SELECT: two
    concurrent creates both pass a pre-check and the second still fails at
    INSERT, so a pre-check adds a query and removes no error path.

    The SQLSTATE test matters. Mapping every IntegrityError to 409 would answer
    "that name is taken" to a failure that has nothing to do with the name -- a
    template deleted mid-request, say -- and send the user into a renaming loop
    that cannot possibly succeed. A driver that does not expose `sqlstate` falls
    through to 409, because on this statement the unique constraint is the only
    one a user's input can realistically breach.
    """
    if getattr(exc.orig, "sqlstate", None) not in (_UNIQUE_VIOLATION, None):
        raise exc
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=f"You already have an agent named {name!r}.",
    ) from exc


def _audit(
    db: AsyncSession,
    user: User,
    action: str,
    agent_id: uuid.UUID,
    metadata: dict[str, Any],
) -> None:
    """Record one lifecycle event. PRD 4.5.

    Day 1's argument for building rather than buying rests on "a full audit
    trail"; this table is what makes that true, so create and delete write here
    even though `agents` itself already shows the current state. Delete
    especially: after the row cascades away, this is the only record that the
    agent -- and its whole corpus -- ever existed.

    The column is `metadata` in Postgres but MUST be written as `audit_metadata`
    in Python: `metadata` is reserved on a declarative Base (it is the MetaData
    registry), so the mapping renames it. Everything in `metadata` has to be
    JSON-serialisable for JSONB, which is why ids are stringified.

    Added to the session, not committed -- the caller owns the transaction, so
    the audit row lands with the change it describes or not at all.
    """
    db.add(
        AuditLog(
            user_id=user.id,
            action=action,
            resource_type="agent",
            resource_id=str(agent_id),
            audit_metadata=metadata,
        )
    )


async def _document_count(db: AsyncSession, agent_id: uuid.UUID) -> int:
    """How many documents this agent holds. One row, one aggregate."""
    total = await db.scalar(
        select(func.count()).select_from(Document).where(Document.agent_id == agent_id)
    )
    return int(total or 0)


# --------------------------------------------------------------------------
# Templates
# --------------------------------------------------------------------------

@router.get("/agent-templates")
async def list_templates(user: CurrentUser, db: DbSession) -> list[TemplateOut]:
    """The presets available when creating an agent.

    `user` is unused in the body and is not decoration: it makes the route
    require a session. Templates are seed data rather than tenant data, so
    nothing here leaks across users -- but the only caller is the authenticated
    create-agent screen, and an endpoint reachable without a cookie is one more
    thing to reason about for a benefit nobody asked for.

    Inactive templates are filtered out rather than flagged. `is_active` is how a
    preset is retired, and a retired preset that still appears in the picker has
    not been retired.
    """
    rows = await db.scalars(
        select(AgentTemplate)
        .where(AgentTemplate.is_active.is_(True))
        .order_by(_TEMPLATE_ORDER, AgentTemplate.name)
    )
    return [TemplateOut.model_validate(row) for row in rows.all()]


# --------------------------------------------------------------------------
# Agents
# --------------------------------------------------------------------------

@router.get("/agents")
async def list_agents(user: CurrentUser, db: DbSession) -> list[AgentOut]:
    """The caller's agents, newest first, each with its document count.

    One statement, one round trip. The obvious alternative -- list the agents,
    then count documents per agent -- is an N+1 that grows with the dashboard,
    and on an async session each of those follow-up loads is a separate awaited
    query rather than the cheap lazy access it looks like in the source.

    `func.count(Document.id)` rather than `count(*)`: the join is an OUTER join
    so that an agent with no corpus still appears, and on those rows `count(*)`
    would count the single all-NULL join row and report 1 document. Counting a
    column that is NULL on a miss reports 0, which is the truth.

    GROUP BY the primary key alone is legal here -- Postgres knows every other
    `agents` column is functionally dependent on it -- so the whole ORM entity
    comes back without listing its columns.
    """
    rows = await db.execute(
        select(Agent, func.count(Document.id))
        .outerjoin(Document, Document.agent_id == Agent.id)
        .where(Agent.owner_user_id == user.id)
        .group_by(Agent.id)
        # Newest first: the agent you just created is the one you came back to
        # click.
        .order_by(Agent.created_at.desc())
    )
    return [AgentOut.from_agent(agent, count) for agent, count in rows.all()]


@router.post("/agents", status_code=status.HTTP_201_CREATED)
async def create_agent(
    body: AgentCreate, user: CurrentUser, db: DbSession
) -> AgentOut:
    """Create an agent, optionally starting from a template.

    Nothing is provisioned in Pinecone here, and nothing needs to be: a
    namespace is created lazily on first upsert and `Agent.namespace` is derived
    from the id, so an agent with no documents simply has no namespace yet. That
    is also why `status` starts at "empty" and `embedding_model` stays NULL --
    ingest stamps the model only on the success path, so a non-null value means
    "there are vectors under this model", not "this is the model we intend to
    use". Setting it here would make the mismatch check in PRD 7 compare against
    a model that never embedded anything.
    """
    template: AgentTemplate | None = None
    if body.template_id is not None:
        template = await db.scalar(
            select(AgentTemplate).where(
                AgentTemplate.id == body.template_id,
                # A retired template is not a valid starting point for a NEW
                # agent, even though agents already built from it keep working
                # -- they carry their own copy and never read back through here.
                AgentTemplate.is_active.is_(True),
            )
        )
        if template is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Agent template {body.template_id} not found.",
            )

    agent = Agent(
        owner_user_id=user.id,
        # Provenance only. Read the module docstring before this line reads like
        # a reference the config should be looked up through.
        template_id=body.template_id,
        name=body.name,
        description=body.description,
        # Both are the model's default anyway; stated because they are the two
        # values that describe a brand-new agent to the UI. "empty" is PRD 4.2's
        # first state, and only "private" is honoured today.
        status="empty",
        visibility="private",
    )

    if template is not None:
        # THE COPY, and the whole reason `agents` duplicates these columns.
        #
        # After this loop the template is out of the picture forever: the agent
        # holds its own values, an admin editing the preset tomorrow changes
        # nothing about this agent, and every eval run recorded against it stays
        # valid because the configuration that produced those numbers is still
        # sitting on the row. Reading through `template_id` instead would make a
        # template edit a silent, untraceable re-tune of every agent built from
        # it (PRD 4.2).
        #
        # `system_prompt` is included: it is a tuned parameter here, not
        # metadata, and it is the control that actually produces refusals.
        for field in TEMPLATE_PARAMETERS:
            setattr(agent, field, getattr(template, field))
    # No else. From scratch means the model defaults, and `system_prompt` stays
    # NULL -- which is safe rather than ungrounded: `pipeline.answer_question`
    # resolves `agent.system_prompt or DEFAULT_SYSTEM_PROMPT`, so NULL means
    # "use the default rules", never "no rules".

    db.add(agent)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        # `body.name`, not `agent.name`: a rollback expires every attribute on
        # every instance in the session, so reading one back here would fire a
        # lazy refresh -- which on an async session raises MissingGreenlet from
        # inside the exception handler and buries the real error.
        _conflict_or_reraise(exc, body.name)

    _audit(
        db,
        user,
        "agent.create",
        agent.id,
        {
            "name": agent.name,
            # Both recorded: the id is the link, the slug is what a human reading
            # the log a year later can actually interpret.
            "template_id": str(template.id) if template else None,
            "template_slug": template.slug if template else None,
        },
    )
    await db.commit()

    # `created_at` is a server default, so its value comes from Postgres rather
    # than from Python. SQLAlchemy usually fetches it via RETURNING on the
    # INSERT, but when it does not, the attribute is left pending a lazy load --
    # and a lazy load on an async session raises MissingGreenlet at the moment
    # the serialiser touches it, which reads as a driver fault rather than a
    # missing refresh. One SELECT on the create path settles it.
    await db.refresh(agent)

    # 0 by construction, not by query: documents cannot exist for an agent that
    # was created a millisecond ago.
    return AgentOut.from_agent(agent, 0)


@router.get("/agents/{agent_id}")
async def get_agent(agent: OwnedAgent, db: DbSession) -> AgentOut:
    """One agent. `OwnedAgent` has already answered 404-or-403."""
    return AgentOut.from_agent(agent, await _document_count(db, agent.id))


@router.patch("/agents/{agent_id}")
async def update_agent(
    body: AgentUpdate, agent: OwnedAgent, user: CurrentUser, db: DbSession
) -> AgentOut:
    """Edit an agent's name, description or tuning parameters.

    `exclude_unset=True` is what makes this a PATCH rather than a PUT: a field
    the client did not send is untouched, which is not the same as a field sent
    as null. `system_prompt: null` legitimately clears the prompt back to the
    pipeline default, and only the sent/unsent distinction can tell those apart.

    Changing chunking here does NOT re-chunk what is already indexed. Existing
    vectors keep the size they were built with and the new value applies to the
    next upload, so an agent can hold two chunkings at once. That is a property
    of the design (PRD 3.3), not a bug -- but it is why the UI should say
    "applies to new uploads" next to these fields.

    `user` is taken alongside `agent` purely for the audit trail on a rename.
    FastAPI caches dependencies per request, so `current_user` resolves once and
    both `OwnedAgent` and this parameter share it. It is not read off
    `agent.owner_user_id`, which is the same person today only because sharing
    does not exist yet -- when it does, the audit must name who acted, not who
    owns.
    """
    fields = body.model_dump(exclude_unset=True)

    # THE REFUSAL. The embedding model is part of the index, not part of the
    # query code (PRD 7): the vectors under this agent's namespace were produced
    # by one model, and a query embedded with another lands in a different vector
    # space. Matching dimensions do not make it the same space, so nothing
    # raises -- the retriever returns confident nonsense and the trace looks
    # normal. A config PATCH is exactly the shape of request that would let that
    # through unnoticed, which is why the refusal is explicit and carries the
    # remedy rather than being a quietly-ignored field.
    #
    # Refused on CHANGE, not on presence. `AgentOut` exposes `embedding_model`,
    # so a client that reads an agent, edits one field and sends the object back
    # will include it; failing that round trip would be a 400 for a request that
    # changes nothing.
    if "embedding_model" in fields and fields["embedding_model"] != agent.embedding_model:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "embedding_model cannot be changed on an existing agent. The "
                "vectors in this agent's namespace were built with "
                f"{agent.embedding_model or 'no model yet'}, and a query "
                "embedded with a different model searches a different vector "
                "space -- matching dimensions do not make it the same space, so "
                "retrieval would return plausible nonsense instead of failing. "
                "Delete the documents and re-ingest them to move this agent to "
                "another embedding model."
            ),
        )
    fields.pop("embedding_model", None)

    # Checked against the MERGED configuration, not against the body: sending
    # only `chunk_overlap` can break a pair whose other half is already on the
    # row. LangChain's splitter raises only when overlap exceeds size, and it
    # raises during the next INGEST -- so without this check the error surfaces
    # on somebody's upload, minutes or days after the request that caused it,
    # pointing at the file. Equality is rejected too: it is legal to LangChain
    # and produces chunks that are entirely overlap.
    chunk_size = fields.get("chunk_size", agent.chunk_size)
    chunk_overlap = fields.get("chunk_overlap", agent.chunk_overlap)
    if chunk_overlap >= chunk_size:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"chunk_overlap ({chunk_overlap}) must be smaller than "
                f"chunk_size ({chunk_size})."
            ),
        )

    for key, value in fields.items():
        setattr(agent, key, value)

    # Captured before the write for the same reason as in `create_agent`: if the
    # commit raises, the rollback expires the instance and reading `agent.name`
    # to build the error message would trigger an async lazy load.
    name = fields.get("name", agent.name)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        # A rename can collide with another of this owner's agents, exactly as a
        # create can.
        _conflict_or_reraise(exc, name)

    return AgentOut.from_agent(agent, await _document_count(db, agent.id))


@router.delete("/agents/{agent_id}")
async def delete_agent(
    agent: OwnedAgent, user: CurrentUser, db: DbSession
) -> dict[str, bool]:
    """Delete an agent, its corpus and its vectors.

    VECTORS FIRST, ROWS SECOND -- the same ordering, and the same argument, as
    `rag/delete.py`. A crash between the two steps leaves either orphaned rows
    (vectors gone, `agents` row still there: visible in the UI, fixed by pressing
    delete again) or orphaned vectors (rows gone, vectors still in the
    namespace: nothing left to enumerate them from, permanent, and still
    matching every query issued against that namespace). Only one of those is
    recoverable, so the recoverable one is what a failure is allowed to leave
    behind. If Pinecone is unreachable this route 500s and deletes nothing,
    which is the correct outcome.
    """
    # Read before the rows go -- afterwards there is nothing to count, and the
    # audit entry is the only surviving record of how large the corpus was.
    document_count = await _document_count(db, agent.id)
    # Plain values, captured now. Below, the row is deleted out from under the
    # instance, and its attributes must not be read after that.
    agent_id = agent.id
    audit_payload = {
        "name": agent.name,
        "document_count": document_count,
        # The namespace is derived, so once the id is gone there is no way to
        # reconstruct which Pinecone namespace this agent owned.
        "namespace": agent.namespace,
    }

    await delete_agent_namespace(agent)

    _audit(db, user, "agent.delete", agent_id, audit_payload)

    # Core DELETE, not `await db.delete(agent)`. The ORM path cascades along
    # `Agent.documents`, and that relationship declares no delete cascade, so
    # SQLAlchemy's default is to de-associate the children by setting
    # `documents.agent_id` to NULL -- a NOT NULL column. It would also have to
    # LOAD that collection first, and a lazy load on an async session raises
    # MissingGreenlet. The foreign keys already carry ON DELETE CASCADE all the
    # way down to `chunks`; going through Core lets Postgres perform the cascade
    # it was built for, in one statement.
    await db.execute(sa_delete(Agent).where(Agent.id == agent_id))
    await db.commit()

    return {"ok": True}
