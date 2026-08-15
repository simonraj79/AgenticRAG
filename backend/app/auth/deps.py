"""Request -> User. The one place a cookie becomes an identity.

Route modules should never read `request.cookies` themselves; they depend on
`CurrentUser` and get a `User` or a 401, with no branch of their own to get
wrong. Authorisation over *resources* is a separate concern and lives in
`app/api/deps.py`.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.session import SESSION_COOKIE, resolve_session
from app.db.models import Session, User
from app.db.session import get_db

# Saves every route signature in the codebase from repeating `Depends(get_db)`.
DbSession = Annotated[AsyncSession, Depends(get_db)]


async def optional_session(request: Request, db: DbSession) -> Session | None:
    """The live session row, or None. The single cookie read in the app.

    Returns the row rather than the user because logout needs something to
    revoke, and `queries.session_id` needs the id.
    """
    return await resolve_session(db, request.cookies.get(SESSION_COOKIE))


async def optional_user(request: Request, db: DbSession) -> User | None:
    """The signed-in user, or None for an anonymous caller.

    For routes that legitimately serve both -- a landing endpoint, anything that
    varies by auth rather than requiring it. Everything else wants `CurrentUser`.
    """
    row = await optional_session(request, db)
    if row is None:
        return None
    # A deactivated account is treated as not signed in, so existing sessions
    # stop working the moment `is_active` flips without anyone having to hunt
    # down and revoke every row the user holds.
    if not row.user.is_active:
        return None
    return row.user


async def current_user(request: Request, db: DbSession) -> User:
    """The signed-in user, or 401.

    Missing, expired, revoked and deactivated all produce the same 401 with the
    same body. The distinction is real but it is not the client's business, and
    the browser's correct response is identical in every case: log in again.
    """
    user = await optional_user(request, db)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    return user


CurrentUser = Annotated[User, Depends(current_user)]


async def require_admin(user: CurrentUser) -> User:
    """Admin-only routes -- the Evaluate view and the marketplace oversight list.

    403 rather than 401 on purpose: the caller IS authenticated, and re-logging
    in will not help. A 401 would send the frontend into a pointless login loop.
    """
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator role required",
        )
    return user


AdminUser = Annotated[User, Depends(require_admin)]
