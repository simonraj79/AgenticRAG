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
    desc,
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


def _updated_at() -> Mapped[datetime]:
    """A last-touched timestamp maintained by SQLAlchemy, not by the database.

    `onupdate` emits `now()` in the UPDATE statement, so it fires only when this
    row itself is written. There is no trigger behind it: inserting a child row
    that points here leaves the parent's timestamp alone.
    """
    return mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


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

    # Persona presentation. These carry no behaviour: the persona lives entirely
    # in `system_prompt`, and these four exist so the picker can show what the
    # prompt does without making a user read it. All nullable, because a
    # hand-created template has no obligation to be a persona and the three
    # templates seeded before this column existed have no pedagogy to claim.
    persona_role: Mapped[str | None] = mapped_column(String(64))
    pedagogy: Mapped[str | None] = mapped_column(Text)
    icon: Mapped[str | None] = mapped_column(String(16))
    # explain / practice / reflect / assess / general. Deliberately a plain
    # String rather than an enum: adding a category must not need a migration,
    # and an unrecognised value degrades to "ungrouped" rather than to an error.
    category: Mapped[str | None] = mapped_column(String(32))

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

    # Copied from the template at creation alongside every other parameter
    # (PRD 4.2). Duplicated onto the agent rather than read back through
    # `template_id` for the same reason the tuning parameters are: renaming a
    # template's persona must not silently re-label agents somebody already
    # built, and an agent whose template was deleted keeps its identity.
    persona_role: Mapped[str | None] = mapped_column(String(64))
    pedagogy: Mapped[str | None] = mapped_column(Text)
    icon: Mapped[str | None] = mapped_column(String(16))
    category: Mapped[str | None] = mapped_column(String(32))

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

class Conversation(Base):
    """One chat thread: an ordered run of queries against a single agent.

    PRD section 11 still lists multi-turn conversational memory as out of scope.
    It was requested after that section was written, so this table is the part
    of the schema the PRD does not yet describe - not a contradiction of it.
    """

    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = _pk()
    created_at: Mapped[datetime] = _created_at()
    # The chat list is ordered by recency of activity, which is this column.
    # Nothing bumps it automatically - see `_updated_at`. The code that appends
    # a turn has to touch the conversation row for the list to reorder.
    updated_at: Mapped[datetime] = _updated_at()

    # A thread belongs to one agent: its history is only meaningful against the
    # corpus and persona that produced it, and history-aware retrieval reads
    # earlier turns back as context for a search in this agent's namespace.
    agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # Usually derived from the first question rather than typed. Nullable so a
    # thread can exist before there is anything to name it after.
    title: Mapped[str | None] = mapped_column(String(200))
    # Hides a thread from the list without destroying the trace history that
    # hangs off its queries. There is no delete path that would keep those.
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Ordered here as well as by the index below, so a thread read through the
    # ORM is never rendered out of sequence by whatever order the rows come back.
    queries: Mapped[list["Query"]] = relationship(
        back_populates="conversation", order_by="Query.created_at"
    )


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
    # Nullable, and it stays nullable. Rows already exist from the one-shot era
    # that belong to no thread, and a NOT NULL column cannot be added to a
    # populated table without inventing a conversation to backfill them into -
    # which would fabricate threads that were never had. A null here means "a
    # single question, asked outside any thread", and readers must handle it.
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str | None] = mapped_column(Text)
    model_used: Mapped[str | None] = mapped_column(String(128))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    # A correct refusal is a success case, not a failure.
    refused: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    conversation: Mapped[Conversation | None] = relationship(back_populates="queries")
    trace_events: Mapped[list["TraceEvent"]] = relationship(back_populates="query")

    __table_args__ = (
        Index("ix_queries_user_created", "user_id", "created_at"),
        # The chat view reads exactly this: one thread's turns, oldest first.
        # The single-column index on `conversation_id` finds the thread; this one
        # returns it already ordered, so opening a long conversation never sorts.
        Index("ix_queries_conversation_created", "conversation_id", "created_at"),
    )


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
    """One test question, belonging to the agent whose corpus can answer it."""

    __tablename__ = "golden_questions"

    id: Mapped[uuid.UUID] = _pk()
    created_at: Mapped[datetime] = _created_at()

    # A golden set is only meaningful against ONE corpus. A question about
    # lecture transcripts scored against a policy agent measures nothing - and
    # it measures nothing QUIETLY, because faithfulness and recall still return
    # numbers, they are simply numbers about the wrong corpus. Without this
    # column every agent shares one global golden set, which is silently wrong
    # rather than loudly broken, and a silently wrong scorecard is worse than no
    # scorecard because it is acted on.
    #
    # Nullable for the same reason `queries.conversation_id` is: rows may
    # already exist unscoped, and a NOT NULL column cannot be added to a
    # populated table without a backfill - which here would mean guessing which
    # agent a question was written for, i.e. inventing the very scoping this
    # column exists to record. NULL means "written before golden sets belonged
    # to an agent", and readers must filter it out rather than assume it.
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    # Required by Ragas context_recall - not optional decoration.
    reference_answer: Mapped[str | None] = mapped_column(Text)
    expected_behaviour: Mapped[str] = mapped_column(String(16), default="answer", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # ai_suggested / edited / manual / imported. Provenance is part of what a
    # score is worth: a set the model wrote for itself and nobody reviewed is a
    # weaker test than the same set after a human corrected it, and the two are
    # indistinguishable once they are rows. Deliberately a plain String rather
    # than an enum, for the same reason `agent_templates.category` is - adding a
    # provenance must not need a migration.
    source: Mapped[str] = mapped_column(
        String(16), default="manual", server_default="manual", nullable=False
    )
    # Stable display order in the editor. `created_at` cannot supply it: editing
    # a question does not change when it was created, and a user who drags a
    # question up has nowhere to record that. NOT NULL with a server default,
    # because existing rows have no order to preserve and 0 leaves them tied,
    # which sorts by the secondary key rather than at random.
    order_index: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )

    # The editor reads exactly this: one agent's set, in display order. The
    # single-column index on `agent_id` finds the set; this one hands it back
    # already ordered, so opening a long golden set never sorts.
    __table_args__ = (
        Index("ix_golden_questions_agent_order", "agent_id", "order_index"),
    )


class EvalRun(Base):
    """One scorecard: one agent's golden set, scored once."""

    __tablename__ = "eval_runs"

    id: Mapped[uuid.UUID] = _pk()
    created_at: Mapped[datetime] = _created_at()

    # Which agent was scored. Same reasoning as `golden_questions.agent_id`
    # above, and the same nullability for the same reason: a run predating this
    # column cannot be attributed to an agent after the fact without guessing.
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    judge_model: Mapped[str | None] = mapped_column(String(128))
    # The model that produced the ANSWERS, kept BESIDE `judge_model` rather than
    # collapsed into one "model" column. That separation is the point: when the
    # two are equal the run is self-judged - a model grading its own output -
    # and a scorecard that cannot tell you that is a scorecard you cannot trust.
    # Recorded per run, not read from `agents.generation_model`, because the
    # agent's setting can change afterwards and the number would then be
    # attributed to a model that never produced it.
    generation_model: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), default="running", nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Aggregate scores, plus `weakest_metric`, `scored_count` and the refusal
    # tally. JSONB rather than four float columns, on three grounds:
    #   - it carries more than four floats already, and `weakest_metric` is a
    #     string that would otherwise need a column of its own;
    #   - the shape will grow as metrics are added, and a metric should not cost
    #     a migration;
    #   - it is written ONCE at the end of a run and read whole to render a
    #     card, never queried field-by-field, so the one thing columns buy -
    #     indexed per-field predicates - is not wanted here.
    # The per-question numbers stay in `eval_results`; this is the roll-up.
    summary: Mapped[dict | None] = mapped_column(JSONB)
    # A long run is a background job, so the UI needs somewhere to read
    # "3 of 12" from between polls. NOT NULL with a server default: a run that
    # has not started yet is honestly 0 of 0, whereas NULL would force every
    # reader to decide what a missing count means.
    progress_done: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    progress_total: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    # Run-level failure - the reason a run ended without a summary. Distinct
    # from `eval_results.error`, which is one question going wrong inside an
    # otherwise good run.
    error: Mapped[str | None] = mapped_column(Text)
    # What changed since the last run - the point of eval-driven development.
    notes: Mapped[str | None] = mapped_column(Text)

    # The history list reads exactly this: one agent's runs, newest first.
    # DESC in the index rather than at the call site so the newest-first read -
    # which is every read - is a forward scan.
    __table_args__ = (
        Index("ix_eval_runs_agent_created", "agent_id", desc("created_at")),
    )


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
    # Did the agent do what `expected_behaviour` asked - answer when it should
    # answer, refuse when it should refuse. Not derivable from the four scores
    # above: a correct refusal is a success case that Ragas has nothing to grade,
    # so without this column the only record of a passed refusal question is four
    # NULLs, which is indistinguishable from a run that crashed. Nullable because
    # it is unknown until the question has actually been asked.
    behaviour_ok: Mapped[bool | None] = mapped_column(Boolean)
    # Why THIS question failed - a judge timeout, a generation error. Per-row so
    # that one bad question records its reason and the run carries on; without
    # it the only place to put the failure is the run, where it voids the whole
    # scorecard for a single row.
    error: Mapped[str | None] = mapped_column(Text)


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
