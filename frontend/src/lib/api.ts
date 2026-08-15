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

import type { ApiInit } from "./types.ts";

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

  if (response.status === 204) {
    return undefined as unknown as T;
  }

  return (await response.json()) as T;
}
