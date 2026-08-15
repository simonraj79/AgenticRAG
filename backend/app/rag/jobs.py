"""Ingest, run after the response has already gone out.

`app/api/documents.py` records why the upload cap used to be 10 MB: not to fit
the workload, but because ingest ran inline and held a worker, a connection and
the whole file for its duration. A bigger file under that design did not fail
cleanly, it timed out. This module is the other half of raising that cap --
`settings.max_upload_mb` is only safe because `settings.ingest_in_background`
sends the work here, where taking four minutes is a status badge rather than a
504.

What the route keeps is the fast part: validate, stage a `pending` `documents`
row, commit, answer 201 with something the client can poll. What it hands over
is everything slow. The contract that makes the handover work is that the row
exists and is addressable *before* this function runs, which is why the caller
passes a `document_id` rather than expecting one back.

**The bytes are held in memory until this finishes.** A background task is not a
queue: `data` is a reference the event loop keeps alive after the response is
sent, so N concurrent uploads cost N x filesize of resident memory, and at a
50 MB cap that is the real ceiling on concurrency long before Pinecone or the
embedding quota is. Worth knowing before the cap is raised again.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select

from app.db.models import Agent, Document
from app.db.session import SessionLocal
from app.rag.ingest import ingest_bytes, record_ingest_failure

log = logging.getLogger("uvicorn.error")


async def run_ingest_job(
    agent_id: uuid.UUID,
    document_id: uuid.UUID,
    filename: str,
    data: bytes,
    uploaded_by_user_id: uuid.UUID | None = None,
) -> None:
    """Ingest one already-staged document. Never raises.

    Takes ids and bytes -- no ORM objects, no session. Both halves of that are
    load-bearing and are explained where they are enforced below.
    """
    # ------------------------------------------------------------------
    # THIS FUNCTION OPENS ITS OWN SESSION. It cannot be given one.
    #
    # A FastAPI BackgroundTask runs AFTER the response has been sent, and
    # `get_db` is a generator dependency: FastAPI closes it as the request
    # finishes. So a session captured from the request is already closed by the
    # time this line runs, its connection already returned to the pool and
    # possibly already handed to another request. Using it produces
    # `MissingGreenlet`, "attached to a different loop", or -- worst of the three
    # -- a silent write against a connection someone else is using. None of those
    # errors mention background tasks, and all of them appear at a line that has
    # nothing wrong with it.
    #
    # The same reasoning is why the signature takes `agent_id` rather than an
    # `Agent`: an ORM object belongs to the session that loaded it, and carrying
    # one across is the same bug wearing the shape of an argument. The agent is
    # re-loaded below, inside the session that will actually use it.
    # ------------------------------------------------------------------
    try:
        async with SessionLocal() as db:
            agent = await db.get(Agent, agent_id)
            if agent is None:
                # Deleted between the 201 and this task starting. `documents`
                # cascades from `agents`, so there is very likely no row left to
                # mark either -- and marking a row that is about to vanish would
                # be the only work done here. Log and stop.
                log.warning(
                    "Ingest job for document %s: agent %s no longer exists",
                    document_id,
                    agent_id,
                )
                return

            document = await db.scalar(
                select(Document).where(
                    Document.id == document_id,
                    # Selected on the pair. These two ids arrive as separate
                    # arguments and only the agent has been through the ownership
                    # check in `app/api/deps.py`; selecting on both means a
                    # mismatched pair fetches nothing rather than driving one
                    # tenant's row with another tenant's namespace.
                    Document.agent_id == agent_id,
                )
            )
            if document is None:
                log.warning(
                    "Ingest job: document %s not found under agent %s",
                    document_id,
                    agent_id,
                )
                return

            # pending -> processing, committed on its own before the slow work
            # starts rather than folded into the ingest transaction. The row has
            # to be observable: a client polling during the next several minutes
            # must be able to tell "queued behind something" from "running", and
            # so must an operator looking for the stuck rows described below.
            document.status = "processing"
            await db.commit()

            # ------------------------------------------------------------------
            # WHAT MAKES A STUCK DOCUMENT RECOVERABLE.
            #
            # A row can be left at `processing` with nothing behind it: Render
            # restarts the service, a deploy lands mid-ingest, the worker is
            # OOM-killed. No exception is raised in that case, so nothing below
            # ever marks the row, and `processing` is the worst of the available
            # states precisely because it renders as progress.
            #
            # Such a row can never be RESUMED. The original bytes were never
            # stored (PRD section 7) and existed only as this task's `data`
            # argument, which died with the process. Recovery is therefore always
            # "delete it and upload again", never "retry it", and two properties
            # already hold that make that work:
            #
            #   1. `ingest_bytes`'s dedup matches `status == "ready"` only, so a
            #      stuck row does NOT block re-uploading the same file. This is
            #      the one that matters: without it the stuck row would be a
            #      permanent bar to its own fix.
            #   2. `delete_document` reads its Pinecone id list from `chunks`,
            #      which is empty here, so it skips the vector call entirely and
            #      the delete is a plain row delete. Nothing to fail against a
            #      namespace that was never written.
            #
            # What would close the gap is a sweep at startup flipping
            # `processing` rows older than any plausible ingest to `failed`, so
            # the UI says so rather than the user waiting. That belongs in the
            # app lifespan, which this module does not own.
            # ------------------------------------------------------------------

            # `ingest_bytes` drives the rest of the state machine: processing ->
            # ready, or -> failed with the reason recorded on the way out, and it
            # commits both. Its blocking work -- parse, split, embed, upsert -- is
            # already pushed onto worker threads inside that function, so this
            # await does not pin the event loop for the minutes an ingest can
            # take. That matters more here than anywhere: Render's starter plan
            # runs a single uvicorn worker, and a background task that blocks the
            # loop stalls every request the service is meanwhile trying to serve.
            run = await ingest_bytes(
                db,
                agent,
                filename,
                data,
                uploaded_by_user_id=uploaded_by_user_id,
                # Adopt the row the route already committed instead of inserting
                # a second one. Without this the user would poll a `pending` row
                # that nothing was ever going to touch while a different row
                # quietly went `ready`.
                document_id=document_id,
            )
            log.info(
                "Ingest finished for document %s (%s): %s chunks, run %s",
                document_id,
                filename,
                run.chunk_count,
                run.status,
            )

    except Exception as exc:
        # NOTHING ESCAPES THIS FUNCTION. An exception raised out of a
        # BackgroundTask is not returned to anyone -- the response went out
        # minutes ago -- so at best it lands in the log and at worst it is
        # swallowed by the task machinery. Either way the document stays at
        # `processing` forever, which looks like progress and is the single most
        # confusing outcome available. So: log it with the traceback, then make
        # the row say what happened.
        log.exception("Ingest job failed for document %s (%s)", document_id, filename)
        await _mark_failed(
            document_id,
            str(exc) or exc.__class__.__name__,
            uploaded_by_user_id,
        )


async def _mark_failed(
    document_id: uuid.UUID,
    message: str,
    user_id: uuid.UUID | None,
) -> None:
    """Last resort: force a document to `failed`, from a session of its own.

    A SECOND session, deliberately, and not the one the caller was using. The
    reason we are here may be that the first session is the thing that broke --
    a connection dropped mid-commit leaves it unusable, and every write attempted
    on it afterwards fails too, turning a recorded failure into an unrecorded
    one.

    Does nothing when the row is already `failed`, which is the common case:
    `ingest_bytes` records and commits its own failures, so this fires only for
    the ones that happened outside it -- a bad id pair, a session that died, a
    thread that ran out of memory. That check is what keeps this from writing a
    second, redundant explanation next to every ordinary failure.

    Swallows its own errors too. A failure to record a failure must not become
    the exception that escapes the background task.
    """
    try:
        async with SessionLocal() as db:
            document = await db.get(Document, document_id)
            if document is None or document.status == "failed":
                return

            document.status = "failed"
            record_ingest_failure(db, document, message, user_id=user_id)
            await db.commit()
    except Exception:
        log.exception(
            "Could not mark document %s failed after an ingest error", document_id
        )
