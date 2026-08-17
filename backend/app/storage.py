"""Object storage. **The only place an S3 client is constructed.**

The third application of the seam idiom in this project, and it is deliberate
rather than stylistic. `app/rag/retriever.py` is the only place the retriever and
the embedder are built, which is what keeps the Stage 1 -> Stage 2 change a
one-liner; `app/rag/llm.py` is the only place a chat model is built, which is
what let every model call move to OpenRouter by editing one function. This module
is the same bet for bytes: `storage_route` flips in one branch, in one file,
rather than as a sweep across two writers, three delete paths and a download
route.

**Nothing here calls AWS.** Cloudflare R2 speaks the S3 protocol and boto3 is an
S3-protocol client; it is pointed at `<account>.r2.cloudflarestorage.com` and no
`AWS_*` environment variable is read -- the credentials are passed explicitly,
from `R2_*` settings named that way precisely so boto3's ambient credential chain
cannot pick up something else by accident. This is the same relationship
`langchain-openai` has with OpenRouter, where CLAUDE.md's note that "nothing calls
OpenAI and no OPENAI_API_KEY is needed or exists" has held through two model
migrations.

**Keys are DERIVED, never supplied.** Every function below that names an object
takes ids -- `uuid.UUID` -- and builds the key itself. There is no parameter
through which a caller, or a prompt-injected model, can name an object belonging
to another agent. That is the same structural control as `Agent.namespace` being
a derived property and `SearchCorpusArgs` carrying exactly one field: the
capability is absent rather than guarded.

**These functions are BLOCKING.** boto3 is synchronous, exactly like the Pinecone
and Cohere clients, and this module follows the project's existing arrangement
rather than inventing a second one: background jobs wrap their calls in
`asyncio.to_thread`, and the in-request call sites are the standing deferral
recorded as PRD open item 19. Do not add an async facade here that hides which is
which.
"""

from __future__ import annotations

import logging
import re
import uuid
from functools import lru_cache

from botocore.config import Config

from app.config import settings

log = logging.getLogger(__name__)

# Extension appended to a key, chosen from the mime type rather than from the
# filename. The filename is model-written for a handout and user-supplied for a
# document, so it is exactly the string that must not reach a key; the mime type
# is set by `sandbox.HARVEST_MIME` or `ingest.MIME_TYPES`, both of which are
# tables this repo controls.
#
# The extension is cosmetic -- it exists so a human reading the bucket can tell a
# deck from a chart. An unknown mime yields no suffix rather than a guess.
_EXT_BY_MIME = {
    "image/png": ".png",
    "image/svg+xml": ".svg",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "text/csv": ".csv",
    "text/markdown": ".md",
    "text/plain": ".txt",
    "application/json": ".json",
    "application/pdf": ".pdf",
}


class StorageError(RuntimeError):
    """A storage operation failed in a way the caller must handle.

    Deliberately one class rather than a hierarchy. Every caller does the same
    thing with it -- records `error_kind="storage"` and moves on -- and the
    provider's own message is preserved in the string, which is the part anyone
    debugging actually reads.
    """


def enabled() -> bool:
    """True when bytes belong in R2 rather than in Postgres."""
    return settings.storage_route == "r2"


@lru_cache(maxsize=1)
def get_client():
    """The S3 client, built once.

    `signature_version="s3v4"` is not optional and not a default -- R2 rejects
    SigV2 -- and `region_name="auto"` is R2's documented value, there being no
    region to name. Both are passed explicitly rather than left to botocore's
    resolution chain, because that chain reads `~/.aws/config` and environment
    variables this project does not set and does not want consulted.

    Cached because a boto3 client is expensive to construct and thread-safe to
    share. `lru_cache` rather than a module global for the same reason
    `get_embeddings` uses one: a test can clear it.
    """
    if not settings.r2_endpoint_url:
        raise StorageError(
            "No R2 endpoint. Set R2_ACCOUNT_ID (or R2_ENDPOINT), or set "
            "STORAGE_ROUTE=postgres."
        )

    import boto3

    return boto3.client(
        "s3",
        endpoint_url=settings.r2_endpoint_url,
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=settings.r2_secret_access_key,
        region_name="auto",
        config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
    )


def _ext_for(mime_type: str | None) -> str:
    return _EXT_BY_MIME.get((mime_type or "").split(";")[0].strip().lower(), "")


def agent_prefix(agent_id: uuid.UUID) -> str:
    """Every object belonging to one agent, under one prefix.

    This is what makes agent deletion possible at all. `api/agents.py` deletes an
    agent with a Core DELETE so that Postgres performs the cascade in one
    statement, and there is no `relationship()` on `Handout` anywhere -- so no
    Python ever sees the rows, and nothing could iterate them to delete their
    objects. A prefix delete needs no such iteration, which is the same reason
    `delete_agent_namespace` can clear Pinecone with `delete_all=True`.
    """
    return f"agents/{agent_id}/"


def handout_key(agent_id: uuid.UUID, handout_id: uuid.UUID, mime_type: str | None) -> str:
    """The key for one handout's bytes.

    Both ids are `uuid.UUID`, so a string from a request body cannot reach this
    function without first surviving FastAPI's parsing into a UUID -- which is
    the point. `handout_id` is generated in Python before the row is inserted, so
    the key is knowable BEFORE the row exists, and that is what makes the
    object-first-then-row write ordering possible.
    """
    return f"{agent_prefix(agent_id)}handouts/{handout_id}{_ext_for(mime_type)}"


def document_key(agent_id: uuid.UUID, document_id: uuid.UUID, mime_type: str | None) -> str:
    """The key for one uploaded document's ORIGINAL bytes."""
    return f"{agent_prefix(agent_id)}documents/{document_id}{_ext_for(mime_type)}"


def put_object(key: str, content: bytes, mime_type: str | None) -> None:
    """Write bytes. Raises `StorageError`; never returns a status to check.

    `ContentType` is stored on the object as well as being overridden at presign
    time. The override is what the browser actually sees, so this is redundant --
    and it is kept because a bucket a human is browsing in the Cloudflare console
    should not present every file as `application/octet-stream`.
    """
    try:
        get_client().put_object(
            Bucket=settings.r2_bucket,
            Key=key,
            Body=content,
            ContentType=mime_type or "application/octet-stream",
        )
    except Exception as exc:  # noqa: BLE001
        raise StorageError(f"Could not store {key!r}: {exc}") from exc


def _disposition_safe(filename: str) -> str:
    """Reduce a filename to characters that cannot escape a quoted header value.

    An allowlist, not a denylist, and the same one `api/handouts._safe` applies
    -- deliberately duplicated rather than imported, because the direction of
    that import would be wrong. `app/api` may depend on `app/storage`; the seam
    must not depend on a route module, for the same reason `agent_loop` cannot
    import from `app.api` and the refusal markers moved to `app/rag/refusal.py`.

    **Applied here rather than trusted from the caller**, which is the whole
    point. The value travels as `ResponseContentDisposition` and R2 returns it
    verbatim as a `Content-Disposition` header, so a model-written filename
    reaching this parameter is header injection exactly as it would be reaching
    the header directly. Before this change the sanitiser sat at the one place
    that built the header; now the header is built by Cloudflare, and a control
    that lives at the call site is a control someone can forget to call. Callers
    still sanitise -- double application is a no-op for an allowlist -- so the
    guard has to be missed twice, in two files, which is the same argument that
    keeps `content` off `HandoutOut`.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", (filename or "").strip()).lstrip(".")
    return cleaned[:120] or "download"


def presigned_get_url(key: str, *, filename: str, mime_type: str | None) -> str:
    """A short-lived URL that serves one object, as a download.

    Both response overrides were measured against the live account on 2026-08-17
    rather than read from documentation, because this repo's record with a
    gateway honouring a documented parameter is poor -- four OpenRouter traps,
    four different mechanisms, only one of which produced the error the docs
    would predict. Measured: the disposition came back exactly as sent, the
    content type came back as the pptx mime verbatim, an expired URL returned
    403, and an unsigned GET returned 400.

    Signing is local -- an HMAC over the request -- so this makes no network call
    and cannot fail slowly. That is why it is safe to call inside a request
    handler while `put_object` below is not.
    """
    params = {
        "Bucket": settings.r2_bucket,
        "Key": key,
        "ResponseContentDisposition": (
            f'attachment; filename="{_disposition_safe(filename)}"'
        ),
    }
    if mime_type:
        params["ResponseContentType"] = mime_type

    try:
        return get_client().generate_presigned_url(
            "get_object", Params=params, ExpiresIn=settings.r2_presign_ttl_s
        )
    except Exception as exc:  # noqa: BLE001
        raise StorageError(f"Could not sign {key!r}: {exc}") from exc


def delete_object(key: str) -> None:
    """Delete one object. Raises `StorageError`.

    Deleting an absent key is a SUCCESS on S3 and this function does not pretend
    otherwise, which is what makes every caller idempotent for free -- a retried
    delete, or a delete of a row whose object was never written because the job
    failed before the put, both no-op rather than raise.
    """
    try:
        get_client().delete_object(Bucket=settings.r2_bucket, Key=key)
    except Exception as exc:  # noqa: BLE001
        raise StorageError(f"Could not delete {key!r}: {exc}") from exc


def delete_quietly(key: str | None) -> None:
    """Best-effort delete for the rollback path. Logs, never raises.

    This is the fourth step of the write ordering: put the object, set the key,
    commit, and on failure remove what was written. It must not raise, because
    the caller is already handling an exception -- turning a recoverable rollback
    into a failed request to report a failed cleanup would be strictly worse than
    the orphan it is trying to prevent. What this cannot catch is a process that
    dies between the put and the commit, and that is what
    `scripts/migrate_bytes_to_r2.py --orphans` reconciles.
    """
    if not key:
        return
    try:
        delete_object(key)
    except Exception:  # noqa: BLE001
        # `except Exception`, not `except StorageError`, and the difference was a
        # real defect caught by `storage_check.py` 73b before this function had
        # a caller. `delete_object` wrapped only `ClientError`, so a
        # connection-level failure -- `EndpointConnectionError`, `NoCredentialsError`,
        # anything under `BotoCoreError` -- travelled straight through a function
        # documented as never raising, into an `except` block that was already
        # handling something else. The two most likely moments for R2 to be
        # unreachable are exactly the moments this runs.
        log.warning("Orphaned object left in R2 after rollback: %s", key, exc_info=True)


def delete_prefix(prefix: str) -> int:
    """Delete everything under a prefix. Returns the count. Raises `StorageError`.

    Paginated and batched at 1000, which is the S3 `delete_objects` ceiling --
    the same number `PineconeVectorStore.delete` batches ids at, and for the same
    reason: exceeding it is an error rather than a slow request.
    """
    client = get_client()
    deleted = 0
    try:
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=settings.r2_bucket, Prefix=prefix):
            keys = [{"Key": item["Key"]} for item in page.get("Contents", [])]
            for start in range(0, len(keys), 1000):
                batch = keys[start : start + 1000]
                client.delete_objects(
                    Bucket=settings.r2_bucket, Delete={"Objects": batch}
                )
                deleted += len(batch)
    except Exception as exc:  # noqa: BLE001
        raise StorageError(f"Could not clear prefix {prefix!r}: {exc}") from exc
    return deleted


def get_object(key: str) -> bytes:
    """Read bytes back. Raises `StorageError`.

    The application download path does NOT use this -- it redirects, so the bytes
    never enter this process, which is the main reason the move is worth making
    on a single-worker service. It exists for the backfill script's verification
    pass and for harnesses that must prove what was stored.
    """
    try:
        response = get_client().get_object(Bucket=settings.r2_bucket, Key=key)
        return response["Body"].read()
    except Exception as exc:  # noqa: BLE001
        raise StorageError(f"Could not read {key!r}: {exc}") from exc
