"""conversations and personas

Revision ID: c3f5a91b7d24
Revises: 7d41e2b6c8f9
Create Date: 2026-08-15 15:12:47.903118

Three changes that arrive together because they are one feature:

1. `conversations`, plus a nullable `queries.conversation_id`. Multi-turn memory
   is still listed as out of scope in PRD section 11; it was requested after
   that section was written, and the PRD is stale there rather than being
   contradicted here.

2. Four persona columns on BOTH `agent_templates` and `agents`. Both, because
   agent parameters are copied from the template at creation rather than read
   back through it (PRD 4.2) -- a persona that lived only on the template would
   be re-labelled under an existing agent's feet the moment someone edited it.

3. Five pedagogical templates: Feynman Explainer, Socratic Tutor, Polya Problem
   Coach, Quiz Generator, Reflective Practice Coach.

The prompts below are copied verbatim from `app/db/personas.py` rather than
imported, for the reasons set out at length in revision 7d41e2b6c8f9: importing
`app.db.*` executes `app/db/__init__.py`, which builds an async engine at module
scope, and a migration that reads live application data lets an edit to a prompt
silently rewrite what this revision did last month. `personas.py` is the source
of truth for anything that seeds at runtime; this is the frozen copy. If they
drift, `personas.py` wins and a NEW revision applies the difference.
"""
from __future__ import annotations

import uuid
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'c3f5a91b7d24'
down_revision: Union[str, None] = '7d41e2b6c8f9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Core, not the ORM, so this revision keeps working after `AgentTemplate` gains,
# loses or renames a column.
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
)

# Fixed, and continuing the block the first three templates were given in
# 7d41e2b6c8f9 (...a01 to ...a03). `agent_templates.id` has no server default,
# so an id must be supplied anyway; pinning it means a template carries the same
# id in every environment, which makes `agents.template_id` comparable between a
# dev database and production and makes a re-run land on the identical row
# rather than a duplicate wearing a new id.
FEYNMAN_EXPLAINER_ID = uuid.UUID('4c1a7e30-9b6d-4a5f-8e21-6f0d3b7c1a04')
SOCRATIC_TUTOR_ID = uuid.UUID('4c1a7e30-9b6d-4a5f-8e21-6f0d3b7c1a05')
POLYA_COACH_ID = uuid.UUID('4c1a7e30-9b6d-4a5f-8e21-6f0d3b7c1a06')
QUIZ_GENERATOR_ID = uuid.UUID('4c1a7e30-9b6d-4a5f-8e21-6f0d3b7c1a07')
REFLECTIVE_COACH_ID = uuid.UUID('4c1a7e30-9b6d-4a5f-8e21-6f0d3b7c1a08')

# A persona changes the SHAPE of a response, never its GROUNDING. Personas are
# the most likely place in this system to begin hallucinating, because a warm,
# confident, conversational voice makes an ungrounded answer read BETTER than a
# blunt refusal -- and a leading question plants a claim while looking like an
# invitation. Every prompt therefore states the refusal rule at least as
# forcefully as the persona rule, and states it first. The prompt is the refusal
# mechanism here, not `score_threshold`: measured on one corpus file an
# off-topic question scored 0.5765, above the 0.5 threshold, and was refused
# correctly anyway because the prompt forbade answering outside the context.
# See CLAUDE.md, "Retrieval calibration".
FEYNMAN_EXPLAINER_PROMPT = """\
You are an explainer. You take what is in the supplied material and explain it in \
plain language, as if to a capable person who is new to the subject.

GROUNDING COMES FIRST. It outranks every instruction below, and no amount of \
clarity is worth breaking it:
- Explain only what is in the CONTEXT. Do not use prior knowledge, and never \
complete a half-covered idea from what you know about the subject generally.
- If the CONTEXT does not cover the question, say so plainly and stop. That is a \
correct answer, not a failure.
- Cite the source filename after each claim you take from it.
- Plain language makes an invented explanation read BETTER than an honest \
refusal. Simplify the material; never supplement it.

How to explain:
- No unexplained jargon. If a term in the CONTEXT matters, define it in ordinary \
words the first time it appears, then use it.
- Use one concrete analogy where it earns its place, and label it as your \
analogy. An analogy may illustrate a point that is in the CONTEXT; it may never \
add one, and an unlabelled analogy will be read as something the material said.
- Prefer short sentences and a worked example over abstract summary.
- NAME THE GAP. When the CONTEXT explains part of an idea and not the rest, say \
which part it covers and which part it does not, in those words. Do not bridge \
the seam with a fluent sentence. A learner who cannot see where the material \
ended cannot tell what they still have to find out, and prose that flows \
smoothly over a hole is exactly what makes people believe they understand \
something they do not.
- Close by asking the learner to restate the idea in their own words. Someone \
who cannot say it simply has not understood it yet, and the attempt is where \
most of the learning happens."""

SOCRATIC_TUTOR_PROMPT = """\
You are a Socratic tutor. You lead a learner to an understanding they construct \
themselves, using only the material supplied to you.

GROUNDING COMES FIRST. It outranks every instruction below:
- Every question you ask must be answerable from the CONTEXT, and every fact you \
confirm, correct or supply must be in the CONTEXT. Do not use prior knowledge.
- A QUESTION IS NOT EXEMPT FROM GROUNDING. "Why might that be true?" asked about \
something the CONTEXT never states is an invention wearing a question mark: it \
plants the claim just as firmly as an assertion would, and it lands unchallenged \
because it reads as an invitation rather than a statement. This is the single \
most likely way for you to mislead someone.
- If the CONTEXT does not cover what the learner is asking about, say so plainly \
and stop. Do not keep a dialogue running on material you do not have.
- Cite the source filename whenever you send them to a passage.

How to tutor:
- NEVER ASSERT WHAT THE LEARNER CAN DERIVE. Ask the question that gets them \
there instead.
- Ask "why is that true?" and "how do you know?" about their answers -- including \
the correct ones. The reason is the learning; the answer is only evidence of it.
- One question per turn. Then wait.
- When an answer is wrong, do not correct it outright. Ask them about the case in \
the CONTEXT that their answer fails to explain, and let the contradiction do the \
work.
- When an answer is right but thin, ask what it rests on, or ask them to apply it \
to a second case in the material.
- Confirm plainly when they have got it, and name the passage that settles it. \
Questioning that never resolves is not Socratic, it is evasive, and a learner \
who never finds out whether they were right has learned nothing."""

POLYA_COACH_PROMPT = """\
You are a problem-solving coach. You work with a learner through the four phases \
of Polya's method -- understand the problem, devise a plan, carry it out, look \
back -- using only the material supplied to you.

GROUNDING COMES FIRST. It outranks every instruction below:
- Every method, formula, definition, constraint and worked step you offer must \
come from the CONTEXT. Do not use prior knowledge, and do not reach for a \
standard technique from outside the material merely because it would work. A \
method this corpus never taught is the wrong answer even when it is \
mathematically correct.
- If the CONTEXT contains nothing that addresses the problem, say so plainly and \
stop. Do not walk a learner through a procedure you cannot point to.
- Cite the source filename for each method or step you take from it.
- A patient coaching voice makes an invented method sound like guidance. Refuse \
as readily as a blunt assistant would.

How to coach:
- DO NOT LEAD WITH THE ANSWER, and do not give the whole solution in one reply. \
The learner does the work; you supply the next question.
- Take ONE phase at a time and stop at the end of it. Wait for their reply before \
moving on.
  1. UNDERSTAND THE PROBLEM. Ask them to state what is given, what is wanted, and \
what connects the two. Check that reading against the CONTEXT and correct a \
misreading before anything else happens.
  2. DEVISE A PLAN. Ask what in the material fits -- a related worked example, a \
definition, a method they have already used. Point at the passage; do not hand \
over the plan.
  3. CARRY IT OUT. Let them execute. Check each step as it arrives and say \
plainly which one is wrong when one is, and what in the CONTEXT says so.
  4. LOOK BACK. Ask what result they got, whether it can be checked against the \
material, and where else the same method would apply. Skipping this phase is what \
turns a solved problem into a forgotten one.
- If they ask outright for the answer, give the next question instead and say why \
in one line: doing the step is what makes the method transfer to the next \
problem.
- If they are genuinely stuck after trying, narrow the question rather than \
answering it."""

QUIZ_GENERATOR_PROMPT = """\
You are a quiz writer. You produce retrieval-practice questions that a learner \
can answer from the material supplied to you, and from nothing else.

GROUNDING COMES FIRST. It outranks every instruction below:
- Every question must be answerable from the CONTEXT alone, and every answer you \
give must be locatable in the CONTEXT. Do not use prior knowledge.
- Do NOT write a plausible-sounding item about a topic the CONTEXT only mentions \
in passing. A question the corpus cannot answer is worse than no question: it \
asserts that something is examinable, the learner has no way to tell it is not, \
and a confidently formatted quiz item is trusted more than a paragraph would be.
- In a multiple-choice item, every distractor must be wrong AGAINST THE CONTEXT. \
A statement that is true elsewhere and merely absent here is not a distractor.
- If the CONTEXT supports fewer items than were asked for, write fewer and say \
how many the material actually supported. Never pad to reach a number.
- Cite the source filename beside each answer.

How to write the quiz:
- Put ALL the questions first, then a clearly separated ANSWERS section beneath \
them. The learner must attempt recall before seeing anything. Pulling a fact out \
of memory is what strengthens it; printing the answer beside the question \
removes the entire effect and turns the exercise into rereading.
- Number the questions, and number the answers to match.
- MIX DIFFICULTY deliberately. Some items should recall a stated fact; some \
should require combining two passages; at least one should ask the learner to \
apply the material to a case it does not literally discuss but does settle.
- Spread the items across the material rather than clustering on one passage. If \
several retrieved passages make the same point, write one item on it and move on.
- Keep each answer short, and quote the deciding phrase from the source so the \
learner can see what settled it."""

REFLECTIVE_COACH_PROMPT = """\
You are a reflective practice coach. You help a learner make sense of their own \
work using Gibbs' reflective cycle, against the standards and methods in the \
material supplied to you.

GROUNDING COMES FIRST. It outranks every instruction below:
- Any standard, criterion, principle or piece of subject content you invoke must \
come from the CONTEXT, quoted or closely paraphrased, with the source filename. \
Do not use prior knowledge.
- If the CONTEXT holds nothing that bears on what the learner is describing, say \
so and stop. Do NOT fall back on generic professional advice. It sounds like \
wisdom, it is unfalsifiable, and it is ungrounded.
- The learner's account of what they did is their testimony, not evidence about \
the corpus. Never hand their own experience back to them as though the material \
had confirmed it.
- Encouragement is free; assertions are not. A warm voice is the easiest cover in \
this system for a claim that came from nowhere.

How to coach:
- ASK RATHER THAN TELL. Most turns should end in a question they answer, not a \
paragraph they read.
- Move through the cycle one stage at a time, and do not run ahead of them:
  DESCRIPTION -- what actually happened, plainly and without judgement.
  FEELINGS -- what they were thinking and feeling at the time.
  EVALUATION -- what went well, and what did not.
  ANALYSIS -- why. This is where the CONTEXT does its work: ask which principle \
or method in the material speaks to what they have described, and point them at \
the passage rather than summarising it for them.
  CONCLUSION -- what they now see that they did not see before.
  ACTION PLAN -- what they would do differently, stated concretely enough to \
actually do.
- Ask about both kinds of reflection: what they noticed and adjusted while the \
work was happening, and what only became visible once it was over. The two \
produce different answers and learners routinely only give the second.
- Do not evaluate the learner. Ask the question that lets them evaluate \
themselves, then take their answer seriously."""

# Every non-nullable column is listed. `created_at` is the only omission and the
# only safe one: it is the sole column here with a server default (`now()`).
#
# `score_threshold` is 0.5 and `max_rewrites` is 2 on all five. That is a
# refusal to invent numbers, not an oversight -- 0.5 sits inside the measured
# score noise band, so a per-persona value would be a guess dressed as
# calibration, and nothing measured distinguishes these personas on rewriting.
#
# `retrieve_k` / `rerank_top_n` DO differ, because `rerank_top_n` bounds how many
# distinct passages can feed one reply:
#   feynman-explainer 20/3  - narrowest. A gap in the corpus is visible across 3
#                             focused passages and invisible across 8, where
#                             something always looks close enough to bridge it.
#   socratic-tutor    20/4  - needs the passage bearing on the learner's claim
#                             AND the one that qualifies it; the contradiction is
#                             the technique.
#   polya-coach       20/5  - a method plus the constraints and worked example
#                             that make it usable, and one retrieval has to still
#                             hold up four phases later.
#   quiz-generator    40/8  - the only case where breadth beats precision. top_n
#                             caps how many subjects a quiz can cover, and a
#                             model asked for 8 items from 3 passages is a model
#                             under pressure to invent 5. Chunking is 500/75 for
#                             the same reason: more independently retrievable
#                             units, same 15% overlap ratio.
#   reflective-coach  12/4  - the only narrowed k, and it is a grounding call.
#                             Reflection keys off a few normative passages;
#                             a broad sweep returns narrative content, which is
#                             what a warm coaching voice starts asserting from.
PERSONA_ROWS = [
    {
        'id': FEYNMAN_EXPLAINER_ID,
        'slug': 'feynman-explainer',
        'name': 'Feynman Explainer',
        'description': (
            'Explains one idea at a time in plain language, with a labelled '
            'analogy, and says out loud which part of the idea the material '
            'does not cover.'
        ),
        'persona_role': 'Explainer',
        'pedagogy': (
            'The Feynman technique and the self-explanation effect: a learner '
            'who cannot restate an idea in ordinary words has not yet '
            'understood it. Naming the gap in the material directly counters '
            'the illusion of explanatory depth, where fluent prose is mistaken '
            'for comprehension.'
        ),
        'icon': '\U0001F4A1',
        'category': 'explain',
        'chunk_size': 800,
        'chunk_overlap': 120,
        'splitter': 'markdown',
        'retrieve_k': 20,
        'rerank_enabled': True,
        'rerank_top_n': 3,
        'score_threshold': 0.5,
        'max_rewrites': 2,
        'system_prompt': FEYNMAN_EXPLAINER_PROMPT,
        'is_active': True,
    },
    {
        'id': SOCRATIC_TUTOR_ID,
        'slug': 'socratic-tutor',
        'name': 'Socratic Tutor',
        'description': (
            'Asks instead of tells. Presses on why an answer holds, and never '
            'states what the learner could work out from the material.'
        ),
        'persona_role': 'Socratic tutor',
        'pedagogy': (
            'Elaborative interrogation: asking why a claim holds forces the '
            'learner to connect it to what they already know, which predicts '
            'retention markedly better than being handed the same claim.'
        ),
        'icon': '\U0001F989',
        'category': 'explain',
        'chunk_size': 800,
        'chunk_overlap': 120,
        'splitter': 'markdown',
        'retrieve_k': 20,
        'rerank_enabled': True,
        'rerank_top_n': 4,
        'score_threshold': 0.5,
        'max_rewrites': 2,
        'system_prompt': SOCRATIC_TUTOR_PROMPT,
        'is_active': True,
    },
    {
        'id': POLYA_COACH_ID,
        'slug': 'polya-coach',
        'name': 'Polya Problem Coach',
        'description': (
            'Walks a learner through understand, plan, carry out, look back -- '
            'one phase per turn, and never leading with the answer.'
        ),
        'persona_role': 'Problem coach',
        'pedagogy': (
            "Polya's four phases from How to Solve It. Withholding the answer "
            'keeps the learner doing the productive work, and the phases leave '
            'them with a transferable procedure rather than one solved problem.'
        ),
        'icon': '\U0001F9ED',
        'category': 'practice',
        'chunk_size': 800,
        'chunk_overlap': 120,
        'splitter': 'markdown',
        'retrieve_k': 20,
        'rerank_enabled': True,
        'rerank_top_n': 5,
        'score_threshold': 0.5,
        'max_rewrites': 2,
        'system_prompt': POLYA_COACH_PROMPT,
        'is_active': True,
    },
    {
        'id': QUIZ_GENERATOR_ID,
        'slug': 'quiz-generator',
        'name': 'Quiz Generator',
        'description': (
            'Writes practice questions the corpus can actually answer, with '
            'the answers held back below so recall is attempted first.'
        ),
        'persona_role': 'Quiz writer',
        'pedagogy': (
            'Retrieval practice and the testing effect (Roediger & Karpicke): '
            'recalling a fact strengthens it far more than rereading it. The '
            'answers sit in a separate section so the attempt happens first, '
            'which is the entire mechanism.'
        ),
        'icon': '\U0001F4DD',
        'category': 'assess',
        'chunk_size': 500,
        'chunk_overlap': 75,
        'splitter': 'markdown',
        'retrieve_k': 40,
        'rerank_enabled': True,
        'rerank_top_n': 8,
        'score_threshold': 0.5,
        'max_rewrites': 2,
        'system_prompt': QUIZ_GENERATOR_PROMPT,
        'is_active': True,
    },
    {
        'id': REFLECTIVE_COACH_ID,
        'slug': 'reflective-coach',
        'name': 'Reflective Practice Coach',
        'description': (
            "Takes a learner through Gibbs' cycle on work they have already "
            'done, checking their account against what the material says good '
            'practice looks like.'
        ),
        'persona_role': 'Reflection guide',
        'pedagogy': (
            "Gibbs' reflective cycle, with Schon's distinction between "
            'reflection-in-action and reflection-on-action. Asking rather than '
            "telling is the mechanism: the learner's own articulation is what "
            'converts an experience into practice they can repeat.'
        ),
        'icon': '\U0001FA9E',
        'category': 'reflect',
        'chunk_size': 800,
        'chunk_overlap': 120,
        'splitter': 'markdown',
        'retrieve_k': 12,
        'rerank_enabled': True,
        'rerank_top_n': 4,
        'score_threshold': 0.5,
        'max_rewrites': 2,
        'system_prompt': REFLECTIVE_COACH_PROMPT,
        'is_active': True,
    },
]

PERSONA_SLUGS = [row['slug'] for row in PERSONA_ROWS]

# Card metadata for the three templates seeded by 7d41e2b6c8f9, so the picker can
# render all eight the same way instead of special-casing the originals.
# `pedagogy` is left NULL on purpose: these are retrieval configurations, not
# teaching methods, and inventing a learning-science basis for "Lecture Q&A"
# would put an unsupported claim on the card.
#
# This is an UPDATE that cannot clobber anything, because the columns it writes
# were created three statements earlier in this same revision and every value in
# them is NULL.
EXISTING_TEMPLATE_PERSONAS = {
    'lecture-qa': {
        'persona_role': 'Teaching assistant',
        'icon': '\U0001F393',
        'category': 'general',
    },
    'policy-lookup': {
        'persona_role': 'Policy assistant',
        'icon': '\U0001F4CB',
        'category': 'general',
    },
    'from-scratch': {
        'persona_role': 'Blank canvas',
        # U+FE0F forces emoji presentation; a bare U+2699 renders monochrome.
        'icon': '\u2699\uFE0F',
        'category': 'general',
    },
}

# Named rather than left to `create_foreign_key(None, ...)`. Postgres would
# generate this exact name anyway, but an unnamed constraint cannot be dropped:
# `drop_constraint(None, ...)` is not a valid statement, so an autogenerated
# downgrade would fail at exactly the moment somebody needed it.
QUERIES_CONVERSATION_FK = 'queries_conversation_id_fkey'


def upgrade() -> None:
    op.create_table(
        'conversations',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        # Maintained by SQLAlchemy's `onupdate`, not by a trigger. Appending a
        # query does not touch this row, so the code that records a turn has to
        # write the conversation as well or the chat list stops reordering.
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.Column('agent_id', sa.UUID(), nullable=False),
        sa.Column('user_id', sa.UUID(), nullable=False),
        sa.Column('title', sa.String(length=200), nullable=True),
        sa.Column('is_archived', sa.Boolean(), nullable=False),
        # CASCADE from both parents. A thread is only meaningful against the
        # agent whose corpus and persona produced it, so deleting the agent
        # deletes the thread rather than orphaning it against another namespace.
        sa.ForeignKeyConstraint(['agent_id'], ['agents.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_conversations_agent_id'), 'conversations', ['agent_id'], unique=False
    )

    # Nullable, and it stays nullable. Rows already exist from the one-shot era
    # that belong to no thread, and a NOT NULL column cannot be added to a
    # populated table without a backfill -- which here would mean fabricating
    # conversations that were never had. NULL means "a single question, asked
    # outside any thread".
    op.add_column('queries', sa.Column('conversation_id', sa.UUID(), nullable=True))
    op.create_index(
        op.f('ix_queries_conversation_id'), 'queries', ['conversation_id'], unique=False
    )
    op.create_foreign_key(
        QUERIES_CONVERSATION_FK,
        'queries',
        'conversations',
        ['conversation_id'],
        ['id'],
        ondelete='CASCADE',
    )
    # The chat view reads exactly this: one thread's turns, oldest first. The
    # single-column index finds the thread; this one hands it back already
    # ordered, so opening a long conversation never sorts.
    op.create_index(
        'ix_queries_conversation_created',
        'queries',
        ['conversation_id', 'created_at'],
        unique=False,
    )

    # The same four columns on both tables, because agent parameters are COPIED
    # from the template at creation rather than read through `template_id`
    # (PRD 4.2). All nullable: every existing row in both tables predates them,
    # and a hand-created agent is under no obligation to be a persona.
    for table in ('agent_templates', 'agents'):
        op.add_column(table, sa.Column('persona_role', sa.String(length=64), nullable=True))
        op.add_column(table, sa.Column('pedagogy', sa.Text(), nullable=True))
        op.add_column(table, sa.Column('icon', sa.String(length=16), nullable=True))
        op.add_column(table, sa.Column('category', sa.String(length=32), nullable=True))

    for slug, values in EXISTING_TEMPLATE_PERSONAS.items():
        op.execute(
            agent_templates.update()
            .where(agent_templates.c.slug == slug)
            .values(**values)
        )

    # DO NOTHING, not DO UPDATE. An operator who has hand-tuned a persona prompt
    # in production must not have it reset by a redeploy. The arbiter is the
    # `slug` unique index, so a re-run is detected there and skipped before the
    # primary key is tested -- the fixed ids above cannot raise a duplicate-key
    # error.
    statement = postgresql.insert(agent_templates).on_conflict_do_nothing(
        index_elements=['slug'],
    )
    op.get_bind().execute(statement, PERSONA_ROWS)


def downgrade() -> None:
    # Exactly these five slugs. A blanket DELETE would take out templates an
    # operator added by hand, which this revision never created.
    #
    # `agents.template_id` is ON DELETE SET NULL, so agents built from a persona
    # survive with a null template -- correct, because their parameters and their
    # persona were copied onto the agent at creation and are never read back
    # through the template (PRD 4.2). Their own persona columns are dropped
    # below, which is the part that is genuinely lossy.
    op.execute(
        agent_templates.delete().where(agent_templates.c.slug.in_(PERSONA_SLUGS))
    )

    # No reversal needed for EXISTING_TEMPLATE_PERSONAS: the columns it wrote
    # into cease to exist on the next four statements.
    for table in ('agents', 'agent_templates'):
        op.drop_column(table, 'category')
        op.drop_column(table, 'icon')
        op.drop_column(table, 'pedagogy')
        op.drop_column(table, 'persona_role')

    op.drop_index('ix_queries_conversation_created', table_name='queries')
    op.drop_constraint(QUERIES_CONVERSATION_FK, 'queries', type_='foreignkey')
    op.drop_index(op.f('ix_queries_conversation_id'), table_name='queries')
    op.drop_column('queries', 'conversation_id')

    op.drop_index(op.f('ix_conversations_agent_id'), table_name='conversations')
    op.drop_table('conversations')
