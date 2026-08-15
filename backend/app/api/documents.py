"""Documents: put a file into an agent's corpus, list it, take it out again.

PRD section 3.3 for the pipeline, section 7 for the constraint that shapes this
module more than anything else.

**Read this before the code: the word "namespace" does not appear below.** That
is not an omission, it is the design. The Pinecone namespace is derived from
`Agent.namespace` inside `app/rag/retriever.py`, from an `Agent` object this
module obtained through `OwnedAgent` -- loaded by id and checked against the
session user in `app/api/deps.py`. There is no code path here that could accept
a namespace from a body, a query string or a header, because there is no
variable here that holds one. A client-supplied namespace is not an error that
raises; it is a successful cross-tenant read or, on upload, one tenant's chunks
written permanently into another tenant's namespace. Structure is the only
defence that survives someone editing this file in a hurry.

Three further things worth knowing:

**Validation happens here, on the bytes, before ingest sees them.** The size cap
is measured on what was actually read, never on a `Content-Length` header -- a
header is a client's claim about a client's request and costs nothing to forge.

**Ingest is synchronous.** Uploading blocks the request for as long as it takes
to split, embed and upsert -- seconds for a lecture transcript. That is a
deliberate deferral, not an oversight: `scripts/slice_check.py` already works
this way, a task queue is a second process for Render to run, and the workshop
is teaching retrieval rather than job scheduling. It becomes the wrong answer
the moment someone uploads a large PDF over a slow connection.

**`app.rag` owns the writes; this module owns the status codes.** `ingest_bytes`
and `delete_document` commit their own transactions. Everything here either
happens before them (validation, so no row is written for a request we are going
to reject) or after them (the audit row, so the log records what happened rather
than what was attempted).
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime
from pathlib import PurePosixPath
from typing import Annotated, Any

from fastapi import APIRouter, File, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, DbSession, OwnedAgent
from app.db.models import AuditLog, Chunk, Document, User
from app.rag.delete import delete_document
from app.rag.ingest import SUPPORTED_SUFFIXES, ingest_bytes

log = logging.getLogger("uvicorn.error")

router = APIRouter(prefix="/api/agents", tags=["documents"])

# 10 MB. The corpus this was sized against is ~1.4 MB of text in total (PRD
# section 3.2), so the cap is not there to fit the workload -- it is there
# because ingest runs inline and the request holds a worker, a database
# connection and the whole file in memory for its duration. A constant rather
# than a setting: a limit that can be raised by an environment variable will be
# raised by an environment variable, and the reason it is low is a property of
# how ingest works, not of where the service is deployed.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


class DocumentOut(BaseModel):
    """One corpus entry as the UI sees it.

    `content_hash` and `uploaded_by_user_id` are not here. The hash is an
    internal dedup key and the uploader is audit data (PRD section 4.3) that
    carries no access meaning -- publishing it would invite a frontend to start
    treating it as one.
    """

    id: uuid.UUID
    filename: str
    mime_type: str | None = None
    byte_size: int | None = None
    status: str
    chunk_count: int = 0
    created_at: datetime


def _document_out(document: Document, chunk_count: int) -> DocumentOut:
    """The single construction site for a DocumentOut.

    `chunk_count` is not a column on `documents` -- it is an aggregate over
    `chunks` -- so it has to be supplied by the caller. Funnelling both routes
    through one constructor is what stops the list endpoint and the upload
    endpoint from disagreeing about the shape they return, which is exactly the
    kind of difference a frontend discovers at runtime and nothing else catches.
    """
    return DocumentOut(
        id=document.id,
        filename=document.filename,
        mime_type=document.mime_type,
        byte_size=document.byte_size,
        status=document.status,
        chunk_count=chunk_count,
        created_at=document.created_at,
    )


def _suffix_of(filename: str) -> str:
    """The upload's lowercased extension, or "" when it has none.

    `PurePosixPath`, not `Path`, for the reason `app/rag/ingest.py` gives at
    length: `Path` treats a backslash as a separator on Windows and not on
    Linux, so a filename a browser reports as `C:\\docs\\notes.txt` would
    validate differently in local development than on Render. Nothing here ever
    opens the string, so identical behaviour on both platforms is worth more
    than path semantics.
    """
    return PurePosixPath(filename).suffix.lower()


def _audit(
    # `AsyncSession`, not the `DbSession` alias. That alias carries a
    # `Depends(get_db)` inside it, which means something to FastAPI and nothing
    # to a plain helper -- using it here would imply this function is injectable
    # when it is only ever called with a session that already exists.
    db: AsyncSession,
    user: User,
    action: str,
    document_id: uuid.UUID,
    **metadata: Any,
) -> None:
    """Stage one `audit_log` row. Adds only -- the caller owns the commit.

    Written *after* the operation it records, never before. The alternative
    ordering produces a log that claims an upload or a deletion that then failed,
    and a log that lies is worse than one that occasionally under-reports: a
    missing line prompts someone to go and look, a false line stops them.

    Local to this module rather than shared, because it is four lines and this is
    the first caller. If a second route module needs it, that is the moment to
    lift it into `app/api/deps.py` -- not before.
    """
    db.add(
        AuditLog(
            user_id=user.id,
            action=action,
            resource_type="document",
            resource_id=str(document_id),
            # JSONB, so every value has to be JSON-native. UUIDs are stringified
            # at the call sites for that reason. Note the attribute is
            # `audit_metadata`: the column is named `metadata`, which is
            # reserved on a SQLAlchemy declarative class.
            audit_metadata=metadata,
        )
    )


# --------------------------------------------------------------------------
# Upload
# --------------------------------------------------------------------------

@router.post(
    "/{agent_id}/documents",
    status_code=status.HTTP_201_CREATED,
)
async def upload_document(
    agent: OwnedAgent,
    user: CurrentUser,
    db: DbSession,
    # The PARAMETER NAME is the multipart field name. Renaming it to `upload` or
    # `document` silently changes the API contract and the frontend's FormData
    # key with it, and the failure is a 422 that names a field nobody wrote.
    file: Annotated[UploadFile, File(...)],
    force: bool = False,
) -> DocumentOut:
    """Ingest one uploaded file into this agent's corpus.

    `user` is requested alongside `agent` even though `owned_agent` already
    depends on it. FastAPI caches a dependency per request, so this is a
    dictionary lookup rather than a second session resolution, and it keeps
    `uploaded_by_user_id` and the audit row reading from the same object the
    authorisation check used.

    Blocks for the duration of the ingest. See the module docstring.
    """
    filename = (file.filename or "").strip()
    if not filename:
        # HTTP_422_UNPROCESSABLE_CONTENT, not the ..._ENTITY spelling used in
        # `app/auth/routes.py`. Same number, but Starlette 1.6 emits a
        # DeprecationWarning for the old name -- and these branches fire on user
        # error, so the old spelling would print a warning into Render's logs
        # every time somebody picks the wrong file. Same story for 413 below,
        # which is now HTTP_413_CONTENT_TOO_LARGE.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="The uploaded part has no filename.",
        )

    # 415 before reading the body. The extension list comes from
    # `ingest.SUPPORTED_SUFFIXES` rather than being repeated here, because
    # that set is derived from the MIME table that actually selects the parser
    # -- a second hardcoded list would drift the first time a format is added,
    # and it would drift in the direction of accepting files ingest cannot read.
    suffix = _suffix_of(filename)
    if suffix not in SUPPORTED_SUFFIXES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=(
                f"Unsupported file type {suffix or filename!r}. "
                f"Supported: {', '.join(sorted(SUPPORTED_SUFFIXES))}"
            ),
        )

    # A cheap early rejection, deliberately NOT the enforcement point.
    # `UploadFile.size` is counted by Starlette's multipart parser as it writes
    # the part, so it is measured rather than declared -- unlike Content-Length,
    # which is a number the client typed. Checking it here means a 200 MB upload
    # is refused without first pulling 200 MB off a SpooledTemporaryFile and
    # into this process's heap. The authoritative check is still the one below,
    # on the bytes we actually hold.
    if file.size is not None and file.size > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"File exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit.",
        )

    data = await file.read()

    # THE enforcement point: `len(data)`, the bytes in hand. Nothing a client
    # sends -- not Content-Length, not a multipart part header -- is consulted,
    # because all of it is attacker-controlled and none of it is checked against
    # the body by anything upstream of this function.
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"File exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit.",
        )

    if not data:
        # Caught here so it stays a 4xx. An empty file reaches `_load_text`,
        # splits into nothing, and raises inside ingest's try block -- which
        # first writes a `documents` row and an `ingestion_runs` row, marks them
        # failed, commits, and re-raises as a 500. A user error should not leave
        # durable wreckage or look like a server fault.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="The uploaded file is empty.",
        )

    if not force:
        # `ingest_bytes` is idempotent by content hash and, on a repeat, RETURNS
        # THE EARLIER RUN rather than raising. That is right for
        # `scripts/slice_check.py`, which re-walks a corpus directory and wants
        # a no-op -- and wrong over HTTP, where "your file was ingested" and
        # "nothing happened, you already have this" must not share a status
        # code. So the duplicate is detected here instead, with deliberately the
        # same predicate ingest uses (agent + hash + status "ready"); if that
        # predicate ever changes there, it has to change here too.
        content_hash = hashlib.sha256(data).hexdigest()
        existing = await db.scalar(
            select(Document).where(
                # agent_id, not uploaded_by_user_id. The same bytes uploaded to
                # two different agents are two different corpus entries; dedup
                # is per corpus, and the corpus is the agent.
                Document.agent_id == agent.id,
                Document.content_hash == content_hash,
                Document.status == "ready",
            )
        )
        if existing is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"This file is already in the corpus as {existing.filename!r}. "
                    "Re-upload with ?force=true to ingest it again."
                ),
            )

    try:
        # The namespace is NOT passed. `agent` carries it, `get_vector_store`
        # derives it, and this call site never names it -- see the module
        # docstring. `force=True` is honoured because the pre-check above may
        # have been skipped; ingest's own dedup is the backstop for the race
        # where two uploads of the same file arrive together.
        run = await ingest_bytes(
            db,
            agent,
            filename,
            data,
            uploaded_by_user_id=user.id,
            force=force,
            # The browser's Content-Type is a hint -- Markdown routinely arrives
            # as application/octet-stream -- and ingest ignores it in favour of
            # the extension when it is that generic. Passing it straight through
            # is safe and lets a specific type survive.
            mime_type=file.content_type,
        )
    except ValueError as exc:
        # The reachable case is "produced no chunks": a PDF with no text layer,
        # i.e. a scan. That is a real thing a workshop attendee will upload, it
        # is their problem to fix rather than ours, and 500 would tell them
        # nothing. ingest has already marked the document `failed` and
        # committed, so the row survives and the UI can show why.
        log.warning("Ingest produced no usable text for %r: %s", filename, exc)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "No text could be extracted from this file. "
                "A scanned PDF with no text layer is the usual cause."
            ),
        ) from exc

    document = await db.get(Document, run.document_id)
    if document is None:  # pragma: no cover - ingest just committed this row
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Ingest completed but the document row could not be read back.",
        )

    # Insurance, not bookkeeping. `documents.created_at` is a server default, and
    # an unloaded attribute on an async session refreshes itself with implicit
    # IO, which raises MissingGreenlet -- a 500 whose traceback points at
    # Pydantic serialisation rather than at the column. One SELECT after an
    # operation that just spent seconds embedding is free.
    await db.refresh(document)

    _audit(
        db,
        user,
        "document.upload",
        document.id,
        agent_id=str(agent.id),
        filename=document.filename,
        byte_size=document.byte_size,
        chunk_count=run.chunk_count,
        ingestion_run_id=str(run.id),
        forced=force,
    )
    await db.commit()

    return _document_out(document, run.chunk_count or 0)


# --------------------------------------------------------------------------
# List
# --------------------------------------------------------------------------

@router.get("/{agent_id}/documents")
async def list_documents(agent: OwnedAgent, db: DbSession) -> list[DocumentOut]:
    """This agent's corpus, newest first, each with its chunk count.

    The count comes from one grouped subquery joined in, rather than from
    `len(document.chunks)`. The lazy relationship would issue a SELECT per
    document -- N+1, and on an async session each of those is an implicit-IO
    refresh that raises MissingGreenlet rather than merely being slow.

    Aggregating in a subquery and LEFT JOINing it, instead of grouping the
    outer query directly, keeps the aggregate off the entity: the outer SELECT
    stays a plain row-per-document with no GROUP BY over every mapped column.
    Postgres would accept grouping by the primary key alone, but that leans on
    its functional-dependency analysis, and it is a lean that breaks silently
    the day a column is added.

    LEFT, not INNER: a document that is still `processing`, or one whose ingest
    failed, has no chunks yet and must still appear in the list -- that is
    precisely when the user needs to see it.
    """
    chunk_counts = (
        select(
            Chunk.document_id.label("document_id"),
            func.count(Chunk.id).label("chunk_count"),
        )
        .group_by(Chunk.document_id)
        .subquery()
    )

    result = await db.execute(
        select(Document, func.coalesce(chunk_counts.c.chunk_count, 0))
        .outerjoin(chunk_counts, chunk_counts.c.document_id == Document.id)
        # The tenancy filter. `agent` came from `owned_agent`, so this is scoped
        # to a corpus the caller has already been authorised for.
        .where(Document.agent_id == agent.id)
        # Ties are possible: several files uploaded in one batch can share a
        # `created_at` to the microsecond. The id tiebreak makes the order
        # deterministic, so a re-render does not shuffle the list.
        .order_by(Document.created_at.desc(), Document.id)
    )

    return [_document_out(document, count) for document, count in result.all()]


# --------------------------------------------------------------------------
# Delete
# --------------------------------------------------------------------------

@router.delete("/{agent_id}/documents/{doc_id}")
async def remove_document(
    agent: OwnedAgent,
    user: CurrentUser,
    db: DbSession,
    doc_id: uuid.UUID,
) -> dict:
    """Delete one document: its vectors first, then its rows.

    **The agent filter is in the WHERE clause, not in an `if` after the load.**
    The path pair is two client-supplied ids and only one of them has been
    authorised; loading by `doc_id` alone and then comparing would work, but it
    puts a check where a check can be deleted. Selecting on both means a
    document belonging to another agent is not fetched at all, so there is
    nothing for a later edit to forget to compare.

    404 rather than 403 on a cross-agent id, and the distinction is real: this
    route addresses `agent`'s corpus, and within that corpus the document does
    not exist. That the caller may own the *other* agent changes nothing --
    a document must not be reachable through an agent that does not hold it, or
    the route is not agent-scoped and the tenancy boundary is decorative.
    `delete_document` refuses the mismatch as well, but it does so with a
    ValueError, which arrives at the client as a 500.
    """
    document = await db.scalar(
        select(Document).where(
            Document.id == doc_id,
            Document.agent_id == agent.id,
        )
    )
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found",
        )

    # Read before the delete: the row is gone afterwards, and the audit entry
    # needs the filename that no longer exists anywhere else.
    filename = document.filename

    # Vectors then rows, and `delete_document` commits. Skipping it and deleting
    # the row directly would look complete and leave the vectors in Pinecone
    # still matching every query in this namespace, with the only list of what
    # to clean up destroyed along with the `chunks` rows.
    vectors_deleted = await delete_document(db, agent, document)

    _audit(
        db,
        user,
        "document.delete",
        doc_id,
        agent_id=str(agent.id),
        filename=filename,
        vectors_deleted=vectors_deleted,
    )
    await db.commit()

    return {"ok": True}
