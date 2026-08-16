/**
 * The source-first state for a newly created agent.
 *
 * A grounded agent with no corpus cannot answer a useful question. Rendering
 * the normal composer in that state invites work the product is guaranteed to
 * refuse, so the empty workspace points at the one action that can make the
 * agent usable and leaves no misleading text box behind.
 */
export default function EmptyAgentWorkspace({
  onAddSource,
}: {
  onAddSource: () => void;
}) {
  return (
    <section
      data-testid="empty-agent-workspace"
      aria-labelledby="empty-agent-heading"
      className="flex min-h-0 flex-1 items-center justify-center rounded-xl border border-slate-800 bg-slate-900/40 px-5 py-10 text-center sm:px-8"
    >
      <div className="max-w-md">
        <span
          aria-hidden="true"
          className="mx-auto inline-flex h-12 w-12 items-center justify-center rounded-xl border border-emerald-800/60 bg-emerald-950/40 text-emerald-300"
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
          className="mt-5 text-xl font-semibold tracking-tight text-slate-100"
        >
          Add a source before you ask
        </h2>
        <p className="mt-2 text-sm leading-relaxed text-slate-400">
          Groundwork answers only from this agent&rsquo;s documents. With no sources,
          every question has to be refused instead of guessed.
        </p>

        <button
          type="button"
          data-testid="empty-agent-add-source"
          onClick={onAddSource}
          className="mt-6 min-h-11 rounded-md bg-emerald-500 px-4 py-2 text-sm font-semibold text-emerald-950 transition hover:bg-emerald-400"
        >
          Add your first source
        </button>
        <p className="mt-3 text-xs text-slate-400">
          Upload a PDF, Markdown file or plain-text document.
        </p>
      </div>
    </section>
  );
}
