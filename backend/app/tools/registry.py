"""What the model is handed, and what it may close over.

Two tools, built per turn because both close over turn-scoped state: the agent
whose corpus may be searched, and the ledger that owns this turn's `[n]`
numbering. Neither is a tool *argument*, and that is a security property rather
than a convenience.

**PRD section 7: "the namespace comes from the session, never from the request
body."** A model that can be prompt-injected by a retrieved document must not
have a parameter through which another tenant's corpus could be named. Closing
over the `Agent` object -- the same object `retriever.get_vector_store` derives
`agent.namespace` from -- makes that structural: there is no argument anywhere in
`SearchCorpusArgs` through which a namespace could arrive, because the schema has
exactly one field and it is the query string.

The two shared types live here rather than in `agent_loop` for the same reason:
`ToolContext` is what the tools write into, and a type that travels with the
container it is stored in cannot drift away from it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from langchain_core.tools import BaseTool

from app.db.models import Agent
from app.tools.corpus import build_corpus_tool
from app.tools.interpreter import build_python_tool
from app.tools.sandbox import SandboxArtifact

if TYPE_CHECKING:  # pragma: no cover - import cycle, types only
    # `agent_loop` imports this module at runtime for `build_tools`; importing it
    # back at module scope would deadlock. `from __future__ import annotations`
    # makes every annotation below a string, so the type is never needed at run
    # time.
    from app.rag.agent_loop import ContextLedger


@dataclass
class ToolArtifact:
    """A sandbox artifact, plus everything the Handout row needs.

    `SandboxArtifact` is frozen and deliberately knows nothing about handouts --
    it is the sandbox's output type and its three fields are the three facts the
    sandbox can establish. Title, source code and step number are facts about the
    *call*, not about the file, so they are carried alongside rather than pushed
    into a frozen dataclass that would then have to be constructed differently
    depending on who was calling it.

    The delegating properties exist so `ask.run_turn` can write a `handouts` row
    from this object alone, without reaching through `.artifact.` for half its
    columns and through the wrapper for the other half.
    """

    artifact: SandboxArtifact
    # `RunPythonArgs.purpose`, which becomes `handouts.title`. That is why the
    # tool asks for it: a model that has to state the goal writes to it, and the
    # Handouts panel needs a name that is not `chart.png`.
    title: str
    # The Python that produced it, stored so the panel can show its own working.
    source_code: str
    # Which loop step produced it. Lines the artifact up with its TOOL_CALL row.
    step: int

    @property
    def filename(self) -> str:
        return self.artifact.filename

    @property
    def mime_type(self) -> str:
        return self.artifact.mime_type

    @property
    def content(self) -> bytes:
        return self.artifact.content

    @property
    def byte_size(self) -> int:
        return self.artifact.byte_size


@dataclass(frozen=True)
class ToolOutcome:
    """What the loop needs to know about a call, which is not what the model does.

    Every tool returns a `(payload_for_model, ToolOutcome)` pair through
    langchain's `response_format="content_and_artifact"`, so the string goes into
    the `ToolMessage` and this rides on `ToolMessage.artifact`. Two channels, one
    return value, no side-channel on the context object.

    The split matters most for failure. `run_python` failing is a normal return
    carrying a traceback the model is *meant* to read and correct from -- so the
    payload must reach the model unchanged, while `ok=False` still has to reach
    the trace as a `TOOL_ERROR` row. Encoding failure in the payload string would
    make the trace parse English.
    """

    ok: bool
    # One line, safe to render. Becomes `ToolInvocation.summary`.
    summary: str
    # Spread into the TOOL_RESULT payload. Per feature 1 section 5:
    #   search_corpus -> {returned, new_chunks, top_score, markers}
    #   run_python    -> {artifacts: [...], stdout_chars, exit_code}
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class ToolContext:
    """Turn-scoped state the tools close over. One per `run_agent_loop` call."""

    agent: Agent
    ledger: ContextLedger
    # Appended to by `run_python`, drained by the loop into `LoopResult`, and
    # persisted as Handout rows by `ask.run_turn`. A list rather than a return
    # value because a turn can produce artifacts across several steps.
    artifacts: list[ToolArtifact] = field(default_factory=list)
    # The loop step currently executing. Written by `run_agent_loop` before it
    # executes a step's calls; read by a tool that needs to stamp what it
    # produced. A closure has no other way to know which step it is in, and
    # threading a step argument through the tool schema would expose loop
    # bookkeeping to the model as a parameter it could get wrong.
    step: int = 0


def build_tools(ctx: ToolContext) -> list[BaseTool]:
    """The tools, in a stable order: search first, then python.

    The order is not cosmetic. Some models weight the first tool in the list more
    heavily, and search is both the cheaper call (~1.6 s with reranking against a
    subprocess spawn plus a matplotlib import) and the one that more often
    answers the question. Being biased toward it is the bias worth having.
    """
    return [build_corpus_tool(ctx), build_python_tool(ctx)]
