/**
 * Handouts: the four things this agent can make, and everything it has made.
 *
 * NotebookLM calls this Studio. Groundwork calls it Handouts because Groundwork
 * is a teaching product -- its personas are pedagogies and its corpus is lecture
 * material -- and a handout is the thing an instructor makes *from* a lesson
 * *for* you. It needs no explanation to a workshop attendee, which "Studio",
 * "Artifacts" and "Canvas" all do.
 *
 * **The panel is the chrome-free half of the feature.** It owns no scroll
 * container, no heading bar and no width: at `xl` it is a docked grid column and
 * below `xl` it is the body of a `Drawer`, and both of those provide the box.
 * One component in two frames is what keeps the two from drifting -- the
 * alternative is a docked panel and a drawer panel that gain features at
 * different times.
 *
 * **Two ways in, and the empty state names both.** A handout arrives either
 * because the user pressed one of these four buttons (`origin: "recipe"`) or
 * because the agent wrote Python mid-answer and it produced a file
 * (`origin: "tool"`). A panel with four buttons and no mention of the
 * conversational path teaches half the feature, so the empty state says both out
 * loud.
 *
 * **Polling stops.** Creation answers 202 with the row already inserted at
 * `pending`; the bytes are written afterwards by a background job. So the panel
 * re-reads the list every three seconds *while any row is still moving* and
 * stops the moment none are -- the same lifecycle `AgentEvaluate` uses for eval
 * runs, and for the same reason: a fixed interval that never stops is a request
 * every three seconds, forever, for a panel that stays mounted behind a closed
 * drawer.
 */

import { useCallback, useEffect, useId, useRef, useState } from "react";
import type { FormEvent } from "react";
import { handouts } from "../lib/api.ts";
import type { Handout, HandoutRecipe } from "../lib/types.ts";
import { EmptyState, ErrorBanner, Reveal, Spinner, errorMessage } from "./ui.tsx";
import HandoutCard from "./HandoutCard.tsx";

/**
 * The four recipes, as client-side copy.
 *
 * There is no endpoint that lists these, deliberately: the set is fixed at four
 * and a round trip to learn what four buttons say would be a request for a
 * constant. `key` is the value that goes into `HandoutRequest.recipe` and is
 * what the backend's `RECIPES` dict is keyed on -- it is the only field here
 * that is a contract; the rest is label copy.
 *
 * The blurbs name the file each one produces, because "Table" and "Study sheet"
 * are indistinguishable until you know one is a `.csv` you can open in Excel and
 * the other is markdown you can read.
 */
const RECIPES: HandoutRecipe[] = [
  { key: "chart", label: "Chart", blurb: "A PNG plot of figures from the corpus.", icon: "📊" },
  { key: "deck", label: "Slide deck", blurb: "A .pptx you can open and edit.", icon: "📑" },
  { key: "table", label: "Table", blurb: "A .csv of the numbers, for a spreadsheet.", icon: "📋" },
  { key: "sheet", label: "Study sheet", blurb: "Markdown notes, rendered here too.", icon: "📄" },
];

/**
 * The statuses that mean "this handout has stopped moving".
 *
 * Anything else keeps the poll alive, INCLUDING a status this build has never
 * heard of -- `status` is a `String(16)` column rather than an enum, and
 * treating an unknown value as finished would strand a live job behind a
 * spinner that never resolves. The same rule `AgentEvaluate` applies to run
 * statuses.
 */
const TERMINAL_STATUSES = new Set(["ready", "failed"]);

/** 04 section 2.1: the list query is capped, and the `content` column is
 *  `deferred()` server-side so 200 rows do not drag tens of megabytes of bytea
 *  along with them. Sent explicitly rather than left to the server default, so
 *  the number the client renders against is visible in this file. */
const LIST_LIMIT = 200;

/** Three seconds, flat. A handout job is one or two model calls plus a sandbox
 *  run -- tens of seconds, not the tens of minutes an eval run takes -- so the
 *  backoff `AgentEvaluate` needs to avoid hammering a 25-minute job would only
 *  make a fast one feel slow. The lifecycle is the part that is copied, not the
 *  interval. */
const POLL_MS = 3_000;

/** When to stop watching. Generous against a deck, which is the slowest recipe,
 *  and short enough that a job whose process died does not leave a spinner
 *  turning for the rest of the session. */
const POLL_GIVE_UP_MS = 5 * 60 * 1_000;

export default function HandoutsPanel({
  agentId,
  conversationId,
  onCountChange,
  seed,
}: {
  agentId: string;
  /** The thread on screen, or `null` for an unsaved draft. Both a filter for
   *  the first list and the `conversation_id` a new handout is attributed to --
   *  which is what makes "chart what we just discussed" work. */
  conversationId: string | null;
  /** The badge on the drawer toggle. Must be referentially stable at the call
   *  site (a `useCallback`), because it is an effect dependency here. */
  onCountChange?: (n: number) => void;
  /** Handouts returned by the turn that just completed, prepended without
   *  waiting for a poll. A file the user explicitly asked for should not take
   *  three seconds to appear, and on a narrow viewport -- where this panel is a
   *  closed drawer -- the resulting count bump is the only thing that says the
   *  file exists at all. */
  seed?: Handout[];
}) {
  const [rows, setRows] = useState<Handout[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  /** The recipe the user picked, or `null` when the composer is closed. Picking
   *  one is what reveals the brief field -- four buttons and a permanently open
   *  textarea would be a form whose first question is unanswerable. */
  const [recipe, setRecipe] = useState<string | null>(null);
  const [brief, setBrief] = useState("");
  const [creating, setCreating] = useState(false);

  const [stalled, setStalled] = useState(false);
  /** Bumped to restart the poll after it has given up, and to force an
   *  immediate re-read. Without it "Check again" would refresh once and leave
   *  the panel claiming to watch a job nothing was scheduled to re-read -- the
   *  same slot `AgentEvaluate` calls `pollEpoch`. */
  const [pollEpoch, setPollEpoch] = useState(0);

  /**
   * The panel's clock, in epoch ms, advanced once a second and ONLY while
   * something is pending. Every row reads it, so all of them agree on "now" and
   * there is exactly one timer instead of one per row.
   */
  const [now, setNow] = useState(() => Date.now());

  const briefRef = useRef<HTMLTextAreaElement>(null);
  const briefId = useId();

  /**
   * What each handout this session created was asked for.
   *
   * "Try again" is specified as re-POSTing the same brief, and the brief is
   * NOT on the wire: the server keeps it in `handouts.meta`, and neither
   * `Handout` nor `HandoutDetail` carries a `meta` field. So the only client
   * that can retry without asking the user to retype is the one that made the
   * request -- which is this one, for the rows it made. A ref rather than
   * state because nothing renders from it.
   */
  const briefs = useRef(new Map<string, { recipe: string; brief: string }>());

  // ---- Load, and reload on demand -------------------------------------
  //
  // A `cancelled` flag rather than an AbortController, and the difference is
  // the API surface rather than a preference: `chat.ask` takes an
  // `AbortSignal` because a 45-second turn is worth cancelling, while the
  // `handouts` namespace in `lib/api.ts` accepts none on any of its five
  // calls. A controller constructed here would abort nothing. The flag gives
  // the property that actually matters -- a response that lands after unmount
  // or after the agent changed writes nothing -- which is the shape every
  // other load in this codebase uses.
  useEffect(() => {
    let cancelled = false;
    setLoading(true);

    handouts
      .list(agentId, { limit: LIST_LIMIT })
      .then((fresh) => {
        if (!cancelled) setRows(fresh);
      })
      .catch((cause: unknown) => {
        if (!cancelled) setError(errorMessage(cause));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [agentId, pollEpoch]);

  // ---- The turn's own handouts, without waiting for a poll -------------
  //
  // Prepended by id, and `current` is returned UNCHANGED when there is nothing
  // new -- returning a fresh array every time would be a new `rows` identity,
  // a re-render, and (via the count effect below) a call to `onCountChange` on
  // every render rather than on every change.
  useEffect(() => {
    if (!seed || seed.length === 0) return;
    setRows((current) => {
      const known = new Set(current.map((row) => row.id));
      const fresh = seed.filter((row) => !known.has(row.id));
      return fresh.length > 0 ? [...fresh, ...current] : current;
    });
  }, [seed]);

  // ---- The badge ------------------------------------------------------
  //
  // Keyed on the LENGTH, not on `rows`: the poll replaces the array on every
  // tick, so depending on the array itself would fire this on every tick for a
  // number that has not moved.
  useEffect(() => {
    onCountChange?.(rows.length);
  }, [rows.length, onCountChange]);

  /**
   * Whether anything is still being made.
   *
   * Derived as a BOOLEAN, and that is what makes the poll below terminate
   * cleanly. Keying the effect on `rows` would tear it down and rebuild it on
   * every tick -- restarting the timer each time and, in `AgentEvaluate`'s
   * version, resetting its backoff to the shortest interval. A boolean stays
   * `true` across ticks that changed nothing, so the effect runs once per
   * transition rather than once per response.
   */
  const anyPending = rows.some((row) => !TERMINAL_STATUSES.has(row.status));

  // ---- Poll while, and only while, something is pending ----------------
  useEffect(() => {
    if (!anyPending) return;

    setStalled(false);
    let cancelled = false;
    let timer = 0;
    const deadline = Date.now() + POLL_GIVE_UP_MS;

    const tick = async () => {
      if (cancelled) return;
      try {
        const fresh = await handouts.list(agentId, { limit: LIST_LIMIT });
        if (cancelled) return;
        setRows(fresh);
      } catch {
        // Swallowed on purpose, and only here. A failed poll is not a failed
        // job -- the background task is still working -- so replacing the
        // panel's error banner with a transient network blip would report the
        // wrong problem. Persistent failure surfaces as the stall notice.
      }
      if (cancelled) return;
      if (Date.now() >= deadline) {
        setStalled(true);
        return;
      }
      // `setTimeout` chained from the response rather than `setInterval`: an
      // interval keeps firing while a slow request is in flight and stacks
      // overlapping reads of the same list.
      timer = window.setTimeout(() => void tick(), POLL_MS);
    };

    timer = window.setTimeout(() => void tick(), POLL_MS);

    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [anyPending, agentId, pollEpoch]);

  // ---- The clock, on the same gate as the poll -------------------------
  useEffect(() => {
    if (!anyPending) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, [anyPending]);

  // Exactly one element focused per transition. The StrictMode trap this repo
  // has already paid for is a *two*-step focus -- a heading and then an input,
  // where the blur between them forged a "field has been visited" flag on the
  // second invocation. One element, twice, is harmless.
  useEffect(() => {
    if (recipe) briefRef.current?.focus();
  }, [recipe]);

  const create = useCallback(
    async (recipeKey: string, text: string) => {
      const trimmed = text.trim();
      if (!trimmed || creating) return;

      setCreating(true);
      setError(null);
      try {
        const row = await handouts.create(agentId, {
          recipe: recipeKey,
          brief: trimmed,
          conversation_id: conversationId,
        });
        briefs.current.set(row.id, { recipe: recipeKey, brief: trimmed });
        // Filtered before prepending so a retry that somehow returns an id
        // already on screen moves it to the top rather than duplicating it --
        // two `<li>` with the same React key is a warning and a rendering bug.
        setRows((current) => [row, ...current.filter((existing) => existing.id !== row.id)]);
        setRecipe(null);
        setBrief("");
      } catch (cause) {
        // A 409 here is the quota, and its message names the limit. Shown
        // verbatim: refused is a decision, not a failure, and "nothing was
        // deleted to make room" is the part worth reading.
        setError(errorMessage(cause));
      } finally {
        setCreating(false);
      }
    },
    [agentId, conversationId, creating],
  );

  async function remove(id: string) {
    setError(null);
    try {
      await handouts.remove(agentId, id);
      setRows((current) => current.filter((row) => row.id !== id));
      briefs.current.delete(id);
    } catch (cause) {
      setError(errorMessage(cause));
    }
  }

  /**
   * Re-run a failed handout.
   *
   * Straight back to the API when this session made it, which is the specified
   * behaviour and the common case -- a job usually fails within a minute of
   * being asked for. When it does not (a failure left over from a previous
   * session, or a row this browser never created), there is nothing to re-POST,
   * because the brief lives only in `handouts.meta` server-side. Rather than
   * disable the button or send a guess, the composer is opened with the recipe
   * pre-picked and the title as a starting brief, and the user presses Make it.
   * The button says "Try again" in both cases because in both cases that is
   * what it does; only the number of clicks differs.
   */
  function retry(row: Handout) {
    const remembered = briefs.current.get(row.id);
    if (remembered) {
      void create(remembered.recipe, remembered.brief);
      return;
    }
    setRecipe(row.kind);
    setBrief(row.title);
  }

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (recipe) void create(recipe, brief);
  }

  function checkAgain() {
    setStalled(false);
    setPollEpoch((epoch) => epoch + 1);
  }

  /*
    One request, split here, rather than two filtered requests.

    `lib/api.ts` offers a `conversationId` filter and describes the panel as
    reading the list twice, once scoped and once not. The two lists are a
    PARTITION of the same set, though, and two requests taken thirty
    milliseconds apart can disagree with each other -- a handout created
    between them appears in both lists or in neither, and the second is the
    failure that looks like the feature is broken. One read cannot contradict
    itself, and it also halves the traffic of a three-second poll. The filter
    stays available for a caller that wants only one half.
  */
  const scoped = conversationId
    ? rows.filter((row) => row.conversation_id === conversationId)
    : [];
  const rest = conversationId
    ? rows.filter((row) => row.conversation_id !== conversationId)
    : rows;

  /** The two props every row takes identically. The handlers are NOT in here:
   *  each row needs its own closure over its own id, and a shared one spread in
   *  and then overridden reads as though the shared value might survive. */
  const rowProps = { agentId, now };

  return (
    <div data-testid="handouts-panel" className="flex min-w-0 flex-col gap-5">
      <section className="min-w-0">
        <h3 className="text-xs font-medium tracking-wide text-slate-400 uppercase">
          Make a handout
        </h3>

        <div className="mt-2 grid grid-cols-2 gap-2">
          {RECIPES.map((entry) => {
            const selected = entry.key === recipe;
            return (
              <button
                key={entry.key}
                type="button"
                data-testid="handout-recipe"
                data-recipe={entry.key}
                aria-pressed={selected}
                onClick={() => {
                  // Pressing the selected recipe again closes the composer,
                  // which is the only way back out of it that does not require
                  // finding the Cancel button below the fold on a phone.
                  setRecipe((current) => (current === entry.key ? null : entry.key));
                }}
                className={`flex min-h-11 min-w-0 flex-col items-start gap-0.5 rounded-md border px-3 py-2 text-left transition ${
                  selected
                    ? "border-emerald-600 bg-emerald-950/30"
                    : "border-slate-700 bg-slate-900 hover:border-slate-600"
                }`}
              >
                <span className="text-base leading-none" aria-hidden="true">
                  {entry.icon}
                </span>
                <span className="text-sm font-medium text-slate-200">{entry.label}</span>
                <span className="text-[0.7rem] leading-snug text-slate-400">{entry.blurb}</span>
              </button>
            );
          })}
        </div>

        {recipe && (
          /*
            `noValidate`, even though nothing here carries a constraint yet.

            The create-agent wizard has already paid for the alternative: native
            constraint validation ABORTS the submit event, so a `required`
            attribute silently stops `onSubmit` from running and the custom
            handling beside it becomes dead code that looks like it works. Any
            `required` added to this form later would do the same thing, and the
            attribute would still be the right semantic for the accessibility
            tree -- so the flag goes on now, while it costs nothing.
          */
          <form onSubmit={onSubmit} noValidate className="mt-3 min-w-0 space-y-2">
            <label htmlFor={briefId} className="block text-xs text-slate-400">
              What should it cover?
            </label>
            <textarea
              id={briefId}
              ref={briefRef}
              data-testid="handout-brief"
              rows={3}
              value={brief}
              onChange={(event) => setBrief(event.target.value)}
              placeholder="e.g. the power budget by subsystem, as discussed above"
              className="min-h-11 w-full min-w-0 resize-y rounded-md border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 outline-none focus:border-slate-500"
            />
            {/* The brief is not only a prompt -- it is what searches the corpus.
                Saying so is what stops "make me a deck" arriving as a brief with
                nothing in it for retrieval to work from. */}
            <p className="text-[0.7rem] leading-snug text-slate-400">
              This also searches the corpus, so name the material.
              {conversationId
                ? " The recent turns in this conversation are included too."
                : " Open a conversation first if you want its answers included."}
            </p>
            <div className="flex flex-wrap gap-2">
              <button
                type="submit"
                data-testid="handout-create"
                disabled={creating || brief.trim() === ""}
                className="min-h-11 rounded-md bg-emerald-500 px-4 py-2 text-sm font-semibold text-emerald-950 transition hover:bg-emerald-400 disabled:opacity-50"
              >
                {creating ? "Starting…" : "Make it"}
              </button>
              <button
                type="button"
                data-testid="handout-cancel"
                onClick={() => {
                  setRecipe(null);
                  setBrief("");
                }}
                className="min-h-11 rounded-md border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-300 transition hover:border-slate-600"
              >
                Cancel
              </button>
            </div>
          </form>
        )}
      </section>

      <ErrorBanner error={error} />

      {stalled && (
        <div className="flex flex-wrap items-center gap-3 rounded-lg border border-amber-800/60 bg-amber-950/20 px-3 py-2 text-xs text-amber-300">
          <span>Stopped watching after five minutes. The job may still be running.</span>
          <button
            type="button"
            data-testid="handouts-recheck"
            onClick={checkAgain}
            className="min-h-11 rounded-md border border-slate-700 bg-slate-900 px-2.5 py-1 font-medium text-slate-300 transition hover:border-slate-600"
          >
            Check again
          </button>
        </div>
      )}

      {loading && rows.length === 0 && <Spinner label="Loading handouts" />}

      {!loading && rows.length === 0 && (
        <div data-testid="handouts-empty">
          {/* Both routes in, named. A panel that mentions only the four buttons
              teaches half the feature -- the agent producing a file mid-answer
              is the other half, and it is the half nobody discovers by
              accident. */}
          <EmptyState
            title="No handouts yet."
            detail="Pick a recipe above, or ask the agent in chat to chart, tabulate or summarise something — anything it makes while answering lands here too."
          />
        </div>
      )}

      {conversationId && scoped.length > 0 && (
        <section className="min-w-0">
          <h3 className="mb-2 flex items-baseline justify-between gap-2 text-xs font-medium tracking-wide text-slate-400 uppercase">
            <span>In this conversation</span>
            <span className="font-mono text-slate-500">{scoped.length}</span>
          </h3>
          <ol data-testid="handouts-list-conversation" className="space-y-2">
            {scoped.map((row) => (
              <HandoutCard
                key={row.id}
                handout={row}
                {...rowProps}
                onDelete={() => void remove(row.id)}
                onRetry={() => retry(row)}
              />
            ))}
          </ol>
        </section>
      )}

      {rest.length > 0 &&
        (conversationId ? (
          /*
            Collapsed, because with a thread open the list that matters is the
            one above and everything else is history. Without a thread open
            there is no "above" -- the whole panel would then be a single closed
            disclosure, which reads as an empty panel -- so that case renders
            the same rows expanded under a plain heading instead.
          */
          <Reveal summary={`All handouts (${rest.length})`} testId="handouts-all">
            <ol data-testid="handouts-list-all" className="space-y-2">
              {rest.map((row) => (
                <HandoutCard
                  key={row.id}
                  handout={row}
                  {...rowProps}
                  onDelete={() => void remove(row.id)}
                  onRetry={() => retry(row)}
                />
              ))}
            </ol>
          </Reveal>
        ) : (
          <section className="min-w-0">
            <h3 className="mb-2 flex items-baseline justify-between gap-2 text-xs font-medium tracking-wide text-slate-400 uppercase">
              <span>All handouts</span>
              <span className="font-mono text-slate-500">{rest.length}</span>
            </h3>
            <ol data-testid="handouts-list-all" className="space-y-2">
              {rest.map((row) => (
                <HandoutCard
                  key={row.id}
                  handout={row}
                  {...rowProps}
                  onDelete={() => void remove(row.id)}
                  onRetry={() => retry(row)}
                />
              ))}
            </ol>
          </section>
        ))}
    </div>
  );
}
