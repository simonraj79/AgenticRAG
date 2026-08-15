"""seed agent templates

Revision ID: 7d41e2b6c8f9
Revises: 8603d8ed3e6d
Create Date: 2026-08-15 14:36:02.118940

Seeds the three starting templates from PRD sections 4.2 and 10: Lecture Q&A,
Policy Lookup, From scratch. Agent creation is blocked without them (PRD 10,
open item 1).

Why the template data is copied in here rather than imported from
`app/db/seed.py`, which holds the same values:

1. It cannot be imported cheaply. `app.db.seed` is data-only, but reaching it
   executes `app/db/__init__.py`, which imports the ORM models and constructs
   an async engine at module scope. A migration that imports app models breaks
   the moment a model changes, and one that builds an engine breaks whenever
   the environment differs from the developer's.

2. Even if it were free, it would be wrong. A migration is a record of what the
   database was actually given on a date. Reading live application data means
   editing a prompt in `seed.py` silently rewrites history: a database migrated
   last month and one migrated from scratch today would disagree about what
   this revision did.

So `seed.py` is the source of truth for anything that seeds at runtime, and
this file is the frozen copy. If they drift, `seed.py` wins and the difference
is applied by a new revision, never by editing this one.
"""
from __future__ import annotations

import uuid
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '7d41e2b6c8f9'
down_revision: Union[str, None] = '8603d8ed3e6d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# A lightweight stand-in for the table, declared against Core rather than the
# ORM so this revision keeps working after `AgentTemplate` gains, loses or
# renames a column.
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
)

# Fixed rather than generated. `agent_templates.id` has no server default -- the
# ORM's `default=uuid.uuid4` is Python-side and invisible to a Core insert -- so
# an id has to be supplied anyway, and pinning it means the same template
# carries the same id in every environment. That makes `agents.template_id`
# comparable across a dev database and production, and it makes a re-run land on
# the identical row instead of a duplicate wearing a new id.
LECTURE_QA_ID = uuid.UUID('4c1a7e30-9b6d-4a5f-8e21-6f0d3b7c1a01')
POLICY_LOOKUP_ID = uuid.UUID('4c1a7e30-9b6d-4a5f-8e21-6f0d3b7c1a02')
FROM_SCRATCH_ID = uuid.UUID('4c1a7e30-9b6d-4a5f-8e21-6f0d3b7c1a03')

# The prompt is the refusal mechanism, not `score_threshold`. Measured on one
# corpus file, an off-topic question scored 0.5765 -- above the 0.5 threshold,
# so no rewrite was triggered -- and was still refused correctly, because the
# prompt forbade answering outside the context. Each prompt therefore carries
# its own grounding and refusal rules. See CLAUDE.md, "Retrieval calibration".
LECTURE_QA_PROMPT = """\
You are a teaching assistant answering questions about a course, using only the \
lecture material supplied to you.

Rules:
- Answer only from the CONTEXT below. Do not use prior knowledge, and do not \
fill a gap from what you know about the subject generally.
- If the CONTEXT does not contain the answer, say so plainly and stop. A correct \
refusal is a better answer than a plausible guess.
- Cite the source filename in brackets after each claim you take from it.
- Where the exact phrasing carries the meaning, quote the material; otherwise be \
concise and concrete.
- If the question is ambiguous, answer the reading the CONTEXT supports and say \
which reading you took."""

POLICY_LOOKUP_PROMPT = """\
You are a policy assistant. You answer questions about the policy documents \
supplied to you, and about nothing else.

Rules:
- Answer only from the CONTEXT below. Never rely on prior knowledge of similar \
policies elsewhere; near-identical wording in another organisation's policy is \
not evidence about this one.
- Quote the governing clause verbatim in quotation marks, and cite the source \
filename together with any clause or section number that appears in the text. \
State the rule first, then any exception recorded in the same passage.
- If the CONTEXT does not cover the question, or covers it only in part, say \
exactly that and stop. Do not infer, generalise, or reason by analogy from a \
neighbouring clause. An unanswered question sends the reader to a human; a \
confident wrong answer does not.
- Report what the policy says, not what it probably means. Do not soften a \
requirement into advice."""

FROM_SCRATCH_PROMPT = """\
Answer the question using only the CONTEXT below.

- Do not use prior knowledge.
- If the CONTEXT does not contain the answer, say so and stop.
- Cite the source filename for each claim."""

# Every non-nullable column is listed explicitly. `created_at` is the one
# omission and the only one that is safe: it is the sole column here with a
# server default (`now()`).
#
# Parameter choices, against PRD 10:
#   lecture-qa    - the documented default. 800/120 markdown-aware, sized for
#                   lecture transcripts, which dominate the corpus; retrieval
#                   follows the workshop unchanged (k=20 -> rerank -> top-3,
#                   rewrite below 0.5, at most 2 rewrites).
#   policy-lookup - 400/60. A policy answer is one clause, so an 800-token chunk
#                   drags in neighbouring clauses the reranker then has to
#                   discount; 400 keeps a clause roughly whole, and overlap
#                   holds the same 15% ratio. Rerank stays on at top-3 because
#                   precision matters more than recall here, and k stays at 20
#                   so the reranker is not starved of candidates.
#   from-scratch  - every value is the model default. That is the point: "create
#                   your own" is this template, not a separate branch (PRD 4.2).
TEMPLATE_ROWS = [
    {
        'id': LECTURE_QA_ID,
        'slug': 'lecture-qa',
        'name': 'Lecture Q&A',
        'description': (
            'The PRD default. Large markdown-aware chunks sized for lecture '
            'transcripts, where a single answer is spread over several turns of '
            'dialogue.'
        ),
        'chunk_size': 800,
        'chunk_overlap': 120,
        'splitter': 'markdown',
        'retrieve_k': 20,
        'rerank_enabled': True,
        'rerank_top_n': 3,
        'score_threshold': 0.5,
        'max_rewrites': 2,
        'system_prompt': LECTURE_QA_PROMPT,
        'is_active': True,
    },
    {
        'id': POLICY_LOOKUP_ID,
        'slug': 'policy-lookup',
        'name': 'Policy Lookup',
        'description': (
            'Small chunks and strict quoting, for clause-structured policy '
            'documents where the answer is one specific passage rather than a '
            'span of discussion.'
        ),
        'chunk_size': 400,
        'chunk_overlap': 60,
        'splitter': 'markdown',
        'retrieve_k': 20,
        'rerank_enabled': True,
        'rerank_top_n': 3,
        'score_threshold': 0.5,
        'max_rewrites': 2,
        'system_prompt': POLICY_LOOKUP_PROMPT,
        'is_active': True,
    },
    {
        'id': FROM_SCRATCH_ID,
        'slug': 'from-scratch',
        'name': 'From scratch',
        'description': (
            'Model defaults and a minimal grounding prompt. The starting point '
            'when you want to tune every parameter yourself.'
        ),
        'chunk_size': 800,
        'chunk_overlap': 120,
        'splitter': 'markdown',
        'retrieve_k': 20,
        'rerank_enabled': True,
        'rerank_top_n': 3,
        'score_threshold': 0.5,
        'max_rewrites': 2,
        'system_prompt': FROM_SCRATCH_PROMPT,
        'is_active': True,
    },
]

SEEDED_SLUGS = [row['slug'] for row in TEMPLATE_ROWS]


def upgrade() -> None:
    # DO NOTHING, not DO UPDATE. An operator who has hand-tuned a prompt or a
    # chunk size in production must not have it silently reset by a redeploy --
    # and on Render `alembic upgrade head` runs in the START command, so this
    # executes on every single boot, not once. Re-running is a no-op.
    #
    # The arbiter is the `slug` unique index. On a re-run the conflict is
    # detected there and the row is skipped before the primary key is ever
    # tested, so the fixed ids above cannot raise a duplicate-key error.
    statement = postgresql.insert(agent_templates).on_conflict_do_nothing(
        index_elements=['slug'],
    )
    op.get_bind().execute(statement, TEMPLATE_ROWS)


def downgrade() -> None:
    # Exactly these three slugs. A blanket DELETE would take out any template an
    # operator added by hand, which this revision never created and has no
    # business removing.
    #
    # `agents.template_id` is ON DELETE SET NULL, so agents built from these
    # survive with a null template -- which is correct, because their parameters
    # were copied onto the agent at creation and never read back through the
    # template (PRD 4.2).
    op.execute(
        agent_templates.delete().where(agent_templates.c.slug.in_(SEEDED_SLUGS))
    )
