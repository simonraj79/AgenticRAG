/**
 * One retrieved chunk, presented as the evidence behind one inline marker.
 *
 * The card leads with the marker glyph rather than the filename because the
 * user arrives here by clicking `[2]` in the answer: the first thing they need
 * to confirm is that they landed on 2. The rank is shown separately and is a
 * different fact -- marker is "which bracket in the prose", rank is "where the
 * retriever placed it".
 *
 * **`tabIndex={-1}` is what makes the chip work.** Focus is how a screen reader
 * and the keyboard find out that anything happened when the chip was clicked;
 * without a tabindex the element is not focusable at all and `focus()` is a
 * silent no-op. Negative, not zero, so the card stays out of the natural tab
 * order -- tabbing through a thread should reach the chips and the toggles, not
 * every chunk preview.
 *
 * ## What changed in the redesign, and the constraint it had to respect
 *
 * This card now lives in an evidence MARGIN beside the answer at >=1280px
 * rather than in a list underneath it, so at that width it is on screen for
 * every turn without anyone opening anything. (1280 rather than 1024 because of
 * the arithmetic in the `.gw-apparatus` media query -- below it the prose column
 * drops to a ~40-character measure, which is worse than having no margin.)
 *
 * That could not be done by simply un-collapsing the old card. The reason
 * sources were collapsed is recorded in `Message.tsx` and is still true: "a
 * thread of ten turns each showing three chunk previews is unreadable". A
 * permanently-open margin full of full-length passages would reintroduce
 * exactly that, having moved it sideways.
 *
 * So the card became two things at once. At rest it is an APPARATUS ENTRY --
 * marker, filename, rank, score, and two lines of the passage. Activated by its
 * chip it becomes the EVIDENCE -- the passage in full, in the reading face,
 * because at that moment the user is checking a specific claim against a
 * specific chunk and truncation is the one thing that would defeat them.
 *
 * `line-clamp-2` is therefore load-bearing rather than cosmetic. It is what
 * buys the margin its permanence.
 */

import type { CSSProperties } from "react";
import type { Citation } from "../lib/types.ts";
import { formatScore } from "../lib/format.ts";

export default function CitationCard({
  citation,
  active,
  cardRef,
  peakStrength,
}: {
  citation: Citation;
  /** True while this is the card the user just jumped to. */
  active: boolean;
  /** Registers the DOM node with the message, which owns the marker -> element
   *  map that a chip click looks up. */
  cardRef: (element: HTMLLIElement | null) => void;
  /** The highest score among the citations on THIS turn. See the note below --
   *  the bar is a within-turn comparison, so it needs the turn's own scale. */
  peakStrength: number | null;
}) {
  /*
    The score, as a width.

    `formatScore` renders the real number beside this bar and that text is the
    accessible value -- the bar is `aria-hidden` and tells a screen reader
    nothing it is missing. What it adds for a sighted reader is comparison:
    stacked bars answer "did reranking actually reorder anything" at a glance,
    which is the Stage 2 teaching point and is genuinely hard to read off a
    column of four-decimal numbers.

    **The bar is scaled against the turn's own peak, not against 1.0, and that is
    a correction rather than a preference.** Drawing it as `score * 100%`
    assumed a 0-1 scale. Measured against this repo's live data, Cohere's
    `rerank-v3.5` returns values around 0.02 on real passages -- so every bar
    computed to about 2% and all of them rendered as the same near-empty stub.
    The chart was drawn, and conveyed nothing.

    That is the same defect as the Admin cost chart's `Math.max(1, ...)` peak, in
    a different file on the same day, and the shared lesson is worth stating: a
    bar whose scale is ASSUMED rather than derived from its own data will render
    plausibly and mean nothing, and nothing raises.

    Relative is also the honest reading. The question this bar answers is "which
    of these passages did the reranker prefer", which is a question about this
    turn. The absolute number stays in the text next to it, so the exact value is
    never lost -- only the drawing is normalised.

    Falls back to similarity when reranking is off for this agent, and draws
    nothing at all when neither exists (an old row, or a refusal whose passages
    were never scored). A zero-width bar would claim "scored zero", which is a
    different and wrong statement.
  */
  const strength = citation.rerank_score ?? citation.similarity_score;
  const hasStrength = typeof strength === "number" && Number.isFinite(strength);
  const fraction =
    hasStrength && peakStrength && peakStrength > 0
      ? Math.max(0.04, Math.min(1, strength / peakStrength))
      : 0;

  return (
    <li
      ref={cardRef}
      tabIndex={-1}
      data-testid="citation-card"
      aria-current={active ? "true" : undefined}
      aria-label={`Source ${citation.marker}: ${citation.filename}, chunk ${citation.chunk_index}, retrieval rank ${citation.rank}`}
      className={`scroll-mt-4 rounded-md border p-3 outline-none transition ${
        active
          ? "border-accent bg-accent-soft"
          : "border-line bg-surface hover:border-line-strong"
      }`}
    >
      <div className="flex items-baseline gap-2">
        {/*
          The same glyph as the inline chip in the answer -- same shape, same
          size, same colour. That correspondence IS the apparatus: the reader
          matches a bracket in the prose to an entry in the margin by sight, and
          two different treatments of the same number would make them do it by
          reading.
        */}
        <span
          aria-hidden="true"
          className="inline-flex h-5 min-w-5 shrink-0 items-center justify-center rounded-sm border border-accent-line bg-accent-soft px-1 font-mono text-xs font-semibold text-accent"
        >
          {citation.marker}
        </span>
        <span className="min-w-0 flex-1 truncate text-xs font-medium text-ink">
          {citation.filename}
        </span>
      </div>

      <p className="mt-1.5 font-mono text-xs text-faint">
        chunk {citation.chunk_index} &middot; rank {citation.rank}
      </p>

      {hasStrength && (
        <div className="mt-2 flex items-center gap-2">
          {/*
            The strata rule: the one structural motif in this design, used only
            where something is ordered by strength. Width is the datum. See the
            `.gw-strata` block in index.css.
          */}
          <span className="h-0.5 min-w-0 flex-1 rounded-full bg-line" aria-hidden="true">
            <span
              className="gw-strata"
              style={{ "--gw-strata-width": `${Math.round(fraction * 100)}%` } as CSSProperties}
            />
          </span>
          <span className="shrink-0 font-mono text-xs tabular-nums text-muted">
            {formatScore(strength)}
          </span>
        </div>
      )}

      {/*
        Both scores in full, and only once the card is the one being questioned.
        Similarity is what the embedding ranked; rerank is what Cohere thought of
        that ranking, and the gap between them IS the Stage 2 demo -- if
        reranking never reorders anything it is not earning its Singapore -> US
        round trip. That is a detail worth having and not worth carrying in the
        margin of every turn, so it arrives with the rest of the evidence.
      */}
      {active && (
        <p className="mt-1.5 font-mono text-xs text-faint">
          sim {formatScore(citation.similarity_score)} &middot; rerank{" "}
          {formatScore(citation.rerank_score)}
        </p>
      )}

      {/*
        The passage itself, and the one place in this component that is set in
        the reading face -- it is verbatim text out of the user's corpus, which
        is exactly what serif means in this design.

        Clamped to two lines at rest and whole when active. See the note at the
        top: that clamp is what lets the margin stay open on every turn.
      */}
      <p
        className={`mt-2 border-t border-line pt-2 font-serif text-xs leading-relaxed text-muted ${
          active ? "" : "line-clamp-2"
        }`}
      >
        {citation.text_preview}
      </p>
    </li>
  );
}
