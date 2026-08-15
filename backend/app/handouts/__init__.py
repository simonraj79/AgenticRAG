"""Handouts: the four recipes, and the job that fills one in.

`new features/04-handouts-panel.md` sections 1 and 2. A handout is a file the
agent made -- a chart, a slide deck, a table or a study sheet -- and it arrives
one of two ways. `origin="tool"` is the agent writing Python mid-conversation
and a file falling out of it; that path belongs to `app/api/ask.py` and the tool
loop. `origin="recipe"` is the user pressing a button in the panel and
describing what they want, and that path is this package.

Three modules, split the way the rest of the backend splits:

    recipes.py   the four recipes, their prompts, and where their material
                 comes from -- data and grounding, no session, no LLM call
    jobs.py      one handout, generated off the request thread -- the same
                 shape as `app/rag/jobs.py` and `app/eval/jobs.py`
    (routes live in `app/api/handouts.py`, beside the other route modules)

Nothing is re-exported here. Import the module you need, so that a route module
asking for `RECIPES` does not drag in the sandbox and the chat model with it.
"""
