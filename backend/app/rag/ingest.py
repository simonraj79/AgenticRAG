"""Ingestion: load -> split -> embed -> upsert, and the rows that record it.

PRD section 3.3. One file in, one `ingestion_runs` row out. The Postgres writes
are not bookkeeping: `chunks.text` is the source of truth for chunk text, which
is what makes a later dimension or embedding-model change a re-embed from the
database rather than a re-parse of files we deliberately do not keep. And
`ingestion_runs.embedding_model` is what makes a model mismatch *detectable* --
querying an index with a different model than it was built with returns
confident nonsense, never an error, so the only defence is a recorded value to
compare against.

There are two doors into one pipeline. `ingest_bytes` is the primary entry
point, because that is the shape uploads actually arrive in -- an HTTP upload is
bytes in memory and never touches disk, which is what "we do not store original
files" means in practice on an ephemeral filesystem. `ingest_file` reads a path
and delegates. Everything after the read is shared, deliberately: a second copy
of the split/embed/upsert sequence is a second place for the two to drift apart
and start producing differently-chunked vectors for the same document.
"""

from __future__ import annotations

import hashlib
import io
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

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

# Extension -> MIME type, and the same mapping is the supported-format list, so
# adding a format cannot leave the two out of step. The previous version labelled
# every non-PDF as "text/markdown". That was harmless while ingest only ever ran
# over the workshop corpus of .md files, and wrong the moment the upload endpoint
# starts accepting .txt: `documents.mime_type` is what the UI renders and what any
# later content-type-sensitive handling would branch on.
MIME_TYPES = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".pdf": "application/pdf",
}

SUPPORTED_SUFFIXES = frozenset(MIME_TYPES)


def _suffix_of(filename: str) -> str:
    """The validated, lowercased extension of an upload.

    `PurePosixPath` rather than `Path` because `Path` is platform-dependent: on
    Windows it treats a backslash as a separator and on Linux it does not, so a
    filename a browser reports as `C:\\docs\\notes.txt` would parse differently
    in local development than on Render. This only ever splits a display string
    -- the filename never reaches a filesystem call -- so a stable, identical
    answer on both platforms is worth more than path semantics.

    Raises rather than guessing. The caller validates before writing any rows,
    so an unsupported upload is rejected outright instead of leaving a `failed`
    document row behind for the user to clean up.
    """
    suffix = PurePosixPath(filename).suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ValueError(
            f"Unsupported file type {suffix or filename!r}; "
            f"expected one of {sorted(SUPPORTED_SUFFIXES)}"
        )
    return suffix


def _load_text(filename: str, data: bytes) -> str:
    """Extract plain text from an upload's raw bytes.

    Bytes, not a path. Original uploads are never stored (PRD section 7) and
    Render's disk is ephemeral, so there is no file to read -- and pypdf accepts
    any file-like object, which removes the only reason a temporary file was ever
    needed to reach it. The filename is here purely to select the parser.
    """
    if _suffix_of(filename) == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        return "\n\n".join(page.extract_text() or "" for page in reader.pages)

    # errors="replace", not the default "strict". Real uploads carry stray
    # non-UTF-8 bytes -- a cp1252 smart quote pasted into an otherwise-UTF-8 file
    # is the common one -- and strict decoding turns that single byte into a
    # UnicodeDecodeError that rejects the whole document. The trade is not close:
    # a replacement character costs at most a word of retrieval quality in one
    # chunk, while a failed upload costs the entire corpus entry, and the user
    # has no way to diagnose or fix it from the browser.
    return data.decode("utf-8", errors="replace")


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


async def ingest_bytes(
    db: AsyncSession,
    agent: Agent,
    filename: str,
    data: bytes,
    *,
    uploaded_by_user_id: uuid.UUID | None = None,
    force: bool = False,
    mime_type: str | None = None,
) -> IngestionRun:
    """Ingest one document, given its bytes, into one agent's namespace.

    The whole pipeline lives here and nowhere else; `ingest_file` is a wrapper
    that supplies the bytes from disk.

    Idempotent by content hash: re-ingesting unchanged bytes is a no-op unless
    `force=True`, matching the convention the provisioning scripts follow. Note
    that the hash is over content only, so the same file uploaded twice under
    different names is correctly recognised as already ingested.

    `uploaded_by_user_id` is audit only. It is deliberately not the scoping key --
    `documents.agent_id` is, because a user owns several agents and each must
    retrieve only its own corpus.

    `filename` is a display and citation string: it is stored on the row and
    copied into Pinecone metadata, and it is never used to open anything, so it
    needs no path sanitising.
    """
    # Validate before any row exists. `_load_text` would reject an unsupported
    # type too, but only from inside the try block below, which would first have
    # written a `documents` row and an `ingestion_runs` row and would then mark
    # them failed -- durable clutter for an upload we never even attempted.
    suffix = _suffix_of(filename)

    # The browser's Content-Type is a hint, not the truth: it comes from an OS
    # registry lookup on the extension, so Markdown routinely arrives as the
    # generic application/octet-stream. The extension is what actually selects
    # the parser above, so a derived type is the one value that cannot disagree
    # with how the bytes were read. A caller's value is kept only when it is
    # more specific than the catch-all.
    if not mime_type or mime_type == "application/octet-stream":
        mime_type = MIME_TYPES[suffix]

    content_hash = hashlib.sha256(data).hexdigest()

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
        filename=filename,
        mime_type=mime_type,
        byte_size=len(data),
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
        text = _load_text(filename, data)
        pieces = _build_splitter(agent).split_text(text)
        if not pieces:
            raise ValueError(f"{filename} produced no chunks")

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


async def ingest_file(
    db: AsyncSession,
    agent: Agent,
    path: Path,
    *,
    uploaded_by_user_id: uuid.UUID | None = None,
    force: bool = False,
) -> IngestionRun:
    """Ingest one file from disk. A thin door onto `ingest_bytes`.

    Kept with its original signature because `scripts/slice_check.py` walks the
    corpus directory, and because reading a local file is genuinely a different
    source from an HTTP upload. The difference ends at the read: everything past
    it is the shared pipeline, so a file and an upload of the same bytes split
    into the same chunks and hash to the same `content_hash` -- which is what
    lets the two paths deduplicate against each other rather than each ingesting
    its own copy. (Vector ids differ between runs; they are fresh UUIDs per
    chunk row, and the hash is what dedup keys on.)
    """
    path = Path(path)
    return await ingest_bytes(
        db,
        agent,
        path.name,
        path.read_bytes(),
        uploaded_by_user_id=uploaded_by_user_id,
        force=force,
    )
