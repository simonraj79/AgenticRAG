/**
 * The Better Auth client, and the JWT the FastAPI service is given instead.
 *
 * TWO CREDENTIALS, ONE SIGN-IN, AND THE SPLIT IS THE WHOLE DESIGN.
 *
 *   session cookie   set by Better Auth, httpOnly, 14 days, SAME ORIGIN as this
 *                    page. Never readable by JavaScript, never sent to the API.
 *   JWT              short-lived, fetched with that cookie, sent to the API in
 *                    an `Authorization` header.
 *
 * The reason for two is the reason this whole service exists. `onrender.com` is
 * on the Public Suffix List, so the SPA's host and the API's host are different
 * SITES, not merely different origins -- a cookie sent to the API is
 * third-party, and Safari, Incognito, Brave and strict-mode Firefox drop it.
 * That is the "signs in and lands back on the login page" bug.
 *
 * A header is not a cookie. It carries no site, no partition and no browser
 * policy, so it crosses to the API unconditionally. The long-lived credential
 * stays in an httpOnly cookie where XSS cannot reach it; only a token measured
 * in minutes is ever exposed to JavaScript.
 *
 * WHICH IS WHY THIS CACHE IS IN A MODULE VARIABLE AND NOT IN localStorage.
 * Storing it would persist a bearer credential where any injected script can
 * read it at leisure, in exchange for saving one request per page load. A
 * module variable dies with the tab, and the cookie -- which no script can read
 * -- is what survives a refresh.
 */

import { createAuthClient } from "better-auth/react";
import { jwtClient } from "better-auth/client/plugins";

/**
 * Same origin as the page, always. Not configurable, and that is deliberate:
 * a `VITE_AUTH_URL` pointing anywhere else would make the session cookie
 * third-party again and silently reintroduce the exact bug this arrangement
 * removes. The one legitimate deployment shape is "auth serves the SPA", and
 * `window.location.origin` states that structurally rather than by convention.
 */
export const AUTH_URL =
  typeof window === "undefined" ? "http://localhost:5173" : window.location.origin;

export const authClient = createAuthClient({
  baseURL: AUTH_URL,
  plugins: [jwtClient()],
});

/** Refresh this many ms before `exp`, so a token never expires mid-flight. */
const REFRESH_MARGIN_MS = 60_000;

let cached: { token: string; expiresAt: number } | null = null;

/**
 * De-duplicates concurrent refreshes. The app fires several requests on mount
 * -- `/api/auth/me`, the agent list, the document list -- and without this each
 * one would independently fetch a token. Same shape as `JwksCache`'s lock on
 * the Python side, and the same reason: one cold start, many callers.
 */
let inflight: Promise<string | null> | null = null;

/**
 * Read `exp` out of the token without verifying it.
 *
 * Unverified is FINE here and would not be fine anywhere else. This value is
 * used only to decide when to ask for a new token; the API verifies the
 * signature itself and would reject anything forged. A client that trusted this
 * for an access decision would be the bug -- so nothing here returns a claim,
 * only a timestamp.
 */
function expiryOf(token: string): number {
  try {
    const payload = token.split(".")[1];
    if (!payload) return 0;
    const json = atob(payload.replace(/-/g, "+").replace(/_/g, "/"));
    const exp = (JSON.parse(json) as { exp?: number }).exp;
    return typeof exp === "number" ? exp * 1000 : 0;
  } catch {
    // A token we cannot parse is still usable -- the API is the authority. Zero
    // means "assume it is about to expire", so it is refetched next call rather
    // than cached forever.
    return 0;
  }
}

async function fetchToken(): Promise<string | null> {
  try {
    const result = (await authClient.token()) as
      | { data?: { token?: string } | null; error?: unknown }
      | { token?: string }
      | null;

    // The plugin has returned both shapes across versions. Accepting either
    // costs one line and turns a silent "everything is 401" into a non-event.
    const token =
      (result as { data?: { token?: string } })?.data?.token ??
      (result as { token?: string })?.token ??
      null;

    if (!token) return null;
    cached = { token, expiresAt: expiryOf(token) };
    return token;
  } catch {
    // No session, or the auth service is unreachable. Both mean "this request
    // goes out unauthenticated", which the API answers with a 401 and the app
    // already knows how to handle. Throwing here would turn a signed-out state
    // into a crash.
    return null;
  }
}

/**
 * A currently-valid JWT, or null when nobody is signed in.
 *
 * Null is not an error and must not be treated as one: an anonymous request is
 * legitimate (the landing page makes them), and during the cutover the API also
 * still accepts the old session cookie.
 */
export async function getAuthToken(): Promise<string | null> {
  if (cached && Date.now() < cached.expiresAt - REFRESH_MARGIN_MS) {
    return cached.token;
  }
  if (inflight) return inflight;

  inflight = fetchToken().finally(() => {
    inflight = null;
  });
  return inflight;
}

/**
 * Drop the cached token. Call on sign-out.
 *
 * Without this, a signed-out tab keeps presenting a valid-looking bearer token
 * until it expires -- and the API would keep honouring it, because a JWT is
 * valid until `exp` by construction and this one was genuinely issued. That is
 * the standing trade of stateless tokens, and it is why `signOut` below clears
 * the cache in the same breath as revoking the session.
 */
export function clearAuthToken(): void {
  cached = null;
  inflight = null;
}

/**
 * Start the Google flow. Returns to `callbackURL` on success.
 *
 * THIS FUNCTION'S JOB IS TO FAIL LOUDLY, AND THAT IS NOT DEFENSIVE PADDING.
 *
 * `authClient.signIn.social` does NOT throw when sign-in fails. Better Auth's
 * client resolves with `{ data, error }` -- so a `403 Invalid Origin`, a cold
 * auth service, or a 200 from a host that has no auth service at all are all
 * ORDINARY FULFILLED PROMISES. `await` returns, nothing navigates, and the
 * caller's `.catch()` never runs. The button in `Login.tsx` then sits on
 * "Redirecting to Google..." for ever with no message, which is what was
 * actually shipped and what was measured in a browser on 2026-08-27.
 *
 * The specific failure was that the OLD static frontend is still live and
 * still serving the CURRENT bundle. It has no `/api/auth/*`, so its SPA
 * fallback answers the sign-in POST with a 200 and an empty body: no error
 * exists anywhere in the system, and nothing happens. Every error-shaped check
 * passed.
 *
 * So the guard below triggers on THE ABSENCE OF THE OUTCOME -- "did we get a
 * URL to send the browser to" -- and never on the presence of an error. That
 * is the standing rule for anything here whose failure mode is silence.
 *
 * The navigation is also performed HERE rather than left to Better Auth's
 * built-in `redirect` fetch plugin. That plugin does the same assignment
 * inside a bare `try {} catch {}` (client/fetch-plugins.mjs), so if it ever
 * fails or is dropped from a future default set, the failure is again silent.
 * Assigning a second time to the same URL is harmless -- the navigation is
 * already in flight and the URL is identical -- and it makes the one thing
 * this function exists to do visible in this file.
 */
export async function signInWithGoogle(callbackURL = "/"): Promise<void> {
  const result = (await authClient.signIn.social({ provider: "google", callbackURL })) as
    | { data?: { url?: string } | null; error?: { message?: string; status?: number } | null }
    | { url?: string }
    | null;

  const failure = (result as { error?: { message?: string; status?: number } | null })?.error;
  if (failure) {
    const status = failure.status ? ` (HTTP ${failure.status})` : "";
    throw new Error(`${failure.message ?? "The auth service rejected the sign-in"}${status}`);
  }

  // Both shapes accepted, exactly as `fetchToken` above accepts both: the
  // plugin has returned `{ data: { url } }` and a flat `{ url }` across
  // versions, and tolerating either costs one line where guessing wrong costs
  // a login that silently does nothing.
  const url =
    (result as { data?: { url?: string } })?.data?.url ??
    (result as { url?: string })?.url ??
    null;

  if (!url) {
    // Naming the origin IS the diagnosis. This request is same-origin by
    // construction (see AUTH_URL above), so "no Google URL came back" means
    // this page is not being served by the auth service -- a stale bookmark to
    // a previous frontend host, most likely. A message that omitted the origin
    // would send the reader to debug Google, which is working perfectly.
    // `window.location.origin` rather than the module-level `AUTH_URL`: they
    // are the same string by construction, but this one is read at the moment
    // of failure and therefore cannot be stale.
    throw new Error(
      `${window.location.origin} did not return a Google sign-in URL. This page must be served by ` +
        `the auth service itself; if you have followed an old link, open the app at ` +
        `its current address and try again.`,
    );
  }

  window.location.href = url;
}

/** Revoke the Better Auth session AND drop the cached bearer token. */
export async function signOut(): Promise<void> {
  try {
    await authClient.signOut();
  } finally {
    // In `finally` so a failed network call cannot leave a live token cached in
    // a tab whose user believes they have signed out.
    clearAuthToken();
  }
}
