"""THE SEAM -- the only place a retriever is constructed.

PRD section 3.5. Stage 1 builds a plain similarity retriever; Stage 2 wraps that
same object in a `ContextualCompressionRetriever`. Everything downstream calls
`retriever.invoke(question)` and is byte-identical between the two stages -- but
only for as long as this module stays the single construction site. Call
`similarity_search()` anywhere else and promoting Stage 1 to Stage 2 stops being
a one-line change and becomes a refactor.

Scored retrieval lives here too (`search_with_scores`, `aretrieve`), for the
same reason: Stage 2's threshold check and the `query_chunks.similarity_score`
column both need scores, and letting them reach for the vector store directly
would punch a hole through the seam on day one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

from langchain_classic.retrievers import ContextualCompressionRetriever
from langchain_cohere import CohereRerank
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.retrievers import BaseRetriever
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_openai import OpenAIEmbeddings
from langchain_pinecone import PineconeVectorStore
from pinecone import Pinecone

from app.config import settings
from app.db.models import Agent

# Pinecone metadata keys. `TEXT_KEY` is what PineconeVectorStore reads back into
# `Document.page_content`; the rest are ours. `chunk_id` is the join key back to
# Postgres and is what makes a retrieved vector traceable to a `chunks` row.
TEXT_KEY = "text"
META_CHUNK_ID = "chunk_id"
META_DOCUMENT_ID = "document_id"
META_AGENT_ID = "agent_id"
META_FILENAME = "filename"
META_CHUNK_INDEX = "chunk_index"

# Where CohereRerank writes its score. Verified against langchain_cohere/rerank.py
# (`doc_copy.metadata["relevance_score"] = res["relevance_score"]`), which also
# deepcopies the base metadata -- so `chunk_id` survives the rerank and a
# reranked document still joins back to its Postgres row.
RERANK_SCORE_KEY = "relevance_score"


@lru_cache(maxsize=1)
def get_embeddings() -> Embeddings:
    """The embedding model. One instance, shared. Two roads to one space.

    `embedding_route` picks the gateway. The SPACE is identical either way --
    verified 2026-08-16, three strings through both routes at cosine 1.000000 on
    `embed_documents` AND on `embed_query`, all vectors L2-normalised, against a
    cross-string control of 0.616566 -- which is why `settings.embedding_model`,
    the string stamped onto `agents.embedding_model` and onto every
    `ingestion_runs` row, does not change with it. No re-ingest, no index change.
    `scripts/embed_check.py` is that measurement, kept runnable.

    The return type is the `Embeddings` INTERFACE, not either concrete class.
    Both consumers -- `get_vector_store` below and
    `app/eval/ragas_runner.py:_judge_embeddings` -- only ever call
    `embed_documents` / `embed_query` / `aembed_query`, so widening costs nothing
    and stops the route from leaking into a caller's type.

    THE FOUR KWARGS BELOW ARE NOT STYLE. Each one is a different 400, and you
    only see the next after fixing the one before it.

    `model_kwargs={"encoding_format": "float"}` -- openai-python injects
    `encoding_format="base64"` unconditionally when the caller does not set it
    (`openai/resources/embeddings.py:111-112`), and OpenRouter's Google backend
    answers that with `400 ... do not support base64 encoding_format`. Without
    this line EVERY call fails. This is a fourth distinct OpenRouter parameter
    mechanism, and CLAUDE.md's taxonomy of three did not cover it: not
    unadvertised-and-404, not unadvertised-and-fine, not advertised-then-rejected
    -- INJECTED BY THE CLIENT LIBRARY WITHOUT BEING ASKED FOR. That is now the
    third time langchain-openai/openai-python has done this here, after
    `max_completion_tokens` and `parallel_tool_calls`. It is a property of the
    library, not three coincidences.

    `check_embedding_ctx_length=False` -- the default True routes through
    `_get_len_safe_embeddings`, which tiktoken-encodes the input and sends
    ARRAYS OF INTEGERS (`base.py:560, 624`). Observed on the wire as
    `input[0]=[791, 15690, 13941, ...]` and rejected with `400 Invalid input
    format`. It also means one flag governs both write and read paths, because
    `embed_query` is literally `embed_documents([text])[0]` (`base.py:807`) --
    which is what keeps ingest and query in one space by construction. Do NOT
    reach for `tiktoken_enabled=False` instead: that branch imports
    `transformers.AutoTokenizer`, which is not installed, and raises.

    `chunk_size` -- a hard provider ceiling of 100 inputs per request, not a
    tuning knob. `OpenAIEmbeddings` defaults to 1000 and `langchain-google-genai`
    re-batched at 100 internally, so this is the one kwarg whose absence passes
    every small probe: 25 or 100 texts go in a single request and certify a
    broken config. See `settings.embedding_batch_size` and case 5 of
    `embed_check.py`.

    `dimensions` -- omit it and the model returns its 3072d default (measured).
    The index is 768d. Unlike the chat path, this request carries no `provider`
    block at all, so `require_parameters` never applies to it and `dimensions`
    is not filtered at routing -- verified live, 768d came back. **Do not add a
    provider block here.**

    Two things stay deliberately absent on BOTH routes. There is no `task_type`:
    `gemini-embedding-001` had one and every tutorial still passes
    `RETRIEVAL_DOCUMENT`/`RETRIEVAL_QUERY`, but `gemini-embedding-2` has no such
    constructor parameter and the cosine measurement above shows it is inert on
    the wire too. And there is no manual L2 normalisation: `embedding-001` needed
    it after MRL truncation and silently degraded cosine similarity without it,
    while `gemini-embedding-2` renormalises on its own -- re-adding that step
    would double-normalise. `embed_check.py` case 6 asserts the norm is 1.0 so
    that a re-added step is caught rather than absorbed.
    """
    if settings.embedding_route == "openrouter":
        return OpenAIEmbeddings(
            model=settings.openrouter_embedding_model,
            base_url=settings.openrouter_base_url,
            api_key=settings.openrouter_api_key,
            dimensions=settings.embedding_dimension,
            check_embedding_ctx_length=False,
            chunk_size=settings.embedding_batch_size,
            model_kwargs={"encoding_format": "float"},
            # **A ceiling, not a tidiness.** `OpenAIEmbeddings` defaults to
            # `request_timeout=None`, and as of 2026-08-16 embedding is on the
            # REQUEST hot path for every question asked -- so a stalled
            # OpenRouter connection hangs the turn and its SSE stream with no
            # error and no bound, on a single uvicorn worker that is meanwhile
            # trying to serve everyone else. The chat path has carried this same
            # ceiling since it moved to OpenRouter; this is the half that was
            # missing. Probed rather than assumed: the kwarg constructs cleanly
            # on langchain-openai 1.5.1 and sets `request_timeout=120.0`.
            timeout=settings.openrouter_timeout_s,
        )

    # The rollback, and the only reason `langchain-google-genai` is still
    # installed. `gemini-embedding-2` has no `task_type` in its constructor --
    # but note langchain-google-genai 4.3.4 injects one at CALL time anyway
    # (`RETRIEVAL_DOCUMENT` on embed_documents, `RETRIEVAL_QUERY` on embed_query,
    # embeddings.py:420 and :486). The OpenRouter route sends neither, and the
    # cosine check above was run against BOTH shapes for exactly that reason.
    #
    # `output_dimensionality` must equal the index dimension exactly. The index
    # is 768d and a mismatched vector is rejected at upsert, which is the one
    # failure in this file that is loud rather than silent.
    return GoogleGenerativeAIEmbeddings(
        model=settings.embedding_model,
        google_api_key=settings.gemini_api_key,
        output_dimensionality=settings.embedding_dimension,
    )


@lru_cache(maxsize=1)
def _index():
    """The Pinecone index handle. Cached -- constructing it opens a connection."""
    return Pinecone(api_key=settings.pinecone_api_key).Index(settings.pinecone_index_name)


def get_vector_store(agent: Agent) -> PineconeVectorStore:
    """The vector store bound to one agent's namespace.

    Takes the `Agent` object rather than a namespace string, and that is the
    whole point: the namespace is derived server-side from `Agent.namespace`,
    so there is no parameter anywhere in this module through which a
    client-supplied namespace could reach Pinecone. PRD section 3.2 requires
    that isolation to be structural rather than remembered.
    """
    return PineconeVectorStore(
        index=_index(),
        embedding=get_embeddings(),
        namespace=agent.namespace,
        text_key=TEXT_KEY,
    )


def _build_compressor(agent: Agent) -> CohereRerank:
    """The reranker. One construction site, two consumers.

    `build_retriever` hands this to `ContextualCompressionRetriever`;
    `aretrieve` calls `acompress_documents` on it directly. Those are the same
    operation -- `ContextualCompressionRetriever` does nothing but run the
    compressor over whatever its base retriever returned -- and they are written
    against one constructor so that changing the model or `top_n` cannot fix one
    path and quietly miss the other.
    """
    return CohereRerank(
        model=settings.rerank_model,
        top_n=agent.rerank_top_n,
        cohere_api_key=settings.cohere_api_key,
    )


def build_retriever(agent: Agent, *, rerank: bool | None = None) -> BaseRetriever:
    """Build the retriever for an agent.

    `rerank=False` is Stage 1: embed, search, return top-k.
    `rerank=True` is Stage 2's retrieval half: search top-k, then compress to
    `agent.rerank_top_n` with Cohere.

    Passing `rerank` overrides the agent's own setting, which is what lets the
    UI show the same question answered both ways. Left as None it follows
    `agent.rerank_enabled`.
    """
    if rerank is None:
        rerank = agent.rerank_enabled

    base = get_vector_store(agent).as_retriever(
        search_kwargs={"k": agent.retrieve_k},
    )
    if not rerank:
        return base

    return ContextualCompressionRetriever(
        base_compressor=_build_compressor(agent),
        base_retriever=base,
    )


@dataclass
class Retrieval:
    """One retrieval pass, with every score it produced still attached.

    `documents` is the FINAL context set -- post-rerank when reranking ran --
    and `scored` is the pre-rerank candidate list carrying Pinecone's cosine
    similarity. Both are kept because neither can be derived from the other and
    they answer different questions: `query_chunks.similarity_score` and PRD
    3.5's threshold check need the first-pass numbers, while the prompt and the
    citation list need the final set. A rerank that lifts a chunk from rank 18
    to rank 1 is only visible if both orderings survive, and that promotion IS
    the Stage 2 demo.
    """

    query: str
    documents: list[Document] = field(default_factory=list)
    scored: list[tuple[Document, float]] = field(default_factory=list)
    reranked: bool = False

    @property
    def top_score(self) -> float | None:
        """Best first-pass similarity, or None when nothing came back.

        Higher is closer -- see `search_with_scores` for why this must not be
        inverted. This is the number PRD 3.5's `< 0.5 -> rewrite` compares.
        """
        return float(self.scored[0][1]) if self.scored else None

    @property
    def similarity_scores(self) -> list[float]:
        """First-pass cosine scores, aligned to `scored` (NOT to `documents`).

        Reranking reorders and truncates, so position i of `documents` is not
        position i of `scored` once it has run. Join the two through
        `chunk_id` metadata rather than by index.
        """
        return [float(score) for _, score in self.scored]

    @property
    def rerank_scores(self) -> list[float | None]:
        """Cohere's scores, aligned to `documents`.

        All None when reranking did not run, and that is data rather than a
        gap: a Stage 1 answer genuinely has a similarity score and no rerank
        score. Showing the pair side by side is what makes the reranking
        demo legible.
        """
        return [
            float(raw) if (raw := doc.metadata.get(RERANK_SCORE_KEY)) is not None
            else None
            for doc in self.documents
        ]


async def aretrieve(
    agent: Agent, query: str, *, rerank: bool | None = None, k: int | None = None
) -> Retrieval:
    """Retrieve once, keeping the scores. The path the pipeline uses.

    **This exists because `BaseRetriever` has nowhere to put a score.** Its
    interface returns `list[Document]`, so anything needing
    `query_chunks.similarity_score` -- or Stage 2's threshold, which branches on
    the top score -- had to search a second time for numbers the first search
    already paid for. `app/api/ask.py` did exactly that, and CLAUDE.md prices
    the duplicate at embed 365 ms + Pinecone k=20 394 ms per question.

    `build_retriever` above is unchanged and is still THE seam for the Stage 1
    -> Stage 2 story. This is the same two steps in the same order against the
    same objects -- `get_vector_store` then `_build_compressor` -- with the
    scores kept instead of dropped on the floor. It is a decomposition of that
    retriever, not a second implementation of it, which is what stops the two
    from drifting into answering differently.

    Async rather than sync-in-a-threadpool: `asimilarity_search_with_score`
    awaits a genuine asyncio Pinecone client (`_IndexAsyncio`), so the event
    loop stays free for the length of the index query rather than merely being
    handed off to a worker thread. Cohere has no async client, so
    `acompress_documents` runs it in an executor -- the same thing a threadpool
    would do, done once by the library instead of at every call site.
    """
    if rerank is None:
        rerank = agent.rerank_enabled

    scored = await get_vector_store(agent).asimilarity_search_with_score(
        query, k=k or agent.retrieve_k
    )
    documents = [doc for doc, _ in scored]

    # `and documents`: reranking an empty list is a network call that cannot
    # change anything, and `reranked` is recorded on the result, so claiming a
    # rerank happened when there was nothing to rerank would put a RERANK step
    # in a trace whose before/after lists are both empty.
    reranked = bool(rerank and documents)
    if reranked:
        # list(), because CohereRerank returns a Sequence and everything
        # downstream indexes, slices and enumerates it.
        documents = list(
            await _build_compressor(agent).acompress_documents(documents, query)
        )

    return Retrieval(
        query=query, documents=documents, scored=scored, reranked=reranked
    )


def search_with_scores(
    agent: Agent, query: str, k: int | None = None
) -> list[tuple[Document, float]]:
    """Retrieval with similarity scores attached.

    Pinecone's cosine metric returns higher-is-closer, so PRD section 3.5's
    `top score < 0.5 -> rewrite` transfers with no inversion. Do not "fix" this
    by subtracting from 1: that would silently invert the Stage 2 rewrite
    trigger, making it fire on good matches and stay quiet on bad ones.
    """
    return get_vector_store(agent).similarity_search_with_score(
        query, k=k or agent.retrieve_k
    )
