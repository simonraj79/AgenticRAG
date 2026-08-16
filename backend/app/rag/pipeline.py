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
relationship to the vector for the rover it stands for. So a rewrite step
resolves the question into a standalone one and *that* is what gets embedded. It
runs before retrieval and leaves the chain's shape untouched.

**As of 2026-08-16 it runs on EVERY turn, first ones included**
(`settings.rewrite_every_turn`). The step widened along with the trigger: it
still resolves references, and it now also repairs typos and shorthand, because
a first turn has no pronouns and can perfectly well have both of those. Per
`new features/loop.md` section 6 item 1 that makes it a code path rather than a
tool or a trigger -- something that must happen every time is something this
module calls itself.

**The step does NOT expand acronyms, and that is a removal rather than an
omission.** Expansion was built here, measured fabricating ("Ka-band
(Kurtz-band)" in 2 of 5 trials; "LS&T" -> "Link System and Telemetry", which
moved retrieval to the wrong file), narrowed to a conditional version that
measured 5/5, and removed anyway. The comment above
`CONTEXTUALIZE_SYSTEM_PROMPT` is where that measurement lives; the prompt now
carries a flat prohibition.

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

**The generation step now has two shapes, and the first one is unchanged.** With
tools off (`_tools_active` -> False) it is the chain above, invoked once, with
`format_context` producing exactly the string it always has -- that is a hard
requirement rather than a courtesy, because every number in EVAL.md was measured
against it. With tools on, the single `chain.ainvoke` becomes
`app/rag/agent_loop.run_agent_loop`: the model is handed `search_corpus` and
`run_python` and decides for itself whether a second lookup or a chart is worth
its latency. Retrieval still runs first in both cases and is not replaced by the
tool; the loop is the *generation* step, not the retrieval one.
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
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from app.config import settings
from app.db.models import Agent
from app.rag import events
from app.rag.agent_loop import ContextLedger, ToolInvocation, run_agent_loop
from app.rag.llm import build_chat_model
from app.rag.retriever import (
    META_CHUNK_INDEX,
    META_FILENAME,
    RERANK_SCORE_KEY,
    aretrieve,
)
from app.tools.registry import ToolArtifact

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
# **This prompt widened on 2026-08-16, and the sentence it replaced was right for
# the job it had.** It used to say "keep the user's wording everywhere else",
# because every invented word moves the vector away from the user's intent. That
# is still true of INVENTION. It is not true of REPAIR: a misspelled or
# abbreviated term is already not the user's vocabulary matching the corpus's,
# and leaving it alone preserves a distance rather than avoiding one. The line
# between the two is that a repair must be recoverable from the question itself
# plus the conversation -- never from the corpus, never from world knowledge
# about the subject.
#
# **The asymmetry, which is what shapes the wording below.** A FALSE POSITIVE is
# the expensive direction: rewriting a question that was already fine risks
# changing what the user asked, and the answer then arrives confidently about
# something adjacent with no visible cause. A FALSE NEGATIVE -- a typo left
# alone -- costs slightly worse retrieval on a question the corpus may well
# answer anyway. Those are not symmetric, so the prompt is biased toward MINIMAL
# edits and closes with an explicit instruction to return a clean question
# unchanged. `scripts/rewrite_check.py` case 8 is that bias made testable: a
# well-formed question must come back byte-identical, and it is as load-bearing
# as the repair cases, because it is the one protecting the user's meaning.
#
# **ACRONYM EXPANSION WAS BUILT, MEASURED, AND THEN REMOVED THE SAME DAY. That
# is the most important thing in this comment, and the prohibition below is what
# is left of it.**
#
# The bullet first read "Expand acronyms and initialisms into the full term,
# KEEPING the acronym as well" -- which CONTRADICTED the do-not-invent bullet
# four lines under it, because an acronym a first-turn question does not define
# can only be expanded from world knowledge. The model resolved the contradiction
# in favour of the more specific instruction and invented, the same way
# `new features/loop.md` T1 describes two competing instructions resolving. Every
# line here is an observation rather than a worry:
#
#   "how fast is the Ka-band downlink?" -> "Ka-band (Kurtz-band) downlink"
#                                       -> "Ka-band (Kurzwellen-band) downlink"
#        2 of 5 trials, both fabricated, both wrong -- and the rewrite_check case
#        guarding that very question PASSED while printing them.
#   "wats the LS&T alloc" -> "Link System and Telemetry (LS&T)", invented, and it
#        moved retrieval to the WRONG FILE.
#   "hskpng tlm vol per day" -> "Hong Kong SpacePort (HSKP)".
#
# A conditional version was then built and measured green -- expand only when the
# question or the conversation spells the term out, 5/5 in both directions. **It
# was removed anyway, and deliberately.** The feature's whole value was the
# first-turn case, where nothing has spelled anything out; gated on
# recoverability it fires almost never, and what remains is a standing invitation
# to a model that has already been observed reaching past it. The repair bullets
# survive because they were measured clean (typos and shorthand, 5/5, no
# fabrication in any trial); this one does not, because the thing it was for is
# exactly the thing that is unsafe.
#
# So the rule is now UNCONDITIONAL, and that is why it says "not even when the
# conversation spells it out": a conditional prohibition is the shape that failed
# once already. An acronym passes through untouched. Cases 7a and 7c in
# `scripts/rewrite_check.py` pin it, 7c with the measured-harmful "LS&T" string.
#
# **A leave-alone bullet is not free, and the first draft of this one cost a
# repair.** It read "Leave acronyms, initialisms and product names exactly as the
# user wrote them", and typo repair immediately fell from 5/5 to 3/5 -- the
# regression was "ka bnd", which the model had been fixing to "Ka band" and now
# read as a product name to be preserved. Same two words, opposite instructions,
# and the more recent one won. Narrowing the bullet to acronyms and initialisms,
# with an explicit carve-out for ordinary misspellings, restored 5/5 on the next
# run. **A prohibition written to stop one behaviour will stop its neighbours
# unless its edge is stated**, and the only reason this was caught is that case 6
# asserts the repair rather than merely asserting no fabrication.
#
# **What survives here is COREFERENCE, not expansion, and the difference is
# measured rather than argued.** With no history, "C&T" and "LS&T" pass through
# untouched 5/5. With a history that spells the term out, the rewrite comes back
# as "S-band command and telemetry uplink rate" 5/5 -- the only variable being
# the conversation, so those words came from the user's own thread and not from
# priors. That is the coreference bullet resolving a reference, and an
# unconditional acronym ban could only beat it by damaging coreference, which is
# the rewriter's oldest job. Case 7b therefore asserts GROUNDEDNESS -- every
# content word traceable to the question or the conversation -- rather than
# silence, because groundedness is the property whose absence produced
# "Kurtz-band" in the first place.
#
# **The version worth building instead is GROUNDED rather than forbidden.**
# Passing the agent's own document titles into this prompt would let a real
# expansion come from the corpus rather than from priors, which is what the
# bullet was reaching for and never had. That needs `contextualize_question` to
# take the agent, a cache not keyed on one static prompt, and its own
# measurement -- recorded in `new features/10-*.md`. It is not a widening of the
# bullet below; it is a different feature, and the bullet below stays either way.
CONTEXTUALIZE_SYSTEM_PROMPT = """\
Rewrite the user's latest question into the best possible search query for a \
document index, so that it can be understood on its own.

Do all of these:
- Resolve pronouns and references ("it", "that", "the second one") into the \
words they stand for, using the conversation above when there is one.
- Correct obvious misspellings and expand shorthand into the full words \
("dwnlink" -> "downlink", "thruput" -> "throughput").
- Leave acronyms and initialisms exactly as the user wrote them. This does not \
apply to an ordinary misspelled word, which you should still repair.

Do none of these:
- Do not answer the question, or any part of it.
- Do not add facts, qualifiers, topics or subject-matter words that are not in \
the question or in the conversation above.
- NEVER expand an acronym, not even when the conversation above spells it out \
and not even when you are confident. An acronym is the document's own wording \
and will match it; an expansion is a guess, and a wrong guess will not match.
- Do not change what is being asked, and do not make the question broader or \
narrower.
- If the question is already well spelled, unabbreviated and standalone, return \
it unchanged."""


class StandaloneQuestion(BaseModel):
    """The rewrite, as a typed object rather than as a string to be parsed.

    **One field, and it stayed one field when the prompt widened on 2026-08-16.**
    A second required field is a second thing the model must fill, which
    invalidates `config.decision_model`'s 9/9 parsed measurement outright; a
    second optional field costs schema surface for nothing, because the only new
    fact the trace wants -- whether this was a first turn or a follow-up -- is
    computable from `len(history)` at the call site and needs no model to report
    it.
    """

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
    # The standalone question that was actually embedded, or None when the
    # rewrite produced nothing. None is NOT "unchanged" -- see
    # `contextualize_question`.
    rewritten_question: str | None = None
    # Whether the rewriter was CALLED, which stopped being derivable from
    # `rewritten_question is None` when first turns started being rewritten.
    # None used to mean "not asked"; it now means "asked and got nothing back",
    # and the two need opposite responses from anyone reading a trace -- the
    # first is a configuration, the second is a degraded turn.
    rewrite_attempted: bool = False
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

    # ---- the agent loop's output. All default-empty, so every caller that
    # predates it is unaffected and an agent with tools off returns exactly the
    # object it returned before.
    #
    # `tool_calls` is plain data rather than trace rows because this module never
    # touches the database -- `TraceRecorder` is constructed in `ask.run_turn`
    # and is not reachable from here. The loop accumulates, `run_turn` writes,
    # and every row of a turn still lands in one transaction.
    tool_calls: list[ToolInvocation] = field(default_factory=list)
    # Files `run_python` produced, with the source and title a Handout row needs.
    # Bytes, held in memory for the length of the turn; `run_turn` persists them.
    artifacts: list[ToolArtifact] = field(default_factory=list)
    # **`generation_ms` stops meaning "the whole model call" once a loop runs.**
    # `tool_ms` is time spent inside tools and `generation_ms` is summed model
    # time, so `contextualize_ms + retrieval_ms + tool_ms + generation_ms` adds
    # up to the turn instead of overlapping. One combined field would make the
    # trace's own arithmetic wrong, which is worse than an unrecorded number.
    tool_ms: int = 0
    tool_steps: int = 0
    # "max_steps" | "tool_error" | None
    stopped_reason: str | None = None

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


def get_chat_model(agent: Agent | None = None, **overrides) -> ChatOpenAI:
    """The generation model.

    Sampling defaults come from the Gemma 4 model card's "standardized sampling
    configuration across all use cases" (temperature 1.0, top_p 0.95, top_k 64),
    not from the temperature-0 reflex that grounded RAG usually invites. Gemma is
    calibrated for those values; squeezing sampling far below them trades a small
    determinism gain for a real risk of repetition loops. Override per call to
    measure, not by habit.

    **`top_k` survives the move to OpenRouter only because `build_chat_model`
    carries it in `extra_body`.** It is not an OpenAI-API parameter, so the
    client class has no field for it, and the three values above are one
    configuration rather than three knobs -- sending two thirds of it would run
    Gemma outside what it was calibrated for while looking, in the code, like it
    had been configured correctly.

    `convert_system_message_to_human` is gone with the Google client and needs no
    replacement: Gemma 4 supports the system role natively, and the OpenAI
    protocol carries a system message as its own turn. The old comment warned
    against flattening the grounding rules into the user turn; that flattening is
    now not expressible.

    **`top_k` is still passed here even though the default model no longer wants
    it, and that is deliberate.** `build_chat_model` decides per family
    (`_NO_TOP_K_PREFIXES`) and drops it for both Gemini and DeepSeek. Stripping it
    at this call site instead would put the decision in the caller, where the next
    caller would have to rediscover it -- the same argument that keeps every other
    provider quirk inside `llm.py`. An agent pointed back at Gemma still gets its
    card's full sampling configuration from this one line.
    """
    params = {
        "model": (agent.generation_model if agent and agent.generation_model else settings.generation_model),
        "temperature": settings.generation_temperature,
        "top_p": settings.generation_top_p,
        "top_k": settings.generation_top_k,
        "max_tokens": settings.generation_max_tokens,
        # Off by default, and `settings.generation_reasoning` carries the 2x2 that
        # says why turning it off does not cost tool use. Models with no reasoning
        # channel ignore it; it is advertised by every endpoint serving the models
        # this project uses, so it narrows routing for none of them.
        "reasoning": settings.generation_reasoning,
    }
    params.update(overrides)
    return build_chat_model(**params)


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
    model = build_chat_model(
        settings.decision_model,
        temperature=settings.generation_temperature,
        top_p=settings.generation_top_p,
        top_k=settings.generation_top_k,
    )
    # `method="function_calling"` now has a second thing holding it up, and the
    # second one lives outside this file. It emits `tools` and `tool_choice`, and
    # OpenRouter DROPS parameters the routed provider does not support rather
    # than rejecting the request -- two of the eighteen endpoints serving this
    # model advertise no tool support. `openrouter_require_parameters` is what
    # keeps the request from silently landing on one of them and coming back as
    # prose. Turning that flag off breaks this line without touching it.
    return prompt | model.with_structured_output(
        StandaloneQuestion, method=settings.structured_output_method
    )


async def contextualize_question(
    question: str, history: Sequence[ChatTurn]
) -> str | None:
    """Rewrite a question into the best search query it can stand on its own as.

    Returns the rewritten question, or None **only when the rewrite did not
    produce one** -- `settings.rewrite_every_turn` is off and there is no
    history, or the call failed. `AnswerResult.rewrite_attempted` is what
    separates those two, and a caller that needs to know whether a rewrite was
    *tried* must read that rather than infer it from the None.

    **None is not "unchanged".** A rewrite that comes back byte-identical still
    returns the string, because "the model read this and left it alone" and "the
    model was never asked" are different facts and the caller records which one
    happened. Collapsing them would make a question the rewriter declined to
    touch indistinguishable from one it never saw.

    **It runs on first turns as of 2026-08-16.** The old early-out assumed a
    first turn had no references to resolve, which is true and was too narrow: a
    typo and a piece of shorthand are not references, and they are the two
    things most likely to put a question's vector somewhere the corpus is not.
    An acronym is a third such thing and is deliberately NOT repaired -- see the
    comment above `CONTEXTUALIZE_SYSTEM_PROMPT` for the measurement that removed
    expansion after it fabricated.

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
    if not history and not settings.rewrite_every_turn:
        # The pre-2026-08-16 behaviour, preserved behind the flag: a first turn
        # was assumed to have no references to resolve, so it passed through
        # byte-identically. Typos and shorthand are not references, which is
        # what made that assumption too narrow -- and the flag is what keeps
        # `new features/loop.md` S4's "with the feature off the output is
        # byte-identical" expressible for a feature with no per-agent column.
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


def format_context(
    documents: list[Document], *, markers: Sequence[int] | None = None
) -> str:
    """Render retrieved chunks for the prompt, tagged with their filename.

    **One renderer, two forms, and the default is byte-identical to what this
    function has always produced.** Omitting `markers` gives the filename-tagged
    blocks the chain has used since Stage 1, which is what keeps an agent with
    tools off reproducible against every measurement already in EVAL.md.

    `markers` is passed only by `ContextLedger.format_context`, which owns the
    `[n]` numbering for a turn in which the model may retrieve again. Numbering
    the blocks is what lets a marker inside a tool result be the SAME marker the
    user later sees in the citation list -- and `agent_loop.TOOL_GUIDANCE` asks
    the model to cite with `[n]` accordingly. `ask.normalise_citation_markers`
    already resolves both a bare number and a filename, so neither form leaves a
    dangling citation.

    `strict=True` on the zip: a marker list of a different length from the
    document list is a ledger bug, and silently truncating it would attribute
    text to the wrong source while looking perfectly well-formed.
    """
    if markers is None:
        return "\n\n---\n\n".join(
            f"[{doc.metadata.get(META_FILENAME, 'unknown')}]\n{doc.page_content}"
            for doc in documents
        )

    return "\n\n---\n\n".join(
        f"[{marker}] {doc.metadata.get(META_FILENAME, 'unknown')}"
        f"#{doc.metadata.get(META_CHUNK_INDEX, '?')}\n{doc.page_content}"
        for marker, doc in zip(markers, documents, strict=True)
    )


def _tools_active(agent: Agent) -> bool:
    """Both gates must pass, and they are gates on different things.

    `settings.agent_tools_enabled` is an operator kill switch -- one environment
    variable that returns every agent on a deployment to the classic path without
    a code change or a migration. `agents.tools_enabled` is the per-agent choice,
    and its migration deliberately backfills existing rows to false: an agent
    whose eval runs are already recorded in EVAL.md keeps behaving exactly as it
    was measured, while anything created after this ships is agentic out of the
    box.

    Either one off means the classic path, byte for byte.
    """
    return bool(settings.agent_tools_enabled and agent.tools_enabled)


async def answer_question(
    agent: Agent,
    question: str,
    *,
    rerank: bool | None = None,
    history: Sequence[ChatTurn] | None = None,
    rewrite: bool | None = None,
    emit: events.Emit | None = None,
    **model_overrides,
) -> AnswerResult:
    """Run one question through the chain, and return the evidence with it.

    `history` is the thread's prior turns, oldest first, and it is the only new
    input: omit it and this behaves exactly as it did before, which is what
    keeps a one-shot `/api/ask` and a threaded chat on one code path rather than
    two that will diverge.

    `rewrite` overrides `settings.rewrite_every_turn` for THIS call and `None`
    means "use the setting". It exists for one caller -- `app/eval/jobs.py`,
    which needs its golden questions embedded verbatim -- and it deliberately
    does not override history-driven contextualisation, because a caller that
    supplies history has already opted into resolving what its pronouns refer
    to.

    The result now carries the scored retrieval, so a caller that needs
    `query_chunks.similarity_score` no longer searches a second time for numbers
    this function already had.

    **`emit` is a transport, not a decision, and this module still never touches
    the database.** It writes onto an `asyncio.Queue` that `app/api/stream.py`
    drains; it is deliberately not a `TraceRecorder`, so `new features/loop.md`
    S3 stays intact -- the pipeline accumulates, `run_turn` records, and every
    row of a turn still lands in one transaction. With `emit is None` every
    branch below is the identical line it was before streaming existed.

    The phase frames come from HERE rather than from `run_turn` for one reason
    that decides it: `run_turn` computes all of its trace rows *after*
    `answer_question` returns, so a phase emitted there would arrive once the
    stage it describes was already over. A progress event that cannot be early is
    not progress.
    """
    started = time.perf_counter()

    # 1. Rewrite the question into the best search query for it.
    #
    # Known BEFORE the call, because a `finished` with no `started` is the one
    # shape the phase contract reserves for `rerank`. It is also the gate on the
    # call itself rather than only on the frames: `contextualize_question` reads
    # `settings.rewrite_every_turn` for its own early-out and knows nothing about
    # this call's `rewrite` override, so leaving the call unguarded would spend a
    # model call on exactly the eval turn the override exists to keep verbatim.
    t0 = time.perf_counter()
    rewrite_attempted = bool(history) or (
        settings.rewrite_every_turn if rewrite is None else rewrite
    )
    if emit is not None and rewrite_attempted:
        await events.phase(emit, events.PHASE_REWRITE, events.STARTED)
    # **Gated on `rewrite_attempted`, never on `history`.** Leaving it on history
    # is not a cosmetic difference now that first turns rewrite: it is 1-1.6 s of
    # dead air after `start` and before `retrieve started`, with `HEARTBEAT_S`
    # at 10.0 s so nothing at all reaches the browser in the meantime.
    rewritten = (
        await contextualize_question(question, history or ())
        if rewrite_attempted
        else None
    )
    contextualize_ms = int((time.perf_counter() - t0) * 1000)
    if emit is not None and rewrite_attempted:
        await events.phase(
            emit,
            events.PHASE_REWRITE,
            events.FINISHED,
            duration_ms=contextualize_ms,
            # Null and unchanged are different facts -- see
            # `contextualize_question`. A client showing "searched for: ..." must
            # be able to tell "the model read this and left it alone" from "the
            # rewrite failed and the raw question was used".
            rewritten_question=rewritten,
            # And `changed` is the third fact, which the client cannot compute
            # from a phase frame alone -- it never sees the raw question on this
            # channel. Without it the banner would have to fire on every turn,
            # quoting a sentence the user just typed.
            rewritten_changed=(rewritten is not None and rewritten != question),
        )
    search_query = rewritten or question

    # 2. Retrieve once, with the scores.
    t0 = time.perf_counter()
    if emit is not None:
        await events.phase(emit, events.PHASE_RETRIEVE, events.STARTED)
    retrieval = await aretrieve(agent, search_query, rerank=rerank)
    retrieval_ms = int((time.perf_counter() - t0) * 1000)
    if emit is not None:
        await events.phase(
            emit,
            events.PHASE_RETRIEVE,
            events.FINISHED,
            duration_ms=retrieval_ms,
            chunk_count=len(retrieval.scored),
            # Advisory only. `SCORE_CHECK` records why: on-topic questions
            # measured 0.61-0.67 here and off-topic ones 0.49-0.58, so 0.5 sits
            # INSIDE the overlap. It is on the wire because the Trace view shows
            # it, never because a client should branch on it.
            top_score=retrieval.top_score,
        )
        if retrieval.reranked:
            # **`finished` with no `started`, and that asymmetry is deliberate.**
            # Reranking happens inside `retriever.aretrieve`, which stays the
            # single retriever construction seam and therefore takes no `emit`.
            # Threading one in would punch through the seam that keeps the
            # Stage 1 -> Stage 2 change a one-liner, to buy a frame announcing
            # the start of an ~830 ms call that is already bracketed by the
            # retrieve pair. `chunk_count` is what SURVIVED, against retrieve's
            # count of what was returned -- the two numbers next to each other
            # are the reranking demo.
            await events.phase(
                emit,
                events.PHASE_RERANK,
                events.FINISHED,
                chunk_count=len(retrieval.documents),
            )

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
    system_prompt = agent.system_prompt or DEFAULT_SYSTEM_PROMPT
    model = get_chat_model(agent, **model_overrides)

    tool_calls: list[ToolInvocation] = []
    artifacts: list[ToolArtifact] = []
    tool_ms = 0
    tool_steps = 0
    stopped_reason: str | None = None
    documents = retrieval.documents

    if _tools_active(agent):
        # **The branch, and it is the only structural change to this function.**
        # Retrieval above still ran unconditionally and is not replaced by the
        # search tool: the first search is wanted in every real turn, so making
        # the model ask for it would waste a round trip, and RETRIEVE /
        # SCORE_CHECK keep working unchanged. The tool is for the second search
        # onward.
        #
        # `agent.max_tool_steps` is used directly rather than being clamped by
        # `settings.agent_max_tool_steps`. The setting is the DEFAULT for the
        # column (the migration's server default), not a ceiling -- clamping
        # would make an operator's per-agent value silently not apply, which is
        # the failure mode where a knob looks set and does nothing.
        #
        # The ledger is constructed INSIDE this branch, not above it. Feature
        # 1's sketch seeds it before the branch, which reads well and would put
        # a dedupe pass on the classic path for a result that path never uses --
        # and "the classic path is byte-identical when tools are off" is a hard
        # requirement here, easiest to keep by making it structurally true.
        ledger = ContextLedger.seed(retrieval)
        loop = await run_agent_loop(
            agent=agent,
            question=search_query,
            ledger=ledger,
            system_prompt=system_prompt,
            model=model,
            max_steps=(
                agent.max_tool_steps
                if agent.max_tool_steps is not None
                else settings.agent_max_tool_steps
            ),
            # The loop emits its own `generate` phases, one pair per step, plus
            # the three tool events -- it has to, because a tool call's timing is
            # only knowable from inside it, and `run_turn` writes the durable
            # TOOL_* rows long after the user has stopped waiting.
            emit=emit,
        )
        text = loop.text
        tool_calls, artifacts = loop.tool_calls, loop.artifacts
        tool_ms, tool_steps, stopped_reason = (
            loop.tool_ms,
            loop.steps,
            loop.stopped_reason,
        )
        generation_ms = loop.generation_ms
        # **The ledger, not the retrieval.** A search the model ran mid-turn adds
        # entries here, and `ask.run_turn` enumerates this list from 1 to build
        # `AskOut.citations` -- so a chunk the tool found is only citable if it
        # is in the list the pipeline returns. Marker order is list order, which
        # is the invariant the whole ledger exists to hold.
        documents = ledger.documents
    else:
        chain = (
            ChatPromptTemplate.from_messages(
                [
                    SystemMessage(content=system_prompt),
                    ("human", USER_TEMPLATE),
                ]
            )
            | model
            | StrOutputParser()
        )
        chain_input = {
            "context": format_context(retrieval.documents),
            "question": search_query,
        }
        t0 = time.perf_counter()
        if emit is None:
            # The identical line it has always been. `new features/loop.md` S4:
            # with the feature off the output is byte-identical, not similar --
            # every number in EVAL.md was measured through this call.
            text = await chain.ainvoke(chain_input)
            generation_ms = int((time.perf_counter() - t0) * 1000)
        else:
            await events.phase(emit, events.PHASE_GENERATE, events.STARTED)
            # The chain ends in `StrOutputParser()`, so `astream` yields plain
            # `str` and accumulation is a join -- no chunk merging, no message
            # object, which is why the classic path carries none of the risk the
            # tool path had to be probed for.
            #
            # Empty fragments are dropped rather than sent. They carry nothing,
            # and every frame costs a `seq` a client is entitled to treat as
            # meaningful.
            parts: list[str] = []
            async for piece in chain.astream(chain_input):
                if not piece:
                    continue
                parts.append(piece)
                await emit(events.TOKEN, {"text": piece})
            text = "".join(parts)
            generation_ms = int((time.perf_counter() - t0) * 1000)
            await events.phase(
                emit,
                events.PHASE_GENERATE,
                events.FINISHED,
                duration_ms=generation_ms,
            )

    return AnswerResult(
        question=question,
        answer=text,
        documents=documents,
        # The FIRST retrieval's candidates, deliberately, even when tools ran.
        # `ask.run_turn` builds the RETRIEVE event out of this list, and that
        # event describes the unconditional retrieval that happened before the
        # loop -- its `top_score` is what SCORE_CHECK compares and what Stage 3
        # will calibrate. Appending the model's own searches here would make one
        # trace event describe several retrievals at once, and `top_score` would
        # silently become "the best score of any search this turn".
        scored=retrieval.scored,
        rewritten_question=rewritten,
        rewrite_attempted=rewrite_attempted,
        model=(agent.generation_model or settings.generation_model),
        reranked=retrieval.reranked,
        latency_ms=int((time.perf_counter() - started) * 1000),
        contextualize_ms=contextualize_ms,
        retrieval_ms=retrieval_ms,
        generation_ms=generation_ms,
        tool_calls=tool_calls,
        artifacts=artifacts,
        tool_ms=tool_ms,
        tool_steps=tool_steps,
        stopped_reason=stopped_reason,
    )
