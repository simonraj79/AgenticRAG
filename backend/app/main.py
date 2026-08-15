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
app.include_router(eval_router)


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
        "embedding_model": settings.embedding_model,
        "embedding_dimension": settings.embedding_dimension,
        "pinecone_index": settings.pinecone_index_name,
        "frontend_url": settings.frontend_url,
        "oauth_redirect_uri": settings.oauth_redirect_uri,
        "secrets_present": {
            # Two model providers, not one, and the split is not arbitrary:
            # `openrouter` serves every chat model, `gemini` is now the
            # EMBEDDING key. A deployment missing the second one fails at
            # retrieval, not at generation -- which looks nothing like a missing
            # model key, so both are reported separately.
            "openrouter": bool(settings.openrouter_api_key),
            "gemini": bool(settings.gemini_api_key),
            "pinecone": bool(settings.pinecone_api_key),
            "cohere": bool(settings.cohere_api_key),
            "google_oauth": bool(settings.google_oauth_client_secret),
            "database": bool(settings.database_url),
        },
    }
