"""Rebuild one turn's tool trajectory from its trace rows, as a ragas sample.

Ragas' agent metrics consume a `MultiTurnSample` -- an ordered message list of
Human / AI / Tool. This system does not keep one: `run_agent_loop` builds its
`messages` locally and returns only the final text, because `pipeline.py` never
touches the database and `TraceRecorder` is unreachable from the loop. What it
DOES keep is one row per tool call and one per result, which is enough to
reconstruct the trajectory after the fact.

Two rules decide whether this file is correct, and both are the kind that pass a
casual test and fail a real turn.

**Pair by `call_id`, never by adjacency.** Rows come back ordered by
`step_index`, and today a step's calls are dispatched sequentially so call and
result happen to alternate. That is a coincidence of the current implementation
-- `agent_loop` says in as many words that sequential dispatch is a choice, not a
constraint -- and pairing on it would be a bug waiting for the day that changes.
Case 13 in `scripts/agent_metrics_check.py` interleaves two calls specifically to
fail an adjacency-based reader.

**Synthesise a missing result; never skip the call.** `MultiTurnSample` runs a
`field_validator` requiring every `ToolMessage` to follow an `AIMessage` whose
`tool_calls` is non-empty. Dropping an unpaired result would leave the CALL in
place and silently shorten the trajectory -- the judge would then read a
conversation in which the agent asked and was never answered, and score the agent
for it. An empty `ToolMessage` says "we have no record of what came back", which
is true.

**These are ragas' message classes, not langchain's.** `ragas.messages` and
`langchain_core.messages` both export `HumanMessage`, `AIMessage` and
`ToolMessage`, and importing the wrong pair does not raise -- `MultiTurnSample`
accepts the list and every metric reads zero tool calls off it. Case 10 asserts
over this file's source text that langchain's are never imported here, because
that is the only way to catch a swap that produces no error.
"""

from __future__ import annotations

import uuid
from typing import Any, Iterable, Protocol

from ragas.dataset_schema import MultiTurnSample
from ragas.messages import AIMessage, HumanMessage, ToolCall, ToolMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import TraceEvent
from app.rag.trace import TOOL_CALL, TOOL_ERROR, TOOL_RESULT


class _Event(Protocol):
    """What the pure function needs. A `TraceEvent` satisfies it; so does a stub.

    Typed as a Protocol rather than as `TraceEvent` so the builder is testable
    with no database and no ORM -- which is what keeps its cases at layer 1,
    where they run in milliseconds and cannot be skipped for want of a network.
    """

    event_type: str
    payload: dict[str, Any] | None


def trajectory_from_rows(
    *,
    question: str,
    answer: str | None,
    events: Iterable[_Event],
) -> MultiTurnSample | None:
    """Assemble the sample. Returns None only when there is no turn to describe.

    **A turn with NO tool call still gets a sample**, and the first version of this
    function got that wrong in a way no offline case could see. It returned `None`
    whenever `calls` was empty, on the reasoning that the agent metrics have
    nothing to say about a turn where the model answered directly. That reasoning
    is right about the two TOOL-CALL metrics -- which this project does not use --
    and wrong about the one it does: `AgentGoalAccuracyWithReference` asks whether
    the agent achieved what was asked, and "it answered without searching" is an
    outcome to be judged, not a turn to be skipped.

    The cost of the mistake was invisible in every harness and obvious in one real
    run: `agentic_check` S35 reported `searched=False goal_accuracy=None` on a turn
    that had a question, an answer and a reference answer. **The judged metric was
    silently opting out of every turn the model answered directly** -- so a card
    reading "not measured" would have meant "the model was efficient", which is the
    opposite of what a reader would conclude.

    `None` is now reserved for the one case where there is genuinely nothing to
    describe: no question. Whether a missing tool call is a FAILURE remains the
    caller's decision, read off `expected_tool_use` -- which is where it belonged
    all along.
    """
    rows = list(events)

    calls: list[tuple[str, dict[str, Any], str, str]] = []  # call_id, args, tool, assistant_text
    results: dict[str, str] = {}

    for row in rows:
        payload = row.payload or {}
        call_id = str(payload.get("call_id") or "")
        if row.event_type == TOOL_CALL:
            args = payload.get("args")
            calls.append(
                (
                    call_id,
                    args if isinstance(args, dict) else {},
                    str(payload.get("tool") or "unknown"),
                    str(payload.get("assistant_text") or ""),
                )
            )
        elif row.event_type in (TOOL_RESULT, TOOL_ERROR):
            # `.get`-as-migration: turns from before change set 16 have no
            # `content` key at all. An empty string is the honest rendering --
            # "we did not record this" -- and `summary` is deliberately NOT
            # substituted, because a one-line human summary standing in for the
            # model's actual input would make the trajectory a plausible fiction
            # rather than an admitted gap.
            if call_id:
                results[call_id] = str(payload.get("content") or "")

    if not (question or "").strip():
        # Nothing to describe. Every turn has a question, so this is a guard
        # against a caller passing garbage rather than a state the system reaches.
        return None

    messages: list[Any] = [HumanMessage(content=question or "")]
    for call_id, args, tool, assistant_text in calls:
        messages.append(
            AIMessage(
                # ragas' `Message.content` is a required `str`; a model that
                # emitted a bare tool call with no prose is common, and an empty
                # string is what it actually said.
                content=assistant_text,
                tool_calls=[ToolCall(name=tool, args=args)],
            )
        )
        # `.get`, so an unpaired call still produces the ToolMessage the
        # validator requires. See the module docstring.
        messages.append(ToolMessage(content=results.get(call_id, "")))

    messages.append(AIMessage(content=answer or ""))
    return MultiTurnSample(user_input=messages)


async def build_trajectory(
    db: AsyncSession, query_id: uuid.UUID, *, question: str, answer: str | None
) -> MultiTurnSample | None:
    """Fetch this turn's trace rows and hand them to the pure function.

    Deliberately thin. Everything decidable is in `trajectory_from_rows`, which
    needs no database -- the split is what lets the ordering and pairing rules be
    tested at layer 1 rather than behind a live run.
    """
    rows = (
        await db.scalars(
            select(TraceEvent)
            .where(TraceEvent.query_id == query_id)
            .order_by(TraceEvent.step_index)
        )
    ).all()
    return trajectory_from_rows(question=question, answer=answer, events=rows)
