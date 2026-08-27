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
 * ## The apparatus
 *
 * A turn is laid out as a critical edition rather than as a chat exchange: the
 * question as an entry heading, the answer in a reading measure, and the
 * sources in a margin beside it. That is the whole design, and it is here
 * because it is the only arrangement that shows WHICH claim rests on WHICH
 * chunk. A list of sources at the end of an answer says "these five files were
 * involved somewhere", which is the claim this product exists not to make.
 *
 * Two facts about it are worth carrying:
 *
 * **The margin appears at 1280px and not before.** It is `.gw-apparatus` in
 * `index.css`, and the media query there has the arithmetic: inside a chat
 * column that has already given 17rem to the conversation rail, a 15rem margin
 * below 1280px leaves the prose at about forty characters. Narrower than that,
 * the sources collapse behind their toggle exactly as they always did.
 *
 * **Sources are still collapsed by default below that width, and the reason has
 * not changed** -- a thread of ten turns each showing three chunk previews is
 * unreadable. What changed is that `CitationCard` clamps its passage to two
 * lines at rest, which is what makes a permanently-visible margin readable
 * rather than a wall. The chip still expands the one chunk being questioned.
 */

import { Children, useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { ChatMessage } from "../lib/types.ts";
import { formatDuration, formatTimestamp } from "../lib/format.ts";
import { resolveSpecialist } from "../lib/specialists.ts";
// The component map lives in `lib/markdown.tsx` rather than here, because the
// handouts panel renders a study sheet's preview through the same pipeline and
// a copied seventy-line map is how the answer and the handout end up with
// different heading sizes six months apart. `createMarkdownComponents` takes
// the `inline` hook that turns `[1]` into a citation chip; the panel calls the
// same factory with no hook.
import { createMarkdownComponents } from "../lib/markdown.tsx";
import {
  ACCENT_TONE,
  BTN_QUIET,
  EYEBROW,
  NOTICE,
  PILL,
  PILL_NEUTRAL,
  PROSE,
  WARN_TONE,
} from "../lib/styles.ts";
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
  /** The personas that answered, in the order the server routed them. Empty on
   *  a classic agent and on every turn recorded before routing existed, which
   *  is what makes the pill render nothing rather than a hole. */
  const routed = routedSlugs(message);

  /*
    The strongest score on THIS turn, which is the scale every source bar in the
    margin is drawn against.

    It lives here rather than in `CitationCard` because a card cannot see its
    siblings, and the comparison the bar exists to make -- which of these
    passages did the reranker prefer -- is a comparison across them. Computing it
    per card against a fixed 1.0 is what made every bar render as the same
    near-empty stub: Cohere's rerank scores on real passages are around 0.02.
    See the note in `CitationCard` for the full measurement.
  */
  const peakStrength = useMemo(() => {
    const scores = message.citations
      .map((citation) => citation.rerank_score ?? citation.similarity_score)
      .filter((score): score is number => typeof score === "number" && Number.isFinite(score));
    return scores.length > 0 ? Math.max(...scores) : null;
  }, [message.citations]);

  return (
    <li data-testid="chat-message" className="border-t border-line pt-6 first:border-t-0 first:pt-0">
      {/*
        The question, as an entry heading rather than as a bubble.

        A right-aligned bubble says "this is a chat, scroll for more". A rule and
        a heading say "this is an entry, and what follows is its answer" -- which
        is what the thread actually is, and it is what makes ten turns readable
        as a document instead of as a transcript. The eyebrow does the work a
        bubble's alignment used to: it names the speaker in one word, at a size
        that cannot compete with the answer.
      */}
      <div className="border-l-2 border-line-strong pl-3.5">
        <p className={EYEBROW}>Asked</p>
        <p className="mt-1 text-sm font-medium break-words whitespace-pre-wrap text-ink">
          {message.question}
        </p>
      </div>

      <div className="gw-apparatus mt-4">
        {/* ------------------------------------------------ the answer column */}
        <div className="min-w-0">
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

              Accent rather than a warning colour: a rewrite is what the system
              did, not something that went wrong, and the accent is this design's
              colour for "here is how the answer was reached".
            */
            <p
              data-testid="rewritten-question"
              className={`${NOTICE} ${ACCENT_TONE} mb-4`}
            >
              <span className="font-medium">Searched for</span>{" "}
              <span className="font-serif italic">
                &ldquo;{message.rewritten_question}&rdquo;
              </span>
            </p>
          )}

          {/* The testid sits on the answer alone, so its textContent is the answer
              and not also the latency and the model name.

              `break-words` is here rather than on each tag: `pre` and `table`
              carry their own `overflow-x-auto` in the component map, but a
              60-character URL or an unspaced identifier inside an ordinary
              paragraph has nothing to scroll and pushes the column past the
              viewport, taking the whole document's width with it.

              `PROSE` is `.gw-prose` -- the serif reading face, the 65ch measure
              and the inter-block rhythm. Serif is not decoration here: it is the
              provenance signal this design runs on. Sans is the harness
              speaking; serif is text assembled out of the user's own documents. */}
          <div
            data-testid="chat-answer"
            className={`${PROSE} break-words`}
          >
            <Markdown remarkPlugins={[remarkGfm]} components={components}>
              {message.answer ?? ""}
            </Markdown>
          </div>

          {message.refused && (
            // Spelled out because a refusal looks like a failure and is not one.
            // The system prompt forbids answering outside the retrieved context,
            // and declining is the behaviour the golden set scores.
            <p className="mt-4 border-t border-line pt-3 text-xs leading-relaxed text-muted">
              The agent declined because the retrieved context did not support an answer.
              That is a correct outcome, not an error.
            </p>
          )}

          <div className="mt-4 flex flex-wrap items-center gap-x-3 gap-y-2 border-t border-line pt-3">
            {message.refused && (
              <span className={`${PILL} ${WARN_TONE}`}>refused</span>
            )}

            {/*
              The sources toggle exists only where the margin does not. Above
              1280px the apparatus is on screen permanently and a button
              offering to reveal what is already visible is worse than no
              button -- so it is `xl:hidden`, matching the `.gw-apparatus` media
              query exactly. The two must move together.
            */}
            {message.citations.length > 0 && (
              <button
                type="button"
                aria-expanded={sourcesOpen}
                aria-controls={sourcesId}
                onClick={() => setSourcesOpen((open) => !open)}
                className={`${BTN_QUIET} border border-line text-xs xl:hidden`}
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
                className={`${PILL_NEUTRAL} min-h-11 transition hover:border-line-strong hover:text-ink`}
              >
                {toolActivity}
              </button>
            )}

            {routed.map((slug) => {
              /*
                Which teaching approach answered, beside the chip that says what
                the turn DID.

                The old palette gave these two facts two different hues -- cyan
                for tool activity, violet for a route -- which was one of eleven
                hues in a product that has three states. They are both neutral
                now, and the distinction that used to be carried by colour is
                carried by the words, which said it anyway.

                A slug this client has never heard of still renders -- as the
                slug, with a neutral glyph -- because the roster is a database
                column and a sixth persona must not leave a hole in the chip row.
                That is the same rule `CategoryBadge` follows for an unrecognised
                category.

                Not a button. `PersonaIcon` was the obvious reuse and does not
                fit: it draws a bordered tile, which is a card affordance rather
                than an inline pill, so the glyph goes in directly and carries
                `aria-hidden` for the reason that component gives -- it always
                sits beside the role it stands for.
              */
              const specialist = resolveSpecialist(slug);
              return (
                <span
                  key={slug}
                  data-testid="route-pill"
                  title={routeExplanation(message.route_trigger, specialist?.role ?? slug)}
                  className={PILL_NEUTRAL}
                >
                  <span aria-hidden="true">{specialist?.icon ?? "\u{1F9E0}"}</span>
                  {specialist?.role ?? slug}
                </span>
              );
            })}

            {message.self_check_verdict === "ungrounded" && (
              /*
                The honest surfacing of the last row of the self-check table: the
                critic said the draft was not grounded, the step budget was spent,
                and **the draft was kept exactly as the model wrote it**. Editing
                an answer to add a caveat the model did not write is the one
                outcome worse than shipping the draft, because it makes the
                system's voice unreliable in a way no trace event records. So the
                caveat is a chip beside the answer rather than a sentence inside
                it.

                Warn, like `stopped`, and for the same reason: this is a caveat
                about completeness and not an error. The error tone would teach
                the reader that something broke, when what happened is that a
                check ran and reported honestly -- which is the product working.
              */
              <span
                data-testid="ungrounded-chip"
                title="This answer was checked against the passages it cited, and some of its claims were not carried by them. The text is exactly what the model wrote -- nothing was edited."
                aria-label="Unverified claims: this answer was checked against its sources and some claims were not carried by them."
                className={`${PILL} ${WARN_TONE}`}
              >
                Unverified claims
              </span>
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

                Warn rather than error: this is not a fault. The user pressed
                Stop and got what had arrived, which is the behaviour they asked
                for.
              */
              <span
                data-testid="stopped-chip"
                className={`${PILL} ${WARN_TONE}`}
                title="You stopped reading this turn. The agent finished it on the server -- reload to see the whole answer."
              >
                stopped early &middot; reload for the full answer
              </span>
            )}

            <span className="ml-auto font-mono text-xs text-faint">
              {formatTimestamp(message.created_at)}
              {message.latency_ms !== null ? ` · ${formatDuration(message.latency_ms)}` : ""}
              {message.model_used ? ` · ${message.model_used}` : ""}
            </span>
          </div>

          {/* On its own line, not in the row above: the panel it opens is
              full-width, and a flex item that expands to a JSON payload drags the
              whole row's layout with it. */}
          <TracePanel queryId={message.query_id} openSignal={traceSignal} />
        </div>

        {/* ---------------------------------------------- the evidence margin */}
        {message.citations.length > 0 && (
          /*
            One node, two placements. Above 1280px `.gw-apparatus` puts this in
            the grid's second track and `xl:block` keeps it open; below it, the
            element is an ordinary block under the answer, shown only when the
            toggle above says so.

            Rendering it once rather than twice is not tidiness -- a second copy
            would duplicate `data-testid="citation-card"` and the marker -> element
            map that a chip click looks up would register whichever copy mounted
            last, so on a narrow screen the chip would focus a node inside a
            `display:none` subtree and silently do nothing.

            `block`/`hidden` plus `xl:block`, never `contents`/`hidden`: those two
            are utilities of equal specificity and which one wins is decided by
            their order in the generated stylesheet rather than by the string --
            a coin-flip this codebase has already paid for once.
          */
          <aside
            id={sourcesId}
            aria-label="Retrieved passages"
            className={`${sourcesOpen ? "block" : "hidden"} mt-4 xl:mt-0 xl:block`}
          >
            <p className={`${EYEBROW} xl:border-b xl:border-line xl:pb-2`}>
              {message.refused ? "Passages checked" : "Sources"}
            </p>
            <p className="mt-1.5 mb-2.5 text-xs leading-relaxed text-muted">
              {message.refused
                ? "The closest passages the agent checked. They did not support an answer."
                : "Every claim above is numbered to one of these."}
            </p>
            <ol className="space-y-2">
              {message.citations.map((citation) => (
                <CitationCard
                  key={`${citation.chunk_id}-${citation.marker}`}
                  citation={citation}
                  active={activeMarker === citation.marker}
                  peakStrength={peakStrength}
                  cardRef={(element) => {
                    if (element) cards.current.set(citation.marker, element);
                    else cards.current.delete(citation.marker);
                  }}
                />
              ))}
            </ol>
          </aside>
        )}
      </div>
    </li>
  );
}

/**
 * Which personas answered this turn, or `[]`.
 *
 * `specialists` wins when it holds anything, because a two-`@mention` turn is
 * recorded as one ROUTE event with a list rather than as two half-events --
 * so reading `specialist` first would show one pill on a turn that produced
 * two sections. Falls back to the singular field for the ordinary routed turn
 * and for anything replayed before the list existed.
 */
function routedSlugs(message: ChatMessage): string[] {
  if (message.specialists && message.specialists.length > 0) return message.specialists;
  return message.specialist ? [message.specialist] : [];
}

/**
 * The pill's tooltip: who decided, in one sentence.
 *
 * Three triggers, three different claims, and the one worth spelling out is
 * `"mention"` -- the user chose, and the router was skipped entirely. A
 * tooltip saying "chosen for you" over a choice somebody made themselves is
 * the same misattribution the trace panel avoids one level down. An
 * unrecognised trigger degrades to naming the role without claiming a cause.
 */
function routeExplanation(trigger: string | null | undefined, role: string): string {
  if (trigger === "mention") return `You asked for the ${role} by name.`;
  if (trigger === "router") return `Answered as the ${role}, chosen for this question.`;
  if (trigger === "fallback") {
    return `Routing did not settle on a persona, so this was answered as the ${role}.`;
  }
  return `Answered as the ${role}.`;
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
      //
      // Drawn identically to the marker on the card in the margin, deliberately:
      // matching a bracket in the prose to an entry in the apparatus should be
      // something the reader does by sight rather than by reading.
      className="gw-chip mx-0.5 inline-flex h-5 min-w-5 items-center justify-center rounded-sm border border-accent-line bg-accent-soft px-1 align-super font-mono text-xs font-semibold text-accent transition hover:border-accent hover:bg-accent hover:text-inverse"
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
