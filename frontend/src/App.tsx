/**
 * The shell: who is signed in, and which view is on screen.
 *
 * **No router.** `react-router` is not a dependency of this project and adding
 * one buys nothing here: there are three views, the deep-linkable one (an
 * agent) is reachable in a single click from the dashboard, and the OAuth
 * round trip always lands back at `/` regardless. A discriminated union in
 * `useState` is the whole routing layer, and it is legible in one screen --
 * which for a teaching artifact is worth more than URL persistence. If shared
 * agent links ever become a requirement, that is the moment to add the
 * dependency, not before.
 *
 * **Auth is bootstrapped, not assumed.** The session cookie is httpOnly, so
 * JavaScript cannot read it and cannot know whether it is valid. The only way
 * to find out is to ask: `GET /api/auth/me` on mount, 200 means signed in, 401
 * means show Login. Every later 401 from any call routes to the same place via
 * the handler registered below, so an expired session anywhere in the app ends
 * at the login screen instead of at a broken page.
 *
 * **A 401 and a failed request are different answers, and conflating them cost
 * a real debugging session.** This bootstrap used to `.catch()` everything and
 * fall through to Login, on the reasoning that a 401 here "is not an error, it
 * is the answer to the question". That is true of a 401 and false of everything
 * else: `lib/api.ts` throws `ApiError(0, "Cannot reach the API...")` when fetch
 * itself rejects, so a timeout, a DNS blip or a backend restart also rendered
 * the login screen -- telling a signed-in user their session was gone, when in
 * fact the question was never asked. The cookie is still in the jar, so a manual
 * reload fixes it, which is exactly what makes the bug look like a flaky login
 * rather than a bug.
 *
 * 401 means *nobody is signed in* -> Login.
 * Anything else means *we could not find out* -> say so, and offer Retry.
 */

import { useCallback, useEffect, useState } from "react";
import { ApiError, api, setUnauthorizedHandler } from "./lib/api.ts";
import type { User } from "./lib/types.ts";
import { ErrorBanner, Spinner, errorMessage } from "./components/ui.tsx";
import Login from "./views/Login.tsx";
import Dashboard from "./views/Dashboard.tsx";
import AgentDetail from "./views/AgentDetail.tsx";
import Admin from "./views/Admin.tsx";

type View =
  | { kind: "dashboard" }
  | { kind: "agent"; agentId: string }
  // Admin is a view like any other, not a route: this app has no router by
  // choice (see the header above). The 403 from `require_admin` is the access
  // control -- the nav entry below is hidden from non-admins only because a
  // link that always fails is bad UI, never as a security measure.
  | { kind: "admin" };

export default function App() {
  const [user, setUser] = useState<User | null>(null);
  const [booting, setBooting] = useState(true);
  const [view, setView] = useState<View>({ kind: "dashboard" });
  const [error, setError] = useState<string | null>(null);
  // Distinct from `error`, which banners a failure inside the signed-in shell.
  // This one means the session question could not be ASKED, so there is no
  // shell to banner it in yet.
  const [bootError, setBootError] = useState<string | null>(null);

  // Registered before the first request goes out, and re-registered on every
  // mount (StrictMode mounts twice in development; the second registration
  // simply replaces the first).
  useEffect(() => {
    setUnauthorizedHandler(() => {
      setUser(null);
      setView({ kind: "dashboard" });
    });
    return () => setUnauthorizedHandler(null);
  }, []);

  // `loadSession` rather than an inline effect body, because Retry below has to
  // run exactly the same thing. A second copy of this that drifted from the
  // first would be a bug nobody could see.
  const loadSession = useCallback(async (signal?: { cancelled: boolean }) => {
    const live = () => !signal?.cancelled;
    if (live()) {
      setBooting(true);
      setBootError(null);
    }
    try {
      const me = await api<User>("/api/auth/me");
      if (live()) setUser(me);
    } catch (cause) {
      // 401 is the ANSWER to the question, not a failure to ask it: nobody is
      // signed in, so fall through to Login with no error shown.
      if (cause instanceof ApiError && cause.status === 401) {
        if (live()) setUser(null);
      } else if (live()) {
        // Anything else -- ApiError(0) from a rejected fetch, a 500, a backend
        // mid-restart -- means we never found out. Rendering Login here would
        // assert "you are signed out" on no evidence, and the assertion is
        // usually FALSE: the httpOnly cookie is still in the jar, so the very
        // next request would have worked. Say what happened and offer Retry.
        setBootError(errorMessage(cause));
      }
    } finally {
      if (live()) setBooting(false);
    }
  }, []);

  useEffect(() => {
    const signal = { cancelled: false };
    void loadSession(signal);
    return () => {
      signal.cancelled = true;
    };
  }, [loadSession]);

  async function logout() {
    setError(null);
    try {
      // Fire the request before clearing local state so the cookie is still
      // attached and the server can actually revoke the session row. Clearing
      // first would log the browser out while leaving a live session in the
      // database -- which is precisely the thing server-side sessions exist to
      // prevent.
      await api("/api/auth/logout", { method: "POST" });
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setUser(null);
      setView({ kind: "dashboard" });
    }
  }

  if (booting) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-slate-950">
        <Spinner label="Checking your session" />
      </div>
    );
  }

  // Ordered BEFORE the `!user` check on purpose. When the session question
  // could not be answered, `user` is also null -- and falling through to Login
  // is precisely the bug this branch exists to prevent.
  if (bootError) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-slate-950 px-6">
        <div className="w-full max-w-md space-y-4 text-center">
          <h1 className="text-lg font-semibold tracking-tight text-slate-100">
            Could not check your session
          </h1>
          <ErrorBanner error={bootError} />
          <p className="text-sm text-slate-400">
            You have not been signed out &mdash; the app could not reach the API to
            find out. If you signed in just now, your session is most likely intact.
          </p>
          <button
            type="button"
            data-testid="retry-session"
            onClick={() => void loadSession()}
            className="min-h-11 rounded-md border border-slate-700 bg-slate-900 px-4 py-2 text-sm text-slate-200 transition hover:border-slate-600"
          >
            Try again
          </button>
        </div>
      </div>
    );
  }

  if (!user) {
    return <Login onAuthenticated={setUser} />;
  }

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      {/*
        Sticky, which it did not need to be when every view fitted on a screen.
        A conversation with an agent does not: sign-out and the way back to the
        agent list would otherwise be a full scroll away from wherever the
        thread has got to.
      */}
      <nav className="sticky top-0 z-20 border-b border-slate-800 bg-slate-950/90 backdrop-blur">
        {/*
          `flex-wrap` on both rows is the floor, not the fix. At 320px the
          brand, the monogram, the email, the admin pill and Sign out come to
          more than the viewport with `gap-3` between them, and a flex row with
          no wrap does not overflow its own box -- it overflows the DOCUMENT,
          which is the horizontal scrollbar the whole app then inherits. Wrapping
          converts that into a taller nav, which is merely ugly. The pill moving
          behind `sm:` below is what stops it ever having to.
        */}
        <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-3 px-4 py-3 sm:px-6">
          <button
            type="button"
            onClick={() => setView({ kind: "dashboard" })}
            className="min-h-11 rounded-md px-1 text-sm font-semibold tracking-tight text-slate-100 transition hover:text-white"
          >
            Groundwork
          </button>

          <div className="flex flex-wrap items-center gap-3">
            <Initials user={user} />
            <span className="hidden text-xs text-slate-400 sm:inline">{user.email}</span>
            {user.role === "admin" && (
              // Hidden below `sm` for the same reason the email is: it is a
              // status label, not a control, and the narrow viewport has to
              // spend its width on the two things that are (the way back to the
              // agent list, and the way out). The monogram stays because it is
              // the only remaining answer to "who am I signed in as".
              // Was a status label; it is now the way in. The `sm:` gate stays
              // for the reason the comment above gives -- at 320px the width
              // belongs to the way back and the way out.
              <button
                type="button"
                onClick={() => setView({ kind: "admin" })}
                aria-current={view.kind === "admin" ? "page" : undefined}
                className={`hidden min-h-11 rounded-full border px-2.5 py-0.5 text-xs font-medium transition sm:inline-flex sm:items-center ${
                  view.kind === "admin"
                    ? "border-sky-500 bg-sky-900/70 text-sky-100"
                    : "border-sky-800/60 bg-sky-950/40 text-sky-300 hover:border-sky-600 hover:text-sky-100"
                }`}
              >
                admin
              </button>
            )}
            <button
              type="button"
              data-testid="logout"
              onClick={() => void logout()}
              className="min-h-11 rounded-md border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-300 transition hover:border-slate-600"
            >
              Sign out
            </button>
          </div>
        </div>
      </nav>

      {error && (
        <div className="mx-auto max-w-6xl px-6 pt-6">
          <ErrorBanner error={error} />
        </div>
      )}

      <main>
        {view.kind === "dashboard" && (
          <Dashboard onOpenAgent={(agentId) => setView({ kind: "agent", agentId })} />
        )}

        {view.kind === "admin" && (
          <Admin onBack={() => setView({ kind: "dashboard" })} />
        )}

        {view.kind === "agent" && (
          // Keyed on the agent id so opening a different agent remounts rather
          // than reusing the previous agent's loaded documents and conversation.
          <AgentDetail
            key={view.agentId}
            agentId={view.agentId}
            onBack={() => setView({ kind: "dashboard" })}
          />
        )}
      </main>
    </div>
  );
}

/**
 * The signed-in user, as a monogram.
 *
 * Not `<img src={user.avatar_url}>`: that URL points at googleusercontent.com,
 * so rendering it would have every page view of this app announce itself to
 * Google -- for decoration. The login page inlines Google's own mark as SVG for
 * the same reason. Two letters carry the same "you are signed in as someone"
 * signal at no privacy cost.
 */
function Initials({ user }: { user: User }) {
  const source = user.name?.trim() || user.email;
  const letters = source
    .split(/[\s@._-]+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase() ?? "")
    .join("");

  return (
    <span
      title={user.name ?? user.email}
      className="inline-flex h-7 w-7 items-center justify-center rounded-full border border-slate-700 bg-slate-900 text-[0.65rem] font-semibold text-slate-300"
    >
      {letters || "?"}
    </span>
  );
}
