/**
 * Dashboard: the user's agents, and the form that makes another one.
 *
 * One agent is one corpus, one config and one Pinecone namespace (PRD section
 * 3.2). That is why deleting an agent is a genuinely destructive act and why
 * the delete button here confirms -- the namespace goes with the row, and the
 * vectors in it are not recoverable from Pinecone afterwards. They ARE
 * rebuildable from `chunks.text` in Postgres, but only for documents that still
 * exist, which after this call they do not.
 */

import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";
import { api } from "../lib/api.ts";
import type { Agent, Template } from "../lib/types.ts";
import {
  ConfirmDeleteButton,
  ErrorBanner,
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

  return (
    <div className="mx-auto max-w-5xl px-6 py-10">
      <header className="mb-8">
        <h1 className="text-2xl font-semibold tracking-tight text-slate-100">Your agents</h1>
        <p className="mt-1 text-sm text-slate-400">
          Each agent owns one document corpus and one isolated vector namespace. Nothing
          it retrieves can come from another agent.
        </p>
      </header>

      <div className="mb-8">
        <ErrorBanner error={error} />
      </div>

      <CreateAgentForm
        templates={templates}
        onCreated={async (agent) => {
          setError(null);
          await loadAgents();
          onOpenAgent(agent.id);
        }}
        onError={setError}
      />

      <section className="mt-10">
        <h2 className="mb-4 text-sm font-medium tracking-wide text-slate-400 uppercase">
          Agents ({agents.length})
        </h2>

        {loading && <Spinner label="Loading agents" />}

        {!loading && agents.length === 0 && (
          <p className="rounded-lg border border-dashed border-slate-800 px-4 py-8 text-center text-sm text-slate-500">
            No agents yet. Create one above, then upload documents to it.
          </p>
        )}

        <ul className="grid gap-4 sm:grid-cols-2">
          {agents.map((agent) => (
            <li
              key={agent.id}
              data-testid="agent-card"
              data-agent-id={agent.id}
              className="rounded-xl border border-slate-800 bg-slate-900/50 p-5"
            >
              <div className="flex items-start justify-between gap-3">
                <h3 className="font-medium text-slate-100">{agent.name}</h3>
                <StatusPill status={agent.status} />
              </div>

              {agent.description && (
                <p className="mt-2 line-clamp-2 text-sm text-slate-400">{agent.description}</p>
              )}

              <dl className="mt-4 grid grid-cols-3 gap-2 text-xs">
                <div>
                  <dt className="text-slate-500">Documents</dt>
                  <dd className="mt-0.5 text-slate-200">{agent.document_count}</dd>
                </div>
                <div>
                  <dt className="text-slate-500">Chunk size</dt>
                  <dd className="mt-0.5 text-slate-200">{agent.chunk_size}</dd>
                </div>
                <div>
                  <dt className="text-slate-500">Retrieve k</dt>
                  <dd className="mt-0.5 text-slate-200">{agent.retrieve_k}</dd>
                </div>
              </dl>

              <div className="mt-5 flex items-center gap-2">
                <button
                  type="button"
                  data-testid="agent-open"
                  onClick={() => onOpenAgent(agent.id)}
                  className="rounded-md bg-slate-100 px-3 py-1.5 text-sm font-medium text-slate-900 transition hover:bg-white"
                >
                  Open
                </button>
                <ConfirmDeleteButton
                  testId="agent-delete"
                  label="Delete"
                  confirmLabel="Delete agent + vectors?"
                  busy={deletingId === agent.id}
                  onConfirm={() => void deleteAgent(agent)}
                />
              </div>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}

/**
 * Create an agent, optionally from a template.
 *
 * The picker shows chunk size and retrieve k next to every name because the
 * template names alone ("Lecture Q&A", "Policy Lookup") say what the preset is
 * FOR but not what it DOES, and those two numbers are most of the difference
 * between them. The panel underneath shows the rest of the preset, so the
 * choice is legible before it is made rather than discoverable afterwards in
 * the agent's settings.
 */
function CreateAgentForm({
  templates,
  onCreated,
  onError,
}: {
  templates: Template[];
  onCreated: (agent: Agent) => void | Promise<void>;
  onError: (message: string | null) => void;
}) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [templateId, setTemplateId] = useState("");
  const [busy, setBusy] = useState(false);

  // Default to the first template once they arrive, so the form is submittable
  // without touching the select. `templateId === ""` is still reachable by
  // choosing "No template" explicitly.
  useEffect(() => {
    if (templates.length > 0) {
      setTemplateId((current) => (current === "" ? templates[0].id : current));
    }
  }, [templates]);

  const selected = templates.find((template) => template.id === templateId) ?? null;

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    onError(null);
    try {
      const agent = await api<Agent>("/api/agents", {
        method: "POST",
        json: {
          name: name.trim(),
          description: description.trim() || null,
          // Omitted rather than sent as "" -- the column is a nullable FK and
          // an empty string is not a valid UUID, so it would 422 instead of
          // meaning "from scratch".
          template_id: templateId || null,
        },
      });
      setName("");
      setDescription("");
      await onCreated(agent);
    } catch (cause) {
      // 409 is the interesting one: agent names are unique per owner.
      onError(errorMessage(cause));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      onSubmit={submit}
      className="rounded-xl border border-slate-800 bg-slate-900/50 p-5"
    >
      <h2 className="mb-4 text-sm font-medium tracking-wide text-slate-400 uppercase">
        New agent
      </h2>

      <div className="grid gap-4 sm:grid-cols-2">
        <div>
          <label className="block text-xs font-medium text-slate-400" htmlFor="agent-name">
            Name
          </label>
          <input
            id="agent-name"
            data-testid="agent-name-input"
            required
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="Topic 10 Lecture"
            className="mt-1 w-full rounded-md border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 outline-none focus:border-slate-500"
          />
        </div>

        <div>
          <label className="block text-xs font-medium text-slate-400" htmlFor="agent-template">
            Template
          </label>
          <select
            id="agent-template"
            data-testid="agent-template-select"
            value={templateId}
            onChange={(event) => setTemplateId(event.target.value)}
            className="mt-1 w-full rounded-md border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 outline-none focus:border-slate-500"
          >
            <option value="">No template (server defaults)</option>
            {templates.map((template) => (
              <option key={template.id} value={template.id}>
                {template.name} — {template.chunk_size} tokens, k={template.retrieve_k}
              </option>
            ))}
          </select>
        </div>
      </div>

      <div className="mt-4">
        <label className="block text-xs font-medium text-slate-400" htmlFor="agent-description">
          Description <span className="text-slate-600">(optional)</span>
        </label>
        <input
          id="agent-description"
          value={description}
          onChange={(event) => setDescription(event.target.value)}
          placeholder="What this agent knows about"
          className="mt-1 w-full rounded-md border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 outline-none focus:border-slate-500"
        />
      </div>

      {selected && (
        <div className="mt-4 rounded-lg border border-slate-800 bg-slate-950/60 p-4">
          {selected.description && (
            <p className="mb-3 text-xs text-slate-400">{selected.description}</p>
          )}
          <dl className="grid grid-cols-2 gap-x-6 gap-y-2 text-xs sm:grid-cols-4">
            <Param label="Chunk size" value={selected.chunk_size} />
            <Param label="Overlap" value={selected.chunk_overlap} />
            <Param label="Splitter" value={selected.splitter} />
            <Param label="Retrieve k" value={selected.retrieve_k} />
            <Param label="Rerank" value={selected.rerank_enabled ? "on" : "off"} />
            <Param label="Rerank top n" value={selected.rerank_top_n} />
            <Param label="Score threshold" value={selected.score_threshold} />
            <Param label="Max rewrites" value={selected.max_rewrites} />
          </dl>
          <p className="mt-3 text-xs text-slate-500">
            These values are <em>copied</em> onto the agent. Editing the template later
            will not re-tune an agent you already built.
          </p>
        </div>
      )}

      <button
        type="submit"
        data-testid="agent-create-submit"
        disabled={busy}
        className="mt-5 rounded-md bg-emerald-500 px-4 py-2 text-sm font-semibold text-emerald-950 transition hover:bg-emerald-400 disabled:opacity-50"
      >
        {busy ? "Creating…" : "Create agent"}
      </button>
    </form>
  );
}

function Param({ label, value }: { label: string; value: string | number }) {
  return (
    <div>
      <dt className="text-slate-500">{label}</dt>
      <dd className="mt-0.5 text-slate-200">{value}</dd>
    </div>
  );
}
