"""Every chat model in this project is constructed here, and nowhere else.

The same argument as `app/rag/retriever.py`: there were four construction sites
for `ChatGoogleGenerativeAI` (generation, the rewriter, the golden-set generator,
the Ragas judge), and moving providers meant finding all four and getting the
same four decisions right in each. One seam makes a provider change a one-file
change, exactly as the retriever seam makes the Stage 1 -> Stage 2 change a
one-liner.

------------------------------------------------------------------
WHY OPENROUTER, AND WHAT DELIBERATELY DID NOT MOVE.

Chat goes through OpenRouter. **Embeddings do not, and must not.**
`app/rag/retriever.py` still calls Google directly for `gemini-embedding-2` at
768d, because CLAUDE.md's rule stands: the embedding model is part of the index.
Every vector in Pinecone was written in that space, and matching dimensions do
not imply a shared space -- querying an index built with one embedder using
another returns confident nonsense rather than an error. OpenRouter is a
chat-completions gateway and does not serve that model at all, so "move
everything to OpenRouter" would have meant a full re-ingest to gain nothing.

The practical consequence: **`GEMINI_API_KEY` is still required.** It stops
being the generation key and becomes the embedding key. `OPENROUTER_API_KEY`
does not replace it, and Ragas -- which needs a judge LLM *and* an embedding
model -- now draws its two halves from two different providers.
------------------------------------------------------------------

THREE THINGS HERE WERE ESTABLISHED FROM THE LIVE CATALOGUE, NOT ASSUMED.

1. **OpenRouter silently DROPS parameters the chosen provider does not
   support**, and that default is the trap. Read from the live endpoint list on
   2026-08-15, `google/gemma-4-31b-it` is served by 18 endpoints whose
   capabilities differ: DeepInfra's `turbo` tier (the second-cheapest, and among
   the fastest) lists no `tools`/`tool_choice`, and neither does one of the two
   Together tiers. `with_structured_output(method="function_calling")` sends
   exactly those two fields. Routed to such a provider the request does not
   fail -- the fields are removed and the model answers in prose, so the caller
   gets a parse failure or a `None` for a value it is about to branch on. That is
   the same failure SHAPE CLAUDE.md records for a Gemma markdown fence, arriving
   from a completely different direction.

   `{"provider": {"require_parameters": True}}` converts the silent drop into a
   routing constraint: only providers supporting every parameter in the request
   are eligible. It is on by default and `openrouter_require_parameters` turns
   it off, because the failure it produces when nothing is eligible ("no allowed
   providers") needs an escape hatch that is not a code edit.

2. **`top_k` is not an OpenAI-API parameter, so `ChatOpenAI` has no field for
   it.** It is a real OpenRouter extension and most Gemma providers honour it --
   but it can only travel in `extra_body`. This is load-bearing rather than
   cosmetic: CLAUDE.md is emphatic that Gemma 4's card specifies
   `temperature=1.0, top_p=0.95, top_k=64` as one standardized configuration,
   and dropping `top_k` while keeping the other two is not "close enough", it is
   running the model outside the config it was calibrated for. Combined with
   point 1, a provider that cannot do `top_k` is now routed around rather than
   quietly given a two-thirds version of the sampling config.

3. **No new dependency was added.** `langchain-openai` was already installed --
   CLAUDE.md notes it as dead weight dragged in by `langchain-pinecone`, with
   the aside that nothing calls OpenAI and no `OPENAI_API_KEY` is needed. That
   is still true: `ChatOpenAI` here is an OpenAI-*protocol* client pointed at
   `openrouter.ai`, authenticated with `OPENROUTER_API_KEY`. The package finally
   earns its place.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_openai import ChatOpenAI

from app.config import settings

log = logging.getLogger("uvicorn.error")

# Bare model ids from the direct-Gemini era, mapped to their OpenRouter slugs.
#
# Kept because `agents.generation_model` is a free-text column an operator can
# type into, and a bare `gemma-4-31b-it` sent to OpenRouter returns a 404 naming
# a model that plainly does exist -- an error that reads like an outage rather
# than like a namespace. Every stored value was NULL when this landed (checked),
# so this is a guard against re-entry, not a data migration.
_LEGACY_SLUGS = {
    "gemma-4-31b-it": "google/gemma-4-31b-it",
    "gemini-flash-latest": "google/gemini-3.7-flash",
    "gemini-3.7-flash": "google/gemini-3.7-flash",
    "gemini-flash-lite-latest": "google/gemini-3.7-flash-lite",
}

# Model families that must not be sent `top_k`.
#
# ONE RULE, TWO DIFFERENT CONSEQUENCES, and the second is the reason this comment
# is longer than the tuple. The rule: `top_k` is a Gemma-card parameter that every
# caller in this project inherited from the generation defaults, including callers
# pointed at a non-Gemma model. It matters here only because Gemma's card gives it
# as part of ONE standardized sampling config (see `get_chat_model`), and a model
# outside that family has no such config to honour. So dropping it is right rather
# than merely convenient.
#
# `google/gemini-` -- FAILS LOUDLY. No Gemini endpoint advertises `top_k`, so
# under `require_parameters` there is no eligible provider and the call dies as a
# 404 naming neither the model nor the parameter. That is how the golden-set
# generator broke the first time it was pointed at Gemini 3.7 Flash.
#
# `deepseek/` -- FAILS SILENTLY, WHICH IS WORSE. Measured 2026-08-16 across the 28
# endpoints serving `deepseek/deepseek-v4-flash-0731`: 18 of them advertise
# `top_k`, so the request routes and returns 200. Nothing errors. What it costs is
# invisible -- the ten endpoints WITHOUT `top_k` include DeepSeek's own
# first-party one, which is the only endpoint on the model with
# `supports_implicit_caching: true` and a cache-read price of $0.0028/M against
# $0.028/M or worse everywhere else. Carrying a parameter the model has no card
# for therefore routes around a 10x cheaper cache, at 100% uptime and 640ms p50,
# and reports success while doing it.
#
# The generalisable half: an unadvertised parameter does not only 404. Under
# `require_parameters` it also NARROWS routing, and a narrowed route is a silent
# cost rather than an error. Check `list-model-endpoints` before adding a family
# here, not after a 404 -- and check what the excluded endpoints were, not only
# whether any remain. The authority is that call, never this tuple: the per-model
# `supported_parameters` is a UNION across providers and will claim support the
# endpoint you land on does not have.
_NO_TOP_K_PREFIXES = ("google/gemini-", "deepseek/")


def openrouter_slug(model: str) -> str:
    """`author/model`, which is the only form OpenRouter resolves.

    Anything already carrying a `/` is passed through untouched -- the catalogue
    is the authority on what exists, and second-guessing a slug an operator
    typed deliberately is how a working configuration gets "corrected" into a
    broken one.
    """
    if "/" in model:
        return model
    slug = _LEGACY_SLUGS.get(model)
    if slug is None:
        # Every model this project has ever used is a Google one, so the guess is
        # a good one -- but it IS a guess, and a guess that silently succeeds is
        # worse than one that says so.
        slug = f"google/{model}"
        log.warning(
            "Model id %r has no author prefix; assuming %r. Set the full "
            "OpenRouter slug to remove the guess.",
            model,
            slug,
        )
    return slug


def build_chat_model(
    model: str,
    *,
    temperature: float | None = None,
    top_p: float | None = None,
    top_k: int | None = None,
    max_tokens: int | None = None,
    reasoning_effort: str | None = None,
    reasoning: bool | None = None,
    **overrides: Any,
) -> ChatOpenAI:
    """One chat model, wired to OpenRouter.

    `reasoning_effort` is passed only when given. It is meaningless to the Gemma
    models (`reasoning.default_enabled` is false for them) and mandatory for
    Gemini 3.7 Flash, which cannot have thinking turned off at all -- only turned
    down. Sending it unconditionally would add an unsupported parameter to every
    Gemma request, which under `require_parameters` above would start excluding
    providers for no reason.

    `reasoning` is the on/off switch, and it exists because a model arrived whose
    default is ON. `deepseek/deepseek-v4-flash-0731` reports
    `reasoning.mandatory=false, default_enabled=true, default_effort="high"`, so
    leaving it alone is a decision -- and an expensive one. Measured 2026-08-16,
    three turns on a one-chunk corpus: output tokens [118, 296, 371] of which
    reasoning [70, 198, 293], i.e. **60-79 percent of billed output was thinking**,
    charged at the completion rate. Passing False zeroed it ([0, 0, 0]).

    Two properties make this safe to send unconditionally where it is wanted.
    `reasoning` is advertised by all 28 endpoints serving that model, so unlike
    `top_k` it narrows routing not at all. And it is `None` by default here, so
    every existing caller's request is byte-identical to before this parameter
    existed.

    **What it must not be used to do:** see `settings.generation_reasoning` for
    the measurement that turning it off on the chat path is only safe while
    `TOOL_GUIDANCE`'s final paragraph survives. The two are redundant with each
    other and removing both loses tool use entirely.
    """
    slug = openrouter_slug(model)
    extra_body: dict[str, Any] = {}

    if settings.openrouter_require_parameters:
        extra_body["provider"] = {"require_parameters": True}

    if reasoning is not None:
        extra_body["reasoning"] = {"enabled": reasoning}

    if top_k is not None:
        if slug.startswith(_NO_TOP_K_PREFIXES):
            log.debug("Model %s does not accept top_k; dropping it.", slug)
        else:
            extra_body["top_k"] = top_k

    if max_tokens is not None:
        # **NOT `ChatOpenAI(max_tokens=...)`, and this one cost a debugging
        # session.** `ChatOpenAI` renames the field unconditionally --
        # `_default_params` and `_get_request_payload` both do
        # `payload["max_completion_tokens"] = payload.pop("max_tokens")`, after
        # OpenAI deprecated the old spelling in September 2024. There is no flag.
        #
        # OpenRouter HONOURS `max_completion_tokens` (verified: a request with
        # `max_completion_tokens=10` stopped at exactly 10 tokens with
        # `finish_reason=length`), but it does not ADVERTISE it -- the name
        # appears in no provider's `supported_parameters`. So under
        # `require_parameters` the router can find nobody who claims to support
        # it and the whole request dies as:
        #
        #     404 No endpoints found that can handle the requested parameters
        #
        # A 404 on a working model id, caused by a parameter that works, because
        # of the name it is sent under. Routing a request and executing it
        # consult different sources of truth, and only the first one is strict.
        # Sending the advertised spelling through `extra_body` sidesteps the
        # rename entirely.
        extra_body["max_tokens"] = max_tokens

    params: dict[str, Any] = {
        "model": slug,
        "api_key": settings.openrouter_api_key,
        "base_url": settings.openrouter_base_url,
        # **`with_structured_output` is unusable without this line.**
        #
        # `with_structured_output(method="function_calling")` does not send only
        # `tools` and `tool_choice` -- langchain-openai also binds
        # `parallel_tool_calls: False` (base.py:2514, inside the method itself,
        # not something the caller passed). That field is OpenAI-specific and
        # appears in no OpenRouter provider's advertised parameter list, so under
        # `require_parameters` it disqualifies every endpoint and the rewrite
        # chain dies on the same 404 as an unroutable token cap.
        #
        # `disabled_params` is the library's own answer to exactly this -- its
        # docstring says a disabled parameter "will not be used by default in any
        # methods, e.g. in `with_structured_output`". Reaching instead for
        # `bind_tools` plus a parser would work and would cost the canonical
        # class the repo deliberately kept: CLAUDE.md's reasoning for paying for
        # `langchain-classic` was that hand-rolling makes a one-line teaching
        # change read as bespoke code.
        "disabled_params": {"parallel_tool_calls": None},
        # A socket that never answers is not the same failure as a slow model,
        # and only one of the two is worth waiting out. Ragas' METRIC_TIMEOUT_S
        # bounds the metric; this bounds the individual HTTP call, so a single
        # hung request cannot eat a metric's entire budget and report itself as
        # "timed out" -- the string CLAUDE.md records as already ambiguous
        # between a hang and a rate limit.
        "timeout": settings.openrouter_timeout_s,
        # Attribution only. Optional, listed by OpenRouter as what puts an app on
        # its leaderboards, and carrying no user data -- the URL and title are
        # this project's, not the caller's.
        "default_headers": {
            "HTTP-Referer": settings.openrouter_app_url,
            "X-Title": settings.openrouter_app_title,
        },
    }
    if temperature is not None:
        params["temperature"] = temperature
    if top_p is not None:
        params["top_p"] = top_p
    if reasoning_effort is not None:
        params["reasoning_effort"] = reasoning_effort
    if extra_body:
        params["extra_body"] = extra_body

    params.update(overrides)
    return ChatOpenAI(**params)
