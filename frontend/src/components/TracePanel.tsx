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
 *
 * **Everything in here is diagnostic data, so it is set in mono** -- the step
 * index, the score, the duration, the payload, the Python. Mono is this
 * design's third voice and it means measurement: not the harness talking (sans)
 * and not the corpus answering (serif), but a number recorded about the turn.
 */

import { useEffect, useRef, useState } from "react";
import { chat } from "../lib/api.ts";
import type { TraceEvent } from "../lib/types.ts";
import { formatDuration, formatJson, formatScore } from "../lib/format.ts";
import {
  BAD_TONE,
  BTN_SECONDARY,
  BTN_SM,
  CARD,
  EYEBROW,
  HELP,
  OK_TONE,
  PILL,
  PILL_NEUTRAL,
  ROW,
  WARN_TONE,
  WELL,
} from "../lib/styles.ts";
import { ErrorBanner, Spinner, errorMessage } from "./ui.tsx";

/**
 * Four buckets, not eleven hues.
 *
 * This map used to give eleven event kinds nine different colours -- sky,
 * amber, fuchsia, indigo, emerald, rose, cyan, violet, teal -- on the argument
 * that the shape of a turn should be readable at a glance. It was not: nine
 * hues with no ordering between them is a legend the reader has to learn, and
 * the one distinction anybody actually needs was buried inside it.
 *
 * **The kind is already written on the pill, in words.** So colour is freed to
 * carry the only thing the words do not: did this step go WRONG. GENERATE is
 * the turn succeeding, SCORE_CHECK and SELF_CHECK are the machine grading its
 * own evidence, REFUSE and TOOL_ERROR are the two ways a step ends badly, and
 * everything else is ordinary machinery that ran as designed.
 *
 * That REFUSE sits in the failure bucket is a presentation choice about the
 * TRACE, not a claim about the product: a correct refusal is the behaviour the
 * golden set scores, and the answer above says so in a sentence. Here it is
 * simply the step that stopped the turn.
 *
 * Both maps are read through `??` at the call site, so an event type the
 * backend gains before this file hears about it degrades to a neutral pill and
 * a neutral sentence rather than crashing the panel. **That graceful
 * degradation is exactly why a missing entry is invisible, so both maps get an
 * entry or neither does** -- and with four buckets the cost of adding one is a
 * decision about severity rather than a hunt for an unused colour.
 */
const EVENT_STYLES: Record<string, string> = {
  GENERATE: `${PILL} ${OK_TONE}`,

  SCORE_CHECK: `${PILL} ${WARN_TONE}`,
  SELF_CHECK: `${PILL} ${WARN_TONE}`,

  REFUSE: `${PILL} ${BAD_TONE}`,
  TOOL_ERROR: `${PILL} ${BAD_TONE}`,

  RETRIEVE: PILL_NEUTRAL,
  RERANK: PILL_NEUTRAL,
  REWRITE: PILL_NEUTRAL,
  ROUTE: PILL_NEUTRAL,
  DELEGATE: PILL_NEUTRAL,
  TOOL_CALL: PILL_NEUTRAL,
  TOOL_RESULT: PILL_NEUTRAL,
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
  ROUTE:
    "Chose which teaching approach answers this turn, before searching. The payload names the roster it chose from and whether the choice came from the router, from an @mention you typed, or from a fallback.",
  DELEGATE:
    "Answered one section of this turn as a named specialist, over the same retrieved passages as every other section -- so a citation number means the same chunk throughout.",
  SELF_CHECK:
    "Checked the drafted answer against the passages it cited. The signal is what triggered the check and cost nothing; the verdict is what a second model call concluded.",
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
        className={`${BTN_SECONDARY} ${BTN_SM}`}
      >
        {open ? "Hide retrieval details" : "Retrieval details"}
      </button>

      {open && (
        <div id={panelId} className={`${CARD} mt-3 p-4`}>
          <p className={`${HELP} mb-3`}>
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
            <p className="text-xs text-muted">
              This turn recorded no trace events. A Stage 1 agent answers without writing
              decisions; only the Stage 2 loop has decisions to write.
            </p>
          )}

          <ol className="space-y-2">
            {(events ?? []).map((event) => {
              const style = EVENT_STYLES[event.event_type] ?? PILL_NEUTRAL;
              // The Python is lifted OUT of the payload rather than shown twice.
              // `formatJson` would render it as one long line of `\n` escapes,
              // which is not reading the program so much as decoding it.
              const code = pythonSource(event);
              const payload = formatJson(code === null ? event.payload : withoutCode(event.payload));

              return (
                <li
                  key={String(event.id)}
                  data-testid="trace-event"
                  // A hairline and nothing else. The panel around it is already
                  // a surface, so a second fill here would stack two greys for
                  // no information -- structure in this design is the rule, not
                  // the box it draws.
                  className={`${ROW} border-line`}
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-mono text-xs text-muted">
                      {String(event.step_index).padStart(2, "0")}
                    </span>
                    <span className={style}>{event.event_type}</span>
                    {event.score !== null && (
                      <span className="font-mono text-xs text-muted">
                        score {formatScore(event.score)}
                      </span>
                    )}
                    <span className="font-mono text-xs text-muted">
                      {formatDuration(event.duration_ms)}
                    </span>
                  </div>

                  <p className="mt-1.5 text-xs text-muted">
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
                      <p className={`${EYEBROW} mb-1`}>Python the agent wrote</p>
                      <pre
                        data-testid="trace-python"
                        className={`${WELL} max-h-64 overflow-auto p-2 font-mono text-xs leading-relaxed whitespace-pre text-ink`}
                      >
                        {code}
                      </pre>
                    </div>
                  )}

                  {payload && (
                    <pre
                      className={`${WELL} mt-2 max-h-48 overflow-auto p-2 font-mono text-xs leading-relaxed text-muted`}
                    >
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
