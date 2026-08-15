/**
 * The decision timeline for ONE turn, opened from that turn.
 *
 * It used to live on a separate tab, which quietly broke the thing it exists
 * for. A trace is only evidence if it is attached to the answer it explains;
 * two clicks and a tab switch away, it becomes a feature nobody opens, and the
 * user is back to taking the answer on faith. Same `trace_events` rows, same
 * rendering -- moved to where the question is.
 *
 * **Fetched on open, not on render.** A thread of twenty turns would otherwise
 * fire twenty requests for timelines nobody asked to see. Fetched exactly once
 * per turn as well: the rows are immutable, so a second open reuses them.
 */

import { useState } from "react";
import { chat } from "../lib/api.ts";
import type { TraceEvent } from "../lib/types.ts";
import { formatDuration, formatJson, formatScore } from "../lib/format.ts";
import { ErrorBanner, Spinner, errorMessage } from "./ui.tsx";

/**
 * Colour per decision type, so the shape of a turn is readable at a glance: a
 * Stage 1 chain is RETRIEVE -> GENERATE, while a Stage 2 loop shows amber
 * SCORE_CHECK and fuchsia REWRITE steps in the middle. The fuchsia matches the
 * rewritten-question banner above the answer on purpose -- same decision, seen
 * twice.
 */
const EVENT_STYLES: Record<string, string> = {
  RETRIEVE: "border-sky-800/60 bg-sky-950/40 text-sky-300",
  SCORE_CHECK: "border-amber-800/60 bg-amber-950/40 text-amber-300",
  REWRITE: "border-fuchsia-800/60 bg-fuchsia-950/40 text-fuchsia-300",
  RERANK: "border-indigo-800/60 bg-indigo-950/40 text-indigo-300",
  GENERATE: "border-emerald-800/60 bg-emerald-950/40 text-emerald-300",
  REFUSE: "border-rose-800/60 bg-rose-950/40 text-rose-300",
};

const EVENT_DESCRIPTIONS: Record<string, string> = {
  RETRIEVE: "Searched the indexed document chunks.",
  SCORE_CHECK: "Checked whether the retrieved evidence was strong enough.",
  REWRITE: "Rephrased the question to improve retrieval.",
  RERANK: "Reordered the retrieved passages by relevance.",
  GENERATE: "Generated the response from the selected context.",
  REFUSE: "Declined because the retrieved context did not support an answer.",
};

export default function TracePanel({ queryId }: { queryId: string }) {
  const [open, setOpen] = useState(false);
  // `null` means "never fetched", which is a different state from "fetched and
  // empty" -- the second one is worth saying out loud, because a turn with no
  // trace events means the backend wrote none, not that the panel is loading.
  const [events, setEvents] = useState<TraceEvent[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const panelId = `retrieval-details-${queryId}`;

  async function load() {
    setLoading(true);
    setError(null);
    try {
      setEvents(await chat.trace(queryId));
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setLoading(false);
    }
  }

  function toggle() {
    const next = !open;
    setOpen(next);
    if (next && events === null && !loading) void load();
  }

  return (
    <div className="mt-2">
      <button
        type="button"
        data-testid="trace-toggle"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={toggle}
        className="min-h-11 rounded-md border border-slate-700 bg-slate-900 px-2.5 py-2 text-xs text-slate-300 transition hover:border-slate-600 hover:text-slate-100"
      >
        {open ? "Hide retrieval details" : "Retrieval details"}
      </button>

      {open && (
        <div id={panelId} className="mt-3 rounded-lg border border-slate-800 bg-slate-950/50 p-3">
          <p className="mb-3 text-xs leading-relaxed text-slate-400">
            This is an activity log of observable retrieval steps and scores. It does
            not expose private model reasoning.
          </p>
          <ErrorBanner error={error} />

          {loading && (
            <div role="status" aria-live="polite">
              <Spinner label="Loading retrieval details" />
            </div>
          )}

          {!loading && events !== null && events.length === 0 && (
            <p className="text-xs text-slate-400">
              This turn recorded no trace events. A Stage 1 agent answers without writing
              decisions; only the Stage 2 loop has decisions to write.
            </p>
          )}

          <ol className="space-y-2">
            {(events ?? []).map((event) => {
              const style =
                EVENT_STYLES[event.event_type] ?? "border-slate-700 bg-slate-900 text-slate-300";
              const payload = formatJson(event.payload);

              return (
                <li
                  key={String(event.id)}
                  data-testid="trace-event"
                  className="rounded-md border border-slate-800 bg-slate-900/40 p-2.5"
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-mono text-xs text-slate-400">
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
                    <span className="text-xs text-slate-400">
                      {formatDuration(event.duration_ms)}
                    </span>
                  </div>

                  <p className="mt-1.5 text-xs text-slate-300">
                    {EVENT_DESCRIPTIONS[event.event_type] ?? "Recorded an agent activity."}
                  </p>

                  {payload && (
                    <pre className="mt-2 max-h-48 overflow-auto rounded bg-slate-950 p-2 font-mono text-xs leading-relaxed text-slate-400">
                      {payload}
                    </pre>
                  )}
                </li>
              );
            })}
          </ol>
        </div>
      )}
    </div>
  );
}
