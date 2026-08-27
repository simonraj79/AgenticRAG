"""better auth identity link -- users.better_auth_id

Revision ID: c1d9e4a7b2f3
Revises: a3c81f5d2e07
Create Date: 2026-08-27 09:20:00.000000

ONE column, and it is deliberately the smallest possible change.

Better Auth owns four tables of its own (`user`, `session`, `account`,
`verification`) plus `jwks` for the JWT plugin. NONE of them are created here.
They are migrated by that service's own CLI -- `npx @better-auth/cli migrate` --
because two migration systems owning one table is a worse problem than any it
would solve. `app/db/migration_filter.py` is the other half of that decision: it
stops `--autogenerate` from noticing those five tables and emitting DROP TABLE
for each, which it otherwise does, silently, in a migration that reviews
cleanly. Read that module before touching this one.

WHY A LINK COLUMN AND NOT A REPLACEMENT.

`users.id` is the target of every tenancy-bearing foreign key in the schema --
`agents.owner_user_id`, `documents.uploaded_by_user_id`, `sessions.user_id`,
`queries.user_id`, `api_usage.user_id`, `conversations` through its agent.
Pointing those at Better Auth's `user` table is not a migration, it is a rewrite
of the schema's spine, and it would have to be done atomically against a live
database holding real work.

So `users` stays the identity table and gains one nullable link.
`app/auth/identity.py` documents the four-step resolution that uses it; the
short version is that `google_sub` remains the KEY and this column is a cache of
"which Better Auth account resolved to this row", populated on a user's first
Better-Auth-authenticated request and never again.

NULLABLE, AND IT STAYS NULLABLE. Three populations legitimately have no value:
every row that predates the cutover until its owner next signs in; every
`dev-login` row, because that shim mints an app session directly and never
touches Better Auth; and any row created while the Authlib path is still live.
A NOT NULL column here would make the cutover a flag day.

UNIQUE, because two application users sharing one Better Auth account would mean
one person's sign-in resolving to either of two workspaces depending on row
order -- the same class of defect as `select(User).limit(1)` with no ORDER BY,
which CLAUDE.md records being caught in review rather than by a test. Postgres
allows many NULLs under a UNIQUE constraint, so this costs the nullable
population nothing.

This migration is additive and has no data dependency, so `downgrade` is exact:
it drops a column nothing else references.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c1d9e4a7b2f3"
down_revision = "a3c81f5d2e07"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("better_auth_id", sa.String(length=255), nullable=True),
    )
    # Named explicitly rather than left to Alembic's default, so `downgrade`
    # names the same object and does not depend on a naming convention that can
    # change between SQLAlchemy versions.
    op.create_index(
        "ix_users_better_auth_id",
        "users",
        ["better_auth_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_users_better_auth_id", table_name="users")
    op.drop_column("users", "better_auth_id")
