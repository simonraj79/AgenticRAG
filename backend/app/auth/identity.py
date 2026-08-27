"""One person, two identity systems, and the join between them.

Better Auth keeps its own `user` table. This application keeps `users`, and
every tenancy-bearing row in the schema points at it -- `agents.owner_user_id`,
`documents.uploaded_by_user_id`, `queries.user_id`, `api_usage.user_id`,
`sessions.user_id`. Replacing that table is not a migration, it is a rewrite of
every foreign key in the database, so Better Auth's tables sit BESIDE it and
this module is the seam.

WHY NOT JUST MATCH ON EMAIL.

Because PRD section 3.1 forbids it, for a reason that has not stopped being
true: an address can be reassigned to a different person inside a Workspace
domain. Matching on email would hand a departed employee's agents, documents and
conversation history to whoever inherits their address -- silently, as a
successful login. The join key stays the Google subject id.

WHY THAT IS NOT AS EASY AS IT SOUNDS.

Better Auth's JWT carries ITS OWN user id in `sub`, not Google's. The Google
subject lives one table away, in `account.accountId`, and is not in the token.
So a first-time link costs one extra read -- and only a first-time link, because
`users.better_auth_id` caches the answer forever after:

    1. `users.better_auth_id == sub`         -> done, the common path, one query
    2. read `account` for this Better Auth
       user's google `accountId`             -> the Google subject
    3. `users.google_sub == <that>`          -> an EXISTING user. Link and done.
    4. nothing matched                       -> genuinely new. Create.

Step 3 is the one that matters and it is the reason this file is shaped this
way: it is what makes the 15 existing accounts -- with their agents, their
uploaded corpora and their query history -- survive the cutover instead of
silently becoming 15 empty new ones. A signed-in user seeing an empty workspace
would look like data loss and would be indistinguishable from it.

READING ANOTHER SERVICE'S TABLE.

Step 2 queries `account` with raw SQL, and that is deliberate rather than lazy.
Adding a SQLAlchemy model for it would put it in `Base.metadata`, which would
make alembic try to MANAGE a table Better Auth's own CLI migrates -- two
migration systems owning one table, which is worse than the DROP problem
`app/db/migration_filter.py` exists to prevent. The read is narrow, it is
read-only, and it happens once per user for the lifetime of the account.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User

log = logging.getLogger("uvicorn.error")

# Better Auth names its Google provider rows this. It is a literal in that
# service's config, so it is a literal here; there is no shared constant to
# import across a language boundary.
GOOGLE_PROVIDER_ID = "google"

# `last_login_at` is refreshed at most this often. This function runs on EVERY
# authenticated request, not once per login, so writing the timestamp each time
# would mean an UPDATE per request -- and would quietly redefine the column as
# "last request at". An hour keeps it meaningful and keeps the steady state a
# pure read.
LOGIN_TIMESTAMP_STALE_AFTER = timedelta(hours=1)

# Better Auth's schema is camelCase, which Postgres folds to lowercase unless
# quoted. Written out once, quoted, rather than assembled -- an unquoted
# `userId` becomes `userid` and the query fails with "column does not exist",
# which reads like a missing migration rather than a quoting mistake.
_ACCOUNT_LOOKUP = text(
    'SELECT "accountId" FROM "account" '
    'WHERE "userId" = :user_id AND "providerId" = :provider_id '
    "LIMIT 1"
)


async def _google_sub_for(db: AsyncSession, better_auth_user_id: str) -> str | None:
    """The Google subject behind a Better Auth user id, or None.

    Returns None rather than raising if the table is absent. That is the
    cutover-safety case: before `npx @better-auth/cli migrate` has run, `account`
    does not exist, and the correct behaviour is to fall through to the existing
    cookie path rather than to 500 every request in the deployment window
    between the API shipping and the auth service being migrated.
    """
    try:
        result = await db.execute(
            _ACCOUNT_LOOKUP,
            {"user_id": better_auth_user_id, "provider_id": GOOGLE_PROVIDER_ID},
        )
    except SQLAlchemyError:
        # A failed read here must not poison the caller's transaction for the
        # fallback path that follows it.
        await db.rollback()
        log.warning(
            "Better Auth 'account' table is not readable. Falling back to the "
            "cookie session path. If this persists after deploy, run the Better "
            "Auth CLI migration against this database."
        )
        return None

    row = result.scalar_one_or_none()
    return str(row) if row else None


async def user_from_claims(
    db: AsyncSession, claims: dict[str, Any]
) -> User | None:
    """Map verified JWT claims onto this application's `users` row.

    The token is ALREADY verified when this is called -- signature, expiry,
    issuer. Nothing here re-checks that, and nothing here may be reached with
    unverified claims. `deps.py` is the only caller for exactly that reason.

    Returns None when the claims cannot be resolved to a user at all, which the
    caller treats as "not signed in" rather than as an error.
    """
    better_auth_user_id = claims.get("sub")
    if not better_auth_user_id:
        # `sub` is in `verify_token`'s required-claims list, so reaching this
        # means the token shape changed rather than that a caller misbehaved.
        return None

    # 1. The steady state. One indexed lookup, every request after the first.
    existing = await db.execute(
        select(User).where(User.better_auth_id == better_auth_user_id)
    )
    user = existing.scalar_one_or_none()

    if user is None:
        # 2-3. First sight of this Better Auth user. Find out whether they are
        #      actually a stranger or an existing account arriving by a new road.
        google_sub = await _google_sub_for(db, str(better_auth_user_id))
        if google_sub is None:
            return None

        by_google = await db.execute(select(User).where(User.google_sub == google_sub))
        user = by_google.scalar_one_or_none()

        if user is None:
            # 4. Genuinely new. `google_sub` is still the identity; the Better
            #    Auth id is a link, not a key.
            email = claims.get("email")
            if not email:
                # `users.email` is NOT NULL and the profile scope was requested,
                # so this means the payload shape changed on the auth service.
                log.warning("Better Auth token carried no email claim; refusing.")
                return None
            user = User(
                google_sub=google_sub,
                better_auth_id=str(better_auth_user_id),
                email=email,
                name=claims.get("name"),
                avatar_url=claims.get("picture") or claims.get("image"),
            )
            db.add(user)
        else:
            # THE ROW THAT MAKES THE CUTOVER NON-DESTRUCTIVE. An existing
            # account, recognised by the subject Google has always used for
            # them, now also reachable by its Better Auth id.
            user.better_auth_id = str(better_auth_user_id)
            log.info(
                "Linked existing user %s to Better Auth identity.", user.email
            )

    # Mutable display data, refreshed from the token -- the same rule
    # `_upsert_google_user` follows. Guarded so a payload that omits a field does
    # not blank a column that currently holds a good value.
    if claims.get("email"):
        user.email = claims["email"]
    if claims.get("name"):
        user.name = claims["name"]
    image = claims.get("picture") or claims.get("image")
    if image:
        user.avatar_url = image

    now = datetime.now(timezone.utc)
    last = user.last_login_at
    if last is None or (now - last) > LOGIN_TIMESTAMP_STALE_AFTER:
        user.last_login_at = now

    # THE COMMIT, AND WHY IT HAS TO BE HERE.
    #
    # `get_db` never commits -- it yields a session inside `async with`, so every
    # route owns its own transaction and a route that does not commit discards
    # its writes on the way out. `GET /api/auth/me` is read-only and commits
    # nothing, and this function is reached from a DEPENDENCY rather than from a
    # route body. So without this, `better_auth_id` was written, flushed, and
    # rolled back on every single request.
    #
    # Nothing failed. The user signed in, the page rendered their three agents,
    # every assertion in `auth_check.py` stayed green -- and the link the whole
    # module exists to create never persisted, so the `account` lookup above ran
    # again on the next request, forever. Found by reading the ROW after a
    # successful login, never by watching the login succeed.
    #
    # Committing from a dependency is safe here specifically because it runs
    # BEFORE the route body: nothing else in the request has written yet, so this
    # commits the identity upsert and nothing else. It is also correct that a
    # later failure in the route cannot roll it back -- that someone
    # authenticated is a fact, not part of the route's work.
    if db.new or db.dirty:
        await db.flush()
        await db.commit()

    return user
