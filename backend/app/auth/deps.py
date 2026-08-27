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

from app.auth.identity import user_from_claims
from app.auth.jwt import InvalidToken, JwksCache, UnknownKeyId, extract_bearer, verify_token
from app.auth.session import SESSION_COOKIE, resolve_session
from app.config import settings
from app.db.models import Session, User
from app.db.session import get_db

# Saves every route signature in the codebase from repeating `Depends(get_db)`.
DbSession = Annotated[AsyncSession, Depends(get_db)]


# Process-wide, because the key set is a property of the auth service rather
# than of a request. One uvicorn worker on Render's starter plan means this is
# genuinely one cache, and `JwksCache` collapses a cold-start stampede into a
# single fetch.
_jwks = JwksCache(settings.better_auth_jwks_url)


async def _claims_from_bearer(token: str) -> dict | None:
    """Verify a Better Auth JWT, refreshing the key set once on a rotation.

    `UnknownKeyId` is caught BEFORE `InvalidToken` because it is a subclass and
    Python takes the first matching clause -- reversing them would silently
    disable rotation handling while every test still passed, since a rotation
    is invisible until the day it happens.

    Every other rejection returns None. The reason a token failed is written to
    the log and never to the caller: expired, forged and wrong-audience all
    produce the same 401, and the difference between them is exactly what an
    attacker is probing for.
    """
    issuer = settings.better_auth_expected_issuer
    audience = settings.better_auth_audience or None
    try:
        return verify_token(
            token, await _jwks.get(), issuer=issuer, audience=audience
        )
    except UnknownKeyId:
        try:
            return verify_token(
                token, await _jwks.get(force=True), issuer=issuer, audience=audience
            )
        except InvalidToken:
            return None
    except InvalidToken:
        return None


async def _user_from_bearer(request: Request, db: DbSession) -> User | None:
    """The Better Auth path. None means "this request presented no valid token".

    None is deliberately not distinguished from "presented a bad token": during
    the cutover both must fall through to the cookie, and afterwards both are a
    401. A caller that needed the difference would be reintroducing the oracle
    the collapsed exception type exists to close.
    """
    token = extract_bearer(request.headers.get("authorization"))
    if token is None:
        return None
    claims = await _claims_from_bearer(token)
    if claims is None:
        return None
    return await user_from_claims(db, claims)


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
    # BEARER FIRST, AND THE ORDER IS THE WHOLE POINT. During the cutover both
    # paths are live. If the cookie were consulted first it would keep winning
    # for every already-signed-in user, every request would still succeed -- on
    # the OLD system -- and the new one would look deployed while never once
    # having authenticated anybody. `auth_check.py` case 24 pins this order for
    # that reason: the failure it guards has no symptom.
    user = await _user_from_bearer(request, db)

    if user is None:
        # The Authlib cookie path. Still live, still the only thing `dev-login`
        # produces, and deleted only once the Bearer path has been watched
        # working in the browser.
        row = await optional_session(request, db)
        if row is None:
            return None
        user = row.user

    # A deactivated account is treated as not signed in, so existing sessions
    # stop working the moment `is_active` flips without anyone having to hunt
    # down and revoke every row the user holds. Applied AFTER both paths, so
    # neither can bypass it.
    if not user.is_active:
        return None
    return user


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
