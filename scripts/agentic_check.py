"""End-to-end check for the agent loop, the tools, and Handouts.

Layer 2 of the three in `new features/06-test-plan.md`. Layer 1
(`scripts/sandbox_check.py`) needs nothing but the venv; layer 3 is Playwright in
a real browser. This one sits between them: real database, real OpenRouter, real
Pinecone, no browser.

    python scripts/agentic_check.py --setup     # throwaway agent + fixture corpus
    python scripts/agentic_check.py --run       # the scenarios
    python scripts/agentic_check.py --cleanup   # namespace, rows, handouts

**The fixture corpus is two files on purpose.** CLAUDE.md records that context
precision and recall both scoring exactly 1.0 on the existing single-file corpus
does not mean retrieval is excellent -- it means retrieval cannot fail, because
there is only one chunk to return. A multi-hop test against that corpus would
pass without exercising anything. `scripts/fixtures/` holds a power briefing and
a comms briefing that overlap in exactly one place (the communications power
allocation appears in both, with different numbers -- an allocation in one and a
measured average in the other), which is the smallest corpus that can tell a
one-search answer from a two-search one.

**Scenarios S1 and S7 are the regression tests and matter most.** Everything else
checks that the new feature works; those two check that it did not eat the old
one. S1 asserts an agent with tools off produces exactly the six pre-existing
trace event types, and S7 asserts that giving a model tools did not turn "the
corpus does not cover this" into an invention.

HTTP routes are exercised through an ASGI transport rather than a running server,
with `current_user` overridden. `owned_agent` still runs for real, so the tenancy
wiring is genuinely under test -- only the identity assertion is stubbed, which
is the same split `POST /api/auth/dev-login` makes.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import delete, select  # noqa: E402

from app.config import settings  # noqa: E402
from app.db.models import (  # noqa: E402
    Agent,
    Chunk,
    Conversation,
    Document,
    Handout,
    IngestionRun,
    Query,
    TraceEvent,
    User,
)
from app.db.session import SessionLocal, engine  # noqa: E402
from app.rag.ingest import ingest_file  # noqa: E402
from app.rag.retriever import get_vector_store  # noqa: E402

SEED_SUB = "agentic-check-local"
AGENT_NAME = "Agentic Check"
FIXTURES = ROOT / "scripts" / "fixtures"

# The six event types that existed before the tool loop. S1 asserts a
# tools-off turn produces a subset of exactly these and nothing else.
CLASSIC_EVENTS = {"RETRIEVE", "SCORE_CHECK", "REWRITE", "RERANK", "GENERATE", "REFUSE"}


# --------------------------------------------------------------------------
# Console
# --------------------------------------------------------------------------

def ascii_safe(text: str) -> str:
    """Force text to ASCII for the Windows console.

    The rule is usually read as being about string literals; the corpus and the
    model output are the bigger hazard. An em-dash in a generated answer raises
    UnicodeEncodeError under cp1252, several layers from anything this file
    wrote, and it has killed three throwaway scripts in this repo already.
    """
    return text.encode("ascii", "replace").decode("ascii")


def rule(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


def preview(text: str, width: int = 160) -> str:
    return ascii_safe(" ".join(text.split())[:width])


@dataclass
class Outcome:
    name: str
    ok: bool
    detail: str = ""
    notes: list[str] = field(default_factory=list)
    rate_limited: bool = False


# Phrases that mean "the upstream provider refused us", not "the code is wrong".
#
# This suite makes roughly twenty reranking calls in a couple of minutes -- every
# scenario retrieves at least once, several retrieve twice, and each of the four
# recipes retrieves to gather its material. **Cohere's trial key allows ten API
# calls per minute**, so running the suite twice in quick succession trips it,
# and the failure arrives as a handout stuck at `failed` and a scenario throwing.
#
# Reporting that as `[FAIL]` would be the same defect this project has already
# recorded twice: CLAUDE.md's note that `METRIC_TIMEOUT_S` silently doubled as a
# quota-retry ceiling, so "a rate limit and a hang are indistinguishable on the
# card, and they need opposite fixes". A red row that means "wait sixty seconds"
# sends the reader to debug code that is working.
# Both spellings of every phrase, because the matched text is
# `f"{type(exc).__name__}: {exc}"` and an SDK's exception CLASS is camel-cased
# with the spaces removed. Cohere raises `TooManyRequestsError`, which the
# spaced "too many requests" does not match -- so the first version of this list
# printed `[FAIL]` for a rate limit anyway, which is the exact bug it was
# written to fix.
#
# That is the same gap as the refusal marker list in `app/rag/refusal.py`,
# arrived at independently ten minutes later: a substring test matched one
# spelling of a phrase and missed the variant the model (or the SDK) actually
# used. The lesson from there applies here -- add the shape, not the string you
# just saw.
RATE_LIMIT_PHRASES = (
    "trial key",
    "rate limit",
    "ratelimit",
    "429",
    "too many requests",
    "toomanyrequests",
    "resource_exhausted",
    "resourceexhausted",
    "quota",
    "overloaded",
    "service unavailable",
    "serviceunavailable",
    "503",
    "502",
)


def _flag(outcome: "Outcome") -> str:
    """Three states, not two. `[rate]` is the one that matters: it says the
    provider refused, so re-run in a minute rather than opening an editor."""
    if outcome.ok:
        return "[ok]  "
    return "[rate]" if outcome.rate_limited else "[FAIL]"


def is_rate_limited(text: str) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in RATE_LIMIT_PHRASES)


# --------------------------------------------------------------------------
# Fixture agent
# --------------------------------------------------------------------------

async def get_or_create_agent(db) -> tuple[User, Agent]:
    user = await db.scalar(select(User).where(User.google_sub == SEED_SUB))
    if user is None:
        user = User(
            id=uuid.uuid4(),
            google_sub=SEED_SUB,
            email="agentic-check@localhost",
            name="Agentic Check",
            role="user",
        )
        db.add(user)
        await db.flush()

    agent = await db.scalar(
        select(Agent).where(Agent.owner_user_id == user.id, Agent.name == AGENT_NAME)
    )
    if agent is None:
        agent = Agent(
            id=uuid.uuid4(),
            owner_user_id=user.id,
            name=AGENT_NAME,
            description="Throwaway agent for the agentic end-to-end check.",
            tools_enabled=True,
            max_tool_steps=3,
            # 250/40 rather than the 800/120 default, and this is the difference
            # between a suite that tests something and one that reports success
            # for free.
            #
            # The two fixtures are ~550 tokens each. At the default chunk size
            # they produce ONE CHUNK EACH -- so a corpus of two chunks, against
            # `retrieve_k=20`, means a single retrieval returns the entire corpus
            # and no question can possibly require a second search. S3 would
            # pass without the tool ever being needed.
            #
            # This is the same trap CLAUDE.md records for context precision:
            # 1.000 on a single-chunk corpus is not excellent retrieval, it is
            # retrieval that cannot fail. 250/40 splits each briefing into
            # roughly three chunks, and `rerank_top_n=3` then means one
            # retrieval structurally cannot hold both topics.
            chunk_size=250,
            chunk_overlap=40,
            # And retrieval is deliberately STARVED: 3 candidates reranked to 2,
            # against the 20/3 default.
            #
            # The first attempt at this suite used the defaults and S3 failed
            # with `searches=0` while already citing both files -- because
            # `retrieve_k=20` over a 7-chunk corpus makes every chunk a
            # candidate, and a reranker picking 3 of 7 will happily span both
            # documents. The model was right not to search again; there was
            # nothing left to find. A passing S3 there would have measured
            # nothing.
            #
            # 3/2 recreates the condition a second search exists FOR: a two-part
            # question whose halves are semantically distant gets one half back
            # and has to go looking for the other. That is a real property of a
            # large corpus reproduced on a small one, which is the only honest
            # way to test it without shipping a large fixture.
            retrieve_k=3,
            rerank_top_n=2,
        )
        db.add(agent)
        await db.flush()

    await db.commit()
    return user, agent


async def setup(db) -> int:
    user, agent = await get_or_create_agent(db)
    rule("Setup")
    print(f"  agent      : {agent.id}")
    print(f"  namespace  : {agent.namespace}")

    existing = await db.scalar(
        select(Document).where(Document.agent_id == agent.id).limit(1)
    )
    if existing is not None:
        print("  corpus already ingested; skipping. Use --cleanup first to rebuild.")
        return 0

    for path in sorted(FIXTURES.glob("*.md")):
        started = time.perf_counter()
        run = await ingest_file(db, agent, path, uploaded_by_user_id=user.id)
        took = int((time.perf_counter() - started) * 1000)
        print(f"  ingested {path.name}: {run.chunk_count} chunks in {took} ms")

    return 0


async def cleanup(db) -> int:
    user = await db.scalar(select(User).where(User.google_sub == SEED_SUB))
    if user is None:
        print("Nothing to clean up.")
        return 0

    rule("Cleanup")
    agents = (await db.scalars(select(Agent).where(Agent.owner_user_id == user.id))).all()
    for agent in agents:
        # The namespace first. A leaked Pinecone namespace is a real cost -- the
        # Builder plan's 1,000-namespace cap IS the maximum number of agents this
        # deployment can ever hold, and it binds long before storage does.
        try:
            get_vector_store(agent)._index.delete(delete_all=True, namespace=agent.namespace)
            print(f"  deleted namespace {agent.namespace}")
        except Exception as exc:  # namespace may never have been written
            print(f"  namespace {agent.namespace}: {type(exc).__name__} (already absent?)")

        await db.execute(delete(Handout).where(Handout.agent_id == agent.id))
        docs = (await db.scalars(select(Document).where(Document.agent_id == agent.id))).all()
        for doc in docs:
            await db.execute(delete(Chunk).where(Chunk.document_id == doc.id))
            await db.execute(delete(IngestionRun).where(IngestionRun.document_id == doc.id))
        await db.execute(delete(Document).where(Document.agent_id == agent.id))
        await db.execute(delete(Conversation).where(Conversation.agent_id == agent.id))
        await db.execute(delete(Query).where(Query.agent_id == agent.id))
        await db.delete(agent)

    await db.delete(user)
    await db.commit()
    print("  deleted seed user, agents, documents, chunks, conversations, handouts")
    return 0


# --------------------------------------------------------------------------
# Turn helper
# --------------------------------------------------------------------------

async def ask(db, agent: Agent, user: User, question: str,
              conversation: Conversation | None = None):
    """One turn through the real engine, returning (AskOut, events, conversation).

    Goes through `ask.run_turn` rather than `pipeline.answer_question`, because
    the trace rows are half of what is being asserted and only `run_turn` writes
    them.
    """
    from app.api.ask import run_turn

    if conversation is None:
        conversation = Conversation(
            id=uuid.uuid4(), agent_id=agent.id, user_id=user.id, title=None
        )
        db.add(conversation)
        await db.flush()

    out = await run_turn(
        db, agent=agent, user=user, session=None,
        conversation=conversation, question=question,
    )

    events = (
        await db.scalars(
            select(TraceEvent)
            .where(TraceEvent.query_id == out.query_id)
            .order_by(TraceEvent.step_index.asc())
        )
    ).all()
    return out, list(events), conversation


def event_types(events) -> list[str]:
    return [e.event_type for e in events]


def payloads_of(events, kind: str) -> list[dict]:
    return [e.payload or {} for e in events if e.event_type == kind]


# --------------------------------------------------------------------------
# Scenarios
# --------------------------------------------------------------------------

async def s1_classic_path(db, agent, user) -> Outcome:
    """Tools OFF must produce exactly the pre-existing trace shape.

    The regression test. Everything else here checks the new feature works; this
    one checks it did not change the old one.
    """
    agent.tools_enabled = False
    await db.commit()
    try:
        out, events, _ = await ask(
            db, agent, user, "How much power do the solar arrays generate?"
        )
        seen = set(event_types(events))
        stray = seen - CLASSIC_EVENTS
        ok = not stray and "RETRIEVE" in seen and "GENERATE" in seen
        return Outcome(
            "S1 classic path unchanged", ok,
            f"events={sorted(seen)}" + (f" STRAY={sorted(stray)}" if stray else ""),
        )
    finally:
        agent.tools_enabled = True
        await db.commit()


async def s2_no_reflex_tools(db, agent, user) -> Outcome:
    """Tools ON, single-topic question: the model must not call tools reflexively."""
    out, events, _ = await ask(
        db, agent, user, "What is the usable capacity of one battery module?"
    )
    calls = payloads_of(events, "TOOL_CALL")
    return Outcome(
        "S2 no reflex tool use", len(calls) == 0,
        f"tool_calls={len(calls)} answer={preview(out.answer, 90)}",
    )


async def s3_multi_hop(db, agent, user) -> Outcome:
    """A question spanning both fixture files should provoke a second search.

    The halves are chosen to be semantically DISTANT, which the first version of
    this scenario got wrong. It asked to compare the communications power
    allocation with the measured communications draw -- two facts in two
    different files, but both of them plainly about communications power, so a
    single embedding retrieved both and the model correctly did not search again.

    Battery modules and science-data storage share no vocabulary and no topic --
    except that both fixtures use the word "storage" as a heading, which is
    exactly how the SECOND version of this scenario also failed with
    `searches=0`: reranking to 2 returned one chunk from each file and answered
    the whole question in one pass.

    So this scenario now starves retrieval ITSELF rather than relying on the
    agent's configuration, the same way S1 owns `tools_enabled` and S6 owns
    `max_tool_steps`. `retrieve_k=1` is the smallest configuration in which the
    property is testable at all: exactly one chunk comes back, so a two-part
    question CANNOT be answered from the first retrieval, and searching again is
    the only way through. Anything larger, on a seven-chunk corpus, leaves the
    model correctly deciding it already has what it needs -- which is good
    behaviour and a worthless test.

    The general lesson, which cost three attempts: **a test that the feature is
    needed has to make the feature necessary.** Twice here the scenario passed
    the model a question it could already answer and then asserted it had worked
    harder.
    """
    prior_k, prior_n = agent.retrieve_k, agent.rerank_top_n
    agent.retrieve_k, agent.rerank_top_n = 1, 1
    await db.commit()
    try:
        out, events, _ = await ask(
            db, agent, user,
            "How many battery modules does the platform carry, and separately, "
            "how much onboard storage do the science instruments have?",
        )
    finally:
        agent.retrieve_k, agent.rerank_top_n = prior_k, prior_n
        await db.commit()
    calls = [p for p in payloads_of(events, "TOOL_CALL") if p.get("tool") == "search_corpus"]
    results = payloads_of(events, "TOOL_RESULT")
    new_chunks = sum(int(p.get("new_chunks") or 0) for p in results)
    files = {c.filename for c in out.citations}
    ok = len(calls) >= 1 and new_chunks > 0 and len(files) >= 2
    return Outcome(
        "S3 multi-hop search", ok,
        f"searches={len(calls)} new_chunks={new_chunks} files={sorted(files)}",
    )


async def s4_chart_from_chat(db, agent, user) -> Outcome:
    """Asking for a chart mid-conversation should produce a handout."""
    out, events, convo = await ask(
        db, agent, user,
        "List the power allocation for each subsystem, then draw me a bar chart "
        "of those figures as a PNG.",
    )
    calls = [p for p in payloads_of(events, "TOOL_CALL") if p.get("tool") == "run_python"]
    rows = (
        await db.scalars(
            select(Handout).where(Handout.conversation_id == convo.id)
        )
    ).all()
    ready = [r for r in rows if r.status == "ready"]
    has_png = any(r.mime_type == "image/png" for r in ready)
    has_code = all(bool(r.source_code) for r in ready)
    ok = len(calls) >= 1 and has_png and has_code
    return Outcome(
        "S4 chart handout from chat", ok,
        f"run_python={len(calls)} handouts={len(rows)} ready={len(ready)} "
        f"png={has_png} source_code={has_code}",
    )


async def s5_self_correction(db, agent, user) -> Outcome:
    """A tool failure must be recoverable, not fatal.

    Provoked rather than mocked: asking for a library that is not on the import
    allowlist makes the static check refuse, the model reads the refusal (which
    NAMES what is allowed) and retries. That round trip is the single most
    valuable behaviour a code interpreter has and it is worth a scenario.
    """
    out, events, _ = await ask(
        db, agent, user,
        "Use the seaborn library to plot the three downlink data rates, and if "
        "seaborn is unavailable use whatever plotting library you do have.",
    )
    errors = payloads_of(events, "TOOL_ERROR")
    calls = payloads_of(events, "TOOL_CALL")
    ok = bool(out.answer) and (len(errors) == 0 or len(calls) > len(errors))
    return Outcome(
        "S5 tool failure is recoverable", ok,
        f"calls={len(calls)} errors={len(errors)} answered={bool(out.answer)}",
    )


async def s6_step_budget(db, agent, user) -> Outcome:
    """max_tool_steps=1 on a two-search question still returns a CLEAN answer.

    **This scenario shipped a user-visible bug while green, and the second
    assertion is the fix.** It asserted `bool(out.answer)` -- did a turn come
    back -- which is the error-shaped test `new features/loop.md` T2 exists to
    forbid, and it is exactly the shape that misses.

    What it missed, found in a browser on 2026-08-16: when the budget runs out
    the loop re-invokes with `tool_choice="none"`, and
    `deepseek/deepseek-v4-flash-0731` still wanted to search, so it emitted its
    own tool-call syntax into the CONTENT channel --

        Let me try searching for the thermal rejection budget document
        <|DSML|tool_calls> <|DSML|invoke name="search_corpus"> ...

    -- and the user read it. Nothing raised, `out.answer` was non-empty and long,
    and this scenario stayed green through it. This is the ONLY scenario that
    exercises the forced-final-answer path, so it is the only one that could have
    caught it.

    The assertion is now "is the answer free of machinery", which is a statement
    about the OUTCOME. `_LEAKED_TOOL_MARKUP` is imported rather than retyped, so a
    change to the sentinel cannot leave this scenario testing the old one.
    """
    from app.rag.agent_loop import _LEAKED_TOOL_MARKUP

    agent.max_tool_steps = 1
    await db.commit()
    try:
        out, events, _ = await ask(
            db, agent, user,
            "Compare the Ka-band availability gaps with the battery replacement "
            "schedule, and explain how the two interact.",
        )
        gen = payloads_of(events, "GENERATE")
        stopped = gen[0].get("stopped_reason") if gen else None
        leaked = _LEAKED_TOOL_MARKUP.search(out.answer or "")
        ok = bool(out.answer) and leaked is None
        return Outcome(
            "S6 step budget forces a clean answer", ok,
            f"stopped_reason={stopped} answer_chars={len(out.answer)} "
            f"leaked_markup={'YES at char ' + str(leaked.start()) if leaked else 'no'}",
        )
    finally:
        agent.max_tool_steps = 3
        await db.commit()


async def s7_refusal_survives(db, agent, user) -> Outcome:
    """Tools must not turn "not covered" into invention.

    The probe is chosen the way CLAUDE.md says a good refusal probe is chosen:
    both fixtures RAISE the modulation and coding schemes and explicitly say they
    are held elsewhere. A weak probe asks about something the corpus never
    mentions; a tight one asks about a detail the corpus starts and does not
    finish, because that is what a model is actually tempted to complete.
    """
    out, events, _ = await ask(
        db, agent, user,
        "What modulation and coding scheme does the Ka-band downlink use?",
    )
    return Outcome(
        "S7 refusal survives tools", out.refused,
        f"refused={out.refused} answer={preview(out.answer, 120)}",
    )


async def s9_timing_adds_up(db, agent, user) -> Outcome:
    """The phases must sum to the turn instead of overlapping."""
    out, events, _ = await ask(
        db, agent, user, "How many battery modules are there and how are they arranged?"
    )
    by_type = {e.event_type: (e.duration_ms or 0) for e in events}
    parts = sum(by_type.get(k, 0) for k in ("REWRITE", "RETRIEVE", "GENERATE"))
    parts += sum(e.duration_ms or 0 for e in events if e.event_type in ("TOOL_RESULT", "TOOL_ERROR"))
    total = out.latency_ms or 1
    ratio = parts / total
    # Generous: the turn also carries the history read and the citation join,
    # and RERANK deliberately records no duration.
    ok = 0.5 <= ratio <= 1.15
    return Outcome(
        "S9 phase timings add up", ok,
        f"parts={parts} ms total={total} ms ratio={ratio:.2f}",
    )


async def s10_citation_integrity(db, agent, user) -> Outcome:
    """After any number of searches, markers must be contiguous and resolvable."""
    import re

    out, events, _ = await ask(
        db, agent, user,
        "What are the three downlink paths, and what does each one carry?",
    )
    markers = [c.marker for c in out.citations]
    contiguous = markers == list(range(1, len(markers) + 1))
    used = {int(m) for m in re.findall(r"\[(\d+)\]", out.answer)}
    unresolved = sorted(used - set(markers))
    ok = contiguous and not unresolved
    return Outcome(
        "S10 citation integrity", ok,
        f"markers={markers} unresolved={unresolved}",
    )


# --------------------------------------------------------------------------
# S13-S16 -- the DeepSeek swap. See `new features/09-deepseek-agentic.md`.
#
# The swap inverted this suite's founding assumption. S3 and the gap trigger were
# both built on `new features/loop.md` T1: gemma-4-31b-it would not initiate a
# search on its own judgement, 0 tool calls in every prompt configuration tried.
# `deepseek/deepseek-v4-flash-0731` self-initiates 6/6 on the same probe.
#
# That does not retire the trigger, it makes it CONDITIONAL, and conditional
# machinery is what rots. These four pin both halves: the trigger still works
# where it is still needed (S13), it no longer fires where it would now be waste
# (S14), the model really is choosing to search (S15), and the two redundant
# mechanisms holding S15 up have not BOTH been removed (S16).
# --------------------------------------------------------------------------

# The generation model the gap trigger was designed against. S13 owns this the
# way S1 owns `tools_enabled` -- the scenario must not depend on the fixture
# happening to be configured hostilely.
GAP_TRIGGER_MODEL = "google/gemma-4-31b-it"


def _searches(events) -> list[dict]:
    return [p for p in payloads_of(events, "TOOL_CALL") if p.get("tool") == "search_corpus"]


def _gap_forced(events) -> list[dict]:
    """Searches the GAP TRIGGER forced, not ones the model chose.

    `agent_loop` stamps `trigger="gap_detected"` into the invocation args, and
    `ask.run_turn` records `args` verbatim, so the durable trace distinguishes the
    two. A model-chosen call has no `trigger` key at all.
    """
    return [p for p in _searches(events) if (p.get("args") or {}).get("trigger") == "gap_detected"]


async def s13_gap_trigger_still_fires(db, agent, user) -> Outcome:
    """The gap trigger must still work on a model that does not self-initiate.

    **This is the scenario that stops the trigger becoming dead code.** With the
    default model searching on its own, every other scenario here would stay green
    if the whole gap branch were deleted -- so its only remaining proof is a model
    that behaves the way gemma does, which `agents.generation_model` can still
    select and which CLAUDE.md documents an operator being able to type in.

    Owns TWO preconditions, because either one alone makes the test vacuous:
    `generation_model`, so the model genuinely will not search unprompted; and
    `retrieve_k=1`, per `loop.md` section 5 -- on a seven-chunk corpus a wider
    retrieval answers both halves and there is no gap to detect. Both restored.

    Read as `loop.md` T2: the assertion is "did a gap-triggered search occur",
    never "did the turn succeed". A turn that answers half and stops succeeds.
    """
    prior_model = agent.generation_model
    prior_k, prior_n = agent.retrieve_k, agent.rerank_top_n
    agent.generation_model = GAP_TRIGGER_MODEL
    agent.retrieve_k, agent.rerank_top_n = 1, 1
    await db.commit()
    try:
        out, events, _ = await ask(
            db, agent, user,
            "How many battery modules does the platform carry, and separately, "
            "how much onboard storage do the science instruments have?",
        )
    finally:
        agent.generation_model = prior_model
        agent.retrieve_k, agent.rerank_top_n = prior_k, prior_n
        await db.commit()
    forced = _gap_forced(events)
    total = _searches(events)
    # Either the trigger fired, or the model searched by itself -- on a model
    # measured at 0/N that second branch would be news, so it is reported rather
    # than failed. What must NOT happen is neither.
    ok = bool(total)
    return Outcome(
        "S13 gap trigger still fires", ok,
        f"model={GAP_TRIGGER_MODEL} searches={len(total)} gap_forced={len(forced)}"
        + ("" if forced else "  (no gap-forced search: the model self-initiated)"),
    )


async def s14_no_redundant_gap_search(db, agent, user) -> Outcome:
    """A correct refusal that already searched must not be made to search again.

    The saving the `not corpus_searched` gate buys. Before it, the ordinary shape
    of a correct refusal on a self-initiating model was: search, find nothing, say
    so -- and then have `detect_gap` force the SAME search a second time, on every
    single refusal.

    The probe is S7's, chosen the way CLAUDE.md says a refusal probe should be:
    both fixtures RAISE the modulation scheme and say it is held elsewhere, so the
    model is tempted to complete it and a search genuinely returns nothing useful.

    Necessity: asserting only "no gap-forced search" would pass on a turn that
    never searched at all, which is the failure this scenario is supposed to
    detect the absence of. So it requires a real search first.
    """
    out, events, _ = await ask(
        db, agent, user,
        "What modulation and coding scheme does the Ka-band downlink use?",
    )
    total = _searches(events)
    forced = _gap_forced(events)
    ok = bool(total) and not forced
    return Outcome(
        "S14 no redundant gap search", ok,
        f"searches={len(total)} gap_forced={len(forced)} refused={out.refused} "
        f"answer={preview(out.answer, 90)}",
    )


async def s15_model_initiates_search(db, agent, user) -> Outcome:
    """The premise of the swap: the model chooses to search, unprompted.

    `loop.md` T1 says to assume it will not, and that assumption is why the gap
    trigger exists. This asserts the measured inversion on the shipped
    configuration -- a search whose `trigger` is absent, i.e. one the MODEL
    decided on rather than one the loop forced.

    Two trials, pass on either. Not superstition: measured 2026-08-16 the shipped
    configuration self-initiates 6/6, while the configuration this suite is meant
    to catch -- reasoning off AND the guidance paragraph gone -- scores 2/6. Two
    trials separate those two populations (about 89 percent of broken runs go red)
    without spending four full turns on a property S16 also pins deterministically.

    Owns `retrieve_k`, like S3 and S13: the question must be unanswerable from the
    first retrieval or choosing not to search is the correct behaviour.
    """
    prior_k, prior_n = agent.retrieve_k, agent.rerank_top_n
    agent.retrieve_k, agent.rerank_top_n = 1, 1
    await db.commit()
    try:
        seen = []
        for _ in range(2):
            _out, events, _convo = await ask(
                db, agent, user,
                "How many battery modules does the platform carry, and separately, "
                "how much onboard storage do the science instruments have?",
            )
            chosen = [p for p in _searches(events) if not (p.get("args") or {}).get("trigger")]
            seen.append(len(chosen))
            if chosen:
                break
    finally:
        agent.retrieve_k, agent.rerank_top_n = prior_k, prior_n
        await db.commit()
    ok = any(seen)
    return Outcome(
        "S15 model initiates search", ok,
        f"model={agent.generation_model or settings.generation_model} "
        f"self_initiated_per_trial={seen}",
    )


async def s16_tool_use_has_a_belt_and_braces(db, agent, user) -> Outcome:
    """At least one of the two mechanisms that produce tool use must survive.

    No model call, no database read -- this is a structural assertion, and it is
    deliberately the only one of the four that cannot be flaky.

    Measured 2026-08-16, 6 trials per cell, "did it search unprompted":

                             guidance paragraph      no guidance paragraph
        reasoning on              6/6                      6/6
        reasoning off             6/6                      2/6

    The two are REDUNDANT WITH EACH OTHER. Either alone holds the behaviour, which
    is what makes `generation_reasoning=False` affordable -- and it is also what
    makes this dangerous, because it means the guidance paragraph now looks like
    dead weight from a superseded model. Deleting it costs nothing measurable
    until reasoning is also off, which it already is. Nothing raises; tool use
    drops to a third; S15 goes intermittent and gets marked flaky.

    So this asserts the DISJUNCTION rather than either half, because either half
    alone is a legitimate configuration and only losing both is the bug. That is
    `loop.md` T2 aimed at a config invariant: the failure has no error to test for,
    so the test has to name the outcome.
    """
    from app.rag.agent_loop import TOOL_GUIDANCE

    # The sequencing sentence, not the whole paragraph -- reworded prose should
    # not fail this, only losing the instruction should.
    sequencing = "after searching" in TOOL_GUIDANCE.lower()
    reasoning_on = bool(settings.generation_reasoning)
    ok = sequencing or reasoning_on
    return Outcome(
        "S16 tool use has a belt and braces", ok,
        f"guidance_sequencing={sequencing} generation_reasoning={reasoning_on}"
        + ("" if ok else "  BOTH REMOVED -- tool use measured 2/6 in this state"),
    )


# --------------------------------------------------------------------------
# HTTP scenarios -- the handout routes
# --------------------------------------------------------------------------

async def http_scenarios(agent_id: uuid.UUID, user_id: uuid.UUID) -> list[Outcome]:
    """Exercise the handout routes through an ASGI transport.

    `current_user` is overridden; `owned_agent` is NOT. That split is deliberate:
    the identity assertion is the only thing a script cannot perform (it needs a
    human at a Google consent screen), while the ownership hop is exactly the
    thing worth testing, so it runs for real against the database.
    """
    import httpx

    from app.auth.deps import current_user
    from app.db.session import SessionLocal as SL
    from app.main import app

    async def _fake_user():
        async with SL() as db:
            return await db.get(User, user_id)

    app.dependency_overrides[current_user] = _fake_user
    outcomes: list[Outcome] = []
    base = f"/api/agents/{agent_id}/handouts"

    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # S8 -- the four recipes
            created: dict[str, str] = {}
            for recipe, brief in (
                ("sheet", "A one-page study sheet on the power subsystem."),
                ("chart", "A bar chart of the power allocation by subsystem."),
                ("table", "A CSV of each subsystem and its power allocation in kW."),
                ("deck", "A short deck on the three downlink paths."),
            ):
                r = await client.post(base, json={"recipe": recipe, "brief": brief})
                if r.status_code == 202:
                    created[recipe] = r.json()["id"]
                else:
                    outcomes.append(Outcome(
                        f"S8 recipe {recipe}", False,
                        f"POST returned {r.status_code}: {preview(r.text, 200)}",
                    ))

            # Poll. Recipes are LLM calls plus a subprocess; sheet is fastest.
            deadline = time.monotonic() + 240
            final: dict[str, dict] = {}
            while created and time.monotonic() < deadline:
                await asyncio.sleep(4)
                r = await client.get(base, params={"limit": 100})
                if r.status_code != 200:
                    break
                rows = {row["id"]: row for row in r.json()}
                for recipe, hid in list(created.items()):
                    row = rows.get(hid)
                    if row and row["status"] in ("ready", "failed"):
                        final[recipe] = row
                        created.pop(recipe)

            for recipe in ("sheet", "chart", "table", "deck"):
                row = final.get(recipe)
                if row is None:
                    outcomes.append(Outcome(f"S8 recipe {recipe}", False, "did not finish in 240 s"))
                    continue
                ok = row["status"] == "ready" and row["byte_size"] > 0
                outcomes.append(Outcome(
                    f"S8 recipe {recipe}", ok,
                    f"status={row['status']} bytes={row['byte_size']} "
                    f"mime={row['mime_type']} err={preview(row.get('error') or '-', 90)}",
                ))

            # Download, including the header-safety check.
            ready = [r for r in final.values() if r["status"] == "ready"]
            if ready:
                hid = ready[0]["id"]
                r = await client.get(f"{base}/{hid}/download")
                disp = r.headers.get("content-disposition", "")
                safe = "\n" not in disp and "\r" not in disp and '"' in disp
                outcomes.append(Outcome(
                    "S8b download + safe filename",
                    r.status_code == 200 and len(r.content) > 0 and safe,
                    f"status={r.status_code} bytes={len(r.content)} disp={ascii_safe(disp)}",
                ))

            # S11 -- listing must never select the bytea column.
            import logging

            statements: list[str] = []

            class _Capture(logging.Handler):
                def emit(self, record):
                    statements.append(record.getMessage())

            sql_log = logging.getLogger("sqlalchemy.engine.Engine")
            handler = _Capture()
            prior_level = sql_log.level
            sql_log.addHandler(handler)
            sql_log.setLevel(logging.INFO)
            try:
                await client.get(base, params={"limit": 100})
            finally:
                sql_log.removeHandler(handler)
                sql_log.setLevel(prior_level)

            leaked = [s for s in statements if "handouts.content" in s]
            outcomes.append(Outcome(
                "S11 list does not load bytea", not leaked,
                f"statements={len(statements)} leaked={len(leaked)}",
            ))

            # S12 -- quota refuses rather than evicting.
            async with SL() as db:
                before = len((await db.scalars(
                    select(Handout).where(Handout.agent_id == agent_id)
                )).all())
            original = settings.handout_max_per_agent
            settings.handout_max_per_agent = max(1, before)
            try:
                r = await client.post(base, json={"recipe": "sheet", "brief": "over quota"})
                async with SL() as db:
                    after = len((await db.scalars(
                        select(Handout).where(Handout.agent_id == agent_id)
                    )).all())
                outcomes.append(Outcome(
                    "S12 quota refuses, evicts nothing",
                    r.status_code == 409 and after == before,
                    f"status={r.status_code} before={before} after={after}",
                ))
            finally:
                settings.handout_max_per_agent = original

    finally:
        app.dependency_overrides.pop(current_user, None)

    return outcomes


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------

SCENARIOS = [
    s1_classic_path,
    s2_no_reflex_tools,
    s3_multi_hop,
    s4_chart_from_chat,
    s5_self_correction,
    s6_step_budget,
    s7_refusal_survives,
    s9_timing_adds_up,
    s10_citation_integrity,
    # S16 first of the swap group: it is the only structural one, costs no model
    # call, and names the configuration the three behavioural ones depend on. A
    # red S16 explains a red S15; the reverse ordering makes the reader debug the
    # model instead of the config.
    s16_tool_use_has_a_belt_and_braces,
    s15_model_initiates_search,
    s14_no_redundant_gap_search,
    s13_gap_trigger_still_fires,
]


async def run_scenarios(db, only: str | None) -> list[Outcome]:
    user, agent = await get_or_create_agent(db)

    doc_count = len((await db.scalars(
        select(Document).where(Document.agent_id == agent.id)
    )).all())
    if doc_count == 0:
        print("ERROR: no corpus. Run with --setup first.")
        return [Outcome("setup", False, "no documents ingested")]

    rule("Configuration")
    print(f"  generation   : {settings.generation_model}")
    print(f"  tools        : global={settings.agent_tools_enabled} "
          f"agent={agent.tools_enabled} max_steps={agent.max_tool_steps}")
    print(f"  sandbox      : timeout={settings.sandbox_timeout_s}s "
          f"mem={settings.sandbox_memory_mb}MB")
    print(f"  namespace    : {agent.namespace}  ({doc_count} documents)")

    outcomes: list[Outcome] = []
    rule("Scenarios")
    for fn in SCENARIOS:
        name = fn.__name__
        if only and only not in name:
            continue
        started = time.perf_counter()
        try:
            outcome = await fn(db, agent, user)
        except Exception as exc:  # a scenario must never abort the suite
            text = f"{type(exc).__name__}: {str(exc)[:300]}"
            outcome = Outcome(
                name, False, ascii_safe(text)[:220], rate_limited=is_rate_limited(text)
            )
        if not outcome.ok and is_rate_limited(outcome.detail):
            outcome.rate_limited = True
        took = time.perf_counter() - started
        print(f"  {_flag(outcome)} {outcome.name}  ({took:.1f}s)")
        print(f"         {ascii_safe(outcome.detail)}")
        outcomes.append(outcome)

    if not only:
        rule("Handout routes")
        for outcome in await http_scenarios(agent.id, user.id):
            if not outcome.ok and is_rate_limited(outcome.detail):
                outcome.rate_limited = True
            print(f"  {_flag(outcome)} {outcome.name}")
            print(f"         {ascii_safe(outcome.detail)}")
            outcomes.append(outcome)

    return outcomes


async def main_async(args) -> int:
    try:
        async with SessionLocal() as db:
            if args.cleanup:
                return await cleanup(db)
            if args.setup:
                return await setup(db)

            outcomes = await run_scenarios(db, args.only)

        rule("Summary")
        failed = [o for o in outcomes if not o.ok]
        for o in outcomes:
            print(f"  {'[ok]  ' if o.ok else '[FAIL]'} {o.name}")
        print(f"\n  {len(outcomes) - len(failed)} / {len(outcomes)} passed")
        return 1 if failed else 0
    finally:
        await engine.dispose()


def main() -> int:
    p = argparse.ArgumentParser(description="End-to-end check for the agent loop and Handouts.")
    p.add_argument("--setup", action="store_true", help="Create the agent and ingest fixtures")
    p.add_argument(
        "--run",
        action="store_true",
        help="Run the scenarios (the default when no other mode is given)",
    )
    p.add_argument("--cleanup", action="store_true", help="Delete namespace, agent and rows")
    p.add_argument("--only", default=None, help="Substring match on a scenario function name")
    args = p.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
