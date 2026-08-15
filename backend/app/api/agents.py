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
numbers moved. The persona columns are copied for the same reason and are not an
exception to it: what an agent is *called* has to stay as fixed as how it
behaves, or a card and the traces under it start describing different things.
`template_id` survives creation for provenance only: it answers "where did this
start", never "what is this now".

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
# it copied here and missing from the editor, or the reverse.
#
# The last four are the PERSONA -- role, pedagogy, icon, category -- and they are
# copied for exactly the PRD 4.2 reason the tuning numbers are, not as a
# convenience. A persona is what an agent is: somebody picked "Socratic Tutor",
# built a corpus for it and recorded eval runs against it. Read back through
# `template_id` instead, and an admin renaming or recategorising the preset
# tomorrow silently re-labels every agent already built from it, so the card and
# the traces underneath it would start describing different things. Note that
# `system_prompt` -- the persona's actual behaviour -- was copied from the start,
# which is why the gap this closes was a labelling one: without these four an
# agent behaved like a Socratic tutor and displayed as nothing in particular.
#
# What is still NOT in it: `slug`, `name`, `description` and `is_active` describe
# the template as an entry in the picker rather than the agent (which carries its
# own user-chosen name and description), and `id` is the provenance link that
# stays in `template_id`.
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
    "persona_role",
    "pedagogy",
    "icon",
    "category",
)

# Postgres' SQLSTATE for a unique violation. Used to make sure a 409 is only
# ever claimed for a genuine duplicate -- see `_conflict_or_reraise`.
_UNIQUE_VIOLATION = "23505"

# --- Template display order -------------------------------------------------
#
# Ordered deliberately, because nothing on the row supplies a usable order on its
# own: `created_at` cannot (the templates are seeded by migration, in one
# transaction, on one timestamp) and alphabetical order would put "From scratch"
# at the top of the picker -- the worst possible first option for a user who came
# here to be handed a starting point. Three keys, applied in this order.
#
# 1. THE BLANK CANVAS SORTS LAST, whatever category it carries. `from-scratch` is
#    categorised `general`, so grouping alone would leave it third of eight,
#    reading as just another preset rather than as the fallback for somebody who
#    has rejected all of them.
# 2. THEN BY CATEGORY. Eight entries is where a flat list stops being scannable,
#    and keying the grouping on the column rather than on a hand-maintained list
#    means a template added later lands with its peers instead of at the end.
#    NULL and unrecognised categories fall through to the ELSE and group last --
#    the same "ungrouped" degradation the frontend badge makes, and the reason
#    `AgentTemplate.category` is a plain String rather than an enum.
# 3. THEN BY THE SEED'S DECLARATION ORDER as the tiebreak inside a group, and
#    finally by name for anything this repo did not seed.
#
# Keys 1 and 2 reproduce `seed.ALL_TEMPLATES` order exactly as it stands today.
# That is not redundancy: the declaration list is what the seed used, and the
# category rank is what keeps the picker sensible when the next persona is added
# to the middle of it.
_CATEGORY_ORDER: tuple[str, ...] = (
    "general",
    "explain",
    "practice",
    "assess",
    "reflect",
)

# `seed.py` owns this slug and keeps its own copy private. Duplicated rather than
# imported because the two uses are independent: there it splices the seed list,
# here it pins one row to the bottom of a SQL sort.
_BLANK_CANVAS_SLUG = "from-scratch"

_BLANK_CANVAS_LAST = case(
    {_BLANK_CANVAS_SLUG: 1}, value=AgentTemplate.slug, else_=0
)

_CATEGORY_ORDER_RANK = case(
    {name: index for index, name in enumerate(_CATEGORY_ORDER)},
    value=AgentTemplate.category,
    else_=len(_CATEGORY_ORDER),
)

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

    **`system_prompt` used to be deliberately absent**, on the grounds that it is
    long, that it is the load-bearing safety control (see `app/db/seed.py`), and
    that the picker offered a set of retrieval numbers rather than a prompt --
    the copy landed on the agent, where `AgentOut.system_prompt` exposed it for
    editing against the agent that would actually use it.

    That reasoning is inverted by personas, and the reversal is the point rather
    than a relaxation. What a user now picks between is a Socratic tutor and a
    quiz writer, and the entire difference between those two IS the prompt: the
    retrieval parameters barely move. Withholding it would mean asking somebody
    to choose a teaching method from a one-line description while the thing they
    are actually choosing stayed hidden. It is still the safety control, and
    showing it is what lets a tutor read the refusal rules before trusting the
    agent with a class.

    The other four are presentation only (`app/db/models.py` says so at the
    column): they let a card show what the prompt does without making the reader
    parse it. All nullable -- `pedagogy` is null on the three original templates,
    which are retrieval configurations rather than teaching methods.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    slug: str
    name: str
    description: str | None = None
    persona_role: str | None = None
    pedagogy: str | None = None
    icon: str | None = None
    category: str | None = None
    chunk_size: int
    chunk_overlap: int
    splitter: str
    retrieve_k: int
    rerank_enabled: bool
    rerank_top_n: int
    score_threshold: float
    max_rewrites: int
    system_prompt: str | None = None


class AgentOut(BaseModel):
    """One agent and its effective configuration.

    Every field here is the agent's own column, never read through `template_id`
    -- the persona labels included. What the UI shows is what the next ingest and
    the next query will actually use, and what the card says is what this agent
    was built as, not what the preset it started from has since been renamed to.

    All four persona fields are nullable and the frontend has to treat them that
    way: every agent created before the columns existed has them null, and
    `pedagogy` is null even on a fresh agent built from one of the three original
    templates.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None = None
    persona_role: str | None = None
    pedagogy: str | None = None
    icon: str | None = None
    category: str | None = None
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


class AgentTunables(BaseModel):
    """The per-agent knobs, defined once so create and update cannot drift apart.

    Every field is optional and defaults to None, which in both subclasses means
    "not supplied" rather than "set to null" -- `AgentUpdate` reads that through
    `exclude_unset`, `AgentCreate` through `exclude_none`, and each route says
    why it chose the one it did.

    The bounds live here, on the definition, rather than at the two call sites.
    That is the point of the base class: a knob that a PATCH refuses and a POST
    accepts is not a bound, it is a bound with a way around it, and two copies of
    these `Field(...)` declarations is exactly how that happens. The comments
    travel with the fields for the same reason -- each one records the
    measurement or the platform limit that chose the number, and a bound whose
    reason has been left behind in another class is a bound the next person
    rounds off.
    """

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


# Derived from the base class, never typed out. `create_agent` uses this to
# decide which keys of the request body may be written onto the agent row, and a
# hand-maintained tuple would be one edit away from sweeping `name`,
# `description` or `template_id` into that loop -- `template_id` especially,
# which is a real column and would therefore be written without complaint.
# Reading the field names off `AgentTunables` makes that structurally impossible:
# the three fields that must not be overridable are declared on the SUBCLASSES,
# so they are not in this set and cannot be added to it by accident.
_TUNABLE_FIELDS: frozenset[str] = frozenset(AgentTunables.model_fields)


class AgentCreate(AgentTunables):
    """Body for POST /api/agents. Name, optional template, optional tunables.

    **The tunables used to be deliberately absent**, on the grounds that
    parameters came from the template (or from the model defaults) and never
    from the request, which is what made "create from a template" and "create
    your own" one code path rather than two.

    That property is preserved rather than abandoned, and preserving it is the
    reason this reads as an extra step instead of a second path. The template
    copy still establishes the WHOLE configuration; anything sent explicitly is
    then applied on top as a uniform override that runs identically whether a
    template was chosen or not. Defaults, then template, then explicit request --
    one direction, no branching, and an agent created from a template with no
    overrides comes out byte-identical to what it was before.

    **Why it changed: the frontend now sets these at creation time, in a
    wizard.** The alternative it replaces -- create, then immediately PATCH -- is
    not atomic, and the failure is asymmetric in the worst way. The POST
    succeeds, the PATCH is rejected for a bound or a chunk pair, and what is left
    behind is an agent that EXISTS carrying parameters its owner never chose. The
    only screen in the app that could fix that is the tuning editor on an agent
    the user was in the middle of deciding they wanted. Rejecting the whole
    request is the only outcome that leaves nothing to clean up.

    Unknown keys are still ignored rather than rejected -- a client that posts
    `rerank_topn` here gets a working agent carrying the template's value and can
    PATCH it a moment later, which is a far smaller harm than a failed creation.
    `AgentUpdate` takes the opposite line, and the asymmetry is explained there.
    """

    name: AgentName
    description: str | None = None
    # Omitted means "from scratch": model defaults, and `template_id` stays
    # null. There is a `from-scratch` TEMPLATE too, and picking it is a
    # different, equally valid route to nearly the same agent -- it supplies the
    # same values plus a minimal grounding prompt, and records the provenance.
    template_id: uuid.UUID | None = None


class AgentUpdate(AgentTunables):
    """Partial config update. Only the fields actually SENT are applied.

    `extra="forbid"`, unlike `AgentCreate`. The failure modes are not symmetric:
    an ignored extra field on create costs a PATCH, while an ignored extra field
    on update is a tuning UI that lies -- the user types `rerank_topn: 5`, gets a
    200, and then debugs an agent that is still reranking to 3 with nothing
    anywhere to say the value never landed. A 422 naming the unknown field is the
    only outcome that cannot mislead.

    The bounds are cheap insurance against configurations that fail somewhere
    else, later, in a message that points nowhere near this request. They are
    inherited rather than declared here, so the create wizard cannot set a value
    this editor would refuse.
    """

    model_config = ConfigDict(extra="forbid")

    name: AgentName | None = None
    description: str | None = None

    # Declared ONLY so it can be refused with an explanation rather than with a
    # generic "extra fields not permitted". See `update_agent`.
    embedding_model: str | None = None


# Which patchable fields map to a NOT NULL column, read off the table rather than
# listed here so a future migration cannot leave this set behind.
#
# Every optional field above defaults to None to mean "not sent", which makes an
# EXPLICIT null indistinguishable from the default at the type level -- Pydantic
# validates both identically, and only `exclude_unset` can tell them apart. So
# the refusal has to live in the handler, and without it `{"chunk_size": null}`
# reached a NOT NULL column and came back a 500: an unhandled server error for a
# malformed request, which is the wrong half of the 4xx/5xx split and tells the
# caller nothing about what to send instead.
#
# `system_prompt` and `description` are deliberately absent: their columns really
# are nullable, and clearing a prompt back to the pipeline default is a request
# somebody legitimately makes.
_NOT_NULL_FIELDS: frozenset[str] = frozenset(
    name
    for name in AgentUpdate.model_fields
    if name in Agent.__table__.c and not Agent.__table__.c[name].nullable
)


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


def _pending_value(agent: Agent, field: str) -> Any:
    """What `field` will actually hold on the row, read before the INSERT runs.

    SQLAlchemy applies a mapped column's `default=` at FLUSH time, not at
    construction, so on a freshly-built from-scratch agent `chunk_size` is still
    None in Python while the row it produces will hold 800. Any check that has to
    run before `db.add` therefore cannot just read the attribute.

    That is not a tidiness point. `_reject_overlapping_chunks` exists precisely
    for the half-specified pair -- a request that sends `chunk_overlap` and lets
    the other half come from somewhere else -- and reading the attribute alone
    would see None, skip the comparison, and let exactly that case through to
    fail at ingest instead. The column default IS the other half when nothing
    else supplied one, so it has to be consulted like any other source.
    """
    value = getattr(agent, field)
    if value is not None:
        return value
    return Agent.__table__.c[field].default.arg


def _reject_overlapping_chunks(chunk_size: int, chunk_overlap: int) -> None:
    """422 unless overlap is strictly smaller than size. Create and update share it.

    Callers pass the MERGED configuration, never the request body. On a PATCH,
    sending only `chunk_overlap` can break a pair whose other half is already on
    the row; on a POST it can break a pair whose other half came from the
    template or from the column default. LangChain's splitter raises only when
    overlap exceeds size, and it raises during the next INGEST -- so without this
    the error surfaces on somebody's upload, minutes or days after the request
    that caused it, pointing at the file. Equality is rejected too: it is legal
    to LangChain and produces chunks that are entirely overlap.

    One helper rather than a copy in each route, because the two now accept the
    same fields. A pair that PATCH refuses and POST accepts is not a validation
    rule, it is a validation rule with a documented way around it.
    """
    if chunk_overlap >= chunk_size:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"chunk_overlap ({chunk_overlap}) must be smaller than "
                f"chunk_size ({chunk_size})."
            ),
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
    """The presets available when creating an agent, grouped and ordered.

    **The order is part of the response, not a detail of it.** The picker renders
    this list verbatim -- it does not sort or group client-side -- so blank
    canvas last and personas beside their own kind are decided here and nowhere
    else. The three sort keys and why each exists are above
    `_CATEGORY_ORDER`.

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
        .order_by(
            _BLANK_CANVAS_LAST,
            _CATEGORY_ORDER_RANK,
            _TEMPLATE_ORDER,
            AgentTemplate.name,
        )
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

    Configuration is resolved in ONE direction and in three layers: model
    defaults, then the template copy over the top of them, then whatever the
    request sent explicitly over the top of that. There is no branch in it -- the
    override step below runs the same way whether a template was chosen or not,
    which is what keeps "create from a template" and "create your own" one code
    path even now that the request can carry tunables. See `AgentCreate` for why
    it can.

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
        # metadata, and it is the control that actually produces refusals. So are
        # the four persona columns, which travel with it -- an agent that behaves
        # like a Socratic tutor and cannot say so is half-copied.
        for field in TEMPLATE_PARAMETERS:
            setattr(agent, field, getattr(template, field))
    # No else. From scratch means the model defaults, `system_prompt` stays NULL
    # -- which is safe rather than ungrounded: `pipeline.answer_question`
    # resolves `agent.system_prompt or DEFAULT_SYSTEM_PROMPT`, so NULL means "use
    # the default rules", never "no rules" -- and the persona columns stay NULL
    # too, which every consumer already handles because agents created before
    # those columns existed have them.

    # LAYER THREE: the request itself, applied over whatever the two layers
    # above left behind. Precedence is model defaults, then template, then
    # explicit request -- one direction, and this loop is the only place the
    # third layer is written, so a value the client sent always wins and a value
    # it did not send is never touched.
    #
    # `exclude_none`, NOT `exclude_unset`, and the difference is load-bearing in
    # a way it is not on PATCH. Both agree about a field the client omitted --
    # neither emits it, so neither can clobber a template value with None -- and
    # they differ only on an explicit `"chunk_size": null`. `AgentUpdate` needs
    # that distinction, because `system_prompt: null` legitimately CLEARS a
    # prompt back to the pipeline default. Here there is nothing yet to clear:
    # from-scratch already leaves the prompt NULL, and a template's prompt is the
    # substance of what was picked rather than something to be nulled in the same
    # breath. What `exclude_unset` would buy instead is a 500 -- a wizard that
    # serialises its untouched fields as null would send `chunk_size: null` at a
    # NOT NULL column, and the resulting IntegrityError carries SQLSTATE 23502,
    # which `_conflict_or_reraise` correctly declines to call a name conflict and
    # re-raises. So the user gets an unhandled 500 for having left a field alone.
    #
    # Restricted to `_TUNABLE_FIELDS` so `name`, `description` and `template_id`
    # cannot be re-applied here; see the comment on that set.
    overrides = {
        field: value
        for field, value in body.model_dump(exclude_none=True).items()
        if field in _TUNABLE_FIELDS
    }
    for field, value in overrides.items():
        setattr(agent, field, value)

    # BEFORE `db.add`, deliberately. The instance is still detached, so a 422
    # here leaves the session exactly as it found it -- nothing pending, nothing
    # to roll back, and no half-built agent for the next flush in this request to
    # trip over. Reading through `_pending_value` because the column defaults
    # have not been applied yet at this point.
    _reject_overlapping_chunks(
        _pending_value(agent, "chunk_size"),
        _pending_value(agent, "chunk_overlap"),
    )

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
            # WHICH knobs the request overrode, sorted so two identical requests
            # produce identical log rows. Without this the entry names a template
            # and nothing else, so a reader concludes the agent carries that
            # template's configuration -- and since `system_prompt` is overridable
            # too, an agent whose four persona columns all say "Socratic Tutor"
            # could be running a prompt the log never mentioned. Field NAMES
            # only: the values are on the agent, and copying them here would put
            # a whole system prompt in an audit row and let the two disagree
            # after the next PATCH.
            "overrides": sorted(overrides),
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

    # An explicit null at a NOT NULL column, refused as a 422 here rather than
    # left to arrive as an IntegrityError that `_conflict_or_reraise` correctly
    # declines to call a name conflict and re-raises -- which surfaced as a 500,
    # the wrong half of the 4xx/5xx split for a malformed request, carrying
    # nothing the caller could act on. Fields are named so the caller knows which
    # key to drop.
    nulled = sorted(
        field for field, value in fields.items() if value is None and field in _NOT_NULL_FIELDS
    )
    if nulled:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"These fields cannot be set to null: {', '.join(nulled)}. "
                "Omit a field to leave it unchanged."
            ),
        )

    # The MERGED configuration -- the sent value where there is one, the row's
    # own where there is not -- because sending only `chunk_overlap` can break a
    # pair whose other half is already on the row. The reasoning, and why
    # equality is rejected as well, is on `_reject_overlapping_chunks`; it is
    # shared with `create_agent` rather than duplicated. Unlike there, the column
    # defaults have long since been applied, so the attributes can be read
    # directly.
    #
    # Tested against None explicitly, not with `or` and not with `.get`'s default.
    # `.get(key, agent.x)` returns the None that `exclude_unset` keeps for an
    # EXPLICIT `{"chunk_size": null}`, and `None >= int` is a TypeError -- a 500
    # on a request that deserved a 422. `or` fixes that and breaks something
    # else: `chunk_overlap` is `ge=0`, and a perfectly legal `{"chunk_overlap":
    # 0}` is falsy, so it would silently compare the row's OLD overlap instead of
    # the zero just sent.
    #
    # The null is left in `fields` deliberately. The setattr loop below writes
    # it, the NOT NULL column rejects it, and that is the error the request has
    # earned rather than one this line invented on its behalf.
    sent_size = fields.get("chunk_size")
    sent_overlap = fields.get("chunk_overlap")
    _reject_overlapping_chunks(
        agent.chunk_size if sent_size is None else sent_size,
        agent.chunk_overlap if sent_overlap is None else sent_overlap,
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
