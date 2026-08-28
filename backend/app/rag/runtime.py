"""Which engine executes the generation step. One function, one `if`.

THIS FILE IS THE WHOLE MIGRATION SURFACE, and that is a claim `grep` settles
rather than a claim this docstring makes: only one line in `backend/app/` calls
`run_agent_loop` outside a self-check redraft, and only one file in `scripts/`
drives it. Everything anyone thinks of as "the agent" -- the SSE contract,
`ask.run_turn`, the four trace row types, `api_usage`, the eval job, the Ragas
scorecard -- sits ABOVE that call and reads a `LoopResult`.

So a second runtime is not a rewrite. It is a second callable with the identical
signature and the identical return type, chosen here.

------------------------------------------------------------------
THE LAZY IMPORT IS THE ROLLBACK, AND IT IS LOAD-BEARING.

`app.adk` is imported INSIDE the branch, never at module scope. With
`AGENT_RUNTIME=langchain` the `google.adk` package is never imported into the
process at all -- so the rollback is not "the ADK code is not called", it is
"the ADK code does not exist in this interpreter". That is the difference
between a rollback that is argued and one that is structural, and it is the same
shape `storage.py` uses for the `postgres` road.

`scripts/adk_model_check.py` A11 asserts that nothing outside `app/adk/` imports
`google.adk`, so the property cannot decay by someone adding a convenient
top-level import here.
------------------------------------------------------------------

WHY THERE IS NO `agents.agent_runtime` COLUMN.

The obvious design gives each agent its own runtime, which would let one pilot
agent move while the rest stay put. It was not built, for a reason worth
recording: a nullable column immediately introduces the three-valued question
CLAUDE.md records the admin console getting wrong twice -- NULL means "predates
the column", which is NOT "langchain" -- and it does that in exchange for a
choice that an operator makes roughly once.

The A/B this change set actually needs is *"run the same golden set through both
engines and diff the scorecards"*, and that is served by the `runtime` argument
below, which the eval driver and every harness can pass explicitly without a
migration against a production database holding real users.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Protocol

from app.config import AGENT_RUNTIMES, settings

if TYPE_CHECKING:  # pragma: no cover -- types only
    from app.rag.agent_loop import LoopResult

log = logging.getLogger("uvicorn.error")


class LoopCallable(Protocol):
    """The contract both runtimes satisfy, stated once.

    Declared as a Protocol rather than left implicit because the entire safety
    argument of this change set is *"the two callables are interchangeable"*, and
    an interchangeability claim that is never written down is a claim nobody can
    check. `scripts/adk_loop_check.py` asserts the two signatures match; this is
    what it asserts them against.
    """

    async def __call__(self, **kwargs: Any) -> LoopResult: ...


def select_runtime(runtime: str | None = None) -> LoopCallable:
    """The loop callable for this turn.

    `runtime` overrides the setting and exists for exactly two callers: the eval
    driver running the same golden set through both engines, and the harnesses.
    A request path passes nothing and gets `settings.agent_runtime`.

    **An unknown value raises rather than falling back.** `settings.agent_runtime`
    is already validated at load, so the only way an unknown value reaches here is
    an explicit argument from a caller who believes they are selecting something.
    Falling through to the langchain loop would answer their question correctly
    with the wrong engine, and a scorecard would then be attributed to a runtime
    that never ran -- which is precisely the failure the validator on the setting
    exists to prevent, arriving through the door the validator does not watch.
    """
    chosen = (runtime or settings.agent_runtime or "").strip()
    if chosen not in AGENT_RUNTIMES:
        raise ValueError(
            f"unknown agent runtime {chosen!r}; expected one of {AGENT_RUNTIMES}"
        )

    if chosen == "adk":
        # Imported here, not at module scope. See the docstring above -- this is
        # the rollback, and moving this line to the top of the file silently
        # removes it while every test still passes.
        from app.adk.loop import run_agent_loop_adk

        return run_agent_loop_adk

    from app.rag.agent_loop import run_agent_loop

    return run_agent_loop


def active_runtime(runtime: str | None = None) -> str:
    """The runtime name that `select_runtime` would choose, for recording.

    Separate from `select_runtime` because the eval run and the trace want to
    RECORD which engine answered, and deriving that by comparing function
    identities at the call site is the kind of cleverness that stops being true
    when someone wraps one of them.
    """
    return (runtime or settings.agent_runtime or "").strip()
