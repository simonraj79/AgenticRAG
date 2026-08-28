"""Turn-scoped state for one ADK invocation.

Deliberately NOT ADK's `ToolContext`. ADK's object is per-tool-call and belongs to
the framework; this one is per-TURN and belongs to us, and conflating them is how
the tenancy boundary would erode -- `google.adk.tools.ToolContext.state` is copied
wholesale into a child agent by `AgentTool`, so it is not a boundary and must
never hold anything tenancy-bearing. (`scripts/tenancy_check.py` T6 bans
`AgentTool` outright for that reason.)

**It WRAPS `app.tools.registry.ToolContext` rather than replacing it**, and that
is the single most important decision in this package. The langchain tools --
`search_corpus` and `run_python` -- are reused verbatim by `app/adk/tools.py`
rather than reimplemented, so the retrieval policy, the ledger merge, the sandbox
spawn, the `ToolOutcome` shape and the tenancy closure are all *the same objects*
under both runtimes. A second implementation of `_search` would be a second place
for the namespace to leak, and CLAUDE.md's standing rule is that the copy which
drifted is never the one you are reading.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover -- types only
    from app.db.models import Agent
    from app.rag.agent_loop import ContextLedger, ToolInvocation
    from app.tools.registry import ToolContext


@dataclass
class AdkTurnContext:
    """Everything one `run_agent_loop_adk` call accumulates.

    `agent` and `ledger` are the two things the tools close over. Everything else
    is bookkeeping the loop reads back after the runner has finished, because an
    ADK `Runner` returns an event stream rather than a result object and the facts
    `LoopResult` needs -- which step a call belonged to, how long it took, whether
    the gap trigger fired -- are only knowable from inside the callbacks.
    """

    agent: Agent
    ledger: ContextLedger

    # --- what the tools write ------------------------------------------------
    invocations: list[ToolInvocation] = field(default_factory=list)
    artifacts: list[Any] = field(default_factory=list)

    # --- what the plugins write ----------------------------------------------
    #
    # The loop step currently executing. Incremented by the budget plugin in
    # `before_model_callback`, read by the tools to stamp what they produced.
    # Not a tool ARGUMENT, for the same reason it is not one in the langchain
    # runtime: loop bookkeeping the model could get wrong.
    step: int = 0
    # Steps in which at least one tool actually RAN. Distinct from `step` above,
    # which counts model invocations. See `LoopResult.steps` -- this is the field
    # that makes "tools were on and it chose not to" distinguishable from "tools
    # were off", and `scripts/agent_loop_check.py` cases 1 and 2 are the pair that
    # pins it.
    tool_steps: int = 0
    # Has any search run this turn? The gap trigger's second gate. On a model that
    # self-initiates a search, "no tool calls this step" does NOT imply "never
    # searched", and without this gate every correct refusal earns a redundant
    # forced retrieval.
    corpus_searched: bool = False
    # The gap trigger fires at most once per turn.
    gap_fired: bool = False
    # True only for the duration of the gap-FORCED model call. Read by the budget
    # callback (which narrows the tool list to the single named tool) and by the
    # trigger (which synthesises a call if the model declines anyway). A phase
    # flag rather than a step number, because the forced call is not a step: it is
    # a retry of one, and counting it would credit the model with a decision the
    # code made.
    forcing: bool = False
    # True from the moment the trigger fires until the forced invocation ends.
    # DISTINCT from `forcing`, which is true only for the single forced model
    # CALL: a tool dispatched as a result of that call runs after `forcing` has
    # been cleared, so a tool stamping itself off `forcing` would stamp nothing.
    # This is the flag `app/adk/tools.py` reads to mark a search as gap-triggered.
    gap_phase: bool = False
    # The forced call was declined and the search was dispatched synthetically.
    # Recorded because it is the difference between "the provider honoured a named
    # tool" and "ADK dispatched it for us", and only the second is deterministic.
    gap_synthetic: bool = False
    # "max_steps" | "tool_error" | None
    stopped_reason: str | None = None
    # Summed separately so the trace ADDS UP to the turn rather than overlapping:
    # a tool call's milliseconds must never also be counted as generation.
    tool_ms: int = 0
    generation_ms: int = 0
    # The step whose generation put text on the client's screen. Read only by the
    # gap trigger, to decide whether a retraction has anything to retract.
    streamed_text_step: int | None = None
    # Text of the draft that admitted a gap, carried onto the forced call's
    # `assistant_text` so a trajectory shows the search's motive.
    gap_draft_text: str = ""
    # The marker `detect_gap` matched, for the ANSWER_RESET payload.
    gap_marker: str | None = None

    _tool_ctx: ToolContext | None = field(default=None, repr=False)

    @property
    def tool_ctx(self) -> ToolContext:
        """The langchain `ToolContext` the reused tools close over.

        Built lazily and cached, so `AdkTurnContext` can be constructed in a
        harness (`tenancy_check` T3) without dragging in the tool registry.
        """
        if self._tool_ctx is None:
            from app.tools.registry import ToolContext

            self._tool_ctx = ToolContext(agent=self.agent, ledger=self.ledger)
        # `step` lives on this object and is mirrored onto the registry context
        # before every dispatch, so a tool stamping an artifact reads one number.
        self._tool_ctx.step = self.step
        return self._tool_ctx

    def drain_artifacts(self) -> list[Any]:
        """Artifacts produced this turn, from wherever the tools put them.

        `run_python` appends to the registry context's list because that is the
        object it closes over; the loop reads them from here. One accessor rather
        than two call sites reaching through `._tool_ctx`.
        """
        if self._tool_ctx is None:
            return list(self.artifacts)
        return [*self.artifacts, *self._tool_ctx.artifacts]
