"""FastAPI application entrypoint."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from app.api.agents import router as agents_router
from app.api.ask import router as ask_router
from app.api.conversations import router as conversations_router
from app.api.documents import router as documents_router
from app.api.eval import router as eval_router
from app.api.handouts import router as handouts_router
from app.api.stream import router as stream_router
from app.auth.routes import router as auth_router
from app.config import settings
from app.db.session import engine

log = logging.getLogger("uvicorn.error")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.async_database_url:
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            log.info("Database reachable.")
        except Exception as exc:  # noqa: BLE001 - startup diagnostics only
            log.warning("Database unreachable at startup: %s", exc)
    else:
        log.warning("DATABASE_URL is not set.")
    yield
    await engine.dispose()


app = FastAPI(
    title="Agentic RAG API",
    version="0.1.0",
    lifespan=lifespan,
)

# Signs the cookie Authlib uses to carry OAuth state/nonce/PKCE verifier.
# same_site must be "none" because the SPA is served from a different origin;
# Starlette's default of "lax" survives the redirect back from Google but
# breaks the first XHR from React that expects the cookie.
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret_key,
    same_site="none",
    https_only=True,
)

# Explicit allowlist, never "*" - browsers reject wildcard origins outright
# when allow_credentials is true.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# DO NOT ADD GZipMiddleware HERE. `app/api/stream.py` serves SSE, and a
# compressing middleware BUFFERS in order to compress - which turns a token
# stream into one late blob. That failure renders perfectly, throws nothing, and
# passes any check shaped like "did the connection open?", so it would be found
# by a user reporting that streaming "feels the same" rather than by a test. The
# stream sets `Cache-Control: no-transform` to say the same thing to
# intermediaries it does not control; this comment is the half that is in reach.
# If compression is ever wanted, exclude `text/event-stream` explicitly.


# Auth first: everything below it depends on `current_user`, and mounting it
# last would still work but reads as though the API were the primary surface.
# `agents` before `documents`/`ask` only for legibility - the paths are
# distinct segments, so FastAPI's registration order does not disambiguate
# anything here.
app.include_router(auth_router)
app.include_router(agents_router)
app.include_router(documents_router)
app.include_router(ask_router)
app.include_router(conversations_router)
# After both, because it imports from both -- `run_turn` from `ask` and the
# conversation ownership chain from `conversations`. The paths are distinct
# segments (`/ask` vs `/ask/stream`), so registration order disambiguates
# nothing; this is legibility.
app.include_router(stream_router)
app.include_router(eval_router)
app.include_router(handouts_router)


@app.get("/api/health")
async def health() -> dict:
    """Render health check target. Must stay unauthenticated."""
    db_ok = False
    if settings.async_database_url:
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            db_ok = True
        except Exception:  # noqa: BLE001
            db_ok = False

    return {
        "status": "ok",
        "version": app.version,
        "database": "ok" if db_ok else "unavailable",
    }


@app.get("/api/config")
async def config() -> dict:
    """Non-sensitive configuration, for debugging a deployment.

    Deliberately reports only whether secrets are PRESENT, never their values.
    """
    return {
        "generation_model": settings.generation_model,
        "decision_model": settings.decision_model,
        "judge_model": settings.ragas_judge_model,
        "golden_set_model": settings.golden_set_model,
        # The SPACE. Unchanged by the 2026-08-16 route move, on purpose -- it is
        # what `agents.embedding_model` is compared against.
        "embedding_model": settings.embedding_model,
        "embedding_dimension": settings.embedding_dimension,
        # The ROAD, and the reason this key exists at all: a wrong route is the
        # one fault in this system that returns confident nonsense instead of an
        # error, so "which gateway embedded this?" must be answerable from
        # outside the process.
        "embedding_route": settings.embedding_route,
        # The RERANKER. The one model in the turn that is neither an OpenRouter
        # chat slug nor part of the Pinecone index, so nothing else on this
        # payload implies it -- a reader of a live deployment could not name it
        # at all until this key existed. It is worth naming: reranking is ~830 ms
        # of every turn, and `config.py` records that PRD section 2's
        # "rerank-v3" is a FAMILY the Cohere API rejects, so the live id
        # (rerank-v3.5) is a choice this deployment made rather than a spec it
        # inherited. Cohere ids also differ by generation and price.
        "rerank_model": settings.rerank_model,
        "rewrite_every_turn": settings.rewrite_every_turn,
        "eval_rewrite_questions": settings.eval_rewrite_questions,
        "pinecone_index": settings.pinecone_index_name,
        "frontend_url": settings.frontend_url,
        "oauth_redirect_uri": settings.oauth_redirect_uri,
        "secrets_present": {
            # Two model providers, not one, and the split is not arbitrary.
            # `openrouter` serves every chat model AND, since 2026-08-16, the
            # embeddings -- so it is now the key whose absence breaks everything.
            #
            # `gemini` is the embedding key for the `google` ROLLBACK route only.
            # Read it together with `embedding_route` above: absent while the
            # route is "openrouter" is fine, absent while the route is "google"
            # fails at RETRIEVAL rather than at generation, which looks nothing
            # like a missing model key.
            "openrouter": bool(settings.openrouter_api_key),
            "gemini": bool(settings.gemini_api_key),
            "pinecone": bool(settings.pinecone_api_key),
            "cohere": bool(settings.cohere_api_key),
            "google_oauth": bool(settings.google_oauth_client_secret),
            "database": bool(settings.database_url),
            # Object storage, and the reason it is reported here is the same one
            # `embedding_route` is reported above: the interesting question is
            # not "is a key set" but "is it set on the road actually selected".
            #
            # Under `storage_route="r2"` a blank credential cannot reach this
            # endpoint at all -- the settings validator refuses to construct --
            # so a `false` here means the route is "postgres" and nothing is
            # broken. Under "postgres" it is purely informational. Either way it
            # answers the question somebody debugging a 503 from the download
            # route will ask first.
            "r2": bool(settings.r2_access_key_id and settings.r2_bucket),
        },
        "storage_route": settings.storage_route,
    }
