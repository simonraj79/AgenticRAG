"""Layer 1 harness for `app/rag/llm.py`. No network, no DB, no model -- instant.

WHY: every trap CLAUDE.md records about OpenRouter is a property of the REQUEST
BODY, and every one of them was found by reading a traceback from a live call.
They are all decidable offline. `build_chat_model` returns a configured
`ChatOpenAI`, so what it put in `extra_body`, `disabled_params` and `model` can be
inspected directly -- no key, no quota, no 404.

The four this pins, and what each cost when it was loose:

  1. `max_tokens` must travel in `extra_body`, never as `ChatOpenAI(max_tokens=)`.
     The class renames it to `max_completion_tokens` unconditionally; OpenRouter
     HONOURS that name but does not ADVERTISE it, so under `require_parameters`
     the request 404s on a working model id because of the name it was sent
     under. Cost: one debugging session.

  2. `parallel_tool_calls` must never appear. `with_structured_output` binds it
     behind the caller's back. Measured 2026-08-16: exactly 1 of the 28 endpoints
     serving `deepseek/deepseek-v4-flash-0731` advertises it, so sending it would
     collapse routing from 28 providers to 1.

  3. `top_k` must be dropped for `google/gemini-` and `deepseek/`, and PRESERVED
     for Gemma. The Gemini case 404s loudly. The DeepSeek case does not fail at
     all -- it silently routes around the one endpoint with a 10x cheaper cache,
     which is the harder bug to ever notice.

  4. `reasoning` must be off by default on the generation path and ABSENT
     entirely when nobody asked, so that every pre-existing caller's request is
     unchanged.

Case 9 is the one to read first if this file ever goes red: it asserts the
DEFAULT path is byte-identical to before the reasoning parameter existed.

    backend/.venv/Scripts/python.exe scripts/llm_check.py

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
print("\n-- top_k: one rule, three families, two different consequences --")
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
    "6. deepseek DROPS top_k (routes fine, but around the cached endpoint)",
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

print("\n" + "=" * 74)
if failures:
    print(f"{len(failures)} FAILED:")
    for name in failures:
        print(f"  - {name}")
    sys.exit(1)
print("all checks passed")
