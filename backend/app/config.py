"""Application settings, loaded from environment.

Reads the repo-root .env in local development. On Render, the same names are
set as service environment variables and no .env file exists.
"""

from __future__ import annotations

import ssl
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Database ---
    database_url: str = ""

    # --- Google OAuth ---
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    oauth_redirect_uri: str = "http://localhost:8000/api/auth/google/callback"
    session_secret_key: str = "dev-only-change-me"

    # --- Frontend ---
    frontend_url: str = "http://localhost:5173"

    # --- Models / vector store (used from the RAG layer) ---
    gemini_api_key: str = ""
    pinecone_api_key: str = ""
    pinecone_index_name: str = "agentic-rag-ntu"
    cohere_api_key: str = ""

    generation_model: str = "gemma-4-31b-it"
    decision_model: str = "gemini-flash-latest"
    embedding_model: str = "models/gemini-embedding-2"
    embedding_dimension: int = 768

    @property
    def async_database_url(self) -> str:
        """SQLAlchemy async URL.

        Render hands out `postgresql://...`, which SQLAlchemy maps to psycopg2.
        We use asyncpg, so the driver has to be named explicitly. asyncpg also
        rejects libpq-style query params such as `sslmode`, so they are stripped
        here -- asyncpg negotiates TLS on its own.
        """
        url = self.database_url
        if not url:
            return ""

        parts = urlsplit(url)
        scheme = parts.scheme
        if scheme in ("postgres", "postgresql"):
            scheme = "postgresql+asyncpg"

        drop = {"sslmode", "channel_binding", "gssencmode", "target_session_attrs"}
        query = "&".join(
            kv
            for kv in parts.query.split("&")
            if kv and kv.split("=", 1)[0] not in drop
        )
        return urlunsplit((scheme, parts.netloc, parts.path, query, parts.fragment))

    @property
    def db_connect_args(self) -> dict:
        """asyncpg connect args.

        Two separate Render quirks meet here.

        1. Render Postgres refuses non-TLS connections with
           `InvalidAuthorizationSpecificationError: SSL/TLS required`. asyncpg
           does not read libpq's `sslmode`, so TLS must be requested here --
           stripping sslmode from the URL (see async_database_url) is only half
           the fix and on its own produces exactly that error.

        2. The INTERNAL endpoint presents a self-signed certificate, so a
           verifying context fails with
           `SSLCertVerificationError: self-signed certificate`. The EXTERNAL
           endpoint has a normal public certificate and verifies fine.

        Internal hostnames have no dots (`dpg-xxx-a`); external ones are FQDNs
        (`dpg-xxx-a.singapore-postgres.render.com`). We verify when we can and
        fall back to encrypted-but-unverified on the private network, where the
        traffic never leaves Render.
        """
        if not self.database_url:
            return {}

        host = urlsplit(self.database_url).hostname or ""
        ctx = ssl.create_default_context()
        if "." not in host:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return {"ssl": ctx}

    @property
    def cors_origins(self) -> list[str]:
        origins = {self.frontend_url.rstrip("/")}
        origins.add("http://localhost:5173")
        return sorted(o for o in origins if o)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
