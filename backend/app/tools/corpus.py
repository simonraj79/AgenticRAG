"""`search_corpus` -- the retriever, handed to the model.

One argument: a search query. The model decides when a second lookup is worth its
latency and what to look up. That is the whole tool.

**It goes through `aretrieve`, never `similarity_search`.**
`app/rag/retriever.py` is the one place a retriever is constructed, and that is
what keeps the Stage 1 -> Stage 2 change a one-liner. A tool reaching for the
vector store directly would bypass reranking and punch through the seam the
entire codebase is organised around -- on day one, and invisibly, because the
answers would still look fine.

------------------------------------------------------------------
WHY THE SCHEMA HAS EXACTLY ONE FIELD.

Three parameters are conspicuously absent and each is a decision:

**No `k`.** The model has no calibrated sense of how many chunks it needs and
would pick a number for the wrong reasons -- more when the question feels hard,
which is not what k means. `agent.retrieve_k` is an operator-tuned parameter that
Stage 3 measures (EVAL.md section 5); letting the model overwrite it per call
would make retrieval parameters unmeasurable, because two runs of the same eval
would not have used the same retrieval configuration.

**No namespace, agent id, or corpus name.** PRD section 7: the namespace comes
from the session, never from the request body. A model that can be
prompt-injected by a retrieved document must not have a parameter that names
another tenant's corpus. The tool closes over the `Agent` object; there is no
argument through which a namespace could arrive.

**No filename filter.** Plausible, and out of scope. It would need metadata
filtering inside `aretrieve`, which is a change to the one place the retriever is
built -- worth doing deliberately rather than as a side effect of adding a tool.
------------------------------------------------------------------
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from langchain_core.documents import Document
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from app.rag.retriever import META_CHUNK_INDEX, META_FILENAME, aretrieve

if TYPE_CHECKING:  # pragma: no cover - import cycle, types only
    from app.tools.registry import ToolContext

SEARCH_CORPUS = "search_corpus"

# How much of each chunk is echoed back in the tool result.
#
# The full text is already in the ledger and therefore in the rebuilt context
# block the model reads on the next step, so repeating it here would double the
# token cost of every search for no new information. What the snippet is for is
# recognition -- enough to tell whether `[7]` is the thing that was missing.
SNIPPET_CHARS = 400


class SearchCorpusArgs(BaseModel):
    """The whole schema. Adding a field to this fails review -- see the module docstring."""

    query: str = Field(
        description=(
            "What to look up. Search for the specific missing thing, not the "
            "whole question again -- the context above already holds the first "
            "search's results."
        )
    )


TOOL_DESCRIPTION = (
    "Search this agent's course material for a specific thing. Use it when the "
    "question has more than one part, when the context you were given does not "
    "cover something you were asked, or when a term in the question does not "
    "appear in the context. Returns numbered chunks whose [n] markers are the "
    "same markers you cite with."
)


def _snippet(doc: Document) -> str:
    """One indented line per chunk, whitespace collapsed and truncated.

    Collapsed because a chunk is prose that soft-wraps, and a multi-line snippet
    under a marker turns the result into something the model has to parse rather
    than scan. The ellipsis is ASCII -- CLAUDE.md records three throwaway scripts
    broken by the Windows console codepage, and a tool result is a string that
    ends up printed by verification scripts as often as it ends up in a prompt.
    """
    text = " ".join(doc.page_content.split())
    if len(text) > SNIPPET_CHARS:
        text = text[:SNIPPET_CHARS].rstrip() + " ..."
    return f"    {text}"


def _label(doc: Document) -> str:
    """`filename#chunk_index`, the same handle the numbered context block uses."""
    filename = doc.metadata.get(META_FILENAME, "unknown")
    index = doc.metadata.get(META_CHUNK_INDEX, "?")
    return f"{filename}#{index}"


def build_corpus_tool(ctx: ToolContext) -> BaseTool:
    """The search tool, bound to one agent and one turn's ledger."""

    # Imported here rather than at module scope: `registry` imports this module
    # to build the tool list, so importing `registry` back at the top would be a
    # cycle. The names below are only needed when a call actually runs.
    from app.tools.registry import ToolOutcome

    async def _search(query: str) -> tuple[str, ToolOutcome]:
        # `rerank=None` follows the agent's own setting. Overriding it here would
        # mean the model's searches were configured differently from the
        # pipeline's first search, and the citation list would then mix chunks
        # chosen by two different retrieval policies.
        retrieval = await aretrieve(ctx.agent, query)

        # Markers BEFORE the merge, so "new" is countable. Everything numbered
        # above this line was assigned by this search.
        held_before = len(ctx.ledger)
        markers = ctx.ledger.merge(retrieval)
        new_markers = [m for m in markers if m > held_before]

        top_score = retrieval.top_score

        if not retrieval.documents:
            # **Not an error, and treating it as one would be a correctness
            # bug.** "The corpus does not cover this" is one of the most valuable
            # things this system can determine, and the refusal behaviour the
            # system prompt asks for depends on the model being able to reach
            # that conclusion from a tool result rather than from a failure.
            payload = (
                f'Searched: "{query}"\n'
                "0 results above the retrieval floor. "
                "The corpus does not appear to cover this."
            )
            return payload, ToolOutcome(
                ok=True,
                summary=f'no matches for "{query}"',
                detail={
                    "query": query,
                    "returned": 0,
                    "new_chunks": 0,
                    "top_score": top_score,
                    "markers": [],
                },
            )

        lines = [
            f'Searched: "{query}"',
            # **The header carries the counts and the top score** because those
            # are the numbers a model reasons about when deciding whether to
            # search again. "3 new" says the search paid for itself; "0 new" says
            # stop.
            f"{len(retrieval.documents)} results, {len(new_markers)} new."
            + (f" Top similarity {top_score:.2f}." if top_score is not None else ""),
            "",
        ]
        for marker, doc in zip(markers, retrieval.documents, strict=True):
            # **Chunks already in context are labelled and shown anyway.** The
            # model needs to see that its new query returned old material -- that
            # is the signal to stop searching. Hiding them produces a loop that
            # searches three times for the same thing, and showing them
            # unlabelled produces an answer that cites the same source twice as
            # though it were two.
            suffix = "   (already in context)" if marker <= held_before else ""
            lines.append(f"[{marker}] {_label(doc)}{suffix}")
            lines.append(_snippet(doc))
            lines.append("")

        return "\n".join(lines).rstrip(), ToolOutcome(
            ok=True,
            summary=(
                f"{len(retrieval.documents)} results, {len(new_markers)} new "
                f'for "{query}"'
            ),
            detail={
                "query": query,
                "returned": len(retrieval.documents),
                "new_chunks": len(new_markers),
                "top_score": top_score,
                "markers": markers,
                "reranked": retrieval.reranked,
            },
        )

    # A retrieval that fails outright -- an embedding quota, a Pinecone outage --
    # raises out of `_search` and is caught by `agent_loop._execute`, which turns
    # it into a TOOL_ERROR the model is told about and can answer around. It is
    # deliberately not caught here: the exception type and message are what a
    # trace wants to record, and re-wrapping them in a friendly string would
    # replace a diagnosis with a sentence.
    return StructuredTool.from_function(
        coroutine=_search,
        name=SEARCH_CORPUS,
        description=TOOL_DESCRIPTION,
        args_schema=SearchCorpusArgs,
        # The string goes to the model; the `ToolOutcome` rides on
        # `ToolMessage.artifact` and becomes the TOOL_RESULT payload.
        response_format="content_and_artifact",
    )
