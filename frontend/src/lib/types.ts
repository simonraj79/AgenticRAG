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
  /**
   * Whether the model is handed `search_corpus` and `run_python` and left to
   * decide for itself whether to call them.
   *
   * **False for every agent that existed before the tool loop shipped, and that
   * asymmetry is deliberate.** The migration backfills existing rows to false
   * while the server default for new rows is true, so an agent whose scorecard
   * is already recorded in EVAL.md keeps behaving exactly as it was measured --
   * a tool loop changes the answer, and a historical run that silently stops
   * being reproducible is worse than one that was never taken.
   */
  tools_enabled: boolean;
  /** Tool round-trips allowed in one turn before the loop is closed and an
   *  answer is forced. A ceiling, not a target: most turns use none. */
  max_tool_steps: number;
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

// --------------------------------------------------------------------------
// Handouts -- the generated-asset panel
// --------------------------------------------------------------------------

/**
 * One generated file: a chart, a slide deck, a data table or a study sheet.
 *
 * Handouts arrive by two routes and this type does not separate them beyond
 * `origin`. "tool" means the agent wrote Python in the middle of answering and
 * it produced a file -- the user asked to chart some numbers and got a chart as
 * part of the answer. "recipe" means the user pressed a button in the panel and
 * described what they wanted, with no conversation turn involved. Same row,
 * same list, one small label.
 *
 * `kind` is "chart" | "deck" | "sheet" | "table", `status` is "pending" |
 * "ready" | "failed" and `origin` is "tool" | "recipe" -- all three typed as
 * plain `string` for the reason recorded on `GoldenQuestion` below: they are
 * `String(16)` columns rather than enums specifically so a fifth recipe costs a
 * seed row instead of a migration. A union here would make this file the thing
 * that breaks when the backend gains a value, which is backwards. Every read
 * site must render an unrecognised value rather than throw.
 *
 * **A `pending` handout has no downloadable content yet.** Creation answers 202
 * and a background job writes the bytes afterwards, so until `status` flips to
 * "ready" the row is real and listable but empty: `byte_size` is 0, the
 * download route answers 409, and a thumbnail `<img>` pointed at that URL will
 * fail. The row is returned early on purpose -- it is what the panel shows a
 * spinner against -- but nothing may read its content on the strength of it
 * existing. A row that failed carries `error`; a row still "pending" long after
 * it was created means the job died without writing its terminal status.
 *
 * There is no `content` field, and that is a guard rather than an omission: the
 * bytes never cross this boundary as JSON. They are fetched by a plain link --
 * see `handouts.downloadUrl` in `api.ts`.
 */
export type Handout = {
  id: string;
  kind: string;
  title: string;
  filename: string;
  mime_type: string;
  /** 0 while `status` is "pending" -- nothing has been written yet. */
  byte_size: number;
  status: string;
  origin: string;
  /** Why a "failed" row failed. Without it a failure is a red card with no way
   *  to find out what went wrong short of reading the server log. */
  error: string | null;
  /** The thread this was made in or from, null for a handout made from the
   *  panel with no conversation open. Deleting the conversation CASCADEs, so a
   *  handout listed under a thread does not outlive it. */
  conversation_id: string | null;
  /** The turn that produced it, for `origin: "tool"`. `ON DELETE SET NULL`
   *  rather than cascade -- a handout outlives the query it came from. */
  query_id: string | null;
  created_at: string;
};

/**
 * One handout, opened.
 *
 * Two extra fields, both fetched only when a row is expanded, because both can
 * run to kilobytes and the list renders up to 200 rows.
 *
 * **`source_code` is shown, not hidden.** It is the Python that produced the
 * file, and displaying it is a product decision rather than a debugging
 * leftover: this is an application whose entire purpose is making a pipeline
 * inspectable, so concealing the one step that generates an artefact would be
 * the single place it stopped practising what it teaches. It is also the
 * fastest route to understanding why a chart is wrong. Null for the "sheet"
 * recipe, which the model writes directly with no sandbox involved.
 *
 * `preview_text` is the markdown body for a "sheet" and a caption otherwise, so
 * a study sheet renders inline without a second request for its bytes.
 */
export type HandoutDetail = Handout & {
  preview_text: string | null;
  source_code: string | null;
};

/**
 * One of the four things the panel offers to make.
 *
 * Client-side copy, deliberately: there is no endpoint that lists recipes,
 * because the set is fixed at four and a round trip to learn what four buttons
 * say would be a request for a constant. `key` is the value that goes into
 * `HandoutRequest.recipe` and is what the backend's `RECIPES` dict is keyed on;
 * the other three fields are label copy for the button.
 */
export type HandoutRecipe = {
  key: string;
  label: string;
  /** One line under the button, saying what this recipe is for. */
  blurb: string;
  icon: string;
};

/**
 * The create body -- named for what it is, since section 4.6 of the plan gives
 * `handouts.create(agentId, req)` without naming `req`.
 *
 * `brief` is what the handout should cover, in the user's own words, and it is
 * also what searches the corpus: a handout is grounded the same way an answer
 * is, or it becomes the one place the product hallucinates freely.
 * `conversation_id` adds the recent answers of a thread on top of that, which
 * is the case the panel exists for ("chart what we just discussed"). Omitted,
 * the brief alone still retrieves -- so the panel works with no thread open.
 */
export type HandoutRequest = {
  recipe: string;
  brief: string;
  conversation_id?: string | null;
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
  /**
   * Files this turn produced, if the agent ran Python while answering.
   *
   * Carried on the replayed turn and not only on the fresh one, deliberately: a
   * reloaded conversation must show the same thing the live one did, and the
   * alternative -- the panel re-deriving which handout belongs to which turn
   * from a separate list request -- puts the join in the client, where it can
   * disagree with the server. Empty for every turn on an agent with tools off,
   * and empty for every turn recorded before handouts existed.
   */
  handouts: Handout[];
  /** How many tool round-trips the answer took; 0 when the model answered
   *  without calling one, which is the common case. The count is a chip on the
   *  turn -- the TOOL_CALL / TOOL_RESULT trace events hold the detail. */
  tool_steps: number;
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
  /**
   * Files this turn just produced. Both fields default to empty server-side, so
   * an agent with tools disabled serialises exactly as it did before.
   *
   * The panel prepends these rather than waiting for its 3-second poll to
   * notice them -- a file the user explicitly asked for should not take three
   * seconds to appear. On a narrow viewport, where the panel is a closed
   * drawer, this is also what lets the answer carry a chip that opens it;
   * otherwise the artefact is invisible on the device most likely to be used.
   */
  handouts: Handout[];
  tool_steps: number;
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
