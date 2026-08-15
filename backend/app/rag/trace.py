"""Decision logging -- the rows that ARE the Trace view.

PRD section 4.3 says of `trace_events`: "one row per agent decision; this table
*is* the Trace view". That phrasing is the whole specification. There is no
second store, no log file to correlate against, no in-memory span that a UI
could read later -- if a decision is not written here it did not happen as far
as the product is concerned, and the Stage 2 deliverable ("show me that the
agent chose to rewrite") has nothing to render.

Two consequences shape this module.

**Payloads must be reconstructive, not descriptive.** "Reranked 20 -> 3" is a
sentence; `{"before": [...], "after": [...]}` is evidence. The Stage 2 demo is
the reordering itself, so the ordering has to be in the row. JSONB was chosen in
the PRD precisely so the shape can vary per event type without a column per
field, and Postgres can still query into it.

**Ordering is ours to assign, not the clock's.** `created_at` ties: several
events inside one turn land within the same millisecond, and two rows written in
the same transaction can share a `now()`. `step_index` is a counter on this
object, which is why the recorder is stateful rather than a free function.

The recorder buffers into the caller's session and never commits. A turn's
trace belongs to its `queries` row -- half a trace pointing at a query that was
rolled back is worse than no trace, because it looks like a complete record of a
different thing.
"""

from __future__ import annotations

import math
import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import TraceEvent

# The six event types from PRD section 4.3, as constants rather than bare
# strings at every call site. `trace_events.event_type` is a plain String(32)
# with no CHECK constraint, so a typo is accepted by Postgres, stored happily,
# and then simply fails to render in a Trace view that switches on the known
# set -- a missing step, not an error.
RETRIEVE = "RETRIEVE"
SCORE_CHECK = "SCORE_CHECK"
REWRITE = "REWRITE"
RERANK = "RERANK"
GENERATE = "GENERATE"
REFUSE = "REFUSE"

# Three more for the agent loop. The six above describe a fixed pipeline, where
# the only question a trace answers is "what did each stage do?". Once the model
# chooses its own actions there is a prior question -- "why did it do that at
# all?" -- and TOOL_CALL is the row that answers it: the arguments the model
# picked are the decision, and the result is merely the consequence.
#
# TOOL_ERROR is separate from TOOL_RESULT rather than a flag on it, because a
# failed tool call is not a failed turn. The loop hands the error back to the
# model, which usually fixes its own code and succeeds on the next step, and a
# trace that renders those two identically hides the single most interesting
# thing an agentic system does.
#
# No migration: `event_type` is String(32) with no CHECK constraint and `payload`
# is JSONB. The only gate is this frozenset -- `record()` raises on anything
# outside it, which is why extending it has to happen before the first write.
TOOL_CALL = "TOOL_CALL"
TOOL_RESULT = "TOOL_RESULT"
TOOL_ERROR = "TOOL_ERROR"

EVENT_TYPES = frozenset(
    {
        RETRIEVE,
        SCORE_CHECK,
        REWRITE,
        RERANK,
        GENERATE,
        REFUSE,
        TOOL_CALL,
        TOOL_RESULT,
        TOOL_ERROR,
    }
)


def _jsonable(value: Any) -> Any:
    """Coerce a payload into something JSONB will actually accept.

    Two traps live here, both of which surface far from their cause.

    A `uuid.UUID` is the obvious one: chunk ids are the most useful thing a
    payload can carry, they are UUIDs everywhere else in this codebase, and
    `json.dumps` refuses them. The failure arrives at flush time as a
    serialisation error naming the driver, several statements after the
    `record()` call that built the dict.

    A non-finite float is the subtle one. Postgres `jsonb` rejects `NaN` and
    `Infinity` outright -- they are valid Python floats and invalid JSON -- with
    "invalid input syntax for type json", which reads as a malformed payload
    rather than as one bad number inside a well-formed one. Scores should never
    be non-finite, but a payload is diagnostic data and it must not be the thing
    that kills the request it was diagnosing.

    Anything unrecognised is stringified rather than raised on, for the same
    reason: tracing is observation, and an observer that can abort the thing it
    observes is worse than one that records an imperfect note.
    """
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    # bool before int/float: bool subclasses int, and JSON wants true/false.
    if isinstance(value, bool) or value is None or isinstance(value, (str, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


class TraceRecorder:
    """Appends `trace_events` rows for one query, in order.

    Stateful on purpose -- it owns the `step_index` counter, which is the only
    reliable ordering within a turn (see the module docstring). One recorder per
    `queries` row; constructing a second one for the same query would restart
    the counter and collide on `ix_trace_query_step`'s ordering, producing two
    interleaved step 0s.

    `record()` is synchronous because it does no I/O: it adds a pending row to
    the session and returns. The caller commits, once, alongside the `queries`
    and `query_chunks` rows the trace describes.
    """

    def __init__(self, db: AsyncSession, query_id: uuid.UUID) -> None:
        self._db = db
        self.query_id = query_id
        # 0-based, matching `chunks.chunk_index`. It is an index into the turn,
        # not a rank -- the first thing that happened is step 0.
        self.step_index = 0
        # Kept so a caller can inspect what it recorded without a round trip
        # back to Postgres. These are the same pending objects the session
        # holds, so reading `.id` before the flush gives the client-side UUID
        # default rather than None.
        self.events: list[TraceEvent] = []

    def record(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
        score: float | None = None,
        duration_ms: int | None = None,
    ) -> TraceEvent:
        """Append one decision. Returns the pending row.

        `score` is promoted out of the payload into its own column because it is
        the one field worth filtering and charting across turns -- "show every
        query whose top retrieval score fell below 0.5" is a WHERE clause on a
        Float, not a JSONB extraction. Put it in both when it is also part of a
        payload's story; duplication of one number is cheaper than a query that
        has to reach into JSON.

        An unknown `event_type` raises rather than being stored. The alternative
        is a row that no Trace view knows how to draw, and the bug then presents
        as a missing step in a UI rather than as a failure at the line that
        wrote it.
        """
        if event_type not in EVENT_TYPES:
            raise ValueError(
                f"Unknown trace event type {event_type!r}; "
                f"expected one of {sorted(EVENT_TYPES)}"
            )

        event = TraceEvent(
            id=uuid.uuid4(),
            query_id=self.query_id,
            step_index=self.step_index,
            event_type=event_type,
            payload=_jsonable(payload) if payload is not None else None,
            score=float(score) if score is not None else None,
            duration_ms=int(duration_ms) if duration_ms is not None else None,
        )
        self.step_index += 1

        self._db.add(event)
        self.events.append(event)
        return event
