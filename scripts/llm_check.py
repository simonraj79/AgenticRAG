"""Layer 1 harness for `app/rag/llm.py`. No network, no DB, no model -- instant.

WHY: every trap CLAUDE.md records about OpenRouter is a property of the REQUEST
BODY, and every one of them was found by reading a traceback from a live call.
They are all decidable offline. `build_chat_model` returns a configured
`ChatOpenAI`, so what it put in `extra_body`, `disabled_params` and `model` can be
inspected directly -- no key, no quota, no 404.

The five this pins, and what each cost when it was loose:

  1. `max_tokens` must travel in `extra_body`, never as `ChatOpenAI(max_tokens=)`.
     The class renames it to `max_completion_tokens` unconditionally; OpenRouter
     HONOURS that name but does not ADVERTISE it, so under `require_parameters`
     the request 404s on a working model id because of the name it was sent
     under. Cost: one debugging session.

  2. `parallel_tool_calls` must never appear. `with_structured_output` binds it
     behind the caller's back. Measured 2026-08-16: exactly 1 of the 28 endpoints
     serving `deepseek/deepseek-v4-flash-0731` advertises it, so sending it would
     collapse routing from 28 providers to 1.

  3. `top_k` must be dropped for `google/gemini-`, `deepseek/` and `minimax/`,
     and PRESERVED for Gemma. The Gemini case 404s loudly. The other two do not
     fail at all -- an endpoint that does not advertise `top_k` is EXCLUDED by
     `require_parameters`, so the request silently lands on a different (and
     measurably more expensive) provider. That is the harder bug to ever notice.

  4. `reasoning` must be off by default on the generation path and ABSENT
     entirely when nobody asked, so that every pre-existing caller's request is
     unchanged.

  5. **Nothing in this repo may narrow or reorder routing.** No `provider.order`
     -- the DeepSeek pin was probed on 2026-08-16 and is a NO_GO, because it
     returned 200 on 3/3 while being served by Baidu -- and no `provider.sort`,
     which opts out of quality-based provider selection on tool-calling
     requests, and every generation turn here is one.

Case 9 is the one to read first if this file ever goes red: it asserts the
DEFAULT path is byte-identical to before the reasoning parameter existed.

**What this file structurally CANNOT check**, and the reason case 27-29 exist as
a tripwire rather than as proof: it asserts what the repo put in the request,
never what OpenRouter did with it. A pin that is silently ignored looks exactly
like a pin that worked. Only a live call reading the response's served-provider
field can tell those apart.

**`scripts/route_check.py` is that live call, and it is the other half of this
file.** It makes three cheap generations through `build_chat_model` and reads the
SERVED provider back, so run it whenever anything here changes the request body.
Two of its findings are worth knowing before reading this file's green output as
proof: langchain-openai DROPS OpenRouter's top-level `provider` field (the
`llm_output` whitelist at base.py:1873), so the served endpoint is recoverable
only via `GET /generation?id=`; and one model with one parameter set drew FOUR
different providers in four calls inside twenty seconds. On a pool that wide, a
preference that is ignored and a preference that lost a coin toss look identical
from in here.

    backend/.venv/Scripts/python.exe scripts/llm_check.py             # this file, offline
    backend/.venv/Scripts/python.exe scripts/route_check.py --live    # what OpenRouter did

ASCII in print(), exit 1 on any failure -- `new features/06-test-plan.md`.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.config import settings  # noqa: E402
from app.rag.llm import build_chat_model, openrouter_slug  # noqa: E402

GEMMA = "google/gemma-4-31b-it"
DEEPSEEK = "deepseek/deepseek-v4-flash-0731"
GEMINI = "google/gemini-3.7-flash"
MINIMAX = "minimax/minimax-m3"

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "[ok]  " if condition else "[FAIL]"
    print(f"{status} {name}" + (f" -- {detail}" if detail else ""))
    if not condition:
        failures.append(name)


def body(model: str, **kw) -> dict:
    """The `extra_body` a real call would carry."""
    return dict(getattr(build_chat_model(model, **kw), "extra_body", None) or {})


print("=" * 74)
print("llm.py -- the request body, decided offline")
print("=" * 74)

# ---------------------------------------------------------------------------
print("\n-- slugs --")
check("1. an author/model slug passes through", openrouter_slug(DEEPSEEK) == DEEPSEEK)
check("2. a legacy bare id maps", openrouter_slug("gemma-4-31b-it") == GEMMA)
check(
    "3. the deepseek slug is not mangled by the google-guessing fallback",
    build_chat_model(DEEPSEEK).model_name == DEEPSEEK,
    build_chat_model(DEEPSEEK).model_name,
)

# ---------------------------------------------------------------------------
print("\n-- top_k: one rule, four families, two different consequences --")
check(
    "4. gemma KEEPS top_k (its card gives it as one sampling config)",
    body(GEMMA, top_k=64).get("top_k") == 64,
    str(body(GEMMA, top_k=64).get("top_k")),
)
check(
    "5. gemini DROPS top_k (no endpoint advertises it -> 404)",
    "top_k" not in body(GEMINI, top_k=64),
    str(body(GEMINI, top_k=64)),
)
check(
    "6. deepseek DROPS top_k (routes fine, but around the 2 cheapest endpoints)",
    "top_k" not in body(DEEPSEEK, top_k=64),
    str(body(DEEPSEEK, top_k=64)),
)

# ---------------------------------------------------------------------------
print("\n-- the two parameters that 404 a working model id --")
check(
    "7. max_tokens rides in extra_body, not as a client field",
    body(DEEPSEEK, max_tokens=2048).get("max_tokens") == 2048,
    str(body(DEEPSEEK, max_tokens=2048).get("max_tokens")),
)
check(
    "8. parallel_tool_calls is disabled, so structured output cannot bind it",
    build_chat_model(DEEPSEEK).disabled_params == {"parallel_tool_calls": None},
    str(build_chat_model(DEEPSEEK).disabled_params),
)

# ---------------------------------------------------------------------------
print("\n-- reasoning: off where measured, absent where nobody asked --")
check(
    "9. ABSENT when not passed (every pre-existing caller unchanged)",
    "reasoning" not in body(DEEPSEEK),
    str(body(DEEPSEEK)),
)
check(
    "10. reasoning=False sends enabled:false",
    body(DEEPSEEK, reasoning=False).get("reasoning") == {"enabled": False},
    str(body(DEEPSEEK, reasoning=False).get("reasoning")),
)
check(
    "11. reasoning=True sends enabled:true",
    body(DEEPSEEK, reasoning=True).get("reasoning") == {"enabled": True},
    str(body(DEEPSEEK, reasoning=True).get("reasoning")),
)
check(
    "12. the shipped default is OFF for generation",
    settings.generation_reasoning is False,
    f"generation_reasoning={settings.generation_reasoning}",
)

# ---------------------------------------------------------------------------
print("\n-- routing --")
check(
    "13. require_parameters is on, converting a silent drop into a 404",
    body(DEEPSEEK).get("provider") == {"require_parameters": True},
    str(body(DEEPSEEK).get("provider")),
)

# ---------------------------------------------------------------------------
# 14-15. The generation path end to end -- what `get_chat_model` actually builds
# for the shipped default, which is the only configuration users will hit.
# ---------------------------------------------------------------------------
print("\n-- what the shipped generation path really sends --")
from app.rag.pipeline import get_chat_model  # noqa: E402

shipped = dict(getattr(get_chat_model(None), "extra_body", None) or {})
check(
    "14. no top_k, reasoning off, cap in extra_body, provider pinned",
    (
        "top_k" not in shipped
        and shipped.get("reasoning") == {"enabled": False}
        and shipped.get("max_tokens") == settings.generation_max_tokens
        and shipped.get("provider") == {"require_parameters": True}
    ),
    str(shipped),
)
check(
    "15. an agent pointed back at gemma still gets its full sampling config",
    (lambda b: b.get("top_k") == settings.generation_top_k)(
        dict(
            getattr(
                get_chat_model(None, model=GEMMA, top_k=settings.generation_top_k),
                "extra_body",
                None,
            )
            or {}
        )
    ),
    "gemma keeps top_k through get_chat_model",
)

# ---------------------------------------------------------------------------
# 16-19. Reasoning cannot be turned OFF on every family, and the picker made
#        that load-bearing. Gemini answers a `reasoning:{enabled:false}` with a
#        hard 400 ("Reasoning is mandatory for this endpoint and cannot be
#        disabled"), so a user selecting it would break every turn.
# ---------------------------------------------------------------------------
print("\n-- reasoning: the flag some models refuse --")
check(
    "16. gemini does NOT receive reasoning=false (it 400s on it)",
    "reasoning" not in body(GEMINI, reasoning=False),
    str(body(GEMINI, reasoning=False)),
)
check(
    "17. gemini DOES receive reasoning=true (only disabling is refused)",
    body(GEMINI, reasoning=True).get("reasoning") == {"enabled": True},
    str(body(GEMINI, reasoning=True).get("reasoning")),
)
check(
    "18. deepseek still receives reasoning=false",
    body(DEEPSEEK, reasoning=False).get("reasoning") == {"enabled": False},
)
check(
    "19. gemma still receives reasoning=false",
    body(GEMMA, reasoning=False).get("reasoning") == {"enabled": False},
)

# ---------------------------------------------------------------------------
# 20-25. Model ids the API will now accept from a user, since `generation_model`
#        is editable. The point of the guard is to fail at SAVE time with a
#        message, instead of at chat time with a 404 that reads like an outage.
# ---------------------------------------------------------------------------
print("\n-- the model id a user may now type --")
from fastapi import HTTPException  # noqa: E402

from app.api.agents import _reject_unroutable_model  # noqa: E402
from app.rag.llm import _LEGACY_SLUGS  # noqa: E402


def accepts(value) -> bool:
    try:
        _reject_unroutable_model(value)
        return True
    except HTTPException:
        return False


check("20. None is allowed -- it clears back to the server default", accepts(None))
check("21. a full author/model id is allowed", accepts(DEEPSEEK))
check("22. an unknown vendor is still allowed (no whitelist)", accepts("acme/some-new-model"))
check(
    "23. a bare id is REFUSED rather than guessed into a later 404",
    not accepts("deepseek-v4-flash-0731"),
)
check(
    "24. a known legacy bare id is still allowed (llm.py maps it)",
    accepts("gemma-4-31b-it"),
)
check(
    "25. every _LEGACY_SLUGS target is a real author/model id",
    all("/" in v for v in _LEGACY_SLUGS.values()),
    f"targets={sorted(_LEGACY_SLUGS.values())}",
)

# ---------------------------------------------------------------------------
# 26-29. Routing WIDTH. Every case above asks "is the parameter there?". These
#        ask the opposite question -- "did anything shrink or reorder the set of
#        providers allowed to serve this request?" -- because that failure
#        arrives as a correct answer from the wrong endpoint, with nothing
#        erroring anywhere. It is the one class of bug this file was built for
#        and the one it can only half-see: it asserts what the repo put in the
#        request, never what OpenRouter did with it. `scripts/route_check.py
#        --live` is the other half -- it reads the SERVED provider back, which is
#        the only evidence that separates a pin that worked from a pin that was
#        accepted and ignored.
# ---------------------------------------------------------------------------
print("\n-- routing width: nothing here may narrow or reorder it --")
check(
    "26. minimax DROPS top_k (its card gives none; json_schema routing 4 -> 5)",
    "top_k" not in body(MINIMAX, top_k=64),
    str(body(MINIMAX, top_k=64)),
)
# 27-29 are a decision, not a property, and this is the only place it is
# ENFORCED rather than written down. The DeepSeek provider pin was probed live
# on 2026-08-16 and is a NO_GO: `order: ["DeepSeek"]` returned http=200 on 3/3
# and was served by Baidu every time, because this account's privacy setting
# removes the first-party endpoint -- a line that does nothing and reports
# success. `sort` is the same tripwire from the other side: it is one of exactly
# three documented ways to opt OUT of quality-based provider selection for
# tool-calling requests, and `agent_loop.py` binds tools on all three model
# invocations, so every generation turn here is one. Asserted for all three
# families because a pin would arrive keyed on a prefix.
for num, name, slug in (
    (27, "gemma", GEMMA),
    (28, "deepseek", DEEPSEEK),
    (29, "minimax", MINIMAX),
):
    prov = body(slug).get("provider") or {}
    check(
        f"{num}. {name} carries no provider.order (NO_GO) and no provider.sort",
        "order" not in prov and "sort" not in prov,
        str(prov),
    )

# ---------------------------------------------------------------------------
# 30. The TOOL-BOUND request, which is the one every generation turn actually
#     sends -- `agent_loop` binds tools on all three of its model invocations.
#     Every case above inspects a bare `build_chat_model`; this one inspects what
#     `bind_tools` produced on top of it, with the real registry rather than a
#     stand-in.
#
#     It is here because `12-robust-handouts/06-tool-path-parity.md` (A6) put
#     deck rules into `TOOL_DESCRIPTION` and `TOOL_GUIDANCE`. PROMPT TEXT IS FREE
#     AT ROUTING and a parameter is not, and the margin is smaller than it looks.
#     Measured 2026-08-17 against `deepseek/deepseek-v4-flash-0731`, 28 endpoints
#     serving it:
#
#         tools + top_k          -- today's shape        19 / 28
#         + response_format + structured_outputs         15 / 28
#         + parallel_tool_calls                           1 / 28
#
#     So `disabled_params={"parallel_tool_calls": None}` (case 8) is far more
#     load-bearing on DeepSeek than on the Gemma 404 it was written for: it is
#     the difference between a pool and a single endpoint. The other two arrive
#     through `with_structured_output`, which nothing on the generation path
#     calls today -- this case is the tripwire for the day something does.
#
#     `loop.md` T5: check endpoints BEFORE adding to a tool-bound request, not
#     after a 404. And note the same structural blind spot as 26-29 -- this reads
#     what the repo put in the request, never what OpenRouter did with it.
# ---------------------------------------------------------------------------
print("\n-- the tool-bound request: prompt text is free, a parameter is not --")
from app.tools.registry import ToolContext, build_tools  # noqa: E402

# Neither field is touched by the factories; `build_tools` only closes over them.
_tools = build_tools(ToolContext(agent=None, ledger=None))  # type: ignore[arg-type]
_bound = get_chat_model(None).bind_tools(_tools)
_kwargs = dict(getattr(_bound, "kwargs", None) or {})
_extra = dict(getattr(getattr(_bound, "bound", None), "extra_body", None) or {})
BANNED = ("parallel_tool_calls", "response_format", "structured_outputs")
_present = [key for key in BANNED if key in _kwargs or key in _extra]
check(
    "30. binding the real tools adds `tools` and NOTHING that narrows routing",
    not _present
    and sorted(_kwargs) == ["tools"]
    and sorted(t["function"]["name"] for t in _kwargs["tools"])
    == ["run_python", "search_corpus"],
    f"banned_present={_present} kwargs={sorted(_kwargs)} extra_body={sorted(_extra)}",
)

# ---------------------------------------------------------------------------
# 31. METERING MUST NOT TOUCH THE REQUEST.
#
#     `MeteredChatOpenAI` overrides one PARSING method to keep the usage frame
#     langchain discards while streaming. It adds no field to the request -- and
#     this case is the tripwire, not the claim, because everything above it is a
#     record of what an added field costs: `max_completion_tokens` 404s at
#     routing, `parallel_tool_calls` collapses 28 endpoints to 1, and `top_k` on
#     the wrong family fails with NO ERROR AT ALL, just a different provider and
#     a different bill.
#
#     Two of the four documented traps are silent, so "the app still works" is
#     not evidence here. Byte equality is.
#
#     Checked across all four families, because the three per-family branches
#     (`_NO_TOP_K_PREFIXES`, `_REASONING_ALWAYS_ON_PREFIXES`, and the Gemma
#     passthrough) each build a different `extra_body`, and a subclass that
#     perturbed only one of them would pass a single-model check.
# ---------------------------------------------------------------------------
print("\n-- metering changes parsing, never the request --")

_was_metering = settings.metering_enabled


def _request_shape(model: str) -> dict:
    """Everything that reaches OpenRouter, plus the two langchain-side guards."""
    m = build_chat_model(
        model,
        temperature=settings.generation_temperature,
        top_k=settings.generation_top_k,
        max_tokens=settings.generation_max_tokens,
        reasoning=settings.generation_reasoning,
    )
    return {
        "extra_body": dict(getattr(m, "extra_body", None) or {}),
        "disabled_params": dict(getattr(m, "disabled_params", None) or {}),
        "model_kwargs": dict(getattr(m, "model_kwargs", None) or {}),
        "model_name": getattr(m, "model_name", None),
        "temperature": getattr(m, "temperature", None),
        # `stream_usage` must stay unset. OpenRouter DEPRECATED
        # `stream_options: {include_usage: true}` and returns usage regardless,
        # and base.py:1417 parses arriving usage unconditionally -- so setting it
        # would add an unprobed key to the request to buy nothing at all.
        # Measured 2026-08-20: identical usage either way.
        "stream_usage": getattr(m, "stream_usage", None),
    }


_diffs = []
for _model in (DEEPSEEK, GEMMA, GEMINI, MINIMAX):
    try:
        settings.metering_enabled = False
        _off = _request_shape(_model)
        settings.metering_enabled = True
        _on = _request_shape(_model)
    finally:
        settings.metering_enabled = _was_metering
    if _off != _on:
        _diffs.append(f"{_model}: off={_off} on={_on}")

check(
    "31. metering on/off leaves the request byte-identical, all four families",
    not _diffs,
    "; ".join(_diffs) if _diffs else "extra_body/disabled_params/model_kwargs equal",
)

check(
    "31b. and `stream_usage` is never set -- it would add stream_options for nothing",
    all(
        _request_shape(m)["stream_usage"] in (None, False)
        for m in (DEEPSEEK, GEMMA)
    ),
)

print("\n" + "=" * 74)
if failures:
    print(f"{len(failures)} FAILED:")
    for name in failures:
        print(f"  - {name}")
    sys.exit(1)
print("all checks passed")
