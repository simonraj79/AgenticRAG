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
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    desc,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, deferred, mapped_column, relationship


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
    # explain / practice / reflect / assess / general / orchestrate. Deliberately
    # a plain String rather than an enum: adding a category must not need a
    # migration, and an unrecognised value degrades to "ungrouped" rather than to
    # an error.
    category: Mapped[str | None] = mapped_column(String(32))

    # The specialist roster this template seeds, as a list of slugs from
    # `app/db/specialists.py`. NULL on every template that is not an
    # orchestrator, which is all of them but `adaptive-tutor`.
    #
    # On the TEMPLATE as well as on the agent because `agents.py`'s
    # TEMPLATE_PARAMETERS copy loop is a field-by-field getattr/setattr (PRD
    # 4.2): a roster that lived only here would be re-tuned under an existing
    # agent's feet the moment somebody edited the preset.
    #
    # JSONB rather than a join table because a roster is a five-element list of
    # code-defined constants, not an entity - `specialists.BY_SLUG` is the
    # authority, and a foreign key would create a second one that could disagree.
    specialists: Mapped[list[str] | None] = mapped_column(JSONB)

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

    # Whether generation runs as a bounded tool loop rather than as one model
    # call. A per-agent column and not only a setting, because turning the loop
    # on changes what an agent IS: it can search its own corpus a second time and
    # it can write and run Python, so the same question can produce a different
    # answer, a different trace and a different latency. An eval run is recorded
    # against an agent, so that switch has to be part of the agent's stored
    # configuration for the same PRD 4.2 reason every tuning parameter is - a
    # scorecard whose agent could have silently become agentic underneath it is a
    # scorecard that no longer describes anything.
    #
    # `server_default` true so a NEW agent is agentic without the API having to
    # say so. The migration that adds this column then backfills every EXISTING
    # row to false, and that asymmetry is deliberate rather than an oversight:
    # agents whose runs are already written up in EVAL.md keep behaving exactly
    # as they were measured. `settings.agent_tools_enabled` is a separate global
    # kill switch above this, not a duplicate of it.
    tools_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
    # How many tool round-trips one turn may make before the loop is closed and
    # an answer is forced. The workshop names cost blowout as one of four agentic
    # failure modes, and an unbounded tool loop is the textbook way to reach it -
    # every step is a fresh model call carrying the whole accumulated transcript,
    # so cost per step grows while the value of another step falls. Same argument
    # as `max_rewrites`, one loop further out.
    max_tool_steps: Mapped[int] = mapped_column(
        Integer, default=3, server_default=text("3"), nullable=False
    )

    # Which teaching specialists may answer a turn for this agent, as slugs from
    # `app/db/specialists.py`.
    #
    # **ONE COLUMN CARRIES BOTH THE ON/OFF AND THE ROSTER, and that is the whole
    # design.** NULL means "not an orchestrator": no routing call, no prompt
    # substitution, no ROUTE event, and a turn byte-identical to the one this
    # agent took yesterday. A separate `orchestrating` boolean would make the
    # classic path depend on two columns agreeing, and the failure of that
    # agreement - roster set, flag off - is silent, which is the shape this
    # project keeps paying for. `specialists.roster(None)` returns an empty
    # tuple rather than the default five for the same reason.
    #
    # No backfill was needed when this landed, unlike `tools_enabled` above:
    # NULL is already the classic path, so every agent that predates the column
    # is unchanged by construction rather than by an UPDATE.
    specialists: Mapped[list[str] | None] = mapped_column(JSONB)

    # Whether a drafted answer is checked against its own ledger before it is
    # returned. ORTHOGONAL to `specialists`, and on `agents` only - following
    # `tools_enabled`, which is also absent from `agent_templates`, because a
    # persona is a claim about HOW to answer rather than about which quality
    # controls the operator wants running.
    #
    # `server_default false` and no backfill, which is the inverse of
    # `tools_enabled`'s asymmetry and needs no explanation beyond this: false IS
    # the pre-existing behaviour, so a new agent and an old one both start where
    # every EVAL.md number was measured.
    self_check_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )

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
    # Where the ORIGINAL upload lives in Cloudflare R2. Before the object-storage
    # change set there was no such thing: `rag/ingest._load_text` took bytes,
    # returned a `str`, and the original was unreachable the moment
    # `run_ingest_job` returned -- which is why a row stranded at `processing`
    # has always been unresumable rather than merely unretried.
    #
    # NULL means the original was not kept: every document ingested before this
    # column existed, and any upload made while `storage_route` is "postgres".
    #
    # Keeping the original does NOT make chunking retroactive. `chunk_size` is
    # still read once, at ingest (`rag/ingest.py`), and there is still no
    # re-ingest route -- so `AgentSettingsSheet`'s "Takes effect on the next
    # upload" remains true. What this column changes is that a re-chunk feature
    # is now POSSIBLE, because the bytes a new split would need are no longer
    # gone. `chunks.text` cannot serve that purpose: chunk boundaries are lossy,
    # so re-splitting already-split text is not the same operation as splitting
    # the original.
    storage_key: Mapped[str | None] = mapped_column(Text)

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
    """One PROVIDER CALL and what it cost. The unit the admin console groups on.

    The first four columns are original and unchanged; everything below
    `agent_id` arrived with the admin console (`new features/14-admin-
    observability/`). The table had existed with zero rows since the initial
    schema, which is why this is an extension rather than a new table.

    **Per CALL, not per turn, and that was a decision.** One question makes 1-3
    generation calls plus a rewrite plus a route plus possibly a critic. But the
    deciding argument was coverage rather than granularity: the Ragas judge and
    the golden-set drafter belong to no `queries` row at all, so a turn-shaped
    unit would have had nowhere to put evaluation spend and would have shown a
    complete-looking total that silently excluded it.

    **Every foreign key here is SET NULL, never CASCADE, and that is the point of
    the table.** Deleting an agent must not delete the evidence that it cost
    money -- an accounting record whose subject is gone is still an accounting
    record. Contrast `query_chunks`, whose CASCADE CLAUDE.md records as silently
    destroying the stored contexts of every past query when a document is
    deleted: scores survive, evidence does not, and nothing signals it.
    """

    __tablename__ = "api_usage"

    id: Mapped[uuid.UUID] = _pk()
    created_at: Mapped[datetime] = _created_at()

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    # "openrouter" | "cohere" | "pinecone"
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    # "chat" | "embedding" | "rerank"
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    # Whatever the provider bills in when it is not tokens -- Cohere search
    # units, documents embedded. Null for a chat call, where tokens are below.
    units: Mapped[int | None] = mapped_column(Integer)
    # ORIGINAL COLUMN, and its name is now load-bearing rather than incidental:
    # it holds a cost we had to ESTIMATE, and `cost_usd` below holds one the
    # provider REPORTED. Keeping them apart is what lets the console say which
    # half of a total it actually knows.
    estimated_cost: Mapped[float | None] = mapped_column(Float)

    # -- added with the admin console ------------------------------------
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agents.id", ondelete="SET NULL"), index=True
    )
    query_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("queries.id", ondelete="SET NULL"), index=True
    )
    # Which part of the system spent this. See `app.metering.context.CALL_KINDS`.
    call_kind: Mapped[str | None] = mapped_column(String(32), index=True)
    model: Mapped[str | None] = mapped_column(String(128))
    # WHICH OpenRouter endpoint served the call. CLAUDE.md records that
    # `llm_check.py` structurally cannot know this and that only a live call can
    # -- and OpenRouter has been sending it on every response all along, in a
    # field langchain-openai drops. Two identical requests have been measured
    # here costing 2.002e-05 and 5.684e-06 depending on which endpoint answered,
    # so this column is what makes a cost anomaly explainable instead of eerie.
    served_provider: Mapped[str | None] = mapped_column(String(64))
    # OpenRouter's `gen-...`. Its absence on the 76 pre-instrumentation queries
    # is precisely why they cannot be backfilled: `GET /api/v1/generation?id=`
    # works, and there is no id to give it.
    generation_id: Mapped[str | None] = mapped_column(String(128))

    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    # Billed at the completion rate. CLAUDE.md measured 60-79% of billed output
    # being thinking on this model's default, which is why generation turns it
    # off -- this column is how that stays true rather than remembered.
    reasoning_tokens: Mapped[int | None] = mapped_column(Integer)
    cached_tokens: Mapped[int | None] = mapped_column(Integer)

    # REPORTED by the provider. NULL means not measured, never free.
    cost_usd: Mapped[float | None] = mapped_column(Float)
    cost_is_estimated: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )

    duration_ms: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        Index("ix_api_usage_created", "created_at"),
        Index("ix_api_usage_user_created", "user_id", "created_at"),
        Index("ix_api_usage_agent_created", "agent_id", "created_at"),
    )


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


# --------------------------------------------------------------------------
# 4.6 Handouts
# --------------------------------------------------------------------------

class Handout(Base):
    """A generated asset - a chart, a slide deck, a table or a study sheet.

    PRD section 11 does not describe this table for the same reason it does not
    describe `conversations`: the feature was specified after that section was
    written. It is the durable half of the tool loop. A `run_python` call
    produces bytes inside a temporary directory that is deleted moments later,
    so without a row here the one thing the agent can now make that it could not
    make before would exist only for the length of an HTTP response.

    A handout arrives one of two ways and `origin` is the only thing that
    distinguishes them: "tool", where the agent wrote Python mid-conversation
    and a file fell out of it, and "recipe", where the user pressed a button and
    described what they wanted. They are one table rather than two because
    everything downstream - listing, downloading, deleting, the quota - treats
    them identically, and a user thinking about a chart they were given does not
    think about which of the two ways they asked for it.

    **The bytes live in Postgres.** Object storage is the right long-term answer
    (PRD open item 10) and is deliberately not built here; instead the column is
    capped per file and per agent, and `content` is deferred so the cost of
    keeping it in the row is paid only by the one request that actually wants
    the file.
    """

    __tablename__ = "handouts"

    id: Mapped[uuid.UUID] = _pk()
    created_at: Mapped[datetime] = _created_at()

    # Scoping key, and the reason every handout route can be nested under
    # `/api/agents/{agent_id}/...` and authorised by `OwnedAgent` alone. A
    # handout is made out of one agent's corpus, so it belongs to that agent the
    # way a document does; `created_by_user_id` below is audit, not access
    # control.
    agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # NULL for a handout made from the panel with no thread open. CASCADE rather
    # than SET NULL, and the asymmetry with `query_id` below is the decision
    # worth reading: deleting a thread is an explicit user action on something
    # they can see, and a handout still pointing at a conversation that no longer
    # exists would be filed under a heading the UI cannot render.
    #
    # No `index=True` here - the index this column needs is declared in
    # `__table_args__` instead, because it has to be dropped by name in the
    # migration's downgrade. Setting both would build two identical indexes.
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE")
    )
    # SET NULL, NOT CASCADE, and this is the one FK on the table that would be
    # actively wrong the other way. A handout OUTLIVES the turn that produced it:
    # the user downloaded a slide deck a week ago and still has it listed, and
    # the query row is only ever provenance - "this came out of that answer".
    # Under CASCADE, anything that removes a query would silently destroy files
    # the user never asked to lose, which is the same class of quiet data loss
    # as the `query_chunks` cascade recorded in CLAUDE.md. NULL here reads as
    # "made outside any turn", which is exactly what a recipe handout is.
    query_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("queries.id", ondelete="SET NULL")
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    # chart / deck / sheet / table. A plain String rather than an enum, for the
    # reason `agent_templates.category` is one: adding a recipe must not need a
    # migration, and an unrecognised kind degrades to a generic card rather than
    # to an error.
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    # What the file is called on download. Model-written, therefore sanitised at
    # the route before it reaches a Content-Disposition header - a filename
    # derived from a generated string is header injection if it is trusted.
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    # Stored rather than derived with `length(content)`, precisely so that the
    # list query can show a size without touching the deferred column below.
    byte_size: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # pending / ready / failed. A recipe handout is inserted "pending" and
    # finished by a background job, so the row exists before its bytes do and the
    # panel has something to poll. The job writes a terminal status in a
    # `finally`: a task that dies silently leaves a row at "pending" forever,
    # which reads as progress and never gets investigated.
    status: Mapped[str] = mapped_column(String(16), default="ready", nullable=False)
    # tool / recipe. See the class docstring.
    origin: Mapped[str] = mapped_column(String(16), default="tool", nullable=False)

    # DEFERRED, and not as an optimisation. The panel lists up to
    # `settings.handout_max_per_agent` rows at a time; a `SELECT *` that eagerly
    # loads bytea returns tens of megabytes for a list that renders filenames and
    # sizes, and the symptom is a slow network rather than anything that points
    # at this column. Deferring means the bytes are fetched only when something
    # actually reads `.content` - which is the download route, and nothing else.
    # `HandoutOut` has no `content` field either, so there are two independent
    # guards, because this is a mistake that only shows up under real data.
    content: Mapped[bytes | None] = deferred(mapped_column(LargeBinary))
    # Where the bytes live in Cloudflare R2, when they live there. Derived from
    # `agent_id` and this row's own `id` by `app/storage.handout_key` -- never
    # supplied by a caller, which is the same structural control that makes
    # `Agent.namespace` a derived property rather than a parameter.
    #
    # NULL means "not in object storage", which covers a `pending` row, a
    # `failed` row that never produced bytes, and any row predating the R2
    # change set. `content` and this column are BOTH nullable and neither
    # excludes the other: during the blue/green window a backfilled row has both,
    # which is what makes `storage_route=postgres` a working rollback rather
    # than a comment.
    #
    # NOT deferred, and the contrast with `content` above is the point. That
    # column is deferred because a `SELECT *` over 200 rows would drag tens of
    # megabytes of bytea; a key is a short string, and the list route needs to
    # know whether a row HAS one. But note what that costs: every `select(Handout)`
    # loads this by default, so it must never carry a presigned URL. A URL is a
    # bearer capability with a TTL, and one in a 200-row list body is 200 leaked
    # capabilities. `HandoutOut` has no field for either.
    storage_key: Mapped[str | None] = mapped_column(Text)
    # The markdown body for a study sheet, a caption for everything else. Held
    # beside `content` rather than decoded from it so the panel can render a
    # preview inline without a second request and without guessing an encoding.
    preview_text: Mapped[str | None] = mapped_column(Text)
    # The Python that produced the file, kept and shown. NotebookLM does not do
    # this; for a product whose whole purpose is making a pipeline inspectable,
    # hiding the generation step would be the one place it stopped practising
    # what it teaches - and it is the fastest way for a user to see why a chart
    # is wrong. Both attempts are stored when a failed run is retried, so the
    # correction is visible too.
    source_code: Mapped[str | None] = mapped_column(Text)
    # `{"chunk_ids": [...], "recipe": ..., "brief": ..., "model": ...}` - what a
    # handout was made from, so it can be traced back to the chunks that produced
    # it the way an answer can.
    #
    # The attribute is `meta` and so is the column. `metadata` is reserved on a
    # declarative Base (it is the MetaData registry), which is why `AuditLog`
    # above maps `audit_metadata` onto a column literally named "metadata" - that
    # rename exists to preserve a column name that already shipped. There is no
    # such history here, so the column is simply called `meta` and the Python
    # name matches it. Do not "fix" this into the AuditLog shape: it would buy a
    # reserved-word collision and a name mismatch for nothing.
    meta: Mapped[dict | None] = mapped_column(JSONB)
    # Why THIS handout failed, so a failed row can explain itself in the panel
    # and offer a retry. `documents` has no such column, which is why ingest
    # failures have to be dug out of `audit_log`.
    error: Mapped[str | None] = mapped_column(Text)

    # The panel reads exactly these two lists: one agent's handouts newest first,
    # and one conversation's. DESC in the first index rather than at the call
    # site, so the newest-first read - which is every read - is a forward scan,
    # the same reasoning as `ix_eval_runs_agent_created`.
    __table_args__ = (
        Index("ix_handouts_agent_created", "agent_id", desc("created_at")),
        Index("ix_handouts_conversation", "conversation_id"),
    )
