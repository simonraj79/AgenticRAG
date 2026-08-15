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

**The expensive work is synchronous and is pushed onto a worker thread here,
once, rather than at each call site.** Parsing a PDF, splitting, counting tokens,
embedding and upserting are all blocking calls; awaiting them directly on the
event loop stops every other request in the process for the duration, and Render
runs a single uvicorn worker. Both doors go through the same two
`asyncio.to_thread` hops, so an ingest started from a background job and one
started from a request block the loop identically -- which is to say, neither
does.

**Failure text goes into `audit_log`, because `documents` has no error column.**
A document left at `failed` with no reason attached is the state a user cannot
act on: it looks like our bug rather than their scanned PDF. `_record_failure`
writes the message under `INGEST_FAILURE_ACTION`, keyed to the document id, in
the table `app/api/documents.py` already logs uploads and deletions into.
"""

from __future__ import annotations

import asyncio
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
from app.db.models import Agent, AuditLog, Chunk, Document, IngestionRun
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

# The `audit_log.action` an ingest failure is recorded under. `documents` has no
# error column, so the reason a document is `failed` lives in
# `audit_log.metadata` instead -- see `_record_failure`. Named once here because
# the writer below and whatever surfaces the reason in the UI must agree on the
# string, and a mismatch is not an error: it is an empty explanation panel.
INGEST_FAILURE_ACTION = "document.ingest_failed"


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


def _build_splitter(
    chunk_size: int, chunk_overlap: int, splitter: str
) -> RecursiveCharacterTextSplitter:
    """The splitter for an agent's configuration.

    Markdown-aware means the separator list splits on headings and fenced code
    before falling back to paragraphs. That matters for this corpus: lecture
    transcripts carry their meaning across whole sections, and a splitter that
    breaks mid-heading decapitates the context a chunk needs to be answerable.

    Takes the three values rather than the `Agent` row it used to, because this
    now runs on a worker thread. Reading an ORM attribute off the event loop can
    trip a lazy refresh, and implicit IO on an async session raises
    MissingGreenlet -- an error that names greenlets and points nowhere near the
    thread that caused it. The caller reads the columns on the loop and passes
    plain values across.
    """
    kwargs = {
        "chunk_size": chunk_size,
        "chunk_overlap": chunk_overlap,
    }
    if splitter == "markdown":
        kwargs["separators"] = RecursiveCharacterTextSplitter.get_separators_for_language(
            Language.MARKDOWN
        )
        kwargs["is_separator_regex"] = True

    return RecursiveCharacterTextSplitter.from_tiktoken_encoder(
        encoding_name=TOKENIZER_ENCODING, **kwargs
    )


def _prepare_chunks(
    filename: str,
    data: bytes,
    *,
    chunk_size: int,
    chunk_overlap: int,
    splitter: str,
) -> list[tuple[str, int]]:
    """Parse, split and token-count. `(text, token_count)` per chunk.

    Every blocking, CPU-bound step in the pipeline is gathered into one function
    so that one `asyncio.to_thread` covers all of it. Splitting them across
    several hops would buy nothing: the loop is only free between awaits, and
    there is no useful await to be had in the middle of parsing a PDF.

    A thread is not a subprocess and the distinction is worth being honest
    about. pypdf is pure Python and holds the GIL, so this makes the event loop
    *responsive* rather than *unaffected* -- other requests get slower while a
    50 MB PDF is parsed, but they are still served. tiktoken releases the GIL,
    so the token counting genuinely runs alongside.

    Raises ValueError when nothing usable comes out, which is the scanned-PDF
    case and reaches the user as a 422 rather than a 500.
    """
    text = _load_text(filename, data)
    pieces = _build_splitter(chunk_size, chunk_overlap, splitter).split_text(text)
    if not pieces:
        raise ValueError(f"{filename} produced no chunks")

    encoding = tiktoken.get_encoding(TOKENIZER_ENCODING)
    return [(piece, len(encoding.encode(piece))) for piece in pieces]


def record_ingest_failure(
    db: AsyncSession,
    document: Document,
    message: str,
    *,
    user_id: uuid.UUID | None,
    ingestion_run_id: uuid.UUID | None = None,
) -> None:
    """Stage the durable reason a document is `failed`. Adds only; caller commits.

    Public rather than private because `app/rag/jobs.py` needs the same writer
    for the failures that happen before this module's own `except` can reach --
    two spellings of an ingest failure would be two shapes for a reader to
    handle.

    `documents` has no error column, so there is nowhere on the row itself to
    put this and adding one is a migration in a file this module does not own.
    `audit_log` is where it goes instead, and it is a better fit than it first
    looks: it already carries `resource_type`/`resource_id` pointing at
    documents, its `metadata` is JSONB so the text needs no column width
    negotiated in advance, and `app/api/documents.py` already writes
    `document.upload` and `document.delete` rows there -- so a reader has one
    table to join for the whole life of a corpus entry.

    `user_id` is nullable on the row and is nullable here: an ingest started by
    `scripts/slice_check.py` has no session user, and a failure with no
    attributed uploader is still worth recording.

    The message is a rendered exception string. It is shown to the user, so it
    must stay a description of *their* file -- "produced no chunks" -- rather
    than a traceback; the traceback goes to the log, where the operator is.
    """
    db.add(
        AuditLog(
            user_id=user_id,
            action=INGEST_FAILURE_ACTION,
            resource_type="document",
            resource_id=str(document.id),
            # JSONB: every value has to be JSON-native, hence the str() on ids.
            # The attribute is `audit_metadata` because the column is named
            # `metadata`, which is reserved on a SQLAlchemy declarative class.
            audit_metadata={
                "error": message,
                "filename": document.filename,
                "ingestion_run_id": str(ingestion_run_id) if ingestion_run_id else None,
            },
        )
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
    document_id: uuid.UUID | None = None,
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

    `document_id` adopts a row the caller already committed instead of inserting
    one. That exists for the background path: an upload that returns 201 before
    ingest starts has to hand the user *something* to poll, so the route stages a
    `pending` row, answers with it, and `app/rag/jobs.py` passes its id back here.
    Left as None -- the request path and `scripts/slice_check.py` -- the row is
    created here exactly as before, so nothing about the synchronous door
    changes.
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

    # Resolve the adopted row first, before the dedup shortcut can return. The
    # shortcut's contract is "write nothing and hand back the earlier run", and
    # that is only harmless while no row is waiting on this call -- once one is,
    # returning early strands it at `processing`.
    document: Document | None = None
    if document_id is not None:
        document = await db.scalar(
            select(Document).where(
                Document.id == document_id,
                # Selected on the pair, not fetched by id and checked after.
                # `document_id` and `agent` arrive here from a background job as
                # two separately-passed values, and a mismatched pair must fetch
                # nothing rather than write one agent's chunks against another
                # agent's row -- the same reason `app/api/documents.py` puts its
                # agent filter in the WHERE clause instead of in an `if`.
                Document.agent_id == agent.id,
            )
        )
        if document is None:
            raise ValueError(
                f"document {document_id} does not belong to agent {agent.id}"
            )

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
                if document is not None and document.id != existing.id:
                    # The adopted row turns out to duplicate one already
                    # ingested. `app/api/documents.py` rejects that with a 409
                    # before scheduling, so reaching here means two uploads of
                    # the same bytes raced -- and the loser must be resolved,
                    # not abandoned. Marked failed with the reason attached: a
                    # row left at `processing` is the worst available state
                    # because the UI renders it as progress and nothing will
                    # ever finish it.
                    document.status = "failed"
                    record_ingest_failure(
                        db,
                        document,
                        f"Already in this corpus as {existing.filename!r}. "
                        "Re-upload with force to ingest it again.",
                        user_id=uploaded_by_user_id,
                    )
                    await db.commit()
                return run

    if document is None:
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
        db.add(document)
    else:
        # Derived values are written onto the adopted row rather than trusted
        # from it: the extension picks the MIME type and the hash is over the
        # bytes actually in hand. A caller that stages a minimal `pending` row is
        # then correct by construction instead of by having remembered to
        # duplicate this derivation. `filename` and `uploaded_by_user_id` are
        # left alone -- those are the caller's to state, not ours to re-derive.
        document.mime_type = mime_type
        document.byte_size = len(data)
        document.content_hash = content_hash
        document.status = "processing"

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
    # `run` only. The document is either already in the session (adopted) or was
    # added above; re-adding a persistent instance is harmless but says the wrong
    # thing about which of the two branches produced it.
    db.add(run)
    await db.flush()

    try:
        # Both hops off the event loop. Parsing, splitting and token counting
        # take seconds on a lecture PDF and minutes on a 50 MB one; embedding and
        # upserting are network calls made by a synchronous client. Awaiting any
        # of it directly would stall the single uvicorn worker Render's starter
        # plan gives us, which means every other request in the process waits on
        # one person's upload.
        prepared = await asyncio.to_thread(
            _prepare_chunks,
            filename,
            data,
            # Read here, on the loop, and handed over as plain values. Reaching
            # through `agent` from the worker thread could trigger a lazy
            # refresh, and implicit IO on an async session raises MissingGreenlet.
            chunk_size=agent.chunk_size,
            chunk_overlap=agent.chunk_overlap,
            splitter=agent.splitter,
        )

        chunks = [
            Chunk(
                id=uuid.uuid4(),
                document_id=document.id,
                chunk_index=i,
                text=text,
                token_count=token_count,
            )
            for i, (text, token_count) in enumerate(prepared)
        ]
        # The Pinecone vector id IS the chunk's primary key, so a search result
        # joins straight back to a `chunks` row with no lookup table.
        for chunk in chunks:
            chunk.pinecone_id = str(chunk.id)
        db.add_all(chunks)

        # Namespace comes from the store, which was bound to `agent.namespace` at
        # construction -- it is never passed in here. Baked into every vector at
        # upsert, so the scheme cannot be changed later without re-ingesting.
        # Constructed on the loop, for the `agent.namespace` read; only the call
        # crosses into the thread.
        store = get_vector_store(agent)

        # `to_thread(store.add_texts)` rather than `await store.aadd_texts(...)`,
        # and the async method does exist on the installed langchain-pinecone.
        # Two reasons not to take it. It embeds through `aembed_documents`, a
        # different code path from the `embed_documents` that `search_with_scores`
        # queries with -- and the one property this index cannot afford to lose is
        # that writes and reads share a vector space, since a mismatch there
        # returns confident nonsense rather than an error. And it opens a second,
        # asyncio Pinecone client per ingest alongside the cached synchronous
        # index in `retriever.py`, whose lifecycle is its own thing to get wrong.
        # A thread runs the exact call this corpus was built with, off the loop,
        # which is all that was actually needed.
        await asyncio.to_thread(
            store.add_texts,
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
    except Exception as exc:
        document.status = "failed"
        run.status = "failed"
        # Staged inside the `except`, so it lands in the same commit the
        # `finally` performs. A `failed` status and the reason for it must not be
        # able to arrive separately: a document that failed with no explanation
        # beside it reads to the user as our bug rather than their scanned PDF,
        # and there is nowhere else the reason survives.
        #
        # `str(exc)` and not the traceback. This string is rendered in the UI, so
        # it has to describe the file; the traceback belongs in the log, where the
        # operator is. Some exceptions stringify to "", hence the class-name
        # fallback -- an empty message is indistinguishable from no message.
        record_ingest_failure(
            db,
            document,
            str(exc) or exc.__class__.__name__,
            user_id=uploaded_by_user_id,
            ingestion_run_id=run.id,
        )
        # Bare re-raise, and the exception TYPE is deliberately unchanged.
        # `app/api/documents.py` catches ValueError to turn "produced no chunks"
        # into a 422; wrapping this in an ingest-specific error would silently
        # demote that to a 500.
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
    # The read is off the loop too, for the same reason the parse is: at the new
    # 50 MB cap this is no longer a trivially fast call, and it is the one piece
    # of blocking work that belongs to this door alone rather than to the shared
    # pipeline. Signature and behaviour are otherwise untouched --
    # `scripts/slice_check.py` calls this and must keep working as it does.
    data = await asyncio.to_thread(path.read_bytes)
    return await ingest_bytes(
        db,
        agent,
        path.name,
        data,
        uploaded_by_user_id=uploaded_by_user_id,
        force=force,
    )
