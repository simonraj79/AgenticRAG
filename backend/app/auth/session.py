"""Server-side sessions: an opaque token in an httpOnly cookie, a hash in Postgres.

PRD section 2.2 chose DB-backed sessions over signed stateless tokens for one
reason: a signed token cannot be revoked before it expires. The `sessions` table
gives real logout and "sign out everywhere", and it survives a backend restart,
which a process-local store would not on Render's ephemeral filesystem.

The rule that shapes this module (PRD section 7): **a database read must not
yield a working credential.** Only `sha256(token)` is stored, so a dumped table,
a leaked backup or a log line containing a row is useless to an attacker -- the
raw token exists exactly twice, in the Set-Cookie header and in the browser.
Nothing here ever writes `raw_token` to a log.
"""

from __future__ import annotations

import hashlib
import ipaddress
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from starlette.responses import Response

from app.db.models import Session, User

# The browser-facing name. Prefixed rather than the generic "session" so it
# cannot collide with the Starlette SessionMiddleware cookie, which carries
# Authlib's OAuth state on the same origin and is a completely different thing.
SESSION_COOKIE = "arag_session"

# Fourteen days. Long enough that a workshop participant is not re-authenticating
# mid-exercise, short enough that a stolen cookie is not a permanent credential.
# The cookie's max_age and the row's expires_at are both derived from this, so
# they cannot drift apart -- a cookie that outlives its row would surface as a
# mysterious 401 rather than as an expiry.
SESSION_LIFETIME = timedelta(days=14)

# 32 bytes of os.urandom, base64url-encoded to 43 characters. Guessing is not a
# threat model at that width; the token only has to survive being carried around.
TOKEN_BYTES = 32


def hash_token(raw_token: str) -> str:
    """The value that goes in `sessions.token_hash`.

    Plain SHA-256 rather than a password hash on purpose. bcrypt/argon2 exist to
    make brute force expensive against LOW-entropy secrets; this token has 256
    bits of entropy from a CSPRNG, so there is nothing to brute force, and a slow
    hash here would only add work to every authenticated request. It also has to
    stay deterministic: lookup is an indexed equality match on the hash, not a
    scan-and-verify.
    """
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def anonymise_ip(raw_ip: str | None) -> str | None:
    """Drop the host portion of an address before it is stored.

    PRD section 7: IP addresses are personal data under GDPR/PDPA, and
    `sessions.ip_address` is there for "roughly where was this session used",
    not for identifying a person. Truncating to /24 (IPv4) or /64 (IPv6) keeps
    the coarse answer and discards the identifier. Applied inside
    `create_session` so a caller cannot forget it.

    Anything unparseable (a proxy header artefact, a unix socket path) is
    discarded rather than stored raw.
    """
    if not raw_ip:
        return None
    try:
        addr = ipaddress.ip_address(raw_ip)
    except ValueError:
        return None
    if addr.version == 4:
        network = ipaddress.ip_network(f"{addr}/24", strict=False)
    else:
        network = ipaddress.ip_network(f"{addr}/64", strict=False)
    return str(network)


async def create_session(
    db: AsyncSession,
    user: User,
    *,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> tuple[Session, str]:
    """Mint a session. Returns the row and the RAW token, in that order.

    The raw token is returned rather than stored because this is the only moment
    it exists in the process. Hand it straight to `set_session_cookie` and let it
    fall out of scope; never persist it, never log it.

    Flushes but does not commit -- the caller owns the transaction, so the
    session row and whatever else that request wrote (a `users` upsert, an audit
    row) land or roll back together. The flush is what assigns `Session.id`,
    which `queries.session_id` later references.
    """
    raw_token = secrets.token_urlsafe(TOKEN_BYTES)

    row = Session(
        user_id=user.id,
        token_hash=hash_token(raw_token),
        expires_at=datetime.now(timezone.utc) + SESSION_LIFETIME,
        user_agent=user_agent,
        ip_address=anonymise_ip(ip_address),
    )
    db.add(row)
    await db.flush()
    return row, raw_token


async def resolve_session(db: AsyncSession, raw_token: str | None) -> Session | None:
    """Look up a live session by its raw token, or None.

    "Live" means all three of: the hash matches a row, `revoked_at` is unset, and
    `expires_at` is in the future. Every rejection returns None rather than
    raising, so the caller decides whether a missing session is a 401 or simply
    an anonymous request.

    The user is eager-loaded. Under async SQLAlchemy a lazy `row.user` access
    outside the awaited context raises `MissingGreenlet` -- an error about
    greenlets that is really about a missing `selectinload`, and it would fire in
    `deps.current_user` rather than here.
    """
    if not raw_token:
        return None

    result = await db.execute(
        select(Session)
        .options(selectinload(Session.user))
        .where(Session.token_hash == hash_token(raw_token))
    )
    row = result.scalar_one_or_none()
    if row is None:
        return None
    if row.revoked_at is not None:
        return None

    # `expires_at` is a timestamptz, so asyncpg hands back an aware datetime and
    # this comparison is safe. Comparing an aware value against a naive
    # utcnow() would raise TypeError on every request.
    if row.expires_at <= datetime.now(timezone.utc):
        return None
    return row


async def revoke_session(db: AsyncSession, session: Session) -> None:
    """Mark a session dead. The row stays: logout is history, not a deletion.

    Idempotent -- re-revoking keeps the original timestamp, so a double-clicked
    logout does not rewrite when the session actually ended.
    """
    if session.revoked_at is None:
        session.revoked_at = datetime.now(timezone.utc)


def set_session_cookie(response: Response, raw_token: str) -> None:
    """Attach the session cookie to an outgoing response.

    Every attribute here is load-bearing:

    `httponly` keeps the token out of `document.cookie`, so an XSS bug in the
    React app cannot read it.

    `samesite="none"` with `secure=True` is forced by the deployment shape (PRD
    section 6.5): the static site and the API are different origins, so the
    cookie is third-party on every XHR. Starlette's default of "lax" survives the
    top-level redirect back from Google -- login LOOKS like it worked -- and then
    the first fetch() from React arrives without the cookie and 401s. Browsers
    reject `SameSite=None` without `Secure`, which is why the two travel together.
    Chrome and Firefox both treat http://localhost as a trustworthy origin, so
    Secure does not break local development.

    `partitioned` (CHIPS) is deliberately NOT set. It would key the cookie to the
    top-level site at the moment it was stored -- the API's own origin, mid
    redirect from Google -- and it would then be invisible to XHR from the
    frontend's site. It looks like the modern answer to third-party cookie
    deprecation and it would silently break this exact flow.

    `max_age` mirrors SESSION_LIFETIME so the browser forgets the cookie at the
    same moment the row stops resolving.
    """
    response.set_cookie(
        SESSION_COOKIE,
        raw_token,
        max_age=int(SESSION_LIFETIME.total_seconds()),
        path="/",
        httponly=True,
        secure=True,
        samesite="none",
    )


def clear_session_cookie(response: Response) -> None:
    """Remove the session cookie.

    The attributes must match `set_session_cookie` exactly. A browser identifies
    a cookie by name + domain + path, and Starlette's `delete_cookie` defaults to
    `samesite="lax", secure=False` -- attributes a browser will refuse to send
    back cross-site, leaving the real cookie in place while the response looks
    like it worked.
    """
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        httponly=True,
        secure=True,
        samesite="none",
    )
