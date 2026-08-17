"""Deletion: the Pinecone half that Postgres cascades cannot reach.

Dropping a `documents` row cascades to `chunks` and looks complete. It is not.
The vectors those chunks pointed at stay in Pinecone and keep matching queries,
so an agent whose documents were all "deleted" goes on answering from them. That
is a correctness bug in retrieval, not untidiness -- and it is invisible from the
database, because once the `chunks` rows are gone nothing records that the
vectors ever existed. Deleting rows without deleting vectors is worse than not
deleting at all: it destroys the only list of what to clean up.

Everything here reaches Pinecone through `get_vector_store(agent)` rather than
through a raw index handle, for the reason retriever.py exists at all: the store
is bound to `agent.namespace` at construction, so there is no parameter in this
module through which a delete could be aimed at another agent's namespace.
"""

from __future__ import annotations

import asyncio

from pinecone.exceptions import NotFoundException
from sqlalchemy import delete as sa_delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.db.models import Agent, Chunk, Document
from app.rag.retriever import get_vector_store


async def delete_document(db: AsyncSession, agent: Agent, document: Document) -> int:
    """Remove one document's vectors and rows. Returns the vector count deleted.

    `agent` is not redundant with `document`: the namespace is derived from the
    agent while the vector ids come from the document, and if those two disagree
    the delete would aim one agent's id list at another agent's namespace. The
    check below is a line of code and the failure it prevents is silent, so it is
    made explicitly rather than left to the caller to remember.
    """
    if document.agent_id != agent.id:
        raise ValueError(
            f"document {document.id} belongs to agent {document.agent_id}, not {agent.id}"
        )

    result = await db.scalars(
        select(Chunk.pinecone_id).where(
            Chunk.document_id == document.id,
            Chunk.pinecone_id.is_not(None),
        )
    )
    pinecone_ids = list(result.all())

    if pinecone_ids:
        # VECTORS FIRST, ROWS SECOND, and the order is the whole design.
        #
        # A crash between the two steps leaves one of two messes, and they are
        # not equally bad. Orphaned rows -- vectors gone, rows remaining -- are
        # visible in `documents`, still carry their `pinecone_id` values, and are
        # fixed by running this function again; the second Pinecone delete is a
        # no-op. Orphaned vectors -- rows gone, vectors remaining -- have lost
        # the only record of which ids to delete, so they are unreachable,
        # permanent, and still matching every query in this namespace.
        #
        # Delete the recoverable thing last.
        #
        # No `namespace=` argument: PineconeVectorStore.delete() falls back to
        # the namespace the store was constructed with, which is
        # `agent.namespace`. It also batches ids in chunks of 1000, which is
        # what keeps a large document from exceeding Pinecone's per-request id
        # limit.
        get_vector_store(agent).delete(ids=pinecone_ids)

    # Core DELETE rather than `await db.delete(document)`. The ORM path walks
    # `Document.chunks`, and that relationship declares no delete cascade, so
    # SQLAlchemy's default is to de-associate the children by setting
    # `chunks.document_id` to NULL -- a NOT NULL column, so it fails with an
    # IntegrityError that reads as a schema problem rather than a mapping one.
    # The foreign key already carries ON DELETE CASCADE; going through Core lets
    # Postgres perform the cascade it was built for. `chunks` is deleted
    # explicitly anyway so that this does not silently depend on the migration
    # having emitted that clause.
    await db.execute(sa_delete(Chunk).where(Chunk.document_id == document.id))
    await db.execute(sa_delete(Document).where(Document.id == document.id))
    await db.commit()

    return len(pinecone_ids)


async def delete_agent_namespace(agent: Agent) -> None:
    """Delete every vector under one agent's namespace.

    For deleting an agent outright. Postgres cascades `agents` -> `documents` ->
    `chunks` on its own, so this is the half that cascade cannot reach, and it
    must run before those rows go for the ordering reason in `delete_document`.

    Deliberately not `index.delete_namespace()`. Two reasons: the published
    example calls it as `delete_namespace(name=...)` while the pinecone 7.3.0
    signature installed here is `delete_namespace(namespace: str)`, so the
    documented call raises TypeError; and going through the vector store keeps
    the namespace derived from the agent rather than passed as a string.
    """
    try:
        get_vector_store(agent).delete(delete_all=True)
    except NotFoundException:
        # 404 is success here. Pinecone creates a namespace lazily on first
        # upsert and drops it when its last vector goes, so an agent that never
        # ingested anything has no namespace to delete -- and neither does one
        # whose deletion is being retried after a partial failure. In both cases
        # the requested end state (no vectors under this agent) already holds.
        # Raising would make deleting an empty agent fail for precisely the
        # reason that guarantees it is safe.
        return


async def delete_agent_objects(agent: Agent) -> int:
    """Delete every stored file under one agent's prefix. Returns the count.

    **The exact counterpart of `delete_agent_namespace` above, and it exists for
    a reason that is easy to miss when reading `api/agents.py`.** That route
    deletes the agent with a Core `DELETE` so Postgres performs the whole
    cascade in one statement -- and there is no `relationship()` on `Handout`
    anywhere in the model layer, so no Python code ever sees the handout rows go.
    There is nothing to iterate and nowhere to hang a per-row cleanup. Without a
    prefix delete, every file an agent ever produced survives its agent
    permanently, with no row left naming the key.

    Which is why the key scheme puts `agent_id` in the first path segment: this
    function is the whole justification for it.

    Runs BEFORE the row cascade, the same ordering `delete_document` argues at
    length -- orphaned rows are visible and re-deletable, orphaned objects are
    not. An empty prefix deletes nothing and reports 0, so an agent that never
    made a handout is not a special case.
    """
    if not storage.enabled():
        return 0
    return await asyncio.to_thread(storage.delete_prefix, storage.agent_prefix(agent.id))


async def delete_document_object(document: Document) -> None:
    """Delete one document's stored original, if it has one.

    Tolerates a missing key rather than treating it as an error: every document
    ingested before object storage existed has `storage_key = None`, and so does
    every upload made while `storage_route` is "postgres". Both are normal.
    """
    if not storage.enabled() or not document.storage_key:
        return
    await asyncio.to_thread(storage.delete_object, document.storage_key)


async def namespace_vector_count(agent: Agent) -> int:
    """Live vector count for one agent's namespace, straight from Pinecone.

    Read from the index rather than counted from `chunks`, because the point of
    this function is to check the two stores agree. Counting rows would answer a
    different question and would report success in exactly the case this exists
    to catch: rows deleted, vectors left behind.

    Eventually consistent. Pinecone's stats lag writes by a short interval, so a
    count read immediately after an ingest or a delete may still show the old
    value; poll rather than asserting once.
    """
    stats = get_vector_store(agent).index.describe_index_stats()

    # `.to_dict()` rather than attribute access. These are generated OpenAPI
    # models, and an absent optional field raises PineconeApiAttributeError --
    # which subclasses AttributeError, so even `getattr(stats, "namespaces", {})`
    # would swallow it and return a confidently wrong answer instead of failing.
    # `to_dict()` returns plain nested dicts and simply omits what is absent.
    # Same family of trap as IndexTags, where `dict()` raises and `.to_dict()`
    # works.
    namespaces = stats.to_dict().get("namespaces") or {}

    # 0, never an exception, when the namespace is absent. An agent that has
    # never ingested has no namespace, and "no vectors" is the true answer. The
    # isolation check asserts on this value, and a raise there would read as a
    # broken test rather than as an empty agent.
    return int(namespaces.get(agent.namespace, {}).get("vector_count") or 0)
