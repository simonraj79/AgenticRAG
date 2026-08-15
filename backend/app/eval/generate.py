"""Suggest a golden set by reading the agent's own indexed corpus.

PRD section 3.6 assumes ten golden questions exist. Writing them by hand is the
step that does not happen: a workshop attendee who has just uploaded a corpus
will not stop and author ten questions plus reference answers before pressing
Evaluate, so the scorecard never gets run and Stage 3 never gets demonstrated.
This module produces a *draft* set the user then edits -- which is why every row
comes back tagged `source="ai_suggested"` and why `golden_questions.source`
distinguishes that from `edited`. A set the model wrote for itself and nobody
reviewed is a weaker test than the same set after a human corrected it, and the
provenance column exists so the two are not indistinguishable once they are rows.

**The corpus is read from Postgres, never from Pinecone.** `chunks.text` is the
source of truth (CLAUDE.md), and the two stores answer different questions: a
similarity search returns what is *near a query*, and there is no query here.
What is wanted is the corpus itself, enumerated in a stable order so that two
runs over an unchanged corpus see the same passages. Pinecone would also fold in
the retriever's own behaviour, which is the thing under test -- questions
sampled through the retriever would be biased towards material the retriever
already finds easily, and a golden set that cannot fail is not a test.

**This module does not write to the database.** It returns dicts. Persistence,
ownership checks and `order_index` collision with an existing set belong to the
API layer; keeping the two apart is what makes this callable from a script, from
a test, and from a route without three different transaction stories.

**One deliberate departure from house convention lives here.** This is the only
model call in the codebase not using `function_calling`, because Gemma cannot
fill this schema's array of objects through a tool call and fails at it
silently. The measurement is at `STRUCTURED_OUTPUT_METHOD` below; read it before
"fixing" the method back.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Literal

from langchain_core.messages import SystemMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Agent, Chunk, Document
from app.rag.llm import build_chat_model

log = logging.getLogger("uvicorn.error")


# --------------------------------------------------------------------------
# Limits
# --------------------------------------------------------------------------

# **The cap on how much corpus text reaches the model: 60,000 characters**,
# roughly 15,000 tokens at the usual four-characters-per-token rule of thumb.
#
# This is a budget we choose, not the model's ceiling. Two reasons to keep it
# well under whatever the ceiling is. First, it has to bind: the whole workshop
# document set is ~1.4 MB of text, so a realistically-sized agent corpus exceeds
# any sane prompt and sampling is not an optional refinement -- without a cap
# this call would simply fail on the first non-trivial agent. Second, CLAUDE.md
# measured generation at 89% of a turn's latency and token-bound; a prompt four
# times this size buys marginally better coverage for a wait the user is already
# watching a spinner through.
#
# Raising it is safe as far as correctness goes. The sampling below degrades
# smoothly: a larger budget just means more passages, still spread across
# documents in the same order.
MAX_CONTEXT_CHARS = 60_000

# Per-passage cap, above the largest chunk any template produces (the personas
# use 800 or 500 tokens, so ~3,200 chars at the top end). It exists for the
# pathological case -- a splitter that could not find a boundary in a wall of
# text -- so that one chunk cannot consume the whole budget and reduce the
# sample to a single document. In normal operation it never fires.
MAX_CHUNK_CHARS = 4_000

# Output headroom. A ten-question set is ten question/reference-answer pairs
# plus the topic inventory below plus tool-call JSON overhead; `generation_max_tokens`
# (2048) sizes a chat answer and is uncomfortably close. A truncated tool call
# fails loudly here -- malformed arguments do not validate -- but failing at all
# for want of a larger integer is a poor trade.
MAX_OUTPUT_TOKENS = 4_096

# How similar two questions may be before the second is dropped. Jaccard over
# content words, so 0.8 means "four of five meaningful words shared".
NEAR_DUPLICATE_JACCARD = 0.8

PASSAGE_SEPARATOR = "\n\n---\n\n"

# Removed before comparing questions for near-duplication. Without this, "what
# is the chunk size?" and "what is the overlap?" share three of four words and
# the second is discarded as a duplicate of the first -- the interrogative
# scaffolding is identical across almost every question in a golden set, so it
# has to be stripped or it dominates the similarity.
_STOPWORDS = frozenset(
    """
    a an and are as at be by can could describe do does explain for from
    give had has have how i in into is it its list may might must name of on
    or should so than that the their there these they this those to was were
    what when where which who whom why will with would you your
    """.split()
)


class GoldenSetSuggestionError(RuntimeError):
    """Suggestion could not be produced. Map to a 502-shaped response."""


class EmptyCorpusError(GoldenSetSuggestionError):
    """The agent has no indexed chunks to write questions about.

    Separate from the base class because the two want different HTTP statuses
    and different UI copy: this one is the user's turn (upload something, or
    wait for ingest to finish), whereas the base class is the service failing.
    Suggesting questions about an empty corpus is a bug, not an edge case -- the
    model would happily invent ten questions about nothing.
    """


# --------------------------------------------------------------------------
# The schema the model fills
# --------------------------------------------------------------------------

class SuggestedQuestion(BaseModel):
    """One drafted golden question."""

    question: str = Field(
        description=(
            "The question to ask the agent. One question, no preamble, phrased "
            "the way a learner would actually type it."
        )
    )
    reference_answer: str = Field(
        description=(
            "For an answerable question: the correct answer, taken from the "
            "passages, in one or two sentences. For a refusal question: one "
            "sentence naming what the passages do not say."
        )
    )
    expected_behaviour: Literal["answer", "refuse"] = Field(
        description=(
            "'answer' if the passages contain the answer; 'refuse' if they do "
            "not and the agent should say so."
        )
    )
    # NOT persisted -- `golden_questions` has no difficulty column and this key
    # is dropped before the dicts are returned. It is in the schema because
    # asking for the label is what makes the mix happen: a model that must
    # commit to "lookup" or "synthesis" per item plans a spread, where the same
    # model asked only for ten questions produces ten single-fact lookups, which
    # score high and teach nothing. Nothing here measures whether that holds; it
    # costs one enum field.
    difficulty: Literal["lookup", "synthesis"] = Field(
        description=(
            "'lookup' if one passage answers it outright; 'synthesis' if it "
            "requires combining two or more different passages."
        )
    )


# **The first two fields are a scratchpad, and their position is the point.**
# Gemini's structured output preserves the schema's property order, so these are
# written *before* the first question -- the refusal questions are then drawn
# from an inventory the model has already committed to on paper, rather than
# improvised alongside the answerable ones. Neither field is returned to the
# caller. It is a nudge rather than a mechanism, and it costs two lists of short
# strings; observed filled with 4-6 plausible entries on every trial run.
#
# **Keep the docstrings on these two classes short.** Pydantic puts `__doc__`
# into the schema as the description, and this schema is sent to the model as a
# function declaration -- a paragraph of implementation rationale here is a
# paragraph of noise in the prompt. Rationale goes in comments like this one.
class SuggestedGoldenSet(BaseModel):
    """A drafted golden set: what the material covers, where it stops, and the questions."""

    covered_topics: list[str] = Field(
        description=(
            "Before writing any questions: 4-8 short phrases naming what the "
            "passages actually cover."
        )
    )
    adjacent_gaps: list[str] = Field(
        description=(
            "Before writing any questions: 3-6 short phrases naming things a "
            "reader of these passages would reasonably expect to find here but "
            "which the passages do NOT state. These become the refusal "
            "questions."
        )
    )
    questions: list[SuggestedQuestion] = Field(
        description="The drafted golden set, answerable questions first."
    )


# --------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------

# A `SystemMessage`, not a `("system", ...)` tuple -- the tuple form would make
# this a TEMPLATE and every brace in it a required variable. There are no braces
# here today, and this way there never can be a reason to worry about one.
#
# The refusal section is the longest part of this prompt on purpose. Every other
# instruction here is one a competent model follows without being pushed; the
# plausible-neighbour requirement is the one it will quietly ignore, because
# "write a question this material cannot answer" is trivially satisfiable by
# changing the subject, and changing the subject produces a test that always
# passes.
SUGGEST_SYSTEM_PROMPT = """\
You write evaluation sets for a retrieval-augmented question answering system.

You will be shown passages sampled from one agent's document corpus. You write \
questions that will be asked of that agent, and the agent will be scored on how \
it answers them. You are writing a TEST, not a study guide.

ANSWERABLE QUESTIONS
- Each must be answerable from the passages shown BELOW AND NOTHING ELSE. Not \
from the subject in general, not from what you know, not from a part of the \
source document that was not shown to you. If you cannot point at the sentence \
that answers it, do not write it.
- Each needs a reference_answer taken from the passage text. This is not \
decoration: the scoring metric context_recall is computed against it, and a \
missing or invented reference answer silently disables a quarter of the \
scorecard while everything still renders a number.
- Ask about substance. "What does this document discuss?" is not a test.

REFUSAL QUESTIONS -- READ THIS TWICE. IT IS THE HARDEST PART AND THE MOST \
IMPORTANT.

A refusal question must be a PLAUSIBLE NEIGHBOUR of the material: something a \
reader of these exact passages would reasonably expect to find alongside them, \
in the same register, using the same vocabulary -- whose answer is genuinely \
absent from every passage shown.

An absurd off-topic question tests NOTHING. Given a corpus about a space \
station, every model on earth refuses "what is the capital of France?", so \
refusing it is not evidence that grounding holds. The question worth asking is \
"what is the crew's leave entitlement?" -- it sounds like it belongs, it uses \
the corpus's own vocabulary, and the only way to answer it is to invent an \
answer. THAT is the failure this test is hunting for.

Derive yours the same way from the passages below. Take a topic the material \
clearly cares about, then ask about a facet of it the passages never state: a \
number that is referred to but never given, a procedure mentioned but not \
described, a boundary case the text does not cover, a comparison it sets up but \
does not make.

Before you commit to each refusal question, re-read every passage and confirm \
the answer really is not there. A "refusal" question the corpus can actually \
answer is worse than no question at all: the agent will answer it correctly and \
be marked wrong for doing so.

VARY THE DIFFICULTY DELIBERATELY. A set of single-fact lookups scores high and \
teaches nothing.
- Some questions should be one stated fact from one passage.
- At least two should require combining information from two or more DIFFERENT \
passages. Those are the ones that catch a retriever that finds one good chunk \
and stops.
- Phrase some in the material's own vocabulary and some in a plain paraphrase a \
learner would actually type. A set written entirely in the corpus's wording \
measures word overlap rather than retrieval.
- Spread them across the passages. Where several passages make the same point, \
write one question on it and move on.

NEVER PAD TO REACH A NUMBER. If the material honestly supports fewer questions \
than you were asked for, write fewer. A weak question does not measure less \
than a good one, it measures the wrong thing, and it will be acted on."""

# Only this half is a template, and only the four names below are variables.
# Values substituted in are not re-parsed, so braces occurring inside the
# corpus text are safe.
SUGGEST_USER_TEMPLATE = """\
Write {answer_count} answerable questions and {refusal_count} refusal questions \
about the passages below.

The corpus is called "{agent_name}" and these passages come from: {filenames}

PASSAGES
{passages}"""


# --------------------------------------------------------------------------
# Reading the corpus
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class SampledChunk:
    """One passage, as it will be shown to the model."""

    filename: str
    chunk_index: int
    text: str


@dataclass
class _CorpusDocument:
    """One document's chunks, in `chunk_index` order."""

    filename: str
    chunks: list[SampledChunk] = field(default_factory=list)


async def _load_corpus(db: AsyncSession, agent: Agent) -> list[_CorpusDocument]:
    """Every chunk this agent owns, grouped by document, deterministically.

    `Document.agent_id == agent.id` is the scoping key and the tenancy boundary
    -- `chunks` has no agent column, so the join through `documents` is the only
    thing standing between one agent's golden set and another agent's corpus.

    The ORDER BY is doing real work rather than tidying: `created_at` alone ties
    for documents uploaded in the same batch, and a tie means Postgres may hand
    back a different order on the next call, which would make the sample below
    -- and therefore the questions -- silently non-reproducible across runs over
    an unchanged corpus. `Document.id` breaks the tie.
    """
    rows = (
        await db.execute(
            select(Document.id, Document.filename, Chunk.chunk_index, Chunk.text)
            .join(Chunk, Chunk.document_id == Document.id)
            .where(Document.agent_id == agent.id)
            .order_by(Document.created_at, Document.id, Chunk.chunk_index)
        )
    ).all()

    documents: list[_CorpusDocument] = []
    by_id: dict[uuid.UUID, _CorpusDocument] = {}
    for document_id, filename, chunk_index, text in rows:
        document = by_id.get(document_id)
        if document is None:
            document = _CorpusDocument(filename=filename)
            by_id[document_id] = document
            documents.append(document)
        document.chunks.append(
            SampledChunk(filename=filename, chunk_index=chunk_index, text=text)
        )
    return documents


async def _describe_empty_corpus(db: AsyncSession, agent: Agent) -> str:
    """Why there are no chunks, in words a user can act on.

    "No indexed content" is true for an agent with nothing uploaded and for an
    agent whose upload is three minutes into ingest, and those want opposite
    responses from the user -- upload something, versus wait. One cheap
    aggregate turns the error message into an instruction.
    """
    rows = (
        await db.execute(
            select(Document.status, func.count())
            .where(Document.agent_id == agent.id)
            .group_by(Document.status)
        )
    ).all()
    if not rows:
        return "it has no documents yet"
    counts = ", ".join(f"{count} {status}" for status, count in sorted(rows))
    return f"none of its documents are indexed ({counts})"


# --------------------------------------------------------------------------
# Sampling
# --------------------------------------------------------------------------

def _render_passage(chunk: SampledChunk) -> str:
    """One labelled passage.

    Labelled `[filename #index]`, matching `pipeline.format_context`, so the
    model sees the corpus in the same shape the agent under test will.

    The truncation marker is not cosmetic: without it the model reads a chunk
    that stops mid-sentence as a complete passage and can write a question about
    the fragment, whose reference answer is then a half-thought. Saying the text
    was cut lets it skip that passage instead.
    """
    text = chunk.text.strip()
    if len(text) > MAX_CHUNK_CHARS:
        text = text[:MAX_CHUNK_CHARS].rstrip() + "\n[passage truncated]"
    return f"[{chunk.filename} #{chunk.chunk_index}]\n{text}"


def _even_indices(size: int, quota: int) -> list[int]:
    """`quota` positions spread evenly over `size` items, ends included.

    The single-pick case takes the MIDDLE rather than the first. A document's
    first chunk is disproportionately likely to be a title, a table of contents
    or a preamble -- the one part of a file that supports no question worth
    asking.
    """
    if quota >= size:
        return list(range(size))
    if quota == 1:
        return [size // 2]
    return sorted({round(i * (size - 1) / (quota - 1)) for i in range(quota)})


def _round_robin(picks: list[list[SampledChunk]]):
    """One from each document, then the next from each, and so on."""
    for row in range(max((len(p) for p in picks), default=0)):
        for position, document_picks in enumerate(picks):
            if row < len(document_picks):
                yield position, document_picks[row]


def _sample_corpus(
    documents: list[_CorpusDocument], *, budget: int = MAX_CONTEXT_CHARS
) -> list[SampledChunk]:
    """Choose the passages to show, spread across documents and within them.

    **Why not just take the first N chunks.** A 40-chunk corpus truncated at the
    front produces ten questions about its first page, and a golden set that
    only interrogates the introduction reports a high score for an agent that
    cannot answer anything past it. Worse, that is the *stable* failure: it will
    look fine on every run.

    Three passes, each fixing a different way of clustering:

    1. **Round-robin quota across documents.** Every document gets one passage
       before any document gets two. A corpus of one 300-chunk transcript and
       four 5-chunk handouts is 94% transcript by chunk count, so a purely
       proportional sample tests the handouts with one passage between them or
       none at all. The floor is what this pass buys; once the small documents
       are exhausted the remainder still flows to the large one, so it is a
       guarantee for the small files rather than an equal split.
    2. **Even spacing within each document** (`_even_indices`), ends included.
       Round-robin alone still takes chunk 0 of everything, which is the same
       first-page problem one level down.
    3. **Round-robin append order, with the character budget checked per
       passage.** The budget is enforced while appending rather than afterwards
       so that running out costs the *tail* of each document's allocation
       evenly, instead of dropping the last documents entirely. The survivors
       are then sorted back into document-and-chunk order, because the prompt
       reads better as coherent runs of text than as an interleaved shuffle --
       the fair truncation is the reason for the interleaving, not the display.

    The capacity estimate uses the mean rendered passage length, so it is an
    estimate; step 3's per-passage check is what actually holds the line.
    """
    sizes = [len(document.chunks) for document in documents]
    total_chunks = sum(sizes)
    if not total_chunks:
        return []

    rendered_lengths = [
        len(_render_passage(chunk))
        for document in documents
        for chunk in document.chunks
    ]
    mean_cost = sum(rendered_lengths) / total_chunks + len(PASSAGE_SEPARATOR)
    capacity = max(1, int(budget // mean_cost))
    target = min(total_chunks, capacity)

    # Terminates: `target <= total_chunks == sum(sizes)`, and every iteration
    # that does not allocate moves to a different document.
    quotas = [0] * len(documents)
    allocated = 0
    position = 0
    while allocated < target:
        if quotas[position] < sizes[position]:
            quotas[position] += 1
            allocated += 1
        position = (position + 1) % len(documents)

    picks = [
        [document.chunks[i] for i in _even_indices(sizes[d], quotas[d])]
        for d, document in enumerate(documents)
    ]

    selected: list[tuple[int, int, SampledChunk]] = []
    used = 0
    for document_position, chunk in _round_robin(picks):
        cost = len(_render_passage(chunk)) + len(PASSAGE_SEPARATOR)
        # `selected and` -- one passage always survives, even a pathological one
        # larger than the whole budget. It cannot be, given MAX_CHUNK_CHARS, but
        # a sampler that can return nothing turns a small corpus into an
        # unexplained empty prompt.
        if selected and used + cost > budget:
            break
        selected.append((document_position, chunk.chunk_index, chunk))
        used += cost

    selected.sort(key=lambda item: (item[0], item[1]))
    return [chunk for _, _, chunk in selected]


# --------------------------------------------------------------------------
# The model call
# --------------------------------------------------------------------------

# **This call uses `json_schema`, and it is the ONE place in this codebase that
# does not use `function_calling`. That is a measurement, not a preference.**
#
# `settings.structured_output_method` is `function_calling` and stays that way:
# CLAUDE.md's trials, and `pipeline.get_contextualizer` which depends on them,
# are about a schema with a single required string. Gemma fills that reliably
# through a tool call. It cannot fill THIS one. Measured 2026-08-15 against
# gemma-4-31b-it at the model card's sampling, asking for 4 answerable + 2
# refusal questions over a three-passage corpus:
#
#   | schema shape                       | method           | outcome            |
#   |------------------------------------|------------------|--------------------|
#   | `list[str]` only                   | function_calling | fills correctly    |
#   | `list[QuestionObject]` only        | function_calling | NO TOOL CALL, 0 tok|
#   | full nested (this module's)        | function_calling | tool called with   |
#   |                                    |                  | every array EMPTY  |
#   | parallel `list[str]` per field     | function_calling | 0/3 usable         |
#   | full nested (this module's)        | json_schema      | 3/3, 13-15 s       |
#
# So an array of OBJECTS is the thing Gemma will not do through a tool call --
# scalars and arrays of strings are fine, which is exactly the shape CLAUDE.md
# measured. The failure is silent in the worst way: a well-formed tool call
# whose arrays are all `[]`, which parses cleanly into an empty golden set.
#
# The parallel-arrays workaround -- one `list[str]` per field, zipped back
# together -- keeps function_calling and was rejected on evidence rather than on
# taste. Three trials returned, in order: lists of length (2, 2, 6), i.e.
# questions silently paired with the wrong behaviour; an empty set; and a
# correct set after 155 seconds. A misaligned golden set does not fail, it
# scores, and it scores a question against another question's reference answer.
#
# **`json_schema` is not `json_mode`.** json_mode asks for free JSON and
# describes the schema in the prompt; `json_schema` binds a schema the provider
# constrains decoding against. It does return on the text channel, so CLAUDE.md's
# markdown-fence hazard applies in principle -- and the mitigation is the one
# CLAUDE.md already credits for json_mode scoring 5/5 where raw
# `response.parsed` scored 4/5: LangChain's parser strips the fence. The
# `None`-not-exception shape is still checked for at the call site.
#
# **What it binds changed with the gateway, and the trials above still stand.**
# Under `ChatGoogleGenerativeAI` this was Gemini's native `response_json_schema`;
# through OpenRouter it is OpenAI-shaped `response_format: {"type":
# "json_schema", ...}`, which providers advertise as `structured_outputs`.
# Re-verified live on `google/gemma-4-31b-it` via OpenRouter: a two-question
# nested set came back correctly populated in 2.5 s -- so the array-of-objects
# failure that ruled out `function_calling` here is still avoided, and this is
# the one call in the project that deliberately does NOT use function calling.
STRUCTURED_OUTPUT_METHOD = "json_schema"


@lru_cache(maxsize=1)
def _get_suggester() -> Runnable:
    """The suggestion chain, bound to the schema. One instance, shared.

    The structured-output method is `STRUCTURED_OUTPUT_METHOD` above, which is
    `json_schema` rather than the `function_calling` every other model call in
    this codebase uses. The measurement forcing that is recorded there in full;
    the short version is that Gemma will not fill an array of objects through a
    tool call, and fails at it silently.

    **The model is `golden_set_model`, not `decision_model`.** Those were the
    same setting until a head-to-head showed they should not be: this call writes
    the measuring instrument, the rewriter dereferences a pronoun, and the reasons
    to pick a model differ completely. See `golden_set_model` in app/config.py for
    the comparison and for the one cost it carries -- the drafting model is also
    the judge, so it grades against reference answers it wrote.

    Sampling stays at the Gemma 4 model card's standard configuration, and here
    that is not merely the default being respected -- ten questions drawn at low
    temperature from one corpus converge on ten phrasings of the same question,
    which is the exact failure `_is_near_duplicate` exists to catch downstream.
    `top_k` is passed unconditionally and `build_chat_model` drops it for model
    families that do not accept it; sending Gemma's card value to Gemini 3.7 Flash
    left no eligible provider and 404'd, which is how that guard was found.

    One call costs 6-12 s on a three-passage corpus (Flash 5.8 s, Gemma 11.8 s,
    measured). That is generation-bound, consistent with CLAUDE.md's finding that
    generation dominates every latency budget here, so it is a background job's
    worth of wait rather than something a form submit should block on.

    Cached and agent-independent: the prompt is a template and the agent's name,
    corpus and counts all arrive as invocation variables. Nothing agent-specific
    is baked in. The agent's own persona deliberately does not colour this call
    -- a Socratic tutor writing its own exam in character would produce a set
    that measures the persona rather than the retrieval.
    """
    prompt = ChatPromptTemplate.from_messages(
        [
            SystemMessage(content=SUGGEST_SYSTEM_PROMPT),
            ("human", SUGGEST_USER_TEMPLATE),
        ]
    )
    model = build_chat_model(
        settings.golden_set_model,
        temperature=settings.generation_temperature,
        top_p=settings.generation_top_p,
        top_k=settings.generation_top_k,
        max_tokens=MAX_OUTPUT_TOKENS,
    )
    # The model CLASS, not a schema dict. Handed a class, LangChain validates
    # the reply through it and yields a `SuggestedGoldenSet`; handed a dict it
    # yields an unvalidated dict and the enum fields stop being enforced. The
    # `json_schema` path resolves Pydantic's `$defs`/`$ref` itself, so the
    # nested `list[SuggestedQuestion]` needs no flattening here.
    return prompt | model.with_structured_output(
        SuggestedGoldenSet, method=STRUCTURED_OUTPUT_METHOD
    )


# --------------------------------------------------------------------------
# Cleaning up what came back
# --------------------------------------------------------------------------

def _normalise(question: str) -> str:
    """Lowercase, punctuation to spaces, whitespace collapsed."""
    return re.sub(r"[^a-z0-9]+", " ", question.lower()).strip()


def _content_tokens(question: str) -> frozenset[str]:
    return frozenset(_normalise(question).split()) - _STOPWORDS


def _is_near_duplicate(tokens: frozenset[str], seen: list[frozenset[str]]) -> bool:
    """Jaccard over content words against everything kept so far.

    Cheap and lexical, and that is a deliberate ceiling rather than a shortcut.
    "What is the chunk size?" and "how large is each chunk?" survive this, and
    they should: the human editing the draft is the real filter, and a
    suggestion tool that silently discards half its output is harder to trust
    than one that shows its work. What this catches is the model literally
    restating itself, which is the common failure when a small corpus is asked
    for ten questions.
    """
    for other in seen:
        union = tokens | other
        if union and len(tokens & other) / len(union) >= NEAR_DUPLICATE_JACCARD:
            return True
    return False


def _accept(
    suggestion: SuggestedQuestion, seen: list[frozenset[str]]
) -> tuple[str, str | None] | None:
    """Validate one suggestion. Returns `(question, reference_answer)` or None."""
    question = suggestion.question.strip()
    if not question:
        return None

    tokens = _content_tokens(question)
    # An all-stopword question ("what is it?") has no content words to compare,
    # so it can neither be a duplicate nor be worth keeping.
    if not tokens or _is_near_duplicate(tokens, seen):
        return None

    reference = suggestion.reference_answer.strip() or None
    if reference is None and suggestion.expected_behaviour == "answer":
        # Dropped rather than kept with a NULL. `context_recall` is computed
        # against the reference answer (CLAUDE.md), so an answerable row without
        # one contributes to three metrics and silently abstains from the
        # fourth -- and the scorecard still prints a mean, over a different
        # number of rows than the one beside it.
        return None

    seen.append(tokens)
    return question, reference


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

async def suggest_golden_questions(
    db: AsyncSession,
    agent: Agent,
    *,
    count: int = 10,
    refusal_count: int = 2,
) -> list[dict[str, Any]]:
    """Draft a golden set for `agent` from its own indexed chunks.

    Returns dicts carrying `question`, `reference_answer`, `expected_behaviour`,
    `order_index` and `source="ai_suggested"` -- the caller adds `agent_id` and
    persists. `order_index` starts at 0; a caller appending to an existing set
    must offset it, because this function deliberately does not read the rows it
    is about to sit beside.

    `count` is the total and `refusal_count` is how many of it are refusal
    probes -- 10 and 2 by default, so 8 answerable. Both are parameters because
    the right ratio is a judgement about the corpus, not a constant: a narrow
    policy corpus has a large plausible-neighbour surface and wants more
    refusals, a broad one wants fewer.

    **Fewer questions may come back than were asked for**, and that is correct
    behaviour rather than a partial failure. Near-duplicates are dropped, so are
    answerable questions with no reference answer, and the prompt tells the model
    not to pad a thin corpus. A short honest set beats ten items where two are
    filler -- filler does not measure less, it measures the wrong thing, and it
    is acted on all the same.

    Raises `EmptyCorpusError` when the agent has no chunks, and
    `GoldenSetSuggestionError` when the model returns nothing usable.
    """
    if count < 1:
        raise ValueError("count must be at least 1")
    if not 0 <= refusal_count <= count:
        raise ValueError("refusal_count must be between 0 and count")
    answer_count = count - refusal_count

    documents = await _load_corpus(db, agent)
    if not documents:
        raise EmptyCorpusError(
            f"Agent '{agent.name}' has no indexed chunks: "
            f"{await _describe_empty_corpus(db, agent)}. "
            "Upload and index a document before suggesting golden questions."
        )

    sampled = _sample_corpus(documents)
    filenames = sorted({document.filename for document in documents})
    log.info(
        "Golden-set suggestion for agent %s: %s passages sampled from %s "
        "documents (%s chunks total)",
        agent.id,
        len(sampled),
        len(documents),
        sum(len(document.chunks) for document in documents),
    )

    try:
        result = await _get_suggester().ainvoke(
            {
                "answer_count": answer_count,
                "refusal_count": refusal_count,
                "agent_name": agent.name,
                "filenames": ", ".join(filenames),
                "passages": PASSAGE_SEPARATOR.join(
                    _render_passage(chunk) for chunk in sampled
                ),
            }
        )
    except Exception as exc:  # noqa: BLE001 - re-raised, not swallowed
        # Deliberately broad and deliberately NOT swallowed -- the opposite of
        # `pipeline.contextualize_question`, which degrades to the raw question
        # because a worse search still answers the user. There is no degraded
        # golden set. Wrapped so the API layer has one exception type to map
        # instead of a quota error, a transport error and a Pydantic
        # `ValidationError` (a model answering "refusal" instead of "refuse")
        # arriving as three unrelated shapes.
        raise GoldenSetSuggestionError(f"The suggestion model failed: {exc}") from exc

    # **Structured output hands back None instead of raising.** CLAUDE.md
    # records that shape for Gemma's markdown fences. Checked even though the
    # parser should have raised instead, because the cost of being wrong is an
    # AttributeError on the next line that names none of the above.
    if result is None:
        raise GoldenSetSuggestionError(
            "The suggestion model returned nothing at all."
        )

    # An empty `questions` list is its own failure, and a distinct one: it is
    # what a schema the model cannot fill looks like from here -- a well-formed
    # reply with nothing in it. See STRUCTURED_OUTPUT_METHOD; this is the guard
    # that turns that into an error instead of an empty golden set.
    if not result.questions:
        raise GoldenSetSuggestionError(
            "The suggestion model returned a well-formed but empty set."
        )

    # Split, then cap each side independently. Capping the combined list would
    # let a model that wrote twelve answerable questions and two refusals return
    # a set with no refusal probes in it at all -- which is precisely the set
    # that always scores well and never catches ungrounded answering.
    answerable: list[dict[str, Any]] = []
    refusals: list[dict[str, Any]] = []
    seen: list[frozenset[str]] = []
    for suggestion in result.questions:
        accepted = _accept(suggestion, seen)
        if accepted is None:
            continue
        question, reference = accepted
        bucket = refusals if suggestion.expected_behaviour == "refuse" else answerable
        limit = refusal_count if suggestion.expected_behaviour == "refuse" else answer_count
        if len(bucket) >= limit:
            continue
        bucket.append(
            {
                "question": question,
                "reference_answer": reference,
                "expected_behaviour": suggestion.expected_behaviour,
                # `source` is not cosmetic. It is what lets the scorecard say
                # whether a human ever reviewed the questions behind it; the API
                # layer flips it to "edited" on the first edit.
                "source": "ai_suggested",
            }
        )

    # Answerable first, refusals grouped at the end. `order_index` is display
    # order in the editor, and the refusal probes are the rows most likely to be
    # wrong -- a "refusal" question the corpus can actually answer marks a
    # correct answer as a failure -- so they are presented as a block a reviewer
    # can check together rather than scattered through the set.
    questions = answerable + refusals
    for order_index, row in enumerate(questions):
        row["order_index"] = order_index

    if not questions:
        raise GoldenSetSuggestionError(
            "Every suggested question was rejected as empty or duplicated."
        )
    if len(answerable) < answer_count or len(refusals) < refusal_count:
        log.warning(
            "Golden-set suggestion for agent %s returned %s/%s answerable and "
            "%s/%s refusal questions after de-duplication",
            agent.id,
            len(answerable),
            answer_count,
            len(refusals),
            refusal_count,
        )
    return questions
