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
          className="rounded-md border border-slate-700 bg-slate-900 px-3 py-1.5 text-sm text-slate-300 transition hover:border-slate-600"
        >
          Back to agents
        </button>
      </div>
    );
  }

  // An agent with no documents refuses every question, so sending a first-time
  // visitor straight to Chat would show them a persona that can only say "I
  // don't know". The corpus is the prerequisite, and the landing tab says so.
  const activeTab: TabId = tab ?? "documents";

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
  ].join(" · ");

  return (
    <div className="mx-auto max-w-6xl px-4 py-8 sm:px-6 sm:py-10">
      <button
        type="button"
        data-testid="agent-back"
        onClick={onBack}
        className="mb-5 min-h-11 rounded-md px-1 text-sm text-slate-400 transition hover:text-slate-200"
      >
        &larr; All agents
      </button>

      <header className="mb-6">
        <div className="flex items-start gap-4">
          <PersonaIcon icon={agent.icon} fallback={agent.name} size="lg" />

          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-3">
              <h1 className="text-2xl font-semibold tracking-tight text-slate-100">
                {agent.name}
              </h1>
              <StatusPill status={agent.status} />
              <CategoryBadge category={agent.category} />
            </div>

            <p className="mt-1 text-sm text-slate-400">
              {agent.persona_role ?? "Custom agent"}
              <span className="text-slate-600"> · </span>
              <span className="text-slate-300">
                {agent.document_count} {agent.document_count === 1 ? "document" : "documents"}
              </span>
            </p>

            {agent.description && (
              <p className="mt-2 max-w-3xl text-sm text-slate-400">{agent.description}</p>
            )}

            <p className="mt-3 text-xs text-slate-400">{parameterSummary}</p>
          </div>
        </div>

        {agent.pedagogy && (
          <p className="mt-4 max-w-3xl border-l-2 border-slate-800 pl-4 text-xs leading-relaxed text-slate-400">
            <span className="font-medium text-slate-400">Rests on: </span>
            {agent.pedagogy}
          </p>
        )}

        <div className="mt-5 space-y-3">
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
        className="mb-6 flex gap-1 border-b border-slate-800"
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

      <div
        role="tabpanel"
        id="agent-panel-chat"
        aria-labelledby="agent-tab-chat"
        hidden={activeTab !== "chat"}
        className="min-w-0"
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
