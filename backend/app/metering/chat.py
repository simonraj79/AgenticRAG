"""`ChatOpenAI`, minus one lossy normalisation. It changes PARSING, never the request.

**Read this first: nothing in this file adds a field to the request body.** That
is not a stylistic claim, it is the safety property. CLAUDE.md records four
distinct ways an OpenRouter parameter can be wrong -- unadvertised and 404s at
routing (`max_completion_tokens`), unadvertised and works anyway (`stream`),
advertised then rejected at execution (`reasoning`), and injected by the client
without being asked for (`encoding_format`) -- plus the measurement that
`parallel_tool_calls` alone would collapse routing from 28 endpoints to 1. A
subclass that touched `extra_body` would re-open all of it. `llm_check.py` case
31 asserts the request body is byte-identical with this class in place.

WHAT IT RECOVERS, and why the recovery is needed at all. OpenRouter sends a final
SSE frame with no `choices` and a `usage` object carrying the real cost, the
serving provider and the generation id:

    {"id": "gen-1787192108-...", "provider": "Relace", "choices": [],
     "usage": {"prompt_tokens": 9, "completion_tokens": 2, "cost": 9.1e-07, ...}}

`_create_usage_metadata` (langchain_openai/chat_models/base.py:4175) maps that
onto the OpenAI-standard `UsageMetadata` shape and discards `cost`,
`cost_details`, `provider` and `id`. Tokens survive; money does not. This class
keeps the raw frame beside the normalised one.

**And it removes the reason `stream_usage` was ever considered.**
`app/rag/agent_loop.py` declines `ChatOpenAI(stream_usage=True)` because it
injects an unprobed `stream_options` key. That caution stands and is now moot
twice over: base.py:1417 reads `chunk.get("usage")` UNCONDITIONALLY -- the flag
gates the SEND, never the PARSE -- and OpenRouter has deprecated
`stream_options: {include_usage: true}` outright, returning usage on every
response with no parameter at all. Measured 2026-08-20: `stream_usage=True`
produced byte-identical usage to leaving it off. Do not set it.
"""

from __future__ import annotations

from typing import Any

import openai
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from langchain_openai import ChatOpenAI

from app.metering.meter import PROVIDER_KEY, USAGE_KEY


class MeteredChatOpenAI(ChatOpenAI):
    """`ChatOpenAI` that keeps what OpenRouter sends and langchain drops.

    Two overrides, one per path, because the two paths lose different things:

        streaming      loses `cost`, `provider` and the `gen-` id
        non-streaming  keeps cost and id in `llm_output`, loses `provider`

    The second was found only by writing a row to the database and reading it
    back -- every offline case passed with `served_provider` null, because none
    of them asked. That is the harness-shaped half of the same lesson this
    project keeps relearning: a case added after the code is a case written to
    pass, and a case that never asks a question cannot fail it.
    """

    def _convert_chunk_to_generation_chunk(
        self,
        chunk: dict,
        default_chunk_class: type,
        base_generation_info: dict | None,
    ) -> ChatGenerationChunk | None:
        generation_chunk = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info
        )

        usage: Any = chunk.get("usage") if isinstance(chunk, dict) else None
        if generation_chunk is None or not usage:
            return generation_chunk

        # **A LIST, and this is the load-bearing line of the file.**
        #
        # Streaming merges `generation_info` across every chunk with
        # `langchain_core.utils._merge.merge_dicts`, which concatenates strings,
        # concatenates lists, recurses into dicts -- and RAISES TypeError on two
        # unequal scalars. Measured on this route: `finish_reason` merges to
        # "stopstop" and `model_name` to a doubled slug, so the merge is not
        # hypothetical, it happens on every stream.
        #
        # A bare `generation_info["cost"] = 9.1e-07` therefore works on every
        # model measured today and kills a LIVE TURN the first time any provider
        # emits two usage frames with different values. A list concatenates, so
        # two frames become two entries and the meter takes the last.
        #
        # `scripts/metering_check.py` case 2 is that provider, simulated. It
        # asserts a list of 2 rather than asserting nothing threw -- loop.md T2.
        record = {
            "usage": usage,
            "provider": chunk.get("provider"),
            "id": chunk.get("id"),
        }
        info = dict(generation_chunk.generation_info or {})
        info[USAGE_KEY] = [record]
        generation_chunk.generation_info = info
        return generation_chunk

    def _create_chat_result(
        self,
        response: dict | openai.BaseModel,
        generation_info: dict | None = None,
    ) -> ChatResult:
        """The non-streamed path. Recovers the one field its `llm_output` drops.

        `llm_output` is built from a FIXED dict literal (base.py:1873) -- token
        usage, model name, system fingerprint, id -- and OpenRouter's top-level
        `provider` is not in it. `model_provider` is the decoy:
        `scripts/route_check.py` already documents it as the hard-coded string
        `"openai"`, which is true of the protocol and says nothing about who
        served the call.

        Without this, `served_provider` was null on every non-streamed row --
        i.e. on the rewrite, the router, the critic, the handout coder, the
        golden-set drafter and the Ragas judge, which is most of the table. It
        was the streamed path, the one that needed a subclass at all, that
        happened to work.

        Why the column is worth an override: two identical requests have been
        measured here costing 2.002e-05 and 5.684e-06 because they landed on
        different endpoints. Without the provider name a 3.5x cost swing on
        unchanged traffic is unexplainable, and the natural next move -- pinning
        a provider -- is a documented NO_GO on this account.
        """
        result = super()._create_chat_result(response, generation_info)

        if isinstance(response, openai.BaseModel):
            served = getattr(response, "provider", None)
        elif isinstance(response, dict):
            served = response.get("provider")
        else:
            served = None

        if served:
            # `llm_output` is a plain dict here and nothing merges it across
            # calls, so a scalar is safe -- unlike `generation_info` above, where
            # streaming's merge is what forces the list.
            result.llm_output = {**(result.llm_output or {}), PROVIDER_KEY: served}
        return result
