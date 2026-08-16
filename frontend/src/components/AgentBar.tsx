/**
 * The agent bar: one row that replaces five hundred pixels of header.
 *
 * **This exists because of a measurement, not a preference.** At 1440x900 the
 * old header stack -- back link, icon, name, badges, persona line, description,
 * parameter summary, pedagogy note and two `<Reveal>` panels -- put the chat
 * panel's top edge at **576px, 64% of the viewport**. Opening `Retrieval
 * parameters`, which is the exercise this workshop is built around, moved it to
 * **1092px: 121% of the viewport**, and `AgentDetail` sizes the panel as
 * `calc(100dvh - top)`, so the complement went negative and the chat collapsed
 * to 24px with zero visible thread.
 *
 * Nothing threw. No console error, no failed request, no horizontal overflow.
 * The page rendered perfectly and the product was gone -- which is
 * `new features/loop.md` T2 arriving in a module that has never seen a tool:
 * the error-shaped check passes while the outcome you wanted is absent.
 *
 * So the fix is structural rather than a smaller header. **This bar is a single
 * flex row and cannot grow**, which is what makes the height context above it
 * safe: there is no content in here whose expansion could push the workspace
 * off the bottom of the screen. Everything that used to expand -- the
 * parameters and the system prompt -- moved behind `[...]`, where opening it
 * costs the workspace nothing because a sheet is an overlay.
 *
 * **Why three dots and not a hamburger or an (i).** A hamburger means global
 * navigation, and this is not navigation. An (i) means read-only information,
 * and this sheet writes: it is the first caller of `PATCH /api/agents/{id}`,
 * which the server has always served and no frontend code has ever used. Three
 * dots is the standing convention for "more actions on this specific object",
 * which is exactly what it opens.
 *
 * **The tab strip is one DOM node that wraps**, not two rendered conditionally.
 * `w-full sm:w-auto` inside a `flex-wrap` row puts it on its own line below
 * `sm` and inline at `sm` and up. Rendering it twice with an `sm:hidden` pair
 * would put two elements carrying `data-testid="tab-evaluate"` in the document
 * at once, and a Playwright locator matches a `display:none` element -- the
 * same strict-mode hazard `AgentDocuments` documents for its card/table pair.
 */

import type { Agent } from "../lib/types.ts";
import { CategoryBadge, PersonaIcon, StatusPill } from "./ui.tsx";

export type ViewId = "workspace" | "sources" | "evaluate";

/**
 * Three views, and `sources` appearing here as well as in the workspace rail is
 * deliberate rather than a duplication left in by accident.
 *
 * The rail is the glance: what is this agent grounded in, while I am talking to
 * it. This view is the desk: the full ingest table with sizes, mime types,
 * upload times and the drag-and-drop zone, at the width a six-column table
 * actually needs. Merging them would mean either a rail too wide to leave room
 * for a thread, or losing the table -- and the table is where an ingest failure
 * explains itself.
 */
const VIEWS: { id: ViewId; label: string; testId: string }[] = [
  { id: "workspace", label: "Workspace", testId: "tab-workspace" },
  { id: "sources", label: "Sources", testId: "tab-sources" },
  // `tab-evaluate` is deliberately NOT reused here: `AgentEvaluate` already
  // puts that id on its own panel root, so a locator for it currently matches
  // two live elements whenever the tab is open. Naming this one `tab-eval`
  // leaves that pre-existing collision alone rather than widening it.
  { id: "evaluate", label: "Evaluate", testId: "tab-eval" },
];

export default function AgentBar({
  agent,
  view,
  onView,
  onBack,
  onOpenSettings,
  settingsRef,
}: {
  agent: Agent;
  view: ViewId;
  onView: (next: ViewId) => void;
  onBack: () => void;
  onOpenSettings: () => void;
  /** So the sheet can return focus here on close. Owned by the parent because
   *  the sheet and the button are siblings, not parent and child. */
  settingsRef: React.RefObject<HTMLButtonElement | null>;
}) {
  // The same one-line summary the old header carried, kept verbatim in spirit:
  // `tools` is named because it is the only parameter here that changes what
  // the agent can DO rather than how well it retrieves.
  const summary = [
    `${agent.chunk_size}-token chunks`,
    `k=${agent.retrieve_k}`,
    agent.rerank_enabled ? `rerank top ${agent.rerank_top_n}` : "rerank off",
    agent.tools_enabled ? `tools on (${agent.max_tool_steps} steps)` : "tools off",
  ].join(" · ");

  return (
    <div
      data-testid="agent-bar"
      className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1 border-b border-slate-800 px-3 py-1 sm:px-6"
    >
      <button
        type="button"
        data-testid="agent-back"
        onClick={onBack}
        aria-label="Back to all agents"
        // `min-w-11` as well as `min-h-11`: the wizard already records that
        // height alone is not a touch target, and this is icon-only.
        className="min-h-11 min-w-11 rounded-md text-slate-400 transition hover:text-slate-200"
      >
        <span aria-hidden="true" className="text-lg">
          &larr;
        </span>
      </button>

      <PersonaIcon icon={agent.icon} fallback={agent.name} size="sm" />

      {/* `min-w-0` is what lets `truncate` work at all -- a flex item's default
          `min-width: auto` refuses to shrink below its content, so without it a
          long agent name pushes the status pill and `[...]` off the row. */}
      <div className="flex min-w-0 flex-1 items-center gap-2">
        <h1 className="truncate text-sm font-semibold tracking-tight text-slate-100 sm:text-base">
          {agent.name}
        </h1>
        <StatusPill status={agent.status} />
        <span className="hidden xl:inline-block">
          <CategoryBadge category={agent.category} />
        </span>
      </div>

      {/* Reference material, and the first thing to go when the row is tight.
          Hidden below `lg` rather than truncated: half a parameter summary is
          worse than none, because `k=3 · rerank top...` reads as a value. */}
      <span className="hidden text-xs text-slate-400 lg:inline">{summary}</span>

      {/* `order-last w-full` below `sm` puts this on its own line; at `sm` it
          returns to the row. One node, two layouts -- see the file docstring. */}
      <nav
        aria-label="Agent views"
        className="order-last flex w-full gap-1 sm:order-none sm:w-auto"
      >
        {VIEWS.map((entry) => {
          const active = view === entry.id;
          return (
            <button
              key={entry.id}
              type="button"
              data-testid={entry.testId}
              aria-current={active ? "page" : undefined}
              onClick={() => onView(entry.id)}
              className={`min-h-11 flex-1 rounded-md px-3 text-sm font-medium transition sm:flex-none ${
                active
                  ? "bg-slate-800 text-slate-100"
                  : "text-slate-400 hover:bg-slate-900 hover:text-slate-200"
              }`}
            >
              {entry.label}
            </button>
          );
        })}
      </nav>

      <button
        type="button"
        ref={settingsRef}
        data-testid="agent-settings-open"
        onClick={onOpenSettings}
        aria-label="Agent settings"
        aria-haspopup="dialog"
        className="min-h-11 min-w-11 rounded-md border border-slate-700 bg-slate-900 text-slate-300 transition hover:border-slate-600 hover:text-slate-100"
      >
        {/* Drawn rather than typed. A literal "..." is three full stops that a
            screen reader announces as an ellipsis and that shift with the font;
            the button's accessible name comes from `aria-label` either way, so
            the glyph is decoration and is marked as such. */}
        <svg
          aria-hidden="true"
          viewBox="0 0 20 20"
          className="mx-auto h-4 w-4 fill-current"
        >
          <circle cx="4" cy="10" r="1.6" />
          <circle cx="10" cy="10" r="1.6" />
          <circle cx="16" cy="10" r="1.6" />
        </svg>
      </button>
    </div>
  );
}
