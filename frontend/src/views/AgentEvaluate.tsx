/**
 * Evaluate: the golden set, and the scorecards it produces.
 *
 * This is Stage 3, and its whole claim is one sentence from PRD section 4.4 —
 * *find your weakest metric, that points at your next investment*. Everything
 * on this page is arranged to deliver that sentence: the golden set is what the
 * measurement rests on, the run is how it is taken, and the scorecard names the
 * weakest metric and the knobs that move it.
 *
 * **A run is genuinely slow, and the copy says so up front.** Every question is
 * a full agent turn — measured at ~15 s bare and 30-60 s under a coaching
 * persona, because generation is token-bound and 89% of a turn is the model
 * writing — plus four judged calls on top. Ten questions is minutes, not
 * seconds. A progress note that under-promises is worse than none: the user
 * starts counting against it and concludes the thing has hung, which this
 * project has already watched happen once with a "10-15 s" label on chat.
 *
 * **The run button is disabled with a reason, never silently.** Zero active
 * questions means there is nothing to score, and a button that just does
 * nothing reads as a broken feature rather than as an empty golden set.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { evaluation } from "../lib/api.ts";
import type { Agent, EvalRun, EvalRunDetail, GoldenQuestion } from "../lib/types.ts";
import { formatTimestamp } from "../lib/format.ts";
import { ConfirmDeleteButton, ErrorBanner, Spinner, errorMessage } from "../components/ui.tsx";
import GoldenSetEditor from "../components/GoldenSetEditor.tsx";
import Scorecard from "../components/Scorecard.tsx";

/** The statuses that mean "this run has stopped moving". Anything else keeps
 *  the poll loop alive, including a status this frontend does not recognise --
 *  `status` is a plain String column, and treating an unknown value as finished
 *  would strand a live run behind a stale progress bar. */
const TERMINAL_RUN_STATUSES = new Set(["completed", "failed"]);

const POLL_FIRST_MS = 2_000;
const POLL_MAX_MS = 6_000;
const POLL_GROWTH = 1.25;
/** Long, because the ceiling it guards is ten persona turns plus forty judge
 *  calls, not a slow network. */
const POLL_GIVE_UP_MS = 45 * 60 * 1_000;

export default function AgentEvaluate({
  agent,
  agentId,
}: {
  /**
   * Either form works, and both are optional for one reason: this view is
   * mounted by AgentDetail, which holds the whole `Agent` record, while its
   * sibling tabs take a bare `agentId`. Accepting both means the mount site can
   * pass whichever it has without this file needing to change, and the runtime
   * guard below turns the one impossible case -- neither -- into a visible
   * message instead of a blank tab.
   */
  agent?: Agent;
  agentId?: string;
}) {
  const resolvedId = agent?.id ?? agentId ?? null;

  const [runs, setRuns] = useState<EvalRun[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<EvalRunDetail | null>(null);
  const [questions, setQuestions] = useState<GoldenQuestion[]>([]);
  const [notes, setNotes] = useState("");

  const [loadingRuns, setLoadingRuns] = useState(true);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [starting, setStarting] = useState(false);
  const [stalled, setStalled] = useState(false);
  /** Bumped to restart the poll loop after it has given up. Without it,
   *  "Check again" would refresh once and leave the page claiming to watch a
   *  run that nothing was scheduled to re-read. */
  const [pollEpoch, setPollEpoch] = useState(0);
  const [error, setError] = useState<string | null>(null);

  // Read by the poll tick, which needs to know which run the user is looking at
  // NOW. The closure's captured id is the value at the moment the loop started,
  // and a tick that lands after the user has clicked a different run would
  // otherwise overwrite the run on screen with the one it was watching.
  const selectedIdNow = useRef<string | null>(null);
  useEffect(() => {
    selectedIdNow.current = selectedId;
  }, [selectedId]);

  const refreshRuns = useCallback(async () => {
    if (!resolvedId) return [];
    const rows = await evaluation.listRuns(resolvedId);
    setRuns(rows);
    return rows;
  }, [resolvedId]);

  // First load. An unfinished run is selected in preference to a finished one,
  // because a user who started a run, navigated away and came back is asking
  // "is it done yet" -- and showing them last week's scorecard answers a
  // question they did not ask.
  useEffect(() => {
    if (!resolvedId) {
      setLoadingRuns(false);
      return;
    }

    let cancelled = false;
    setLoadingRuns(true);

    refreshRuns()
      .then((rows) => {
        if (cancelled) return;
        const live = rows.find((row) => !TERMINAL_RUN_STATUSES.has(row.status));
        const chosen = live ?? rows[0];
        if (chosen) setSelectedId(chosen.id);
      })
      .catch((cause: unknown) => {
        if (!cancelled) setError(errorMessage(cause));
      })
      .finally(() => {
        if (!cancelled) setLoadingRuns(false);
      });

    return () => {
      cancelled = true;
    };
  }, [resolvedId, refreshRuns]);

  // The selected run's detail. Separate from the poll loop below so that
  // switching between two finished runs never starts a timer.
  useEffect(() => {
    if (!selectedId) {
      setDetail(null);
      return;
    }

    let cancelled = false;
    setLoadingDetail(true);
    setStalled(false);

    evaluation
      .loadRun(selectedId)
      .then((row) => {
        if (!cancelled) setDetail(row);
      })
      .catch((cause: unknown) => {
        if (!cancelled) setError(errorMessage(cause));
      })
      .finally(() => {
        if (!cancelled) setLoadingDetail(false);
      });

    return () => {
      cancelled = true;
    };
  }, [selectedId]);

  /**
   * Watch a run that is still going.
   *
   * Keyed on the run's ID rather than on `detail`, for the reason
   * AgentDocuments keys its loop on a boolean: `detail` gets a new object
   * identity on every tick, so depending on it would tear the effect down and
   * rebuild it each time, resetting the backoff to its shortest interval and
   * turning a backing-off poll into a fixed 2 s one for three quarters of an
   * hour.
   */
  const watchId =
    detail && !TERMINAL_RUN_STATUSES.has(detail.status) && detail.id === selectedId
      ? detail.id
      : null;

  useEffect(() => {
    if (!watchId) return;

    setStalled(false);
    let cancelled = false;
    let timer = 0;
    let delay = POLL_FIRST_MS;
    const deadline = Date.now() + POLL_GIVE_UP_MS;

    const tick = async () => {
      if (cancelled) return;
      try {
        const fresh = await evaluation.loadRun(watchId);
        if (cancelled) return;
        // The user moved to another run while this was in flight. The run is
        // still progressing server-side; it just is not what is on screen.
        if (selectedIdNow.current !== watchId) return;

        setDetail(fresh);
        if (TERMINAL_RUN_STATUSES.has(fresh.status)) {
          // The list carries the summary, so it is stale the moment a run
          // finishes. Refreshed once, on the transition, rather than every tick.
          void refreshRuns().catch(() => {
            // A stale history list is not worth an error banner over a
            // delivered scorecard.
          });
          return;
        }
      } catch {
        // Swallowed on purpose, and only here. A failed poll is not a failed
        // run -- the job is still working -- so replacing the page's error
        // banner with a transient network blip would report the wrong problem.
        // Persistent failure surfaces as the stall notice below.
      }
      if (cancelled) return;
      if (Date.now() >= deadline) {
        setStalled(true);
        return;
      }
      delay = Math.min(Math.round(delay * POLL_GROWTH), POLL_MAX_MS);
      timer = window.setTimeout(() => void tick(), delay);
    };

    timer = window.setTimeout(() => void tick(), delay);

    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [watchId, pollEpoch, refreshRuns]);

  // Stable identity: GoldenSetEditor stores this in a ref and calls it after
  // every load, so a fresh arrow function each render would be harmless here
  // but is exactly the shape that turns into a fetch loop elsewhere.
  const handleQuestions = useCallback((rows: GoldenQuestion[]) => {
    setQuestions(rows);
  }, []);

  const activeCount = questions.filter((row) => row.is_active).length;
  const running = watchId !== null;

  async function startRun(): Promise<void> {
    if (!resolvedId) return;
    setStarting(true);
    setError(null);
    try {
      const started = await evaluation.startRun(resolvedId, notes.trim() || null);
      setRuns((current) => [started, ...current]);
      setSelectedId(started.id);
      selectedIdNow.current = started.id;
      setNotes("");
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setStarting(false);
    }
  }

  async function removeRun(runId: string): Promise<void> {
    setError(null);
    try {
      await evaluation.deleteRun(runId);
      const rows = await refreshRuns();
      if (selectedIdNow.current === runId) {
        const next = rows[0]?.id ?? null;
        setSelectedId(next);
        selectedIdNow.current = next;
      }
    } catch (cause) {
      setError(errorMessage(cause));
    }
  }

  function checkAgain(): void {
    setStalled(false);
    setPollEpoch((epoch) => epoch + 1);
  }

  if (!resolvedId) {
    return (
      <ErrorBanner error="No agent was passed to the Evaluate view (expected an `agent` record or an `agentId`)." />
    );
  }

  return (
    /*
      `evaluate-panel`, NOT `tab-evaluate` -- do not rename it back, that
      restores a bug. This root used to carry `tab-evaluate` while the tab
      BUTTON carried it too (`AgentDetail`'s TABS array, before 4e5e6bd moved
      the strip into `AgentBar`), so a `getByTestId("tab-evaluate")` matched two
      live elements whenever this view was open and threw on any strict-mode
      query. 4e5e6bd defused it by naming the button `tab-eval`; that left the
      real name unused and the panel wearing a `tab-` prefix it does not earn.

      The panel is what moved, because in this codebase `tab-*` means tab
      BUTTON -- `tab-workspace` and `tab-sources` (`AgentBar.tsx`) and
      `rail-tab-sources` (`AgentChat.tsx`) are all buttons, and this root was
      the only non-button wearing the prefix. `<noun>-panel` matches the one
      existing panel-root id, `handouts-panel` (`HandoutsPanel.tsx`). The
      button now holds the full `tab-evaluate`, symmetric with its two siblings.
    */
    <div data-testid="evaluate-panel" className="space-y-8">
      <section className="rounded-xl border border-slate-800 bg-slate-900/30 p-5">
        <h2 className="text-sm font-medium tracking-wide text-slate-400 uppercase">
          Run an evaluation
        </h2>
        <p className="mt-2 max-w-3xl text-sm leading-relaxed text-slate-400">
          Each active question is put to {agent?.name ?? "this agent"} as a real turn, and
          the answer plus the chunks that produced it are scored by a judge model on four
          Ragas metrics. The output is not a grade — it is a pointer at whichever metric is
          weakest, which is the parameter worth changing next.
        </p>

        {/*
          The honest number, stated before the button rather than discovered
          after it. Ten questions x (one full agent turn + four judged calls) is
          minutes; quoting seconds here would be a promise the system cannot
          keep, and the user would conclude it had hung long before it had.
        */}
        <p className="mt-3 max-w-3xl rounded-lg border border-slate-800 bg-slate-950/50 px-3 py-2 text-xs leading-relaxed text-slate-400">
          <span className="font-medium text-slate-300">This takes several minutes.</span> Every
          question is a complete agent turn — 15 s for a terse agent, 30-60 s for a coaching
          persona, because generation is token-bound and is 89% of a turn — and then four
          more judged calls on top. A ten-question run is comfortably five to fifteen
          minutes. Leaving this tab is safe: the run continues server-side and this page
          picks it back up.
        </p>

        <div className="mt-4 flex flex-wrap items-end gap-3">
          {/* The `min-w` is gated on `sm` because it is a LAYOUT hint -- keep
              the notes field and the run button on one line while there is room
              for both -- and below `sm` there is not. 18rem is 288px against
              248px of usable width at 320px (320 - 32 page padding - 40 card
              padding), so unconditionally it forces the card wider than the
              viewport and the whole document scrolls sideways. */}
          <label className="flex-1 text-xs text-slate-400 sm:min-w-[18rem]">
            What changed since the last run?
            <input
              type="text"
              data-testid="eval-notes"
              value={notes}
              onChange={(event) => setNotes(event.target.value)}
              placeholder="e.g. rerank on, retrieve_k 20 -> 30"
              className="mt-1 min-h-11 w-full rounded-md border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 outline-none focus:border-slate-500"
            />
          </label>

          <button
            type="button"
            data-testid="eval-run"
            disabled={starting || running || activeCount === 0}
            onClick={() => void startRun()}
            className="min-h-11 rounded-md bg-emerald-500 px-4 py-2 text-sm font-semibold text-emerald-950 transition hover:bg-emerald-400 disabled:opacity-50"
          >
            {starting ? "Starting…" : running ? "Run in progress" : "Run evaluation"}
          </button>
        </div>

        {activeCount === 0 && !loadingRuns && (
          // Said out loud. A disabled button with no explanation is
          // indistinguishable from a broken one.
          <p className="mt-3 text-xs text-amber-300">
            Nothing to run: this agent has no active golden questions. Suggest ten below, or
            add one by hand.
          </p>
        )}

        {activeCount > 0 && !running && (
          <p className="mt-3 text-xs text-slate-400">
            {activeCount} active {activeCount === 1 ? "question" : "questions"} ready to
            score. Notes are how two runs become an experiment rather than two numbers.
          </p>
        )}

        <div className="mt-4">
          <ErrorBanner error={error} />
        </div>

        {detail && running && (
          <div
            data-testid="eval-progress"
            className="mt-4 rounded-lg border border-slate-800 bg-slate-950/50 p-4"
          >
            <div className="flex flex-wrap items-center justify-between gap-2">
              <Spinner
                label={
                  detail.status === "pending"
                    ? "Queued — waiting to start"
                    : "Running — asking, then judging, one question at a time"
                }
              />
              <span className="font-mono text-sm text-slate-300">
                {detail.progress.done} of {detail.progress.total || "?"}
              </span>
            </div>

            <div className="mt-3 h-2 overflow-hidden rounded-full bg-slate-800">
              <div
                className="h-full rounded-full bg-emerald-500 transition-all"
                style={{
                  // `progress_total` defaults to 0 rather than NULL so a queued
                  // run renders honestly as "0 of 0". Dividing by it would give
                  // NaN and a bar of width "NaN%", which renders as full.
                  width:
                    detail.progress.total > 0
                      ? `${Math.min(100, (detail.progress.done / detail.progress.total) * 100)}%`
                      : "0%",
                }}
              />
            </div>

            {stalled && (
              <div className="mt-3 flex flex-wrap items-center gap-3 text-xs text-amber-300">
                <span>Stopped watching after 45 minutes. The run may still be going.</span>
                <button
                  type="button"
                  data-testid="eval-recheck"
                  onClick={checkAgain}
                  className="min-h-11 rounded-md border border-slate-700 bg-slate-900 px-2.5 py-1 font-medium text-slate-300 transition hover:border-slate-600"
                >
                  Check again
                </button>
              </div>
            )}
          </div>
        )}
      </section>

      {loadingDetail && !detail && <Spinner label="Loading scorecard" />}

      {detail && <Scorecard run={detail} />}

      <section>
        <h3 className="mb-3 text-sm font-medium tracking-wide text-slate-400 uppercase">
          Run history
        </h3>

        {loadingRuns && <Spinner label="Loading runs" />}

        {!loadingRuns && runs.length === 0 && (
          <p className="rounded-lg border border-dashed border-slate-800 px-4 py-8 text-center text-sm text-slate-400">
            No runs yet. The first one is the baseline every later run is read against.
          </p>
        )}

        {runs.length > 0 && (
          /*
            Newest first, which is the order the API returns and the reason
            `eval_runs` is persisted at all: a score is only meaningful beside
            the score before it (PRD 4.4, "what changed since last run").
          */
          <ol data-testid="eval-run-history" className="space-y-2">
            {runs.map((row) => {
              const active = row.id === selectedId;
              return (
                <li
                  key={row.id}
                  data-testid="eval-run-row"
                  data-run-id={row.id}
                  data-status={row.status}
                  className={`flex flex-wrap items-center gap-3 rounded-lg border px-4 py-3 ${
                    active
                      ? "border-slate-600 bg-slate-800/50"
                      : "border-slate-800 bg-slate-900/30"
                  }`}
                >
                  <button
                    type="button"
                    onClick={() => setSelectedId(row.id)}
                    className="min-w-0 flex-1 text-left"
                  >
                    <span className="flex flex-wrap items-center gap-2">
                      <RunStatusPill status={row.status} />
                      <span className="text-sm text-slate-200">
                        {formatTimestamp(row.started_at ?? row.finished_at)}
                      </span>
                      {row.summary?.weakest_metric && (
                        <span className="text-xs text-amber-300">
                          weakest: {row.summary.weakest_metric}{" "}
                          {row.summary.weakest_score !== null
                            ? row.summary.weakest_score.toFixed(2)
                            : ""}
                        </span>
                      )}
                      {!TERMINAL_RUN_STATUSES.has(row.status) && (
                        <span className="font-mono text-xs text-slate-400">
                          {row.progress.done}/{row.progress.total || "?"}
                        </span>
                      )}
                    </span>

                    {row.notes && (
                      <span className="mt-1 block truncate text-xs text-slate-400">
                        {row.notes}
                      </span>
                    )}

                    {row.error && (
                      <span className="mt-1 block truncate text-xs text-rose-300">
                        {row.error}
                      </span>
                    )}
                  </button>

                  <ConfirmDeleteButton
                    testId="eval-run-delete"
                    label="Delete"
                    confirmLabel="Confirm"
                    size="sm"
                    onConfirm={() => void removeRun(row.id)}
                  />
                </li>
              );
            })}
          </ol>
        )}
      </section>

      <GoldenSetEditor
        agentId={resolvedId}
        onQuestionsChanged={handleQuestions}
        runInFlight={running}
      />
    </div>
  );
}

/**
 * The run vocabulary is its own: "pending" / "running" / "completed" /
 * "failed", where the shared `StatusPill` speaks the document and agent one
 * ("ready" / "indexed" / "indexing"). Mapping "completed" onto "ready" would
 * put a word on screen that appears nowhere in this feature's API.
 */
const RUN_STATUS_STYLES: Record<string, string> = {
  completed: "border-emerald-800/60 bg-emerald-950/40 text-emerald-300",
  running: "border-amber-800/60 bg-amber-950/40 text-amber-300",
  pending: "border-slate-700 bg-slate-900 text-slate-400",
  failed: "border-rose-800/60 bg-rose-950/40 text-rose-300",
};

function RunStatusPill({ status }: { status: string }) {
  const style = RUN_STATUS_STYLES[status] ?? "border-slate-700 bg-slate-900 text-slate-400";
  return (
    <span className={`inline-block rounded-full border px-2 py-0.5 text-xs font-medium ${style}`}>
      {status}
    </span>
  );
}
