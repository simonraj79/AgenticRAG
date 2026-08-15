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
 */

import { useEffect, useState } from "react";
import { api, setUnauthorizedHandler } from "./lib/api.ts";
import type { User } from "./lib/types.ts";
import { ErrorBanner, Spinner, errorMessage } from "./components/ui.tsx";
import Login from "./views/Login.tsx";
import Dashboard from "./views/Dashboard.tsx";
import AgentDetail from "./views/AgentDetail.tsx";

type View = { kind: "dashboard" } | { kind: "agent"; agentId: string };

export default function App() {
  const [user, setUser] = useState<User | null>(null);
  const [booting, setBooting] = useState(true);
  const [view, setView] = useState<View>({ kind: "dashboard" });
  const [error, setError] = useState<string | null>(null);

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

  useEffect(() => {
    let cancelled = false;
    api<User>("/api/auth/me")
      .then((me) => {
        if (!cancelled) setUser(me);
      })
      .catch(() => {
        // Swallowed deliberately, and this is the ONE place that is right.
        // A 401 here is not an error, it is the answer to the question: nobody
        // is signed in. Every other call surfaces its message.
      })
      .finally(() => {
        if (!cancelled) setBooting(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

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

  if (!user) {
    return <Login onAuthenticated={setUser} />;
  }

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      <nav className="border-b border-slate-800 bg-slate-950/80 backdrop-blur">
        <div className="mx-auto flex max-w-5xl items-center justify-between px-6 py-3">
          <button
            type="button"
            onClick={() => setView({ kind: "dashboard" })}
            className="text-sm font-semibold tracking-tight text-slate-100"
          >
            Agentic RAG
          </button>

          <div className="flex items-center gap-3">
            <span className="text-xs text-slate-400">{user.email}</span>
            {user.role === "admin" && (
              <span className="rounded-full border border-sky-800/60 bg-sky-950/40 px-2 py-0.5 text-xs font-medium text-sky-300">
                admin
              </span>
            )}
            <button
              type="button"
              data-testid="logout"
              onClick={() => void logout()}
              className="rounded-md border border-slate-700 bg-slate-900 px-3 py-1.5 text-sm text-slate-300 transition hover:border-slate-600"
            >
              Sign out
            </button>
          </div>
        </div>
      </nav>

      {error && (
        <div className="mx-auto max-w-5xl px-6 pt-6">
          <ErrorBanner error={error} />
        </div>
      )}

      {view.kind === "dashboard" && (
        <Dashboard onOpenAgent={(agentId) => setView({ kind: "agent", agentId })} />
      )}

      {view.kind === "agent" && (
        // Keyed on the agent id so opening a different agent remounts rather
        // than reusing the previous agent's loaded documents and answer.
        <AgentDetail
          key={view.agentId}
          agentId={view.agentId}
          onBack={() => setView({ kind: "dashboard" })}
        />
      )}
    </div>
  );
}
