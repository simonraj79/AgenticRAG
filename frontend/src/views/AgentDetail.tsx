/**
 * Agent detail: what this agent is, its corpus, and the conversation with it.
 *
 * The agent record lives here rather than in each tab because the Documents tab
 * changes it -- uploading or deleting moves `document_count` and `status` -- and
 * a count that disagrees with the list underneath it is the kind of small
 * wrongness that makes a demo look unfinished. One owner, one refetch,
 * `onCorpusChanged`.
 *
 * **The header leads with the persona, not the tuning.** It used to open with a
 * six-column grid of chunk size, overlap, k, rerank and threshold, which
 * answered "how is this configured" before "what is this". Those numbers are
 * still on the page -- summarised in one line and expanded one click away --
 * because this is a workshop artifact and "change chunk_size, watch the answer
 * change" is the exercise. Demoted, not deleted.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import { api } from "../lib/api.ts";
import type { Agent } from "../lib/types.ts";
import {
  CategoryBadge,
  ErrorBanner,
  Fact,
  PersonaIcon,
  Reveal,
  Spinner,
  StatusPill,
  errorMessage,
} from "../components/ui.tsx";
import AgentDocuments from "./AgentDocuments.tsx";
import AgentChat from "./AgentChat.tsx";
import AgentEvaluate from "./AgentEvaluate.tsx";

type TabId = "documents" | "chat" | "evaluate";

// Order is the workflow, not the alphabet: you talk to an agent, you feed it,
// and only then is there anything to measure. Evaluate sits last because a
// scorecard over an empty corpus is noise -- PRD section 3.6 scores answers, and
// there are none until the first two tabs have been used.
const TABS: { id: TabId; label: string; testId: string }[] = [
  { id: "chat", label: "Chat", testId: "tab-chat" },
  { id: "documents", label: "Documents", testId: "tab-documents" },
  { id: "evaluate", label: "Evaluate", testId: "tab-evaluate" },
];

export default function AgentDetail({
  agentId,
  onBack,
}: {
  agentId: string;
  onBack: () => void;
}) {
  const [agent, setAgent] = useState<Agent | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<TabId | null>(null);
  const tabRefs = useRef<Record<TabId, HTMLButtonElement | null>>({
    chat: null,
    documents: null,
    evaluate: null,
  });

  // An agent with no documents refuses every question, so sending a first-time
  // visitor straight to Chat would show them a persona that can only say "I
  // don't know". The corpus is the prerequisite, and the landing tab says so.
  //
  // Derived here rather than after the early returns below because the height
  // effect needs it, and hooks cannot live behind a `return`.
  const activeTab: TabId = tab ?? "documents";

  /** Everything above the tab panels: the back link, the agent header, the
   *  error slot and the tab strip. Measured as a block because its height is
   *  what the chat panel's height is the complement of. */
  const chromeRef = useRef<HTMLDivElement>(null);
  const chatPanelRef = useRef<HTMLDivElement>(null);
  const [chatHeight, setChatHeight] = useState<string | null>(null);

  /**
   * The chat tab's height, measured rather than guessed.
   *
   * `AgentChat` used to size its thread pane `h-[70dvh]`, a fraction it invented
   * with no knowledge of what sat above it -- and on a 390x844 phone what sat
   * above it was a sticky nav, a wrapped agent header with two open disclosures
   * and a tab strip. 70% of the viewport did not fit in the ~40% that was left,
   * so the document scrolled AND the pane scrolled, and the composer began below
   * the fold. `flex-1 min-h-0` is the fix, but it only means anything if
   * something in the chain has a DEFINITE height, and nothing did: `App.tsx`
   * leaves `<main>` in document flow deliberately, because every other view
   * relies on that.
   *
   * So this establishes it, and it measures instead of subtracting a constant
   * for two reasons that are both real at 320px: the nav wraps to two rows, and
   * this header's own height changes when the description appears, when the name
   * wraps, and when a `<Reveal>` is opened. Any hardcoded offset is wrong for at
   * least one of those, and being wrong by 30px is exactly the "composer just
   * below the fold" bug again.
   *
   * `rect.top + scrollY` is the panel's distance from the top of the DOCUMENT,
   * which is where the viewport's top edge sits at `scrollTop = 0` -- so
   * `100dvh` minus it is exactly the room left, and the page's total height
   * comes to exactly `100dvh`. The `dvh` unit is left in the calc rather than
   * resolved here on purpose: a mobile browser collapsing its URL bar changes it
   * continuously, and CSS tracks that for free.
   *
   * A null height is a safe fallback, not a broken one: the panel simply grows
   * to its content and the page scrolls, which is what it did before this
   * existed.
   */
  useEffect(() => {
    if (activeTab !== "chat") {
      setChatHeight(null);
      return;
    }

    const panel = chatPanelRef.current;
    if (!panel) return;

    const measure = () => {
      const top = panel.getBoundingClientRect().top + window.scrollY;
      setChatHeight(`calc(100dvh - ${Math.round(top)}px)`);
    };

    measure();
    window.addEventListener("resize", measure);

    // Observed on the CHROME, never on the panel. Observing the panel would
    // feed this effect's own output back into it -- the classic
    // "ResizeObserver loop completed with undelivered notifications" -- whereas
    // the chrome sits above the panel and is unaffected by how tall the panel
    // ends up being. Guarded because ResizeObserver is the one API here that a
    // very old browser might not have, and a missing observer costs a stale
    // height on a `<Reveal>` toggle, not a crash.
    const chrome = chromeRef.current;
    const observer =
      chrome && typeof ResizeObserver !== "undefined" ? new ResizeObserver(measure) : null;
    observer?.observe(chrome as Element);

    return () => {
      window.removeEventListener("resize", measure);
      observer?.disconnect();
    };
  }, [activeTab, agent]);

  const loadAgent = useCallback(async () => {
    try {
      const record = await api<Agent>(`/api/agents/${agentId}`);
      setAgent(record);
      // Settled ONCE, on the first load, and never recomputed. Derived fresh on
      // every render it would change under the user: uploading the first
      // document moves `document_count` off zero, and the tab they are watching
      // the upload on would swap itself for Chat mid-ingest.
      setTab((current) => current ?? (record.document_count > 0 ? "chat" : "documents"));
    } catch (cause) {
      // 403 means authenticated-but-not-owner and 404 means gone. Both are
      // shown as-is: the tenancy boundary refusing is worth seeing, not
      // smoothing over into "not found".
      setError(errorMessage(cause));
    }
  }, [agentId]);

  /**
   * Stable across renders, and that stability is load-bearing rather than an
   * optimisation: the Documents tab polls on a backoff timer whose effect
   * depends on this callback, so a fresh arrow function each render would tear
   * the timer down and restart it at the shortest interval every time the agent
   * refetched -- a backoff that never backs off.
   */
  const handleCorpusChanged = useCallback(() => {
    void loadAgent();
  }, [loadAgent]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    loadAgent().finally(() => {
      if (!cancelled) setLoading(false);
    });
    return () => {
      cancelled = true;
    };
  }, [loadAgent]);

  if (loading) {
    return (
      <div className="mx-auto max-w-6xl px-6 py-10">
        <Spinner label="Loading agent" />
      </div>
    );
  }

  if (!agent) {
    return (
      <div className="mx-auto max-w-6xl space-y-4 px-6 py-10">
        <ErrorBanner error={error ?? "Agent not found."} />
        <button
          type="button"
          onClick={onBack}
          // `min-h-11`: this was ~32px, and it is the ONLY control on the page
          // when an agent fails to load -- the one screen where a missed tap
          // leaves the user with no way forward at all.
          className="min-h-11 rounded-md border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-300 transition hover:border-slate-600"
        >
          Back to agents
        </button>
      </div>
    );
  }

  /**
   * The chat tab hides its reference material below `sm`.
   *
   * The description, the pedagogy note, the parameter summary and the two
   * `<Reveal>` panels are things you read to understand an agent, not things you
   * read while asking it a question -- and on a 390px screen they are 300-400px
   * of header standing between the tab strip and the first message. They are one
   * tap away on Overview, and every pixel they give back goes to the thread.
   *
   * Hidden with `hidden sm:...` rather than removed from the tree: the same
   * markup has to be there at `sm` and up, and conditionally rendering it would
   * mean the header re-mounts on a resize across the breakpoint.
   */
  const compactHeader = activeTab === "chat";

  function moveTab(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    let nextIndex: number | null = null;
    if (event.key === "ArrowRight") nextIndex = (index + 1) % TABS.length;
    if (event.key === "ArrowLeft") nextIndex = (index - 1 + TABS.length) % TABS.length;
    if (event.key === "Home") nextIndex = 0;
    if (event.key === "End") nextIndex = TABS.length - 1;
    if (nextIndex === null) return;

    event.preventDefault();
    const nextTab = TABS[nextIndex].id;
    setTab(nextTab);
    window.requestAnimationFrame(() => tabRefs.current[nextTab]?.focus());
  }

  const parameterSummary = [
    `${agent.chunk_size}-token chunks`,
    `k=${agent.retrieve_k}`,
    agent.rerank_enabled ? `rerank top ${agent.rerank_top_n}` : "rerank off",
    `rewrite below ${agent.score_threshold.toFixed(2)}`,
    // Named in the one-line summary rather than left to the grid below, because
    // it is the only parameter here that changes what the agent can DO. The
    // others tune how well it retrieves; this one decides whether it may search
    // a second time and run code at all, which is worth seeing without opening
    // anything.
    agent.tools_enabled ? `tools on (${agent.max_tool_steps} steps)` : "tools off",
  ].join(" · ");

  /** Shorthand for "reference material, gone below `sm` on the chat tab". */
  const asideOnChat = compactHeader ? "hidden sm:block" : "";

  return (
    /*
      `xl:max-w-[90rem]` on the chat tab, and UNCONDITIONALLY -- not only when
      the Handouts panel is open.

      A layout that reflows when you open a panel is worse than one that is
      simply wider: the thread reflows, the scroll position lands somewhere
      else, and the panel appears to have pushed the conversation sideways. It
      is also an independent win. Every view in this app is capped at
      `max-w-6xl` (1152px), so on a 1920px monitor two-thirds of the screen is
      gutter; 1440px is what makes a 15rem rail, a readable thread and a 22rem
      panel fit at once. The other tabs stay at 1152px because a dashboard of
      cards genuinely does not benefit from more.

      `pb-0` on the chat tab is load-bearing rather than cosmetic: the panel
      below is sized to exactly the room left under it, so a bottom padding
      OUTSIDE it would add its own height to the document and put the composer
      that many pixels below the fold. The breathing room moves inside the panel
      instead.
    */
    <div
      className={`mx-auto max-w-6xl px-4 sm:px-6 ${
        compactHeader ? "pt-6 pb-0 sm:pt-10" : "py-8 sm:py-10"
      } ${compactHeader ? "xl:max-w-[90rem]" : ""}`}
    >
      {/* Everything the chat panel's height is measured against. See the
          `chatHeight` effect. */}
      <div ref={chromeRef}>
        <button
          type="button"
          data-testid="agent-back"
          onClick={onBack}
          className="mb-5 min-h-11 rounded-md px-1 text-sm text-slate-400 transition hover:text-slate-200"
        >
          &larr; All agents
        </button>

        <header className={compactHeader ? "mb-4 sm:mb-6" : "mb-6"}>
          <div className="flex items-start gap-4">
            <PersonaIcon icon={agent.icon} fallback={agent.name} size="lg" />

            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-3">
                <h1 className="text-2xl font-semibold tracking-tight text-slate-100">
                  {agent.name}
                </h1>
                {/* The status pill survives the collapse. "empty" is the reason a
                    brand-new agent refuses every question, and that is precisely
                    the thing worth knowing while looking at the composer. */}
                <StatusPill status={agent.status} />
                <span className={compactHeader ? "hidden sm:inline-block" : ""}>
                  <CategoryBadge category={agent.category} />
                </span>
              </div>

              <p className={`mt-1 text-sm text-slate-400 ${asideOnChat}`}>
                {agent.persona_role ?? "Custom agent"}
                <span className="text-slate-600"> · </span>
                <span className="text-slate-300">
                  {agent.document_count} {agent.document_count === 1 ? "document" : "documents"}
                </span>
              </p>

              {agent.description && (
                <p className={`mt-2 max-w-3xl text-sm text-slate-400 ${asideOnChat}`}>
                  {agent.description}
                </p>
              )}

              <p className={`mt-3 text-xs text-slate-400 ${asideOnChat}`}>{parameterSummary}</p>
            </div>
          </div>

          {agent.pedagogy && (
            <p
              className={`mt-4 max-w-3xl border-l-2 border-slate-800 pl-4 text-xs leading-relaxed text-slate-400 ${asideOnChat}`}
            >
              <span className="font-medium text-slate-400">Rests on: </span>
              {agent.pedagogy}
            </p>
          )}

          <div className={`mt-5 space-y-3 ${asideOnChat}`}>
            <Reveal summary="Retrieval parameters" testId="agent-parameters">
              <dl className="grid grid-cols-2 gap-x-6 gap-y-2 text-xs sm:grid-cols-4">
                <Fact label="Documents" value={agent.document_count} />
                <Fact label="Chunk size" value={agent.chunk_size} />
                <Fact label="Overlap" value={agent.chunk_overlap} />
                <Fact label="Splitter" value={agent.splitter} />
                <Fact label="Retrieve k" value={agent.retrieve_k} />
                <Fact
                  label="Rerank"
                  value={agent.rerank_enabled ? `top ${agent.rerank_top_n}` : "off"}
                />
                <Fact label="Score threshold" value={agent.score_threshold} />
                <Fact label="Max rewrites" value={agent.max_rewrites} />
                {/*
                  Both tool facts are shown even when tools are off, and the
                  step count is dashed rather than hidden in that case. A
                  parameter that disappears when it is inactive reads as a
                  parameter that does not exist, and this grid is where someone
                  goes to find out why an agent behaves differently from another
                  one.
                */}
                <Fact label="Tools" value={agent.tools_enabled ? "on" : "off"} />
                <Fact
                  label="Max tool steps"
                  value={agent.tools_enabled ? agent.max_tool_steps : "--"}
                />
              </dl>
              <p className="mt-3 text-xs text-slate-400">
                Copied from the template at creation and owned by this agent since. The
                embedding model is <span className="text-slate-300">{agent.embedding_model ?? "unset"}</span>,
                and it cannot change without re-ingesting: a namespace built by one model and
                queried with another returns confident nonsense rather than an error.
              </p>
            </Reveal>

            {agent.system_prompt && (
              <Reveal summary="System prompt" testId="agent-prompt">
                <pre className="max-h-72 overflow-y-auto text-xs leading-relaxed whitespace-pre-wrap text-slate-400">
                  {agent.system_prompt}
                </pre>
              </Reveal>
            )}
          </div>
        </header>

        <div className="mb-6">
          <ErrorBanner error={error} />
        </div>

        <div
          role="tablist"
          aria-label="Agent views"
          className={`flex gap-1 border-b border-slate-800 ${
            compactHeader ? "mb-4 sm:mb-6" : "mb-6"
          }`}
        >
          {TABS.map((entry, index) => {
            const active = activeTab === entry.id;
            return (
              <button
                key={entry.id}
                type="button"
                role="tab"
                id={`agent-tab-${entry.id}`}
                ref={(element) => {
                  tabRefs.current[entry.id] = element;
                }}
                aria-selected={active}
                aria-controls={`agent-panel-${entry.id}`}
                tabIndex={active ? 0 : -1}
                data-testid={entry.testId}
                onClick={() => setTab(entry.id)}
                onKeyDown={(event) => moveTab(event, index)}
                className={`-mb-px min-h-11 border-b-2 px-4 py-2 text-sm font-medium transition ${
                  active
                    ? "border-emerald-400 text-slate-100"
                    : "border-transparent text-slate-400 hover:text-slate-200"
                }`}
              >
                {entry.label}
              </button>
            );
          })}
        </div>
      </div>

      {/*
        Both tabs stay mounted-on-demand rather than hidden with CSS, and the
        chat is keyed on the agent so switching agents cannot leave the previous
        conversation on screen. The trace no longer needs a tab of its own: it
        hangs off the citations inside an answer, where the decision it explains
        is actually visible.
      */}
      <div
        role="tabpanel"
        id="agent-panel-documents"
        aria-labelledby="agent-tab-documents"
        hidden={activeTab !== "documents"}
        className="min-w-0"
      >
        {activeTab === "documents" && (
          <AgentDocuments agentId={agent.id} onCorpusChanged={handleCorpusChanged} />
        )}
      </div>

      {/*
        The height context Task 3's `flex-1 min-h-0` resolves against.

        `flex flex-col` plus a definite height is the whole mechanism: without
        the height, `flex-1` means "grow to content" and the thread runs off the
        bottom of the page; without `min-h-0` the flex item's default
        `min-height: auto` refuses to shrink below its content and the composer
        is pushed out of the box even though the box is the right size.

        The padding is INSIDE the measured height (`box-sizing: border-box` is
        Tailwind's preflight default), which is why the page container above
        drops its bottom padding on this tab -- see the note there.
      */}
      <div
        ref={chatPanelRef}
        role="tabpanel"
        id="agent-panel-chat"
        aria-labelledby="agent-tab-chat"
        hidden={activeTab !== "chat"}
        style={chatHeight ? { height: chatHeight } : undefined}
        className="flex min-h-0 min-w-0 flex-col pb-4 sm:pb-6"
      >
        {activeTab === "chat" && <AgentChat key={agent.id} agentId={agent.id} />}
      </div>

      {/*
        Keyed on the agent for the same reason AgentChat is: an eval run is
        polled on an interval, and switching agents while one is in flight must
        start a fresh component rather than let the old poll write a scorecard
        into the new agent's view.
      */}
      <div
        role="tabpanel"
        id="agent-panel-evaluate"
        aria-labelledby="agent-tab-evaluate"
        hidden={activeTab !== "evaluate"}
        className="min-w-0"
      >
        {activeTab === "evaluate" && <AgentEvaluate key={agent.id} agent={agent} />}
      </div>
    </div>
  );
}
