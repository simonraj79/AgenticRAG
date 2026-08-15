"""Seed data for `agent_templates` -- PRD sections 4.2 and 10.

Templates are the "premade" starting points. Creating an agent from one copies
these values onto the agent row, so "create from a template" and "create your
own" are the same code path (PRD 4.2) and editing a template never re-tunes an
agent somebody already built.

Data only -- no SQLAlchemy, no models. Whatever seeds the database (the
migration beside this file today, an admin re-seed command later) supplies its
own persistence. Keeping it inert means this module can be imported to *read*
the defaults without dragging in an engine.

The system prompts are the load-bearing safety control here, not
`score_threshold`. Measured on one corpus file, on-topic questions scored
0.61-0.67 and off-topic ones 0.49-0.58: an off-topic question about a refund
policy scored 0.5765, comfortably above the 0.5 threshold, and was refused
anyway because the prompt forbade answering outside the context. The threshold
governs *rewriting*; the prompt governs *refusing*. Every prompt below must
therefore carry its own grounding and refusal rules, because nothing downstream
will supply them. See CLAUDE.md, "Retrieval calibration".
"""

from __future__ import annotations

from typing import Any

# --------------------------------------------------------------------------
# System prompts
# --------------------------------------------------------------------------

# Transcripts are conversational: the lecturer states a thing, qualifies it two
# turns later, and the useful answer is the pair. Hence "quote the wording where
# the phrasing matters" rather than always-summarise.
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

# Policy answers get acted on, and the failure mode is specific: a model that
# has read a hundred other institutions' policies will reconstruct a plausible
# clause that this corpus does not contain. Reasoning by analogy from a
# neighbouring clause is banned explicitly for the same reason.
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

# Minimal, but not empty. Leaving this blank would hand the user a blank box in
# the editor and let the grounding rules go missing without anyone noticing --
# and refusal comes from the prompt.
FROM_SCRATCH_PROMPT = """\
Answer the question using only the CONTEXT below.

- Do not use prior knowledge.
- If the CONTEXT does not contain the answer, say so and stop.
- Cite the source filename for each claim."""


# --------------------------------------------------------------------------
# Templates
# --------------------------------------------------------------------------

# Every non-nullable column is spelled out. The ORM's `default=` values are
# Python-side only and do not exist in the database, so a Core insert that omits
# one hits a NOT NULL violation rather than quietly picking up the default.
AGENT_TEMPLATES: list[dict[str, Any]] = [
    {
        "slug": "lecture-qa",
        "name": "Lecture Q&A",
        "description": (
            "The PRD default. Large markdown-aware chunks sized for lecture "
            "transcripts, where a single answer is spread over several turns of "
            "dialogue."
        ),
        # PRD 10, "Chunking default": 800 tokens / 120 overlap, markdown-aware.
        # 800 holds a multi-turn exchange together while using 10% of
        # gemini-embedding-2's 8,192-token ceiling, so nothing truncates; 120 is
        # the standard 15% insurance against an answer straddling a boundary;
        # markdown separators keep a slide heading attached to its body.
        "chunk_size": 800,
        "chunk_overlap": 120,
        "splitter": "markdown",
        # PRD 10: "Retrieval parameters follow the workshop unchanged: top-20 ->
        # rerank -> top-3, rewrite below 0.5, max 2 rewrites."
        "retrieve_k": 20,
        "rerank_enabled": True,
        "rerank_top_n": 3,
        "score_threshold": 0.5,
        "max_rewrites": 2,
        "system_prompt": LECTURE_QA_PROMPT,
        "is_active": True,
    },
    {
        "slug": "policy-lookup",
        "name": "Policy Lookup",
        "description": (
            "Small chunks and strict quoting, for clause-structured policy "
            "documents where the answer is one specific passage rather than a "
            "span of discussion."
        ),
        # Halved from the lecture default, and deliberately: a policy answer is
        # one clause, so an 800-token chunk pulls in three neighbouring clauses
        # that the reranker then has to discount. 400 keeps a clause roughly
        # whole. Overlap stays at the same 15% ratio (60), which is enough to
        # catch a clause that runs over a boundary without duplicating whole
        # clauses across chunks and crowding the top-k with near-copies.
        "chunk_size": 400,
        "chunk_overlap": 60,
        "splitter": "markdown",
        # Unchanged from the default: precision matters more than recall here,
        # and that is exactly what the k=20 -> rerank -> top-3 funnel buys. A
        # smaller k would only starve the reranker of candidates.
        "retrieve_k": 20,
        "rerank_enabled": True,
        "rerank_top_n": 3,
        # Left at the PRD value rather than raised. The 0.5 threshold sits
        # inside the score noise band, so a higher number here would be an
        # invented figure dressed up as a safety control. Strictness lives in
        # the prompt, which is where it was measured to work. Stage 3 exists to
        # turn 0.5 into a measured number.
        "score_threshold": 0.5,
        "max_rewrites": 2,
        "system_prompt": POLICY_LOOKUP_PROMPT,
        "is_active": True,
    },
    {
        "slug": "from-scratch",
        "name": "From scratch",
        "description": (
            "Model defaults and a minimal grounding prompt. The starting point "
            "when you want to tune every parameter yourself."
        ),
        # Every value is the model default from app/db/models.py. That is the
        # point: "create your own" is not a separate branch in the code, it is
        # this template (PRD 4.2). If a default changes in the model, it changes
        # here too.
        "chunk_size": 800,
        "chunk_overlap": 120,
        "splitter": "markdown",
        "retrieve_k": 20,
        "rerank_enabled": True,
        "rerank_top_n": 3,
        "score_threshold": 0.5,
        "max_rewrites": 2,
        "system_prompt": FROM_SCRATCH_PROMPT,
        "is_active": True,
    },
]

# The slugs this module owns. Anything seeding or un-seeding should scope itself
# to these and leave hand-created templates alone.
TEMPLATE_SLUGS: list[str] = [template["slug"] for template in AGENT_TEMPLATES]
