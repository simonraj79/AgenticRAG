"""End-to-end check of the RAG vertical slice.

Ingests one file into one agent and asks one question, exercising embedding ->
upsert -> namespace scoping -> retrieval -> rerank -> generation in a single
pass. The point is to surface the risky unknowns (is 768d good enough on this
corpus, are 800/120 chunks sensible, does the retrieval actually ground the
answer) before any UI is built on top of them.

Idempotent, like the provisioning scripts: re-running reuses the same seed user
and agent, and ingestion is skipped when the file's content hash is unchanged.

Usage:
    python scripts/slice_check.py
    python scripts/slice_check.py --file "D:/path/to/3.2-lesson-gist.md"
    python scripts/slice_check.py --question "What is gradient descent?"
    python scripts/slice_check.py --no-rerank      # Stage 1 retrieval only
    python scripts/slice_check.py --cleanup        # drop the namespace and rows

Requires the full credential set in .env, and this machine's public IP on the
Render Postgres allow-list.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import delete, select  # noqa: E402

from app.config import settings  # noqa: E402
from app.db.models import Agent, Chunk, Document, IngestionRun, User  # noqa: E402
from app.db.session import SessionLocal, engine  # noqa: E402
from app.rag.ingest import ingest_file  # noqa: E402
from app.rag.pipeline import answer_question  # noqa: E402
from app.rag.retriever import get_vector_store, search_with_scores  # noqa: E402

# A stable identity so re-runs reuse the same rows rather than accumulating them.
SEED_SUB = "slice-check-local"
AGENT_NAME = "Slice Check"

DEFAULT_FILE = Path("D:/03 Module Machine Learning/Corpus/3.1-corpus/3.1-lesson-gist.md")
DEFAULT_QUESTION = "What is this lesson about, and what are its main points?"


def rule(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def ascii_safe(text: str) -> str:
    """Force text to ASCII for the Windows console.

    CLAUDE.md's "ASCII in print()" rule is usually read as being about string
    literals, but the corpus is the bigger hazard: these lesson notes are full of
    arrows and em-dashes, and printing a retrieved chunk raises
    UnicodeEncodeError under the cp1252 codepage -- a crash sourced entirely in
    the data, several layers away from anything this script wrote.
    """
    return text.encode("ascii", "replace").decode("ascii")


def preview(text: str, width: int) -> str:
    """One-line, ASCII-safe excerpt of a chunk."""
    return ascii_safe(" ".join(text.split())[:width])


async def get_or_create_agent(db) -> tuple[User, Agent]:
    user = await db.scalar(select(User).where(User.google_sub == SEED_SUB))
    if user is None:
        user = User(
            id=uuid.uuid4(),
            google_sub=SEED_SUB,
            email="slice-check@localhost",
            name="Slice Check",
            role="user",
        )
        db.add(user)
        await db.flush()

    agent = await db.scalar(
        select(Agent).where(Agent.owner_user_id == user.id, Agent.name == AGENT_NAME)
    )
    if agent is None:
        # Defaults come from the model columns: 800/120 markdown chunks, top-20
        # retrieve, rerank to 3, rewrite below 0.5. PRD section 10.
        agent = Agent(
            id=uuid.uuid4(),
            owner_user_id=user.id,
            name=AGENT_NAME,
            description="Throwaway agent for the vertical-slice check.",
        )
        db.add(agent)
        await db.flush()

    await db.commit()
    return user, agent


async def cleanup(db) -> int:
    user = await db.scalar(select(User).where(User.google_sub == SEED_SUB))
    if user is None:
        print("Nothing to clean up.")
        return 0

    agents = (await db.scalars(select(Agent).where(Agent.owner_user_id == user.id))).all()
    for agent in agents:
        try:
            get_vector_store(agent)._index.delete(delete_all=True, namespace=agent.namespace)
            print(f"  deleted namespace {agent.namespace}")
        except Exception as exc:  # namespace may not exist yet
            print(f"  namespace {agent.namespace}: {type(exc).__name__} (already absent?)")

        docs = (await db.scalars(select(Document).where(Document.agent_id == agent.id))).all()
        for doc in docs:
            await db.execute(delete(Chunk).where(Chunk.document_id == doc.id))
            await db.execute(delete(IngestionRun).where(IngestionRun.document_id == doc.id))
        await db.execute(delete(Document).where(Document.agent_id == agent.id))
        await db.delete(agent)

    await db.delete(user)
    await db.commit()
    print("  deleted seed user, agent, documents, chunks, ingestion runs")
    return 0


async def run(args) -> int:
    async with SessionLocal() as db:
        if args.cleanup:
            return await cleanup(db)

        path = Path(args.file)
        if not path.exists():
            print(f"ERROR: test file not found: {path}")
            return 1

        rule("0. Configuration")
        print(f"  embedding      : {settings.embedding_model} @ {settings.embedding_dimension}d")
        print(f"  generation     : {settings.generation_model}")
        print(f"  decision       : {settings.decision_model} ({settings.structured_output_method})")
        print(f"  reranker       : {settings.rerank_model}")
        print(f"  index          : {settings.pinecone_index_name}")
        print(f"  sampling       : temp={settings.generation_temperature} "
              f"top_p={settings.generation_top_p} top_k={settings.generation_top_k}")

        user, agent = await get_or_create_agent(db)
        rule("1. Agent")
        print(f"  agent id       : {agent.id}")
        print(f"  namespace      : {agent.namespace}")
        print(f"  chunking       : {agent.chunk_size} tokens / {agent.chunk_overlap} overlap "
              f"({agent.splitter})")
        print(f"  retrieval      : k={agent.retrieve_k}, rerank={agent.rerank_enabled} "
              f"-> top {agent.rerank_top_n}, threshold={agent.score_threshold}")

        rule("2. Ingest")
        print(f"  file           : {path.name}  ({path.stat().st_size:,} bytes)")
        run_row = await ingest_file(db, agent, path, uploaded_by_user_id=user.id)
        print(f"  status         : {run_row.status}")
        print(f"  chunks         : {run_row.chunk_count}")
        print(f"  recorded model : {run_row.embedding_model} @ {run_row.embedding_dimension}d")

        chunks = (
            await db.scalars(
                select(Chunk)
                .join(Document, Chunk.document_id == Document.id)
                .where(Document.agent_id == agent.id)
                .order_by(Chunk.chunk_index)
            )
        ).all()
        if chunks:
            counts = [c.token_count or 0 for c in chunks]
            print(f"  token counts   : min={min(counts)} max={max(counts)} "
                  f"mean={sum(counts) // len(counts)}")

        rule("3. Retrieval (scored, pre-rerank)")
        print(f"  question       : {args.question}\n")
        scored = search_with_scores(agent, args.question, k=min(5, agent.retrieve_k))
        if not scored:
            print("  NO RESULTS -- the namespace is empty or the query embedding failed.")
            return 1
        for i, (doc, score) in enumerate(scored, 1):
            flag = "" if score >= agent.score_threshold else "   <- BELOW THRESHOLD"
            print(f"  {i}. score={score:.4f}{flag}")
            print(f"     {preview(doc.page_content, 110)}...")
        top = scored[0][1]
        print(f"\n  top score      : {top:.4f} vs threshold {agent.score_threshold}")
        print(f"  Stage 2 would  : {'ANSWER directly' if top >= agent.score_threshold else 'REWRITE the query'}")

        rule("4. Answer")
        result = await answer_question(agent, args.question, rerank=args.rerank)
        print(f"  model          : {result.model}")
        print(f"  reranked       : {result.reranked} ({len(result.documents)} chunks in prompt)")
        print(f"  latency        : {result.latency_ms} ms\n")
        print(ascii_safe(result.answer))

        rule("5. Chunks used in the prompt")
        for i, doc in enumerate(result.documents, 1):
            meta = doc.metadata
            rel = meta.get("relevance_score")
            extra = f"  rerank={rel:.4f}" if isinstance(rel, (int, float)) else ""
            print(f"  {i}. {meta.get('filename', '?')} #{meta.get('chunk_index', '?')}{extra}")
            print(f"     {preview(doc.page_content, 160)}...")

        return 0


async def main_async(args) -> int:
    """Run, then dispose the pool inside the SAME event loop.

    Disposing from a second `asyncio.run()` looks harmless and is not: the pool
    still holds asyncpg connections bound to the first loop, and closing them
    from a loop that is already gone surfaces as `RuntimeError: Event loop is
    closed` stacked on top of whatever the real error was.
    """
    try:
        return await run(args)
    finally:
        await engine.dispose()


def main() -> int:
    p = argparse.ArgumentParser(description="Run the RAG vertical slice end to end.")
    p.add_argument("--file", default=str(DEFAULT_FILE), help="File to ingest")
    p.add_argument("--question", default=DEFAULT_QUESTION, help="Question to ask")
    p.add_argument("--no-rerank", dest="rerank", action="store_false", default=None,
                   help="Stage 1 retrieval only, skipping the Cohere rerank")
    p.add_argument("--cleanup", action="store_true", help="Delete the namespace and seed rows")
    args = p.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
