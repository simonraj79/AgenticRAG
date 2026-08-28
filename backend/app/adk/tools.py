"""What the model is handed under the ADK runtime, and what it may NOT name.

------------------------------------------------------------------
THE TENANCY BOUNDARY IS THE REASON THIS FILE SUBCLASSES `BaseTool`.

ADK's ergonomic path is `FunctionTool(func)`, which derives the schema from the
CALLABLE'S SIGNATURE. That pushes hard toward

    def search_corpus(query: str, agent_id: str) -> dict: ...

which is idiomatic ADK and a tenancy hole -- PRD section 7 is explicit that "the
namespace comes from the session, never from the request body". And it is the
worst possible hole to introduce, because a CORRECT `agent_id` produces a correct
answer: retrieval works, citations resolve, `agentic_check` passes, the scorecard
renders. The defect only appears when a prompt-injected document names a
different tenant, which no test does.

So the declaration is written BY HAND. There is no signature to introspect, no
pydantic model to widen, and no code path through which the closed-over `Agent`
could reach the schema. `scripts/tenancy_check.py` T3/T4 assert that against the
SERIALISED declaration -- the bytes a provider would receive -- because the Python
object is exactly what looks correct.

Two ADK-specific amplifiers this also sidesteps: `FunctionTool._get_declaration`
and `find_context_parameter` are module-level `lru_cache`s keyed on the function
object, so a per-request closure would be retained along with everything it closes
over; and `AgentTool` copies the whole parent session state into a child agent, so
`tool_context.state` is not a boundary either (banned by `tenancy_check` T6).
------------------------------------------------------------------

THE BODIES ARE NOT REIMPLEMENTED. Each tool wraps the EXISTING langchain tool
built by `app/tools/registry.build_tools`, invoked as a `ToolCall` so the
`content_and_artifact` pair comes back intact. The retrieval policy, the ledger
merge, the sandbox spawn and the `ToolOutcome` shape are therefore *the same
objects* under both runtimes -- and a second implementation of `_search` would be
a second place for the namespace to leak.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext as AdkToolContext
from google.genai import types

from app.adk.context import AdkTurnContext
from app.config import settings
from app.tools.corpus import SEARCH_CORPUS, TOOL_DESCRIPTION as CORPUS_DESCRIPTION

log = logging.getLogger("uvicorn.error")

RUN_PYTHON = "run_python"

# **Sequential tool dispatch, made a property of the TOOL rather than of the loop.**
#
# The langchain runtime dispatches a step's calls one at a time (agent_loop.py),
# deliberately: `run_python` spawns a subprocess and Render's starter plan runs a
# single uvicorn worker, so two at once is two interpreters competing for one core.
# ADK's runner may dispatch parallel `function_call` parts concurrently.
#
# Putting the guard here means sequentiality holds regardless of what ADK's
# dispatcher does today AND if it changes -- which is the difference between a
# property and a coincidence. It also closes a real race that exists in BOTH
# runtimes and has never had a case: `ContextLedger._absorb` reads `len(entries)`
# and then appends, so two concurrent merges hand two chunks the same `[n]`. The
# answer renders, the citation chips resolve, and they point at the wrong sources.
_DISPATCH_LOCK = asyncio.Lock()


def _clip(text: str) -> str:
    """What the model READ, bounded by the setting rather than by a literal.

    `ToolInvocation.content` is the string the model actually saw, and it feeds
    the trajectory a judge reads. Clipping to a hardcoded number here would make
    the eval's input silently different from the operator's configured one.
    """
    limit = settings.trajectory_max_tool_content_chars
    if limit and len(text) > limit:
        return text[:limit]
    return text


class _WrappedLangchainTool(BaseTool):
    """An ADK tool whose body is the langchain tool of the same name.

    `_get_declaration` is abstract here on purpose: every subclass writes its
    schema out literally, so adding a tool means writing a declaration rather than
    inheriting one that introspects something.
    """

    def __init__(self, *, name: str, description: str, ctx: AdkTurnContext) -> None:
        super().__init__(name=name, description=description)
        # Private attributes, set through `object.__setattr__` because ADK's
        # `BaseTool` is a plain class but pydantic-adjacent bases have bitten this
        # repo before (`CohereRerank` and `OpenAIEmbeddings` both forbid extras).
        object.__setattr__(self, "_ctx", ctx)
        object.__setattr__(self, "_lc_tool", None)

    @property
    def ctx(self) -> AdkTurnContext:
        return getattr(self, "_ctx")

    def _langchain_tool(self):
        """The langchain tool, built once per turn and cached.

        Built lazily rather than in `__init__` so constructing the tool list --
        which `tenancy_check` T3 does with a `SimpleNamespace` agent and no ledger
        -- never touches the retriever.
        """
        cached = getattr(self, "_lc_tool")
        if cached is None:
            from app.tools.registry import build_tools

            by_name = {t.name: t for t in build_tools(self.ctx.tool_ctx)}
            object.__setattr__(self, "_lc_tool", by_name)
            cached = by_name
        return cached[self.name]

    async def run_async(self, *, args: dict[str, Any], tool_context: AdkToolContext) -> Any:
        """Execute, record a `ToolInvocation`, and return what the model reads.

        **Never raises.** A tool that raises out of `run_async` aborts the whole
        ADK invocation, and this repo's most valuable code-interpreter behaviour is
        a model reading its own traceback and correcting -- which requires the
        failure to arrive as a normal return the model can read. The langchain
        tools already guarantee this (`sandbox.run` never raises); the try/except
        is the backstop for the wrapper itself, not a substitute for that.
        """
        from app.rag.agent_loop import ToolInvocation

        ctx = self.ctx
        call_id = getattr(tool_context, "function_call_id", None) or f"{self.name}_{ctx.step}"
        started = time.perf_counter()

        async with _DISPATCH_LOCK:
            # `step` is mirrored onto the registry context here, immediately
            # before dispatch, so a tool stamping an artifact reads the same
            # number the trace row will carry.
            ctx.tool_ctx.step = ctx.step
            try:
                message = await self._langchain_tool().ainvoke(
                    {"name": self.name, "args": dict(args), "id": call_id, "type": "tool_call"}
                )
                payload = str(message.content or "")
                outcome = getattr(message, "artifact", None)
            except Exception as exc:  # noqa: BLE001 -- see the docstring
                log.exception("ADK tool %s raised", self.name)
                payload = f"{type(exc).__name__}: {exc}"
                outcome = None

        duration_ms = int((time.perf_counter() - started) * 1000)
        ok = bool(getattr(outcome, "ok", False))
        summary = str(getattr(outcome, "summary", "") or payload[:120])
        detail = dict(getattr(outcome, "detail", None) or {})
        error = getattr(outcome, "error", None)
        if outcome is None:
            ok, error = False, payload

        # **Stamped HERE, at creation, not patched on afterwards.** The loop used
        # to walk the invocation list after the forced pass and mark whichever
        # entries looked recent; that is a heuristic over a list, and it marked the
        # wrong rows the moment a turn had a model-chosen search too. A tool that
        # runs during the gap phase knows it, so it says so.
        stamped_args = dict(args)
        stamped_detail = dict(detail)
        assistant_text = ""
        if ctx.gap_phase and self.name == SEARCH_CORPUS:
            stamped_args["trigger"] = "gap_detected"
            stamped_detail["trigger"] = "gap_detected"
            # The retracted draft, not the forced call's own preamble: this is the
            # text that ADMITTED the gap and is the reason the search happened at
            # all. A trajectory that dropped it would show a search with no motive.
            assistant_text = ctx.gap_draft_text

        ctx.invocations.append(
            ToolInvocation(
                step=ctx.step,
                call_id=call_id,
                tool=self.name,
                args=stamped_args,
                ok=ok,
                summary=summary,
                detail=stamped_detail,
                duration_ms=duration_ms,
                error=error,
                content=_clip(payload),
                assistant_text=assistant_text,
            )
        )
        ctx.tool_ms += duration_ms
        if self.name == SEARCH_CORPUS and ok:
            # The gap trigger's second gate. Set for a MODEL-CHOSEN search and for
            # a forced one alike -- the outcome the trigger wants is "the model
            # searched before it declined", and a search having run means that
            # outcome has occurred however it was caused.
            ctx.corpus_searched = True

        # A dict with one key, unwrapped again by `request_to_messages`, so the
        # model reads the numbered chunk block exactly as it does under the
        # langchain runtime rather than a JSON object wrapping it.
        return {"result": payload}


class SearchCorpusTool(_WrappedLangchainTool):
    """`search_corpus`, with a schema of exactly one field."""

    def __init__(self, ctx: AdkTurnContext) -> None:
        super().__init__(name=SEARCH_CORPUS, description=CORPUS_DESCRIPTION, ctx=ctx)

    def _get_declaration(self) -> types.FunctionDeclaration:
        # WRITTEN OUT, not derived. See the module docstring: there is deliberately
        # no object here that could be widened by adding a field somewhere else.
        return types.FunctionDeclaration(
            name=self.name,
            description=self.description,
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "query": types.Schema(
                        type=types.Type.STRING,
                        description=(
                            "What to look up. Search for the specific missing "
                            "thing, not the whole question again -- the context "
                            "above already holds the first search's results."
                        ),
                    )
                },
                required=["query"],
            ),
        )


class RunPythonTool(_WrappedLangchainTool):
    """`run_python`, with the same two fields the langchain schema has."""

    def __init__(self, ctx: AdkTurnContext) -> None:
        from app.tools.interpreter import TOOL_DESCRIPTION as PY_DESCRIPTION

        super().__init__(name=RUN_PYTHON, description=PY_DESCRIPTION, ctx=ctx)

    def _get_declaration(self) -> types.FunctionDeclaration:
        return types.FunctionDeclaration(
            name=self.name,
            description=self.description,
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "code": types.Schema(
                        type=types.Type.STRING,
                        description=(
                            "Python source. Write files to the current directory. "
                            "You have no network and no filesystem outside it."
                        ),
                    ),
                    "purpose": types.Schema(
                        type=types.Type.STRING,
                        description="One line: what this produces and why.",
                    ),
                },
                required=["code", "purpose"],
            ),
        )


def build_adk_tools(ctx: AdkTurnContext) -> list[BaseTool]:
    """The tools, in a stable order: search first, then python.

    The order is not cosmetic and is asserted (`adk_model_check` A12) rather than
    left to chance: some models weight the first tool in the list more heavily,
    and search is both the cheaper call -- roughly 1.6 s with reranking, against a
    subprocess spawn plus a matplotlib import -- and the one that more often
    answers the question. Being biased toward it is the bias worth having.

    Never sorted. A `sorted()` here would put `run_python` first and nothing would
    fail.
    """
    return [SearchCorpusTool(ctx), RunPythonTool(ctx)]
