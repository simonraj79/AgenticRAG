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
  /** The persona fields. All four are nullable and stay that way: the three
   *  original templates were seeded before these columns existed, so `pedagogy`
   *  in particular is null for them and for every agent created from them. A
   *  card that assumes a pedagogy line renders a hole. */
  persona_role: string | null;
  pedagogy: string | null;
  icon: string | null;
  /** "explain" | "practice" | "assess" | "reflect" | "general", but a plain
   *  string in the database rather than an enum -- adding a category must not
   *  need a migration. An unrecognised value has to degrade to "ungrouped",
   *  never throw. */
  category: string | null;
  /** Shown in the picker, not hidden behind it. Once a template IS a persona,
   *  the prompt is the thing being chosen; concealing it conceals the choice. */
  system_prompt: string | null;
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
  /** Copied from the template at creation, like every other parameter -- so an
   *  agent keeps the persona it was created with even if the template is later
   *  retuned. Null for agents created before these columns existed. */
  persona_role: string | null;
  pedagogy: string | null;
  icon: string | null;
  category: string | null;
  document_count: number;
  created_at: string;
};

/**
 * `status`: "pending" | "processing" | "indexed" | "failed".
 *
 * Genuinely asynchronous now: the upload returns 202 with "pending" and the
 * client polls. `error` carries why a "failed" row failed -- without it a
 * failure is a red pill with no way to find out what went wrong short of
 * reading the server log, which a workshop attendee cannot do.
 */
export type DocumentRow = {
  id: string;
  filename: string;
  mime_type: string | null;
  byte_size: number | null;
  status: string;
  error: string | null;
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
  /**
   * 1-based, and the only thing that ties an answer to its evidence.
   *
   * The answer text carries `[1]`, `[2]` inline; each one indexes into
   * `citations` by THIS field, not by array position. They usually agree, but
   * relying on position would silently mis-attribute the moment the backend
   * drops or reorders one -- and a citation pointing at the wrong source is
   * worse than no citation, because it still looks right.
   */
  marker: number;
  chunk_id: string;
  document_id: string;
  filename: string;
  chunk_index: number;
  rank: number;
  similarity_score: number | null;
  rerank_score: number | null;
  text_preview: string;
};

/** One turn as the server records it: the question, the answer, and everything
 *  needed to justify the answer. This is what a reloaded conversation is made
 *  of, and what a freshly asked turn is folded into. */
export type ChatMessage = {
  query_id: string;
  question: string;
  /** Nullable because `queries.answer` is -- a turn can be recorded without a
   *  completed answer. */
  answer: string | null;
  /** True when the agent declined for lack of grounding. A CORRECT outcome, not
   *  an error -- the golden set contains questions whose right answer is "I
   *  don't know" (PRD section 4.4). The UI labels it, never hides it. */
  refused: boolean;
  latency_ms: number | null;
  model_used: string | null;
  /**
   * The history-contextualised query that was actually embedded, or null when
   * the question went to Pinecone unchanged.
   *
   * "You asked X, I searched for Y" is the one thing a multi-turn RAG can tell
   * a user about itself that a single-turn one cannot, and it is exactly what
   * goes wrong invisibly: a rewrite that drags in the wrong antecedent from two
   * turns ago produces a confidently irrelevant answer with no visible cause.
   */
  rewritten_question: string | null;
  created_at: string;
  citations: Citation[];
};

/** `POST .../ask`. The same turn as `ChatMessage`, minus the question (the
 *  caller has it) and plus the thread it landed in -- which the one-shot route
 *  creates implicitly, so it is not always known before the call. */
export type AskResult = {
  query_id: string;
  conversation_id: string;
  answer: string;
  refused: boolean;
  latency_ms: number;
  model_used: string | null;
  rewritten_question: string | null;
  citations: Citation[];
};

/**
 * One chat thread against one agent.
 *
 * `updated_at` is what the list sorts on and it is maintained by SQLAlchemy's
 * `onupdate`, not by a database trigger -- so it moves only when something
 * writes the conversation row itself. The client re-reads the list after each
 * turn rather than assuming local ordering is still right.
 */
export type Conversation = {
  id: string;
  agent_id: string;
  /** Usually derived from the first question rather than typed, so it is null
   *  for a thread whose first turn has not landed yet. */
  title: string | null;
  message_count: number;
  created_at: string;
  updated_at: string;
};

export type ConversationDetail = Conversation & { messages: ChatMessage[] };

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

// --------------------------------------------------------------------------
// Stage 3 -- golden set and scorecards
// --------------------------------------------------------------------------

/**
 * One test question, belonging to the agent whose corpus can answer it.
 *
 * `expected_behaviour` is "answer" | "refuse" and `source` is
 * "ai_suggested" | "edited" | "manual" | "imported", but both are typed as
 * plain strings on purpose -- they are `String(16)` columns rather than
 * enums specifically so a new value costs a seed row instead of a migration.
 * A union type here would make the frontend the thing that breaks when the
 * backend gains a value, which is exactly backwards. Every read site treats an
 * unrecognised value as the default ("answer" / "manual") rather than throwing.
 *
 * `reference_answer` is nullable in the database but is NOT optional in
 * practice: Ragas cannot compute `context_recall` without it, so an "answer"
 * question with no reference silently drops a quarter of the scorecard to null.
 * The editor warns about exactly that.
 *
 * `agent_id` is nullable because rows written before golden sets belonged to an
 * agent exist and cannot be attributed after the fact. The server filters them
 * out; it is here so the shape matches what the API actually returns.
 */
export type GoldenQuestion = {
  id: string;
  agent_id: string | null;
  question: string;
  reference_answer: string | null;
  expected_behaviour: string;
  is_active: boolean;
  source: string;
  order_index: number;
  created_at: string;
};

/** The fields a client may write. Everything else -- `source`, `id`,
 *  `created_at` -- is the server's to decide; `source` in particular flips to
 *  "edited" on a PATCH, which is the whole point of showing it. */
export type GoldenQuestionInput = {
  question: string;
  reference_answer?: string | null;
  expected_behaviour?: string;
  is_active?: boolean;
  order_index?: number;
};

/**
 * The roll-up of one run.
 *
 * **Every metric is nullable and null does not mean zero.** Null is "nothing
 * could be scored"; zero is "scored, and the answer was unsupported". Rendering
 * the first as the second is the easiest way to make a measurement lie, so the
 * type keeps them distinct and the Scorecard renders "not scored".
 *
 * **The means rest on `scored_count` rows, not on the whole golden set.**
 * Questions whose `expected_behaviour` is "refuse" are excluded from all four
 * means and reported separately as `refusal_pass / refusal_total`. A correct
 * refusal has no useful retrieved context and an answer that deliberately does
 * not follow from it, so faithfulness and context_recall score near zero for
 * behaving perfectly -- averaging them in would punish the agent for being
 * right, and `weakest_metric` would then point at whichever metric refusals
 * punish hardest instead of at the real weakness.
 */
export type ScoreSummary = {
  faithfulness: number | null;
  answer_relevance: number | null;
  context_precision: number | null;
  context_recall: number | null;
  /** The key of the lowest-scoring metric: "faithfulness" | "answer_relevance"
   *  | "context_precision" | "context_recall". Null when nothing was scored. */
  weakest_metric: string | null;
  weakest_score: number | null;
  /** How many questions the four means actually rest on. */
  scored_count: number;
  refusal_pass: number;
  refusal_total: number;
};

/**
 * One scorecard header. `status` is "pending" | "running" | "completed" |
 * "failed", typed loosely for the same reason as the fields above.
 *
 * `generation_model` is recorded per run rather than read live from the agent,
 * because the agent's setting can change after a run and reading it back would
 * attribute a score to a model that never produced the answer. When it equals
 * `judge_model` the run is self-judged, which `judge_is_generator` states
 * outright so the UI does not have to compare two strings and guess.
 */
export type EvalRun = {
  id: string;
  agent_id: string | null;
  status: string;
  judge_model: string | null;
  generation_model: string | null;
  notes: string | null;
  started_at: string | null;
  finished_at: string | null;
  /** Why the RUN ended without a summary. Distinct from `EvalResult.error`,
   *  which is one question failing inside an otherwise good run. */
  error: string | null;
  progress: { done: number; total: number };
  summary: ScoreSummary | null;
  judge_is_generator: boolean;
};

/**
 * One question inside one run.
 *
 * `behaviour_ok` is not derivable from the four floats: a correct refusal is a
 * success case Ragas has nothing to grade, so a passed refusal question is four
 * nulls plus `behaviour_ok: true`, and without this field it would be
 * indistinguishable from a row that crashed.
 */
export type EvalResult = {
  id: string;
  golden_question_id: string;
  question: string;
  expected_behaviour: string;
  query_id: string | null;
  answer: string | null;
  refused: boolean;
  behaviour_ok: boolean | null;
  faithfulness: number | null;
  answer_relevance: number | null;
  context_precision: number | null;
  context_recall: number | null;
  error: string | null;
};

export type EvalRunDetail = EvalRun & { results: EvalResult[] };

/**
 * One question as it appears in the plain-JSON golden-set file.
 *
 * Deliberately looser than `GoldenQuestion`: the file is meant to be edited in
 * a text editor, so import accepts a bare list as well as the wrapper object,
 * ignores unknown keys, and tolerates a missing `expected_behaviour`. A
 * hand-edited file must not be rejected on a technicality.
 */
export type GoldenSetFileQuestion = {
  question: string;
  reference_answer?: string | null;
  expected_behaviour?: string | null;
};

export type GoldenSetFile = {
  agent_name?: string;
  exported_at?: string;
  questions: GoldenSetFileQuestion[];
};
