/**
 * The single door to the backend. Every network call in this app goes through
 * `api()`; nothing else calls `fetch`.
 *
 * That is not tidiness, it is the fix for two failures this project has already
 * paid for once each.
 *
 * 1. **`credentials: "include"` cannot be optional.** The session cookie is
 *    issued `SameSite=None; Secure` because the static site and the API are
 *    different origins (PRD section 6.5). A cross-origin `fetch` omits cookies
 *    by default and does so SILENTLY -- no console warning, no CORS error. Login
 *    appears to work (the OAuth redirect is a top-level navigation, so the
 *    cookie is set fine), and then every XHR afterwards 401s. Forgetting the
 *    flag in one call site out of thirty produces one mysteriously broken
 *    feature. Setting it here means it cannot be forgotten anywhere.
 *
 * 2. **The API's error text is the whole point.** This is a teaching artifact:
 *    "Something went wrong" tells a workshop attendee nothing, while
 *    "Unsupported file type '.zip'; expected one of ['.markdown', '.md',
 *    '.pdf', '.txt']" tells them exactly what the backend rejected and why.
 *    `readError` unwraps FastAPI's `detail` -- both the plain-string form and
 *    the 422 validation-array form -- so the real message reaches the screen.
 */

import type {
  ApiInit,
  AskResult,
  Conversation,
  ConversationDetail,
  EvalRun,
  EvalRunDetail,
  GoldenQuestion,
  GoldenQuestionInput,
  GoldenSetFileQuestion,
  Handout,
  HandoutDetail,
  HandoutRequest,
  TraceEvent,
} from "./types.ts";

/**
 * The ONLY configuration value the frontend receives.
 *
 * Never put a key of any kind in a `VITE_*` variable. Vite inlines them into
 * the JS bundle as string literals, so they are readable in devtools by anyone
 * who loads the page -- and this repository is PUBLIC, so they would also be
 * readable in git. Every credential (Gemini, Pinecone, Cohere, the OAuth client
 * secret) stays in FastAPI. See PRD section 7.
 */
const CONFIGURED_API_URL = import.meta.env.VITE_API_URL as string | undefined;

// Trailing slash stripped so `${API_URL}/api/...` never produces a double slash;
// Starlette treats `//api/agents` as a different path and 404s on it.
export const API_URL = (CONFIGURED_API_URL ?? "http://localhost:8000").replace(/\/+$/, "");

/** An error carrying the HTTP status, so callers can branch on 404 vs 409 vs 413. */
export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/**
 * Called whenever any request comes back 401.
 *
 * A module-level slot rather than a prop threaded through every view: the
 * session can expire during any call, in any component, and the response is
 * always the same -- drop local auth state and show Login. Registered once by
 * `App`.
 */
type UnauthorizedHandler = () => void;
let unauthorizedHandler: UnauthorizedHandler | null = null;

export function setUnauthorizedHandler(handler: UnauthorizedHandler | null): void {
  unauthorizedHandler = handler;
}

/** Pull the most specific message the response actually contains. */
async function readError(response: Response): Promise<string> {
  const raw = await response.text().catch(() => "");
  if (!raw) {
    return `HTTP ${response.status} ${response.statusText}`.trim();
  }

  try {
    const body = JSON.parse(raw) as { detail?: unknown };
    const detail = body.detail;

    if (typeof detail === "string") {
      return detail;
    }

    // FastAPI's 422 shape: [{ loc: ["body", "name"], msg: "...", type: "..." }].
    // Flattened rather than dumped as JSON, because the field name is the part
    // the user needs and it is buried in the middle of the raw structure.
    if (Array.isArray(detail)) {
      const parts = detail
        .map((item) => {
          const entry = item as { loc?: unknown[]; msg?: unknown };
          const field = Array.isArray(entry.loc) ? entry.loc.slice(1).join(".") : "";
          const message = typeof entry.msg === "string" ? entry.msg : "invalid value";
          return field ? `${field}: ${message}` : message;
        })
        .filter(Boolean);
      if (parts.length > 0) {
        return parts.join("; ");
      }
    }
  } catch {
    // Not JSON at all -- a proxy error page, or the API being down. The raw
    // body is still more informative than a generic string.
  }

  return raw.slice(0, 500);
}

/**
 * Issue one request and decode the response.
 *
 * Pass `json` for a JSON body (the header is set for you). Pass `body` directly
 * for `FormData` -- and note that no `Content-Type` is set in that case, which
 * is deliberate: the browser has to generate the multipart boundary itself, and
 * setting the header by hand produces a boundary-less content type that FastAPI
 * rejects as an unparseable body.
 */
export async function api<T>(path: string, init: ApiInit = {}): Promise<T> {
  const { json, headers: initHeaders, ...rest } = init;

  const headers = new Headers(initHeaders);
  let body = rest.body;
  if (json !== undefined) {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(json);
  }

  let response: Response;
  try {
    response = await fetch(`${API_URL}${path}`, {
      ...rest,
      body,
      headers,
      // See the module docstring. This line is why this wrapper exists.
      credentials: "include",
    });
  } catch (cause) {
    // Cancellation is an intentional user action, not a network failure. Keep
    // the native AbortError intact so the chat can restore the draft without
    // showing a misleading "Cannot reach the API" banner.
    if (cause instanceof DOMException && cause.name === "AbortError") {
      throw cause;
    }
    // A network-level failure (backend down, CORS preflight rejected, DNS).
    // `fetch` rejects with a bare "Failed to fetch", which names neither the
    // host nor the cause, so the URL is added here -- a wrong VITE_API_URL is
    // the single most likely reason a workshop attendee sees this.
    throw new ApiError(0, `Cannot reach the API at ${API_URL} (${String(cause)})`);
  }

  if (response.status === 401) {
    const detail = await readError(response);
    unauthorizedHandler?.();
    throw new ApiError(401, detail || "Your session has expired. Sign in again.");
  }

  if (!response.ok) {
    throw new ApiError(response.status, await readError(response));
  }

  // An empty body is decoded as `undefined` rather than handed to a JSON
  // parser. 204 is the obvious case, but 202 is the one that bites: the Stage 3
  // routes accept work and answer "started", and a backend that answers 202
  // with no body at all would make `response.json()` reject with a bare
  // "Unexpected end of JSON input" -- an error naming neither the route nor the
  // real problem, on a call that in fact succeeded.
  const text = await response.text();
  // Trimmed, because a body of a single newline is as empty as no body at all
  // and would otherwise take the parse path below.
  if (!text.trim()) {
    return undefined as unknown as T;
  }

  try {
    return JSON.parse(text) as T;
  } catch {
    // A 200 carrying HTML is a proxy or a wrong VITE_API_URL, not a bug in the
    // caller. Naming the path and quoting the body is what makes that findable.
    throw new ApiError(
      response.status,
      `Expected JSON from ${path} but got: ${text.slice(0, 200)}`,
    );
  }
}

// --------------------------------------------------------------------------
// Chat
// --------------------------------------------------------------------------

/**
 * The conversation routes, transcribed once.
 *
 * The other views build their paths inline, which is fine when a view owns one
 * endpoint. Chat owns six, split across two components -- the thread reads and
 * writes conversations, the trace panel reads a query's timeline -- and two of
 * them differ only in which id goes in the path. Writing
 * `/api/conversations/${id}/ask` by hand in one place and
 * `/api/agents/${id}/ask` in another is exactly the shape of mistake that
 * returns a 404 blamed on the backend.
 *
 * Everything here still goes through `api()`, so `credentials: "include"` and
 * the error unwrapping are not bypassed.
 */
export const chat = {
  /** Threads for one agent, most recently active first. */
  list: (agentId: string) =>
    api<Conversation[]>(`/api/agents/${encodeURIComponent(agentId)}/conversations`),

  /** One thread with its full history. */
  load: (conversationId: string) =>
    api<ConversationDetail>(`/api/conversations/${encodeURIComponent(conversationId)}`),

  rename: (conversationId: string, title: string) =>
    api<Conversation>(`/api/conversations/${encodeURIComponent(conversationId)}`, {
      method: "PATCH",
      json: { title },
    }),

  remove: (conversationId: string) =>
    api<{ ok: boolean }>(`/api/conversations/${encodeURIComponent(conversationId)}`, {
      method: "DELETE",
    }),

  /**
   * Ask inside an existing thread. Slow, and the range is wide: ~15 s for a
   * terse agent, 30-60 s for a coaching persona, because generation is
   * token-bound and a persona answer carries an analogy, a worked example and
   * a named gap. A follow-up adds ~4 s more for contextualisation before the
   * question is even embedded. That is why the caller shows elapsed time and a
   * named stage rather than a spinner.
   */
  ask: (conversationId: string, question: string, signal?: AbortSignal) =>
    api<AskResult>(`/api/conversations/${encodeURIComponent(conversationId)}/ask`, {
      method: "POST",
      json: { question },
      signal,
    }),

  /**
   * Ask with no thread yet. The server creates one and returns its id, so the
   * client never has to create an empty conversation up front and then decide
   * what to do with it if the question is never sent.
   */
  askNew: (agentId: string, question: string, signal?: AbortSignal) =>
    api<AskResult>(`/api/agents/${encodeURIComponent(agentId)}/ask`, {
      method: "POST",
      json: { question },
      signal,
    }),

  /** The decision timeline for one turn. */
  trace: (queryId: string) => api<TraceEvent[]>(`/api/trace/${encodeURIComponent(queryId)}`),
};

// --------------------------------------------------------------------------
// Evaluation (Stage 3)
// --------------------------------------------------------------------------

/**
 * The Stage 3 routes, transcribed once.
 *
 * Named `evaluation` and not `eval`: `eval` is not a legal binding name under
 * strict mode, which every ES module is, so `export const eval = {...}` is a
 * syntax error rather than a shadowing warning.
 *
 * Eleven endpoints across two resources, and the golden-set half is split
 * between agent-scoped paths (list, create, suggest, import, export) and
 * question-scoped ones (patch, delete) -- a shape that invites writing
 * `/api/agents/{id}/golden-questions/{qid}` by hand in one place and getting a
 * 404 blamed on the backend. Everything still goes through `api()`, so
 * `credentials: "include"` and the error unwrapping are not bypassed.
 */
export const evaluation = {
  /** One agent's set, in `order_index` order. Server-side filtering on
   *  `agent_id` is what keeps unscoped legacy rows out; this client never sees
   *  them. */
  goldenSet: (agentId: string) =>
    api<GoldenQuestion[]>(`/api/agents/${encodeURIComponent(agentId)}/golden-questions`),

  createQuestion: (agentId: string, input: GoldenQuestionInput) =>
    api<GoldenQuestion>(`/api/agents/${encodeURIComponent(agentId)}/golden-questions`, {
      method: "POST",
      json: input,
    }),

  /**
   * Ask the model to propose ten questions from this agent's own indexed
   * chunks. Answers **202** and does the work in the background, so the caller
   * polls `goldenSet` until they appear rather than awaiting them here.
   */
  suggestQuestions: (agentId: string) =>
    api<unknown>(`/api/agents/${encodeURIComponent(agentId)}/golden-questions/suggest`, {
      method: "POST",
    }),

  importQuestions: (agentId: string, questions: GoldenSetFileQuestion[]) =>
    api<GoldenQuestion[]>(`/api/agents/${encodeURIComponent(agentId)}/golden-questions/import`, {
      method: "POST",
      json: { questions },
    }),

  /**
   * A URL, not a request.
   *
   * Export is a plain `<a href>` to the endpoint, whose `Content-Disposition:
   * attachment` header does the saving. The alternative -- fetch the JSON,
   * wrap it in a Blob, click a synthetic `<a download>` -- is inert inside the
   * viewer sandbox, which blocks downloads a page starts itself. A normal link
   * is a navigation the browser owns, and it carries the session cookie because
   * that cookie is `SameSite=None; Secure` (PRD section 6.5).
   */
  exportUrl: (agentId: string) =>
    `${API_URL}/api/agents/${encodeURIComponent(agentId)}/golden-questions/export`,

  /** Edit any field. Saving a suggested question is what flips its `source` to
   *  "edited" -- server-side, so the response is the authority on the badge. */
  updateQuestion: (questionId: string, patch: Partial<GoldenQuestionInput>) =>
    api<GoldenQuestion>(`/api/golden-questions/${encodeURIComponent(questionId)}`, {
      method: "PATCH",
      json: patch,
    }),

  deleteQuestion: (questionId: string) =>
    api<{ ok: boolean }>(`/api/golden-questions/${encodeURIComponent(questionId)}`, {
      method: "DELETE",
    }),

  /**
   * Start a run. Answers **202** with the run row, which is why the caller can
   * begin polling immediately instead of guessing an id.
   *
   * Slow by construction: every question is a full agent turn (15 s bare, 30-60
   * s under a coaching persona, because generation is token-bound) plus four
   * judged calls. Ten questions is minutes, not seconds.
   */
  startRun: (agentId: string, notes: string | null) =>
    api<EvalRun>(`/api/agents/${encodeURIComponent(agentId)}/eval-runs`, {
      method: "POST",
      json: { notes },
    }),

  /** This agent's runs, newest first -- the "what changed since last run"
   *  history that is the whole reason `eval_runs` is persisted (PRD 4.4). */
  listRuns: (agentId: string) =>
    api<EvalRun[]>(`/api/agents/${encodeURIComponent(agentId)}/eval-runs`),

  loadRun: (runId: string) =>
    api<EvalRunDetail>(`/api/eval-runs/${encodeURIComponent(runId)}`),

  deleteRun: (runId: string) =>
    api<{ ok: boolean }>(`/api/eval-runs/${encodeURIComponent(runId)}`, { method: "DELETE" }),
};

// --------------------------------------------------------------------------
// Handouts
// --------------------------------------------------------------------------

/**
 * The generated-asset routes, transcribed once.
 *
 * **Every path carries the agent id**, including the two that address a single
 * handout by its own uuid. That is not repetition: nesting under `{agent_id}`
 * is what makes ownership structural rather than checked by hand (PRD section
 * 7). The three routes in this app that are reached by their own id --
 * conversations, golden questions, eval runs -- have to verify ownership in
 * Python, and they are the highest-risk lines in the backend. Handouts do not
 * join that list.
 *
 * Everything here goes through `api()`, so `credentials: "include"` and the
 * error unwrapping are not bypassed. `downloadUrl` is the one exception and it
 * is not a request at all -- see below.
 */
export const handouts = {
  /**
   * One agent's handouts, newest first.
   *
   * All three filters are optional and all three are applied server-side. The
   * panel reads the list twice with different filters -- once scoped to the
   * open thread, once unscoped for "All handouts" -- which is why
   * `conversationId` is a filter rather than something the client sorts out
   * from one big response.
   */
  list: (
    agentId: string,
    opts: { conversationId?: string; kind?: string; limit?: number } = {},
  ) => {
    // Built with URLSearchParams rather than string concatenation so the values
    // are escaped once, by the platform, and an absent filter contributes
    // nothing at all -- `?kind=undefined` is a filter the backend would try to
    // honour and match zero rows.
    const query = new URLSearchParams();
    if (opts.conversationId) query.set("conversation_id", opts.conversationId);
    if (opts.kind) query.set("kind", opts.kind);
    if (opts.limit !== undefined) query.set("limit", String(opts.limit));
    const encoded = query.toString();
    const suffix = encoded ? `?${encoded}` : "";
    return api<Handout[]>(
      `/api/agents/${encodeURIComponent(agentId)}/handouts${suffix}`,
    );
  },

  /** One handout with `preview_text` and `source_code`. Fetched when a row is
   *  expanded, never for the list: both fields can run to kilobytes and the
   *  list renders up to 200 rows. Same fetch-on-first-open shape as
   *  `TracePanel`. */
  load: (agentId: string, handoutId: string) =>
    api<HandoutDetail>(
      `/api/agents/${encodeURIComponent(agentId)}/handouts/${encodeURIComponent(handoutId)}`,
    ),

  /**
   * Ask for a handout. Answers **202** with the row already inserted at
   * `status: "pending"` -- the bytes are written afterwards by a background
   * job, so **the caller must poll `list` until the status changes** rather
   * than treating this response as a finished file. Nothing about the returned
   * row is downloadable yet: `byte_size` is 0 and the download route 409s.
   *
   * Polling stops when no row is pending, which is the shape `AgentEvaluate`
   * already uses for eval runs. A fixed interval that never stops is the
   * failure mode to avoid.
   *
   * A 409 here means the agent is at its handout quota. It is refused, never
   * silently evicted -- a panel that deletes the user's oldest deck to make
   * room for a chart is worse than one that says no.
   */
  create: (agentId: string, req: HandoutRequest) =>
    api<Handout>(`/api/agents/${encodeURIComponent(agentId)}/handouts`, {
      method: "POST",
      json: req,
    }),

  remove: (agentId: string, handoutId: string) =>
    api<{ ok: boolean }>(
      `/api/agents/${encodeURIComponent(agentId)}/handouts/${encodeURIComponent(handoutId)}`,
      { method: "DELETE" },
    ),

  /**
   * A URL, not a request -- exactly like `evaluation.exportUrl` above.
   *
   * The download is a plain `<a href download>`, a navigation the browser owns,
   * whose `Content-Disposition: attachment` header does the saving. The
   * alternative -- fetch the bytes, wrap them in a Blob, click a synthetic
   * link -- is inert inside the viewer sandbox, which blocks downloads a page
   * starts itself, and it would also pull megabytes of image data through JS
   * for no gain. The route is a cookie-authenticated GET and the session cookie
   * is `SameSite=None; Secure` (PRD section 6.5), so the link carries it.
   *
   * The same URL is the `src` of a chart's thumbnail. **Only once `status` is
   * "ready"**: pointed at a pending row it answers 409 and the `<img>` renders
   * broken.
   */
  downloadUrl: (agentId: string, handoutId: string) =>
    `${API_URL}/api/agents/${encodeURIComponent(agentId)}/handouts/${encodeURIComponent(handoutId)}/download`,
};
