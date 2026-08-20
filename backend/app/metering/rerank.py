"""Cohere rerank spend. The one cost centre where a number has to be inferred.

**Cohere reports UNITS and does not report COST.** Measured 2026-08-20 against
`rerank-v3.5` through this repo's own compressor:

    meta.billed_units  ApiMetaBilledUnits(search_units=1.0, ...)

That is exactly what Cohere bills on — one search unit is one query against up
to 100 documents — so `api_usage.units` here is a MEASUREMENT, not an estimate,
and it is the honest thing to put on the console.

**A dollar figure is opt-in and off by default, and that is deliberate.** The
rest of this package reads cost off the response and never multiplies, because
OpenRouter routes across endpoints whose prices differ by up to 3.5x. Cohere has
no such routing, so a published price times a measured unit count would be
defensible arithmetic — and it would still be a number nobody re-checks. This
repository has a standing example of exactly that failure: a Cohere key that had
been silently downgraded to a trial tier, discovered only under load, having
looked identical the whole time. A stale hardcoded price fails the same way and
is worse, because it renders confidently.

So `settings.cohere_search_unit_usd` defaults to `0.0`, meaning **do not
estimate**: units are recorded, cost stays NULL, and the console shows the call
under `calls` but not under `priced_calls`. An operator who wants the dollar
figure sets the current published price and gets it back in `estimated_cost` —
a different column from `cost_usd`, so a reported total and an inferred one are
never summed into one number.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app.metering.context import current_scope
from app.metering.meter import UsageRecord, UsageSink

log = logging.getLogger(__name__)


def _search_units(response: Any) -> float | None:
    """`meta.billed_units.search_units`, defended at every hop."""
    meta = getattr(response, "meta", None)
    billed = getattr(meta, "billed_units", None)
    units = getattr(billed, "search_units", None)
    return float(units) if isinstance(units, (int, float)) else None


class _MeteredRerankClient:
    """Wraps Cohere's `ClientV2`. Records, then returns the response untouched."""

    def __init__(self, inner: Any, on_usage, *, is_async: bool) -> None:
        self._inner = inner
        self._on_usage = on_usage
        self._is_async = is_async

    def rerank(self, **kwargs: Any) -> Any:
        if self._is_async:
            return self._arerank(**kwargs)
        started = time.perf_counter()
        response = self._inner.rerank(**kwargs)
        self._on_usage(response, kwargs, int((time.perf_counter() - started) * 1000))
        return response

    async def _arerank(self, **kwargs: Any) -> Any:
        started = time.perf_counter()
        response = await self._inner.rerank(**kwargs)
        self._on_usage(response, kwargs, int((time.perf_counter() - started) * 1000))
        return response

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def meter_rerank(
    compressor: Any,
    *,
    model: str,
    sink: UsageSink,
    unit_price_usd: float = 0.0,
    strict: bool = False,
) -> Any:
    """Attach metering to a `CohereRerank`. Returns the same object.

    Wraps the CLIENT rather than `compress_documents`, for the same reason
    `app/metering/embeddings.py` does: `CohereRerank` is reached two ways in this
    codebase -- through `ContextualCompressionRetriever` and directly from
    `aretrieve` -- and only one seam covers both.
    """

    def on_usage(response: Any, kwargs: dict, duration_ms: int) -> None:
        try:
            units = _search_units(response)
            scope = current_scope()
            documents = kwargs.get("documents")
            estimated = (
                units * unit_price_usd
                if units is not None and unit_price_usd > 0
                else None
            )
            record = UsageRecord(
                call_kind="rerank",
                user_id=scope.user_id,
                agent_id=scope.agent_id,
                query_id=scope.query_id,
                model=model,
                # Cohere is a single endpoint; there is no routing to record.
                served_provider="cohere",
                # Rerank is not token-billed. Both stay NULL rather than 0, so a
                # SUM over the token columns is not diluted by rows that never
                # had tokens to contribute.
                prompt_tokens=None,
                completion_tokens=None,
                # NEVER `cost_usd` -- that column means "the provider told us".
                cost_usd=None,
                cost_is_estimated=estimated is not None,
                duration_ms=duration_ms,
                detail={
                    "units": int(units) if units is not None else None,
                    "documents": len(documents) if isinstance(documents, list) else None,
                    "estimated_cost": estimated,
                },
            )
            # `store.to_row` reads `cost_usd` for the reported column; the
            # estimate travels in `detail` and is unpacked there.
            sink.record(record)
        except Exception:
            if strict:
                raise
            log.warning("rerank metering failed; call unaffected", exc_info=True)

    client = getattr(compressor, "client", None)
    async_client = getattr(compressor, "async_client", None)
    if client is None and async_client is None:
        return compressor

    if client is not None:
        compressor.client = _MeteredRerankClient(client, on_usage, is_async=False)
    if async_client is not None:
        compressor.async_client = _MeteredRerankClient(
            async_client, on_usage, is_async=True
        )
    return compressor
