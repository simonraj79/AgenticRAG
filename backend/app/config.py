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

    # Two settings that exist only to gate POST /api/auth/dev-login, the
    # authentication bypass a browser test needs because a real Google sign-in
    # ends at a human-only password prompt. Full reasoning in app/auth/routes.py.
    #
    # `environment` is informational everywhere else; only the exact string
    # "development" unlocks the bypass, so an unset or misspelled value fails
    # closed. Set ENVIRONMENT=production on Render.
    environment: str = "development"

    # Off unless a local .env turns it on deliberately. This repository is
    # public and deploys straight to production, so the flag defaults to the
    # safe value and never inherits one. It is the weakest of the three gates on
    # its own -- the loopback check is what holds if this one is set by mistake.
    dev_auth_enabled: bool = False

    # --- Frontend ---
    frontend_url: str = "http://localhost:5173"

    # --- Uploads and ingest ---
    # `app/api/documents.py` argued against exactly this setting, and it was
    # right: "a limit that can be raised by an environment variable will be
    # raised by an environment variable, and the reason it is low is a property
    # of how ingest works, not of where the service is deployed." The property it
    # meant is that ingest ran INLINE -- the request held a worker, a database
    # connection and the whole file in memory while it split, embedded and
    # upserted. Under that design a large file did not fail cleanly, it timed
    # out, and a mysterious timeout is the failure a size cap exists to prevent.
    # The 10 MB constant was not sizing the workload (the whole workshop corpus
    # is ~1.4 MB); it was protecting the request.
    #
    # So the cap and the blocking had to move together, and neither alone.
    # Raising the limit while ingest still ran in the request would have made the
    # failure worse rather than merely larger. `ingest_in_background` is the half
    # that moved ingest onto a background job with its own session
    # (`app/rag/jobs.py`), and only once that landed did this number become a
    # property of the deployment -- memory, embedding spend, how long a user will
    # watch a status badge -- rather than the thing holding the request timeout
    # up. Set `ingest_in_background=false` and this number is dangerous again;
    # they are one decision with two names, not two independent knobs.
    max_upload_mb: int = 50
    ingest_in_background: bool = True

    # --- Models / vector store (used from the RAG layer) ---
    gemini_api_key: str = ""
    pinecone_api_key: str = ""
    pinecone_index_name: str = "agentic-rag-ntu"
    cohere_api_key: str = ""

    generation_model: str = "gemma-4-31b-it"

    # Stage 2's rewrite decision must come back as a typed object. The PRD hedged
    # this to Gemini Flash because structured output is undocumented for Gemma,
    # and said to collapse to one model if it turned out to work. Measured
    # 2026-08-15, 5 trials per configuration:
    #
    #   gemma  raw google-genai response_schema  T=1.0   4/5   ~2.2s
    #   gemma  raw google-genai response_schema  T=0.2   5/5   ~2.2s
    #   gemma  langchain function_calling        T=1.0   5/5   ~3.5s
    #   gemma  langchain json_mode               T=1.0   5/5   ~2.6s
    #   flash  langchain function_calling        T=1.0   5/5   ~2.3s
    #
    # Gemma emits schema-correct JSON but occasionally wraps it in a markdown
    # fence at its recommended temperature. `response.parsed` is strict and
    # returns None on that; LangChain's parser strips the fence, which is the
    # entire difference between the failing row and the passing ones -- not a
    # different API capability. `function_calling` sidesteps the text channel
    # altogether, so it cannot be broken by a stray fence, and it is the path
    # Gemma's model card actually documents.
    #
    # So: collapsed to one model. Flash remains one env var away.
    decision_model: str = "gemma-4-31b-it"
    structured_output_method: str = "function_calling"
    embedding_model: str = "models/gemini-embedding-2"
    embedding_dimension: int = 768

    # PRD section 2 says "Cohere rerank-v3". That is a family, not a model id --
    # the API rejects it. The live ids are rerank-english-v3.0,
    # rerank-multilingual-v3.0, rerank-v3.5, rerank-v4.0-fast and
    # rerank-v4.0-pro; v3.5 is the multilingual v3 successor and the closest
    # real id to what the workshop specifies.
    rerank_model: str = "rerank-v3.5"

    # Gemma 4's model card gives one "standardized sampling configuration across
    # all use cases": temperature 1.0, top_p 0.95, top_k 64. That reads oddly for
    # grounded RAG, where the instinct is temperature 0 -- but the instinct is
    # borrowed from models calibrated differently, and Gemma degenerates into
    # repetition when sampling is squeezed far below what it was tuned for. Start
    # at the documented values and change them only against a measurement.
    generation_temperature: float = 1.0
    generation_top_p: float = 0.95
    generation_top_k: int = 64
    generation_max_tokens: int = 2048

    # --- Evaluation (Stage 3) ---
    # The model that GRADES the answers. PRD 2.1 specifies Gemini Flash Lite for
    # this and the reason is worth stating plainly: with the default below, the
    # judge is the same model as `generation_model`, so the run is
    # self-assessment. `faithfulness` asks "is this answer supported by these
    # contexts?" of the very model that wrote the answer, and LLM-as-judge setups
    # are known to score their own output generously (self-preference bias). The
    # number is not meaningless -- it is measured against the retrieved contexts,
    # not against taste -- but it is not independent either.
    #
    # It stays `gemma-4-31b-it` because that is what was asked for, and three
    # things keep the choice honest rather than hidden: switching is one env var,
    # `eval_runs` records `judge_model` and `generation_model` separately per run
    # (never read back from `agents.generation_model`, which can change after a
    # run), and the scorecard says so when the two are equal.
    ragas_judge_model: str = "gemma-4-31b-it"

    # How many judge calls may be in flight at once, within one question.
    #
    # This exists because of the Gemini free tier. Each scored question costs
    # FOUR judged metrics, and three of them are themselves multi-call
    # (faithfulness generates statements then verdicts them; answer relevance
    # generates `strictness` questions from the answer). A ten-question run is
    # therefore tens of judge requests on top of ten generations, and a 429 is
    # the most likely way it fails -- as a burst, not as a total. Two is slow and
    # boring on purpose: the run is a background job, so the cost of being
    # conservative is a progress bar that moves less quickly, while the cost of
    # being greedy is a scorecard full of nulls that looks like a broken judge.
    ragas_max_concurrency: int = 2

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
