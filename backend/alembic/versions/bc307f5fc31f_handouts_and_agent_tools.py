"""handouts and agent tools

Revision ID: bc307f5fc31f
Revises: b8d2f47a91c5
Create Date: 2026-08-16 07:07:53.180400

Adds the durable half of the agentic tool loop, and the two columns that decide
whether an agent runs one at all.

1. `handouts`. The tool loop can now write and run Python, and Python produces
   files - a chart, a slide deck, a table - inside a temporary directory that is
   deleted moments later. Without this table the one new thing the product can
   make would exist only for the length of an HTTP response. The same table
   holds handouts a user asked for directly from the panel; `origin` is the only
   thing that separates the two, because everything downstream treats them
   identically.

   `content` is `LargeBinary` and the bytes live in Postgres. Object storage is
   the right long-term answer (PRD open item 10) and is deliberately not built
   here; the mitigation is a per-file cap, a per-agent quota, and a DEFERRED
   mapping on the ORM side so a list query never selects the column.

   Two FK asymmetries are decisions rather than oversights. `conversation_id` is
   ON DELETE CASCADE: deleting a thread is an explicit user action, and a
   handout filed under a conversation that no longer exists is unreachable in
   the UI. `query_id` is ON DELETE **SET NULL**: a handout OUTLIVES the turn
   that produced it, the query row is provenance only, and CASCADE there would
   silently destroy a deck the user downloaded a week ago - the same class of
   quiet data loss as the `query_chunks` cascade.

2. `agents.tools_enabled` and `agents.max_tool_steps`. Whether generation runs
   as a bounded tool loop is part of what an agent IS - it changes the answer,
   the trace and the latency - so it is stored configuration for the same PRD
   4.2 reason every tuning parameter is, not a global setting alone.

3. **The backfill, which is the load-bearing statement in this file.**
   `tools_enabled` gets `server_default true` so new agents are agentic out of
   the box, and every existing row is then set to false. See the comment on that
   line: without it, every scorecard already written up in EVAL.md silently
   stops being reproducible.

Core only, no ORM import - the reason set out in 7d41e2b6c8f9: importing
`app.db.*` executes `app/db/__init__.py`, which builds an async engine at module
scope. Core also keeps this revision working after the models drift.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'bc307f5fc31f'
down_revision: Union[str, None] = 'b8d2f47a91c5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Named rather than left to Postgres, for the reason given in b8d2f47a91c5: an
# unnamed constraint cannot be dropped by name, so anything that later has to
# alter one has nothing to name. `drop_table` below takes all four with it, so
# the downgrade does not reference them - they are named for the migration after
# this one, not for this one.
HANDOUTS_AGENT_FK = 'handouts_agent_id_fkey'
HANDOUTS_CONVERSATION_FK = 'handouts_conversation_id_fkey'
HANDOUTS_QUERY_FK = 'handouts_query_id_fkey'
HANDOUTS_USER_FK = 'handouts_created_by_user_id_fkey'

# Declared as globals for the same reason: the downgrade drops each one by name,
# and a name typed twice is a name that can differ.
HANDOUTS_AGENT_INDEX = 'ix_handouts_agent_id'
HANDOUTS_AGENT_CREATED_INDEX = 'ix_handouts_agent_created'
HANDOUTS_CONVERSATION_INDEX = 'ix_handouts_conversation'


def upgrade() -> None:
    # --- handouts ---------------------------------------------------------
    op.create_table(
        'handouts',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.Column('agent_id', sa.UUID(), nullable=False),
        sa.Column('conversation_id', sa.UUID(), nullable=True),
        sa.Column('query_id', sa.UUID(), nullable=True),
        sa.Column('created_by_user_id', sa.UUID(), nullable=True),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('filename', sa.String(length=255), nullable=False),
        sa.Column('mime_type', sa.String(length=128), nullable=False),
        sa.Column('byte_size', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('origin', sa.String(length=16), nullable=False),
        # The bytes. NULL until a background job finishes writing them, which is
        # why `status` exists and why a row can be listed before it has content.
        sa.Column('content', sa.LargeBinary(), nullable=True),
        sa.Column('preview_text', sa.Text(), nullable=True),
        sa.Column('source_code', sa.Text(), nullable=True),
        # `meta`, not `metadata`. The attribute name `metadata` is reserved on a
        # declarative Base, and `audit_log` only carries a column of that name
        # because it shipped before the collision was understood. Nothing here
        # has to preserve it, so the column and the attribute share one name.
        sa.Column('meta', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        # CASCADE: a handout is made out of one agent's corpus and means nothing
        # without it.
        sa.ForeignKeyConstraint(
            ['agent_id'], ['agents.id'], name=HANDOUTS_AGENT_FK, ondelete='CASCADE'
        ),
        # CASCADE: deleting a thread is an explicit user action, and a handout
        # pointing at a deleted conversation is unreachable in the UI.
        sa.ForeignKeyConstraint(
            ['conversation_id'],
            ['conversations.id'],
            name=HANDOUTS_CONVERSATION_FK,
            ondelete='CASCADE',
        ),
        # SET NULL, and deliberately NOT CASCADE. A handout outlives the turn
        # that produced it; the query is provenance, not ownership. NULL here
        # reads as "made outside any turn", which is what a recipe handout is.
        sa.ForeignKeyConstraint(
            ['query_id'], ['queries.id'], name=HANDOUTS_QUERY_FK, ondelete='SET NULL'
        ),
        # SET NULL: the creator is audit, not access control - `agent_id` is the
        # scoping key. Losing the user must not lose the file.
        sa.ForeignKeyConstraint(
            ['created_by_user_id'],
            ['users.id'],
            name=HANDOUTS_USER_FK,
            ondelete='SET NULL',
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f(HANDOUTS_AGENT_INDEX), 'handouts', ['agent_id'], unique=False)
    # The panel's two reads. One agent's handouts newest first, and one
    # conversation's. DESC in the index rather than at the call site, so the
    # newest-first read - which is every read - is a forward scan.
    op.create_index(
        op.f(HANDOUTS_AGENT_CREATED_INDEX),
        'handouts',
        ['agent_id', sa.text('created_at DESC')],
        unique=False,
    )
    op.create_index(
        op.f(HANDOUTS_CONVERSATION_INDEX), 'handouts', ['conversation_id'], unique=False
    )

    # --- agents -----------------------------------------------------------
    # server_default on both, because `agents` is populated and Postgres has to
    # have a value to write into every existing row before it can accept NOT
    # NULL. The defaults are kept afterwards rather than dropped, and the ORM
    # declares the same ones, so autogenerate sees no drift.
    op.add_column(
        'agents',
        sa.Column('tools_enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    )
    op.add_column(
        'agents',
        sa.Column('max_tool_steps', sa.Integer(), server_default=sa.text('3'), nullable=False),
    )

    # THE ASYMMETRIC BACKFILL. Read this before deleting it.
    #
    # The line above just wrote `true` into every existing agent, which is what a
    # NOT NULL column with that server default has to do. This line takes it
    # back - but only for rows that already existed, because a row inserted after
    # this migration runs picks the server default up again and comes out `true`.
    # New agents are agentic out of the box; every agent that predates this
    # revision keeps behaving exactly as it did.
    #
    # That matters because the tool loop is not a refinement of the existing
    # turn, it is a different turn: the model can search its own corpus again and
    # can run Python, so the same question yields a different answer, a different
    # trace and a different latency. Every scorecard in EVAL.md was measured
    # against an agent that could do neither. Without this statement those runs
    # do not fail - they silently stop being reproducible, which is strictly
    # worse, because the numbers still render and nothing anywhere says they now
    # describe a system that no longer exists. Same failure class as the refusal
    # detector and the `strictness=3` bug: the measurement quietly becoming wrong
    # while continuing to look right.
    #
    # A plain UPDATE and not a `WHERE created_at < now()`: at this point in the
    # transaction every row in the table is by definition an existing one.
    op.execute("UPDATE agents SET tools_enabled = false")


def downgrade() -> None:
    # Reverse order of upgrade(). Genuinely lossy in one direction that no
    # downgrade can help with: dropping `handouts` destroys every generated file,
    # and the bytes exist nowhere else - unlike `chunks.text`, there is no source
    # of truth to rebuild them from. The per-agent `tools_enabled` choice is
    # destroyed too, and a re-upgrade re-runs the backfill, so every agent comes
    # back with the loop off regardless of what it was set to.
    op.drop_column('agents', 'max_tool_steps')
    op.drop_column('agents', 'tools_enabled')

    # Dropping the table takes its indexes and foreign keys with it, so these
    # three drops are redundant - and stated anyway, so that this function
    # reverses upgrade() statement for statement rather than relying on a cascade
    # to tidy up.
    op.drop_index(op.f(HANDOUTS_CONVERSATION_INDEX), table_name='handouts')
    op.drop_index(op.f(HANDOUTS_AGENT_CREATED_INDEX), table_name='handouts')
    op.drop_index(op.f(HANDOUTS_AGENT_INDEX), table_name='handouts')
    op.drop_table('handouts')
