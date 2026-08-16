/**
 * Dashboard: the user's agents first, and the form that makes another one
 * second.
 *
 * **Ordering is the design.** The create form used to open the page, which put
 * a returning user's own agents below the fold behind a task they had already
 * done. Creating an agent happens once per corpus; opening one happens every
 * session. So the agents lead, and creation lives behind a disclosure that
 * expands in place -- with one exception: a user who owns nothing has no
 * "open my agent" to do, and for them the form IS the page, so it starts open.
 *
 * **The picker sells the teaching method, not the tuning.** A template is now a
 * persona -- a Socratic tutor, a Polya coach, a quiz writer -- and the thing
 * being chosen is a stance towards the learner, backed by a named piece of
 * learning science. "800 tokens, k=20" is true, was what the old `<select>` led
 * with, and answers a question nobody choosing between eight teaching styles is
 * asking. The parameters are still in the flow, one step further in, because
 * this is a workshop artifact and they are the thing Build #1 changes.
 *
 * **Creation itself moved into `CreateAgentWizard`.** It was a single form in
 * this file, which is why the persona grid it led with buried the one required
 * field a thousand pixels down the page; the reasoning is in that component's
 * header. What stayed here is what this view is actually about -- the list, the
 * disclosure that opens creation, and what happens after an agent exists.
 *
 * One agent is one corpus, one config and one Pinecone namespace (PRD section
 * 3.2). That is why deleting an agent is a genuinely destructive act, why the
 * delete button confirms, and why it is not placed beside Open.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../lib/api.ts";
import type { Agent, Template } from "../lib/types.ts";
import CreateAgentWizard from "../components/CreateAgentWizard.tsx";
import Drawer from "../components/Drawer.tsx";
import {
  CategoryBadge,
  ConfirmDeleteButton,
  EmptyState,
  ErrorBanner,
  PersonaIcon,
  Spinner,
  StatusPill,
  errorMessage,
} from "../components/ui.tsx";

export default function Dashboard({ onOpenAgent }: { onOpenAgent: (agentId: string) => void }) {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [templates, setTemplates] = useState<Template[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const createButtonRef = useRef<HTMLButtonElement>(null);
  const wizardNameRef = useRef<HTMLInputElement>(null);

  const loadAgents = useCallback(async () => {
    setAgents(await api<Agent[]>("/api/agents"));
  }, []);

  useEffect(() => {
    let cancelled = false;

    (async () => {
      try {
        // Both at once: they are independent reads and the create form is
        // useless until each has landed, so serialising them only adds a round
        // trip to the time before the page is usable.
        const [agentList, templateList] = await Promise.all([
          api<Agent[]>("/api/agents"),
          api<Template[]>("/api/agent-templates"),
        ]);
        if (cancelled) return;
        setAgents(agentList);
        setTemplates(templateList);
        // Opened here rather than derived from `agents.length` on every render,
        // so it is a starting position and not a rule: deleting your last agent
        // does not yank the form open underneath you, and closing it stays
        // closed.
        if (agentList.length === 0) setCreateOpen(true);
      } catch (cause) {
        // A 401 has already been handled centrally by `api()` -- it cleared
        // auth state and the app has swapped to Login, so this component is
        // unmounting. Setting error state anyway is harmless and covers every
        // other status.
        if (!cancelled) setError(errorMessage(cause));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, []);

  async function deleteAgent(agent: Agent) {
    setDeletingId(agent.id);
    setError(null);
    try {
      await api(`/api/agents/${agent.id}`, { method: "DELETE" });
      await loadAgents();
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setDeletingId(null);
    }
  }

  function closeCreate() {
    setCreateOpen(false);
    // The drawer normally restores the trigger itself. This explicit return
    // also covers the first-run case, where the drawer opened automatically
    // because the account had no agents and therefore had no focused trigger.
    window.requestAnimationFrame(() => createButtonRef.current?.focus());
  }

  return (
    <>
      {/*
        A creation flow is a focused task, not another dashboard section. While
        its modal drawer is open the dashboard is removed from both the tab
        order and the accessibility tree, so mobile users do not encounter the
        card list "behind" step 1. The backdrop provides the same separation
        visually and catches an intentional click-away.
      */}
      <div
        aria-hidden={createOpen ? true : undefined}
        inert={createOpen}
        className="mx-auto max-w-6xl px-4 py-8 sm:px-6 sm:py-10"
      >
      <header className="mb-8 flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-slate-100">Your agents</h1>
          <p className="mt-1 max-w-2xl text-sm text-slate-400">
            Each agent owns one document corpus, one teaching persona and one isolated
            vector namespace. Nothing it retrieves can come from another agent.
          </p>
        </div>

        <button
          ref={createButtonRef}
          type="button"
          data-testid="create-agent-toggle"
          aria-expanded={createOpen}
          aria-controls="create-agent-panel"
          onClick={() => setCreateOpen(true)}
          className="min-h-11 rounded-md border border-emerald-500 bg-emerald-500 px-4 py-2 text-sm font-medium text-emerald-950 transition hover:bg-emerald-400"
        >
          New agent
        </button>
      </header>

      <div className="mb-8">
        <ErrorBanner error={error} />
      </div>

      <section>
        <h2 className="mb-4 text-sm font-medium tracking-wide text-slate-400 uppercase">
          Agents ({agents.length})
        </h2>

        {loading && <Spinner label="Loading agents" />}

        {!loading && agents.length === 0 && (
          <EmptyState
            title="No agents yet."
            detail="Start with New agent: pick a teaching persona, name it, then upload the documents it should answer from."
          />
        )}

        <ul className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {agents.map((agent) => (
            <li
              key={agent.id}
              data-testid="agent-card"
              data-agent-id={agent.id}
              className="flex flex-col rounded-xl border border-slate-800 bg-slate-900/50"
            >
              <div className="flex-1 p-5">
                <div className="flex items-start gap-3">
                  <PersonaIcon icon={agent.icon} fallback={agent.name} />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-start justify-between gap-2">
                      <h3 className="truncate font-medium text-slate-100">{agent.name}</h3>
                      <StatusPill status={agent.status} />
                    </div>
                    {/*
                      What it is and how much it knows, in one line. Chunk size
                      and k used to occupy this row; neither tells you whether
                      this is the agent you meant to open.
                    */}
                    <p className="mt-1 truncate text-xs text-slate-400">
                      {agent.persona_role ?? "Custom agent"}
                      <span className="text-slate-400"> · </span>
                      {agent.document_count} {agent.document_count === 1 ? "document" : "documents"}
                    </p>
                  </div>
                </div>

                {agent.description && (
                  <p className="mt-3 line-clamp-2 text-sm text-slate-400">{agent.description}</p>
                )}
              </div>

              <div className="flex items-center justify-between gap-3 border-t border-slate-800 px-5 py-3">
                <button
                  type="button"
                  data-testid="agent-open"
                  aria-label={`Open ${agent.name}`}
                  onClick={() => onOpenAgent(agent.id)}
                  className="min-h-11 rounded-md bg-slate-100 px-3 py-2 text-sm font-medium text-slate-900 transition hover:bg-white"
                >
                  Open
                </button>
                <CategoryBadge category={agent.category} />
              </div>

              {/*
                Its own strip, below the primary action and behind its own
                divider. Delete and Open were adjacent, which put an
                irreversible action one cursor-width from the one people click
                every session -- and arm-then-confirm does nothing about a click
                that was aimed at the wrong button in the first place.
              */}
              <div className="flex items-center justify-between gap-3 rounded-b-xl border-t border-slate-800/70 bg-slate-950/40 px-5 py-2">
                <span className="text-[0.65rem] text-slate-400">
                  Deletes the corpus and its vectors
                </span>
                <ConfirmDeleteButton
                  testId="agent-delete"
                  label="Delete"
                  confirmLabel="Delete agent + vectors?"
                  accessibleLabel={`Delete ${agent.name}`}
                  accessibleConfirmLabel={`Confirm deletion of ${agent.name} and its vectors`}
                  size="sm"
                  busy={deletingId === agent.id}
                  onConfirm={() => void deleteAgent(agent)}
                />
              </div>
            </li>
          ))}
        </ul>
      </section>
      </div>

      <Drawer
        open={createOpen}
        onClose={closeCreate}
        title="Create a new agent"
        testId="create-agent-panel"
        width="lg"
        initialFocusRef={wizardNameRef}
      >
        <CreateAgentWizard
          templates={templates}
          initialNameRef={wizardNameRef}
          // `agents` is unique on (owner, name). Handing the wizard the names
          // already taken lets it say so on step 1, instead of the server
          // saying it as a 409 after four steps of work.
          existingNames={agents.map((agent) => agent.name)}
          onCreated={async (agent) => {
            setError(null);
            setCreateOpen(false);
            await loadAgents();
            onOpenAgent(agent.id);
          }}
        />
      </Drawer>
    </>
  );
}
