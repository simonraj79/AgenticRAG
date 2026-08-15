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
 */

import type { Citation } from "../lib/types.ts";
import { formatScore } from "../lib/format.ts";

export default function CitationCard({
  citation,
  active,
  cardRef,
}: {
  citation: Citation;
  /** True while this is the card the user just jumped to. */
  active: boolean;
  /** Registers the DOM node with the message, which owns the marker -> element
   *  map that a chip click looks up. */
  cardRef: (element: HTMLLIElement | null) => void;
}) {
  return (
    <li
      ref={cardRef}
      tabIndex={-1}
      data-testid="citation-card"
      aria-current={active ? "true" : undefined}
      className={`scroll-mt-4 rounded-lg border p-3 outline-none transition ${
        active
          ? "border-sky-600 bg-sky-950/30 ring-1 ring-sky-600/50"
          : "border-slate-800 bg-slate-950/40"
      }`}
    >
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <span className="inline-flex h-5 min-w-5 items-center justify-center rounded border border-sky-700/70 bg-sky-950/60 px-1 font-mono text-xs font-semibold text-sky-300">
          {citation.marker}
        </span>
        <span className="text-sm font-medium text-slate-200">{citation.filename}</span>
        <span className="text-xs text-slate-500">
          chunk {citation.chunk_index} &middot; rank {citation.rank}
        </span>

        {/*
          Both scores, side by side. Similarity is what the embedding ranked;
          rerank is what Cohere thought of that ranking. The gap between the two
          IS the Stage 2 demo -- if reranking never reorders anything, it is not
          earning its Singapore -> US round trip.
        */}
        <span className="ml-auto font-mono text-xs text-slate-400">
          sim {formatScore(citation.similarity_score)} &middot; rerank{" "}
          {formatScore(citation.rerank_score)}
        </span>
      </div>

      <p className="mt-2 text-xs leading-relaxed text-slate-400">{citation.text_preview}</p>
    </li>
  );
}
