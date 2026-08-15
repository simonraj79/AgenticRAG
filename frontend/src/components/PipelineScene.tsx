/**
 * The landing hero: the pipeline this app builds, drawn in depth.
 *
 * Six panes on the Z axis under one `perspective`, one per stage, with a pulse
 * travelling front-ward through them on a loop. It is a picture of the thing
 * the product does -- a question entering at the back and arriving as a scored
 * answer at the front -- rather than an abstract shape, which is the only
 * reason a decorative animation earns a place on the first screen a person
 * sees.
 *
 * **`aria-hidden`, and that is load-bearing rather than lazy.** Every stage
 * name here is repeated as real text in the hero beneath it, so a screen reader
 * that skips this subtree loses nothing at all. Announcing six divs of pipeline
 * jargon would be strictly worse than silence.
 *
 * **Nothing here is random.** Positions are computed from the index by two pure
 * helpers below. `Math.random()` in a render body would reshuffle every dot on
 * each re-render -- and this component re-renders whenever the dev-login form
 * beside it takes a keystroke, so the bug would look like the page twitching
 * while you type rather than like anything to do with randomness.
 *
 * Motion stops entirely under `prefers-reduced-motion` via the global rule in
 * index.css. The scene is still legible frozen: the panes keep their depth,
 * because the offsets are transforms, not animation state.
 */

const STAGES = [
  { key: "ingest", label: "Ingest", detail: "PDF, DOCX, Markdown" },
  { key: "embed", label: "Embed", detail: "gemini-embedding-2" },
  { key: "retrieve", label: "Retrieve", detail: "Pinecone, top-k" },
  { key: "rerank", label: "Rerank", detail: "Cohere, top-n" },
  { key: "generate", label: "Generate", detail: "grounded, or it declines" },
  { key: "measure", label: "Measure", detail: "Ragas, four metrics" },
] as const;

/** Depth per pane. Front pane at 0, each one 108px further back. */
function depthFor(index: number): number {
  return -index * 108;
}

/**
 * A dot's resting position, from its index alone.
 *
 * Two incommensurable multipliers (golden-ratio-ish) so the dots scatter
 * instead of banding, without a random source. The same index always yields the
 * same point, which is what keeps them still across re-renders.
 */
function dotAt(index: number): { left: string; top: string; delay: string; depth: number } {
  const x = (index * 61.8) % 100;
  const y = (index * 38.2 + index * index * 3.7) % 100;
  return {
    left: `${x.toFixed(2)}%`,
    top: `${y.toFixed(2)}%`,
    delay: `${((index * 0.47) % 4).toFixed(2)}s`,
    depth: -((index * 53) % 480),
  };
}

const DOTS = Array.from({ length: 18 }, (_, index) => dotAt(index));

export default function PipelineScene() {
  return (
    <div
      aria-hidden="true"
      className="gw-scene pointer-events-none relative h-[300px] w-full select-none sm:h-[380px] lg:h-[460px]"
    >
      {/*
        The glow is a SIBLING of the rig, never a parent. A blur filter on an
        ancestor flattens `preserve-3d` and the whole scene silently collapses
        to 2D -- see the note in index.css.
      */}
      <div className="absolute inset-0 flex items-center justify-center">
        <div
          className="h-56 w-56 rounded-full bg-emerald-500/25 blur-3xl sm:h-72 sm:w-72"
          style={{ animation: "gw-glow 9s ease-in-out infinite" }}
        />
      </div>
      <div className="absolute inset-0 flex items-center justify-center">
        <div
          className="h-40 w-72 rounded-full bg-sky-500/20 blur-3xl"
          style={{ animation: "gw-glow 11s ease-in-out infinite reverse" }}
        />
      </div>

      <div className="gw-rig absolute inset-0 flex items-center justify-center">
        {/* Chunks suspended in the vector space, behind the panes. */}
        {DOTS.map((dot, index) => (
          <span
            key={index}
            className="absolute h-1.5 w-1.5 rounded-full bg-emerald-300"
            style={{
              left: dot.left,
              top: dot.top,
              transform: `translateZ(${dot.depth}px)`,
              animation: `gw-drift ${7 + (index % 5)}s ease-in-out ${dot.delay} infinite`,
            }}
          />
        ))}

        {STAGES.map((stage, index) => (
          <div
            key={stage.key}
            className="gw-pane absolute flex h-[132px] w-[248px] flex-col justify-between rounded-xl border border-emerald-400/25 bg-slate-900/40 p-4 shadow-[0_0_40px_-12px_rgba(16,185,129,0.5)] sm:h-[150px] sm:w-[290px]"
            data-stage={stage.key}
            style={
              {
                "--z": `${depthFor(index)}px`,
                "--y": `${index * 5}px`,
                // Panes further back are dimmer. Depth cueing by opacity, not by
                // fog: the panes are translucent, so a fog layer between them
                // would tint the ones in front of it too.
                opacity: 1 - index * 0.11,
              } as React.CSSProperties
            }
          >
            <div className="flex items-center justify-between">
              <span className="text-[11px] font-semibold uppercase tracking-[0.18em] text-emerald-300">
                {stage.label}
              </span>
              <span className="font-mono text-[10px] text-slate-500">
                {String(index + 1).padStart(2, "0")}
              </span>
            </div>

            {/* A few bars standing in for the content moving through. */}
            <div className="space-y-1.5">
              <div className="h-1 w-full rounded-full bg-gradient-to-r from-emerald-400/60 to-transparent" />
              <div className="h-1 w-4/5 rounded-full bg-gradient-to-r from-sky-400/45 to-transparent" />
              <div className="h-1 w-3/5 rounded-full bg-gradient-to-r from-emerald-400/30 to-transparent" />
            </div>

            <span className="text-[11px] text-slate-400">{stage.detail}</span>
          </div>
        ))}

        {/* The query, travelling the stages back-to-front on a loop. */}
        <div
          className="absolute h-[132px] w-[248px] rounded-xl border-2 border-emerald-300/80 bg-emerald-400/10 shadow-[0_0_60px_-4px_rgba(52,211,153,0.85)] sm:h-[150px] sm:w-[290px]"
          style={{ animation: "gw-pulse 6.5s cubic-bezier(0.4, 0, 0.6, 1) infinite" }}
        />
      </div>
    </div>
  );
}
