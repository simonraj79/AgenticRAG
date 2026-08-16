/**
 * One turn: the question asked, the answer given, and the evidence for it.
 *
 * **Markdown, not plain text.** Every persona in this system emits numbered
 * steps, headings and bold emphasis -- a Polya coach literally answers in four
 * stages, a quiz generator in a list. Rendered as plain text that arrives as
 * literal `1.` and `**understand**`, which reads as a broken model rather than
 * a missing renderer. `react-markdown` plus `remark-gfm` is the whole fix, and
 * Tailwind's preflight is why every tag needs an explicit class: the reset
 * strips list markers and heading sizes, so an unstyled `<ul>` renders as a
 * paragraph run together. The tag-by-tag map that supplies those classes
 * started here and now lives in `lib/markdown.tsx`, because the handouts panel
 * renders a study sheet through the same pipeline and two copies of it drift.
 *
 * **The chips are the point of the feature.** The answer carries `[1]`, `[2]`
 * inline; each becomes a superscript button that opens the sources list and
 * focuses the matching card. Citation to source in one motion, with no tab
 * switch and no scrolling to hunt for a filename, is what "traceable" has to
 * mean -- an answer here and its evidence somewhere else is the arrangement
 * this replaced.
 *
 * Sources start collapsed. A thread of ten turns each showing three chunk
 * previews is unreadable, and the chip is the affordance that reveals the one
 * chunk actually being questioned.
 */

import { Children, useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { ChatMessage } from "../lib/types.ts";
import { formatDuration, formatTimestamp } from "../lib/format.ts";
// The component map lives in `lib/markdown.tsx` rather than here, because the
// handouts panel renders a study sheet's preview through the same pipeline and
// a copied seventy-line map is how the answer and the handout end up with
// different heading sizes six months apart. `createMarkdownComponents` takes
// the `inline` hook that turns `[1]` into a citation chip; the panel calls the
// same factory with no hook.
import { createMarkdownComponents } from "../lib/markdown.tsx";
import CitationCard from "./CitationCard.tsx";
import TracePanel from "./TracePanel.tsx";

/** `[1]` .. `[999]`. Global because `matchAll` throws without it. */
const MARKER_PATTERN = /\[(\d{1,3})\]/g;

export default function Message({ message }: { message: ChatMessage }) {
  const [sourcesOpen, setSourcesOpen] = useState(false);
  const [activeMarker, setActiveMarker] = useState<number | null>(null);
  // Bumped on every chip click so clicking the SAME chip twice re-runs the
  // focus effect. Without it the second click is a no-op, because the state it
  // would set is the state already there.
  const [focusNonce, setFocusNonce] = useState(0);
  // Bumped by the tool-activity chip, which is a summary whose detail lives in
  // the trace panel. A nonce for the same reason as the one above: a boolean
  // would already be true the second time the chip is pressed.
  const [traceSignal, setTraceSignal] = useState(0);

  const cards = useRef(new Map<number, HTMLLIElement>());

  /** Markers the answer is allowed to turn into chips. A model can write `[4]`
   *  with three citations attached; that stays literal text rather than
   *  becoming a button that reveals nothing. */
  const citationsByMarker = useMemo(
    () => new Map(message.citations.map((citation) => [citation.marker, citation.filename])),
    [message.citations],
  );

  const selectCitation = useCallback((marker: number) => {
    setSourcesOpen(true);
    setActiveMarker(marker);
    setFocusNonce((value) => value + 1);
  }, []);

  // Runs after the commit that mounts the sources list, which is why opening
  // and focusing can be one click: the card does not exist at the moment the
  // chip is pressed.
  useEffect(() => {
    if (activeMarker === null) return;
    const card = cards.current.get(activeMarker);
    if (!card) return;
    card.scrollIntoView({ behavior: "smooth", block: "nearest" });
    card.focus({ preventScroll: true });
  }, [activeMarker, focusNonce]);

  const inline = useCallback(
    (children: ReactNode) => withCitations(children, citationsByMarker, selectCitation),
    [citationsByMarker, selectCitation],
  );
  const components = useMemo(() => createMarkdownComponents(inline), [inline]);
  const sourcesId = `sources-${message.query_id}`;
  const toolActivity = summariseToolActivity(
    message.tool_steps,
    message.handouts.length,
    message.tool_calls,
  );

  return (
    <li data-testid="chat-message" className="space-y-2.5">
      <div className="flex justify-end">
        <div className="max-w-[85%] break-words rounded-2xl rounded-br-sm border border-slate-700 bg-slate-800/70 px-4 py-2.5 text-sm whitespace-pre-wrap text-slate-100">
          {message.question}
        </div>
      </div>

      <div className="rounded-2xl rounded-bl-sm border border-slate-800 bg-slate-900/50 p-4">
        {message.rewritten_changed && message.rewritten_question && (
          /*
            The single most useful thing a multi-turn RAG can tell a user about
            itself, and it is invisible everywhere else. "What about the second
            one?" is embedded as "What is the second stage of Polya's method?",
            and when that resolution grabs the wrong antecedent the answer is
            confidently about the wrong thing with no visible cause. Shown
            above the answer rather than inside the trace panel because a cause
            you have to open a panel to see is a cause nobody sees.

            **Gated on `rewritten_changed`, not on the string being present.**
            The rewriter runs on every turn as of 2026-08-16, so the string is
            almost always there -- and a banner on every message in every thread,
            usually quoting a sentence one word away from the question directly
            above it, is not a weaker version of this affordance. It is the thing
            that makes a reader stop looking at it, so the one turn where the
            rewrite really did grab the wrong antecedent reads as more noise.
          */
          <p
            data-testid="rewritten-question"
            className="mb-3 rounded-md border border-fuchsia-900/50 bg-fuchsia-950/30 px-3 py-2 text-xs text-fuchsia-200"
          >
            <span className="text-fuchsia-400/80">Searched for</span>{" "}
            <span className="italic">&ldquo;{message.rewritten_question}&rdquo;</span>
          </p>
        )}

        {/* The testid sits on the answer alone, so its textContent is the answer
            and not also the latency and the model name.

            `break-words` is on the container rather than on each tag: `pre` and
            `table` carry their own `overflow-x-auto` in the component map, but a
            60-character URL or an unspaced identifier inside an ordinary
            paragraph has nothing to scroll and pushes the bubble past the
            viewport, taking the whole document's width with it. */}
        <div
          data-testid="chat-answer"
          className="text-sm leading-relaxed break-words text-slate-100"
        >
          <Markdown remarkPlugins={[remarkGfm]} components={components}>
            {message.answer ?? ""}
          </Markdown>
        </div>

        {message.refused && (
          // Spelled out because a refusal looks like a failure and is not one.
          // The system prompt forbids answering outside the retrieved context,
          // and declining is the behaviour the golden set scores.
          <p className="mt-3 border-t border-slate-800 pt-3 text-xs text-slate-400">
            The agent declined because the retrieved context did not support an answer.
            That is a correct outcome, not an error.
          </p>
        )}

        <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-2 border-t border-slate-800 pt-3">
          {message.refused && (
            <span className="rounded-full border border-amber-800/60 bg-amber-950/40 px-2 py-0.5 text-xs font-medium text-amber-300">
              refused
            </span>
          )}

          {message.citations.length > 0 && (
            <button
              type="button"
              aria-expanded={sourcesOpen}
              aria-controls={sourcesId}
              onClick={() => setSourcesOpen((open) => !open)}
              className="min-h-11 rounded-md border border-slate-700 bg-slate-900 px-2.5 py-2 text-xs text-slate-300 transition hover:border-slate-600 hover:text-slate-100"
            >
              {sourcesOpen
                ? "Hide retrieved passages"
                : message.refused
                  ? `Passages checked (${message.citations.length})`
                  : `Sources (${message.citations.length})`}
            </button>
          )}

          {toolActivity && (
            /*
              A turn that used tools looked identical to one that did not until
              somebody opened the trace, which is to say: it looked identical.
              This is a SUMMARY, not a control -- it changes nothing about the
              turn -- but it is a button, because the detail it summarises
              already exists one element below and a chip that names something
              unreachable is worse than no chip.
            */
            <button
              type="button"
              data-testid="tool-activity"
              onClick={() => setTraceSignal((value) => value + 1)}
              className="min-h-11 rounded-full border border-cyan-800/60 bg-cyan-950/40 px-2.5 py-2 text-xs font-medium text-cyan-200 transition hover:border-cyan-600 hover:text-cyan-100"
            >
              {toolActivity}
            </button>
          )}

          {message.stopped && (
            /*
              A stopped turn is TRUNCATED, and without this it renders exactly
              like a finished one.

              The flag was set on the message and read nowhere, which is the
              worst version of this: the bubble folds into the transcript
              mid-sentence carrying `refused: false` and `citations: []`, so the
              Sources button is hidden too and there is nothing at all to
              distinguish it from a complete, uncited answer. Worse, the agent
              finishes server-side and commits the WHOLE answer under the same
              `query_id` -- so a reload silently replaces this text with longer
              text, and nothing ever told the reader why.

              Amber rather than rose: this is not an error. The user pressed
              Stop and got what had arrived, which is the behaviour they asked
              for. It is a caveat about completeness, and the palette should say
              so -- rose here would teach them that stopping broke something.
            */
            <span
              data-testid="stopped-chip"
              className="rounded-full border border-amber-800/60 bg-amber-950/30 px-2.5 py-1 text-xs font-medium text-amber-200"
              title="You stopped reading this turn. The agent finished it on the server -- reload to see the whole answer."
            >
              stopped early · reload for the full answer
            </span>
          )}

          <span className="ml-auto text-xs text-slate-400">
            {formatTimestamp(message.created_at)}
            {message.latency_ms !== null ? ` · ${formatDuration(message.latency_ms)}` : ""}
            {message.model_used ? ` · ${message.model_used}` : ""}
          </span>
        </div>

        {/* On its own line, not in the row above: the panel it opens is
            full-width, and a flex item that expands to a JSON payload drags the
            whole row's layout with it. */}
        <TracePanel queryId={message.query_id} openSignal={traceSignal} />

        {sourcesOpen && (
          <div id={sourcesId} className="mt-3">
            <p className="mb-2 text-xs text-slate-400">
              {message.refused
                ? "These are the closest passages the agent checked; they did not support an answer."
                : "Retrieved passage previews used to ground this answer."}
            </p>
            <ol className="space-y-2" aria-label="Retrieved passages">
              {message.citations.map((citation) => (
                <CitationCard
                  key={`${citation.chunk_id}-${citation.marker}`}
                  citation={citation}
                  active={activeMarker === citation.marker}
                  cardRef={(element) => {
                    if (element) cards.current.set(citation.marker, element);
                    else cards.current.delete(citation.marker);
                  }}
                />
              ))}
            </ol>
          </div>
        )}
      </div>
    </li>
  );
}

/**
 * Replace `[n]` in a run of children with clickable chips.
 *
 * **Adjacent strings are joined before splitting.** A marker is only found when
 * it arrives whole inside one child, and it is the markdown parser that decides
 * where the child boundaries fall. Measured against remark: `[1]` with no
 * matching link definition stays a single text node, so a per-child regex would
 * in fact work today. Buffering anyway, because "the parser happens to keep the
 * brackets together" is a property of remark rather than of this code, and the
 * failure it would cause is the invisible kind -- a split marker renders as
 * literal `[1]` and the feature silently does nothing.
 *
 * A marker that DOES have a matching definition elsewhere in the answer becomes
 * a real link and gets no chip. That is correct: at that point the author meant
 * a link.
 *
 * Non-string children pass through untouched, so nested `<strong>` and `<a>`
 * keep their own rendering and get linkified by their own component override
 * one level down. Nothing recurses, which is why `code` and `pre` -- the two
 * places where `[1]` must stay literal -- simply do not call this.
 *
 * Verified end to end against real `react-markdown` output: chips render in
 * paragraphs, list items, table cells and inside `<strong>`; a `[7]` with no
 * matching citation stays literal; inline and fenced code are untouched.
 */
function withCitations(
  children: ReactNode,
  citations: Map<number, string>,
  onSelect: (marker: number) => void,
): ReactNode {
  const parts: ReactNode[] = [];
  let buffer = "";
  let chips = 0;

  function flush(): void {
    if (!buffer) return;

    let cursor = 0;
    for (const match of buffer.matchAll(MARKER_PATTERN)) {
      const marker = Number(match[1]);
      if (!citations.has(marker)) continue;

      const at = match.index ?? 0;
      if (at > cursor) parts.push(buffer.slice(cursor, at));
      parts.push(
        <CitationChip
          key={`chip-${chips}`}
          marker={marker}
          sourceName={citations.get(marker) ?? "retrieved passage"}
          onSelect={onSelect}
        />,
      );
      chips += 1;
      cursor = at + match[0].length;
    }

    if (cursor < buffer.length) parts.push(buffer.slice(cursor));
    buffer = "";
  }

  for (const child of Children.toArray(children)) {
    if (typeof child === "string" || typeof child === "number") {
      buffer += String(child);
      continue;
    }
    flush();
    parts.push(child);
  }
  flush();

  return parts;
}

function CitationChip({
  marker,
  sourceName,
  onSelect,
}: {
  marker: number;
  sourceName: string;
  onSelect: (marker: number) => void;
}) {
  return (
    <button
      type="button"
      data-testid="citation-chip"
      aria-label={`Show source ${marker}: ${sourceName}`}
      onClick={() => onSelect(marker)}
      // `gw-chip` is the 44px hit area, and it is the one place in this app
      // where the tap target and the drawn box deliberately differ. Growing the
      // button itself to `min-h-11` would space every line of the paragraph it
      // sits in by 44px, which trades a touch defect for a typographic one; the
      // pseudo-element in `index.css` gives the finger its area and leaves the
      // prose alone. See the comment there for why the overlap with a
      // neighbouring chip is harmless.
      className="gw-chip mx-0.5 inline-flex h-6 min-w-6 items-center justify-center rounded border border-sky-700 bg-sky-950/60 px-1 align-super font-mono text-[11px] font-semibold text-sky-200 transition hover:border-sky-400 hover:bg-sky-900/60 hover:text-sky-50"
    >
      {marker}
    </button>
  );
}

/**
 * "searched twice · made 1 handout", or `null` when the turn used no tools.
 *
 * Null rather than an empty string, so the caller renders nothing at all rather
 * than an empty pill -- and `tool_steps === 0` with no handouts is the COMMON
 * case, including every turn taken by an agent with tools switched off and every
 * turn recorded before the loop existed.
 *
 * "searched" is the plain reading of the overwhelming majority of tool activity;
 * the second half names the round-trips that produced something instead. The
 * exact per-step detail is in the trace, which is what the chip opens.
 *
 * **It counts CALLS, not ROUNDS, and it did not always have to.** Until
 * 2026-08-16 the generation model emitted at most one call per step, so
 * `tool_steps` was an honest search count. The current model emits two
 * `search_corpus` calls in a single step -- measured 8/8 -- so a turn that ran
 * two retrievals rendered "searched once", understating the work by half in a
 * product whose entire purpose is making the pipeline legible.
 *
 * `Math.max` rather than preferring `tool_calls` outright, because it is 0 on
 * every turn recorded before the server sent it. Taking it unconditionally would
 * relabel an old two-step turn as no searches at all -- replacing an undercount
 * with a wrong count, which is worse, and invisible on exactly the historical
 * conversations nobody re-reads.
 *
 * "once" and "twice" rather than "1 time" and "2 times", because this is a
 * sentence fragment in prose and not a counter.
 */
function summariseToolActivity(
  steps: number,
  handouts: number,
  calls = 0,
): string | null {
  const parts: string[] = [];
  const searches = Math.max(steps, calls);

  if (searches === 1) parts.push("searched once");
  else if (searches === 2) parts.push("searched twice");
  else if (searches > 2) parts.push(`searched ${searches} times`);

  if (handouts === 1) parts.push("made 1 handout");
  else if (handouts > 1) parts.push(`made ${handouts} handouts`);

  return parts.length > 0 ? parts.join(" · ") : null;
}

