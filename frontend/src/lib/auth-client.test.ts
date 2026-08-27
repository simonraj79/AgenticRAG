/**
 * `signInWithGoogle`, and the one property that matters about it: it must FAIL
 * LOUDLY when the browser did not go to Google.
 *
 * THE BUG THIS FILE EXISTS FOR, measured in a browser on 2026-08-27.
 *
 * Better Auth's client does NOT throw on failure -- `signIn.social` resolves
 * with `{ data, error }`. So a sign-in that goes nowhere is an ordinary
 * fulfilled promise, `Login.tsx`'s `.catch()` never runs, `setBusy(false)`
 * never runs, and the button sits on "Redirecting to Google..." for ever with
 * no message. The page renders perfectly with no product on it.
 *
 * The specific way it happened is worth keeping, because no error existed
 * anywhere: the OLD static frontend (`agentic-rag-web-e9e9.onrender.com`) is
 * still live and still serving the CURRENT bundle, and that host has no
 * `/api/auth/*` -- the SPA fallback answers the sign-in POST with a 200 and an
 * empty body. Better Auth reads a 200, so `error` is null; there is no `url`,
 * so nothing navigates. Every error-shaped check passes.
 *
 * Hence these cases assert the ABSENCE OF THE OUTCOME rather than the presence
 * of an error, which is this repository's standing rule for anything whose
 * failure mode is silence (CLAUDE.md, `new features/loop.md` T2).
 *
 * Case ids referenced by the change set:
 *   A1  an error object -- resolved, not thrown -- must reject
 *   A2  a 200 carrying no url (the shipped bug) must reject
 *   A3  a null/undefined return must reject
 *   A4  the happy path must resolve AND must have navigated
 *   A5  the flat `{ url }` shape must be accepted too
 *   A6  the message must name the origin, because that is the diagnosis
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

/**
 * Mocked at the module boundary rather than by faking `fetch`. The subject is
 * how this module reads Better Auth's RETURN VALUE, so the return value is the
 * thing to control; going through the real client would test better-fetch's
 * parser instead, which is not ours and is not where the bug was.
 */
const socialMock = vi.fn();

vi.mock("better-auth/react", () => ({
  createAuthClient: () => ({
    signIn: { social: socialMock },
    signOut: vi.fn(),
    token: vi.fn(),
  }),
}));

vi.mock("better-auth/client/plugins", () => ({ jwtClient: () => ({}) }));

const { signInWithGoogle } = await import("./auth-client.ts");

/**
 * jsdom refuses a real navigation with "Not implemented: navigation", so
 * `location` is replaced with a plain object that records the assignment. The
 * assertion is on that record: "did this module send the browser anywhere",
 * which is the outcome the whole flow exists to produce.
 */
let assigned: string | null = null;

beforeEach(() => {
  socialMock.mockReset();
  assigned = null;
  Object.defineProperty(window, "location", {
    configurable: true,
    value: {
      origin: "https://agentic-rag-web-e9e9.onrender.com",
      get href() {
        return assigned ?? "https://agentic-rag-web-e9e9.onrender.com/";
      },
      set href(value: string) {
        assigned = value;
      },
    },
  });
});

const GOOGLE = "https://accounts.google.com/o/oauth2/v2/auth?client_id=x";

describe("signInWithGoogle", () => {
  it("A1 rejects when Better Auth RESOLVES with an error object", async () => {
    // Not a thrown error -- a fulfilled promise carrying one. This is the shape
    // a 403 Invalid Origin arrives in, and the old code returned normally.
    socialMock.mockResolvedValue({
      data: null,
      error: { status: 403, message: "Invalid origin" },
    });

    await expect(signInWithGoogle("/")).rejects.toThrow(/Invalid origin/);
    expect(assigned).toBeNull();
  });

  it("A2 rejects when the call SUCCEEDS but carries no url", async () => {
    // The shipped bug, exactly: a 200 from a host with no auth service. No
    // error anywhere, and nothing happens. If this case ever goes green
    // without the guard, the infinite spinner is back.
    socialMock.mockResolvedValue({ data: null, error: null });

    await expect(signInWithGoogle("/")).rejects.toThrow(/did not return a Google/i);
    expect(assigned).toBeNull();
  });

  it("A3 rejects when the client returns nothing at all", async () => {
    socialMock.mockResolvedValue(undefined);
    await expect(signInWithGoogle("/")).rejects.toThrow();
    expect(assigned).toBeNull();
  });

  it("A4 resolves on the happy path, and the browser actually left", async () => {
    // The pair matters. A guard that rejected everything would pass A1-A3 and
    // break sign-in; only this case notices.
    socialMock.mockResolvedValue({ data: { url: GOOGLE, redirect: true }, error: null });

    await expect(signInWithGoogle("/agents")).resolves.toBeUndefined();
    expect(assigned).toBe(GOOGLE);
    expect(socialMock).toHaveBeenCalledWith({
      provider: "google",
      callbackURL: "/agents",
    });
  });

  it("A5 accepts the flat { url } shape as well as { data: { url } }", async () => {
    // `fetchToken` in this same module already carries the note that the
    // plugin has returned both shapes across versions. Same tolerance here,
    // for the same one-line price.
    socialMock.mockResolvedValue({ url: GOOGLE, redirect: true });

    await expect(signInWithGoogle("/")).resolves.toBeUndefined();
    expect(assigned).toBe(GOOGLE);
  });

  it("A6 names the origin in the failure, because that IS the diagnosis", async () => {
    // The bug was "you are on the wrong host". A message that does not say
    // which host sends the reader to debug Google instead.
    socialMock.mockResolvedValue({ data: null, error: null });

    await expect(signInWithGoogle("/")).rejects.toThrow(
      /agentic-rag-web-e9e9\.onrender\.com/,
    );
  });
});
