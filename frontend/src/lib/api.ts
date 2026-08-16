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
  Agent,
  AgentPatch,
  ApiInit,
  AskResult,
  AskStreamAnswerReset,
  AskStreamEvent,
  AskStreamPhase,
  AskStreamStart,
  AskStreamToolCall,
  AskStreamToolError,
  AskStreamToolResult,
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
    /*
      A file that moved between being PICKED and being SENT never leaves the
      browser, and blaming the network for it sends the reader to the wrong
      place entirely.

      `fetch` streams a `File` from disk at request time, not at pick time, so a
      body containing one can fail locally -- moved, renamed, deleted, or the
      permission withdrawn. The browser surfaces that as `NotReadableError` or
      `NotFoundError`. The duplicate-upload retry makes this reachable rather
      than theoretical: the user picks a file, gets the 409 prompt, goes to look
      at the file, and comes back to press "Upload it again anyway".

      Reported before the generic branch because the generic branch would name
      the API host for a failure that never touched it.
    */
    const name = cause instanceof DOMException ? cause.name : "";
    if (name === "NotReadableError" || name === "NotFoundError") {
      throw new ApiError(
        0,
        "That file could not be read from disk. It may have been moved, renamed or " +
          "deleted since you picked it. Choose it again.",
      );
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
// Agents
// --------------------------------------------------------------------------

/**
 * Read and retune one agent.
 *
 * `get` is a thin wrapper over the request `AgentDetail` was already making
 * inline, and it is here so the two cannot drift: `update` returns the same
 * `AgentOut` shape, so a settings screen swaps the response straight into
 * whatever `get` filled. Two hand-written paths that must agree on a response
 * type is the shape of mistake this file exists to prevent.
 *
 * **Errors are not wrapped, and that is a decision rather than an omission.**
 * `api()` already throws `ApiError` carrying both the server's `detail` string
 * and the HTTP status, so a caller renders `errorMessage(cause)` like every
 * other view and branches with `cause instanceof ApiError && cause.status ===
 * 409` where it needs to. Three statuses are worth telling apart here and all
 * three arrive with a usable message:
 *
 * - **409** -- the new name collides with another of this owner's agents. The
 *   only one the user fixes by editing the field they just touched, so it is
 *   the one worth attaching to the name input rather than to a banner.
 * - **422** -- an explicit null at a NOT NULL column, an unknown key (see
 *   `AgentPatch`), or `chunk_overlap >= chunk_size`. The last is evaluated
 *   against the MERGED configuration, so a patch that sends only
 *   `chunk_overlap` can be refused for a `chunk_size` it never mentioned; the
 *   detail names the fields.
 * - **400** -- an attempt to change `embedding_model`. `AgentPatch` cannot
 *   express it, so this should be unreachable from this client.
 */
export const agents = {
  /** One agent, including `document_count`. */
  get: (agentId: string) => api<Agent>(`/api/agents/${encodeURIComponent(agentId)}`),

  /**
   * Apply a partial config change and get the whole updated record back.
   *
   * **Send only the keys that changed.** The server applies exactly what it
   * receives, so a screen that posts every field it rendered will overwrite
   * values another tab may have moved, and -- worse -- will send `null` for
   * every field it has no value for, which is a 422 rather than a no-op.
   *
   * Retuning chunking here does NOT re-chunk what is already indexed: existing
   * vectors keep the size they were built with and the new value applies to the
   * next upload, so an agent can hold two chunkings at once. A settings UI has
   * to say "applies to new uploads" next to those fields, because nothing in
   * the response reveals it.
   */
  update: (agentId: string, patch: AgentPatch) =>
    api<Agent>(`/api/agents/${encodeURIComponent(agentId)}`, {
      method: "PATCH",
      json: patch,
    }),
};

// --------------------------------------------------------------------------
// Streaming one turn
// --------------------------------------------------------------------------

/**
 * What the caller wants told, as it happens.
 *
 * **Callbacks rather than an async iterator, and that is a correctness decision
 * rather than a style one.** The caller is a React component that must apply its
 * address guard -- "is the user still on the thread this turn was sent to?" --
 * to every single frame, roughly two hundred times a turn instead of once. A
 * `for await` loop in an event handler is a second place for that guard to be
 * forgotten, and forgetting it streams one thread's answer into another thread's
 * transcript.
 *
 * Every handler is optional. A caller that supplies none still gets the awaited
 * `AskResult`, which is exactly the non-streaming behaviour -- and that is the
 * degradation path if a frame type is never emitted.
 */
export type AskStreamHandlers = {
  onStart?: (event: AskStreamStart) => void;
  onPhase?: (event: AskStreamPhase) => void;
  onTool?: (event: AskStreamToolCall | AskStreamToolResult | AskStreamToolError) => void;
  /** ONE delta. Concatenate verbatim; never trim, never re-encode. */
  onToken?: (delta: string) => void;
  onAnswerReset?: (event: AskStreamAnswerReset) => void;
};

const SSE_CONTENT_TYPE = "text/event-stream";

/**
 * Parse one SSE frame into an event, or `null` if it carries nothing.
 *
 * `:` lines are comments -- the ten-second heartbeat that keeps an intermediary
 * from closing the connection through the five-to-eight second gap before the
 * first token -- and must be ignored without being counted as an event. `id:`
 * and `retry:` are ignored too; the server sends neither.
 *
 * **Dispatch is on the payload's `type`, not on the `event:` header.** The
 * contract guarantees they are equal, so one of them is redundant, and the body
 * is the one that also carries `seq` and the fields. A frame may legally carry
 * several `data:` lines, which join with a newline -- the server sends one,
 * because `JSON.stringify` escapes newlines, which is the standard SSE trap.
 */
function parseFrame(frame: string): AskStreamEvent | null {
  const data: string[] = [];

  for (const line of frame.split("\n")) {
    if (line === "" || line.startsWith(":")) continue;
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    if (field !== "data") continue;
    const value = colon === -1 ? "" : line.slice(colon + 1);
    // One optional leading space after the colon is part of the framing, not
    // part of the value -- stripping more would corrupt a token delta.
    data.push(value.startsWith(" ") ? value.slice(1) : value);
  }

  if (data.length === 0) return null;

  try {
    const body = JSON.parse(data.join("\n")) as { type?: unknown };
    // An event with no `type` is unroutable. An event with an UNKNOWN type is
    // routable and ignored, which is the rule this codebase applies to every
    // loose string the backend sends: render nothing, never throw.
    if (typeof body.type !== "string") return null;
    return body as AskStreamEvent;
  } catch {
    return null;
  }
}

/** Hand one non-terminal frame to its handler. `done` and `error` never reach
 *  here -- they end the stream, so the read loop consumes them itself. */
function dispatch(event: AskStreamEvent, handlers: AskStreamHandlers): void {
  switch (event.type) {
    case "start":
      handlers.onStart?.(event);
      return;
    case "phase":
      handlers.onPhase?.(event);
      return;
    case "tool_call":
    case "tool_result":
    case "tool_error":
      handlers.onTool?.(event);
      return;
    case "token":
      // Guarded because this string is concatenated into what the user reads,
      // and `String(undefined)` in the middle of an answer is a defect that
      // renders perfectly.
      if (typeof event.text === "string") handlers.onToken?.(event.text);
      return;
    case "answer_reset":
      handlers.onAnswerReset?.(event);
      return;
    default:
      return;
  }
}

/**
 * Ask a question and read the answer as it is written.
 *
 * **`fetch` + `ReadableStream` + a hand-written frame parser, not
 * `EventSource`.** That is not a preference; `EventSource` is disqualified four
 * times over, and each disqualification is a feature of this file:
 *
 * 1. It is GET-only. A 4,000-character question would go in the URL, and
 *    therefore into every access log along the way.
 * 2. It exposes no HTTP status and no response body on failure, so `ApiError`,
 *    `readError` and the 401 -> sign-out path are all impossible. An expired
 *    session becomes indistinguishable from a dropped connection.
 * 3. `.close()` rejects nothing, so the awaiting code never learns that the user
 *    cancelled -- and the chat branches on exactly the native `AbortError` that
 *    `api()` above is careful to rethrow.
 * 4. It auto-reconnects with `Last-Event-ID`. On a turn that writes database rows
 *    and bills tokens, a transparent retry re-runs the whole generation.
 *
 * Everything `api()` guarantees is repeated here rather than bypassed:
 * `credentials: "include"` (the session cookie is `SameSite=None; Secure` and a
 * cross-origin fetch drops it SILENTLY), `readError` for a non-2xx, the 401
 * handler, and the native `AbortError` passed through untouched.
 *
 * Resolves with the `AskResult` from the terminal `done` frame.
 *
 * `onMissingRoute` is called instead of throwing when the route itself answers
 * 404 -- see the callers. It is deliberately checked HERE, at the only point
 * where "this backend has no such path" is distinguishable, and never against a
 * 404 that arrives as an `error` frame: by then a turn has already started, and
 * re-issuing it against the JSON route would run it a second time.
 */
async function apiStream(
  path: string,
  question: string,
  handlers: AskStreamHandlers,
  signal?: AbortSignal,
  onMissingRoute?: () => Promise<AskResult>,
): Promise<AskResult> {
  let response: Response;
  try {
    response = await fetch(`${API_URL}${path}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        // Not content negotiation -- the route speaks SSE whatever this says.
        // Sent because an intermediary that inspects it has one more reason not
        // to buffer the response in order to compress it.
        Accept: SSE_CONTENT_TYPE,
      },
      body: JSON.stringify({ question }),
      credentials: "include",
      signal,
    });
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === "AbortError") throw cause;
    throw new ApiError(0, `Cannot reach the API at ${API_URL} (${String(cause)})`);
  }

  // Every failure that happens BEFORE the first byte is an ordinary HTTP status
  // with FastAPI's usual body, and is handled exactly as `api()` handles it.
  // Only a failure after the headers has to travel as an `error` frame.
  if (response.status === 401) {
    const detail = await readError(response);
    unauthorizedHandler?.();
    throw new ApiError(401, detail || "Your session has expired. Sign in again.");
  }
  if (!response.ok) {
    // The static site and the API deploy separately, so a browser holding a
    // fresh bundle against an API that has not restarted yet is an ordinary
    // Tuesday. Nothing has run at this point -- no turn, no row, no tokens --
    // which is what makes re-asking on the JSON route free. A 404 that really
    // means "no such agent" simply arrives twice, with the same message.
    if (response.status === 404 && onMissingRoute) return onMissingRoute();
    throw new ApiError(response.status, await readError(response));
  }

  /*
    The fallback, and its trigger is the ABSENCE OF A STREAM rather than the
    presence of an error.

    A 200 on this path carrying an ordinary JSON body -- a gateway that
    materialises streams, a deployment where something else answered -- throws
    nothing at all and has exactly one thing wrong with it: the header saying
    what it is. Branching on "did an exception happen" sails past that and then
    fails deep inside the frame parser with a message about JSON.

    Note what this does NOT catch, because it is a different failure with a
    different fix: an intermediary that BUFFERS still sends `text/event-stream`,
    and every frame simply arrives in one late read. That degrades to the old
    wait rather than breaking, which is why it is fought with `no-transform` and
    `X-Accel-Buffering: no` on the response instead of detected here.
  */
  const contentType = (response.headers.get("content-type") ?? "").toLowerCase();
  if (!response.body || !contentType.includes(SSE_CONTENT_TYPE)) {
    const text = await response.text();
    try {
      return JSON.parse(text) as AskResult;
    } catch {
      throw new ApiError(
        response.status,
        `Expected an answer stream from ${path} but got: ${text.slice(0, 200)}`,
      );
    }
  }

  const reader = response.body.getReader();
  // `{ stream: true }` on every decode, without exception. A multi-byte
  // character split across two network chunks decodes to U+FFFD otherwise --
  // which is not an error, does not warn, and puts a replacement character in
  // the middle of an answer.
  const decoder = new TextDecoder();
  let buffer = "";
  let result: AskResult | null = null;
  let failure: ApiError | null = null;

  try {
    reading: for (;;) {
      const { value, done } = await reader.read();
      if (done) {
        buffer += decoder.decode();
        break;
      }
      buffer += decoder.decode(value, { stream: true });
      // Normalised so the frame split is one rule rather than two. A `\r` that
      // arrives at the end of a chunk with its `\n` in the next one is joined
      // here on the following pass, because the whole remaining buffer is
      // re-scanned -- and the buffer only ever holds the unparsed tail.
      if (buffer.includes("\r")) buffer = buffer.replace(/\r\n/g, "\n");

      for (;;) {
        const split = buffer.indexOf("\n\n");
        // The trailing partial frame stays in the buffer. A frame can and will
        // split across network chunks; parsing what has arrived so far would
        // silently drop the second half of it.
        if (split === -1) break;

        const frame = buffer.slice(0, split);
        buffer = buffer.slice(split + 2);

        const event = parseFrame(frame);
        if (!event) continue;

        if (event.type === "done") {
          result = event.result;
          break reading;
        }
        if (event.type === "error") {
          if (event.status === 401) unauthorizedHandler?.();
          failure = new ApiError(event.status, event.detail);
          break reading;
        }
        dispatch(event, handlers);
      }
    }
  } finally {
    // Not awaited: the terminal frame has already been read, the turn is durable
    // server-side, and there is nothing to wait for. Rejections are swallowed
    // because cancelling an already-finished or already-aborted body is not an
    // event anybody needs told about -- an unhandled rejection here would fail
    // the zero-console-errors check for a stream that worked.
    reader.cancel().catch(() => {});
  }

  if (failure) throw failure;
  if (result) return result;

  /*
    The stream ended with neither terminal frame.

    Again the trigger is the absence of the outcome, not the presence of an
    error: a connection an intermediary closed at its idle timeout, or a worker
    that died mid-turn, both end the body cleanly with `done: true` and throw
    nothing at all. Reported rather than resolved with a half-built answer,
    because the turn may well be finishing server-side and a reload is the
    honest instruction.
  */
  throw new ApiError(
    0,
    "The answer stream ended before the turn finished. It may still be recorded -- reload the conversation to check.",
  );
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

  /**
   * The same two questions, streamed.
   *
   * Separate routes rather than a `?stream=` flag on the two above, and the two
   * JSON handlers are not edited at all. A flag would force `response_model`
   * off the JSON handler and delete the validation that makes the terminal
   * payload byte-identical to the one-shot body -- which is the single property
   * everything else here rests on.
   *
   * **Both fall back to the one-shot route when the streaming route answers
   * 404**, and the trigger is the absence of the route rather than an error:
   * falling back keeps the chat working un-streamed against an API that has not
   * restarted yet, where not falling back turns a deploy ordering into a dead
   * product. It fires only BEFORE the stream opens -- `apiStream` takes it as a
   * callback for exactly that reason, so a 404 arriving as an `error` frame,
   * with a turn already underway, can never re-run it.
   */
  askStream: (
    conversationId: string,
    question: string,
    handlers: AskStreamHandlers,
    signal?: AbortSignal,
  ): Promise<AskResult> =>
    apiStream(
      `/api/conversations/${encodeURIComponent(conversationId)}/ask/stream`,
      question,
      handlers,
      signal,
      () => chat.ask(conversationId, question, signal),
    ),

  askNewStream: (
    agentId: string,
    question: string,
    handlers: AskStreamHandlers,
    signal?: AbortSignal,
  ): Promise<AskResult> =>
    apiStream(
      `/api/agents/${encodeURIComponent(agentId)}/ask/stream`,
      question,
      handlers,
      signal,
      () => chat.askNew(agentId, question, signal),
    ),

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
