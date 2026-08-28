"""Layer 1 harness: the tenancy boundary is STRUCTURAL, in both runtimes.

No DB, no network, no model.

WHY THIS FILE EXISTS.

PRD section 7: *"the namespace comes from the session, never from the request
body."* `app/tools/registry.py` implements that by closing over the `Agent`
object, so `SearchCorpusArgs` has exactly one field and there is no argument
through which another tenant's corpus could be named.

**Nothing asserted it.** Until this file, the property was carried entirely by a
docstring and by review. And it is the single worst property in this repository
to leave unasserted, because breaking it is invisible to every other harness:
add `agent_id: str` to the schema and retrieval still works, citations still
resolve, `agentic_check` still passes, and the scorecard still renders -- because
a CORRECT `agent_id` produces a correct answer. The defect only appears when a
prompt-injected document names a different tenant, which no test does.

That is `new features/loop.md` T2 in its most expensive form: the error-shaped
check passes while the thing you wanted silently did not happen.

THE ADK MIGRATION IS WHY IT WAS WRITTEN NOW.

Google ADK's `FunctionTool` derives a tool's schema from the CALLABLE'S SIGNATURE.
That ergonomic pushes hard toward

    def search_corpus(query: str, agent_id: str) -> dict: ...

which is idiomatic ADK and a tenancy hole. `app/adk/tools.py` therefore subclasses
`BaseTool` and writes `_get_declaration()` by hand, so no code path could
introspect the closed-over `Agent` into the schema. T3 and T4 assert that against
the SERIALISED declaration -- the bytes that would reach a provider -- rather than
against the Python object, because the object is exactly what looks correct.

Cases:
  T1  SearchCorpusArgs exposes exactly one field
  T2  the langchain bound-tool schema names no tenancy parameter
  T3  the ADK declaration, SERIALISED, names no tenancy parameter
  T4  agent.namespace appears nowhere in a serialised ADK request body
  T5  no tool callable in backend/app/ takes a tenancy-shaped parameter (AST)
  T6  AgentTool is imported nowhere in backend/app/

    backend/.venv/Scripts/python.exe scripts/tenancy_check.py

ASCII in print(), exit 1 on any failure -- `new features/06-test-plan.md`.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

APP = ROOT / "backend" / "app"

# The words that must never appear as a MODEL-SUPPLIED tool parameter.
#
# `k` and `filter` are in here beside the obvious three because they are the
# other half of the same hole: a model that can set `k` can starve retrieval, and
# a model that can pass a metadata `filter` can select documents by any property
# the index carries -- including ones belonging to another agent. The boundary is
# "the model chooses WHAT to look for, never WHERE or HOW MUCH".
FORBIDDEN_PARAMS = (
    "agent_id",
    "agent",
    "namespace",
    "corpus",
    "tenant",
    "user_id",
    "filter",
    "k",
    "index",
)

failures: list[str] = []
checks = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {label}" + (f" -- {detail}" if detail else ""))
    if not ok:
        failures.append(label)


def _forbidden_in(names) -> list[str]:
    lowered = {str(n).lower() for n in names}
    return sorted(lowered & set(FORBIDDEN_PARAMS))


# ---------------------------------------------------------------- T1, T2
def t1_t2_langchain() -> None:
    print("T1/T2 -- the langchain tool schema")
    from app.tools.corpus import SEARCH_CORPUS, SearchCorpusArgs, build_corpus_tool
    from app.tools.registry import ToolContext

    fields = list(SearchCorpusArgs.model_fields.keys())
    check("T1 SearchCorpusArgs has exactly one field", fields == ["query"], str(fields))

    ctx = ToolContext(agent=SimpleNamespace(id="a", namespace="agent_secret"), ledger=None)
    tool = build_corpus_tool(ctx)
    schema = tool.args_schema
    props = (
        list(schema.model_fields.keys())
        if hasattr(schema, "model_fields")
        else list((schema or {}).get("properties", {}).keys())
    )
    check("T2 bound-tool schema exposes only 'query'", props == ["query"], str(props))
    check(
        "T2 bound-tool schema names no tenancy parameter",
        not _forbidden_in(props),
        str(_forbidden_in(props)),
    )
    check("T2 tool name is stable", tool.name == SEARCH_CORPUS, tool.name)


# ---------------------------------------------------------------- T3, T4
def t3_t4_adk() -> None:
    print("T3/T4 -- the ADK declaration, serialised")
    from app.adk.tools import build_adk_tools
    from app.adk.context import AdkTurnContext

    agent = SimpleNamespace(id="a", namespace="agent_THE_SECRET_NAMESPACE")
    ctx = AdkTurnContext(agent=agent, ledger=None)
    tools = build_adk_tools(ctx)

    by_name = {t.name: t for t in tools}
    check("T3 search_corpus tool is present", "search_corpus" in by_name, str(list(by_name)))
    if "search_corpus" not in by_name:
        return

    decl = by_name["search_corpus"]._get_declaration()
    # SERIALISED, not the object. The object is what looks correct; the bytes are
    # what a provider would receive.
    blob = decl.model_dump_json(exclude_none=True)
    parsed = json.loads(blob)
    params = parsed.get("parameters") or parsed.get("parametersJsonSchema") or {}
    props = list((params.get("properties") or {}).keys())

    check("T3 declaration exposes only 'query'", props == ["query"], str(props))
    check(
        "T3 declaration names no tenancy parameter",
        not _forbidden_in(props),
        str(_forbidden_in(props)),
    )
    check(
        "T4 the namespace string appears nowhere in the serialised declaration",
        "THE_SECRET_NAMESPACE" not in blob,
        "leaked" if "THE_SECRET_NAMESPACE" in blob else "",
    )

    # Every tool, not just search: run_python must not grow one either.
    for name, tool in by_name.items():
        d = tool._get_declaration()
        p = json.loads(d.model_dump_json(exclude_none=True))
        pr = (p.get("parameters") or p.get("parametersJsonSchema") or {}).get("properties") or {}
        check(
            f"T4 '{name}' names no tenancy parameter",
            not _forbidden_in(pr.keys()),
            str(_forbidden_in(pr.keys())),
        )


# ---------------------------------------------------------------- T5
def t5_ast() -> None:
    """No tool callable anywhere in backend/app/ takes a tenancy-shaped parameter.

    Reads the APPLICATION'S SOURCE rather than a shape the harness invented. That
    distinction is the whole lesson of `metering_check` case 12: a harness can
    prove the instrumentation it was handed works, and only a source walk can
    prove there is not a second call site nobody wrapped.

    Scoped to the two tool modules plus anything decorated with langchain's
    `@tool`, because a tenancy parameter is only dangerous on a callable the
    MODEL can reach.
    """
    print("T5 -- AST walk over tool callables")
    targets = [APP / "tools", APP / "adk"]
    offenders: list[str] = []
    scanned = 0

    for base in targets:
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError as exc:  # pragma: no cover
                offenders.append(f"{path.name}: unparseable ({exc})")
                continue
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                decorated = any(
                    (isinstance(d, ast.Name) and d.id == "tool")
                    or (isinstance(d, ast.Attribute) and d.attr == "tool")
                    for d in node.decorator_list
                )
                # `_search` / `_run_python` are the inner closures langchain wraps;
                # they are the real model-facing signatures.
                inner = node.name in {"_search", "_run_python", "_run", "run_async"}
                if not (decorated or inner):
                    continue
                scanned += 1
                names = [
                    a.arg
                    for a in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
                    if a.arg not in {"self", "cls", "args", "tool_context"}
                ]
                bad = _forbidden_in(names)
                if bad:
                    offenders.append(f"{path.name}:{node.lineno} {node.name}{bad}")

    check(
        "T5 AST actually found tool callables to scan",
        scanned > 0,
        f"scanned={scanned}",
    )
    check("T5 no tool callable takes a tenancy parameter", not offenders, str(offenders))


# ---------------------------------------------------------------- T6
def t6_agent_tool() -> None:
    """`AgentTool` copies the parent session state into a child agent.

    ADK's `AgentTool.run_async` forwards the whole parent state, filtering only
    `_adk`-prefixed keys -- so `tool_context.state` is NOT a tenancy boundary, and
    an agent-as-a-tool is a hole with a friendly name. Banned outright rather than
    used carefully, because "used carefully" is not a property a harness can check.
    """
    print("T6 -- AgentTool is banned")

    # **AST, not a text scan, and the first draft of this case is why.**
    #
    # A `grep`-shaped check cannot tell a PROHIBITION from a VIOLATION: it fired
    # on the docstrings in `app/adk/tools.py` and `app/adk/context.py` that
    # explain why the class is banned. That is `deck_check.py` case 14 exactly --
    # CLAUDE.md records it as "the check had to be written so that looking for the
    # thing is not doing the thing, because it scans its own file and matched its
    # own source line on the first run".
    #
    # An AST reference is something prose structurally cannot produce, so the ban
    # is now checkable without also banning the explanation of it.
    banned = {"AgentTool"}
    hits: list[str] = []
    scanned = 0
    for path in sorted(APP.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:  # pragma: no cover
            hits.append(f"{path.relative_to(APP)}: unparseable ({exc})")
            continue
        scanned += 1
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in banned:
                hits.append(f"{path.relative_to(APP)}:{node.lineno} name")
            elif isinstance(node, ast.Attribute) and node.attr in banned:
                hits.append(f"{path.relative_to(APP)}:{node.lineno} attribute")
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name in banned:
                        hits.append(f"{path.relative_to(APP)}:{node.lineno} import")

    check("T6 the AST walk actually parsed the package", scanned > 20, f"files={scanned}")
    check("T6 AgentTool is referenced nowhere under app/", not hits, str(hits))


def main() -> int:
    print("tenancy_check -- the namespace is never a model-visible argument")
    print("")
    t1_t2_langchain()
    print("")
    try:
        t3_t4_adk()
    except ImportError as exc:
        # Before app/adk/ exists this is the EXPECTED failure, and it is a failure
        # rather than a skip on purpose: `build.md` phase 4 says write the case,
        # watch it fail, then build. A skip here would go green over nothing.
        check("T3/T4 the ADK tool module is importable", False, f"ImportError: {exc}")
    print("")
    t5_ast()
    print("")
    t6_agent_tool()
    print("")
    if failures:
        print(f"FAILED {len(failures)} of {checks}:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"all {checks} tenancy checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
