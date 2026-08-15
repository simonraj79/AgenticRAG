import { useEffect, useState } from "react";

// The ONLY config the frontend receives. Never put an API key in a VITE_*
// variable - it is compiled into the bundle and readable in devtools.
const API_URL = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

type Health = { status: string; version: string; database: string };

const VIEWS = [
  { name: "Ingest", stage: "Stage 1", desc: "Upload, chunk, embed, upsert to Pinecone" },
  { name: "Ask", stage: "Stage 1-2", desc: "Query the corpus, streamed answer" },
  { name: "Trace", stage: "Stage 2", desc: "Per-turn REASON / ACT / OBSERVE timeline" },
  { name: "Evaluate", stage: "Stage 3", desc: "Golden set + Ragas scorecard" },
];

export default function App() {
  const [health, setHealth] = useState<Health | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch(`${API_URL}/api/health`, { credentials: "include" })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then(setHealth)
      .catch((e: Error) => setError(e.message));
  }, []);

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      <div className="mx-auto max-w-4xl px-6 py-16">
        <header className="mb-12">
          <h1 className="text-3xl font-semibold tracking-tight">Agentic RAG</h1>
          <p className="mt-2 text-slate-400">
            NTU Harness Engineering &middot; multi-user RAG over private documents
          </p>
        </header>

        <section className="mb-12 rounded-lg border border-slate-800 bg-slate-900/50 p-5">
          <h2 className="mb-3 text-sm font-medium uppercase tracking-wide text-slate-400">
            Backend
          </h2>
          {error && (
            <p className="text-sm text-rose-400">
              Cannot reach {API_URL} &mdash; {error}
            </p>
          )}
          {!error && !health && <p className="text-sm text-slate-500">Checking&hellip;</p>}
          {health && (
            <dl className="grid grid-cols-3 gap-4 text-sm">
              <div>
                <dt className="text-slate-500">API</dt>
                <dd className="mt-1 text-emerald-400">{health.status}</dd>
              </div>
              <div>
                <dt className="text-slate-500">Database</dt>
                <dd
                  className={`mt-1 ${
                    health.database === "ok" ? "text-emerald-400" : "text-amber-400"
                  }`}
                >
                  {health.database}
                </dd>
              </div>
              <div>
                <dt className="text-slate-500">Version</dt>
                <dd className="mt-1 text-slate-300">{health.version}</dd>
              </div>
            </dl>
          )}
        </section>

        <section>
          <h2 className="mb-4 text-sm font-medium uppercase tracking-wide text-slate-400">
            Views
          </h2>
          <ul className="grid gap-3 sm:grid-cols-2">
            {VIEWS.map((v) => (
              <li
                key={v.name}
                className="rounded-lg border border-slate-800 bg-slate-900/50 p-4"
              >
                <div className="flex items-baseline justify-between">
                  <span className="font-medium">{v.name}</span>
                  <span className="text-xs text-slate-500">{v.stage}</span>
                </div>
                <p className="mt-1 text-sm text-slate-400">{v.desc}</p>
              </li>
            ))}
          </ul>
        </section>

        <footer className="mt-12 border-t border-slate-800 pt-6 text-xs text-slate-600">
          Scaffold only &mdash; views are not yet implemented. See PRD.md &sect;10.
        </footer>
      </div>
    </div>
  );
}
