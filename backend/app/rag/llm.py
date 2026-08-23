"""Every chat model in this project is constructed here, and nowhere else.

The same argument as `app/rag/retriever.py`: there were four construction sites
for `ChatGoogleGenerativeAI` (generation, the rewriter, the golden-set generator,
the Ragas judge), and moving providers meant finding all four and getting the
same four decisions right in each. One seam makes a provider change a one-file
change, exactly as the retriever seam makes the Stage 1 -> Stage 2 change a
one-liner.

------------------------------------------------------------------
WHY OPENROUTER, AND WHAT MOVED WITH IT.

**Chat and embeddings both go through OpenRouter, and this paragraph used to
say the exact opposite.** It read "Embeddings do not, and must not", on the
grounds that OpenRouter "is a chat-completions gateway and does not serve that
model at all, so 'move everything to OpenRouter' would have meant a full
re-ingest to gain nothing". Both halves were false, and the second one was the
expensive kind of false -- a cost that was never paid, cited as the reason not
to try. Measured 2026-08-16: OpenRouter serves `google/gemini-embedding-2`
itself, on 3 endpoints, and three strings pushed through **both** roads came
back at cosine **1.000000** on `embed_documents` AND on `embed_query`, every
vector L2-normalised, against a cross-string control of 0.616566. One model,
one space, two gateways. The move cost no re-ingest, no re-index, and no change
to `settings.embedding_model` -- which is still `models/gemini-embedding-2`
because that string is the PROVENANCE stamped onto `agents.embedding_model`,
and the space it names did not change.

The rule the old paragraph was protecting is untouched, and it is why the
cosine was measured rather than assumed: **the embedding model is part of the
index.** Matching dimensions do not imply a shared vector space, and querying
an index built with one embedder using another returns confident nonsense
rather than an error -- there is no exception to that, only a verified case of
two roads reaching the same place. `app/rag/retriever.py` remains the single
construction site and picks the road from `settings.embedding_route`:
`openrouter` ships, `google` is the rollback.

The practical consequence: **`GEMINI_API_KEY` is no longer required.** It is
the rollback route's key -- needed only under `EMBEDDING_ROUTE=google` -- and
is optional otherwise. Ragas needs a judge LLM *and* an embedding model, and
now draws both halves from OpenRouter rather than one from each provider.
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
    # `gemini-flash-lite-latest` used to map to `google/gemini-3.7-flash-lite`,
    # and that slug DOES NOT EXIST -- OpenRouter answers
    # `"google/gemini-3.7-flash-lite is not a valid model ID"`. Verified
    # 2026-08-16. A legacy-id guard whose whole purpose is to stop a bare id
    # 404ing, and which mapped to a model that 400s, is worse than no entry: the
    # unmapped path at least logs a warning naming its own guess. Removed rather
    # than repointed, because there is no evidence any agent ever used it.
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
# `deepseek/` -- FAILS SILENTLY, WHICH IS WORSE, AND THE MECHANISM IS PROVED
# RATHER THAN INFERRED: an endpoint that does not advertise `top_k` is EXCLUDED
# by `require_parameters`, so sending it does not merely waste a field, it picks
# a different provider. Re-measured against the live catalogue 2026-08-16, 28
# endpoints serve `deepseek/deepseek-v4-flash-0731` and 9 of them do not
# advertise `top_k` -- including the two CHEAPEST on the model, StreamLake at
# $0.06426/M prompt and Baidu at $0.0644/M. Send `top_k` and both are ineligible;
# the cheapest survivor is Decart at $0.0657/M. HTTP 200, a correct answer, no
# warning, a bill that is quietly not the one the price table would predict.
# **That is why this tuple is not a cost optimisation.** An optimisation is
# optional; this is the difference between the provider the operator thinks is
# serving the model and the one that is.
#
# An earlier version of this comment argued the case from DeepSeek's own
# first-party endpoint -- the only one of the 28 with
# `supports_implicit_caching: true`, cache reads at $0.0028/M against $0.028/M
# elsewhere -- and that argument is WITHDRAWN as the load-bearing one. Two
# measurements killed it. This account cannot route there at all (see the
# provider-pin NO_GO in `build_chat_model`, where `provider.only: ["deepseek"]`
# answers `"No endpoints available matching your guardrail restrictions and data
# policy"`), so no parameter choice buys it back. And its uncached prompt price
# is $0.14/M -- **2.2x Baidu's** -- so the cache only pays at a high implicit-hit
# rate, which was measured at exactly zero: `prompt_tokens_details
# {cached_tokens: 0, cache_write_tokens: 0}` on every probe call. The retrieved
# chunks sit INSIDE the prompt and change every query, so a stable cacheable
# prefix is a hypothesis, not a property. The conclusion survives; the reason for
# it changed, and a reason nobody re-checks is how a 2.2x markup gets recorded as
# a saving.
#
# `minimax/` -- SAME SILENT SHAPE, one family later, and it arrived with the
# golden-set generator (`settings.golden_set_model = "minimax/minimax-m3"`, which
# inherited `top_k` from the generation defaults exactly as the Gemini case did).
# MiniMax's card reports `default_parameters.top_k = null`: there is **no
# configuration to honour**, which is this tuple's stated criterion above rather
# than a convenience. Measured 2026-08-16 across the 12 endpoints serving
# `minimax/minimax-m3`: 5 do not advertise `top_k`, one of the 5 being MiniMax's
# own first-party endpoint (tag `minimax/fp8`). On the path the golden set
# actually takes -- `STRUCTURED_OUTPUT_METHOD = "json_schema"`, which additionally
# requires `structured_outputs` -- eligibility is CoreWeave, Together, Parasail,
# ModelRun and Morph, and carrying `top_k` drops Morph: **5 endpoints become 4**.
# That matters because Parasail, one of the 4, measured 96.80% uptime. Latency
# paid nothing either way (p50 4.68 s with `top_k`, 4.67 s without).
# **Reversal condition:** if a future measurement shows MiniMax honouring `top_k`
# in a way that improves golden-set quality, this is the line to revert.
#
# The generalisable half: an unadvertised parameter does not only 404. Under
# `require_parameters` it also NARROWS routing, and a narrowed route is a silent
# cost rather than an error. Check `list-model-endpoints` before adding a family
# here, not after a 404 -- and check what the excluded endpoints were, not only
# whether any remain. The authority is that call, never this tuple: the per-model
# `supported_parameters` is a UNION across providers and will claim support the
# endpoint you land on does not have.
#
# **And the failure mode INVERTS the moment any provider pin lands.** Unpinned,
# `top_k` costs a silent reroute. Pinned to a single endpoint that does not
# advertise it, there is nowhere to reroute TO and every turn is a hard 404 --
# proven on BaseTen, which serves this model at 100% uptime and advertises no
# `top_k`. So this tuple stops being about money and becomes a correctness
# requirement if `provider.order` is ever written. Read it again then.
_NO_TOP_K_PREFIXES = ("google/gemini-", "deepseek/", "minimax/")

# Model families that refuse to have reasoning turned OFF.
#
# `settings.generation_reasoning` is False, so every generation call carries
# `reasoning: {"enabled": false}`. On Gemini 3.7 Flash that is not ignored and it
# is not a routing miss -- it is a hard 400:
#
#     Reasoning is mandatory for this endpoint and cannot be disabled.
#
# `reasoning.mandatory` is true for that family (the same property
# `ragas_judge_reasoning_effort` exists to work around, where thinking can only
# be turned DOWN). Verified 2026-08-16: plain generation, a tool-bound call and
# `with_structured_output` all three failed identically.
#
# **This became load-bearing the moment `generation_model` became editable.**
# While it was a settings-only value, nobody was going to point generation at
# Flash by accident. As a field in the settings sheet it is one click, and every
# turn on that agent would 400 -- so the model picker would have shipped with a
# poisoned option. The guard is here rather than in the picker because the column
# is free text and the API is not the only way in: `agentic_check.py` writes it
# directly, and CLAUDE.md describes an operator typing into it.
#
# Dropping the flag rather than raising is right: the caller's intent is "do not
# spend tokens thinking", and on a model that cannot comply the honest outcome is
# the model's default, not a failed turn. The cost is visible in the usage
# numbers, not hidden.
_REASONING_ALWAYS_ON_PREFIXES = ("google/gemini-",)


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

    # **A PROVIDER PIN WAS PROBED HERE ON 2026-08-16 AND DELIBERATELY NOT BUILT.
    # Do not re-attempt it from the code; re-probe first.** The intended line was
    # `order: ["DeepSeek"]` with `allow_fallbacks: true`, to reach the model's
    # only implicit-caching endpoint. Live result: **http=200 on 3/3 calls, served
    # by BAIDU every time.** Not an error, not a warning, not a dropped field --
    # the preference was accepted and ignored, and nothing in the response says so
    # except the top-level `provider` name, which nothing in this project reads.
    # A misspelled or unreachable provider name in `order` behaves identically.
    #
    # The cause is not in this request. `provider.only: ["deepseek"]` says it in
    # as many words:
    #
    #     404 No endpoints available matching your guardrail restrictions and
    #         data policy. Configure: https://openrouter.ai/settings/privacy
    #
    # The first-party endpoint is filtered out by THIS ACCOUNT's model-training /
    # data-policy setting. Verified that per-request `data_collection: "allow"`
    # does not override it (the account toggle is the ceiling), and that
    # `allow_fallbacks: false`, the `deepseek/fp8` tag form and the lowercase slug
    # all fail the same way. `order` itself works fine -- controls pinned Wafer
    # and CoreWeave 5/5, case-insensitively. So the pin would have been a line
    # that does nothing, costs nothing, and reports success: `loop.md` T2 exactly,
    # and `llm_check.py` would have gone green because an offline harness can only
    # assert what this repo put in the dict, never what OpenRouter did with it.
    #
    # The economics are inverted anyway, which is the part worth knowing before
    # anyone "fixes" the toggle: DeepSeek first-party is $0.14/$0.28 per M against
    # Baidu's $0.0644/$0.1288 -- **2.2x MORE on uncached tokens** -- and the
    # $0.0028/M cache read only pays at a high implicit-hit rate. Measured
    # `prompt_tokens_details {cached_tokens: 0, cache_write_tokens: 0}`. Retrieved
    # chunks sit inside the prompt and change every query, so establish that a
    # cacheable prefix exists at all before reopening this.
    #
    # And unblocking it is a HUMAN decision, not a code change: flipping that
    # privacy toggle opts the whole account into providers that may train on
    # prompts, while Groundwork puts user-uploaded course documents into every
    # generation prompt and this repo is public. That is a PRD-level consent
    # question, adjacent to open item 6, and it is account-wide.
    #
    # If it is ever unblocked, the pin MUST be MERGED into this dict, never
    # assigned over it:
    #
    #     prov: dict[str, Any] = {}
    #     if settings.openrouter_require_parameters:
    #         prov["require_parameters"] = True
    #     if slug.startswith(tuple(_PINNED_PROVIDERS)):     # prefix-keyed, like
    #         prov["order"] = [...]                         # the two tuples above
    #         prov["allow_fallbacks"] = True
    #     if prov:
    #         extra_body["provider"] = prov
    #
    # A second `extra_body["provider"] = {...}` statement silently DELETES
    # `require_parameters` (or the pin, depending which ran last), which is the
    # exact failure the flag exists to prevent -- OpenRouter goes back to dropping
    # `tools` on a tool-less tier and `function_calling` returns prose. And
    # `extra_body["provider"]["order"] = ...` raises KeyError the first time an
    # operator sets `OPENROUTER_REQUIRE_PARAMETERS=false`, turning a documented
    # escape hatch into a hard crash on every chat call. `llm_check.py` cases
    # 13/14 assert this dict by EXACT EQUALITY to catch both; do not loosen them.
    #
    # Never `sort`. `provider.sort` looks like it reinforces a pin ("cheapest
    # first, and the cache is cheapest") and is one of exactly three documented
    # ways to opt OUT of OpenRouter's quality-based provider reordering for
    # tool-calling requests -- and `agent_loop.py` binds tools on all three model
    # invocations, so EVERY generation turn here is a tool-calling request. That
    # is a quality regression with no error attached, arriving from a line added
    # to save money. Cases 27-29 are the tripwire.
    if settings.openrouter_require_parameters:
        extra_body["provider"] = {"require_parameters": True}

    if reasoning is not None:
        if reasoning is False and slug.startswith(_REASONING_ALWAYS_ON_PREFIXES):
            log.debug(
                "Model %s cannot disable reasoning; leaving it at the provider "
                "default rather than sending a flag it rejects.",
                slug,
            )
        else:
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

    # **Metering attaches HERE, at the one chokepoint, and adds NOTHING to the
    # request.** Eight call sites reach this function -- generation, the
    # rewriter, routing, the critic, handout code, the golden-set drafter and
    # the Ragas judge -- and none of them bypasses it, the same property
    # CLAUDE.md relies on for `retriever.py`. One handler here meters all of
    # them, including the last two, which belong to no `queries` row and which
    # per-call-site instrumentation would have forgotten.
    #
    # The `else` branch is the regression guarantee rather than a courtesy: with
    # the flag off this returns the exact class that shipped, so "unchanged" is
    # structural instead of careful. `MeteredChatOpenAI` overrides one PARSING
    # method and touches no field of the request; `llm_check.py` case 31 asserts
    # `extra_body`, `disabled_params` and `model_kwargs` are identical either way,
    # because CLAUDE.md records four separate ways an added parameter goes wrong
    # and two of them fail silently.
    if not settings.metering_enabled:
        return ChatOpenAI(**params)

    from app.metering.chat import MeteredChatOpenAI
    from app.metering.meter import LoggingSink, UsageMeter

    # Callbacks are APPENDED, never assigned over: `params.update(overrides)` ran
    # above, so a caller that passed its own `callbacks=` must keep them. Losing
    # a caller's callback to a metering line would be a silent behaviour change
    # in someone else's feature.
    callbacks = list(params.get("callbacks") or [])
    callbacks.append(
        UsageMeter(
            sink=LoggingSink(),
            strict=settings.metering_strict,
            # The RESOLVED slug, not the caller's argument -- `openrouter_slug`
            # may have mapped a legacy bare id, and the admin console must group
            # by what was actually requested. A streamed response cannot supply
            # this: see `UsageMeter.__init__`.
            model=slug,
        )
    )
    params["callbacks"] = callbacks
    return MeteredChatOpenAI(**params)
