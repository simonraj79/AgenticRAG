/**
 * One handout, as a row in the panel.
 *
 * The row has to render three genuinely different things under one layout, and
 * the reason they are one component rather than three is that a handout MOVES
 * between them: a row arrives `pending`, becomes `ready` or `failed` a minute
 * later, and the poll swaps its status in place. Three components would mean
 * three mounts for one object, which throws away the open/closed state of the
 * disclosure and re-fires the detail fetch every time the job finishes.
 *
 * | status    | what is shown                                                |
 * |-----------|--------------------------------------------------------------|
 * | `pending` | spinner, elapsed seconds, and NO download link -- the bytes   |
 * |           | do not exist yet and the route answers 409                    |
 * | `ready`   | thumbnail (charts only), size, download, the code, delete     |
 * | `failed`  | a bad-tone rule, a chip naming the failure class, the error  |
 * |           | text verbatim, and "Try again"                               |
 *
 * Anything else renders as itself. `status`, `kind` and `origin` are `String(16)`
 * columns rather than enums precisely so a fifth recipe costs a seed row instead
 * of a migration, so an unrecognised value must render rather than throw -- the
 * same rule `StatusPill` and `CategoryBadge` already follow.
 */

import { useEffect, useRef, useState } from "react";
import type { MouseEvent } from "react";
import { handouts } from "../lib/api.ts";
import type { Handout, HandoutDetail } from "../lib/types.ts";
import { formatBytes } from "../lib/format.ts";
import { Markdown } from "../lib/markdown.tsx";
import { ConfirmDeleteButton, ErrorBanner, Reveal, Spinner, errorMessage } from "./ui.tsx";
import {
  BAD_TONE,
  BTN_QUIET,
  BTN_SECONDARY,
  CARD,
  PILL,
  WELL,
} from "../lib/styles.ts";

/**
 * Read with `??` at the call site, so a recipe this build has never heard of
 * shows its raw `kind` instead of an empty gap. See the file docstring.
 */
const KIND_LABELS: Record<string, string> = {
  chart: "chart",
  deck: "slide deck",
  table: "table",
  sheet: "study sheet",
};

/**
 * The one visual difference between the two ways a handout arrives, and it is
 * deliberately small: the panel lists both in the same place, in the same
 * shape, because to the user they are the same object. What differs is only how
 * it got there, which is worth a few grey words and nothing more.
 */
const ORIGIN_LABELS: Record<string, string> = {
  tool: "from a turn",
  recipe: "from a recipe",
};

/**
 * What the disclosure on a card is called, per kind.
 *
 * It has to name what is actually inside it, and what is inside it changed: a
 * deck and a table now carry a `preview_text` outline -- slide titles, or the
 * header row and a row count -- above the generating Python. A study sheet has
 * no code at all, so its disclosure holds the sheet itself.
 *
 * A chart keeps "Code", and that is the honest label: its preview is the
 * thumbnail already rendered above, so the only thing the disclosure adds is the
 * matplotlib that drew it.
 *
 * Read through `??` like the two maps above, so an unrecognised kind degrades to
 * a sensible word rather than to `undefined`.
 */
const REVEAL_SUMMARY: Record<string, string> = {
  chart: "Code",
  deck: "Outline and code",
  table: "Outline and code",
  sheet: "Preview",
};

/**
 * What class of failure a `failed` row hit, as words rather than as a slug.
 *
 * The five the sandbox already computes plus `"invalid"`, which is about the
 * ARTEFACT rather than the process: the file was produced, and it does not
 * open. The prose in `handout.error` says what happened; this says what to do
 * about it -- "timed out" means ask for less, "blocked import" means the model
 * reached for something the sandbox does not carry, "unusable file" means the
 * run looked clean and the deck is empty.
 *
 * **This map is a GATE, not a `??` fallback, and that is the one place this
 * file departs from `KIND_LABELS` above.** An unrecognised `kind` is still
 * worth rendering raw, because it is what the row IS and the user is looking
 * for it. An unrecognised failure class is a token with no meaning to a
 * workshop attendee, sitting beside prose that already explains the failure --
 * so it renders nothing, and the row stays exactly as it was before this
 * feature. The backend side of that contract (`error_kind` is one of these
 * six, and stays under 16 characters so it remains promotable to a column) is
 * asserted in `scripts/deck_check.py`; papering over a stray value here is how
 * that assertion would stop meaning anything.
 */
export const ERROR_KIND_LABELS: Record<string, string> = {
  import: "blocked import",
  syntax: "syntax error",
  timeout: "timed out",
  runtime: "crashed",
  output: "output rejected",
  invalid: "unusable file",
};

export default function HandoutCard({
  agentId,
  handout,
  now,
  onDelete,
  onRetry,
}: {
  agentId: string;
  handout: Handout;
  /**
   * The panel's clock, in epoch ms.
   *
   * Passed in rather than read from `Date.now()` here so that every row on
   * screen agrees about what "now" is, and -- more importantly -- so that the
   * ticking lives in exactly one place. The panel advances this once a second
   * ONLY while something is pending; a resting panel's relative times are as of
   * its last render, which is the right trade for a component that stays
   * mounted behind a closed drawer for the whole session. A permanent 1 s timer
   * to keep "4 min ago" honest would be a re-render per second, forever, for a
   * line of grey text nobody is watching.
   */
  now: number;
  onDelete: () => void;
  /** Re-run this handout's brief. The panel owns it because the brief is not on
   *  this row -- see the panel's `briefs` ref for why. */
  onRetry: () => void;
}) {
  // `null` means "never fetched", which is a different fact from "fetched and
  // empty" -- the same distinction TracePanel draws. Both `preview_text` and
  // `source_code` can run to kilobytes and the list renders up to 200 rows, so
  // neither is in the list response at all.
  const [detail, setDetail] = useState<HandoutDetail | null>(null);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  // A thumbnail that 404s or 409s renders as the browser's broken-image glyph,
  // which looks like a bug in the panel rather than a file that is not there.
  const [thumbFailed, setThumbFailed] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState<string | null>(null);

  // The chart thumbnail has to be RESOLVED now rather than pointed at.
  //
  // `<img src>` is a navigation, so it cannot carry the bearer token, and the
  // two roads need different answers: a presigned R2 URL is usable directly
  // (cross-origin, no credential, exactly what a no-CORS image load wants),
  // while the Postgres road needs the bytes fetched and wrapped in a blob.
  // `handouts.resolveSrc` picks, and hands back the matching cleanup.
  const [thumbSrc, setThumbSrc] = useState<string | null>(null);
  const isChart = handout.kind === "chart" && handout.status === "ready";

  useEffect(() => {
    if (!isChart) return;
    let cancelled = false;
    let revoke: (() => void) | null = null;

    handouts
      .resolveSrc(agentId, handout.id)
      .then((resolved) => {
        if (cancelled) {
          // Unmounted while in flight. Revoke immediately or the blob outlives
          // the component that would have released it.
          resolved.revoke();
          return;
        }
        revoke = resolved.revoke;
        setThumbSrc(resolved.src);
      })
      .catch(() => {
        // Same treatment a broken <img> already got. A thumbnail that will not
        // load is not worth an error banner on a handout that downloads fine.
        if (!cancelled) setThumbFailed(true);
      });

    return () => {
      cancelled = true;
      revoke?.();
    };
  }, [agentId, handout.id, isChart]);
  // "Has a request been sent", as a ref rather than state: it is read inside an
  // event handler to decide whether to fetch, and making it state would put a
  // render between the decision and the flag.
  const requested = useRef(false);

  const terminal = handout.status === "ready" || handout.status === "failed";
  const kindLabel = KIND_LABELS[handout.kind] ?? handout.kind;
  const originLabel = ORIGIN_LABELS[handout.origin] ?? handout.origin;
  // Deliberately no `??`, unlike the two above. `undefined` here means "render
  // nothing", which covers both a row that recorded no class and a class this
  // build does not know -- see `ERROR_KIND_LABELS`.
  const errorKindLabel = handout.error_kind
    ? ERROR_KIND_LABELS[handout.error_kind]
    : undefined;

  async function loadDetail() {
    requested.current = true;
    setLoadingDetail(true);
    setDetailError(null);
    try {
      setDetail(await handouts.load(agentId, handout.id));
    } catch (cause) {
      setDetailError(errorMessage(cause));
    } finally {
      setLoadingDetail(false);
    }
  }

  /**
   * Fetch-on-first-open, through a click on the wrapper rather than a prop on
   * the disclosure.
   *
   * `Reveal` is a native `<details>` and exposes no `onToggle`, and the DOM
   * `toggle` event does NOT bubble -- so a handler on this wrapper would never
   * see it, and React's delegation cannot help. A click DOES bubble, and
   * activating a `<summary>` by keyboard dispatches one too, so this catches
   * both the mouse and the Enter/Space paths. The `closest("summary")` test is
   * what keeps it from firing on every click inside the opened panel, and
   * `requested` is what makes it once-only: the rows are immutable, so a second
   * open reuses what the first one fetched.
   */
  function onRevealClick(event: MouseEvent<HTMLDivElement>) {
    if (requested.current) return;
    if (!(event.target instanceof Element)) return;
    if (!event.target.closest("summary")) return;
    void loadDetail();
  }

  return (
    <li
      data-testid="handout-card"
      data-handout-id={handout.id}
      data-status={handout.status}
      data-kind={handout.kind}
      // A failed row differs by its RULE, not by a tinted fill. The chip and the
      // error prose below already carry the bad tone as filled colour, and a
      // washed background under them would put the same signal on the card
      // twice while making the verbatim error text harder to read -- which is
      // the one thing on a failed card that has to be legible.
      className={`${CARD} min-w-0 p-4 ${handout.status === "failed" ? "border-bad-line" : ""}`}
    >
      <div className="flex min-w-0 items-start gap-3">
        {/*
          A chart's own PNG, at its own download URL, and that is not a
          shortcut -- there is no separate thumbnail endpoint and generating one
          would mean a second stored blob per handout.

          `crossOrigin` is deliberately NOT set. Leaving it off keeps this a
          no-CORS image request, which carries the session cookie because that
          cookie is `SameSite=None; Secure` (PRD section 6.5); setting it would
          switch the request into CORS mode and require the download route to
          answer `Access-Control-Allow-Credentials`, which it has no reason to.

          Gated on `status === "ready"`, because the route answers 409 for a
          pending row and the `<img>` would render broken while the job is still
          doing exactly what it should.
        */}
        {isChart && !thumbFailed && thumbSrc && (
          <img
            data-testid="handout-thumb"
            src={thumbSrc}
            alt={handout.title}
            loading="lazy"
            onError={() => setThumbFailed(true)}
            className={`${WELL} h-14 w-14 shrink-0 object-cover`}
          />
        )}

        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-medium text-ink" title={handout.title}>
            {handout.title}
          </p>
          <p className="mt-1 text-xs text-muted">
            {kindLabel}
            {handout.status === "ready" && (
              <>
                <span className="text-faint"> &middot; </span>
                {/* A byte count is a measurement, so it is set in mono like
                    every other number in the app. `tabular-nums` keeps a
                    column of cards from jittering as sizes change under the
                    poll. */}
                <span className="font-mono tabular-nums">
                  {formatBytes(handout.byte_size)}
                </span>
              </>
            )}
            <span className="text-faint"> &middot; </span>
            {relativeTime(handout.created_at, now)}
            <span className="text-faint"> &middot; </span>
            {originLabel}
          </p>
        </div>
      </div>

      {!terminal && (
        /*
          Elapsed seconds, not a bare spinner. A chart is an LLM call plus a
          sandbox run and a deck is more; the same reasoning as the chat
          composer applies -- a spinner with no number reads as hung at about
          four seconds, and "the button does nothing" becomes the bug report for
          a system working exactly as designed.
        */
        <div role="status" aria-live="polite" className="mt-2 flex flex-wrap items-center gap-2">
          <Spinner label="Making this…" />
          <span className="font-mono text-xs tabular-nums text-muted">
            {elapsedSeconds(handout.created_at, now)}s
          </span>
        </div>
      )}

      {handout.status === "failed" && errorKindLabel && (
        /*
          The class, above the prose, as a SIBLING of it rather than a wrapper
          around it.

          A wrapper would put the two on one line and would also change this
          card's spacing for every row that carries no class -- which is every
          row written before `error_kind` existed, and plenty written since. An
          element that is simply absent changes nothing at all, which is what
          "identical to today" has to mean if it is going to be assertable.

          A `<span>`, and no `min-h-11`: that convention is about tap targets,
          and this is not a control -- nothing here is clickable. `StatusPill`
          in `ui.tsx` is the shape being followed.
        */
        <span
          data-testid="handout-error-kind"
          // The raw value alongside the label, so a browser-level check can
          // assert on the class without depending on the copy.
          data-error-kind={handout.error_kind}
          // A STATE, so `PILL` plus a tone -- the same filled, tinted shape
          // `StatusPill` uses for `failed`, rather than a fourth badge look.
          className={`${PILL} ${BAD_TONE} mt-2`}
        >
          {errorKindLabel}
        </span>
      )}

      {handout.status === "failed" && (
        <p
          data-testid="handout-error"
          className="mt-2 text-xs leading-relaxed whitespace-pre-wrap text-bad"
        >
          {/* Verbatim. Without it a failure is a red card with no way to find
              out what went wrong short of reading the server log, which a
              workshop attendee cannot do. */}
          {handout.error ?? "The job failed without recording a reason."}
        </p>
      )}

      <div className="mt-3 flex flex-wrap items-center gap-2">
        {handout.status === "ready" && (
          /*
            A plain anchor, never a fetch-and-Blob. The route is a
            cookie-authenticated GET whose `Content-Disposition: attachment`
            does the saving, so this is a navigation the browser owns; the Blob
            version would pull megabytes of image data through JS and is inert
            inside a sandbox that blocks downloads a page starts itself.

            `inline-flex items-center` is load-bearing rather than decorative:
            an `<a>` is not a flex container by default, so `min-h-11` would
            make a 44px box with the label sitting on its first text line
            instead of centred.

            A BUTTON rather than an anchor, and that is forced. A browser
            NAVIGATION cannot carry an `Authorization` header, so once identity
            moved to a bearer token an `<a href>` here authenticated nobody --
            it worked only while the old session cookie was still live.

            `downloadHandout` resolves the road first: a presigned R2 URL is
            handed to the browser directly (its own
            `ResponseContentDisposition` names the file), and the Postgres road
            fetches the bytes with the token and saves a blob.
          */
          <button
            type="button"
            data-testid="handout-download"
            disabled={downloading}
            onClick={() => {
              setDownloading(true);
              setDownloadError(null);
              handouts
                .downloadHandout(agentId, handout.id)
                .catch((cause) => setDownloadError(errorMessage(cause)))
                .finally(() => setDownloading(false));
            }}
            className={BTN_SECONDARY}
          >
            {downloading ? "Preparing..." : "Download"}
          </button>
        )}

        {handout.status === "failed" && (
          <button
            type="button"
            data-testid="handout-retry"
            onClick={onRetry}
            // Quiet rather than bordered: on a failed card the thing to read is
            // the error, and a second outlined control beside Delete would make
            // the row look like a choice between two equal actions.
            className={BTN_QUIET}
          >
            Try again
          </button>
        )}

        <div className="ml-auto">
          {/* The shared confirm, not a new one. Placed away from Download for
              the reason `ui.tsx` records: arming guards a mis-click on THIS
              button and does nothing about a mis-click aimed at its neighbour. */}
          <ConfirmDeleteButton
            testId="handout-delete"
            label="Delete"
            confirmLabel="Confirm"
            accessibleLabel={`Delete ${handout.title}`}
            accessibleConfirmLabel={`Confirm deletion of ${handout.title}`}
            size="sm"
            onConfirm={onDelete}
          />
        </div>
      </div>

      {/*
        A download that failed has to SAY so. The old anchor could not fail
        visibly -- the browser owned the navigation, so a 401 became a blank
        tab or a silently discarded click. Now that the fetch happens in this
        component, swallowing the rejection would be strictly worse than what
        it replaced: a button that does nothing, with no error anywhere.
      */}
      <ErrorBanner error={downloadError} />

      {/*
        A FAILED handout shows its code too, and that is the whole point of
        storing it.

        This used to be `status === "ready"`, from a time when a failed row
        stored nothing: `_run_sandbox_recipe` raised before the caller ever
        assigned `source_code`, so `len(source_code) == 0` was measured on real
        failed rows and there was genuinely nothing to reveal. The backend now
        carries both attempts through the raise (`HandoutFailure.source_code`),
        so the code exists exactly when somebody needs to read it -- and the
        card was still hiding it.

        Leaving the gate would have shipped that fix invisible: stored, returned
        by `HandoutDetail`, never rendered. That is the "green over a product
        that is not there" shape this whole change set exists to correct, so it
        is worth the two extra words here.

        `pending` stays excluded, because for a row still running there is
        nothing to fetch and the spinner above already says so.
      */}
      {terminal && (
        <div className="mt-2" onClick={onRevealClick}>
          {/*
            The generation step, shown rather than hidden. NotebookLM does not
            do this; for a product whose entire purpose is making a pipeline
            inspectable, concealing the one step that produces an artefact would
            be the single place it stopped practising what it teaches -- and it
            is also the fastest route to understanding why a chart is wrong.

            A study sheet has no code at all: the model writes its markdown
            directly, with no sandbox in the loop, so the disclosure holds the
            sheet itself instead. Same request, same fetch-on-first-open, two
            different things worth reading.
          */}
          {/*
            The label names EVERYTHING behind it, not just the code.

            Found by opening the page, 2026-08-17, and by nothing else: feature
            05 writes a slide-title outline into `preview_text` so a user can see
            a deck is empty without downloading it and opening PowerPoint -- and
            it then sat behind a disclosure called "Code", which is the last
            place anyone looks for slide titles. The outline was written,
            fetched, rendered and reachable; every assertion passed; the feature
            was unusable for the thing it exists for.

            A deck and a table now say "Outline and code", so the word a user is
            looking for is on the control they have to click.
          */}
          <Reveal
            summary={REVEAL_SUMMARY[handout.kind] ?? "Code"}
            testId="handout-reveal"
          >
            <ErrorBanner error={detailError} />

            {loadingDetail && <Spinner label="Loading this handout" />}

            {!loadingDetail && detail && (
              <div className="min-w-0 space-y-3">
                {detail.preview_text && (
                  <div
                    data-testid="handout-preview"
                    // A recessed well, and no font utility on the wrapper: what
                    // is inside came out of the corpus, so `Markdown` sets it in
                    // serif through `.gw-prose`. A `font-mono` here would be
                    // overridden by that rule and read, wrongly, as though this
                    // block were machine text.
                    className={`${WELL} min-w-0 p-3 break-words`}
                  >
                    {/* Through the shared map, never a second copy of it: a
                        study sheet is mostly tables, and tables are a GFM
                        extension that bare CommonMark renders as paragraphs
                        full of pipes. `Markdown` bundles remark-gfm with the
                        component map so a caller cannot get one without the
                        other. */}
                    <Markdown source={detail.preview_text} />
                  </div>
                )}

                {detail.source_code && (
                  /* `whitespace-pre`, not `pre-wrap`: indentation IS Python's
                     syntax, so a wrapped line is a different program. Same
                     block the trace panel uses for the same reason. */
                  <pre
                    data-testid="handout-source"
                    // `text-ink` on a well, not a coloured face. Syntax
                    // colouring by fiat -- one hue for the whole program --
                    // says nothing about the code and spends the accent on
                    // something that is not evidence.
                    className={`${WELL} max-h-64 overflow-auto p-3 font-mono text-xs leading-relaxed whitespace-pre text-ink`}
                  >
                    {detail.source_code}
                  </pre>
                )}

                {!detail.preview_text && !detail.source_code && (
                  <p className="text-xs text-muted">
                    This handout recorded no source code or preview text.
                  </p>
                )}
              </div>
            )}
          </Reveal>
        </div>
      )}
    </li>
  );
}

/** Whole seconds since `iso`, floored at zero -- a client clock running a
 *  little ahead of the server's must not render "-2s". */
function elapsedSeconds(iso: string, now: number): number {
  const at = Date.parse(iso);
  if (Number.isNaN(at)) return 0;
  return Math.max(0, Math.round((now - at) / 1000));
}

/**
 * "2 min ago", and the raw string if the timestamp does not parse.
 *
 * Relative rather than absolute because the question a handout list answers is
 * "is this the one I just made", not "what date was this". Falling back to the
 * input rather than to "Invalid Date" follows `formatTimestamp`: a raw ISO
 * string looks like data, and "Invalid Date" looks like a bug in the UI.
 */
function relativeTime(iso: string, now: number): string {
  const at = Date.parse(iso);
  if (Number.isNaN(at)) return iso;

  const seconds = Math.max(0, Math.round((now - at) / 1000));
  if (seconds < 60) return "just now";

  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;

  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;

  return `${Math.round(hours / 24)} d ago`;
}
