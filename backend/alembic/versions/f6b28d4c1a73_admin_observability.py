"""admin observability -- metering columns and the admin promotion

Revision ID: f6b28d4c1a73
Revises: e5a17c3f9b62
Create Date: 2026-08-20 11:40:00.000000

ONE migration for the whole change set, settled in
`new features/14-admin-observability/PLAN.md` section 3.2 rather than in a
feature file -- two features each adding a column means two revisions racing for
the same `down_revision`.

Two independent things happen here.

1. `api_usage` GAINS COLUMNS. The table has existed since the initial schema and
   has never held a row, so this is an extension of something unused rather than
   a change to something live. Every added column is nullable (the one exception,
   `cost_is_estimated`, carries a server default), so the upgrade cannot fail on
   existing data -- there is none.

   **Every new foreign key is ON DELETE SET NULL.** Deleting an agent must not
   delete the record that it cost money. CLAUDE.md records the opposite choice
   biting in `query_chunks`, where a CASCADE silently empties the stored contexts
   of every past query when a source document is removed: the scorecard keeps its
   scores and loses its evidence, which is worse than losing both because the
   numbers still render.

2. `users.role` IS SET TO 'admin' for the configured emails.

   **This matches on EMAIL and it must match every row, not one.** CLAUDE.md's
   standing rule is "key on `sub`, never `email`", and that rule is about
   AUTHORISATION -- which is unchanged: `app/auth/deps.py:require_admin` still
   reads `users.role` off a row that was still found by `google_sub`. Email is
   used here, once, at promotion, where a mismatch is a reviewable data change
   rather than a live access decision.

   The reason it must match every row is measured rather than theoretical.
   `POST /api/auth/dev-login` stores `google_sub = "dev|<email>"` precisely so a
   dev identity can never collide with a real Google `sub` -- so signing in for
   real creates a SECOND user row with the same email. On this database:

       admin@example.com   dev|admin@example.com        1 agent,  10 queries
       admin@example.com   10954xxxxxxxxxxx16065        3 agents, 62 queries

   Promoting only the Google row would leave the admin console reachable only by
   a human at a consent screen -- unreachable from `dev-login`, and therefore
   untestable by `scripts/ui_check.py` or by anything automated. That is exactly
   the hole `dev-login` was written to fill, so re-creating it would be a
   regression dressed as a security improvement.

   **It is a no-op when `ADMIN_EMAILS` is unset**, which is the safe direction:
   nobody is promoted and the console 403s for everyone.

The downgrade drops the columns and demotes the promoted rows. It cannot
distinguish an admin this migration created from one set by hand afterwards, so
it demotes every admin -- stated here rather than discovered, because a
downgrade that silently leaves an administrator behind is worse than one that
removes a role a human can restore in one UPDATE.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f6b28d4c1a73"
down_revision = "e5a17c3f9b62"
branch_labels = None
depends_on = None


# Nullable everywhere: `api_usage` is empty, and a nullable column is also how
# "not measured" is represented for the life of the table. A NOT NULL default of
# 0 would make an unmeasured call indistinguishable from a free one, which is the
# single failure this whole feature is written to avoid.
_COLUMNS = (
    sa.Column("agent_id", sa.UUID(as_uuid=True), nullable=True),
    sa.Column("query_id", sa.UUID(as_uuid=True), nullable=True),
    sa.Column("call_kind", sa.String(length=32), nullable=True),
    sa.Column("model", sa.String(length=128), nullable=True),
    sa.Column("served_provider", sa.String(length=64), nullable=True),
    sa.Column("generation_id", sa.String(length=128), nullable=True),
    sa.Column("prompt_tokens", sa.Integer(), nullable=True),
    sa.Column("completion_tokens", sa.Integer(), nullable=True),
    sa.Column("reasoning_tokens", sa.Integer(), nullable=True),
    sa.Column("cached_tokens", sa.Integer(), nullable=True),
    sa.Column("cost_usd", sa.Float(), nullable=True),
    sa.Column(
        "cost_is_estimated",
        sa.Boolean(),
        nullable=False,
        server_default=sa.false(),
    ),
    sa.Column("duration_ms", sa.Integer(), nullable=True),
)


def upgrade() -> None:
    for column in _COLUMNS:
        op.add_column("api_usage", column)

    op.create_foreign_key(
        "fk_api_usage_agent",
        "api_usage",
        "agents",
        ["agent_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_api_usage_query",
        "api_usage",
        "queries",
        ["query_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # The console's three access patterns: a global time series, one user's
    # spend, one agent's spend. `call_kind` is indexed because grouping by it is
    # the question "what is the critic actually costing us".
    op.create_index("ix_api_usage_created", "api_usage", ["created_at"])
    op.create_index("ix_api_usage_user_created", "api_usage", ["user_id", "created_at"])
    op.create_index(
        "ix_api_usage_agent_created", "api_usage", ["agent_id", "created_at"]
    )
    op.create_index("ix_api_usage_query_id", "api_usage", ["query_id"])
    op.create_index("ix_api_usage_call_kind", "api_usage", ["call_kind"])

    # ------------------------------------------------------------------
    # The promotion. Imported here rather than at module scope so that
    # `alembic history` and an offline `--sql` render do not need settings.
    # ------------------------------------------------------------------
    from app.config import settings

    emails = settings.admin_email_list
    if not emails:
        print("[admin] ADMIN_EMAILS is unset -- no user promoted.")
        return

    result = op.get_bind().execute(
        sa.text(
            "UPDATE users SET role = 'admin' "
            "WHERE lower(email) = ANY(:emails) AND role <> 'admin'"
        ),
        {"emails": emails},
    )
    # Printed, because a promotion that matched nothing is the failure this is
    # most likely to have -- a typo in an env var -- and it is invisible
    # otherwise. ASCII only: the Windows console mangles anything else.
    print(f"[admin] promoted {result.rowcount} user row(s) for {len(emails)} email(s)")
    if result.rowcount == 0:
        print("[admin] WARNING: no row matched. Check ADMIN_EMAILS against `users`.")


def downgrade() -> None:
    op.execute("UPDATE users SET role = 'user' WHERE role = 'admin'")

    for name in (
        "ix_api_usage_call_kind",
        "ix_api_usage_query_id",
        "ix_api_usage_agent_created",
        "ix_api_usage_user_created",
        "ix_api_usage_created",
    ):
        op.drop_index(name, table_name="api_usage")

    op.drop_constraint("fk_api_usage_query", "api_usage", type_="foreignkey")
    op.drop_constraint("fk_api_usage_agent", "api_usage", type_="foreignkey")

    for column in reversed(_COLUMNS):
        op.drop_column("api_usage", column.name)
