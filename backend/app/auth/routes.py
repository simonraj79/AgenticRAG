"""Auth routes: the Google flow, /me, logout -- and one deliberate bypass.

PRD section 3.7 defines four endpoints here. There is a fifth,
`POST /api/auth/dev-login`, and it needs explaining before anyone reads it as a
convenience feature and starts using it that way.

**Why it exists.** A real Google sign-in cannot be automated: it ends at a
password prompt and a consent screen that require a human. Everything downstream
of identity -- agent creation, upload, ingest, ask, trace -- sits behind the
session cookie, so with no way to obtain a cookie there is no way to exercise any
of it end to end. dev-login stubs the identity assertion and NOTHING else: it
calls the same `_upsert_google_user` and the same `create_session` /
`set_session_cookie` path the real callback does. What a browser test drives is
therefore the production session machinery, not a parallel implementation of it.

**Why it is dangerous.** It is an authentication bypass, in a PUBLIC repository,
in a service that deploys straight to production. Anyone reading the source knows
the route exists and what body it takes. A single misconfigured environment
variable would turn it into "log in as any email address, including an admin".

**So it is gated three ways, all of which must hold** (see `_dev_login_allowed`):
an explicit opt-in flag, an environment that says `development`, and a caller on
loopback. Three independent conditions, because each covers the others' failure:
the flag can be set by accident, `ENVIRONMENT` can be unset and fall back to its
default, and a machine can be misconfigured -- but a request from the public
internet is never on loopback, so condition 3 holds the line even if 1 and 2 both
fail. A rejection is 404, not 403: 403 confirms the route is real and merely
disabled, which is a map for anyone probing the deployment.

Every success logs a WARNING. If that line ever appears in Render's logs,
something is wrong that the gating did not catch.
"""

from __future__ import annotations

import ipaddress
import logging
import uuid
from datetime import datetime, timezone
from typing import Annotated

from authlib.integrations.starlette_client import OAuthError
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import CurrentUser, DbSession, optional_session
from app.auth.oauth import oauth
from app.auth.session import (
    clear_session_cookie,
    create_session,
    revoke_session,
    set_session_cookie,
)
from app.config import settings
from app.db.models import Session, User

log = logging.getLogger("uvicorn.error")

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Logout resolves the session itself instead of requiring one -- see the route.
OptionalSession = Annotated[Session | None, Depends(optional_session)]


class UserOut(BaseModel):
    """What the frontend is allowed to know about the signed-in user.

    `google_sub` is not here. It is the identity key, it is stable forever, and
    the UI has no use for it.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    name: str | None = None
    avatar_url: str | None = None
    role: str
    last_login_at: datetime | None = None


class DevLoginRequest(BaseModel):
    """Body for the gated dev-login route. Plain `str`, not `EmailStr`.

    `EmailStr` needs the `email-validator` package, and adding a dependency for
    the benefit of a route that is disabled in production is the wrong trade.
    """

    email: str
    name: str | None = None


# --------------------------------------------------------------------------
# Shared identity handling. The real callback and dev-login both land here, so
# only the assertion above differs between them.
# --------------------------------------------------------------------------

async def _upsert_google_user(
    db: AsyncSession,
    *,
    google_sub: str,
    email: str,
    name: str | None,
    avatar_url: str | None,
) -> User:
    """Find-or-create the `users` row, keyed on `sub`.

    Keyed on `google_sub`, never on email (PRD section 3.1). Google's own
    guidance is explicit: an address can be reassigned to a different person
    within a Workspace domain, while `sub` is unique and never reused. Keying on
    email would hand a departed employee's history to whoever inherits their
    address -- and it would do so silently, as a successful login.

    Email, name and avatar are refreshed on every login precisely because they
    are the mutable fields. They are display data downstream of the identity,
    not the identity.
    """
    result = await db.execute(select(User).where(User.google_sub == google_sub))
    user = result.scalar_one_or_none()

    if user is None:
        user = User(google_sub=google_sub, email=email, name=name, avatar_url=avatar_url)
        db.add(user)
    else:
        user.email = email
        user.name = name
        user.avatar_url = avatar_url

    user.last_login_at = datetime.now(timezone.utc)
    await db.flush()
    return user


async def _issue_session(
    request: Request, response: Response, db: AsyncSession, user: User
) -> None:
    """Create the session row and attach the cookie to `response`.

    Does not commit; the calling route owns the transaction so the `users`
    upsert and the `sessions` insert land together.
    """
    if not user.is_active:
        # Refused here rather than at the next request. Letting login "succeed"
        # and then 401ing on /me would put the SPA in a redirect loop with no
        # explanation for the user.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account has been deactivated.",
        )

    _, raw_token = await create_session(
        db,
        user,
        user_agent=request.headers.get("user-agent"),
        # Truncated to a /24 or /64 inside create_session -- see PRD section 7.
        ip_address=request.client.host if request.client else None,
    )
    set_session_cookie(response, raw_token)


# --------------------------------------------------------------------------
# The Google flow
# --------------------------------------------------------------------------

@router.get("/google/login")
async def google_login(request: Request) -> RedirectResponse:
    """Step 1: bounce the browser to Google's consent screen.

    The redirect URI comes from config and is NEVER derived from
    `request.url_for()`. Behind Render's TLS-terminating proxy, url_for builds
    the internal `http://` URL, which does not match the `https://` entry
    registered in the Google console -- matching is exact, down to scheme and
    trailing slash -- so Google answers `redirect_uri_mismatch`. It works
    perfectly in local development, which is what makes it a deploy-day bug
    rather than a development-time one. Pinning it in config keeps the string
    byte-identical to the console entry (PRD section 5).

    Authlib stores the state and OIDC nonce in the Starlette SessionMiddleware
    cookie here, and reads them back in the callback. That is why the callback
    must be hit by the same browser, and why the middleware's cookie settings in
    main.py matter to this route.
    """
    return await oauth.google.authorize_redirect(request, settings.oauth_redirect_uri)


@router.get("/google/callback")
async def google_callback(request: Request, db: DbSession) -> RedirectResponse:
    """Step 2: exchange the code, establish identity, start a session.

    Google's access token and refresh token are read and then dropped on the
    floor (PRD section 3.1). We asked for identity, not API access: there is no
    Gmail call, no Calendar call, nothing to authorise later. Storing them would
    create a credential to leak in exchange for no capability at all. The local
    `token` dict is the only place they ever exist, and it goes out of scope with
    this function.
    """
    try:
        token = await oauth.google.authorize_access_token(request)
    except OAuthError as exc:
        # State mismatch, an expired code, a user who cancelled at the consent
        # screen. None of these are server faults.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Google sign-in failed: {exc.error}",
        ) from exc

    # Present only because the scope contains `openid` -- see auth/oauth.py.
    userinfo = token.get("userinfo")
    if not userinfo or not userinfo.get("sub"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Google did not return an identity claim.",
        )

    email = userinfo.get("email")
    if not email:
        # `users.email` is NOT NULL, and the `email` scope was requested, so this
        # means something changed on Google's side rather than in the user's data.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Google did not return an email address.",
        )

    user = await _upsert_google_user(
        db,
        google_sub=userinfo["sub"],
        email=email,
        name=userinfo.get("name"),
        avatar_url=userinfo.get("picture"),
    )

    # 302 rather than the RedirectResponse default of 307: this is a plain GET
    # and there is no method to preserve. The cookie rides on this response.
    response = RedirectResponse(
        url=settings.frontend_url,
        status_code=status.HTTP_302_FOUND,
    )
    await _issue_session(request, response, db, user)
    await db.commit()
    return response


# --------------------------------------------------------------------------
# Session endpoints
# --------------------------------------------------------------------------

@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser) -> User:
    """Who am I? The SPA's first call, and its is-the-cookie-still-good check."""
    return user


@router.post("/logout")
async def logout(response: Response, db: DbSession, session: OptionalSession) -> dict:
    """Revoke the session and clear the cookie.

    Deliberately does not require a valid session. A browser holding an expired
    or already-revoked cookie asking to log out should end up logged out, not
    401 with the dead cookie still in place. Revocation is the part that needs a
    live session; clearing the cookie always happens.
    """
    if session is not None:
        await revoke_session(db, session)
        await db.commit()
    clear_session_cookie(response)
    return {"ok": True}


# --------------------------------------------------------------------------
# The gated development bypass. Read the module docstring before touching this.
# --------------------------------------------------------------------------

def _is_loopback(host: str | None) -> bool:
    """True only for 127.0.0.0/8, ::1 and the IPv4-mapped form of them.

    Parsed with `ipaddress` rather than compared against a literal "127.0.0.1",
    because a dual-stack local connection arrives as `::1` or `::ffff:127.0.0.1`
    depending on how uvicorn bound, and a string comparison that misses one of
    those fails open on the day someone "fixes" it by widening the check.
    """
    if not host:
        return False
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return addr.is_loopback


def _dev_login_allowed(request: Request) -> bool:
    """All three conditions, no shortcuts. See the module docstring."""
    if not settings.dev_auth_enabled:
        return False
    if settings.environment != "development":
        return False
    return _is_loopback(request.client.host if request.client else None)


@router.post("/dev-login", response_model=UserOut)
async def dev_login(
    body: DevLoginRequest,
    request: Request,
    response: Response,
    db: DbSession,
) -> User:
    """Stub the identity assertion only. Everything downstream is the real path.

    404, never 403, when the gate rejects: the response is indistinguishable
    from a build where this route was never registered.
    """
    if not _dev_login_allowed(request):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")

    email = body.email.strip().lower()
    if not email:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="email is required",
        )

    user = await _upsert_google_user(
        db,
        # Prefixed so a stubbed identity can never collide with a real Google
        # `sub`, which is a bare numeric string. Without the prefix, a test user
        # created as "someone@gmail.com" could in principle occupy the row a
        # real sign-in later needs -- and `users.google_sub` is unique, so the
        # collision would surface as a failed login, not as a merge.
        google_sub=f"dev|{email}",
        email=email,
        name=body.name,
        avatar_url=None,
    )
    await _issue_session(request, response, db, user)
    await db.commit()

    # ASCII only: the Windows console mangles anything else. This line is the
    # audit trail for a bypass that should never fire outside a laptop.
    log.warning(
        "DEV LOGIN GRANTED for %s from %s. This bypasses Google OAuth entirely "
        "and is only reachable with DEV_AUTH_ENABLED=true, ENVIRONMENT=development "
        "and a loopback client. If you are reading this in a deployed log, the "
        "service is misconfigured.",
        email,
        request.client.host if request.client else "unknown",
    )
    return user
