"""THE TENANCY BOUNDARY -- resource-level authorisation.

`app/auth/deps.py` answers "who is calling". This module answers "may they touch
this thing", and for an agent it is the only correct answer to that question.

Read PRD section 3.2 and section 7 together. Every vector in Pinecone is written
into a namespace derived from `Agent.namespace` (`agent_{id}`), and retrieval is
namespace-scoped. So the namespace IS the tenancy boundary, and a wrong
namespace is not an error -- it is a successful cross-tenant read that returns
another user's documents with no exception, no log line and no visible symptom.

Two rules follow, and `owned_agent` exists to make them structural rather than
remembered:

1. **A namespace is never accepted from a request.** Not in a body, not in a
   query param, not in a header. It is derived on the server from an `Agent` row
   that was loaded by id and then checked against the session user. The RAG
   layer enforces the other half of this: `rag/retriever.py` takes an `Agent`
   object, never a namespace string, so there is no parameter through which a
   client-supplied namespace could reach Pinecone even by accident.

2. **The ownership check happens once, in a dependency, not per route.** A check
   copied into eight handlers is a check missing from the ninth -- and the ninth
   will be the upload route, where the mistake writes one tenant's chunks into
   another tenant's namespace permanently.

Any route that touches an agent's corpus, config, queries or traces takes
`OwnedAgent` and receives an agent it is already entitled to.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends, HTTPException, status

from app.auth.deps import AdminUser, CurrentUser, DbSession
from app.db.models import Agent

# Re-exported so route modules have one import for the dependency aliases they
# need. `app.auth.deps` stays the definition site.
__all__ = ["AdminUser", "CurrentUser", "DbSession", "OwnedAgent", "owned_agent"]


async def owned_agent(agent_id: uuid.UUID, user: CurrentUser, db: DbSession) -> Agent:
    """Load an agent the caller owns, or refuse.

    `visibility` is not consulted. The column exists for a later sharing model
    (PRD section 4.2) and only "private" is honoured today, so ownership is
    currently the whole of the rule -- when sharing arrives, it arrives here and
    nowhere else.

    403 rather than 404 on a mismatch. Hiding existence behind a 404 buys nothing
    against v4 UUIDs -- there is nothing to enumerate -- and a distinct status
    means a genuine bug in the frontend (a stale agent id after switching
    accounts) reads as "wrong owner" instead of "deleted", which is what it is.
    """
    agent = await db.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found",
        )
    if agent.owner_user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this agent",
        )
    return agent


# The path parameter must be named `agent_id` for this to bind, e.g.
#   @router.post("/api/agents/{agent_id}/documents")
#   async def upload(agent: OwnedAgent, ...): ...
OwnedAgent = Annotated[Agent, Depends(owned_agent)]
