"""Stage 1: the chain.

PRD section 3.4 -- question -> embed -> top-k in the agent's namespace ->
prompt + context -> answer. Deliberately a straight line with no decisions in
it. Stage 2's loop (score check, bounded rewrite, rerank, trace) lands in this
module beside it, and consumes the same retriever object from
`app.rag.retriever`; the distinction the workshop is teaching is that Stage 1 is
a chain and Stage 2 is a loop, which only stays legible if Stage 1 stays a
chain.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from langchain_core.documents import Document
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_google_genai import ChatGoogleGenerativeAI

from app.config import settings
from app.db.models import Agent
from app.rag.retriever import META_FILENAME, build_retriever

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


@dataclass
class AnswerResult:
    """One Stage 1 answer, with what produced it."""

    question: str
    answer: str
    documents: list[Document] = field(default_factory=list)
    model: str = ""
    reranked: bool = False
    latency_ms: int = 0


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


def format_context(documents: list[Document]) -> str:
    """Render retrieved chunks for the prompt, tagged with their filename."""
    return "\n\n---\n\n".join(
        f"[{doc.metadata.get(META_FILENAME, 'unknown')}]\n{doc.page_content}"
        for doc in documents
    )


async def answer_question(
    agent: Agent, question: str, *, rerank: bool | None = None, **model_overrides
) -> AnswerResult:
    """Run one question through the Stage 1 chain."""
    started = time.perf_counter()

    retriever = build_retriever(agent, rerank=rerank)
    documents = await retriever.ainvoke(question)

    chain = (
        ChatPromptTemplate.from_messages(
            [
                ("system", agent.system_prompt or DEFAULT_SYSTEM_PROMPT),
                ("human", USER_TEMPLATE),
            ]
        )
        | get_chat_model(agent, **model_overrides)
        | StrOutputParser()
    )
    text = await chain.ainvoke(
        {"context": format_context(documents), "question": question}
    )

    return AnswerResult(
        question=question,
        answer=text,
        documents=documents,
        model=(agent.generation_model or settings.generation_model),
        reranked=agent.rerank_enabled if rerank is None else rerank,
        latency_ms=int((time.perf_counter() - started) * 1000),
    )
