"""THE SEAM -- the only place a retriever is constructed.

PRD section 3.5. Stage 1 builds a plain similarity retriever; Stage 2 wraps that
same object in a `ContextualCompressionRetriever`. Everything downstream calls
`retriever.invoke(question)` and is byte-identical between the two stages -- but
only for as long as this module stays the single construction site. Call
`similarity_search()` anywhere else and promoting Stage 1 to Stage 2 stops being
a one-line change and becomes a refactor.

Scored retrieval lives here too (`search_with_scores`), for the same reason:
Stage 2's threshold check and the `query_chunks.similarity_score` column both
need scores, and letting them reach for the vector store directly would punch a
hole through the seam on day one.
"""

from __future__ import annotations

from functools import lru_cache

from langchain_classic.retrievers import ContextualCompressionRetriever
from langchain_cohere import CohereRerank
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from langchain_google_genai import GoogleGenerativeAIEmbeddings
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


@lru_cache(maxsize=1)
def get_embeddings() -> GoogleGenerativeAIEmbeddings:
    """The embedding model. One instance, shared.

    Two things are deliberately absent:

    `task_type` is not set. `gemini-embedding-001` had it, and nearly every
    tutorial still passes `task_type="RETRIEVAL_DOCUMENT"` when indexing and
    `"RETRIEVAL_QUERY"` when querying. `gemini-embedding-2` has no such
    parameter. Retrieval intent belongs in the prompt text instead.

    There is no manual L2 normalization. `embedding-001` needed it after MRL
    truncation and silently degraded cosine similarity without it;
    `gemini-embedding-2` renormalizes on its own. Re-adding that step would
    double-normalize.

    `output_dimensionality` must equal the index dimension exactly. The index is
    768d and a mismatched vector is rejected at upsert, which is the one failure
    in this file that is loud rather than silent.
    """
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
        base_compressor=CohereRerank(
            model=settings.rerank_model,
            top_n=agent.rerank_top_n,
            cohere_api_key=settings.cohere_api_key,
        ),
        base_retriever=base,
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
