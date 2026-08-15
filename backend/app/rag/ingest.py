"""Ingestion: load -> split -> embed -> upsert, and the rows that record it.

PRD section 3.3. One file in, one `ingestion_runs` row out. The Postgres writes
are not bookkeeping: `chunks.text` is the source of truth for chunk text, which
is what makes a later dimension or embedding-model change a re-embed from the
database rather than a re-parse of files we deliberately do not keep. And
`ingestion_runs.embedding_model` is what makes a model mismatch *detectable* --
querying an index with a different model than it was built with returns
confident nonsense, never an error, so the only defence is a recorded value to
compare against.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from pathlib import Path

import tiktoken
from langchain_text_splitters import Language, RecursiveCharacterTextSplitter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Agent, Chunk, Document, IngestionRun
from app.rag.retriever import (
    META_AGENT_ID,
    META_CHUNK_ID,
    META_CHUNK_INDEX,
    META_DOCUMENT_ID,
    META_FILENAME,
    get_vector_store,
)

# Chunk sizes in the PRD and the database are denominated in TOKENS (800/120).
# Splitting needs a tokenizer, and cl100k_base is OpenAI's, not Gemini's -- it
# is an approximation, used for sizing only. It is close enough that an 800-token
# target lands within a few percent on English prose, and it costs no API call.
# The alternative, Gemini's own count_tokens endpoint, would mean a network round
# trip per split candidate.
TOKENIZER_ENCODING = "cl100k_base"

SUPPORTED_SUFFIXES = {".md", ".markdown", ".txt", ".pdf"}


def _load_text(path: Path) -> str:
    """Read a file to plain text.

    Only the formats the workshop corpus actually contains. Original uploads are
    never stored (PRD section 7), so this runs once per file, at ingest.
    """
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        return "\n\n".join(page.extract_text() or "" for page in reader.pages)
    if suffix in SUPPORTED_SUFFIXES:
        return path.read_text(encoding="utf-8")
    raise ValueError(f"Unsupported file type {suffix!r}; expected one of {sorted(SUPPORTED_SUFFIXES)}")


def _build_splitter(agent: Agent) -> RecursiveCharacterTextSplitter:
    """The splitter for an agent's configuration.

    Markdown-aware means the separator list splits on headings and fenced code
    before falling back to paragraphs. That matters for this corpus: lecture
    transcripts carry their meaning across whole sections, and a splitter that
    breaks mid-heading decapitates the context a chunk needs to be answerable.
    """
    kwargs = {
        "chunk_size": agent.chunk_size,
        "chunk_overlap": agent.chunk_overlap,
    }
    if agent.splitter == "markdown":
        kwargs["separators"] = RecursiveCharacterTextSplitter.get_separators_for_language(
            Language.MARKDOWN
        )
        kwargs["is_separator_regex"] = True

    return RecursiveCharacterTextSplitter.from_tiktoken_encoder(
        encoding_name=TOKENIZER_ENCODING, **kwargs
    )


async def ingest_file(
    db: AsyncSession,
    agent: Agent,
    path: Path,
    *,
    uploaded_by_user_id: uuid.UUID | None = None,
    force: bool = False,
) -> IngestionRun:
    """Ingest one file into one agent's namespace.

    Idempotent by content hash: re-ingesting an unchanged file is a no-op unless
    `force=True`, matching the convention the provisioning scripts follow.

    `uploaded_by_user_id` is audit only. It is deliberately not the scoping key --
    `documents.agent_id` is, because a user owns several agents and each must
    retrieve only its own corpus.
    """
    path = Path(path)
    raw = path.read_bytes()
    content_hash = hashlib.sha256(raw).hexdigest()

    if not force:
        existing = await db.scalar(
            select(Document).where(
                Document.agent_id == agent.id,
                Document.content_hash == content_hash,
                Document.status == "ready",
            )
        )
        if existing is not None:
            run = await db.scalar(
                select(IngestionRun)
                .where(IngestionRun.document_id == existing.id)
                .order_by(IngestionRun.created_at.desc())
            )
            if run is not None:
                return run

    document = Document(
        id=uuid.uuid4(),
        agent_id=agent.id,
        uploaded_by_user_id=uploaded_by_user_id,
        filename=path.name,
        mime_type="application/pdf" if path.suffix.lower() == ".pdf" else "text/markdown",
        byte_size=len(raw),
        content_hash=content_hash,
        status="processing",
    )
    run = IngestionRun(
        id=uuid.uuid4(),
        document_id=document.id,
        # Recorded per run, not read from settings at query time. If the setting
        # later changes, this row still says what these vectors were built with.
        embedding_model=settings.embedding_model,
        embedding_dimension=settings.embedding_dimension,
        chunk_size=agent.chunk_size,
        chunk_overlap=agent.chunk_overlap,
        started_at=datetime.now(timezone.utc),
        status="running",
    )
    db.add_all([document, run])
    await db.flush()

    try:
        text = _load_text(path)
        pieces = _build_splitter(agent).split_text(text)
        if not pieces:
            raise ValueError(f"{path.name} produced no chunks")

        encoding = tiktoken.get_encoding(TOKENIZER_ENCODING)
        chunks = [
            Chunk(
                id=uuid.uuid4(),
                document_id=document.id,
                chunk_index=i,
                text=piece,
                token_count=len(encoding.encode(piece)),
            )
            for i, piece in enumerate(pieces)
        ]
        # The Pinecone vector id IS the chunk's primary key, so a search result
        # joins straight back to a `chunks` row with no lookup table.
        for chunk in chunks:
            chunk.pinecone_id = str(chunk.id)
        db.add_all(chunks)

        # Namespace comes from the store, which was bound to `agent.namespace` at
        # construction -- it is never passed in here. Baked into every vector at
        # upsert, so the scheme cannot be changed later without re-ingesting.
        get_vector_store(agent).add_texts(
            texts=[c.text for c in chunks],
            ids=[c.pinecone_id for c in chunks],
            metadatas=[
                {
                    META_CHUNK_ID: str(c.id),
                    META_DOCUMENT_ID: str(document.id),
                    META_AGENT_ID: str(agent.id),
                    META_FILENAME: document.filename,
                    META_CHUNK_INDEX: c.chunk_index,
                }
                for c in chunks
            ],
        )

        document.status = "ready"
        run.chunk_count = len(chunks)
        run.status = "succeeded"
        # Stamped only on the success path, so an agent whose embedding_model is
        # set is an agent that actually has vectors under that model.
        agent.embedding_model = settings.embedding_model
        agent.status = "ready"
    except Exception:
        document.status = "failed"
        run.status = "failed"
        raise
    finally:
        run.finished_at = datetime.now(timezone.utc)
        await db.commit()

    return run
