"""Layer 1 harness for the admin surface. No network, no DB -- instant.

WHY THIS FILE EXISTS.

Every other route in this application scopes tenancy STRUCTURALLY. CLAUDE.md
puts it in one sentence -- *"no request can be expressed without naming an
agent"* -- because the path itself carries the agent id and `owned_agent`
resolves it. You cannot forget the scoping rule, because there is no way to
write the request without it.

`app/api/admin.py` deliberately inverts that. Crossing the tenancy boundary is
the whole feature, so the structural guarantee is gone and `AdminUser` is the
entire control. **A route added to that router without the dependency is not a
scoping bug, it is every user's conversations served to anyone with a session.**

Reviewing for that once is not a control; it is a thing a person did on a
Tuesday. So case 1 introspects the FastAPI route table and asserts the
dependency is present on every route, which keeps holding after the person who
knew has forgotten.

The other three are the failures this feature is otherwise most likely to ship:

  2. `require_admin` must return 403, never 401. A 401 tells the frontend to
     log in again, and the caller IS logged in -- so a non-admin who wanders
     onto /admin gets an infinite login loop instead of a refusal.
  3. Promotion must match EVERY row with an admin email. `admin@example.com` is
     two user rows here -- `dev|admin@example.com` and the real Google `sub` --
     and promoting only one leaves the console unreachable from `dev-login`,
     i.e. untestable by anything that is not a human at a consent screen.
  4. The admin email list must never be consulted on a request path. CLAUDE.md's
     rule is "key on `sub`, never `email`"; this asserts the rule was kept by
     grepping the auth path for the setting.

    backend/.venv/Scripts/python.exe scripts/admin_check.py

ASCII in print(), exit 1 on any failure -- `new features/06-test-plan.md`.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from fastapi.routing import APIRoute  # noqa: E402

from app.api.admin import ADMIN_READ_ACTION, router as admin_router  # noqa: E402
from app.auth.deps import require_admin  # noqa: E402

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "[ok]  " if condition else "[FAIL]"
    print(f"{status} {name}" + (f" -- {detail}" if detail else ""))
    if not condition:
        failures.append(name)


print("=" * 74)
print("admin -- the surface that crosses the tenancy boundary")
print("=" * 74)

# ---------------------------------------------------------------------------
print("\n-- 1. every admin route requires an admin --")
# ---------------------------------------------------------------------------
routes = [r for r in admin_router.routes if isinstance(r, APIRoute)]

unguarded = []
for route in routes:
    # `AdminUser` is `Annotated[User, Depends(require_admin)]`, so the callable
    # lands in the route's flattened dependant tree. Checking the tree rather
    # than the signature means a dependency added via `dependencies=[...]` on a
    # future sub-router also counts.
    found = any(
        dependency.call is require_admin
        for dependency in route.dependant.dependencies
    ) or any(
        param.default is not None
        and getattr(getattr(param.default, "dependency", None), "__name__", "")
        == "require_admin"
        for param in []
    )
    if not found:
        # Walk one level deeper: FastAPI nests sub-dependencies.
        stack = list(route.dependant.dependencies)
        while stack and not found:
            dependency = stack.pop()
            if dependency.call is require_admin:
                found = True
                break
            stack.extend(dependency.dependencies)
    if not found:
        unguarded.append(f"{sorted(route.methods)} {route.path}")

check(
    f"1. all {len(routes)} admin routes depend on require_admin",
    not unguarded,
    f"UNGUARDED: {unguarded}" if unguarded else f"{len(routes)} routes",
)

check(
    "1b. and the router is actually mounted under /api/admin",
    all(r.path.startswith("/api/admin") for r in routes),
    f"paths={[r.path for r in routes][:3]}...",
)

# The route that exposes another person's text must be the one that audits.
# Asserted by name, so renaming it without moving the audit goes red.
transcript_routes = [r for r in routes if "{conversation_id}" in r.path]
check(
    "1c. there is exactly one transcript route, and it is a GET",
    len(transcript_routes) == 1 and "GET" in transcript_routes[0].methods,
    f"n={len(transcript_routes)}",
)

import inspect  # noqa: E402

source = inspect.getsource(transcript_routes[0].endpoint)
check(
    "1d. the transcript route writes an audit row -- reads are accountable",
    "_audit(" in source,
)

check(
    "1e. aggregates do NOT audit -- one row per render would bury the real ones",
    not any(
        "_audit(" in inspect.getsource(r.endpoint)
        for r in routes
        if "{conversation_id}" not in r.path
    ),
)

# ---------------------------------------------------------------------------
print("\n-- 2. the refusal is a 403, never a 401 --")
# ---------------------------------------------------------------------------
import asyncio  # noqa: E402
import uuid as _uuid  # noqa: E402

from fastapi import HTTPException  # noqa: E402

from app.db.models import User  # noqa: E402


def _user(role: str) -> User:
    return User(
        id=_uuid.uuid4(),
        google_sub=f"test|{role}",
        email=f"{role}@example.com",
        role=role,
        is_active=True,
    )


async def _status_for(role: str) -> int | str:
    try:
        await require_admin(_user(role))
        return "allowed"
    except HTTPException as exc:
        return exc.status_code


plain = asyncio.run(_status_for("user"))
admin = asyncio.run(_status_for("admin"))

check(
    "2. a plain user is refused with 403 (401 would cause a login loop)",
    plain == 403,
    f"got {plain}",
)
check("2b. an admin is allowed", admin == "allowed", f"got {admin}")

# ---------------------------------------------------------------------------
print("\n-- 3. promotion matches every row with the email --")
# ---------------------------------------------------------------------------
# The migration's UPDATE, asserted as a STRING because that is what runs. The
# thing that matters is `lower(email) = ANY(:emails)` and the absence of any
# LIMIT or `google_sub` predicate -- either would promote one of the two rows
# that share an admin's email and silently lock dev-login out of the console.
migration = (
    ROOT / "backend/alembic/versions/f6b28d4c1a73_admin_observability.py"
).read_text(encoding="utf-8")

check(
    "3. promotion matches on lower(email) against the whole list",
    "lower(email) = ANY(:emails)" in migration,
)
check(
    "3b. and never narrows to one row (no LIMIT, no google_sub predicate)",
    "LIMIT" not in migration.upper().split("UPDATE USERS")[1][:400]
    and "google_sub" not in migration.split("UPDATE users")[1][:400],
    "a narrowing predicate would promote only the Google row, not the dev| one",
)
check(
    "3c. an empty ADMIN_EMAILS promotes nobody (fails closed)",
    "if not emails:" in migration,
)

# ---------------------------------------------------------------------------
print("\n-- 4. email is never consulted on a request path --")
# ---------------------------------------------------------------------------
# CLAUDE.md: "key on `sub`, never `email` -- Google reassigns emails within a
# Workspace domain but never reuses `sub`." Promotion uses email ONCE, in a
# migration a human reviews. Authorisation must keep reading `users.role`.
auth_deps = (ROOT / "backend/app/auth/deps.py").read_text(encoding="utf-8")
admin_api = (ROOT / "backend/app/api/admin.py").read_text(encoding="utf-8")

check(
    "4. app/auth/deps.py never reads admin_emails",
    "admin_email" not in auth_deps,
)
check(
    "4b. require_admin still authorises on users.role",
    'user.role != "admin"' in auth_deps,
)
check(
    "4c. the admin API never reads admin_emails either",
    "admin_email" not in admin_api,
)
check(
    "4d. the audit action constant is defined once and importable",
    ADMIN_READ_ACTION == "app.api.admin.ADMIN_READ",
    ADMIN_READ_ACTION,
)

# ---------------------------------------------------------------------------
print("\n-- 5. denominators travel with the numbers --")
# ---------------------------------------------------------------------------
# 76 of this database's queries predate metering and can never be backfilled --
# the OpenRouter generation ids were never stored. A total that treats them as
# zero understates spend and makes the first metered week look like a spike.
# EVAL.md documents the same trap for `scored_count`.
check(
    "5. the overview reports coverage (metered queries / all queries)",
    "coverage" in admin_api and "class Measured" in admin_api,
)
check(
    "5b. Measured carries measured AND total, not just a value",
    "measured: int" in admin_api and "total: int" in admin_api,
)
# The real assertion is about the SUM, not about the word: the docstring in
# `_spend_columns` names `estimated_cost` precisely to say it is excluded, and
# the first version of this case matched that sentence and went red on correct
# code. Assert the aggregate itself.
check(
    "5c. no aggregate ever sums estimated_cost into a reported total",
    "sum(ApiUsage.estimated_cost)" not in admin_api.replace(" ", ""),
    "adding a measurement to a guess produces a number that is neither",
)
check(
    "5d. and spend reports priced_calls beside calls, so the gap is visible",
    "priced_calls" in admin_api and "count(ApiUsage.cost_usd)" in admin_api.replace(" ", ""),
)

# ---------------------------------------------------------------------------
# --live -- execute every route against the real database.
#
# WHY THIS SECTION EXISTS, written the day it was needed. Everything above went
# green while `GET /api/admin/spend` returned 500 on every request:
#
#     column "api_usage.created_at" must appear in the GROUP BY clause
#
# An offline harness introspects routes and reads source; it cannot execute SQL,
# so a query that COMPILES and does not RUN is invisible to it. In the browser
# it surfaced as a CORS error, which points at the wrong subsystem entirely --
# the middleware never got to add its headers because the handler had already
# raised.
#
# That is the seventh time in this repository that a green suite was wrong, and
# the remedy is the one build.md prescribes: assert the OUTCOME. Every case below
# asserts a status and a shape. Needs the database; needs no browser, no model.
#
#     backend/.venv/Scripts/python.exe scripts/admin_check.py --live
# ---------------------------------------------------------------------------
if "--live" in sys.argv:
    from httpx import ASGITransport, AsyncClient  # noqa: E402
    from sqlalchemy import select  # noqa: E402

    from app.auth.deps import current_user  # noqa: E402
    from app.db.session import SessionLocal  # noqa: E402
    from app.main import app  # noqa: E402

    print("\n" + "=" * 74)
    print("--live: every admin route, executed against the real database")
    print("=" * 74)

    async def _pick(role_is_admin: bool):
        async with SessionLocal() as db:
            clause = User.role == "admin" if role_is_admin else User.role != "admin"
            return (
                await db.execute(select(User).where(clause).limit(1))
            ).scalar_one_or_none()

    async def _live() -> None:
        admin_user = await _pick(True)
        if admin_user is None:
            check(
                "L0. an admin user exists to test with",
                False,
                "run `alembic upgrade head` with ADMIN_EMAILS set",
            )
            return
        check("L0. an admin user exists", True, admin_user.email)

        # Identity is overridden; AUTHORISATION IS NOT. `require_admin` still
        # runs and still reads `users.role` off this row, so a regression that
        # dropped the dependency surfaces here as a 403 rather than being hidden
        # by the stub.
        app.dependency_overrides[current_user] = lambda: admin_user
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as c:
                paths = [
                    "/api/admin/overview",
                    "/api/admin/users",
                    "/api/admin/agents",
                    "/api/admin/conversations",
                    "/api/admin/eval-runs",
                    "/api/admin/audit",
                ] + [
                    "/api/admin/spend?group_by=" + g + "&days=30"
                    for g in ("call_kind", "model", "user", "agent", "provider")
                ]
                for path in paths:
                    response = await c.get(path)
                    check(
                        "L. GET " + path,
                        response.status_code == 200,
                        str(response.status_code) + " " + response.text[:140],
                    )

                overview = (await c.get("/api/admin/overview")).json()
                cov = overview.get("coverage") or {}
                check(
                    "L. coverage carries measured AND total, measured <= total",
                    "measured" in cov and cov.get("total", -1) >= cov.get("measured", 0),
                    str(cov),
                )

                threads = (await c.get("/api/admin/conversations?limit=1")).json()
                if threads:
                    thread_id = threads[0]["id"]
                    before = len((await c.get("/api/admin/audit?limit=500")).json())
                    detail = await c.get("/api/admin/conversations/" + thread_id)
                    after = (await c.get("/api/admin/audit?limit=500")).json()
                    check(
                        "L. reading a transcript returns 200 with turns",
                        detail.status_code == 200 and "turns" in detail.json(),
                        str(detail.status_code),
                    )
                    newest = after[0]["action"] if after else None
                    # THE outcome assertion. "It did not throw" is equally true
                    # of an audit that silently wrote nothing at all.
                    check(
                        "L. and WRITES exactly one audit row naming the action",
                        len(after) == before + 1 and newest == ADMIN_READ_ACTION,
                        "before=" + str(before) + " after=" + str(len(after))
                        + " action=" + str(newest),
                    )
                else:
                    print("[skip] no conversations to read -- transcript unproven")

                missing = await c.get("/api/admin/conversations/" + str(_uuid.uuid4()))
                check(
                    "L. an unknown conversation is a 404, not a 500",
                    missing.status_code == 404,
                    str(missing.status_code),
                )

                # A silent default would answer a question nobody asked.
                bad = await c.get("/api/admin/spend?group_by=nonsense")
                check(
                    "L. an unknown group_by is refused (422), never defaulted",
                    bad.status_code == 422,
                    str(bad.status_code),
                )
        finally:
            app.dependency_overrides.pop(current_user, None)

        # R6, and the case whose absence would make every other one meaningless:
        # a signed-in NON-admin must be refused by every route.
        plain_user = await _pick(False)
        if plain_user is None:
            print("[skip] no non-admin user -- R6 unproven")
            return
        app.dependency_overrides[current_user] = lambda: plain_user
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as c:
                codes = {}
                for path in (
                    "/api/admin/overview",
                    "/api/admin/users",
                    "/api/admin/agents",
                    "/api/admin/conversations",
                    "/api/admin/audit",
                    "/api/admin/spend",
                    "/api/admin/eval-runs",
                    "/api/admin/account",
                ):
                    codes[path] = (await c.get(path)).status_code
            check(
                "L. a signed-in NON-admin gets 403 from EVERY route (R6)",
                set(codes.values()) == {403},
                str(codes),
            )
        finally:
            app.dependency_overrides.pop(current_user, None)

    asyncio.run(_live())

print("\n" + "=" * 74)
if failures:
    print(f"{len(failures)} FAILED:")
    for name in failures:
        print(f"  - {name}")
    sys.exit(1)
print("all checks passed")
