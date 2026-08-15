/**
 * Trace tab: what the agent decided, in order, for the most recent query.
 *
 * `trace_events` IS this view (PRD section 4.3) -- there is no client-side
 * reconstruction of the pipeline, only a rendering of rows the backend wrote as
 * it went. That is the point: the timeline is durable and survives the session,
 * which is what makes the Stage 2 deliverable auditable rather than a console
 * log somebody has to be watching at the time.
 *
 * "Most recent query" is resolved in two ways on purpose. If the user just
 * asked something, the Ask tab hands down that `query_id` directly. If they
 * opened this tab cold -- new page load, previous session -- there is no id in
 * memory, so the first row of `GET /api/agents/{id}/queries` supplies it.
 * Without the fallback the tab would be permanently empty until someone asked
 * a question, which reads as broken.
 */

import { useCallback, useEffect, useState } from "react";
import { api } from "../lib/api.ts";
import type { QueryRow, TraceEvent } from "../lib/types.ts";
import { formatDuration, formatJson, formatScore, formatTimestamp } from "../lib/format.ts";
import { ErrorBanner, Spinner, errorMessage } from "../components/ui.tsx";

/** Colour per decision type, so the shape of a turn is readable at a glance:
 *  a Stage 1 chain is RETRIEVE -> GENERATE, a Stage 2 loop has amber
 *  SCORE_CHECK / REWRITE steps in the middle. */
const EVENT_STYLES: Record<string, string> = {
  RETRIEVE: "border-sky-800/60 bg-sky-950/40 text-sky-300",
  SCORE_CHECK: "border-amber-800/60 bg-amber-950/40 text-amber-300",
  REWRITE: "border-fuchsia-800/60 bg-fuchsia-950/40 text-fuchsia-300",
  RERANK: "border-indigo-800/60 bg-indigo-950/40 text-indigo-300",
  GENERATE: "border-emerald-800/60 bg-emerald-950/40 text-emerald-300",
  REFUSE: "border-rose-800/60 bg-rose-950/40 text-rose-300",
};

export default function AgentTrace({
  agentId,
  queryId,
}: {
  agentId: string;
  /** The query just asked in this session, if any. */
  queryId: string | null;
}) {
  const [events, setEvents] = useState<TraceEvent[]>([]);
  const [query, setQuery] = useState<QueryRow | null>(null);
  const [resolvedId, setResolvedId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      // Always fetch history: it supplies the question text for the header even
      // when `queryId` was handed down, and it is the only source of an id when
      // it was not.
      const history = await api<QueryRow[]>(`/api/agents/${agentId}/queries`);
      const target = queryId ?? history[0]?.id ?? null;
      setResolvedId(target);
      setQuery(history.find((row) => row.id === target) ?? null);

      if (!target) {
        setEvents([]);
        return;
      }
      setEvents(await api<TraceEvent[]>(`/api/trace/${target}`));
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setLoading(false);
    }
  }, [agentId, queryId]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h3 className="text-sm font-medium tracking-wide text-slate-400 uppercase">
            Decision timeline
          </h3>
          {query && (
            <p className="mt-1 max-w-2xl text-sm text-slate-300">
              &ldquo;{query.question}&rdquo;
              <span className="ml-2 text-xs text-slate-500">
                {formatTimestamp(query.created_at)}
                {query.latency_ms !== null ? ` · ${formatDuration(query.latency_ms)}` : ""}
              </span>
            </p>
          )}
          {resolvedId && (
            <p className="mt-1 font-mono text-xs text-slate-600">query {resolvedId}</p>
          )}
        </div>

        <button
          type="button"
          data-testid="trace-refresh"
          onClick={() => void load()}
          className="rounded-md border border-slate-700 bg-slate-900 px-3 py-1.5 text-sm text-slate-300 transition hover:border-slate-600"
        >
          Refresh
        </button>
      </div>

      <ErrorBanner error={error} />

      {loading && <Spinner label="Loading trace" />}

      {!loading && !resolvedId && (
        <p className="rounded-lg border border-dashed border-slate-800 px-4 py-8 text-center text-sm text-slate-500">
          No queries yet. Ask a question and its decision trace appears here.
        </p>
      )}

      {!loading && resolvedId && events.length === 0 && (
        <p className="rounded-lg border border-dashed border-slate-800 px-4 py-8 text-center text-sm text-slate-500">
          This query recorded no trace events.
        </p>
      )}

      <ol className="space-y-3">
        {events.map((event) => {
          const style =
            EVENT_STYLES[event.event_type] ?? "border-slate-700 bg-slate-900 text-slate-300";
          const payload = formatJson(event.payload);

          return (
            <li
              key={String(event.id)}
              data-testid="trace-event"
              className="rounded-lg border border-slate-800 bg-slate-900/40 p-4"
            >
              <div className="flex flex-wrap items-center gap-3">
                <span className="font-mono text-xs text-slate-600">
                  {String(event.step_index).padStart(2, "0")}
                </span>
                <span
                  className={`rounded-full border px-2 py-0.5 text-xs font-medium ${style}`}
                >
                  {event.event_type}
                </span>
                {event.score !== null && (
                  <span className="font-mono text-xs text-slate-400">
                    score {formatScore(event.score)}
                  </span>
                )}
                <span className="text-xs text-slate-500">
                  {formatDuration(event.duration_ms)}
                </span>
              </div>

              {payload && (
                <pre className="mt-3 max-h-64 overflow-auto rounded-md bg-slate-950 p-3 font-mono text-xs leading-relaxed text-slate-400">
                  {payload}
                </pre>
              )}
            </li>
          );
        })}
      </ol>
    </div>
  );
}
