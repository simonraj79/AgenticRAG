/**
 * The six stages, drawn as strata.
 *
 * ## What this replaced, and why
 *
 * This was a CSS 3D scene: six translucent panes on the Z axis under a
 * `perspective`, swinging +-22deg, with a pulse travelling through them. It was
 * well built and it was the wrong drawing. Three reasons, in order of weight:
 *
 * 1. **It said nothing true.** Six abstract panes rotating conveys "pipeline,
 *    probably AI" and no fact about this system. The stage names had to be
 *    repeated underneath it as a real `<ol>` precisely because the picture
 *    carried none of them -- an illustration that needs a full text
 *    transcription beside it is decoration wearing a diagram's clothes.
 * 2. **Its geometry did not survive a phone.** The panes are `w-[248px]`,
 *    rotated, inside a `w-full` scene; at 320px they exceed the viewport and the
 *    only thing preventing horizontal scroll was an `overflow-hidden` on the
 *    page root -- so the landing page's `A7 zero horizontal overflow` depended
 *    on a clip two components away.
 * 3. **`preserve-3d` fails silently in two documented ways** -- any `filter`,
 *    `opacity < 1` or non-visible `overflow` on the carrying element flattens
 *    it, and the 3D context must be contiguous, so one ordinary wrapper div ends
 *    it. Both produce a page that renders perfectly, just flat, with nothing in
 *    devtools naming a cause. That is a permanent maintenance tax on a drawing.
 *
 * ## What it draws now
 *
 * A bar per stage, and **the width is the datum**. Retrieval narrows: a whole
 * corpus becomes k=20 neighbours, becomes the top 5 after reranking, becomes one
 * answer. That narrowing is the single most important thing to understand about
 * how this product works and it is the thing the pipeline diagram should show.
 *
 * The same figure is the wordmark -- six ground layers tapering to a point -- so
 * the mark stops being a logo that happens to sit next to a diagram and becomes
 * the diagram, at small size.
 *
 * **Measure is drawn apart, and that is not a styling choice.** The first five
 * stages are one pass narrowing toward an answer; measurement is a separate act
 * performed on the result, against a golden set, and drawing it as a sixth
 * narrowing bar would claim it is part of the funnel. It is the claim this
 * product is actually built on, so it gets the accent and the gap.
 *
 * No `aria-hidden`, and no duplicate text list beneath it. The old scene needed
 * both because it was a picture of nothing; this is an ordered list of labelled
 * stages that happens to be drawn with rules, so a screen reader gets the real
 * thing and the page carries the stage names exactly once.
 */

import type { CSSProperties } from "react";

/**
 * `width` is proportional and deliberately not to scale -- k=20 to top-5 to one
 * answer is a 20x taper, and drawn faithfully the last bar would be invisible.
 * It is an ordering, and the `detail` column carries the real numbers.
 */
const STAGES: { name: string; detail: string; width: number }[] = [
  { name: "Ingest", detail: "your documents, split into chunks", width: 100 },
  { name: "Embed", detail: "768-dimension vectors, one per chunk", width: 84 },
  { name: "Retrieve", detail: "the k nearest to the question", width: 66 },
  { name: "Rerank", detail: "re-scored, and cut to the best few", width: 45 },
  { name: "Generate", detail: "an answer, from those passages only", width: 27 },
];

const MEASURE = {
  name: "Measure",
  detail: "faithfulness, relevance, precision, recall",
};

export default function PipelineScene() {
  return (
    <div className="mt-10">
      <p className="text-[0.6875rem] font-semibold tracking-[0.08em] text-faint uppercase">
        Six stages, every one inspectable
      </p>

      <ol className="mt-4 space-y-2.5">
        {STAGES.map((stage, index) => (
          <li key={stage.name} className="flex items-baseline gap-3">
            <span className="w-4 shrink-0 font-mono text-xs text-faint tabular-nums">
              {index + 1}
            </span>

            <span className="w-[4.5rem] shrink-0 text-xs font-medium text-ink">
              {stage.name}
            </span>

            {/*
              The bar and the sentence share a row rather than stacking, so the
              taper is read down the left edge in one movement. `min-w-0` on the
              wrapper is what stops a long detail string from pushing the bar
              column out at 320px.
            */}
            <span className="flex min-w-0 flex-1 items-center gap-3">
              <span
                aria-hidden="true"
                className="gw-strata h-[3px] shrink-0"
                style={
                  {
                    // `width` directly, NOT `--gw-strata-width`. An inline
                    // `width` beats the class's `width: var(--gw-strata-width)`
                    // regardless of the custom property, so setting both leaves
                    // one of them silently dead -- the kind of line that reads
                    // as intent and does nothing.
                    //
                    // The 0.42 factor keeps the widest bar inside the row: the
                    // bar shares its flex track with the stage description, and
                    // a 100% bar would push that text out at narrow widths.
                    width: `${stage.width * 0.42}%`,
                    // Later stages are fainter as well as shorter, so the taper
                    // still reads when the bars are only a few pixels apart.
                    "--gw-strata-opacity": 0.3 + (stage.width / 100) * 0.55,
                  } as CSSProperties
                }
              />
              <span className="min-w-0 truncate text-xs text-muted">{stage.detail}</span>
            </span>
          </li>
        ))}

        {/*
          Set apart by a rule, not by a gap alone -- a gap reads as spacing, a
          rule reads as a boundary, and this is a boundary: everything above is
          how an answer is produced, and this is how it is checked.
        */}
        <li className="flex items-baseline gap-3 border-t border-line pt-3.5">
          <span className="w-4 shrink-0 font-mono text-xs text-accent tabular-nums">6</span>
          <span className="w-[4.5rem] shrink-0 text-xs font-semibold text-accent">
            {MEASURE.name}
          </span>
          <span className="min-w-0 flex-1 text-xs text-muted">{MEASURE.detail}</span>
        </li>
      </ol>
    </div>
  );
}
