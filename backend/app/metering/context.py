"""Who is this model call for? A ContextVar, set by the caller, read by the meter.

**Why a contextvar rather than a parameter on `build_chat_model`.** Three of the
eight model call sites build their model ONCE and reuse it for the life of the
process -- `pipeline.get_contextualizer`, `route.get_router` and
`selfcheck.get_critic` are module-level singletons. Binding a user id at
construction time would attribute every rewrite for the rest of the process to
whoever happened to ask the first question. The contextvar binds at CALL time,
which is the only moment at which the answer is known.

**Why it is safe under concurrency.** `ContextVar` is asyncio-task-local: a value
set inside one task is invisible to another, and each task started with
`asyncio.gather` gets its own copy. That is load-bearing rather than incidental
-- `app/eval/jobs.py` runs at `RAGAS_MAX_CONCURRENCY=2`, and the handout and
ingest jobs run off the request thread entirely. `scripts/metering_check.py`
case 5 asserts it with two real concurrent tasks rather than trusting the
documentation, because CLAUDE.md records a concurrency measurement in this repo
that was confidently wrong the first time it was taken.

**An unset scope is not an error, and this is deliberate.** A call made outside
any `meter_as` block still records, with `user_id=None`. Losing the attribution
must never lose the COST -- that is the same reasoning `ask.py` gives for a
nullable `session_id`: "losing the session attribution is not a reason to refuse
an authenticated request". A row nobody owns is a fact; a call nobody recorded
is a hole.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Iterator

# The call kinds. Kept as a tuple rather than an Enum because it is written to a
# `varchar` and read back by SQL the admin console groups on -- an Enum would add
# a conversion on both sides and buy nothing. `metering_check` does not pin this
# list: a new kind should not require a harness edit, only a correct string.
CALL_KINDS = (
    "generation",  # the answer the user reads
    "rewrite",     # contextualize_question -- runs EVERY turn since 2026-08-16
    "route",       # specialist selection
    "critic",      # the self-check second opinion
    "handout",     # deck / chart code generation
    "goldenset",   # the golden-set drafter
    "judge",       # Ragas
    "embedding",   # not a chat call; see meter.py
    "rerank",      # Cohere; not a chat call either
    "unknown",     # a call made outside any scope. Recorded, never dropped.
)


@dataclass(frozen=True)
class MeterScope:
    """Who/what/why, for the duration of one model call."""

    user_id: uuid.UUID | None = None
    agent_id: uuid.UUID | None = None
    query_id: uuid.UUID | None = None
    call_kind: str = "unknown"


_EMPTY = MeterScope()

_scope: ContextVar[MeterScope] = ContextVar("meter_scope", default=_EMPTY)


def current_scope() -> MeterScope:
    """The scope in force, or an empty one. Never raises, never returns None."""
    return _scope.get()


@contextmanager
def meter_as(
    *,
    user_id: uuid.UUID | None = None,
    agent_id: uuid.UUID | None = None,
    query_id: uuid.UUID | None = None,
    call_kind: str = "unknown",
    inherit: bool = True,
) -> Iterator[MeterScope]:
    """Attribute every model call made inside this block.

    **Reentrant, and inheriting by default.** `pipeline` opens a `generation`
    scope; inside it the agent loop may open a `critic` one, and that inner block
    should change the KIND without losing the user and agent the outer block
    established. So an inner `meter_as` merges over the outer rather than
    replacing it, and only the fields it names are overridden. Pass
    `inherit=False` for a genuinely unrelated call -- a background job that
    happens to run inside a request's task.

    The token is reset in a `finally`, so an exception inside the block cannot
    leak an identity into whatever runs next on this task.
    """
    base = _scope.get() if inherit else _EMPTY
    scope = replace(
        base,
        **{
            key: value
            for key, value in (
                ("user_id", user_id),
                ("agent_id", agent_id),
                ("query_id", query_id),
            )
            if value is not None
        },
        call_kind=call_kind,
    )
    token = _scope.set(scope)
    try:
        yield scope
    finally:
        _scope.reset(token)


# --------------------------------------------------------------------------
# Collection
# --------------------------------------------------------------------------
# **Records are BUFFERED and written once, not written as they happen**, and the
# reason is the first entry in CLAUDE.md's Background jobs section: a callback
# must not borrow a database session it did not open. The meter fires from
# inside langchain's callback machinery, part-way through a request whose own
# session is mid-transaction and will flush again -- writing there would
# interleave metering INSERTs with the turn's own writes and roll them back
# together on any failure.
#
# Opening a second session per model call is the other option and is worse: a
# single turn makes 1-3 generation calls plus a rewrite plus a route plus
# possibly a critic, so that is up to six connections per question on a Render
# starter plan running ONE uvicorn worker.
#
# So the callback appends to a list and the request handler persists it once,
# inside the transaction that already exists. That also makes the write
# atomic with the `queries` row it belongs to, which is what lets
# `queries.prompt_tokens` be a trustworthy cache of the SUM.

_records: ContextVar[list | None] = ContextVar("meter_records", default=None)


@contextmanager
def collect_usage() -> Iterator[list]:
    """Buffer every record produced inside this block. Drain it at the end.

    **A nested collection gets its OWN bucket, and the inner block's records do
    NOT propagate outward.** The rule is: whoever opens a collection persists
    what it collects, and nobody else does.

    An earlier version yielded the outer list to an inner block, reasoning that
    a helper wrapping itself defensively should not lose a turn's spend. That is
    a double-count bug, and `app/eval/jobs.py` is where it fires: the eval job
    opens a collection, calls `run_turn` for each of ten golden questions, and
    `run_turn` opens its own and persists everything it finds -- so every
    generation call would be written once by `run_turn` with a `query_id` and
    again by the eval job without one. The console would have reported roughly
    double the real spend, with no error anywhere and no obvious tell, because
    both totals are plausible.

    Losing a record is the lesser failure and it does not even occur here: the
    inner owner persists what it saw. `scripts/metering_check.py` case 8 pins
    the isolation.
    """
    bucket: list = []
    token = _records.set(bucket)
    try:
        yield bucket
    finally:
        _records.reset(token)


def emit_record(record) -> bool:  # noqa: ANN001 -- UsageRecord, avoiding a cycle
    """Append to the active collection. False if there is none.

    False is not an error: a model call outside any collected turn (a harness, a
    background job before it opens one) still gets logged by the sink. Losing the
    row must never mean losing the log line.
    """
    bucket = _records.get()
    if bucket is None:
        return False
    bucket.append(record)
    return True
