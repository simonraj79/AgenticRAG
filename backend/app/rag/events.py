"""The SSE wire vocabulary -- one definition, reachable from both halves.

`app/rag/pipeline.py` and `app/rag/agent_loop.py` *produce* frames;
`app/api/stream.py` encodes and sends them. The producing half may not import
from `app.api` -- the same direction rule `app/rag/refusal.py` states, and for
the same reason: a second copy of a list of strings is the failure worth ruling
out structurally rather than remembering. So the names live here, where both
sides reach them, instead of as literals typed twice.

**These are not trace event types and must never become them.** `app/rag/trace.py`
records what the agent *decided*; this records what a browser is *told while it
waits*. `TraceRecorder.record` raises on an unknown type and `EVENT_TYPES` is
deliberately unchanged by this feature -- putting a wire protocol in the decision
log would mean a transport change showed up as an agent decision forever after.

The one place the two vocabularies touch is `PHASE_*` below, and the touch is
structural on purpose.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from app.rag import trace

# What `pipeline`, `agent_loop` and `run_turn` are handed. `None` everywhere it
# is optional, and `None` means "take the branch this code took before streaming
# existed" -- see `pipeline.answer_question`, where the non-streaming call is the
# IDENTICAL line it always was rather than a similar one.
Emit = Callable[[str, dict[str, Any]], Awaitable[None]]

# ---------------------------------------------------------------- event names
#
# Flat lowercase snake_case, no dots. The SSE `event:` header always equals the
# payload's `type`, so a frame is self-describing whether a parser dispatches on
# the header or on the body.
START = "start"
PHASE = "phase"
TOKEN = "token"
TOOL_CALL = "tool_call"
TOOL_RESULT = "tool_result"
TOOL_ERROR = "tool_error"
ANSWER_RESET = "answer_reset"
DONE = "done"
ERROR = "error"

EVENT_NAMES = frozenset(
    {
        START,
        PHASE,
        TOKEN,
        TOOL_CALL,
        TOOL_RESULT,
        TOOL_ERROR,
        ANSWER_RESET,
        DONE,
        ERROR,
    }
)

# ---------------------------------------------------------------- phase names
#
# **Derived from the trace constants rather than retyped, and that is the whole
# point of importing `trace` into a wire module.** The phase vocabulary IS the
# trace vocabulary lowercased; writing `"rewrite"` here as a literal would let
# the two drift the first time someone renamed a trace event, and the symptom
# would be a phase the client silently ignores rather than an error. The inverse
# mapping a reader needs -- wire name back to trace name -- is `name.upper()`,
# and it holds because of these four lines rather than by convention.
PHASE_REWRITE = trace.REWRITE.lower()
PHASE_RETRIEVE = trace.RETRIEVE.lower()
PHASE_RERANK = trace.RERANK.lower()
PHASE_GENERATE = trace.GENERATE.lower()

STARTED = "started"
FINISHED = "finished"


async def phase(
    emit: Emit,
    name: str,
    status: str,
    *,
    duration_ms: int | None = None,
    **extra: Any,
) -> None:
    """Emit one `phase` frame.

    `duration_ms` is always present and is `null` on a `started` frame, because a
    key that appears and disappears makes the client test for existence where it
    should be testing a value -- and "no duration yet" and "duration was zero"
    are different facts a stage boundary has to be able to state.
    """
    await emit(PHASE, {"name": name, "status": status, "duration_ms": duration_ms, **extra})
