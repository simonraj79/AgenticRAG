/**
 * Tag-by-tag styling for every piece of rendered markdown in the app.
 *
 * **The map is short now, and that is the point.** It used to set a margin, a
 * size and a colour on every tag, which meant paragraph rhythm was decided
 * eleven times in one file. `.gw-prose` in `index.css` owns the family, the
 * measure, the leading, the inter-block rhythm (`> * + *`), `strong` and
 * `blockquote` -- those are descendant rules, and descendant rules are the only
 * thing that can reach the bare elements `react-markdown` emits. What is left
 * here is what a descendant selector genuinely cannot express: the heading
 * down-mapping, the two horizontal scroll containers, and the tags that need
 * the `inline` hook.
 *
 * So the rule when editing this file: **if `.gw-prose` already says it, do not
 * say it again.** A margin written in both places is a margin that will
 * disagree with itself the first time either is tuned, and the copy that
 * drifted is never the one you are reading.
 *
 * **Serif is provenance, not decoration.** Everything rendered through this map
 * came out of the user's corpus -- an answer assembled from retrieved passages,
 * a study sheet written over them. The harness speaks in sans; the corpus
 * speaks in serif. `.gw-prose` carries that, which is why the `Markdown`
 * wrapper below applies it rather than leaving each caller to remember.
 *
 * **Tailwind's preflight still removes the list defaults**, which is why
 * `list-disc` and `list-decimal` are here at all -- without them a bulleted
 * list has no bullets. The alternative is `@tailwindcss/typography`, a build
 * dependency this project deliberately does not carry: `package-lock.json` has
 * to stay consistent for Render's `npm ci`, and one plugin for one prose block
 * is not a trade this project makes.
 *
 * **Extracted here so two callers cannot drift.** The map started inside
 * `Message.tsx`, where it styled an agent's answer. `HandoutsPanel` renders a
 * study sheet's `preview_text` through the same pipeline, and a copied
 * component map is how the answer and the handout end up with different heading
 * sizes six months apart, with nothing to point at as the moment they diverged.
 * One definition, two importers.
 *
 * Two exports, because the callers need different things from the same map:
 *
 * - `createMarkdownComponents(inline)` is the factory `Message.tsx` uses. Its
 *   `inline` hook is applied to every tag that can hold text directly, and it
 *   is what turns `[1]` into a citation chip. `code` and `pre` deliberately
 *   skip it: a chunk of code containing `[0]` is an array index, not a
 *   citation.
 * - `markdownComponents` is the same map with that hook doing nothing, for the
 *   surfaces that render markdown with no citations to link -- handout
 *   previews. It is built by calling the factory, not by repeating it, so the
 *   two genuinely cannot diverge.
 */

import ReactMarkdown from "react-markdown";
import type { Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import type { ReactNode } from "react";
import { LINK, PROSE, WELL } from "./styles.ts";

/** The default `inline` hook: markdown with nothing to linkify passes its
 *  children through untouched. */
const passthrough = (children: ReactNode): ReactNode => children;

export function createMarkdownComponents(
  inline: (children: ReactNode) => ReactNode = passthrough,
): Components {
  return {
    // No block margin anywhere below. `.gw-prose > * + *` sets the rhythm once,
    // for every element `react-markdown` can emit -- including the ones this
    // map does not override, which a per-tag margin could never cover.
    p: ({ children }) => <p>{inline(children)}</p>,

    // `space-y-1` rather than a margin on `li`: a list item is not a DIRECT
    // child of `.gw-prose`, so the `> * + *` rule does not reach it. Leading is
    // inherited from the prose block and is deliberately not restated.
    ul: ({ children }) => <ul className="list-disc space-y-1 pl-5">{children}</ul>,
    ol: ({ children }) => <ol className="list-decimal space-y-1 pl-5">{children}</ol>,
    li: ({ children }) => <li>{inline(children)}</li>,

    /*
      The heading down-mapping, kept from the original map and re-expressed in
      the new scale.

      A model that opens its answer with `#` must not out-typeset the page's own
      `h1`. So `h1` and `h2` both land on `text-base`, `h3` and `h4` both on
      `text-sm` -- four markdown levels onto two visual ones, because an answer
      is a section of a page rather than a document with a title of its own.

      Serif and colour arrive by inheritance from `.gw-prose`; size and weight
      are the only things this map has any business setting.
    */
    h1: ({ children }) => <h1 className="text-base font-semibold">{inline(children)}</h1>,
    h2: ({ children }) => <h2 className="text-base font-semibold">{inline(children)}</h2>,
    h3: ({ children }) => <h3 className="text-sm font-semibold">{inline(children)}</h3>,
    h4: ({ children }) => <h4 className="text-sm font-semibold">{inline(children)}</h4>,

    // `.gw-prose strong` sets the weight; `.gw-prose blockquote` sets the rule,
    // the indent and the colour. Both tags appear here only for the `inline`
    // hook and for the parser -- restating their look would fork it.
    strong: ({ children }) => <strong>{inline(children)}</strong>,
    em: ({ children }) => <em className="italic">{inline(children)}</em>,
    blockquote: ({ children }) => <blockquote>{children}</blockquote>,

    a: ({ children, href }) => (
      <a href={href} target="_blank" rel="noreferrer noopener" className={LINK}>
        {children}
      </a>
    ),

    /*
      Code is machinery quoted inside prose, so it is mono on a recessed well
      rather than coloured. `0.85em` rather than a step on the type scale
      because it has to track whatever it is set inside: a monospaced face at
      the serif's own size reads a size larger than it is.
    */
    code: ({ children }) => (
      <code className={`${WELL} px-1 py-0.5 font-mono text-[0.85em] text-ink`}>
        {children}
      </code>
    ),

    /*
      `overflow-x-auto` is a DIFFERENT axis from the single-scroller rule that
      governs the chat column, and it is what stops an unbroken line of code
      taking the whole document's width with it at 320px.

      The `[&>code]` block undoes the inline treatment on the nested element
      `react-markdown` always emits inside a fence -- the well, its padding, and
      the 0.85em step, which would otherwise compound with this block's own
      `text-xs` and land the program a size below the scale.
    */
    pre: ({ children }) => (
      <pre
        className={`${WELL} overflow-x-auto p-3 font-mono text-xs leading-relaxed text-ink [&>code]:border-0 [&>code]:bg-transparent [&>code]:p-0 [&>code]:text-[1em]`}
      >
        {children}
      </pre>
    ),

    hr: () => <hr className="border-line" />,

    // Same axis argument as `pre`. The wrapper is the direct child of
    // `.gw-prose`, so the wrapper is what picks up the block rhythm.
    table: ({ children }) => (
      <div className="overflow-x-auto">
        <table className="w-full border-collapse text-sm">{children}</table>
      </div>
    ),
    th: ({ children }) => (
      <th className="border-b border-line px-2 py-1.5 text-left text-xs font-semibold text-faint">
        {inline(children)}
      </th>
    ),
    td: ({ children }) => (
      <td className="border-b border-line px-2 py-1.5 align-top">{inline(children)}</td>
    ),
  };
}

/** The map with no inline hook, for markdown that carries no citations. */
export const markdownComponents: Components = createMarkdownComponents();

/**
 * Markdown, rendered the app's way, in one tag.
 *
 * The wrapper carries `PROSE`, so the reading surface arrives WITH the renderer
 * instead of being something every caller has to remember -- the same argument
 * that put the component map in this file rather than in `Message.tsx`.
 * `Message` builds its own `ReactMarkdown` because it needs the citation hook,
 * and supplies the prose class on its own container for the same reason.
 *
 * `remark-gfm` is not optional here: tables and strikethrough are GFM
 * extensions, and a study sheet is mostly tables. Bundling the plugin with the
 * component map means a caller cannot get one without the other -- rendering a
 * pipe table through bare CommonMark produces a paragraph full of `|`, which
 * reads as a broken model rather than a missing plugin.
 */
export function Markdown({ source }: { source: string }) {
  return (
    <div className={PROSE}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
        {source}
      </ReactMarkdown>
    </div>
  );
}
