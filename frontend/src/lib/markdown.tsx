/**
 * Tag-by-tag styling for every piece of rendered markdown in the app.
 *
 * **Long because Tailwind's preflight removes every default.** Without
 * `list-disc` a bulleted list has no bullets; without `font-semibold` bold is
 * not bold; without an explicit size a heading is body text. The alternative is
 * `@tailwindcss/typography`, which is a build dependency this project
 * deliberately does not carry -- `package-lock.json` has to stay consistent for
 * Render's `npm ci`, and one plugin for one prose block is not a trade this
 * project makes.
 *
 * **Extracted here so two callers cannot drift.** The map started inside
 * `Message.tsx`, where it styled an agent's answer. `HandoutsPanel` renders a
 * study sheet's `preview_text` through the same pipeline, and a copied
 * seventy-line component map is how the answer and the handout end up with
 * different heading sizes six months apart, with nothing to point at as the
 * moment they diverged. One definition, two importers.
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

/** The default `inline` hook: markdown with nothing to linkify passes its
 *  children through untouched. */
const passthrough = (children: ReactNode): ReactNode => children;

export function createMarkdownComponents(
  inline: (children: ReactNode) => ReactNode = passthrough,
): Components {
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

/** The map with no inline hook, for markdown that carries no citations. */
export const markdownComponents: Components = createMarkdownComponents();

/**
 * Markdown, rendered the app's way, in one tag.
 *
 * `remark-gfm` is not optional here: tables and strikethrough are GFM
 * extensions, and a study sheet is mostly tables. Bundling the plugin with the
 * component map means a caller cannot get one without the other -- rendering a
 * pipe table through bare CommonMark produces a paragraph full of `|`, which
 * reads as a broken model rather than a missing plugin.
 */
export function Markdown({ source }: { source: string }) {
  return (
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
      {source}
    </ReactMarkdown>
  );
}
