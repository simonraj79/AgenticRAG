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
 * | `failed`  | rose border, the error text verbatim, and "Try again"         |
 *
 * Anything else renders as itself. `status`, `kind` and `origin` are `String(16)`
 * columns rather than enums precisely so a fifth recipe costs a seed row instead
 * of a migration, so an unrecognised value must render rather than throw -- the
 * same rule `StatusPill` and `CategoryBadge` already follow.
 */

import { useRef, useState } from "react";
import type { MouseEvent } from "react";
import { handouts } from "../lib/api.ts";
import type { Handout, HandoutDetail } from "../lib/types.ts";
import { formatBytes } from "../lib/format.ts";
import { Markdown } from "../lib/markdown.tsx";
import { ConfirmDeleteButton, ErrorBanner, Reveal, Spinner, errorMessage } from "./ui.tsx";

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
  // "Has a request been sent", as a ref rather than state: it is read inside an
  // event handler to decide whether to fetch, and making it state would put a
  // render between the decision and the flag.
  const requested = useRef(false);

  const terminal = handout.status === "ready" || handout.status === "failed";
  const kindLabel = KIND_LABELS[handout.kind] ?? handout.kind;
  const originLabel = ORIGIN_LABELS[handout.origin] ?? handout.origin;

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
      className={`min-w-0 rounded-lg border p-3 ${
        handout.status === "failed"
          ? "border-rose-800/60 bg-rose-950/20"
          : "border-slate-800 bg-slate-900/40"
      }`}
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
        {handout.kind === "chart" && handout.status === "ready" && !thumbFailed && (
          <img
            data-testid="handout-thumb"
            src={handouts.downloadUrl(agentId, handout.id)}
            alt={handout.title}
            loading="lazy"
            onError={() => setThumbFailed(true)}
            className="h-14 w-14 shrink-0 rounded border border-slate-800 bg-slate-950 object-cover"
          />
        )}

        <div className="min-w-0 flex-1">
          <p className="truncate text-sm text-slate-200" title={handout.title}>
            {handout.title}
          </p>
          <p className="mt-0.5 text-xs text-slate-400">
            {kindLabel}
            {handout.status === "ready" && (
              <>
                <span className="text-slate-600"> &middot; </span>
                {formatBytes(handout.byte_size)}
              </>
            )}
            <span className="text-slate-600"> &middot; </span>
            {relativeTime(handout.created_at, now)}
            <span className="text-slate-600"> &middot; </span>
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
          <span className="font-mono text-xs text-slate-400">
            {elapsedSeconds(handout.created_at, now)}s
          </span>
        </div>
      )}

      {handout.status === "failed" && (
        <p
          data-testid="handout-error"
          className="mt-2 text-xs whitespace-pre-wrap text-rose-200"
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

            The bare `download` attribute is honest about doing nothing here --
            browsers ignore it cross-origin -- and is kept because the server's
            header is what actually forces the save, and a reader should not
            have to wonder whether the attribute was forgotten.
          */
          <a
            data-testid="handout-download"
            href={handouts.downloadUrl(agentId, handout.id)}
            download
            className="inline-flex min-h-11 items-center rounded-md border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-300 transition hover:border-slate-600 hover:text-slate-100"
          >
            Download
          </a>
        )}

        {handout.status === "failed" && (
          <button
            type="button"
            data-testid="handout-retry"
            onClick={onRetry}
            className="min-h-11 rounded-md border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-300 transition hover:border-slate-600 hover:text-slate-100"
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

      {handout.status === "ready" && (
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
          <Reveal
            summary={handout.kind === "sheet" ? "Preview" : "Code"}
            testId="handout-reveal"
          >
            <ErrorBanner error={detailError} />

            {loadingDetail && <Spinner label="Loading this handout" />}

            {!loadingDetail && detail && (
              <div className="min-w-0 space-y-3">
                {detail.preview_text && (
                  <div
                    data-testid="handout-preview"
                    className="min-w-0 text-sm leading-relaxed break-words text-slate-300"
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
                    className="max-h-64 overflow-auto rounded bg-slate-950 p-2 font-mono text-xs leading-relaxed whitespace-pre text-emerald-200"
                  >
                    {detail.source_code}
                  </pre>
                )}

                {!detail.preview_text && !detail.source_code && (
                  <p className="text-xs text-slate-400">
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
