/**
 * Login: one real path, and one that must never be mistaken for it.
 *
 * The Google button is a full-page navigation rather than a `fetch`. It has to
 * be: the OAuth flow ends with Google redirecting the browser back to
 * `/api/auth/google/callback`, and that response is what carries the
 * `Set-Cookie`. An XHR could not follow the consent screen, and a cookie set on
 * an XHR response to a redirect chain would not stick.
 *
 * The dev box below it is an authentication bypass. See the styling notes on
 * `DevLoginBox` for why it is deliberately unattractive.
 */

import { useState } from "react";
import type { FormEvent } from "react";
import { api, API_URL } from "../lib/api.ts";
import type { User } from "../lib/types.ts";
import { ErrorBanner, Spinner, errorMessage } from "../components/ui.tsx";

export default function Login({ onAuthenticated }: { onAuthenticated: (user: User) => void }) {
  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-950 px-6 py-16">
      <div className="w-full max-w-md">
        <header className="mb-8 text-center">
          <h1 className="text-3xl font-semibold tracking-tight text-slate-100">Agentic RAG</h1>
          <p className="mt-2 text-sm text-slate-400">
            NTU Harness Engineering &middot; retrieval over your own documents
          </p>
        </header>

        <div className="rounded-xl border border-slate-800 bg-slate-900/50 p-6">
          <a
            data-testid="login-google"
            href={`${API_URL}/api/auth/google/login`}
            className="flex w-full items-center justify-center gap-3 rounded-lg bg-slate-100 px-4 py-3 text-sm font-medium text-slate-900 transition hover:bg-white"
          >
            <GoogleMark />
            Sign in with Google
          </a>
          <p className="mt-4 text-center text-xs text-slate-500">
            We store your Google account ID, email and name. Google&rsquo;s access and
            refresh tokens are discarded immediately after the identity check.
          </p>
        </div>

        {/*
          `import.meta.env.DEV` is replaced by Vite with a literal `false` in a
          production build, so this entire subtree is removed by dead-code
          elimination -- it is not merely hidden at runtime. Combined with the
          backend's own three-way gate (opt-in flag, ENVIRONMENT=development,
          loopback client), the bypass has to fail twice independently before it
          could reach a deployed page.
        */}
        {import.meta.env.DEV && <DevLoginBox onAuthenticated={onAuthenticated} />}
      </div>
    </div>
  );
}

/**
 * The gated bypass, styled to look like what it is.
 *
 * Amber everywhere, a hazard-striped header, a literal "DEV ONLY" badge and a
 * sentence naming it an authentication bypass. This is not decoration: the one
 * genuine risk of a dev-login affordance is that it stops reading as
 * exceptional and someone starts using it as the convenient way in, or copies
 * the pattern into a screenshot that ends up in a deployment. It should be
 * impossible to confuse with the button above it, at a glance, from across a
 * room.
 */
function DevLoginBox({ onAuthenticated }: { onAuthenticated: (user: User) => void }) {
  const [email, setEmail] = useState("simoraj@gmail.com");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const user = await api<User>("/api/auth/dev-login", {
        method: "POST",
        json: { email: email.trim(), name: "Dev User" },
      });
      onAuthenticated(user);
    } catch (cause) {
      // A 404 here is the gate refusing, not a missing route -- the backend
      // answers 404 rather than 403 so a probe cannot tell the difference. Said
      // plainly, because otherwise the next twenty minutes go into debugging a
      // routing problem that does not exist.
      setError(
        `${errorMessage(cause)}\n\nA 404 means the backend refused: dev-login requires ` +
          `DEV_AUTH_ENABLED=true, ENVIRONMENT=development, and a request from localhost.`,
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      onSubmit={submit}
      className="mt-6 rounded-xl border-2 border-dashed border-amber-500/70 bg-amber-950/20 p-5"
    >
      <div className="mb-3 flex items-center gap-2">
        <span className="rounded bg-amber-500 px-2 py-0.5 text-xs font-bold tracking-wider text-amber-950">
          DEV ONLY
        </span>
        <span className="text-sm font-medium text-amber-200">Skip Google sign-in</span>
      </div>

      <p className="mb-4 text-xs leading-relaxed text-amber-200/80">
        This is an <strong>authentication bypass</strong>. It exists so the flow can be
        driven end to end without a human at a consent screen. It is disabled outside
        local development and stripped from production builds. Never rely on it.
      </p>

      <label className="block text-xs font-medium text-amber-200/90" htmlFor="dev-login-email">
        Email to sign in as
      </label>
      <input
        id="dev-login-email"
        data-testid="dev-login-email"
        type="email"
        required
        value={email}
        onChange={(event) => setEmail(event.target.value)}
        className="mt-1 w-full rounded-md border border-amber-700/60 bg-slate-950 px-3 py-2 text-sm text-amber-100 outline-none focus:border-amber-500"
      />

      <button
        type="submit"
        data-testid="dev-login-submit"
        disabled={busy}
        className="mt-3 w-full rounded-md bg-amber-500 px-4 py-2 text-sm font-semibold text-amber-950 transition hover:bg-amber-400 disabled:opacity-50"
      >
        {busy ? "Signing in…" : "Dev login"}
      </button>

      {busy && (
        <div className="mt-3">
          <Spinner label="Creating session" />
        </div>
      )}
      <div className="mt-3">
        <ErrorBanner error={error} />
      </div>
    </form>
  );
}

/** Google's mark, inlined as SVG so the page makes no third-party request. */
function GoogleMark() {
  return (
    <svg width="16" height="16" viewBox="0 0 48 48" aria-hidden="true">
      <path
        fill="#EA4335"
        d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"
      />
      <path
        fill="#4285F4"
        d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"
      />
      <path
        fill="#FBBC05"
        d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z"
      />
      <path
        fill="#34A853"
        d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"
      />
    </svg>
  );
}
