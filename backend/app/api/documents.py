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

**Ingest is asynchronous, and this route's work ends at "accepted".** Upload
validates, stages a `pending` `documents` row, commits it and answers **202**
with a row the client can poll; `app/rag/jobs.py` does the loading, splitting,
embedding and upserting afterwards, in a session of its own. Ingest used to run
inline, and that is exactly what held the upload cap at 10 MB -- the request kept
a worker, a database connection and the whole file for the duration, so a large
file did not fail cleanly, it timed out. The cap and the handoff are one decision
with two halves; `app/config.py` spells out why neither is safe alone.

The status code stays 202 even when `ingest_in_background` is false and the work
does happen inside the request. A client cannot see a server setting, and it
polls the row either way -- making the code depend on a flag it has no way to
read would buy accuracy nobody can act on at the price of a contract that shifts
under a redeploy. The body's `status` field is where the truth lives: `pending`
when a job will pick it up, `ready` when the inline path already finished.

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
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, DbSession, OwnedAgent
from app.config import settings
from app.db.models import AuditLog, Chunk, Document, User
from app.rag.delete import delete_document
from app.rag.ingest import INGEST_FAILURE_ACTION, SUPPORTED_SUFFIXES, ingest_bytes
from app.rag.jobs import run_ingest_job

log = logging.getLogger("uvicorn.error")

router = APIRouter(prefix="/api/agents", tags=["documents"])

# The cap, now read from `settings.max_upload_mb` (50 MB) rather than hardcoded
# at 10.
#
# The argument for hardcoding it was: "a limit that can be raised by an
# environment variable will be raised by an environment variable, and the reason
# it is low is a property of how ingest works, not of where the service is
# deployed." That reasoning was right, and it is kept here rather than deleted
# because it is still the reason this number is not a free knob. The property it
# named was that ingest ran INLINE -- the request held a worker, a connection and
# the whole file in memory while it split, embedded and upserted -- so a large
# upload did not fail cleanly, it timed out. The 10 MB was never sizing the
# workload; the entire workshop corpus is ~1.4 MB of text (PRD section 3.2).
#
# What changed is that property, not the opinion about settings. Ingest now hands
# off to `app/rag/jobs.py` and the request no longer waits on it, so the limit
# finally describes the deployment -- memory, embedding spend, how long a user
# will watch a status badge -- which is the only kind of thing an environment
# variable should be allowed to move. The two changes are one decision with two
# names: set `ingest_in_background=false` while this reads 50 MB and the original
# argument applies again in full.
MAX_UPLOAD_BYTES = settings.max_upload_mb * 1024 * 1024

# How long a document may sit at `processing` before a delete stops waiting for
# an ingest to finish behind it. See `remove_document`: within this window a
# delete is refused, because the job is probably still running and deleting under
# it strands vectors in Pinecone permanently; past it the row is treated as
# abandoned, which is what makes the recovery `app/rag/jobs.py` documents --
# delete it and upload again -- actually reachable after a restart or an OOM kill
# left a row that nothing will ever move.
PROCESSING_STALE_AFTER = timedelta(minutes=30)


class DocumentOut(BaseModel):
    """One corpus entry as the UI sees it.

    `status` is the whole state machine and all four values are real:
    `pending` (staged, a job will pick it up), `processing` (a job is running),
    `ready`, `failed`. The first two are deliberately not collapsed into one --
    "queued behind something" and "running for four minutes" are different
    things to be told.

    `error` is the reason a `failed` row failed, and it is null on every other
    row. It is not a column: `documents` has none, so the text is read back out
    of `audit_log` (see `_failure_reasons`). Without it a failure renders as a
    red badge with no cause, which reads to the user as our bug rather than as
    their scanned PDF -- and the difference decides whether they fix it or file
    it.

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
    error: str | None = None
    created_at: datetime


def _document_out(
    document: Document, chunk_count: int, error: str | None = None
) -> DocumentOut:
    """The single construction site for a DocumentOut.

    `chunk_count` is not a column on `documents` -- it is an aggregate over
    `chunks` -- and `error` is not one either; both have to be supplied by the
    caller. Funnelling every route through one constructor is what stops the
    list endpoint and the upload endpoint from disagreeing about the shape they
    return, which is exactly the kind of difference a frontend discovers at
    runtime and nothing else catches.
    """
    return DocumentOut(
        id=document.id,
        filename=document.filename,
        mime_type=document.mime_type,
        byte_size=document.byte_size,
        status=document.status,
        chunk_count=chunk_count,
        error=error,
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


async def _failure_reasons(
    db: AsyncSession, documents: list[Document]
) -> dict[str, str]:
    """Why each `failed` document failed, keyed by stringified document id.

    `documents` has no error column, so `app/rag/ingest.py` records the reason in
    `audit_log` under `INGEST_FAILURE_ACTION` instead. That constant is imported
    rather than retyped: a mismatched string does not raise here, it renders as a
    failed upload with no explanation beside it, which is the one outcome the
    whole mechanism exists to prevent.

    One extra query, and only when something has actually failed -- the common
    case is an all-`ready` corpus and no second round trip. Ordered oldest
    first so that later rows overwrite earlier ones in the dict: a document
    force-re-ingested after a failure accumulates reason rows, and the state
    being explained was produced by the most recent attempt.
    """
    failed = [str(document.id) for document in documents if document.status == "failed"]
    if not failed:
        return {}

    rows = await db.scalars(
        select(AuditLog)
        .where(
            AuditLog.action == INGEST_FAILURE_ACTION,
            AuditLog.resource_type == "document",
            # Scoped by ids taken from documents the caller has already been
            # authorised for. `audit_log` has no tenancy column of its own, so
            # reading it by anything less specific than a known-owned id list
            # would turn this into a cross-tenant read.
            AuditLog.resource_id.in_(failed),
        )
        .order_by(AuditLog.created_at)
    )

    reasons: dict[str, str] = {}
    for row in rows.all():
        message = (row.audit_metadata or {}).get("error")
        if message and row.resource_id:
            reasons[row.resource_id] = str(message)
    return reasons


# --------------------------------------------------------------------------
# Upload
# --------------------------------------------------------------------------

@router.post(
    "/{agent_id}/documents",
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_document(
    agent: OwnedAgent,
    user: CurrentUser,
    db: DbSession,
    background: BackgroundTasks,
    # The PARAMETER NAME is the multipart field name. Renaming it to `upload` or
    # `document` silently changes the API contract and the frontend's FormData
    # key with it, and the failure is a 422 that names a field nobody wrote.
    file: Annotated[UploadFile, File(...)],
    force: bool = False,
) -> DocumentOut:
    """Accept one uploaded file into this agent's corpus. 202, not 201.

    Everything cheap happens now -- reject the wrong type, the oversized, the
    empty and the duplicate, stage a `pending` row, commit -- and everything slow
    happens in `app/rag/jobs.py` afterwards. The response body is the row itself
    so the client has an id to poll before the first chunk has been embedded.

    **The duplicate check stays here, synchronous, ahead of the handoff.** It
    could be left to `ingest_bytes`, which is idempotent by content hash, but a
    202 followed minutes later by a silent no-op is a worse answer than an
    immediate refusal: the user has already been told their file was accepted,
    and nothing that arrives afterwards can unsay it.

    `user` is requested alongside `agent` even though `owned_agent` already
    depends on it. FastAPI caches a dependency per request, so this is a
    dictionary lookup rather than a second session resolution, and it keeps
    `uploaded_by_user_id` and the audit row reading from the same object the
    authorisation check used.
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

    # Hashed once and used twice: to answer the 409 below, and to describe the
    # staged row. `ingest_bytes` re-derives it from the same bytes rather than
    # trusting the row, so this is not a second derivation site that could drift
    # -- it only stops the pending row from claiming nothing about the file it
    # stands for.
    content_hash = hashlib.sha256(data).hexdigest()

    if not force:
        # `ingest_bytes` is idempotent by content hash and, on a repeat, RETURNS
        # THE EARLIER RUN rather than raising. That is right for
        # `scripts/slice_check.py`, which re-walks a corpus directory and wants
        # a no-op -- and wrong over HTTP, where "your file was ingested" and
        # "nothing happened, you already have this" must not share a status
        # code. So the duplicate is detected here instead, with deliberately the
        # same predicate ingest uses (agent + hash + status "ready"); if that
        # predicate ever changes there, it has to change here too.
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

    # One decision with two names, not two knobs -- see `app/config.py`. Read
    # once so the audit row, the handoff and the response cannot disagree about
    # which path this upload took.
    in_background = settings.ingest_in_background

    # THE STAGED ROW. It exists, is committed and is addressable before any slow
    # work starts, which is the whole contract that makes the handover work: the
    # client is handed an id it can poll, and `run_ingest_job` adopts the row
    # rather than creating one, so there is never a `pending` row nothing is
    # going to touch sitting beside a second row quietly going `ready`.
    #
    # `byte_size` and `content_hash` are written now because both are facts about
    # bytes already in hand and the UI shows the size while the row is pending.
    # `mime_type` is left to ingest, which derives it from the extension rather
    # than from the browser's Content-Type header; duplicating that rule here
    # would be a second place for it to drift.
    document = Document(
        id=uuid.uuid4(),
        agent_id=agent.id,
        uploaded_by_user_id=user.id,
        filename=filename,
        byte_size=len(data),
        content_hash=content_hash,
        # `pending`, and NOT `processing`: nothing is running yet. `jobs.py`
        # writes `processing` when the work actually begins, and the two states
        # are kept apart because "queued" and "running" are different answers to
        # a user watching a badge for four minutes.
        status="pending",
    )
    db.add(document)

    # Records the ACCEPTANCE, and that is a change of meaning worth stating.
    # This row used to be written after a completed ingest and carried its chunk
    # count; under the handoff there is no chunk count to know yet. What has
    # definitely happened by the time this commits is that a valid, non-duplicate
    # upload was accepted and staged, so that is what it claims. Completion is
    # recorded elsewhere and durably -- `ingestion_runs` for a success, an
    # `INGEST_FAILURE_ACTION` row for a failure -- so nothing is lost, and both
    # paths write one shape instead of two.
    _audit(
        db,
        user,
        "document.upload",
        document.id,
        agent_id=str(agent.id),
        filename=filename,
        byte_size=len(data),
        forced=force,
        background=in_background,
    )
    await db.commit()

    # Insurance, not bookkeeping. `documents.created_at` is a server default, and
    # an unloaded attribute on an async session refreshes itself with implicit
    # IO, which raises MissingGreenlet -- a 500 whose traceback points at
    # Pydantic serialisation rather than at the column.
    await db.refresh(document)

    if in_background:
        # IDS AND BYTES ONLY. Passing `agent`, `document` or `db` would hand a
        # background task objects belonging to a session FastAPI closes as this
        # request finishes -- see the comment at the top of `app/rag/jobs.py`,
        # which is where that trap is explained and guarded against.
        #
        # Scheduled after the commit, so a task can never be queued against a row
        # that was rolled back. `data` stays resident until the job finishes,
        # which at a 50 MB cap is the real ceiling on concurrent uploads.
        background.add_task(
            run_ingest_job,
            agent.id,
            document.id,
            filename,
            data,
            user.id,
        )
        return _document_out(document, 0)

    # The synchronous fallback: same staged row, same adoption, ingest simply
    # runs before the response instead of after it. Worth keeping because a
    # failure that happens inside the request is a failure a workshop can watch
    # -- `ingest_in_background=false` turns the whole pipeline back into one
    # stack trace -- and because it is the shape a 500 here is easiest to debug
    # in. It is also why the 50 MB cap and the handoff are documented as one
    # decision: this branch is the configuration that makes the cap dangerous.
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
            document_id=document.id,
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

    await db.refresh(document)

    # `run` is not necessarily THIS document's run. When two uploads of the same
    # bytes race past the 409 above, `ingest_bytes` fails the loser and returns
    # the winner's earlier run -- whose chunk count belongs to a different row,
    # and reporting it here would show a healthy chunk total beside a failed
    # document.
    chunk_count = (run.chunk_count or 0) if document.status == "ready" else 0
    return _document_out(document, chunk_count)


# --------------------------------------------------------------------------
# List
# --------------------------------------------------------------------------

@router.get("/{agent_id}/documents")
async def list_documents(agent: OwnedAgent, db: DbSession) -> list[DocumentOut]:
    """This agent's corpus, newest first, each with its chunk count.

    **This is the polling endpoint.** Upload answers 202 and the client watches
    this list for `pending` -> `processing` -> `ready | failed`, so it is called
    repeatedly while anything is still moving and must stay one round trip plus,
    when something has failed, one more for the reasons.

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

    rows = result.all()
    reasons = await _failure_reasons(db, [document for document, _ in rows])
    return [
        _document_out(document, count, reasons.get(str(document.id)))
        for document, count in rows
    ]


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

    **A document that is still being indexed is refused, and the row lock below
    is what makes that answer reliable rather than probable.** See the comments
    on the SELECT and on the 409.
    """
    document = await db.scalar(
        select(Document)
        .where(
            Document.id == doc_id,
            Document.agent_id == agent.id,
        )
        # SELECT ... FOR UPDATE, and it closes a race rather than narrowing one.
        #
        # `app/rag/jobs.py` moves a row `pending` -> `processing` with an UPDATE
        # on exactly this row, so that statement and this SELECT now serialise:
        # either we take the lock first and read `pending`, in which case the
        # job's UPDATE waits, then matches zero rows, raises, and its own
        # handler stops it before a single vector is written -- or the job
        # commits first, we read `processing`, and we refuse below. There is no
        # interleaving left in which this route reads a safe-looking status and
        # the job then goes on to upsert.
        #
        # Without the lock the check underneath is a snapshot: the status can
        # change between reading it and deleting the row, and it is precisely
        # that window that strands vectors (see the 409).
        .with_for_update()
    )
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found",
        )

    # ------------------------------------------------------------------
    # DELETING MID-INGEST IS REFUSED, and this is the one branch here that
    # protects something irreversible.
    #
    # While a job is running, its `chunks` rows are staged in ITS transaction and
    # not yet visible to this one. `delete_document` builds its Pinecone id list
    # from `chunks`, so it would find an empty list, skip the vector call
    # entirely, and delete the row -- and the job would then upsert that
    # document's vectors into the namespace with nothing left anywhere recording
    # that they exist. Unreachable, permanent, and still matching every query
    # this agent makes: exactly the failure `app/rag/delete.py` orders its two
    # steps to prevent, arriving through a different door.
    #
    # `pending` is deliberately NOT refused. A job re-reads the document before
    # it touches anything, finds nothing, logs and stops, so deleting a queued
    # upload is already safe.
    #
    # The age escape hatch is not a hedge. A row can be abandoned at `processing`
    # by a deploy, a restart or an OOM kill, and nothing will ever move it: the
    # original bytes were never stored, so it cannot be resumed, and
    # `app/rag/jobs.py` states the recovery as "delete it and upload again".
    # Refusing forever would make that recovery unreachable and leave the user
    # with an undeletable row. Past PROCESSING_STALE_AFTER we accept the trade
    # openly -- an ingest genuinely still running after half an hour would leave
    # orphaned vectors -- because nothing in this pipeline is designed to take
    # that long, and an undeletable row is a certainty against a possibility.
    # ------------------------------------------------------------------
    if (
        document.status == "processing"
        and datetime.now(timezone.utc) - document.created_at < PROCESSING_STALE_AFTER
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"{document.filename!r} is still being indexed. Deleting it now "
                "would leave its vectors in the index with nothing left to "
                "identify them by. Wait for it to finish or fail, then delete it."
            ),
        )

    # Read before the delete: the row is gone afterwards, and the audit entry
    # needs the filename that no longer exists anywhere else.
    filename = document.filename
    # Recorded because the two are not the same event. Deleting a `ready`
    # document removes vectors; deleting an abandoned `processing` one is a row
    # delete against an ingest that never landed, and only the audit trail will
    # still say which of those happened.
    status_at_delete = document.status

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
        status_at_delete=status_at_delete,
    )
    await db.commit()

    return {"ok": True}
