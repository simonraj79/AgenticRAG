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
 * asking. The parameters are still on the page, one disclosure down, because
 * this is a workshop artifact and they are the thing Build #1 changes.
 *
 * One agent is one corpus, one config and one Pinecone namespace (PRD section
 * 3.2). That is why deleting an agent is a genuinely destructive act, why the
 * delete button confirms, and why it is not placed beside Open.
 */

import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";
import { api } from "../lib/api.ts";
import type { Agent, Template } from "../lib/types.ts";
import {
  CategoryBadge,
  ConfirmDeleteButton,
  EmptyState,
  ErrorBanner,
  Fact,
  PersonaIcon,
  Reveal,
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

  return (
    <div className="mx-auto max-w-6xl px-6 py-10">
      <header className="mb-8 flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-slate-100">Your agents</h1>
          <p className="mt-1 max-w-2xl text-sm text-slate-400">
            Each agent owns one document corpus, one teaching persona and one isolated
            vector namespace. Nothing it retrieves can come from another agent.
          </p>
        </div>

        <button
          type="button"
          data-testid="create-agent-toggle"
          aria-expanded={createOpen}
          aria-controls="create-agent-panel"
          onClick={() => setCreateOpen((open) => !open)}
          className={`rounded-md border px-4 py-2 text-sm font-medium transition ${
            createOpen
              ? "border-slate-700 bg-slate-900 text-slate-300 hover:border-slate-600"
              : "border-emerald-500 bg-emerald-500 text-emerald-950 hover:bg-emerald-400"
          }`}
        >
          {createOpen ? "Close" : "New agent"}
        </button>
      </header>

      <div className="mb-8">
        <ErrorBanner error={error} />
      </div>

      {createOpen && (
        <div id="create-agent-panel" className="mb-10">
          <CreateAgentForm
            templates={templates}
            onCreated={async (agent) => {
              setError(null);
              setCreateOpen(false);
              await loadAgents();
              onOpenAgent(agent.id);
            }}
            onError={setError}
          />
        </div>
      )}

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
                      <span className="text-slate-600"> · </span>
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
                  onClick={() => onOpenAgent(agent.id)}
                  className="rounded-md bg-slate-100 px-3 py-1.5 text-sm font-medium text-slate-900 transition hover:bg-white"
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
                <span className="text-[0.65rem] text-slate-600">
                  Deletes the corpus and its vectors
                </span>
                <ConfirmDeleteButton
                  testId="agent-delete"
                  label="Delete"
                  confirmLabel="Delete agent + vectors?"
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
  );
}

/**
 * Create an agent by choosing a persona.
 *
 * The cards carry icon, name, role, and the line naming the learning science
 * the persona rests on, because that line is the actual difference between two
 * templates whose retrieval parameters are identical -- the Feynman explainer
 * and the Socratic tutor differ in nothing a parameter grid can show. The
 * pedagogy is clamped on unselected cards and shown in full on the selected
 * one, so eight cards stay scannable without the chosen one hiding its reason.
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
  // without touching the picker. The server orders them, and its first entry is
  // the general-purpose one -- the safe default for someone who has not read
  // any of the cards yet.
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
          // meaning "server defaults".
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
    <form onSubmit={submit} className="rounded-xl border border-slate-800 bg-slate-900/50 p-5">
      <h2 className="text-sm font-medium tracking-wide text-slate-400 uppercase">New agent</h2>

      <fieldset className="mt-5">
        <legend className="text-sm font-medium text-slate-200">Choose a teaching persona</legend>
        <p className="mt-1 mb-4 text-xs text-slate-500">
          The persona decides how the agent answers -- what it asks back, what it withholds,
          how it refuses. It never changes what the agent may answer <em>from</em>: every
          persona is bound to this agent&rsquo;s documents alone.
        </p>

        {templates.length === 0 ? (
          <p className="text-xs text-slate-500">
            No templates loaded. The agent will be created with the server&rsquo;s default
            parameters.
          </p>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {templates.map((template) => {
              const active = template.id === templateId;
              return (
                <label
                  key={template.id}
                  data-testid="template-card"
                  data-template-slug={template.slug}
                  data-selected={active}
                  className={`flex cursor-pointer flex-col rounded-xl border p-4 transition focus-within:ring-2 focus-within:ring-emerald-500/60 ${
                    active
                      ? "border-emerald-500/70 bg-emerald-950/20"
                      : "border-slate-800 bg-slate-950/50 hover:border-slate-700"
                  }`}
                >
                  {/*
                    A real radio, visually hidden rather than replaced. Arrow-key
                    navigation within the group, the required/checked semantics
                    and the label-click target all come free from the native
                    control; a div with an onClick would have to reimplement
                    each one and would still be invisible to a screen reader.
                  */}
                  <input
                    type="radio"
                    name="agent-template"
                    className="sr-only"
                    value={template.id}
                    checked={active}
                    onChange={() => setTemplateId(template.id)}
                  />

                  <div className="flex items-start gap-3">
                    <PersonaIcon icon={template.icon} fallback={template.name} size="sm" />
                    <div className="min-w-0 flex-1">
                      <div className="flex items-start justify-between gap-2">
                        <span className="font-medium text-slate-100">{template.name}</span>
                        <CategoryBadge category={template.category} />
                      </div>
                      {template.persona_role && (
                        <span className="mt-0.5 block text-xs tracking-wide text-slate-500 uppercase">
                          {template.persona_role}
                        </span>
                      )}
                    </div>
                  </div>

                  {template.description && (
                    <p className="mt-3 text-sm text-slate-400">{template.description}</p>
                  )}

                  {template.pedagogy && (
                    <p
                      className={`mt-3 border-t border-slate-800 pt-3 text-xs leading-relaxed text-slate-500 ${
                        active ? "" : "line-clamp-3"
                      }`}
                    >
                      <span className="font-medium text-slate-400">Rests on: </span>
                      {template.pedagogy}
                    </p>
                  )}
                </label>
              );
            })}
          </div>
        )}
      </fieldset>

      <div className="mt-6 grid gap-4 sm:grid-cols-2">
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
      </div>

      {selected && (
        <div className="mt-5 space-y-3">
          <Reveal summary="Advanced — retrieval parameters" testId="template-parameters">
            <dl className="grid grid-cols-2 gap-x-6 gap-y-2 text-xs sm:grid-cols-4">
              <Fact label="Chunk size" value={selected.chunk_size} />
              <Fact label="Overlap" value={selected.chunk_overlap} />
              <Fact label="Splitter" value={selected.splitter} />
              <Fact label="Retrieve k" value={selected.retrieve_k} />
              <Fact label="Rerank" value={selected.rerank_enabled ? "on" : "off"} />
              <Fact label="Rerank top n" value={selected.rerank_top_n} />
              <Fact label="Score threshold" value={selected.score_threshold} />
              <Fact label="Max rewrites" value={selected.max_rewrites} />
            </dl>
            <p className="mt-3 text-xs text-slate-500">
              These values are <em>copied</em> onto the agent. Editing the template later
              will not re-tune an agent you already built.
            </p>
          </Reveal>

          {selected.system_prompt && (
            <Reveal summary="Advanced — system prompt" testId="template-prompt">
              <pre className="max-h-64 overflow-y-auto text-xs leading-relaxed whitespace-pre-wrap text-slate-400">
                {selected.system_prompt}
              </pre>
            </Reveal>
          )}
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
