"""Choosing which teaching specialist answers a turn.

Two mechanisms, and the difference between them is the whole design:

- `parse_mentions` is PARSING. The user typed `@feynman`; there is no judgement
  left to make, and asking a model to ratify a decision it can already read is a
  round trip spent on obedience.
- `route_specialist` is a MODEL CALL, and it is a plain code path rather than a
  tool because it runs on every orchestrator turn. `new features/loop.md` section 1:
  "If it must run every time, call it yourself." A `route()` tool would cost a
  round trip to learn something the code is going to ask for unconditionally.

WHY THIS IS NOT A FIELD ON THE REWRITER'S SCHEMA

The obvious edit is to add `specialist` to `StandaloneQuestion` -- one structured
call instead of two, on a rewriter that already runs every turn. That edit is a
trap this repository has already paid for. `new features/10-routing-and-embeddings.md`
section 5.2 records adding a "leave product names alone" bullet to that prompt
taking typo repair from 5/5 to 3/5, and an "expand acronyms" bullet making the
model fabricate "Ka-band (Kurzwellen-band)". A prompt that has been measured is a
prompt with a blast radius, and this one is measured by `scripts/rewrite_check.py`.

The latency argument for merging is answered by concurrency instead: both calls
take the raw question and the same capped history, neither depends on the other,
so `asyncio.gather` in `pipeline.answer_question` costs the slower of the two
rather than their sum.

AND IT ROUTES ON THE RAW QUESTION, NOT THE REWRITTEN ONE

Deliberate, and not merely a consequence of running them concurrently. The signal
a router reads -- "explain", "quiz me", "how do I work out" -- is a property of
how the user ASKED, and normalising a question into a standalone search query is
exactly the operation that flattens it away.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal, Sequence

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.runnables import Runnable
from pydantic import BaseModel, Field

from app.config import settings
from app.db.specialists import SPECIALISTS, Specialist, resolve
from app.rag.llm import build_chat_model
from app.metering.context import meter_as

log = logging.getLogger(__name__)

# How many prior turns the router sees. Matches `pipeline.HISTORY_TURNS` by
# intent rather than by import: the router wants recent shape ("they have been
# asking me to explain things"), not the full thread, and coupling it to the
# rewriter's window would make one number serve two unrelated judgements.
ROUTER_HISTORY_TURNS = 4

# --------------------------------------------------------------------------
# Mentions
# --------------------------------------------------------------------------

# `(?<![\w@])` is what keeps an email address out of the routing path:
# `simon@groundwork.dev` has a word character before the `@`, so it never
# matches. `@{1,2}` accepts `@@feynman` as well as `@feynman` -- multiple
# specialists are expressed as multiple mentions, but a user who doubles the
# sigil out of habit should not silently get plain text.
_MENTION = re.compile(r"(?<![\w@])@{1,2}([A-Za-z][\w-]*)")

# Collapse the whitespace a stripped mention leaves behind, without touching
# newlines -- a multi-line question keeps its shape.
_RUNS_OF_SPACE = re.compile(r"[ \t]{2,}")


@dataclass(frozen=True)
class MentionParse:
    """What `parse_mentions` found.

    `question` is what reaches the rewriter and the embedder; the RAW text is
    what gets stored in `queries.question`, so the thread shows what the user
    actually typed. Those are deliberately different strings: `@feynman` means
    nothing in vector space, and it reaches a rewriter documented to mangle
    terms it does not recognise.
    """

    specialists: tuple[Specialist, ...]
    question: str
    matched: tuple[str, ...]


def parse_mentions(raw: str, roster: Sequence[Specialist]) -> MentionParse:
    """Pull `@specialist` tokens off the front of a question.

    Only tokens resolving to a specialist IN THIS AGENT'S ROSTER are treated as
    mentions. Everything else is left exactly where it was, which is what stops
    `what is @risk in this design` and `mail me @ 5pm` from becoming routing
    events. An unknown token is not an error and not a guess -- it is text.
    """
    allowed = {s.slug for s in roster}
    if not allowed:
        return MentionParse(specialists=(), question=raw, matched=())

    found: list[Specialist] = []
    matched: list[str] = []
    seen: set[str] = set()

    def _swap(match: re.Match[str]) -> str:
        token = match.group(1)
        specialist = resolve(token)
        if specialist is None or specialist.slug not in allowed:
            return match.group(0)  # untouched -- it was never a mention
        matched.append(token)
        if specialist.slug not in seen:
            seen.add(specialist.slug)
            found.append(specialist)
        return ""

    stripped = _MENTION.sub(_swap, raw)
    stripped = _RUNS_OF_SPACE.sub(" ", stripped).strip()

    # `@feynman` on its own leaves nothing to retrieve. Keep the raw text so the
    # turn still has a question, rather than embedding an empty string -- the
    # mention is honoured either way.
    question = stripped or raw.strip()

    return MentionParse(
        specialists=tuple(found), question=question, matched=tuple(matched)
    )


# --------------------------------------------------------------------------
# The router
# --------------------------------------------------------------------------

_SLUGS = tuple(s.slug for s in SPECIALISTS)


class RouteDecision(BaseModel):
    """The router's structured output.

    `specialist` is a `Literal` over the FULL registry rather than over the
    agent's roster, so the schema is static and `get_router` can stay cached.
    Membership in the agent's own roster is checked after the call -- a model
    naming a specialist the owner disabled is treated as a failed route, not as
    permission.
    """

    specialist: Literal[_SLUGS] = Field(  # type: ignore[valid-type]
        description="The slug of the teaching approach that fits this question."
    )
    why: str = Field(
        description=(
            "One short clause naming the signal in the question that decided "
            "it. For the trace only; the learner never sees it."
        )
    )


ROUTER_SYSTEM_PROMPT = """\
You choose which teaching approach should answer a learner's question. You do \
not answer it yourself and you never see the course material.

Choose from exactly these:

{roster}

How to choose:
- Read what the learner is ASKING FOR, not what the topic is. "Explain the link \
budget" and "quiz me on the link budget" share a topic and want opposite \
approaches.
- The last thing they said carries the most weight. Earlier turns are context \
for what they already have, not a reason to keep giving them the same thing.
- If they describe their own work or an attempt they made, that is reflection or \
coaching, not explanation.
- If nothing in the question distinguishes the approaches, choose the Explainer. \
It is the safe default: it answers the question directly and says which part the \
material does not cover.

Give one clause in `why` naming the words that decided it.
"""


def _roster_block(roster: Sequence[Specialist]) -> str:
    return "\n".join(f"- {s.slug} ({s.role}): {s.when_to_use}" for s in roster)


@lru_cache(maxsize=1)
def get_router() -> Runnable:
    """The routing chain, bound to a schema. One instance, shared.

    Cached and agent-independent for the same reason `get_contextualizer` is:
    there is no per-agent decision-model column, and the roster travels as a
    prompt variable rather than as part of the schema.

    `method="function_calling"` is load-bearing and is NOT the default -- see
    `get_contextualizer` for the measurement. Gemma emits schema-correct JSON and
    sometimes fences it, and a strict parser answers a fence with `None` rather
    than an exception, which is the worst possible failure shape for a value the
    caller is about to branch on.
    """
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", ROUTER_SYSTEM_PROMPT),
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
    return prompt | model.with_structured_output(
        RouteDecision, method=settings.structured_output_method
    )


@dataclass(frozen=True)
class RouteResult:
    """Which specialists answer this turn, and how that was decided.

    `specialists` is empty only when the agent has no roster at all -- the
    classic path. A failed route returns the fallback specialist with
    `trigger="fallback"`, never an empty tuple, because a turn must always have
    a prompt.
    """

    specialists: tuple[Specialist, ...]
    trigger: str  # "mention" | "router" | "fallback"
    why: str | None
    failed: bool = False

    @property
    def primary(self) -> Specialist | None:
        return self.specialists[0] if self.specialists else None


async def route_specialist(
    question: str,
    history: Sequence[tuple[str, str | None]],
    roster: Sequence[Specialist],
) -> RouteResult:
    """Pick the specialist for this turn.

    Never raises. A failed router degrades to the agent's own prompt, exactly as
    a failed rewrite degrades to the un-rewritten question: a turn answered in
    the wrong voice is a worse answer, whereas an exception here is no answer at
    all to a question the corpus could have answered.

    Returns `trigger="fallback"` and `failed=True` on that path so the trace can
    distinguish "the router chose the Explainer" from "the router died and the
    Explainer is what you get" -- two facts that are indistinguishable from the
    answer text and need opposite responses.
    """
    if not roster:
        return RouteResult(specialists=(), trigger="router", why=None)

    messages: list[BaseMessage] = []
    for prior_question, prior_answer in list(history)[-ROUTER_HISTORY_TURNS:]:
        messages.append(HumanMessage(content=prior_question))
        if prior_answer:
            messages.append(AIMessage(content=prior_answer))

    try:
        with meter_as(call_kind="route"):
            decision = await get_router().ainvoke(
                {
                    "roster": _roster_block(roster),
                    "history": messages,
                    "question": question,
                }
            )
    except Exception as exc:  # noqa: BLE001 - see the docstring
        log.warning("Routing failed, falling back to the agent prompt: %s", exc)
        return RouteResult(
            specialists=(), trigger="fallback", why=None, failed=True
        )

    # Structured output can hand back None instead of raising. function_calling
    # should make that unreachable; it is checked anyway, because the cost of
    # being wrong is an AttributeError inside a request.
    chosen = resolve(decision.specialist) if decision is not None else None
    if chosen is None or chosen.slug not in {s.slug for s in roster}:
        # A specialist the owner disabled is a failed route, not permission.
        log.warning(
            "Router chose %r, which is not in this agent's roster; falling back",
            getattr(decision, "specialist", None),
        )
        return RouteResult(
            specialists=(), trigger="fallback", why=None, failed=True
        )

    return RouteResult(
        specialists=(chosen,), trigger="router", why=decision.why.strip() or None
    )
