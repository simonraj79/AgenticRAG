"""eval scoping and summary

Revision ID: b8d2f47a91c5
Revises: c3f5a91b7d24
Create Date: 2026-08-15 18:41:09.226704

Scopes evaluation to the agent, and gives a run enough columns to be readable
while it is still running and trustworthy after it has finished.

1. `golden_questions.agent_id` and `eval_runs.agent_id`. A golden set is only
   meaningful against ONE corpus: a question about lecture transcripts scored
   against a policy agent measures nothing, and it measures nothing QUIETLY -
   the metrics still return numbers, they are just numbers about the wrong
   corpus. Without these columns every agent shares one global golden set,
   which is silently wrong rather than loudly broken.

   Both are NULLABLE, for the reason `queries.conversation_id` is: rows may
   already exist unscoped, and a NOT NULL column cannot be added to a populated
   table without a backfill - which here would mean guessing which agent a
   question was written for, i.e. inventing the very scoping the column exists
   to record.

2. `eval_runs.generation_model` beside the existing `judge_model`. When the two
   are equal the run is self-judged, and a scorecard that cannot tell you that
   is a scorecard you cannot trust.

3. `eval_runs.summary` as JSONB rather than four float columns: it also carries
   `weakest_metric`, `scored_count` and the refusal tally, the shape will grow
   as metrics are added, and it is written once at the end of a run and read
   whole rather than queried field-by-field.

4. Progress and error columns, so a background run is observable mid-flight and
   a single failed question does not void an entire scorecard.

Core only, no ORM import - the reason set out in 7d41e2b6c8f9: importing
`app.db.*` executes `app/db/__init__.py`, which builds an async engine at module
scope. Core also keeps this revision working after the models drift.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'b8d2f47a91c5'
down_revision: Union[str, None] = 'c3f5a91b7d24'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Named rather than left to `create_foreign_key(None, ...)`. Postgres would
# generate these exact names anyway, but an unnamed constraint cannot be
# dropped: `drop_constraint(None, ...)` is not a valid statement, so the
# downgrade would fail at exactly the moment somebody needed it.
GOLDEN_QUESTIONS_AGENT_FK = 'golden_questions_agent_id_fkey'
EVAL_RUNS_AGENT_FK = 'eval_runs_agent_id_fkey'


def upgrade() -> None:
    # --- golden_questions -------------------------------------------------
    # ON DELETE CASCADE: a golden set is written against one corpus and has no
    # meaning without it. Deleting the agent must take the questions with it
    # rather than leave them pointing at a namespace that no longer exists.
    op.add_column('golden_questions', sa.Column('agent_id', sa.UUID(), nullable=True))
    op.create_index(
        op.f('ix_golden_questions_agent_id'), 'golden_questions', ['agent_id'], unique=False
    )
    op.create_foreign_key(
        GOLDEN_QUESTIONS_AGENT_FK,
        'golden_questions',
        'agents',
        ['agent_id'],
        ['id'],
        ondelete='CASCADE',
    )

    # server_default on both, because the table may be populated and Postgres
    # has to have a value to write into every existing row before it can accept
    # NOT NULL. The default is kept afterwards rather than dropped: it is what
    # lets an INSERT that predates these columns still succeed, and the ORM
    # declares the same default so autogenerate sees no drift.
    op.add_column(
        'golden_questions',
        sa.Column('source', sa.String(length=16), server_default='manual', nullable=False),
    )
    op.add_column(
        'golden_questions',
        sa.Column('order_index', sa.Integer(), server_default='0', nullable=False),
    )
    # The editor reads exactly this: one agent's set, in display order. The
    # single-column index above finds the set; this one hands it back already
    # ordered, so opening a long golden set never sorts.
    op.create_index(
        'ix_golden_questions_agent_order',
        'golden_questions',
        ['agent_id', 'order_index'],
        unique=False,
    )

    # --- eval_runs --------------------------------------------------------
    op.add_column('eval_runs', sa.Column('agent_id', sa.UUID(), nullable=True))
    op.create_index(op.f('ix_eval_runs_agent_id'), 'eval_runs', ['agent_id'], unique=False)
    op.create_foreign_key(
        EVAL_RUNS_AGENT_FK,
        'eval_runs',
        'agents',
        ['agent_id'],
        ['id'],
        ondelete='CASCADE',
    )

    # The model that produced the ANSWERS, next to the model that graded them.
    # Nullable: runs made before this column exist and there is nothing to
    # backfill them from, and claiming a model they may not have used would be
    # worse than admitting the run does not say.
    op.add_column('eval_runs', sa.Column('generation_model', sa.String(length=128), nullable=True))
    op.add_column(
        'eval_runs',
        sa.Column('summary', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    # A run that has not started is honestly 0 of 0; NULL would force every
    # reader to decide what a missing count means.
    op.add_column(
        'eval_runs', sa.Column('progress_done', sa.Integer(), server_default='0', nullable=False)
    )
    op.add_column(
        'eval_runs', sa.Column('progress_total', sa.Integer(), server_default='0', nullable=False)
    )
    op.add_column('eval_runs', sa.Column('error', sa.Text(), nullable=True))
    # The history list reads exactly this: one agent's runs, newest first. DESC
    # in the index rather than at the call site, so the newest-first read -
    # which is every read - is a forward scan.
    op.create_index(
        'ix_eval_runs_agent_created',
        'eval_runs',
        ['agent_id', sa.text('created_at DESC')],
        unique=False,
    )

    # --- eval_results -----------------------------------------------------
    # A correct refusal is a success case with no faithfulness score to report:
    # the metrics have nothing to grade, so without this column a passed
    # refusal question is four NULLs, indistinguishable from a crashed one.
    op.add_column('eval_results', sa.Column('behaviour_ok', sa.Boolean(), nullable=True))
    # Per-question failure, so one bad row records its reason and the run
    # continues instead of voiding the whole scorecard.
    op.add_column('eval_results', sa.Column('error', sa.Text(), nullable=True))


def downgrade() -> None:
    # Reverse order of upgrade(), table by table. Dropping a column in Postgres
    # takes its indexes and constraints with it, so the explicit drops below are
    # redundant - and stated anyway, so that this function reverses upgrade()
    # statement for statement rather than relying on a cascade to tidy up.
    #
    # Genuinely lossy: every value in these columns is destroyed, and the
    # agent scoping cannot be reconstructed on a re-upgrade.
    op.drop_column('eval_results', 'error')
    op.drop_column('eval_results', 'behaviour_ok')

    op.drop_index('ix_eval_runs_agent_created', table_name='eval_runs')
    op.drop_column('eval_runs', 'error')
    op.drop_column('eval_runs', 'progress_total')
    op.drop_column('eval_runs', 'progress_done')
    op.drop_column('eval_runs', 'summary')
    op.drop_column('eval_runs', 'generation_model')
    op.drop_constraint(EVAL_RUNS_AGENT_FK, 'eval_runs', type_='foreignkey')
    op.drop_index(op.f('ix_eval_runs_agent_id'), table_name='eval_runs')
    op.drop_column('eval_runs', 'agent_id')

    op.drop_index('ix_golden_questions_agent_order', table_name='golden_questions')
    op.drop_column('golden_questions', 'order_index')
    op.drop_column('golden_questions', 'source')
    op.drop_constraint(GOLDEN_QUESTIONS_AGENT_FK, 'golden_questions', type_='foreignkey')
    op.drop_index(op.f('ix_golden_questions_agent_id'), table_name='golden_questions')
    op.drop_column('golden_questions', 'agent_id')
