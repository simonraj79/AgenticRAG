/**
 * The source-first state for a newly created agent.
 *
 * A grounded agent with no corpus cannot answer a useful question. Rendering
 * the normal composer in that state invites work the product is guaranteed to
 * refuse, so the empty workspace points at the one action that can make the
 * agent usable and leaves no misleading text box behind.
 *
 * `CARD_EMPTY` rather than `CARD`: the dashed edge is what makes emptiness read
 * as a STATE rather than as a container that failed to load. This is a
 * first-run moment -- for most people the first thing Groundwork ever says to
 * them -- so it gets the page-section padding and the full measure, not the
 * dense-list treatment.
 *
 * **The heading and the button label are asserted by accessible name**
 * (`EmptyAgentWorkspace.test.tsx`). They are copy, not decoration; restyle
 * freely, reword neither.
 */

import { ACCENT_TONE, BTN_PRIMARY, CARD_EMPTY } from "../lib/styles.ts";

export default function EmptyAgentWorkspace({
  onAddSource,
}: {
  onAddSource: () => void;
}) {
  return (
    <section
      data-testid="empty-agent-workspace"
      aria-labelledby="empty-agent-heading"
      className={`${CARD_EMPTY} flex min-h-0 flex-1 items-center justify-center px-5 py-12 text-center sm:px-8`}
    >
      <div className="mx-auto max-w-prose">
        {/*
          The accent, spent on exactly what it means everywhere else in this
          app: a document, which is the only thing that can ground an answer.
          Tone strings carry the border, the tint and the ink as one triple, so
          the glyph cannot end up legible in one theme and not the other.
        */}
        <span
          aria-hidden="true"
          className={`mx-auto inline-flex h-12 w-12 items-center justify-center rounded-md border ${ACCENT_TONE}`}
        >
          <svg
            width="24"
            height="24"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.75"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
            <path d="M14 2v6h6M12 18v-6M9 15h6" />
          </svg>
        </span>

        <h2
          id="empty-agent-heading"
          className="mt-5 text-lg font-semibold tracking-tight text-ink"
        >
          Add a source before you ask
        </h2>
        <p className="mx-auto mt-2 max-w-prose text-sm leading-relaxed text-muted">
          Groundwork answers only from this agent&rsquo;s documents. With no sources,
          every question has to be refused instead of guessed.
        </p>

        <button
          type="button"
          data-testid="empty-agent-add-source"
          onClick={onAddSource}
          className={`${BTN_PRIMARY} mt-6`}
        >
          Add your first source
        </button>
        <p className="mt-3 text-xs text-muted">
          Upload a PDF, Markdown file or plain-text document.
        </p>
      </div>
    </section>
  );
}
