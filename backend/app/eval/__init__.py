"""Stage 3: golden sets, the Ragas runner, and the scorecard they produce.

Deliberately empty of imports, like `app/rag/__init__.py`. Re-exporting
`suggest_golden_questions` here would pull LangChain, Pinecone and the Gemini
client into the process the moment anything under `app.eval` is touched --
including the API modules that only want a Pydantic response shape. Import from
the submodule that owns the thing.
"""
