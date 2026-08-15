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
 * paragraph run together.
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
import type { Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import type { ChatMessage } from "../lib/types.ts";
import { formatDuration, formatTimestamp } from "../lib/format.ts";
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
  const components = useMemo(() => markdownComponents(inline), [inline]);
  const sourcesId = `sources-${message.query_id}`;

  return (
    <li data-testid="chat-message" className="space-y-2.5">
      <div className="flex justify-end">
        <div className="max-w-[85%] break-words rounded-2xl rounded-br-sm border border-slate-700 bg-slate-800/70 px-4 py-2.5 text-sm whitespace-pre-wrap text-slate-100">
          {message.question}
        </div>
      </div>

      <div className="rounded-2xl rounded-bl-sm border border-slate-800 bg-slate-900/50 p-4">
        {message.rewritten_question && (
          /*
            The single most useful thing a multi-turn RAG can tell a user about
            itself, and it is invisible everywhere else. "What about the second
            one?" is embedded as "What is the second stage of Polya's method?",
            and when that resolution grabs the wrong antecedent the answer is
            confidently about the wrong thing with no visible cause. Shown
            above the answer rather than inside the trace panel because a cause
            you have to open a panel to see is a cause nobody sees.
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
            and not also the latency and the model name. */}
        <div data-testid="chat-answer" className="text-sm leading-relaxed text-slate-100">
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

          <span className="ml-auto text-xs text-slate-400">
            {formatTimestamp(message.created_at)}
            {message.latency_ms !== null ? ` · ${formatDuration(message.latency_ms)}` : ""}
            {message.model_used ? ` · ${message.model_used}` : ""}
          </span>
        </div>

        {/* On its own line, not in the row above: the panel it opens is
            full-width, and a flex item that expands to a JSON payload drags the
            whole row's layout with it. */}
        <TracePanel queryId={message.query_id} />

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
      className="mx-0.5 inline-flex h-6 min-w-6 items-center justify-center rounded border border-sky-700 bg-sky-950/60 px-1 align-super font-mono text-[11px] font-semibold text-sky-200 transition hover:border-sky-400 hover:bg-sky-900/60 hover:text-sky-50"
    >
      {marker}
    </button>
  );
}

/**
 * Tag-by-tag styling for the rendered answer.
 *
 * Long because Tailwind's preflight removes every default: without `list-disc`
 * a bulleted list has no bullets, without `font-semibold` bold is not bold. The
 * alternative is the typography plugin, which is a build dependency this
 * project does not carry -- and `package-lock.json` has to stay consistent for
 * Render's `npm ci`.
 *
 * `inline` is applied to every tag that can hold text directly. `code` and
 * `pre` deliberately skip it: a chunk of code containing `[0]` is an array
 * index, not a citation.
 */
function markdownComponents(inline: (children: ReactNode) => ReactNode): Components {
  return {
    p: ({ children }) => <p className="mb-3 last:mb-0">{inline(children)}</p>,
    ul: ({ children }) => (
      <ul className="mb-3 list-disc space-y-1 pl-5 last:mb-0">{children}</ul>
    ),
    ol: ({ children }) => (
      <ol className="mb-3 list-decimal space-y-1 pl-5 last:mb-0">{children}</ol>
    ),
    li: ({ children }) => <li className="leading-relaxed">{inline(children)}</li>,
    h1: ({ children }) => (
      <h1 className="mt-4 mb-2 text-base font-semibold text-slate-50 first:mt-0">
        {inline(children)}
      </h1>
    ),
    h2: ({ children }) => (
      <h2 className="mt-4 mb-2 text-base font-semibold text-slate-50 first:mt-0">
        {inline(children)}
      </h2>
    ),
    h3: ({ children }) => (
      <h3 className="mt-3 mb-1.5 text-sm font-semibold text-slate-100 first:mt-0">
        {inline(children)}
      </h3>
    ),
    h4: ({ children }) => (
      <h4 className="mt-3 mb-1.5 text-sm font-semibold text-slate-200 first:mt-0">
        {inline(children)}
      </h4>
    ),
    strong: ({ children }) => (
      <strong className="font-semibold text-slate-50">{inline(children)}</strong>
    ),
    em: ({ children }) => <em className="italic">{inline(children)}</em>,
    blockquote: ({ children }) => (
      <blockquote className="mb-3 border-l-2 border-slate-700 pl-3 text-slate-300 last:mb-0">
        {children}
      </blockquote>
    ),
    a: ({ children, href }) => (
      <a
        href={href}
        target="_blank"
        rel="noreferrer noopener"
        className="text-sky-300 underline underline-offset-2 transition hover:text-sky-200"
      >
        {children}
      </a>
    ),
    code: ({ children }) => (
      <code className="rounded bg-slate-950 px-1 py-0.5 font-mono text-[0.85em] text-emerald-200">
        {children}
      </code>
    ),
    pre: ({ children }) => (
      <pre className="mb-3 overflow-x-auto rounded-md bg-slate-950 p-3 text-xs leading-relaxed last:mb-0 [&>code]:bg-transparent [&>code]:p-0">
        {children}
      </pre>
    ),
    hr: () => <hr className="my-4 border-slate-800" />,
    table: ({ children }) => (
      <div className="mb-3 overflow-x-auto last:mb-0">
        <table className="w-full border-collapse text-xs">{children}</table>
      </div>
    ),
    th: ({ children }) => (
      <th className="border border-slate-800 bg-slate-900 px-2 py-1 text-left font-semibold text-slate-200">
        {inline(children)}
      </th>
    ),
    td: ({ children }) => (
      <td className="border border-slate-800 px-2 py-1 align-top">{inline(children)}</td>
    ),
  };
}
