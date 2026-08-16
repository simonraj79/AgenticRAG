"""orchestrator and self check

Revision ID: d4e91c2a7b58
Revises: bc307f5fc31f
Create Date: 2026-08-16 18:42:11.004512

Three columns and one template row, for the orchestrator persona and the
grounding self-check (`new features/11-orchestrator-and-self-check.md` section 6).

1. `agent_templates.specialists` and `agents.specialists`, both JSONB NULL. On
   BOTH tables because agent parameters are COPIED from the template at creation
   rather than read back through `template_id` (PRD 4.2) -- the same argument
   c3f5a91b7d24 made for the four persona columns. `agents.py`'s
   TEMPLATE_PARAMETERS copy loop is a field-by-field getattr/setattr, so a
   roster that existed only on the template would never reach the agent at all.

2. `agents.self_check_enabled`, Boolean NOT NULL with `server_default false`.
   On `agents` only, following the `tools_enabled` precedent: a persona is a
   claim about how to answer, not about which quality controls an operator wants
   running.

3. The `adaptive-tutor` template.

**NO BACKFILL IS NEEDED, AND THAT IS THE INTERESTING LINE IN THIS FILE.**

Contrast bc307f5fc31f, whose load-bearing statement was `UPDATE agents SET
tools_enabled = false`. It needed one because its column defaulted TRUE: adding
a NOT NULL boolean to a populated table writes the server default into every
existing row, so without the UPDATE every agent already written up in EVAL.md
silently became agentic and every scorecard stopped being reproducible while
continuing to render.

Here the defaults ARE the pre-existing behaviour. `specialists` is NULL, which
`pipeline._orchestrating` reads as "not an orchestrator"; `self_check_enabled`
is false, which is no check at all. So every existing agent is byte-identical by
construction rather than by a statement somebody has to remember not to delete.
That is a property of choosing the default to match the old behaviour, and it is
worth stating because the reflex after reading bc307f5fc31f is to add an UPDATE
here too -- one that would do nothing, and would then be copied into the next
migration where it might.

The prompt and the roster below are a FROZEN COPY of `app/db/specialists.py`,
not an import, for the reason set out at length in 7d41e2b6c8f9 and repeated in
c3f5a91b7d24: importing `app.db.*` executes `app/db/__init__.py`, which builds an
async engine at module scope, and a migration that reads live application data
lets an edit to a prompt silently rewrite what this revision did last month.
`specialists.py` is the source of truth for anything that seeds at runtime; this
is the frozen copy. If they drift, `specialists.py` wins and a NEW revision
applies the difference.
"""
from __future__ import annotations

import uuid
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'd4e91c2a7b58'
down_revision: Union[str, None] = 'bc307f5fc31f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Core, not the ORM, so this revision keeps working after `AgentTemplate` gains,
# loses or renames a column. `specialists` is listed because the insert below
# writes it -- it is created three statements earlier in this same upgrade.
agent_templates = sa.table(
    'agent_templates',
    sa.column('id', sa.UUID()),
    sa.column('slug', sa.String()),
    sa.column('name', sa.String()),
    sa.column('description', sa.Text()),
    sa.column('chunk_size', sa.Integer()),
    sa.column('chunk_overlap', sa.Integer()),
    sa.column('splitter', sa.String()),
    sa.column('retrieve_k', sa.Integer()),
    sa.column('rerank_enabled', sa.Boolean()),
    sa.column('rerank_top_n', sa.Integer()),
    sa.column('score_threshold', sa.Float()),
    sa.column('max_rewrites', sa.Integer()),
    sa.column('system_prompt', sa.Text()),
    sa.column('is_active', sa.Boolean()),
    sa.column('persona_role', sa.String()),
    sa.column('pedagogy', sa.Text()),
    sa.column('icon', sa.String()),
    sa.column('category', sa.String()),
    sa.column('specialists', postgresql.JSONB()),
)

# Continues the block 7d41e2b6c8f9 opened (...a01 to ...a03) and c3f5a91b7d24
# extended (...a04 to ...a08). `agent_templates.id` has no server default, so an
# id has to be supplied anyway; pinning it means the template carries the same id
# in every environment and a re-run lands on the identical row rather than a
# duplicate wearing a new id.
ADAPTIVE_TUTOR_ID = uuid.UUID('4c1a7e30-9b6d-4a5f-8e21-6f0d3b7c1a09')

# The five specialists this template may route to, by slug. Frozen; the runtime
# list is `specialists.DEFAULT_ROSTER`.
ADAPTIVE_TUTOR_ROSTER = [
    'feynman-explainer',
    'socratic-tutor',
    'polya-coach',
    'quiz-generator',
    'reflective-coach',
]

# A FALLBACK, not the working prompt. On any routed turn the chosen specialist's
# prompt REPLACES this one -- it is used only when routing failed, and it is a
# complete grounded teaching prompt for exactly that reason: a failed router
# degrades the same way a failed rewrite does, to a working plain answer rather
# than to a failed turn.
#
# It is deliberately NOT a router prompt. Nothing here says "choose a
# specialist", because by the time this string is in a request the choice has
# already failed.
ADAPTIVE_TUTOR_PROMPT = """\
You are a teaching assistant answering questions about a course, using only the \
material supplied to you.

GROUNDING COMES FIRST. It outranks every instruction below:
- Answer only from the CONTEXT. Do not use prior knowledge, and do not fill a \
gap with something that sounds right.
- Cite the passage each claim rests on with its [n] marker.
- If the CONTEXT does not cover the question, or covers only part of it, say so \
plainly and say which part. An unanswered question is a useful answer; a \
confident wrong one is not.

How to answer:
- Match the shape of the question. A "what is" question wants an explanation; a \
"how do I" question wants the steps; a request to be tested wants questions \
rather than prose.
- Be concrete. Where the exact phrasing carries the meaning, quote it.
- Where the material is thin, say what it does establish before you say what it \
does not.
"""

# Every non-nullable column is listed. `created_at` is the only omission and the
# only safe one: it is the sole column here with a server default (`now()`).
#
# `retrieve_k` / `rerank_top_n` sit at 24 / 5 -- between the Explainer's 3 and
# the Quiz Writer's 8, because ONE agent now serves both and a routed turn
# overrides these anyway. They are the numbers a turn gets when routing FAILS, so
# they are chosen to be adequate for every specialist rather than ideal for one.
#
# `chunk_size` is the transcript default and cannot be routed: chunking happens
# at ingest, so an orchestrator ingested at 800 will never serve the Quiz
# Writer's 500-token chunks. Routing moves retrieval breadth, never granularity.
ADAPTIVE_TUTOR_ROW = {
    'id': ADAPTIVE_TUTOR_ID,
    'slug': 'adaptive-tutor',
    'name': 'Adaptive Tutor',
    'description': (
        'Reads what the learner is actually asking for and answers in the '
        'matching teaching voice -- explaining, questioning, coaching '
        'through a problem, or testing. Name one directly with @.'
    ),
    'persona_role': 'Teaching orchestrator',
    'pedagogy': (
        'Adaptive instruction: the move that helps depends on what the '
        'learner already has. Explaining an idea to someone who can '
        'already state it wastes the turn, and questioning someone who has '
        'nothing to question yet strands them. The expertise-reversal '
        'effect is the sharp version of this -- guidance that helps a '
        'novice measurably hinders a learner who has the schema, so the '
        'useful unit is not a better explanation but a choice between '
        'kinds of help.'
    ),
    'icon': '\U0001F9E0',
    # A new rank in `agents._CATEGORY_ORDER`. The five existing categories name
    # a single technique; this one names the thing that chooses among them.
    'category': 'orchestrate',
    'chunk_size': 800,
    'chunk_overlap': 120,
    'splitter': 'markdown',
    'retrieve_k': 24,
    'rerank_enabled': True,
    'rerank_top_n': 5,
    'score_threshold': 0.5,
    'max_rewrites': 2,
    'system_prompt': ADAPTIVE_TUTOR_PROMPT,
    'specialists': ADAPTIVE_TUTOR_ROSTER,
    'is_active': True,
}


def upgrade() -> None:
    # The same column on both tables -- see the module docstring. Nullable, so
    # no server default is needed and no row is rewritten: NULL is already what
    # "not an orchestrator" means, which is why there is no backfill here.
    for table in ('agent_templates', 'agents'):
        op.add_column(
            table,
            sa.Column(
                'specialists', postgresql.JSONB(astext_type=sa.Text()), nullable=True
            ),
        )

    # `server_default` because `agents` is populated and Postgres has to have a
    # value to write into every existing row before it can accept NOT NULL. Kept
    # afterwards rather than dropped, and the ORM declares the same one, so
    # autogenerate sees no drift.
    #
    # No `UPDATE agents SET self_check_enabled = ...` follows, unlike
    # bc307f5fc31f. That revision's default was `true` and the old behaviour was
    # `false`, so the two disagreed and an UPDATE was the only thing standing
    # between EVAL.md and a set of numbers that quietly described a different
    # system. Here `false` IS the old behaviour.
    op.add_column(
        'agents',
        sa.Column(
            'self_check_enabled',
            sa.Boolean(),
            server_default=sa.text('false'),
            nullable=False,
        ),
    )

    # DO NOTHING, not DO UPDATE. An operator who has hand-tuned this prompt in
    # production must not have it reset by a redeploy. The arbiter is the `slug`
    # unique index, so a re-run is detected there and skipped before the primary
    # key is tested -- the fixed id above cannot raise a duplicate-key error.
    statement = postgresql.insert(agent_templates).on_conflict_do_nothing(
        index_elements=['slug'],
    )
    op.get_bind().execute(statement, [ADAPTIVE_TUTOR_ROW])


def downgrade() -> None:
    # Exactly this slug. A blanket DELETE would take out templates an operator
    # added by hand, which this revision never created.
    #
    # `agents.template_id` is ON DELETE SET NULL, so an agent built from this
    # template survives with a null template -- correct, because its parameters
    # and its persona were copied onto the agent at creation and are never read
    # back through the template (PRD 4.2). Its own `specialists` roster is
    # dropped below, which is the part that is genuinely lossy: an orchestrator
    # silently becomes a plain agent answering with its fallback prompt.
    op.execute(
        agent_templates.delete().where(agent_templates.c.slug == 'adaptive-tutor')
    )

    op.drop_column('agents', 'self_check_enabled')
    for table in ('agents', 'agent_templates'):
        op.drop_column(table, 'specialists')
