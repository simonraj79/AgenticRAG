"""Agent tools.

`sandbox.py` is the parent half of the code interpreter: static check, spawn,
harvest, cap. `_sandbox_child.py` is the child half and is executed as a
*script*, never imported from here -- it runs in an interpreter that cannot see
this package at all.

Nothing is re-exported. Import the module you need
(`from app.tools import sandbox`) so that pulling in one tool never drags in
matplotlib or python-pptx by accident.

See `new features/02-code-interpreter.md` section 5 for the security contract.
"""
