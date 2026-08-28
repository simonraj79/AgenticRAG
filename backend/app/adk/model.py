"""An ADK `BaseLlm` backed by this repo's ONE chat-model chokepoint.

------------------------------------------------------------------
WHY THIS IS NOT `google.adk.models.lite_llm.LiteLlm`.

`LiteLlm` is the documented ADK road to a non-Gemini provider, and taking it
would have been the obvious choice. It was measured against this one
(`new features/18-adk-runtime/PLAN.md` section 2) and it loses on five counts,
three of which fail SILENTLY:

1. **`max_output_tokens` -> `max_completion_tokens`.** `lite_llm` hard-maps the
   name with no flag. CLAUDE.md records that exact spelling as a guaranteed
   `404 No endpoints found that can handle the requested parameters` under
   `provider.require_parameters: true`, because OpenRouter HONOURS it and does
   not ADVERTISE it. A 404 on a working model id.
2. **`stream_options={"include_usage": true}`** is hardcoded after
   `_additional_args` and cannot be removed by any constructor kwarg -- and
   OpenRouter has deprecated that parameter.
3. **`http_options.extra_body` clobbers the constructor `extra_body` by
   assignment**, which would silently delete the `provider` block, i.e. turn
   `require_parameters` off. That is the flag whose whole job is to stop
   OpenRouter dropping `tools` on a tool-less tier and returning prose.
4. **`FunctionCallingConfigMode.ANY` maps to `"required"` and
   `allowed_function_names` is DISCARDED.** CLAUDE.md's T4 measurement is that
   `tool_choice="any"` is silently ignored on this route and *only a NAMED tool
   forces a call* -- so that mapping would delete the gap trigger's mechanism
   while leaving its code in place.
5. **Cost and served-provider are lost**, entirely so on streamed calls, and
   `metering_check.py` case 12 -- which walks the call graph seeded on the
   literal callee name `build_chat_model` -- would print GREEN over a completely
   unmetered runtime. That is this repo's seventh green-suite failure, exactly
   ("goldenset" in CALL_KINDS with no `meter_as`: logged, never written, summing
   to 0.0, reading as a quiet week).

Delegating to `build_chat_model` instead inherits all seven of its measured
OpenRouter facts and its metering attachment **by construction rather than by
care**. Verified live, 14/14, before this file was written: body carries
`max_tokens` in `extra_body`, `provider.require_parameters: true`, `top_k`
correctly ABSENT for the DeepSeek family, `parallel_tool_calls` disabled -- and
two `api_usage` records with real `cost_usd` and `served_provider`, with no
metering code written here.

**Consequence: `litellm` is not a dependency of this change set.**
------------------------------------------------------------------

The class implements exactly one method. `BaseLlm.generate_content_async` is the
whole `BaseLlm` contract, which is why an adapter is small enough to be worth
preferring over a framework-provided one.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncGenerator
from typing import Any

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.config import settings
from app.metering.meter import PROVIDER_KEY
from app.rag.llm import build_chat_model

log = logging.getLogger("uvicorn.error")


# --------------------------------------------------------------------------
# Schema translation
# --------------------------------------------------------------------------


def _lower_types(obj: Any) -> Any:
    """`types.Schema` serialises `"type": "OBJECT"`; JSON Schema wants `"object"`.

    google-genai's enum is uppercase and the OpenAI tool-schema dialect that
    OpenRouter speaks is lowercase. Providers vary in how forgiving they are, and
    a rejected schema surfaces as the model never calling the tool -- no error, an
    agent that has simply gone quiet. Normalising costs one walk per tool per
    call and removes an entire class of silent failure.
    """
    if isinstance(obj, dict):
        return {
            key: (value.lower() if key == "type" and isinstance(value, str) else _lower_types(value))
            for key, value in obj.items()
        }
    if isinstance(obj, list):
        return [_lower_types(item) for item in obj]
    return obj


def declaration_to_openai(declaration: types.FunctionDeclaration) -> dict[str, Any]:
    """One ADK function declaration as the OpenAI tool schema `bind_tools` takes.

    Public because `scripts/adk_model_check.py` asserts on its output: the
    declaration is the tenancy boundary, and the assertion has to be made against
    what is actually SENT rather than against the Python object, which is exactly
    what looks correct.
    """
    parameters: dict[str, Any] = {}
    if declaration.parameters is not None:
        parameters = _lower_types(
            declaration.parameters.model_dump(exclude_none=True, mode="json")
        )
    return {
        "type": "function",
        "function": {
            "name": declaration.name,
            "description": declaration.description or "",
            "parameters": parameters,
        },
    }


# --------------------------------------------------------------------------
# Message translation
# --------------------------------------------------------------------------


def _system_text(config: Any) -> str:
    """The system instruction, however google-genai shaped it.

    It can be a bare `str`, a single `Content`, or a list of them, depending on
    what ADK's instruction provider returned. Handling one shape and hoping is how
    a persona prompt silently stops reaching the model -- and a persona that never
    arrives produces a fluent, ungrounded answer rather than an error.
    """
    instruction = getattr(config, "system_instruction", None)
    if not instruction:
        return ""
    if isinstance(instruction, str):
        return instruction
    contents = [instruction] if isinstance(instruction, types.Content) else list(instruction)
    pieces: list[str] = []
    for content in contents:
        if isinstance(content, str):
            pieces.append(content)
            continue
        for part in content.parts or []:
            if part.text:
                pieces.append(part.text)
    return "\n".join(pieces)


def request_to_messages(request: LlmRequest) -> list[Any]:
    """ADK's `contents` as a langchain message list.

    The mapping that matters is `function_response` -> `ToolMessage`, keyed on
    the function-call **id**. ADK puts function responses in a `user`-role
    `Content`, so a translator that dispatched on role alone would hand the model
    a human turn containing raw JSON -- the model would answer it as a question
    rather than read it as a result, and nothing would raise.
    """
    messages: list[Any] = []

    system = _system_text(request.config) if request.config else ""
    if system:
        # `SystemMessage(content=...)` rather than a `("system", ...)` tuple, for
        # the reason `pipeline.answer_question` gives: a brace in user-editable
        # persona text must not be read as a template variable.
        messages.append(SystemMessage(content=system))

    for content in request.contents or []:
        parts = content.parts or []
        texts = [p.text for p in parts if p.text]
        calls = [p.function_call for p in parts if p.function_call]
        responses = [p.function_response for p in parts if p.function_response]

        if responses:
            for response in responses:
                payload = response.response or {}
                # The tools return `{"result": "<the string the model reads>"}`.
                # Unwrapping it here means the model reads the numbered chunk
                # block exactly as it does under the langchain runtime, rather
                # than a JSON object wrapping it -- which would change the input
                # the model reasons over while every assertion stayed green.
                if isinstance(payload, dict) and set(payload) == {"result"}:
                    body = str(payload["result"])
                else:
                    body = json.dumps(payload, ensure_ascii=False, default=str)
                messages.append(
                    ToolMessage(content=body, tool_call_id=response.id or response.name or "call")
                )
            continue

        if content.role == "user":
            messages.append(HumanMessage(content="\n".join(texts)))
            continue

        messages.append(
            AIMessage(
                content="\n".join(texts),
                tool_calls=[
                    {
                        "name": call.name,
                        "args": dict(call.args or {}),
                        "id": call.id or call.name or "call",
                    }
                    for call in calls
                ],
            )
        )

    return messages


def _tool_choice(config: Any) -> str | None:
    """ADK's `tool_config` as langchain's `tool_choice`.

    **`ANY` + `allowed_function_names` becomes the NAMED tool, not `"required"`.**
    That single line is why this adapter exists rather than `LiteLlm`, which drops
    the names. CLAUDE.md's measurement: on this route `tool_choice="any"` is
    accepted and silently ignored, and only a named tool actually forces a call --
    so a dropped name is indistinguishable from a model that chose not to call.

    `NONE` becomes `"none"`, and the caller keeps `config.tools` POPULATED. The
    langchain runtime's forced final answer binds tools with `tool_choice="none"`
    deliberately, because `tools` is a parameter OpenRouter routes on: dropping it
    mid-turn changes the routing constraint and risks a different provider
    answering than the one that made the calls.
    """
    tool_config = getattr(config, "tool_config", None)
    fcc = getattr(tool_config, "function_calling_config", None) if tool_config else None
    if fcc is None or fcc.mode is None:
        return None
    if fcc.mode == types.FunctionCallingConfigMode.NONE:
        return "none"
    if fcc.mode == types.FunctionCallingConfigMode.ANY:
        names = list(fcc.allowed_function_names or [])
        if len(names) == 1:
            return names[0]
        # More than one allowed name has no faithful langchain spelling and
        # `"any"` is ignored on this route, so forcing would be a line that does
        # nothing and reports success -- `loop.md` T2. Say so and fall back to
        # letting the model choose, which is at least an honest state.
        log.warning(
            "ADK requested mode=ANY over %d tools; only a NAMED tool forces a "
            "call on this route, so leaving tool choice to the model.",
            len(names),
        )
        return None
    return None


# --------------------------------------------------------------------------
# The model
# --------------------------------------------------------------------------


class OpenRouterAdkLlm(BaseLlm):
    """ADK's model interface, satisfied by `app.rag.llm.build_chat_model`.

    Sampling parameters are fields rather than read from `settings` at call time
    so the eval driver and the harnesses can build one deterministically, exactly
    as `pipeline.get_chat_model` does for the langchain runtime.
    """

    model: str = settings.generation_model
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    max_tokens: int | None = None
    reasoning: bool | None = None

    # ACCUMULATED across every call this model makes, so `app/adk/loop.py` can
    # attribute generation time by snapshotting before and after an invocation.
    #
    # **A per-call `last_call_ms` was the first design and it was wrong**: an ADK
    # invocation makes several model calls, so the last one's duration is not the
    # invocation's generation time -- it is the tail of it, and `generation_ms`
    # would have under-reported by however many steps ran. The trace's arithmetic
    # is supposed to ADD UP to the turn (`contextualize + retrieval + tool +
    # generation`), so a silently-low number here shows up as a turn whose parts
    # do not sum, with nothing raising.
    total_call_ms: int = 0

    def _chat(self):
        """The configured chat model. **The only construction site in this file.**

        Everything OpenRouter-specific -- `provider.require_parameters`, the
        `extra_body` route for `max_tokens`, the per-family `top_k` and
        `reasoning` decisions, `disabled_params`, the timeout, the attribution
        headers, and the metering callback -- lives inside `build_chat_model` and
        is inherited here. Restating any of it would create the second copy that
        drifts.
        """
        return build_chat_model(
            self.model,
            temperature=self.temperature,
            top_p=self.top_p,
            top_k=self.top_k,
            max_tokens=self.max_tokens,
            reasoning=self.reasoning,
        )

    def _runnable(self, request: LlmRequest):
        """The chat model with this request's tools and tool choice bound."""
        chat = self._chat()
        config = request.config
        declarations = [
            declaration
            for tool in (getattr(config, "tools", None) or [])
            for declaration in (getattr(tool, "function_declarations", None) or [])
        ]
        if not declarations:
            return chat
        choice = _tool_choice(config)
        schemas = [declaration_to_openai(d) for d in declarations]
        if choice is None:
            return chat.bind_tools(schemas)
        return chat.bind_tools(schemas, tool_choice=choice)

    @staticmethod
    def _to_response(message: AIMessage, *, partial: bool = False) -> LlmResponse:
        """One langchain `AIMessage` as an ADK `LlmResponse`.

        `usage_metadata` is populated even though nothing in this project reads
        it, because ADK's own telemetry does: without it the runner logs
        *"Skipping missing token usage metadata"* on every call and ADK's
        observability goes dark while everything still appears to work. Observed
        in the spike, which is the only reason it is here.

        `custom_metadata` carries cost and served provider. They are ALSO recorded
        through the `UsageMeter` callback that `build_chat_model` attaches -- this
        is a read-only echo for anything inspecting the event stream, never the
        metering path itself.
        """
        parts: list[types.Part] = []
        text = message.content if isinstance(message.content, str) else str(message.content or "")
        if text:
            parts.append(types.Part(text=text))
        for call in getattr(message, "tool_calls", None) or []:
            parts.append(
                types.Part(
                    function_call=types.FunctionCall(
                        id=call.get("id"),
                        name=call["name"],
                        args=call.get("args") or {},
                    )
                )
            )

        meta = getattr(message, "response_metadata", None) or {}
        usage = getattr(message, "usage_metadata", None) or {}

        return LlmResponse(
            content=types.Content(role="model", parts=parts or [types.Part(text="")]),
            partial=partial,
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=usage.get("input_tokens") or 0,
                candidates_token_count=usage.get("output_tokens") or 0,
                total_token_count=usage.get("total_tokens") or 0,
            ),
            custom_metadata={
                "served_provider": meta.get(PROVIDER_KEY),
                "cost_usd": (meta.get("token_usage") or {}).get("cost"),
                "finish_reason": meta.get("finish_reason"),
            },
        )

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        """The whole `BaseLlm` contract.

        Streaming yields `partial=True` fragments and then ONE `partial=False`
        aggregate carrying the full text -- the shape ADK's runner expects, and
        the shape every plugin in this package gates on. Emitting the aggregate as
        tokens as well would render the answer twice; `scripts/adk_stream_check.py`
        S8 is that assertion.
        """
        runnable = self._runnable(llm_request)
        messages = request_to_messages(llm_request)
        started = time.perf_counter()

        if not stream:
            message = await runnable.ainvoke(messages)
            self.total_call_ms += int((time.perf_counter() - started) * 1000)
            yield self._to_response(message)
            return

        accumulated: Any = None
        async for chunk in runnable.astream(messages):
            accumulated = chunk if accumulated is None else accumulated + chunk
            piece = getattr(chunk, "text", "") or ""
            if piece:
                # A fragment carries ONLY the delta. The plugins that must not act
                # on a fragment gate on `llm_response.partial`; the text guard
                # deliberately does not, because a two-character sentinel splits
                # across a chunk boundary and a per-fragment matcher would miss it
                # on a schedule nobody can reproduce.
                yield LlmResponse(
                    content=types.Content(role="model", parts=[types.Part(text=piece)]),
                    partial=True,
                )

        self.total_call_ms += int((time.perf_counter() - started) * 1000)
        if accumulated is None:
            # A stream that yielded nothing at all. The same handling the
            # langchain runtime gives it: an empty message, so the loop takes its
            # normal-exit branch rather than raising.
            log.warning("Streamed ADK model call yielded no chunks")
            yield self._to_response(AIMessage(content=""))
            return
        yield self._to_response(accumulated)


def build_adk_model(
    model: str | None = None,
    *,
    temperature: float | None = None,
    top_p: float | None = None,
    top_k: int | None = None,
    max_tokens: int | None = None,
    reasoning: bool | None = None,
) -> OpenRouterAdkLlm:
    """The only place an ADK model is constructed.

    A named builder rather than calling the constructor at each site, so
    `metering_check.py` case 12's `ast` walk has a single literal callee name to
    seed on -- the same property that makes `build_chat_model` the chokepoint it
    checks today. **This function must reach `build_chat_model`**, and it does so
    transitively through `OpenRouterAdkLlm._chat`; case 12b asserts the seed set
    contains both names so a future refactor that severs the link goes red.
    """
    return OpenRouterAdkLlm(
        model=model or settings.generation_model,
        temperature=settings.generation_temperature if temperature is None else temperature,
        top_p=settings.generation_top_p if top_p is None else top_p,
        top_k=settings.generation_top_k if top_k is None else top_k,
        max_tokens=settings.generation_max_tokens if max_tokens is None else max_tokens,
        reasoning=settings.generation_reasoning if reasoning is None else reasoning,
    )
