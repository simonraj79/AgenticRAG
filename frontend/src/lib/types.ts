/**
 * The API contract, transcribed.
 *
 * These mirror the FastAPI response models exactly, field for field. They are
 * hand-written rather than generated because the generator would be another
 * build dependency for eleven small shapes -- but that makes them a copy that
 * can drift, so: if a field here disagrees with the Pydantic model in
 * `backend/app/api/`, the backend is right and this file is stale.
 *
 * UUIDs cross the wire as strings. Timestamps are ISO-8601 strings.
 */

/** Extra option accepted by `api()`: a value to send as a JSON body. */
export type ApiInit = Omit<RequestInit, "credentials"> & { json?: unknown };

export type User = {
  id: string;
  email: string;
  name: string | null;
  avatar_url: string | null;
  role: string;
  last_login_at?: string | null;
};

/**
 * A parameter preset. Values are COPIED onto the agent at creation, never read
 * through afterwards (PRD section 4.2) -- so editing a template never silently
 * re-tunes an agent somebody already built and evaluated. That is why the
 * picker shows the numbers: they are the starting values, and after creation
 * the agent owns them.
 */
export type Template = {
  id: string;
  slug: string;
  name: string;
  description: string | null;
  chunk_size: number;
  chunk_overlap: number;
  splitter: string;
  retrieve_k: number;
  rerank_enabled: boolean;
  rerank_top_n: number;
  score_threshold: number;
  max_rewrites: number;
};

/** `status` is one of "empty" | "indexing" | "ready"; typed loosely so an
 *  unrecognised value renders instead of crashing the card grid. */
export type Agent = {
  id: string;
  name: string;
  description: string | null;
  status: string;
  visibility: string;
  template_id: string | null;
  embedding_model: string | null;
  chunk_size: number;
  chunk_overlap: number;
  splitter: string;
  retrieve_k: number;
  rerank_enabled: boolean;
  rerank_top_n: number;
  score_threshold: number;
  max_rewrites: number;
  system_prompt: string | null;
  document_count: number;
  created_at: string;
};

/** `status`: "pending" | "processing" | "indexed" | "failed". */
export type DocumentRow = {
  id: string;
  filename: string;
  mime_type: string | null;
  byte_size: number | null;
  status: string;
  chunk_count: number;
  created_at: string;
};

/**
 * One retrieved chunk, as shown under an answer.
 *
 * `similarity_score` and `rerank_score` are both present on purpose: side by
 * side they are the Stage 2 demo, showing that reranking reorders what
 * embedding similarity ranked (PRD section 4.3). `rerank_score` is null when
 * the agent has reranking disabled.
 */
export type Citation = {
  chunk_id: string;
  document_id: string;
  filename: string;
  chunk_index: number;
  rank: number;
  similarity_score: number | null;
  rerank_score: number | null;
  text_preview: string;
};

export type AskResult = {
  query_id: string;
  answer: string;
  /** True when the agent declined for lack of grounding. A CORRECT outcome, not
   *  an error -- the golden set contains questions whose right answer is "I
   *  don't know" (PRD section 4.4). The UI labels it, never hides it. */
  refused: boolean;
  latency_ms: number;
  model_used: string | null;
  citations: Citation[];
};

export type QueryRow = {
  id: string;
  question: string;
  answer: string | null;
  refused: boolean;
  latency_ms: number | null;
  model_used: string | null;
  created_at: string;
};

/**
 * One agent decision. `event_type` is RETRIEVE / SCORE_CHECK / REWRITE /
 * RERANK / GENERATE / REFUSE, and `payload` is JSONB whose shape varies by
 * type -- hence `unknown`, rendered as formatted JSON rather than parsed into
 * a per-type view. This table IS the Trace view (PRD section 4.3).
 *
 * `id` is typed loosely because trace_events uses a bigserial key, which some
 * serialisers emit as a number and others as a string.
 */
export type TraceEvent = {
  id: string | number;
  step_index: number;
  event_type: string;
  payload: unknown;
  score: number | null;
  duration_ms: number | null;
  created_at: string;
};
