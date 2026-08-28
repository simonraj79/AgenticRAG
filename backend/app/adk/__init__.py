"""The Google ADK runtime. Nothing outside this package imports `google.adk`.

**This module deliberately imports nothing at module scope.** `app/rag/runtime.py`
selects a runtime with a lazy import inside a branch, so with
`AGENT_RUNTIME=langchain` the `google.adk` package is never loaded into the
process at all. That is what makes the rollback structural rather than argued --
and a convenience re-export here (`from app.adk.loop import run_agent_loop_adk`)
would silently destroy it while every test still passed.

`scripts/adk_model_check.py` A11 asserts the containment; this docstring explains
why the file is empty.
"""
