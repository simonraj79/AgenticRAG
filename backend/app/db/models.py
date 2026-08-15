"""SQLAlchemy models for the schema in PRD.md section 4."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


# --------------------------------------------------------------------------
# 4.1 Identity
# --------------------------------------------------------------------------

class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _pk()
    created_at: Mapped[datetime] = _created_at()

    # Google's stable subject id. NOT email - Google reassigns emails within a
    # Workspace domain but never reuses `sub`.
    google_sub: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    name: Mapped[str | None] = mapped_column(String(255))
    avatar_url: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(32), default="user", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    sessions: Mapped[list["Session"]] = relationship(back_populates="user")
    agents: Mapped[list["Agent"]] = relationship(back_populates="owner")


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = _pk()
    created_at: Mapped[datetime] = _created_at()

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # A hash, never the token itself - a DB read must not yield a credential.
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    user_agent: Mapped[str | None] = mapped_column(Text)
    # Optional and privacy-sensitive; see PRD section 7.
    ip_address: Mapped[str | None] = mapped_column(String(64))

    user: Mapped[User] = relationship(back_populates="sessions")

    __table_args__ = (Index("ix_sessions_user_expires", "user_id", "expires_at"),)


# --------------------------------------------------------------------------
# 4.2 Agents
# --------------------------------------------------------------------------

class AgentTemplate(Base):
    """A named preset of RAG parameters.

    'Create from a template' and 'create your own' are the same code path -
    a template just supplies the starting values.
    """

    __tablename__ = "agent_templates"

    id: Mapped[uuid.UUID] = _pk()
    created_at: Mapped[datetime] = _created_at()

    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    # Indexing parameters
    chunk_size: Mapped[int] = mapped_column(Integer, default=800, nullable=False)
    chunk_overlap: Mapped[int] = mapped_column(Integer, default=120, nullable=False)
    splitter: Mapped[str] = mapped_column(String(32), default="markdown", nullable=False)

    # Retrieval parameters
    retrieve_k: Mapped[int] = mapped_column(Integer, default=20, nullable=False)
    rerank_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    rerank_top_n: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    score_threshold: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    max_rewrites: Mapped[int] = mapped_column(Integer, default=2, nullable=False)

    system_prompt: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    agents: Mapped[list["Agent"]] = relationship(back_populates="template")


class Agent(Base):
    """A user-created RAG agent: one corpus, one config, one namespace."""

    __tablename__ = "agents"

    id: Mapped[uuid.UUID] = _pk()
    created_at: Mapped[datetime] = _created_at()

    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_templates.id", ondelete="SET NULL")
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    # Effective config, copied from the template at creation then editable.
    # Stored per-agent rather than read through the template so that editing a
    # template never silently re-tunes agents someone already built.
    chunk_size: Mapped[int] = mapped_column(Integer, default=800, nullable=False)
    chunk_overlap: Mapped[int] = mapped_column(Integer, default=120, nullable=False)
    splitter: Mapped[str] = mapped_column(String(32), default="markdown", nullable=False)
    retrieve_k: Mapped[int] = mapped_column(Integer, default=20, nullable=False)
    rerank_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    rerank_top_n: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    score_threshold: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    max_rewrites: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    system_prompt: Mapped[str | None] = mapped_column(Text)
    generation_model: Mapped[str | None] = mapped_column(String(128))

    # Reserved for later sharing models; only "private" is honoured today.
    visibility: Mapped[str] = mapped_column(String(16), default="private", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="empty", nullable=False)

    # The embedding model this agent's vectors were built with. A mismatch
    # against the current setting means the namespace must be re-ingested.
    embedding_model: Mapped[str | None] = mapped_column(String(128))

    owner: Mapped[User] = relationship(back_populates="agents")
    template: Mapped[AgentTemplate | None] = relationship(back_populates="agents")
    documents: Mapped[list["Document"]] = relationship(back_populates="agent")

    __table_args__ = (UniqueConstraint("owner_user_id", "name"),)

    @property
    def namespace(self) -> str:
        """Pinecone namespace - one per AGENT, not per user.

        A user owns several agents and each must retrieve only its own corpus,
        so the namespace cannot be keyed on the user. Always derived server-side
        from the session-authorised agent, never accepted from the client.
        """
        return f"agent_{self.id}"


# --------------------------------------------------------------------------
# 4.3 Corpus
# --------------------------------------------------------------------------

class Document(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = _pk()
    created_at: Mapped[datetime] = _created_at()

    # Scoping key: documents belong to an AGENT. The uploader is kept
    # separately for audit, not for access control.
    agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), index=True, nullable=False
    )
    uploaded_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(128))
    byte_size: Mapped[int | None] = mapped_column(BigInteger)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)

    agent: Mapped[Agent] = relationship(back_populates="documents")
    chunks: Mapped[list["Chunk"]] = relationship(back_populates="document")


class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[uuid.UUID] = _pk()
    created_at: Mapped[datetime] = _created_at()

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True, nullable=False
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    # Postgres is the source of truth for chunk text; Pinecone metadata has a
    # per-record size limit, and keeping text here allows re-embedding without
    # re-parsing the original file.
    text: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int | None] = mapped_column(Integer)
    pinecone_id: Mapped[str | None] = mapped_column(String(128), index=True)
    # Optional pointer to a rendered image of this chunk's source (e.g. the
    # slide PNG behind a slides.md section), for citation display. Requires
    # object storage to actually serve - see PRD open items.
    asset_uri: Mapped[str | None] = mapped_column(Text)

    document: Mapped[Document] = relationship(back_populates="chunks")

    __table_args__ = (UniqueConstraint("document_id", "chunk_index"),)


class IngestionRun(Base):
    __tablename__ = "ingestion_runs"

    id: Mapped[uuid.UUID] = _pk()
    created_at: Mapped[datetime] = _created_at()

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    # These two make an embedding-model mismatch detectable rather than a
    # silent source of nonsense. See PRD section 7.
    embedding_model: Mapped[str] = mapped_column(String(128), nullable=False)
    embedding_dimension: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_size: Mapped[int | None] = mapped_column(Integer)
    chunk_overlap: Mapped[int | None] = mapped_column(Integer)
    chunk_count: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)


# --------------------------------------------------------------------------
# 4.3 Query & trace
# --------------------------------------------------------------------------

class Query(Base):
    __tablename__ = "queries"

    id: Mapped[uuid.UUID] = _pk()
    created_at: Mapped[datetime] = _created_at()

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # Which agent answered. Needed to scope trace and eval history per agent.
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sessions.id", ondelete="SET NULL")
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str | None] = mapped_column(Text)
    model_used: Mapped[str | None] = mapped_column(String(128))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    # A correct refusal is a success case, not a failure.
    refused: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    trace_events: Mapped[list["TraceEvent"]] = relationship(back_populates="query")

    __table_args__ = (Index("ix_queries_user_created", "user_id", "created_at"),)


class TraceEvent(Base):
    """One row per agent decision. This table IS the Trace view."""

    __tablename__ = "trace_events"

    id: Mapped[uuid.UUID] = _pk()
    created_at: Mapped[datetime] = _created_at()

    query_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("queries.id", ondelete="CASCADE"), nullable=False
    )
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    # RETRIEVE / SCORE_CHECK / REWRITE / RERANK / GENERATE / REFUSE
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # Shape varies by event type; JSONB keeps it queryable anyway.
    payload: Mapped[dict | None] = mapped_column(JSONB)
    score: Mapped[float | None] = mapped_column(Float)
    duration_ms: Mapped[int | None] = mapped_column(Integer)

    query: Mapped[Query] = relationship(back_populates="trace_events")

    __table_args__ = (Index("ix_trace_query_step", "query_id", "step_index"),)


class QueryChunk(Base):
    """What was retrieved for a query: citations in the UI, contexts for Ragas."""

    __tablename__ = "query_chunks"

    id: Mapped[uuid.UUID] = _pk()
    created_at: Mapped[datetime] = _created_at()

    query_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("queries.id", ondelete="CASCADE"), index=True, nullable=False
    )
    chunk_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chunks.id", ondelete="CASCADE"), nullable=False
    )
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    similarity_score: Mapped[float | None] = mapped_column(Float)
    rerank_score: Mapped[float | None] = mapped_column(Float)


# --------------------------------------------------------------------------
# 4.4 Evaluation
# --------------------------------------------------------------------------

class GoldenQuestion(Base):
    __tablename__ = "golden_questions"

    id: Mapped[uuid.UUID] = _pk()
    created_at: Mapped[datetime] = _created_at()

    question: Mapped[str] = mapped_column(Text, nullable=False)
    # Required by Ragas context_recall - not optional decoration.
    reference_answer: Mapped[str | None] = mapped_column(Text)
    expected_behaviour: Mapped[str] = mapped_column(String(16), default="answer", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class EvalRun(Base):
    __tablename__ = "eval_runs"

    id: Mapped[uuid.UUID] = _pk()
    created_at: Mapped[datetime] = _created_at()

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    judge_model: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), default="running", nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # What changed since the last run - the point of eval-driven development.
    notes: Mapped[str | None] = mapped_column(Text)


class EvalResult(Base):
    __tablename__ = "eval_results"

    id: Mapped[uuid.UUID] = _pk()
    created_at: Mapped[datetime] = _created_at()

    eval_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("eval_runs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    golden_question_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("golden_questions.id", ondelete="CASCADE"), nullable=False
    )
    query_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("queries.id", ondelete="SET NULL")
    )
    faithfulness: Mapped[float | None] = mapped_column(Float)
    answer_relevance: Mapped[float | None] = mapped_column(Float)
    context_precision: Mapped[float | None] = mapped_column(Float)
    context_recall: Mapped[float | None] = mapped_column(Float)


# --------------------------------------------------------------------------
# 4.5 Supporting
# --------------------------------------------------------------------------

class Feedback(Base):
    __tablename__ = "feedback"

    id: Mapped[uuid.UUID] = _pk()
    created_at: Mapped[datetime] = _created_at()

    query_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("queries.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text)


class ApiUsage(Base):
    __tablename__ = "api_usage"

    id: Mapped[uuid.UUID] = _pk()
    created_at: Mapped[datetime] = _created_at()

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    units: Mapped[int | None] = mapped_column(Integer)
    estimated_cost: Mapped[float | None] = mapped_column(Float)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = _pk()
    created_at: Mapped[datetime] = _created_at()

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str | None] = mapped_column(String(64))
    resource_id: Mapped[str | None] = mapped_column(String(128))
    audit_metadata: Mapped[dict | None] = mapped_column("metadata", JSONB)
