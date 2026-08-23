"""The admin console's read API. Every route crosses the tenancy boundary.

**READ THIS BEFORE ADDING A ROUTE HERE.**

Every other route in this application is nested under an agent
(`/api/agents/{agent_id}/...`) and resolves through `owned_agent`, and CLAUDE.md
states why in one sentence: *"no request can be expressed without naming an
agent"*. Tenancy is structural -- there is no way to write a request that asks
for someone else's data, so there is no scoping rule that can be forgotten.

These routes invert that on purpose. Crossing the boundary IS the feature, so
the structural guarantee is gone and `AdminUser` is doing the entire job alone.
That puts this module in the same category CLAUDE.md already assigns to
`/api/conversations/{id}`, `/api/golden-questions/{id}` and
`/api/eval-runs/{id}` -- *"the highest-risk lines in the codebase"* -- with one
difference that makes it worse: those three check ownership by hand and fail
closed for the wrong user, whereas these deliberately return everyone's data to
the right one. **A route added here without `AdminUser` is a full data leak, not
a scoping bug.** `scripts/admin_check.py` asserts every route on this router
carries the dependency, by introspection rather than by review, because a review
is a thing a person does once.

Two further rules, both load-bearing rather than stylistic:

* **Reading a transcript writes an `audit_log` row.** Aggregates do not. A row
  per dashboard render would bury the reads that actually exposed a person's
  text under thousands that exposed nothing.
* **Every aggregate reports its own denominator.** `76` of this database's
  queries predate metering and can never be backfilled -- the OpenRouter
  generation ids were never stored, and `GET /api/v1/generation?id=` needs one.
  A total that silently treats them as zero understates spend and makes the
  first week of real data look like a spike. EVAL.md documents the identical
  trap for `scored_count`, where a metric's mean had its own denominator and the
  scorecard's footnote did not. Every response here carries `measured` and
  `total` side by side.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AdminUser, DbSession
from app.config import settings
from app.db.models import (
    Agent,
    ApiUsage,
    AuditLog,
    Conversation,
    Document,
    EvalRun,
    Handout,
    Query as QueryRow,
    User,
)

router = APIRouter(prefix="/api/admin", tags=["admin"])

# Imported by anything that needs to find these rows, never retyped -- the rule
# `app.rag.ingest.INGEST_FAILURE_ACTION` already sets in this codebase.
ADMIN_READ_ACTION = "app.api.admin.ADMIN_READ"


# --------------------------------------------------------------------------
# Shapes
# --------------------------------------------------------------------------

class Measured(BaseModel):
    """A number, and how much of the population it actually rests on.

    Never collapse this to a bare float. The whole point is that `cost` of
    `0.0` over `measured=0` and `cost` of `0.0` over `measured=400` are
    completely different facts and render identically once the denominator is
    dropped.
    """

    value: float | None = None
    measured: int = 0
    total: int = 0


class SpendOut(BaseModel):
    cost_usd: float
    prompt_tokens: int
    completion_tokens: int
    calls: int
    # Calls whose cost the provider reported, vs calls made. The gap is
    # embeddings and rerank, whose cost this system estimates rather than reads.
    priced_calls: int


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    name: str | None
    role: str
    is_active: bool
    created_at: datetime
    last_login_at: datetime | None
    # `dev|<email>` marks the dev-login shim. Surfaced because this database has
    # two rows for two different real people for exactly that reason, and an
    # admin looking at "15 users" should be able to see that it is not 15 people.
    is_dev_identity: bool
    agents: int
    conversations: int
    queries: int
    spend: SpendOut


class AgentOut(BaseModel):
    id: uuid.UUID
    name: str
    owner_email: str
    owner_user_id: uuid.UUID
    created_at: datetime
    documents: int
    conversations: int
    queries: int
    eval_runs: int
    handouts: int
    spend: SpendOut


class ConversationOut(BaseModel):
    id: uuid.UUID
    title: str | None
    created_at: datetime
    updated_at: datetime
    user_email: str
    agent_name: str
    agent_id: uuid.UUID
    turns: int
    refusals: int
    spend: SpendOut


class TurnOut(BaseModel):
    id: uuid.UUID
    created_at: datetime
    question: str
    answer: str | None
    model_used: str | None
    latency_ms: int | None
    refused: bool
    prompt_tokens: int | None
    completion_tokens: int | None
    cost_usd: float | None
    # None means this turn predates metering. Zero would mean it was free.
    measured: bool


class TranscriptOut(BaseModel):
    id: uuid.UUID
    title: str | None
    user_email: str
    agent_name: str
    created_at: datetime
    turns: list[TurnOut]
    spend: SpendOut


class OverviewOut(BaseModel):
    users: int
    dev_identities: int
    agents: int
    documents: int
    conversations: int
    queries: int
    eval_runs: int
    handouts: int
    refusal_rate: Measured
    spend: SpendOut
    # queries with at least one api_usage row, over queries total. This is the
    # single most important number on the page: it says how much of the history
    # the spend figures actually cover.
    coverage: Measured
    since: datetime | None


# --------------------------------------------------------------------------
# Spend, as a reusable subquery
# --------------------------------------------------------------------------

def _spend_columns():
    """The four aggregates, spelled once.

    `cost_usd` only -- `estimated_cost` is deliberately NOT summed in with it.
    Adding a reported number to a guessed one produces a total that is neither,
    and the column split exists precisely so the console can show which is
    which.
    """
    return (
        func.coalesce(func.sum(ApiUsage.cost_usd), 0.0).label("cost_usd"),
        func.coalesce(func.sum(ApiUsage.prompt_tokens), 0).label("prompt_tokens"),
        func.coalesce(func.sum(ApiUsage.completion_tokens), 0).label(
            "completion_tokens"
        ),
        func.count(ApiUsage.id).label("calls"),
        func.count(ApiUsage.cost_usd).label("priced_calls"),
    )


def _spend(row: Any) -> SpendOut:
    return SpendOut(
        cost_usd=float(row.cost_usd or 0.0),
        prompt_tokens=int(row.prompt_tokens or 0),
        completion_tokens=int(row.completion_tokens or 0),
        calls=int(row.calls or 0),
        priced_calls=int(row.priced_calls or 0),
    )


_EMPTY_SPEND = SpendOut(
    cost_usd=0.0, prompt_tokens=0, completion_tokens=0, calls=0, priced_calls=0
)


async def _audit(
    db: AsyncSession,
    admin: User,
    *,
    resource_type: str,
    resource_id: str,
    metadata: dict | None = None,
) -> None:
    """Record that an administrator read someone's content.

    Not committed here -- the caller's request-scoped session commits. A read
    that fails to render should not leave an audit row claiming it happened.
    """
    db.add(
        AuditLog(
            user_id=admin.id,
            action=ADMIN_READ_ACTION,
            resource_type=resource_type,
            resource_id=resource_id,
            audit_metadata=metadata or {},
        )
    )
    await db.commit()


# --------------------------------------------------------------------------
# GET /api/admin/overview
# --------------------------------------------------------------------------

@router.get("/overview", response_model=OverviewOut)
async def overview(admin: AdminUser, db: DbSession) -> OverviewOut:
    """Everything, counted once. The console's landing numbers."""
    async def scalar(stmt) -> int:
        return int((await db.execute(stmt)).scalar() or 0)

    users = await scalar(select(func.count(User.id)))
    dev = await scalar(
        select(func.count(User.id)).where(User.google_sub.like("dev|%"))
    )
    queries = await scalar(select(func.count(QueryRow.id)))
    refused = await scalar(
        select(func.count(QueryRow.id)).where(QueryRow.refused.is_(True))
    )
    # Queries that have at least one metered call. NOT `prompt_tokens IS NOT
    # NULL` -- that is the denormalised cache, and reading coverage off a cache
    # would report the cache's health rather than the meter's.
    covered = await scalar(
        select(func.count(func.distinct(ApiUsage.query_id))).where(
            ApiUsage.query_id.is_not(None)
        )
    )
    spend_row = (await db.execute(select(*_spend_columns()))).one()
    since = (await db.execute(select(func.min(ApiUsage.created_at)))).scalar()

    return OverviewOut(
        users=users,
        dev_identities=dev,
        agents=await scalar(select(func.count(Agent.id))),
        documents=await scalar(select(func.count(Document.id))),
        conversations=await scalar(select(func.count(Conversation.id))),
        queries=queries,
        eval_runs=await scalar(select(func.count(EvalRun.id))),
        handouts=await scalar(select(func.count(Handout.id))),
        refusal_rate=Measured(
            value=(refused / queries) if queries else None,
            measured=queries,
            total=queries,
        ),
        spend=_spend(spend_row),
        coverage=Measured(
            value=(covered / queries) if queries else None,
            measured=covered,
            total=queries,
        ),
        since=since,
    )


# --------------------------------------------------------------------------
# GET /api/admin/users
# --------------------------------------------------------------------------

@router.get("/users", response_model=list[UserOut])
async def list_users(admin: AdminUser, db: DbSession) -> list[UserOut]:
    """Every user, with what they own and what they spent.

    Correlated scalar subqueries rather than joins, deliberately: joining
    `agents`, `conversations`, `queries` and `api_usage` onto one row multiplies
    them together and every count comes back inflated by the others' cardinality.
    That bug renders as a plausible number, which is the kind this codebase has
    been caught by before.
    """
    agents = (
        select(func.count(Agent.id))
        .where(Agent.owner_user_id == User.id)
        .scalar_subquery()
    )
    convs = (
        select(func.count(Conversation.id))
        .where(Conversation.user_id == User.id)
        .scalar_subquery()
    )
    queries = (
        select(func.count(QueryRow.id))
        .where(QueryRow.user_id == User.id)
        .scalar_subquery()
    )

    rows = (
        await db.execute(
            select(
                User,
                agents.label("agents"),
                convs.label("conversations"),
                queries.label("queries"),
            ).order_by(User.created_at)
        )
    ).all()

    spend_by_user = {
        row.user_id: _spend(row)
        for row in (
            await db.execute(
                select(ApiUsage.user_id, *_spend_columns()).group_by(ApiUsage.user_id)
            )
        ).all()
    }

    return [
        UserOut(
            id=user.id,
            email=user.email,
            name=user.name,
            role=user.role,
            is_active=user.is_active,
            created_at=user.created_at,
            last_login_at=user.last_login_at,
            is_dev_identity=user.google_sub.startswith("dev|"),
            agents=agent_count,
            conversations=conv_count,
            queries=query_count,
            spend=spend_by_user.get(user.id, _EMPTY_SPEND),
        )
        for user, agent_count, conv_count, query_count in rows
    ]


# --------------------------------------------------------------------------
# GET /api/admin/agents
# --------------------------------------------------------------------------

@router.get("/agents", response_model=list[AgentOut])
async def list_agents(admin: AdminUser, db: DbSession) -> list[AgentOut]:
    """Every agent across every owner."""
    def sub(model, column):
        return select(func.count(model.id)).where(column == Agent.id).scalar_subquery()

    rows = (
        await db.execute(
            select(
                Agent,
                User.email,
                sub(Document, Document.agent_id).label("documents"),
                sub(Conversation, Conversation.agent_id).label("conversations"),
                sub(QueryRow, QueryRow.agent_id).label("queries"),
                sub(EvalRun, EvalRun.agent_id).label("eval_runs"),
                sub(Handout, Handout.agent_id).label("handouts"),
            )
            .join(User, User.id == Agent.owner_user_id)
            .order_by(Agent.created_at)
        )
    ).all()

    spend_by_agent = {
        row.agent_id: _spend(row)
        for row in (
            await db.execute(
                select(ApiUsage.agent_id, *_spend_columns()).group_by(ApiUsage.agent_id)
            )
        ).all()
    }

    return [
        AgentOut(
            id=agent.id,
            name=agent.name,
            owner_email=email,
            owner_user_id=agent.owner_user_id,
            created_at=agent.created_at,
            documents=documents,
            conversations=conversations,
            queries=queries,
            eval_runs=eval_runs,
            handouts=handouts,
            spend=spend_by_agent.get(agent.id, _EMPTY_SPEND),
        )
        for agent, email, documents, conversations, queries, eval_runs, handouts in rows
    ]


# --------------------------------------------------------------------------
# GET /api/admin/conversations
# --------------------------------------------------------------------------

@router.get("/conversations", response_model=list[ConversationOut])
async def list_conversations(
    admin: AdminUser,
    db: DbSession,
    user_id: Annotated[uuid.UUID | None, Query()] = None,
    agent_id: Annotated[uuid.UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ConversationOut]:
    """Thread metadata only. The transcript is a separate, audited route."""
    turns = (
        select(func.count(QueryRow.id))
        .where(QueryRow.conversation_id == Conversation.id)
        .scalar_subquery()
    )
    refusals = (
        select(func.count(QueryRow.id))
        .where(
            QueryRow.conversation_id == Conversation.id,
            QueryRow.refused.is_(True),
        )
        .scalar_subquery()
    )

    stmt = (
        select(Conversation, User.email, Agent.name, turns.label("turns"),
               refusals.label("refusals"))
        .join(User, User.id == Conversation.user_id)
        .join(Agent, Agent.id == Conversation.agent_id)
        .order_by(Conversation.updated_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if user_id is not None:
        stmt = stmt.where(Conversation.user_id == user_id)
    if agent_id is not None:
        stmt = stmt.where(Conversation.agent_id == agent_id)

    rows = (await db.execute(stmt)).all()
    ids = [conversation.id for conversation, *_ in rows]

    spend_by_conversation: dict[uuid.UUID, SpendOut] = {}
    if ids:
        spend_by_conversation = {
            row.conversation_id: _spend(row)
            for row in (
                await db.execute(
                    select(QueryRow.conversation_id, *_spend_columns())
                    .join(ApiUsage, ApiUsage.query_id == QueryRow.id)
                    .where(QueryRow.conversation_id.in_(ids))
                    .group_by(QueryRow.conversation_id)
                )
            ).all()
        }

    return [
        ConversationOut(
            id=conversation.id,
            title=conversation.title,
            created_at=conversation.created_at,
            updated_at=conversation.updated_at,
            user_email=email,
            agent_name=agent_name,
            agent_id=conversation.agent_id,
            turns=turn_count,
            refusals=refusal_count,
            spend=spend_by_conversation.get(conversation.id, _EMPTY_SPEND),
        )
        for conversation, email, agent_name, turn_count, refusal_count in rows
    ]


# --------------------------------------------------------------------------
# GET /api/admin/conversations/{id}  -- THE AUDITED ONE
# --------------------------------------------------------------------------

@router.get("/conversations/{conversation_id}", response_model=TranscriptOut)
async def transcript(
    conversation_id: uuid.UUID, admin: AdminUser, db: DbSession
) -> TranscriptOut:
    """One thread in full -- another person's questions and the answers.

    **This is the route that actually exposes someone's content**, and it is the
    only one that writes an `audit_log` row. The audit happens whether or not the
    reader is the owner, because "the admin was looking at their own thread" is a
    conclusion to draw from the log, never a reason to skip writing it.
    """
    row = (
        await db.execute(
            select(Conversation, User.email, Agent.name)
            .join(User, User.id == Conversation.user_id)
            .join(Agent, Agent.id == Conversation.agent_id)
            .where(Conversation.id == conversation_id)
        )
    ).first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found"
        )
    conversation, email, agent_name = row

    cost = (
        select(func.sum(ApiUsage.cost_usd))
        .where(ApiUsage.query_id == QueryRow.id)
        .scalar_subquery()
    )
    measured = (
        select(func.count(ApiUsage.id))
        .where(ApiUsage.query_id == QueryRow.id)
        .scalar_subquery()
    )
    turn_rows = (
        await db.execute(
            select(QueryRow, cost.label("cost_usd"), measured.label("measured"))
            .where(QueryRow.conversation_id == conversation_id)
            .order_by(QueryRow.created_at)
        )
    ).all()

    spend_row = (
        await db.execute(
            select(*_spend_columns())
            .join(QueryRow, ApiUsage.query_id == QueryRow.id)
            .where(QueryRow.conversation_id == conversation_id)
        )
    ).one()

    await _audit(
        db,
        admin,
        resource_type="conversation",
        resource_id=str(conversation_id),
        metadata={
            "owner_user_id": str(conversation.user_id),
            "owner_email": email,
            "agent_id": str(conversation.agent_id),
            "turns": len(turn_rows),
            # True when an admin read their OWN thread. Kept so the log can be
            # filtered down to the reads that crossed a boundary.
            "self_read": conversation.user_id == admin.id,
        },
    )

    return TranscriptOut(
        id=conversation.id,
        title=conversation.title,
        user_email=email,
        agent_name=agent_name,
        created_at=conversation.created_at,
        turns=[
            TurnOut(
                id=query.id,
                created_at=query.created_at,
                question=query.question,
                answer=query.answer,
                model_used=query.model_used,
                latency_ms=query.latency_ms,
                refused=query.refused,
                prompt_tokens=query.prompt_tokens,
                completion_tokens=query.completion_tokens,
                cost_usd=float(cost_usd) if cost_usd is not None else None,
                measured=bool(measured_calls),
            )
            for query, cost_usd, measured_calls in turn_rows
        ],
        spend=_spend(spend_row),
    )


# --------------------------------------------------------------------------
# GET /api/admin/spend
# --------------------------------------------------------------------------

_GROUPS = {
    "user": User.email,
    "agent": Agent.name,
    "model": ApiUsage.model,
    "call_kind": ApiUsage.call_kind,
    "provider": ApiUsage.served_provider,
}


@router.get("/spend")
async def spend(
    admin: AdminUser,
    db: DbSession,
    group_by: Annotated[str, Query()] = "call_kind",
    days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> dict:
    """Spend, grouped. The one route that answers 'where is the money going'.

    `served_provider` is a legitimate grouping and it is the interesting one:
    two identical requests have been measured here at 2.002e-05 and 5.684e-06
    because OpenRouter routed them to different endpoints. Grouping by it turns
    an unexplainable cost swing into a visible one -- and CLAUDE.md records that
    pinning a provider to stop the swing is a NO_GO on this account, so seeing it
    is the available remedy.
    """
    if group_by not in _GROUPS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"group_by must be one of {sorted(_GROUPS)}",
        )

    since = datetime.now(timezone.utc) - timedelta(days=days)
    column = _GROUPS[group_by]

    stmt = select(column.label("key"), *_spend_columns()).where(
        ApiUsage.created_at >= since
    )
    if group_by == "user":
        stmt = stmt.join(User, User.id == ApiUsage.user_id)
    elif group_by == "agent":
        stmt = stmt.join(Agent, Agent.id == ApiUsage.agent_id)
    stmt = stmt.group_by(column).order_by(func.sum(ApiUsage.cost_usd).desc().nullslast())

    rows = (await db.execute(stmt)).all()

    # **`literal_column("'day'")`, not the plain string `"day"`.** Passing a
    # Python str makes SQLAlchemy render a BIND PARAMETER, and Postgres then
    # refuses the statement:
    #
    #     column "api_usage.created_at" must appear in the GROUP BY clause
    #
    # because `date_trunc($1, created_at)` in the SELECT and the same call in
    # GROUP BY are not provably the same expression once a parameter is
    # involved. Building the expression ONCE and reusing the object also means
    # the three clauses cannot drift apart.
    #
    # Found by opening the page. `scripts/admin_check.py` was entirely green
    # while this route returned 500 -- an offline harness cannot execute SQL, so
    # `--live` exists now and hits every route for exactly this class of fault.
    day = func.date_trunc(literal_column("'day'"), ApiUsage.created_at).label("day")
    daily = (
        await db.execute(
            select(
                day,
                func.coalesce(func.sum(ApiUsage.cost_usd), 0.0).label("cost_usd"),
                func.count(ApiUsage.id).label("calls"),
            )
            .where(ApiUsage.created_at >= since)
            .group_by(day)
            .order_by(day)
        )
    ).all()

    return {
        "group_by": group_by,
        "days": days,
        "groups": [
            {
                # NULL is a real answer here -- a call metered before
                # `served_provider` was recoverable, or an unattributed one.
                # Rendering it as "unknown" in SQL would hide that.
                "key": row.key,
                **_spend(row).model_dump(),
            }
            for row in rows
        ],
        "daily": [
            {
                "day": row.day.date().isoformat(),
                "cost_usd": float(row.cost_usd or 0.0),
                "calls": int(row.calls),
            }
            for row in daily
        ],
    }


# --------------------------------------------------------------------------
# GET /api/admin/eval-runs
# --------------------------------------------------------------------------

@router.get("/eval-runs")
async def eval_runs(admin: AdminUser, db: DbSession) -> list[dict]:
    """Every Ragas run, all agents.

    EVAL.md's warnings travel with these numbers and the console repeats the
    important one: a perfect context precision/recall on a tiny corpus is "not
    yet measured", not excellent retrieval.
    """
    rows = (
        await db.execute(
            select(EvalRun, Agent.name, User.email)
            .join(Agent, Agent.id == EvalRun.agent_id)
            .join(User, User.id == Agent.owner_user_id)
            .order_by(EvalRun.created_at.desc())
        )
    ).all()

    return [
        {
            "id": str(run.id),
            "agent_id": str(run.agent_id),
            "agent_name": agent_name,
            "owner_email": email,
            "created_at": run.created_at.isoformat(),
            "status": run.status,
            "judge_model": getattr(run, "judge_model", None),
            "generation_model": getattr(run, "generation_model", None),
            "scored_count": getattr(run, "scored_count", None),
            "error_count": getattr(run, "error_count", None),
            "summary": getattr(run, "summary", None),
        }
        for run, agent_name, email in rows
    ]


# --------------------------------------------------------------------------
# GET /api/admin/account
# --------------------------------------------------------------------------

@router.get("/account")
async def account(admin: AdminUser, db: DbSession) -> dict:
    """What OpenRouter says the account has spent, beside what we recorded.

    **This is the reconciliation, and it is the only external check this system
    has.** Everything else on the console is our own arithmetic over our own
    rows; if the meter silently stopped recording, every other number would fall
    quietly and consistently and look like a quiet week.

    The two figures do NOT have to match, and the console says so rather than
    hiding it: OpenRouter reports per KEY and per ACCOUNT, and this account's key
    also serves work that is not Groundwork -- measured 2026-08-20 at
    `total_usage: 66.61` account-wide against `usage_monthly: 0.83` on this key.
    What matters is that ours is not zero while theirs moves.

    Failures here are reported, never raised: the console must still render when
    OpenRouter is unreachable, because a page that 500s on a provider outage
    teaches its reader to ignore the page.
    """
    recorded = (
        await db.execute(
            select(
                func.coalesce(func.sum(ApiUsage.cost_usd), 0.0).label("cost_usd"),
                func.count(ApiUsage.id).label("calls"),
                func.min(ApiUsage.created_at).label("since"),
            )
        )
    ).one()

    remote: dict[str, Any] = {"ok": False, "error": None}
    headers = {"Authorization": f"Bearer {settings.openrouter_api_key}"}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            credits = await client.get(
                "https://openrouter.ai/api/v1/credits", headers=headers
            )
            key = await client.get(
                "https://openrouter.ai/api/v1/key", headers=headers
            )
        credits.raise_for_status()
        key.raise_for_status()
        credit_data = credits.json().get("data", {})
        key_data = key.json().get("data", {})
        remote = {
            "ok": True,
            "error": None,
            "total_credits": credit_data.get("total_credits"),
            "total_usage": credit_data.get("total_usage"),
            "key_usage": key_data.get("usage"),
            "key_usage_daily": key_data.get("usage_daily"),
            "key_usage_weekly": key_data.get("usage_weekly"),
            "key_usage_monthly": key_data.get("usage_monthly"),
            "key_limit_remaining": key_data.get("limit_remaining"),
        }
    except Exception as exc:  # noqa: BLE001 -- reported, never raised
        remote["error"] = f"{type(exc).__name__}: {exc}"

    return {
        "recorded": {
            "cost_usd": float(recorded.cost_usd or 0.0),
            "calls": int(recorded.calls or 0),
            "since": recorded.since.isoformat() if recorded.since else None,
        },
        "openrouter": remote,
        # Stated in the payload rather than left to the reader, because the
        # obvious reading of two different numbers is that one of them is broken.
        "note": (
            "OpenRouter reports per key and per account; this key also serves "
            "work outside Groundwork, so the totals are not expected to match. "
            "What matters is that the recorded figure moves when theirs does."
        ),
    }


# --------------------------------------------------------------------------
# GET /api/admin/audit
# --------------------------------------------------------------------------

@router.get("/audit")
async def audit_log(
    admin: AdminUser,
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[dict]:
    """Who read what. Includes this console's own transcript reads.

    An admin surface that logs everyone else's activity and not its own is a
    surveillance tool rather than an accountability one.
    """
    rows = (
        await db.execute(
            select(AuditLog, User.email)
            .outerjoin(User, User.id == AuditLog.user_id)
            .order_by(AuditLog.created_at.desc())
            .limit(limit)
        )
    ).all()
    return [
        {
            "id": str(entry.id),
            "created_at": entry.created_at.isoformat(),
            "actor_email": email,
            "action": entry.action,
            "resource_type": entry.resource_type,
            "resource_id": entry.resource_id,
            "metadata": entry.audit_metadata,
        }
        for entry, email in rows
    ]
