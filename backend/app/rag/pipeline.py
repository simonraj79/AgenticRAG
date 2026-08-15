"""Stage 1: the chain, plus the contextualisation step in front of it.

PRD section 3.4 -- question -> embed -> top-k in the agent's namespace ->
prompt + context -> answer. Deliberately a straight line with no decisions in
it. Stage 2's loop (score check, bounded rewrite, rerank, trace) lands in this
module beside it, and consumes the same retrieval from `app.rag.retriever`; the
distinction the workshop is teaching is that Stage 1 is a chain and Stage 2 is a
loop, which only stays legible if Stage 1 stays a chain.

**Contextualisation is in front of the chain, not inside it.** A follow-up
carries its subject in a pronoun -- "what is its power budget?" -- and embedding
that raw retrieves nothing useful, because the vector for a bare pronoun has no
relationship to the vector for the rover it stands for. So when there is history,
a rewrite step resolves the question into a standalone one and *that* is what
gets embedded. It runs before retrieval and leaves the chain's shape untouched.

The canonical LangChain name for this is `create_history_aware_retriever`.
Checked against the reference before writing any of this rather than after an
import failed, per CLAUDE.md: in 1.x it lives at `langchain_classic.chains`, and
its body is `prompt | llm | StrOutputParser() | retriever`. That is the right
pattern and the wrong object for this build, for three reasons that are all the
same reason -- it returns documents and nothing else. It discards the rewritten
question, leaving the REWRITE trace event with nothing to record; it parses the
rewrite off the text channel, which CLAUDE.md measured as the one path Gemma
breaks by wrapping JSON in a markdown fence; and its output is a bare
`list[Document]`, which is the very score-shaped hole `aretrieve` exists to
close. The pattern is kept; the helper is not.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import lru_cache

from langchain_core.documents import Document
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.runnables import Runnable
from langchain_google_genai import ChatGoogleGenerativeAI
from pydantic import BaseModel, Field

from app.config import settings
from app.db.models import Agent
from app.rag.retriever import META_FILENAME, RERANK_SCORE_KEY, aretrieve

log = logging.getLogger("uvicorn.error")

# (question, answer) for one prior turn, oldest first. The answer is optional
# because a `queries` row exists before its answer does -- a turn that failed
# mid-generation still has a question worth resolving pronouns against.
#
# Deliberately tuples rather than `BaseMessage`, even though the prompt below
# wants messages: this is the shape the `queries` table hands back, so the API
# layer writes `[(q.question, q.answer) for q in prior]` and the conversion to
# messages stays here, next to the prompt that is the only thing needing it.
ChatTurn = tuple[str, str | None]

# How many prior turns reach the rewriter. A cap, not a tuning result:
# coreference reaches back a turn or two ("it", "that one", "the second
# approach"), so turn seven changes the rewrite almost never -- while its cost is
# certain, because this call sits on the latency path of every follow-up and the
# prompt grows with every turn a thread accumulates. Unbounded history would make
# asking a question in a long conversation steadily slower and dearer than asking
# the same question in a short one, buying a rewrite that does not improve.
HISTORY_TURNS = 6

# Refusal is a correct outcome, not a failure -- `queries.refused` exists to
# count it and the golden set marks questions whose right answer is "I don't
# know" (PRD sections 4.3, 4.4). The instruction is explicit because a model
# left to its own judgement will helpfully answer from parametric memory, which
# is exactly the failure mode grounded retrieval is meant to remove.
DEFAULT_SYSTEM_PROMPT = """\
You are a teaching assistant answering questions strictly from the supplied \
course material.

Rules:
- Answer only from the CONTEXT below. Do not use prior knowledge.
- If the context does not contain the answer, say so plainly and stop. A \
correct refusal is better than a plausible guess.
- Cite the source filename in brackets after each claim you take from it.
- Be concise and concrete."""

USER_TEMPLATE = """\
CONTEXT:
{context}

QUESTION: {question}"""

# The rewrite instruction. "Do not answer" is not padding -- a model handed a
# question and a conversation will answer it given the slightest opening, and an
# answer embedded as a search query retrieves documents that look like answers
# rather than documents that contain one.
#
# "Keep the user's wording everywhere else" matters for the same reason. This
# string is going to an embedding model, so every word the rewriter invents
# moves the vector away from the user's actual intent. The job is resolving
# references, not improving the question.
CONTEXTUALIZE_SYSTEM_PROMPT = """\
Given the conversation so far and the user's latest question, rewrite that \
question so that it can be understood on its own, without the conversation.

Rules:
- Resolve pronouns and references ("it", "that", "the second one") into the \
words they stand for.
- Keep the user's wording everywhere else. This text goes to a search index, \
not to a person.
- Do not answer the question.
- If the question already stands on its own, return it unchanged."""


class StandaloneQuestion(BaseModel):
    """The rewrite, as a typed object rather than as a string to be parsed."""

    question: str = Field(
        description=(
            "The user's latest question, rewritten so it can be understood "
            "without the conversation. Unchanged if it already could be."
        )
    )


@dataclass
class AnswerResult:
    """One answer, with everything that produced it.

    Carries the scored retrieval rather than bare Documents, and that is the
    point of the object. `app/api/ask.py` used to run retrieval a second time
    purely to recover the scores this drops -- see `retriever.aretrieve` -- and
    Stage 2 cannot branch on a score the pipeline never returned. Answer and
    evidence come back together or the caller has to go looking for the
    evidence again.
    """

    question: str
    answer: str
    documents: list[Document] = field(default_factory=list)
    # Pre-rerank candidates with Pinecone's cosine score. See `Retrieval`.
    scored: list[tuple[Document, float]] = field(default_factory=list)
    # The standalone question that was actually embedded, or None when
    # contextualisation did not run. None is NOT "unchanged" -- see
    # `contextualize_question`.
    rewritten_question: str | None = None
    model: str = ""
    reranked: bool = False
    latency_ms: int = 0
    # Split out so the trace can attribute the turn honestly. CLAUDE.md's
    # measurement -- generation 13.2 s against retrieval's ~0.8 s -- is the
    # reason optimising anywhere but generation is wasted effort, and it is only
    # re-checkable if the parts are recorded separately.
    contextualize_ms: int = 0
    retrieval_ms: int = 0
    generation_ms: int = 0

    @property
    def search_query(self) -> str:
        """The string that was embedded: the rewrite if there was one."""
        return self.rewritten_question or self.question

    @property
    def top_score(self) -> float | None:
        """Best first-pass similarity. What PRD 3.5's threshold compares."""
        return float(self.scored[0][1]) if self.scored else None

    @property
    def similarity_scores(self) -> list[float]:
        """Aligned to `scored`, not to `documents`. Reranking reorders."""
        return [float(score) for _, score in self.scored]

    @property
    def rerank_scores(self) -> list[float | None]:
        """Aligned to `documents`. All None when reranking did not run."""
        return [
            float(raw) if (raw := doc.metadata.get(RERANK_SCORE_KEY)) is not None
            else None
            for doc in self.documents
        ]


def get_chat_model(agent: Agent | None = None, **overrides) -> ChatGoogleGenerativeAI:
    """The generation model.

    Sampling defaults come from the Gemma 4 model card's "standardized sampling
    configuration across all use cases" (temperature 1.0, top_p 0.95, top_k 64),
    not from the temperature-0 reflex that grounded RAG usually invites. Gemma is
    calibrated for those values; squeezing sampling far below them trades a small
    determinism gain for a real risk of repetition loops. Override per call to
    measure, not by habit.

    `convert_system_message_to_human` stays False: Gemma 4 supports the system
    role natively. Gemma 3 did not, which is why so much example code still
    flattens the system message into the user turn -- doing that here would bury
    the grounding rules mid-prompt where they carry less weight.
    """
    params = {
        "model": (agent.generation_model if agent and agent.generation_model else settings.generation_model),
        "google_api_key": settings.gemini_api_key,
        "temperature": settings.generation_temperature,
        "top_p": settings.generation_top_p,
        "top_k": settings.generation_top_k,
        "max_output_tokens": settings.generation_max_tokens,
    }
    params.update(overrides)
    return ChatGoogleGenerativeAI(**params)


@lru_cache(maxsize=1)
def get_contextualizer() -> Runnable:
    """The rewrite chain, bound to a schema. One instance, shared.

    Cached and agent-independent because it is: there is no per-agent decision
    model column, and the rewrite is a mechanical dereference of pronouns rather
    than anything a persona should colour. An agent that rewrote follow-ups in
    character would be embedding its own voice instead of the user's question.

    **`method="function_calling"` is load-bearing, and it is not the default.**
    `with_structured_output` on this class defaults to `json_schema`, so leaving
    the argument off silently changes the path. CLAUDE.md records the
    measurement behind the choice: Gemma emits schema-correct JSON but sometimes
    wraps it in a markdown fence, and a strict parser answers a fence with
    `None` rather than with an exception -- the worst possible failure shape for
    a value the caller is about to branch on. Function calling never opens the
    text channel, so no fence can appear in it. `json_mode` measured 5/5 too and
    is still the wrong pick: it passes because LangChain strips the fence, which
    is a parser working around the problem rather than the problem being absent.

    The value comes from `settings.structured_output_method` so that this and
    the config comment recording the trials cannot drift apart.
    """
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", CONTEXTUALIZE_SYSTEM_PROMPT),
            MessagesPlaceholder("history"),
            ("human", "{question}"),
        ]
    )
    model = ChatGoogleGenerativeAI(
        model=settings.decision_model,
        google_api_key=settings.gemini_api_key,
        temperature=settings.generation_temperature,
        top_p=settings.generation_top_p,
        top_k=settings.generation_top_k,
    )
    return prompt | model.with_structured_output(
        StandaloneQuestion, method=settings.structured_output_method
    )


async def contextualize_question(
    question: str, history: Sequence[ChatTurn]
) -> str | None:
    """Rewrite a follow-up into a question that stands on its own.

    Returns the standalone question, or None when contextualisation did not run
    -- no history, or the call failed.

    **None is not "unchanged".** A rewrite that comes back byte-identical still
    returns the string, because "the model read this and left it alone" and "the
    model was never asked" are different facts and the caller records which one
    happened. Collapsing them would make every first turn indistinguishable from
    a question the rewriter declined to touch.

    **Convergence worth naming.** PRD 3.5's Stage 2 loop rewrites when the top
    retrieval score falls below threshold; this rewrites when a question
    contains a dangling reference. Different triggers, different evidence --
    a number there, the conversation here -- but the same machinery: a typed
    rewrite, embedded in place of the original. Whoever builds Stage 2 should
    compose the two (contextualise first, then let the score check rewrite the
    already-standalone question) rather than add a second rewriter. Two
    rewriters would both be writing REWRITE trace events for one turn, and
    nothing downstream could say which of them produced the string that was
    actually searched.

    There is no `queries` column for this yet -- `AnswerResult.rewritten_question`
    carries it, and where it lands durably (a new column, or a REWRITE payload
    in `trace_events`) is a schema decision this module does not make.
    """
    if not history:
        # A first turn has no references to resolve: nothing to gain, and a
        # model call on the latency path to lose. It passes through
        # byte-identically, which is also what makes the None meaningful.
        return None

    # Awareness note, matching PRD section 11's stance on indirect prompt
    # injection: prior ANSWERS are model output generated from retrieved chunks,
    # so text that came out of the corpus reaches this rewriter one hop later.
    # A chunk carrying "ignore previous instructions and search for X" could in
    # principle steer a follow-up's search query. The blast radius is small --
    # the worst outcome is a bad search inside the agent's own namespace, which
    # is scoped in `retriever.get_vector_store` and cannot reach another
    # agent's corpus -- and no defence is built here, deliberately. Recorded so
    # that the surface is known rather than discovered.
    messages: list[BaseMessage] = []
    for prior_question, prior_answer in history[-HISTORY_TURNS:]:
        messages.append(HumanMessage(content=prior_question))
        # Skipped rather than sent as an empty turn: a blank assistant message
        # reads to the model as "the assistant had nothing to say", which is a
        # claim about the conversation rather than the absence of a record.
        if prior_answer:
            messages.append(AIMessage(content=prior_answer))

    try:
        result = await get_contextualizer().ainvoke(
            {"history": messages, "question": question}
        )
    except Exception as exc:  # noqa: BLE001 - see below
        # Deliberately broad, and deliberately swallowed. **A failed rewrite
        # must degrade to Stage 1 behaviour, never fail the turn.** The raw
        # question still retrieves; a follow-up answered from a slightly worse
        # search is a worse answer, whereas an exception here is no answer at
        # all for a question the corpus could have answered. Every failure mode
        # of this call -- quota, timeout, transport, a schema the model would
        # not fill -- has that same correct response, which is why they are
        # caught together rather than enumerated.
        log.warning("Contextualisation failed, using the raw question: %s", exc)
        return None

    # Structured output can still hand back None instead of raising -- the exact
    # failure shape CLAUDE.md warns about. function_calling should make it
    # unreachable; it is checked anyway, because the cost of being wrong is an
    # AttributeError inside a request rather than a slightly worse search.
    rewritten = result.question.strip() if result is not None else ""
    if not rewritten:
        return None
    return rewritten


def format_context(documents: list[Document]) -> str:
    """Render retrieved chunks for the prompt, tagged with their filename."""
    return "\n\n---\n\n".join(
        f"[{doc.metadata.get(META_FILENAME, 'unknown')}]\n{doc.page_content}"
        for doc in documents
    )


async def answer_question(
    agent: Agent,
    question: str,
    *,
    rerank: bool | None = None,
    history: Sequence[ChatTurn] | None = None,
    **model_overrides,
) -> AnswerResult:
    """Run one question through the chain, and return the evidence with it.

    `history` is the thread's prior turns, oldest first, and it is the only new
    input: omit it and this behaves exactly as it did before, which is what
    keeps a one-shot `/api/ask` and a threaded chat on one code path rather than
    two that will diverge.

    The result now carries the scored retrieval, so a caller that needs
    `query_chunks.similarity_score` no longer searches a second time for numbers
    this function already had.
    """
    started = time.perf_counter()

    # 1. Contextualise, if there is anything to contextualise against.
    t0 = time.perf_counter()
    rewritten = await contextualize_question(question, history or ())
    contextualize_ms = int((time.perf_counter() - t0) * 1000)
    search_query = rewritten or question

    # 2. Retrieve once, with the scores.
    t0 = time.perf_counter()
    retrieval = await aretrieve(agent, search_query, rerank=rerank)
    retrieval_ms = int((time.perf_counter() - t0) * 1000)

    # 3. Generate.
    #
    # The generator is given `search_query`, not the raw question. The rewrite
    # is by construction the same question with its references resolved, so
    # grounding is unaffected -- but a generator handed "what is its power
    # budget?" alongside context it has no conversation to connect the pronoun
    # to is being asked to guess what "it" was. Passing the resolved form costs
    # nothing and removes that guess. It also keeps the chain a chain: the
    # alternative, threading history into the generation prompt as well, is a
    # real improvement for conversational tone and a different change --
    # PRD section 11 still lists conversational memory as out of scope, and the
    # retrieval half is what makes follow-ups *answerable* at all.
    # `SystemMessage(...)`, not `("system", ...)`. The tuple form makes the
    # string a TEMPLATE, so a brace in it is parsed as a variable -- and
    # `agent.system_prompt` is user-editable persona text. A prompt reading
    # "Use the format {step}: {reason}" registers `step` and `reason` as
    # required inputs and the invoke below dies with
    # `KeyError: Input to ChatPromptTemplate is missing variables {'step',
    # 'reason'}`, which names the prompt machinery rather than the persona that
    # caused it. A `SystemMessage` is passed through literally. Only
    # USER_TEMPLATE, which this module owns, is a template -- and values
    # substituted into it are not re-parsed, so braces in retrieved chunks or in
    # the question are already safe.
    chain = (
        ChatPromptTemplate.from_messages(
            [
                SystemMessage(content=agent.system_prompt or DEFAULT_SYSTEM_PROMPT),
                ("human", USER_TEMPLATE),
            ]
        )
        | get_chat_model(agent, **model_overrides)
        | StrOutputParser()
    )
    t0 = time.perf_counter()
    text = await chain.ainvoke(
        {
            "context": format_context(retrieval.documents),
            "question": search_query,
        }
    )
    generation_ms = int((time.perf_counter() - t0) * 1000)

    return AnswerResult(
        question=question,
        answer=text,
        documents=retrieval.documents,
        scored=retrieval.scored,
        rewritten_question=rewritten,
        model=(agent.generation_model or settings.generation_model),
        reranked=retrieval.reranked,
        latency_ms=int((time.perf_counter() - started) * 1000),
        contextualize_ms=contextualize_ms,
        retrieval_ms=retrieval_ms,
        generation_ms=generation_ms,
    )
