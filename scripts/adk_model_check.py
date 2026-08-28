"""Layer 1 harness for `app/adk/model.py`: what the ADK runtime puts on the wire.

No DB, no network, no model.

The sibling of `scripts/llm_check.py`, and the reason it is a SECOND file rather
than more cases in that one: `llm_check` inspects a `ChatOpenAI` built by
`build_chat_model`, and every one of its 32 cases would keep passing on the ADK
path while describing a request nothing sends. A harness that stays green about a
runtime it never examined is this repository's most-repeated failure.

CLAUDE.md's own diagnostic for an OpenRouter parameter problem is **"print the
request body, do not read the call site"** -- because two of the three parameters
that have broken this project were invisible at the call site by construction.
This file is that diagnostic promoted to a harness: it builds the runnable the
adapter would actually invoke and asserts on the parameters attached to it.

Cases (`new features/18-adk-runtime/PLAN.md` section 5):
  A1   `max_tokens` in extra_body; `max_completion_tokens` ABSENT
  A2   `provider` is exactly {"require_parameters": True}
  A3   `top_k` presence matches `_NO_TOP_K_PREFIXES`, read at runtime
  A4   `reasoning` presence matches `_REASONING_ALWAYS_ON_PREFIXES`
  A5   `parallel_tool_calls` disabled
  A6   no `provider.order`, no `provider.sort`, on any family
  A7   mode ANY + allowed_function_names -> tool_choice == that NAME
  A8   mode NONE -> tool_choice == "none" AND tools still bound
  A9   metering on vs off -> byte-identical request parameters
  A11  nothing outside `app/adk/` imports `google.adk`
  A12  tool order is [search_corpus, run_python], unsorted

    backend/.venv/Scripts/python.exe scripts/adk_model_check.py

ASCII in print(), exit 1 on any failure -- `new features/06-test-plan.md`.
"""

from __future__ import annotations

import ast
import sys
import warnings
from pathlib import Path
from types import SimpleNamespace

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
APP = ROOT / "backend" / "app"

from google.genai import types  # noqa: E402

from app.adk.model import build_adk_model  # noqa: E402
from app.config import settings  # noqa: E402
from app.rag.llm import (  # noqa: E402
    _NO_TOP_K_PREFIXES,
    _REASONING_ALWAYS_ON_PREFIXES,
    openrouter_slug,
)

# One per family this project can be pointed at, so a policy that is right for
# DeepSeek and wrong for Gemini cannot pass.
FAMILIES = (
    "deepseek/deepseek-v4-flash-0731",
    "google/gemma-4-31b-it",
    "google/gemini-3.7-flash",
    "minimax/minimax-m3",
)

failures: list[str] = []
checks = 0


def check(label: str, ok: bool, detail: str = "", *, always: bool = False) -> None:
    global checks
    checks += 1
    show = detail if (detail and (always or not ok)) else ""
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" -- {show}" if show else ""))
    if not ok:
        failures.append(label)


def _declaration(name: str = "search_corpus") -> types.FunctionDeclaration:
    return types.FunctionDeclaration(
        name=name,
        description="d",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={"query": types.Schema(type=types.Type.STRING)},
            required=["query"],
        ),
    )


def _request(*, tools: bool = False, mode=None, allowed=None):
    """A minimal `LlmRequest`-shaped object. Only `.config` is read by `_runnable`."""
    tool_config = None
    if mode is not None:
        tool_config = types.ToolConfig(
            function_calling_config=types.FunctionCallingConfig(
                mode=mode, allowed_function_names=allowed
            )
        )
    config = SimpleNamespace(
        tools=[types.Tool(function_declarations=[_declaration()])] if tools else None,
        tool_config=tool_config,
        system_instruction=None,
    )
    return SimpleNamespace(config=config, contents=[])


def _bound_kwargs(runnable) -> dict:
    """The kwargs `bind_tools` attached, or {} for an unbound model."""
    return dict(getattr(runnable, "kwargs", None) or {})


def _underlying(runnable):
    """The ChatOpenAI beneath a RunnableBinding, or the model itself."""
    return getattr(runnable, "bound", runnable)


def main() -> int:
    print("adk_model_check -- what the ADK runtime puts on the wire")
    print("")

    # ---------------------------------------------------------------- A1-A6
    print("A1-A6 -- the request body, per model family")
    for family in FAMILIES:
        model = build_adk_model(family, max_tokens=777, reasoning=False)
        chat = model._chat()
        extra = dict(chat.extra_body or {})
        slug = openrouter_slug(family)
        short = family.split("/")[-1][:22]

        check(
            f"A1 [{short}] max_tokens travels in extra_body",
            extra.get("max_tokens") == 777,
            str(extra.get("max_tokens")),
        )
        check(
            f"A1 [{short}] max_completion_tokens is ABSENT",
            "max_completion_tokens" not in extra
            and getattr(chat, "max_tokens", None) is None,
            f"extra={list(extra)} field={getattr(chat, 'max_tokens', None)}",
        )
        check(
            f"A2 [{short}] provider is exactly require_parameters",
            extra.get("provider") == {"require_parameters": True},
            str(extra.get("provider")),
        )
        # A3/A4 read the POLICY off app.rag.llm at runtime, so this follows the
        # application rather than a copy of it. A hardcoded expectation here would
        # pass for as long as it happened to agree, then silently disagree.
        expect_top_k = not slug.startswith(_NO_TOP_K_PREFIXES)
        check(
            f"A3 [{short}] top_k presence matches _NO_TOP_K_PREFIXES",
            ("top_k" in extra) == expect_top_k,
            f"present={'top_k' in extra} expected={expect_top_k}",
            always=True,
        )
        if "top_k" in extra:
            check(f"A3 [{short}] top_k is an int", isinstance(extra["top_k"], int))
        expect_reasoning = not slug.startswith(_REASONING_ALWAYS_ON_PREFIXES)
        check(
            f"A4 [{short}] reasoning presence matches _REASONING_ALWAYS_ON_PREFIXES",
            ("reasoning" in extra) == expect_reasoning,
            f"present={'reasoning' in extra} expected={expect_reasoning}",
            always=True,
        )
        check(
            f"A5 [{short}] parallel_tool_calls disabled",
            dict(chat.disabled_params or {}) == {"parallel_tool_calls": None},
            str(chat.disabled_params),
        )
        provider = extra.get("provider") or {}
        check(
            f"A6 [{short}] no provider.order and no provider.sort",
            "order" not in provider and "sort" not in provider,
            str(provider),
        )
    print("")

    # ---------------------------------------------------------------- A7/A8
    print("A7/A8 -- tool_config, the gap trigger's only working mechanism")
    model = build_adk_model(FAMILIES[0])

    forced = model._runnable(
        _request(tools=True, mode=types.FunctionCallingConfigMode.ANY, allowed=["search_corpus"])
    )
    kwargs = _bound_kwargs(forced)
    # **Asserted against the WIRE FORM, not the input string.** `bind_tools`
    # normalises a bare name into OpenAI's
    # `{"type": "function", "function": {"name": ...}}`, which IS the named-tool
    # spelling OpenRouter routes on. The first draft compared to the string
    # `"search_corpus"` and failed on correct behaviour -- the exact mistake this
    # file exists to prevent someone making about a request body.
    choice = kwargs.get("tool_choice")
    named = (
        isinstance(choice, dict)
        and choice.get("type") == "function"
        and (choice.get("function") or {}).get("name") == "search_corpus"
    ) or choice == "search_corpus"
    check(
        "A7 mode ANY + one allowed name -> tool_choice NAMES that tool",
        named,
        f"tool_choice={choice!r}",
        always=True,
    )
    check(
        "A7 NOT 'required'/'any' -- litellm's lossy mapping, silently ignored here",
        choice not in ("required", "any"),
        "allowed_function_names would have been discarded",
    )
    check("A7 the tool is still bound", bool(kwargs.get("tools")), str(list(kwargs)))

    none_bound = model._runnable(
        _request(tools=True, mode=types.FunctionCallingConfigMode.NONE)
    )
    kwargs = _bound_kwargs(none_bound)
    check(
        "A8 mode NONE -> tool_choice == 'none'",
        kwargs.get("tool_choice") == "none",
        f"tool_choice={kwargs.get('tool_choice')!r}",
        always=True,
    )
    check(
        "A8 and `tools` is STILL populated (the routing constraint)",
        bool(kwargs.get("tools")),
        "dropping tools mid-turn changes which providers can serve the request",
    )

    auto = model._runnable(_request(tools=True))
    kwargs = _bound_kwargs(auto)
    check("A8 no tool_config -> no tool_choice", "tool_choice" not in kwargs, str(list(kwargs)))
    check("A8 toolless request binds nothing", not _bound_kwargs(model._runnable(_request())))
    print("")

    # ---------------------------------------------------------------- A9
    print("A9 -- metering must add NOTHING to the request")
    original = settings.metering_enabled
    try:
        settings.metering_enabled = False
        off = build_adk_model(FAMILIES[0], max_tokens=512)._chat()
        settings.metering_enabled = True
        on = build_adk_model(FAMILIES[0], max_tokens=512)._chat()
        for field in ("extra_body", "disabled_params", "model_kwargs"):
            check(
                f"A9 {field} identical with metering on and off",
                dict(getattr(off, field, None) or {}) == dict(getattr(on, field, None) or {}),
                f"off={getattr(off, field, None)} on={getattr(on, field, None)}",
            )
        check(
            "A9 metering ON returns the metered subclass",
            type(on).__name__ == "MeteredChatOpenAI",
            type(on).__name__,
            always=True,
        )
        check(
            "A9 metering OFF returns the plain class that shipped",
            type(off).__name__ == "ChatOpenAI",
            type(off).__name__,
        )
    finally:
        settings.metering_enabled = original
    print("")

    # ---------------------------------------------------------------- A11
    print("A11 -- google.adk is contained inside app/adk/")
    offenders: list[str] = []
    scanned = 0
    for path in sorted(APP.rglob("*.py")):
        rel = path.relative_to(APP)
        if rel.parts and rel.parts[0] == "adk":
            continue
        scanned += 1
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("google.adk"):
                        offenders.append(f"{rel}:{node.lineno}")
            elif isinstance(node, ast.ImportFrom):
                if (node.module or "").startswith("google.adk"):
                    offenders.append(f"{rel}:{node.lineno}")
    check("A11 the walk actually scanned the app", scanned > 20, f"files={scanned}")
    check(
        "A11 nothing outside app/adk/ imports google.adk",
        not offenders,
        str(offenders),
    )
    # And the rollback is only real if the SELECTOR imports lazily too.
    runtime_src = (APP / "rag" / "runtime.py").read_text(encoding="utf-8")
    module_level = [
        line
        for line in runtime_src.splitlines()
        if line.startswith("from app.adk") or line.startswith("import app.adk")
    ]
    check(
        "A11 runtime.py imports app.adk INSIDE the branch, not at module scope",
        not module_level,
        str(module_level),
    )
    print("")

    # ---------------------------------------------------------------- A12
    print("A12 -- tool order is a decision, not an accident")
    from app.adk.context import AdkTurnContext
    from app.adk.tools import build_adk_tools

    ctx = AdkTurnContext(agent=SimpleNamespace(id="a", namespace="n"), ledger=None)
    names = [t.name for t in build_adk_tools(ctx)]
    check("A12 order is [search_corpus, run_python]", names == ["search_corpus", "run_python"],
          str(names), always=True)
    check(
        "A12 and it is NOT alphabetical (a sorted() would reverse it)",
        names != sorted(names),
        "sorted() would put run_python first and nothing would fail",
    )
    print("")

    if failures:
        print(f"FAILED {len(failures)} of {checks}:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"all {checks} adk model checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
