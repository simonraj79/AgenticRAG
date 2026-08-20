"""Turn buffered `UsageRecord`s into `api_usage` rows. The only writer.

Called ONCE per turn, from `app/api/ask.py:run_turn`, inside the transaction
that already exists -- never from the callback itself. `app/metering/context.py`
carries the reasoning: a callback must not borrow a session it did not open
(CLAUDE.md, Background jobs), and opening one per model call would mean up to six
connections per question on a Render starter plan running a single uvicorn
worker.

**`query_id` is stamped here rather than carried in the scope**, because
`queries.id` does not exist until the row is flushed and some calls -- the
history-aware rewrite in particular -- can happen either side of that. Every
record collected during one turn belongs to that turn's query by construction,
so filling the field at persist time is both simpler and more correct than
threading it through three subsystems.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ApiUsage
from app.metering.meter import UsageRecord

log = logging.getLogger(__name__)

# Which provider a `call_kind` belongs to. Chat kinds all go to OpenRouter --
# including the Ragas judge and the golden-set drafter, which are the two the
# console would otherwise under-report.
_PROVIDER = {
    "embedding": ("openrouter", "embedding"),
    "rerank": ("cohere", "rerank"),
}
_DEFAULT = ("openrouter", "chat")


def to_row(record: UsageRecord, *, query_id: uuid.UUID | None = None) -> ApiUsage:
    """One record -> one unsaved `api_usage` row."""
    provider, operation = _PROVIDER.get(record.call_kind, _DEFAULT)
    return ApiUsage(
        user_id=record.user_id,
        agent_id=record.agent_id,
        # The record wins if it named one; otherwise the turn's query.
        query_id=record.query_id or query_id,
        provider=provider,
        operation=operation,
        call_kind=record.call_kind,
        model=record.model,
        served_provider=record.served_provider,
        generation_id=record.generation_id,
        prompt_tokens=record.prompt_tokens,
        completion_tokens=record.completion_tokens,
        reasoning_tokens=record.reasoning_tokens,
        cached_tokens=record.cached_tokens,
        # REPORTED cost goes in `cost_usd`; an estimate goes in the original
        # `estimated_cost` column. Keeping them in separate columns is what lets
        # the console say which half of a total it actually knows, rather than
        # summing a measurement and a guess into one confident number.
        # `cost_usd` means THE PROVIDER TOLD US. An inferred figure goes in the
        # original `estimated_cost` column and is never mixed in -- rerank is the
        # only producer of one, and it carries it in `detail` because it has no
        # reported cost to displace. See `app/metering/rerank.py`.
        cost_usd=None if record.cost_is_estimated else record.cost_usd,
        estimated_cost=(
            record.detail.get("estimated_cost")
            if record.cost_is_estimated
            else None
        ),
        cost_is_estimated=record.cost_is_estimated,
        units=record.detail.get("units"),
        duration_ms=record.duration_ms,
    )


def persist(
    db: AsyncSession,
    records: Iterable[UsageRecord],
    *,
    query_id: uuid.UUID | None = None,
) -> tuple[int | None, int | None]:
    """Add the rows to the session and return `(prompt_sum, completion_sum)`.

    Does not commit -- the caller's single commit owns these rows, so a turn that
    fails to save its answer does not leave orphaned accounting behind.

    **The sums are `None` when NOTHING was measured, and that is not a
    micro-optimisation.** `queries.prompt_tokens = 0` would say the turn was
    free; `NULL` says it was not measured. The 76 turns that predate this feature
    are NULL, and the console counts them as unmeasured rather than as zeroes --
    the same trap EVAL.md documents where a metric's mean has its own denominator
    and the footnote does not.
    """
    rows = [to_row(record, query_id=query_id) for record in records]
    if not rows:
        return None, None

    for row in rows:
        db.add(row)

    prompt = [r.prompt_tokens for r in rows if r.prompt_tokens is not None]
    completion = [r.completion_tokens for r in rows if r.completion_tokens is not None]
    return (sum(prompt) if prompt else None, sum(completion) if completion else None)


def persist_quietly(
    db: AsyncSession,
    records: Iterable[UsageRecord],
    *,
    query_id: uuid.UUID | None = None,
) -> tuple[int | None, int | None]:
    """`persist`, but a failure here never costs the user their answer.

    Same asymmetry as `UsageMeter.on_llm_end`: the accounting is what should fail
    if the accounting is broken. The one difference is that this runs inside the
    caller's transaction, so a raise would roll back the ANSWER too -- which is
    why it exists at all rather than the caller just calling `persist`.
    """
    try:
        return persist(db, records, query_id=query_id)
    except Exception:
        log.warning("could not persist usage for this turn", exc_info=True)
        return None, None
