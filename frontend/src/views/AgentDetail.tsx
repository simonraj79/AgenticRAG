/**
 * Agent detail: the corpus, the question box, and the trace, behind three tabs.
 *
 * The agent record lives here rather than in each tab because two of the three
 * change it -- uploading or deleting a document moves `document_count` and
 * `status` -- and a count that disagrees with the list underneath it is the
 * kind of small wrongness that makes a demo look unfinished. One owner, one
 * refetch, `onCorpusChanged`.
 *
 * `lastQueryId` lives here for the same reason: it is produced by the Ask tab
 * and consumed by the Trace tab, which are siblings.
 */

import { useCallback, useEffect, useState } from "react";
import { api } from "../lib/api.ts";
import type { Agent } from "../lib/types.ts";
import { ErrorBanner, Spinner, StatusPill, errorMessage } from "../components/ui.tsx";
import AgentDocuments from "./AgentDocuments.tsx";
import AgentAsk from "./AgentAsk.tsx";
import AgentTrace from "./AgentTrace.tsx";

type TabId = "documents" | "ask" | "trace";

const TABS: { id: TabId; label: string; testId: string }[] = [
  { id: "documents", label: "Documents", testId: "tab-documents" },
  { id: "ask", label: "Ask", testId: "tab-ask" },
  { id: "trace", label: "Trace", testId: "tab-trace" },
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
  const [tab, setTab] = useState<TabId>("documents");
  const [lastQueryId, setLastQueryId] = useState<string | null>(null);

  const loadAgent = useCallback(async () => {
    try {
      setAgent(await api<Agent>(`/api/agents/${agentId}`));
    } catch (cause) {
      // 403 means authenticated-but-not-owner and 404 means gone. Both are
      // shown as-is: the tenancy boundary refusing is worth seeing, not
      // smoothing over into "not found".
      setError(errorMessage(cause));
    }
  }, [agentId]);

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
      <div className="mx-auto max-w-5xl px-6 py-10">
        <Spinner label="Loading agent" />
      </div>
    );
  }

  if (!agent) {
    return (
      <div className="mx-auto max-w-5xl space-y-4 px-6 py-10">
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

  return (
    <div className="mx-auto max-w-5xl px-6 py-10">
      <button
        type="button"
        data-testid="agent-back"
        onClick={onBack}
        className="mb-5 text-sm text-slate-400 transition hover:text-slate-200"
      >
        &larr; All agents
      </button>

      <header className="mb-6">
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-2xl font-semibold tracking-tight text-slate-100">{agent.name}</h1>
          <StatusPill status={agent.status} />
        </div>
        {agent.description && <p className="mt-2 text-sm text-slate-400">{agent.description}</p>}

        {/*
          The retrieval parameters, on screen, always. This is a workshop
          artifact: the whole exercise is "change chunk_size, change k, watch the
          answer change", and that argument is unmakeable if the numbers in force
          are hidden behind a settings panel.
        */}
        <dl className="mt-4 grid grid-cols-2 gap-x-6 gap-y-2 text-xs sm:grid-cols-6">
          <Fact label="Documents" value={agent.document_count} />
          <Fact label="Chunk size" value={agent.chunk_size} />
          <Fact label="Overlap" value={agent.chunk_overlap} />
          <Fact label="Retrieve k" value={agent.retrieve_k} />
          <Fact
            label="Rerank"
            value={agent.rerank_enabled ? `top ${agent.rerank_top_n}` : "off"}
          />
          <Fact label="Threshold" value={agent.score_threshold} />
        </dl>
      </header>

      <div className="mb-6">
        <ErrorBanner error={error} />
      </div>

      <div
        role="tablist"
        aria-label="Agent views"
        className="mb-6 flex gap-1 border-b border-slate-800"
      >
        {TABS.map((entry) => {
          const active = tab === entry.id;
          return (
            <button
              key={entry.id}
              type="button"
              role="tab"
              aria-selected={active}
              data-testid={entry.testId}
              onClick={() => setTab(entry.id)}
              className={`-mb-px border-b-2 px-4 py-2 text-sm font-medium transition ${
                active
                  ? "border-emerald-400 text-slate-100"
                  : "border-transparent text-slate-500 hover:text-slate-300"
              }`}
            >
              {entry.label}
            </button>
          );
        })}
      </div>

      {tab === "documents" && (
        <AgentDocuments agentId={agent.id} onCorpusChanged={() => void loadAgent()} />
      )}
      {tab === "ask" && <AgentAsk agentId={agent.id} onAnswered={setLastQueryId} />}
      {/*
        Keyed on `lastQueryId` so asking a question and switching to Trace
        remounts the tab and refetches. Without the key the component keeps the
        events it loaded for the previous turn, and the trace silently belongs
        to the wrong query -- which is worse than showing nothing, because it
        looks right.
      */}
      {tab === "trace" && (
        <AgentTrace
          key={lastQueryId ?? "latest"}
          agentId={agent.id}
          queryId={lastQueryId}
        />
      )}
    </div>
  );
}

function Fact({ label, value }: { label: string; value: string | number }) {
  return (
    <div>
      <dt className="text-slate-500">{label}</dt>
      <dd className="mt-0.5 text-slate-200">{value}</dd>
    </div>
  );
}
