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

print("\n" + "=" * 74)
if failures:
    print(f"{len(failures)} FAILED:")
    for name in failures:
        print(f"  - {name}")
    sys.exit(1)
print("all checks passed")
