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

import { useEffect, useRef, useState } from "react";
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
 *
 * The three tool events are cyan, which is the one hue this palette had left
 * (slate, emerald, sky, rose, amber, fuchsia, indigo, violet and teal are all
 * spoken for) and reads as "the machine went and did something" beside the
 * retrieval colours. TOOL_ERROR breaks the family and joins REFUSE in rose,
 * because a failed tool call is a failure and colour is the fastest way to say
 * so -- the agent is shown the error and may try again, so the row beneath it
 * is very often another cyan TOOL_CALL.
 *
 * Both maps are read through `??` at the call site, so an event type the
 * backend gains before this file hears about it degrades to a neutral pill and
 * a neutral sentence rather than crashing the panel.
 */
const EVENT_STYLES: Record<string, string> = {
  RETRIEVE: "border-sky-800/60 bg-sky-950/40 text-sky-300",
  SCORE_CHECK: "border-amber-800/60 bg-amber-950/40 text-amber-300",
  REWRITE: "border-fuchsia-800/60 bg-fuchsia-950/40 text-fuchsia-300",
  RERANK: "border-indigo-800/60 bg-indigo-950/40 text-indigo-300",
  GENERATE: "border-emerald-800/60 bg-emerald-950/40 text-emerald-300",
  REFUSE: "border-rose-800/60 bg-rose-950/40 text-rose-300",
  TOOL_CALL: "border-cyan-800/60 bg-cyan-950/40 text-cyan-200",
  TOOL_RESULT: "border-cyan-800/60 bg-cyan-950/40 text-cyan-200",
  TOOL_ERROR: "border-rose-800/60 bg-rose-950/40 text-rose-200",
};

const EVENT_DESCRIPTIONS: Record<string, string> = {
  RETRIEVE: "Searched the indexed document chunks.",
  SCORE_CHECK: "Checked whether the retrieved evidence was strong enough.",
  REWRITE:
    "Read the question for typos, shorthand and references before searching. This runs on every turn and most often returns the question unchanged.",
  RERANK: "Reordered the retrieved passages by relevance.",
  GENERATE: "Generated the response from the selected context.",
  REFUSE: "Declined because the retrieved context did not support an answer.",
  TOOL_CALL: "The agent decided to use a tool and chose these arguments.",
  TOOL_RESULT: "What the tool returned.",
  TOOL_ERROR: "The tool failed. The agent was shown this and could try again.",
};

export default function TracePanel({
  queryId,
  openSignal = 0,
}: {
  queryId: string;
  /**
   * Opened from outside -- the tool-activity chip on the message summarises
   * what happened and this panel holds the detail, so the chip has to be able
   * to reach it.
   *
   * A NONCE rather than a boolean, for the reason `Message` bumps one to re-run
   * its citation focus: a boolean that is already `true` makes the second press
   * a no-op, so a user who opened the panel, closed it and pressed the chip
   * again would get nothing. Zero means "nobody has asked", which is why the
   * effect below is not an unconditional open on mount.
   */
  openSignal?: number;
}) {
  const [open, setOpen] = useState(false);
  // `null` means "never fetched", which is a different state from "fetched and
  // empty" -- the second one is worth saying out loud, because a turn with no
  // trace events means the backend wrote none, not that the panel is loading.
  const [events, setEvents] = useState<TraceEvent[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const panelId = `retrieval-details-${queryId}`;

  // "Has a request been sent for these rows" as a ref rather than as derived
  // state, because the open-from-outside effect below has to ask the question
  // without listing `events` and `loading` as dependencies -- which would
  // re-run it on every fetch and fire a second request for the same immutable
  // rows.
  const requested = useRef(false);

  async function load() {
    requested.current = true;
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
    if (next && !requested.current) void load();
  }

  useEffect(() => {
    if (openSignal === 0) return;
    setOpen(true);
    if (!requested.current) void load();
    // `load` is redeclared every render and depends only on `queryId`, which
    // cannot change for a mounted turn -- a turn is immutable and the panel is
    // keyed by it. Listing it would re-run this on every render instead of on
    // every request to open.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openSignal]);

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
              // The Python is lifted OUT of the payload rather than shown twice.
              // `formatJson` would render it as one long line of `\n` escapes,
              // which is not reading the program so much as decoding it.
              const code = pythonSource(event);
              const payload = formatJson(code === null ? event.payload : withoutCode(event.payload));

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

                  {code !== null && (
                    /*
                      The single most interesting thing in the whole trace, and
                      it gets its own block above the payload rather than a line
                      inside it. An agent that answers by writing a program is
                      making a claim the user is entitled to check, and the
                      check is reading the program -- which is not possible in a
                      JSON dump. `whitespace-pre` rather than `pre-wrap`:
                      indentation IS Python's syntax, so a wrapped line is a
                      different program.
                    */
                    <div className="mt-2">
                      <p className="mb-1 text-xs text-slate-400">Python the agent wrote</p>
                      <pre
                        data-testid="trace-python"
                        className="max-h-64 overflow-auto rounded bg-slate-950 p-2 font-mono text-xs leading-relaxed whitespace-pre text-emerald-200"
                      >
                        {code}
                      </pre>
                    </div>
                  )}

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

// --------------------------------------------------------------------------
// Reading a payload whose shape is not this file's to know
// --------------------------------------------------------------------------

/*
  `trace_events.payload` is JSONB and its shape varies by event type, which is
  why `TraceEvent.payload` is `unknown` rather than a union -- the frontend is
  deliberately not a second copy of the recorder's schema. The two functions
  below are the only place that changes, and they change it by INSPECTION.

  No casts, not even guarded ones. A cast that turns out to be wrong fails at
  the moment a property is read, inside a `.map()` over a list, which unmounts
  the whole message rather than the one row -- and the payload that would
  trigger it is by definition the one nobody has seen yet. Narrowing with
  `typeof` and `in` costs four extra lines and cannot throw at all.
*/

/** The Python a `run_python` call was about to execute, or `null` for every
 *  other event -- including a TOOL_CALL for `search_corpus`, whose args are a
 *  query string and belong in the ordinary payload dump. */
function pythonSource(event: TraceEvent): string | null {
  if (event.event_type !== "TOOL_CALL") return null;

  const payload = event.payload;
  if (typeof payload !== "object" || payload === null) return null;
  if (!("tool" in payload) || payload.tool !== "run_python") return null;
  if (!("args" in payload)) return null;

  const args = payload.args;
  if (typeof args !== "object" || args === null) return null;
  if (!("code" in args) || typeof args.code !== "string") return null;

  return args.code.trim() === "" ? null : args.code;
}

/** The same payload with `args.code` removed, so the program is rendered once
 *  rather than twice. Everything else in the payload -- the step number, the
 *  tool name, the call id, the purpose and filename the model chose -- is still
 *  worth seeing beside it. */
function withoutCode(payload: unknown): unknown {
  if (typeof payload !== "object" || payload === null || !("args" in payload)) return payload;

  const args = payload.args;
  if (typeof args !== "object" || args === null || !("code" in args)) return payload;

  const rest: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(args)) {
    if (key !== "code") rest[key] = value;
  }
  return { ...payload, args: rest };
}
