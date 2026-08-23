"""Read what a model call cost off the result. Never compute it.

THE FINDING THIS MODULE IS BUILT ON, measured 2026-08-20 against this repo's own
`build_chat_model` (deepseek-v4-flash, `top_k` in `extra_body`,
`require_parameters` on, tools bound):

    path                       tokens   cost         served provider
    ainvoke                    yes      2.002e-05    -
    astream (accumulated)      yes      NONE         NONE
    raw SSE, no client         yes      9.1e-07      Relace

Cost is on the wire in BOTH cases. `langchain_openai._create_usage_metadata`
(chat_models/base.py:4175) keeps the OpenAI-standard fields and drops `cost`,
`cost_details`, `provider` and the `gen-` id on the floor. So this is a lossy
normalisation in the CLIENT, not a limitation of OpenRouter -- and that
distinction is the entire reason this project does not need a price table.

**Never multiply tokens by a price.** OpenRouter's actual charge depends on which
of 28 endpoints served the call, and CLAUDE.md records those differing by ~2x on
one model (StreamLake $0.06426/M, Baidu $0.0644/M, Decart $0.0657/M, DeepSeek
first-party $0.14/M). A computed cost would be a confident number that is wrong
by an amount nobody can see. `app/metering/chat.py` recovers the reported one.

**`None` is not `0.0`, and the difference is the whole point.** A call that was
not measured must never render as a call that was free. EVAL.md already documents
this trap under a different name -- a metric's mean has its own denominator and
the scorecard's footnote does not -- and the admin console prints
`n/m measured` for exactly this reason.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.outputs import LLMResult

from app.metering.context import current_scope, emit_record

log = logging.getLogger(__name__)

# The key `app/metering/chat.py` writes into `generation_info` and this module
# reads back. It is the contract between the two halves, so it lives in exactly
# one place -- the same rule CLAUDE.md applies to
# `app.rag.ingest.INGEST_FAILURE_ACTION`: import it, never retype it.
USAGE_KEY = "openrouter_usage"

# The key `app/metering/chat.py` adds to `llm_output` on the NON-streamed path.
# It is deliberately not "provider": `llm_output` already carries
# `model_provider`, hard-coded to "openai" by langchain, and two keys one letter
# apart in the same dict is how the decoy gets read by mistake.
PROVIDER_KEY = "openrouter_provider"


@dataclass
class UsageRecord:
    """One model call. The unit the admin console groups on.

    PER CALL, not per turn, and that was a decision rather than a default: the
    Ragas judge and the golden-set drafter belong to no `queries` row at all, so
    a turn-shaped unit would have had nowhere to put eval spend and would have
    reported a complete-looking total that silently excluded it.
    """

    call_kind: str = "unknown"
    user_id: uuid.UUID | None = None
    agent_id: uuid.UUID | None = None
    query_id: uuid.UUID | None = None

    model: str | None = None
    # WHICH OpenRouter endpoint served it. CLAUDE.md notes that `llm_check.py`
    # structurally cannot answer this -- an offline harness sees only what the
    # repo put in the request -- and that only a live call can. Every live call
    # is now answering it.
    served_provider: str | None = None
    generation_id: str | None = None

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    reasoning_tokens: int | None = None
    cached_tokens: int | None = None

    # None means NOT MEASURED. 0.0 would mean free.
    cost_usd: float | None = None
    # True only where a report was unavailable and a count stood in for it --
    # embeddings and rerank. False for every chat call.
    cost_is_estimated: bool = False

    duration_ms: int | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int | None:
        if self.prompt_tokens is None and self.completion_tokens is None:
            return None
        return (self.prompt_tokens or 0) + (self.completion_tokens or 0)


class UsageSink(Protocol):
    """Where a record goes. Feature 01 ships logging; feature 02 ships the DB."""

    def record(self, record: UsageRecord) -> None: ...


class LoggingSink:
    """The default sink: buffer for the turn if one is collecting, else log.

    **One sink, two behaviours, and that is deliberate.** Making the database
    sink a separate class the caller has to remember to install would mean every
    new entry point starts unmetered and looks fine -- the same failure shape as
    a `meter_as` nobody opened. Instead the sink always tries the active
    collection first (`app/metering/context.collect_usage`) and falls back to a
    log line, so a call made outside a collected turn is visible rather than
    lost.

    It cannot corrupt anything either way: appending to a list and writing a log
    line are the only two things it does. The database write happens later, in
    the request handler, inside the transaction that already exists.
    """

    def record(self, record: UsageRecord) -> None:
        if emit_record(record):
            return
        log.info(
            "usage kind=%s user=%s model=%s provider=%s tokens=%s/%s cost=%s",
            record.call_kind,
            record.user_id,
            record.model,
            record.served_provider,
            record.prompt_tokens,
            record.completion_tokens,
            record.cost_usd,
        )


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


class UsageMeter(AsyncCallbackHandler):
    """One handler, registered inside `build_chat_model`, meters everything.

    That works because `build_chat_model` (`app/rag/llm.py:241`) is the only
    place a chat model is constructed in this project -- the same property
    CLAUDE.md relies on for the retriever. Eight call sites, one factory, so this
    covers generation, the rewriter, routing, the critic, handout code, the
    golden-set drafter AND the Ragas judge without any of them being touched.
    The last two are the ones per-call-site instrumentation would have forgotten.
    """

    # `on_llm_end` firing on both `ainvoke` and `astream` was measured, not
    # assumed -- see the module docstring's table.
    raise_error = False

    def __init__(
        self,
        sink: UsageSink | None = None,
        *,
        strict: bool = False,
        model: str | None = None,
    ) -> None:
        self.sink = sink
        self.strict = strict
        # **The slug is passed in, not read off the response, and that is a fix
        # rather than a convenience.** On a streamed call `llm_output` is empty
        # and the only model name available is `generation_info["model_name"]`,
        # which streaming has CONCATENATED with itself -- measured
        # "deepseek/...-0731deepseek/...-0731". Refusing to read it left the
        # field null on every streamed record, i.e. every generation turn, which
        # is exactly the rows the admin console groups by model.
        #
        # `build_chat_model` knows the resolved slug at construction time and a
        # meter belongs to one model instance, so this is both authoritative and
        # stable. It also removes the last reason to trust anything in
        # `llm_output` for identity.
        self.model = model

    # -- extraction ------------------------------------------------------

    def extract(self, response: LLMResult) -> UsageRecord | None:
        """Pull one record out of a finished call. Pure; no I/O.

        Three sources, in order of how much they carry. Defended at every access
        because none of this shape is ours -- the same reasoning
        `app/handouts/jobs.py` gives for reading `response_metadata`.
        """
        scope = current_scope()
        record = UsageRecord(
            call_kind=scope.call_kind,
            user_id=scope.user_id,
            agent_id=scope.agent_id,
            query_id=scope.query_id,
        )

        generation = None
        generations = getattr(response, "generations", None) or []
        if generations and generations[0]:
            generation = generations[0][0]

        # 1. `ainvoke`: llm_output carries token_usage WITH cost, and the id.
        out = getattr(response, "llm_output", None) or {}
        token_usage = out.get("token_usage") or {}
        record.model = self.model or out.get("model_name")
        record.generation_id = out.get("id")
        # Non-streamed: recovered by `MeteredChatOpenAI._create_chat_result`.
        # NEVER `out.get("model_provider")` -- that is langchain's hard-coded
        # "openai" and names the protocol, not the endpoint that served the call.
        record.served_provider = out.get(PROVIDER_KEY)

        # 2. `astream`: llm_output is EMPTY (measured: `llm_output_keys: []`).
        #    Everything lives in generation_info, put there by MeteredChatOpenAI.
        info = dict(getattr(generation, "generation_info", None) or {})
        recovered = info.get(USAGE_KEY) or []
        if recovered:
            # A LIST because streaming merges generation_info across chunks and
            # `merge_dicts` raises on two unequal scalars while concatenating
            # lists. The last frame is the authoritative one; more than one is
            # not an error, it is a provider that reported twice.
            last = recovered[-1] if isinstance(recovered, list) else recovered
            if isinstance(last, dict):
                token_usage = last.get("usage") or token_usage
                record.served_provider = last.get("provider") or record.served_provider
                record.generation_id = last.get("id") or record.generation_id
                if len(recovered) > 1:
                    record.detail["usage_frames"] = len(recovered)

        # Deliberately NOT falling back to `info.get("model_name")`. On a
        # streamed call that value is the slug concatenated with itself, and a
        # nonsense slug in a GROUP BY is worse than a null: a null shows up as
        # "not measured", a doubled slug shows up as a second model that does not
        # exist and quietly splits the spend for the one that does.

        record.prompt_tokens = _int(token_usage.get("prompt_tokens"))
        record.completion_tokens = _int(token_usage.get("completion_tokens"))
        record.cost_usd = _float(token_usage.get("cost"))
        record.reasoning_tokens = _int(
            (token_usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
        )
        record.cached_tokens = _int(
            (token_usage.get("prompt_tokens_details") or {}).get("cached_tokens")
        )

        # 3. Last resort: langchain's normalised shape. Tokens only -- it is the
        #    very structure that dropped the cost. Better than nothing, and the
        #    null cost is what tells the console this row is unpriced.
        message = getattr(generation, "message", None)
        usage_metadata = getattr(message, "usage_metadata", None) or {}
        if record.prompt_tokens is None:
            record.prompt_tokens = _int(usage_metadata.get("input_tokens"))
        if record.completion_tokens is None:
            record.completion_tokens = _int(usage_metadata.get("output_tokens"))

        return record

    # -- the callback ----------------------------------------------------

    async def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        """Never let metering kill a turn.

        A user asked a question and the model answered it. If the accounting
        fails, the accounting is what should fail. `metering_strict` re-raises so
        that a bug is caught in a harness rather than silently tolerated
        everywhere -- a swallow with no strict mode is how a meter records
        nothing for a month and reports success.
        """
        try:
            record = self.extract(response)
            if record is not None and self.sink is not None:
                self.sink.record(record)
        except Exception:
            if self.strict:
                raise
            log.warning("metering failed for one call; turn unaffected", exc_info=True)
