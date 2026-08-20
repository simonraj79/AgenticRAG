"""Embedding spend, recovered the same way chat spend is — read, never computed.

**Embedding cost is REPORTED, not estimated, and the first version of this
feature said the opposite.** Measured 2026-08-20 through this repo's own
`get_embeddings()`:

    usage    Usage(prompt_tokens=7, total_tokens=7, cost=1.4e-06,
                   cost_details={...})
    provider "Google AI Studio"
    id       "gen-emb-1787199865-2n8YI6ISF4fmdg3MBDvM"

All three survive into the `openai` SDK's response object — `cost` on
`usage.model_extra`, `provider` and `id` on the response's. Nothing is dropped
here the way `_create_usage_metadata` drops chat cost. What was missing was a
reader, not the data.

**THE SEAM IS THE CLIENT, NOT THE METHOD, and that is deliberate.**
`OpenAIEmbeddings` has no callback support, so the obvious move is to subclass
and override `embed_documents` / `aembed_documents`. That would duplicate the
parent's batching loop and, worse, would cover only the branch this repo happens
to take today: `check_embedding_ctx_length=False` routes through a plain loop,
while `True` routes through `_get_len_safe_embeddings`. Both call
`self.client.create(...)`. Wrapping the client meters both, so flipping that flag
— a thing CLAUDE.md documents as one 400 away from being reconsidered — cannot
silently un-meter the system.

`OpenAIEmbeddings` sets `model_config = {"extra": "forbid"}`, so nothing can be
attached to the instance. Assignment to the DECLARED `client` / `async_client`
fields is still allowed, and is verified rather than assumed:
`scripts/metering_check.py` case 9.

**Never estimate from token counts.** Same rule as chat, same reason: OpenRouter
routes to whichever endpoint serves the model, and the reported figure is the one
that is billed.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from langchain_core.embeddings import Embeddings

from app.metering.context import current_scope
from app.metering.meter import UsageRecord, UsageSink

log = logging.getLogger(__name__)


def _usage_from(response: Any) -> tuple[dict, str | None, str | None]:
    """`(usage, provider, generation_id)` off an embeddings response.

    Defended at every access because none of this shape is ours -- the same
    reasoning `app/handouts/jobs.py` gives for reading `response_metadata`. A
    response that carries nothing yields an empty dict, which becomes a row with
    a NULL cost: **not measured, never free**.
    """
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")

    if hasattr(usage, "model_dump"):
        usage = usage.model_dump()
    elif not isinstance(usage, dict):
        usage = {}

    def field(name: str) -> Any:
        value = getattr(response, name, None)
        if value is None and isinstance(response, dict):
            value = response.get(name)
        return value

    return usage, field("provider"), field("id")


class _MeteredResource:
    """Wraps openai's `Embeddings` resource. Records, then returns untouched.

    `__getattr__` delegates everything else, so the wrapper is transparent to any
    part of langchain that reaches for another attribute of the resource.
    """

    def __init__(self, inner: Any, on_usage, *, is_async: bool) -> None:
        self._inner = inner
        self._on_usage = on_usage
        self._is_async = is_async

    def create(self, **kwargs: Any) -> Any:
        if self._is_async:
            return self._acreate(**kwargs)
        started = time.perf_counter()
        response = self._inner.create(**kwargs)
        self._on_usage(response, kwargs, int((time.perf_counter() - started) * 1000))
        return response

    async def _acreate(self, **kwargs: Any) -> Any:
        started = time.perf_counter()
        response = await self._inner.create(**kwargs)
        self._on_usage(response, kwargs, int((time.perf_counter() - started) * 1000))
        return response

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def meter_embeddings(
    embeddings: Embeddings,
    *,
    model: str,
    sink: UsageSink,
    strict: bool = False,
) -> Embeddings:
    """Attach metering to an `OpenAIEmbeddings`. Returns the same object.

    A no-op for any other implementation -- the Google rollback route
    (`EMBEDDING_ROUTE=google`) has no `client` to wrap, and losing metering is
    the correct outcome there rather than a crash on a route that exists to be
    an escape hatch.
    """

    def on_usage(response: Any, kwargs: dict, duration_ms: int) -> None:
        try:
            usage, provider, generation_id = _usage_from(response)
            scope = current_scope()
            inputs = kwargs.get("input")
            record = UsageRecord(
                # ALWAYS "embedding", never the ambient kind. An embed_query
                # inside a turn is scoped `generation` by `run_turn`, and filing
                # it there would hide the retrieval half of the bill inside the
                # answer half.
                call_kind="embedding",
                user_id=scope.user_id,
                agent_id=scope.agent_id,
                query_id=scope.query_id,
                model=model,
                served_provider=provider,
                generation_id=generation_id,
                prompt_tokens=usage.get("prompt_tokens"),
                # Embeddings have no completion side. Left NULL rather than 0,
                # so a SUM over the column is not quietly diluted by rows that
                # never had one.
                completion_tokens=None,
                cost_usd=usage.get("cost"),
                cost_is_estimated=False,
                duration_ms=duration_ms,
                detail={"units": len(inputs) if isinstance(inputs, list) else 1},
            )
            sink.record(record)
        except Exception:
            if strict:
                raise
            log.warning("embedding metering failed; call unaffected", exc_info=True)

    client = getattr(embeddings, "client", None)
    async_client = getattr(embeddings, "async_client", None)
    if client is None and async_client is None:
        # Not an OpenAI-protocol embedder. See the docstring.
        return embeddings

    if client is not None:
        embeddings.client = _MeteredResource(client, on_usage, is_async=False)
    if async_client is not None:
        embeddings.async_client = _MeteredResource(
            async_client, on_usage, is_async=True
        )
    return embeddings
