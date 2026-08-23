"""Trajectory evaluation: expected tool use, per-turn rubric, run tool config

Change set 16. ONE revision for the whole change set, settled in
`new features/16-agent-evaluation/PLAN.md` §4.4 rather than in a feature file --
two features each adding a column means two revisions racing for the same
`down_revision`.

Four columns, all additive and all nullable, so this is safe to apply before the
merge. Apply it and then MERGE PROMPTLY: between the apply and the merge the
database claims a revision the deployed code does not contain, `alembic upgrade
head` exits non-zero, and the start command dies. Nobody notices while the
running instance is left alone, because `/api/health` keeps answering 200 off the
old instance -- and Render restarts on its own schedule. That window cost three
consecutive failed deploys in August 2026.

**Every one is nullable on purpose, and NULL is a distinct fact in each case.**

  golden_questions.expected_tool_use  NULL = no expectation was authored. The row
      is counted in the denominator and not graded. There is deliberately NO
      backfill: inventing expectations nobody stated would put fabricated ground
      truth into a measuring instrument, which is the failure PRD open items 15
      and 16 record.

  eval_results.trajectory             NULL = this turn predates the second
      scoring pass, or it ran with EVAL_TRAJECTORY_ENABLED=false.

  eval_runs.tools_enabled             NULL = this run predates the column. That is
  eval_runs.max_tool_steps            NOT the same as `false`, and the console
      renders it as "not recorded" rather than "off". EVAL.md already named the
      absence of these two as a real gap: toggle tools between two runs and the
      numbers move for a reason the scorecard cannot state.

Note `expected_tool_use` is a VARCHAR with no CHECK constraint, matching
`golden_questions.expected_behaviour` and `trace_events.event_type` beside it.
The vocabulary is enforced in the API against `EXPECTED_TOOL_USE_VALUES`, where a
bad value produces a 422 naming the allowed set, rather than in Postgres, where it
would produce an IntegrityError naming a constraint.

Revision ID: a3c81f5d2e07
Revises: f6b28d4c1a73
Create Date: 2026-08-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a3c81f5d2e07"
down_revision = "f6b28d4c1a73"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "golden_questions",
        sa.Column("expected_tool_use", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "eval_results",
        sa.Column("trajectory", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("eval_runs", sa.Column("tools_enabled", sa.Boolean(), nullable=True))
    op.add_column("eval_runs", sa.Column("max_tool_steps", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("eval_runs", "max_tool_steps")
    op.drop_column("eval_runs", "tools_enabled")
    op.drop_column("eval_results", "trajectory")
    op.drop_column("golden_questions", "expected_tool_use")
