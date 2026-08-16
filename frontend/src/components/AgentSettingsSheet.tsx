/**
 * Agent settings: what `[...]` opens.
 *
 * Two things are happening here and only one of them was asked for.
 *
 * **The relocation.** The retrieval parameters and the system prompt used to sit
 * in the page header as two `<Reveal>` panels. Measured at 1440x900, opening
 * them moved the chat panel's top edge from 576px to 1092px -- past the bottom
 * of a 900px viewport -- and since `AgentDetail` sizes that panel as
 * `calc(100dvh - top)`, the complement went negative and the chat collapsed to
 * 24px with zero visible thread. In a sheet, opening this costs the workspace
 * nothing, because an overlay has no height in the flow.
 *
 * **The part that is new: it writes.** `PATCH /api/agents/{agent_id}` has been
 * complete on the server the whole time and no frontend code has ever called
 * it -- `CreateAgentWizard`'s own header says so: "there is no agent-settings UI
 * anywhere, so an agent created with the wrong parameters could not be corrected
 * from the browser." This is that UI. It matters more than it sounds, because
 * "change chunk_size, watch the answer change" is the exercise the workshop is
 * built around, and until now the only way to do it was to delete the agent and
 * make a new one.
 *
 * ---
 *
 * **The grouping is the design, and it comes from reading `app/rag/` rather than
 * from taste.** Ten parameters rendered as ten identical sliders tell the reader
 * that all ten do the same kind of thing. They do not:
 *
 * - `retrieve_k`, `rerank_*`, `tools_*` and `system_prompt` are read at QUERY
 *   time, so a change is in the very next answer.
 * - `chunk_size`, `chunk_overlap` and `splitter` are read at INGEST time. A
 *   change does NOTHING to material already indexed -- the vectors keep the
 *   chunking they were written with. Silence after saving is exactly what a
 *   broken app looks like, so the group says so before you touch it.
 * - `score_threshold` and `max_rewrites` change nothing at all today. See below.
 *
 * **The two inert parameters are shown, labelled, rather than hidden.** Both were
 * confirmed by exhaustive grep over `app/` and `scripts/`: `max_rewrites` is read
 * by no code whatsoever (PRD 3.5's Stage 2 rewrite loop is unimplemented; the
 * only rewriter is history contextualisation, which is not score-triggered), and
 * `score_threshold` is computed in `ask.py` purely to be recorded, which writes
 * the literal action string "advisory". Hiding them would be tidier and worse:
 * the trace panel prints `score_threshold` on every single turn, so a reader who
 * meets it there would have nowhere to find out what it is. CLAUDE.md already
 * states the same thing out loud -- "governs rewriting, not refusing; not a
 * safety control" -- and recording what is not yet true is this repository's
 * habit rather than an apology.
 *
 * **Validation is the server's job, with exactly one exception.** Every bound is
 * enforced server-side and every rejection comes back with a usable `detail`, so
 * re-implementing them here would create two sources of truth whose drift shows
 * up as a form silently refusing a value the API accepts. The one check
 * duplicated is `chunk_overlap >= chunk_size`, because it is the only rule judged
 * against the MERGED config rather than the request body -- a user can trip it by
 * editing one field while the other keeps its saved value, and the failure would
 * arrive as a 422 after pressing Save. It warns; it does not block. Per
 * `new features/loop.md` T3, that asymmetry is the reason: a false positive costs
 * a warning under a field being edited, a false negative costs a round trip and a
 * rejected save.
 *
 * **What is deliberately read-only.** `AgentUpdate` sets `extra="forbid"`, so an
 * unknown key is a 422 rather than an ignored field. `icon`, `persona_role`,
 * `pedagogy`, `category`, `status` and `visibility` are copied at creation and
 * are not patchable; `embedding_model` is declared on the model only to be
 * refused, because a namespace built by one model and queried with another
 * returns confident nonsense rather than an error. They are rendered, with the
 * reason, rather than omitted -- a parameter that vanishes reads as a parameter
 * that does not exist.
 */

import { useEffect, useId, useMemo, useState } from "react";
import { agents } from "../lib/api.ts";
import type { Agent, AgentPatch } from "../lib/types.ts";
import {
  API_BOUNDS,
  ErrorBanner,
  Fact,
  ParamSlider,
  SLIDER_BAND,
  Segmented,
  errorMessage,
} from "./ui.tsx";
import Drawer from "./Drawer.tsx";

/**
 * The models this build has actually been measured against, and the sentinel for
 * "not one of these".
 *
 * **A shortlist, not a whitelist.** The API deliberately accepts any
 * `author/model` id (see `GenerationModel` in `backend/app/api/agents.py`), and
 * "Other" below reaches it. What this list buys is that the three obvious choices
 * are known to work with the exact request this app sends -- `require_parameters`
 * routing, `max_tokens` in `extra_body`, a `reasoning` flag, and `tools` +
 * `tool_choice` on a tool turn. A model can be live on OpenRouter and still fail
 * every one of those.
 *
 * Measured 2026-08-16 on plain generation, a tool-bound call and
 * `with_structured_output`; all three pass for all three entries. The labels name
 * the property that actually distinguishes them, because "which model" is not a
 * question a workshop attendee can answer from a slug.
 */
const KNOWN_MODELS: { slug: string; label: string }[] = [
  {
    slug: "deepseek/deepseek-v4-flash-0731",
    label: "DeepSeek V4 Flash - searches on its own judgement",
  },
  {
    slug: "google/gemma-4-31b-it",
    label: "Gemma 4 31B - never searches unprompted; the gap trigger does it",
  },
  {
    slug: "google/gemini-3.7-flash",
    label: "Gemini 3.7 Flash - thinking cannot be turned off",
  },
];

/** Sentinel option value. Not a legal model id, because it contains no "/". */
const CUSTOM = "__custom__";

/** The editable shape, flattened out of `Agent`. Strings for the three
 *  free-text fields because neither a `<textarea>` nor a `<select>` can hold
 *  `null` -- the mapping back to null happens in `buildPatch`, in one place. */
type Draft = {
  name: string;
  description: string;
  system_prompt: string;
  chunk_size: number;
  chunk_overlap: number;
  splitter: string;
  retrieve_k: number;
  rerank_enabled: boolean;
  rerank_top_n: number;
  score_threshold: number;
  max_rewrites: number;
  generation_model: string;
  tools_enabled: boolean;
  max_tool_steps: number;
  self_check_enabled: boolean;
};

function draftFrom(agent: Agent): Draft {
  return {
    name: agent.name,
    description: agent.description ?? "",
    system_prompt: agent.system_prompt ?? "",
    chunk_size: agent.chunk_size,
    chunk_overlap: agent.chunk_overlap,
    splitter: agent.splitter,
    retrieve_k: agent.retrieve_k,
    rerank_enabled: agent.rerank_enabled,
    rerank_top_n: agent.rerank_top_n,
    score_threshold: agent.score_threshold,
    // "" is the empty selection, which `buildPatch` maps back to null --
    // the same round trip `description` and `system_prompt` make, because a
    // <select> cannot hold null either.
    generation_model: agent.generation_model ?? "",
    max_rewrites: agent.max_rewrites,
    tools_enabled: agent.tools_enabled,
    max_tool_steps: agent.max_tool_steps,
    self_check_enabled: agent.self_check_enabled,
  };
}

/**
 * Only what changed.
 *
 * This is not an optimisation, it is the contract. The handler uses
 * `exclude_unset=True`, so an omitted key means "leave alone" while an explicit
 * `null` means "set null" -- and several of these columns are NOT NULL, which
 * returns 422 naming the fields. Posting every rendered field would therefore
 * send `null` for each empty box and fail the save, rather than doing nothing.
 *
 * The two nullable fields are mapped from "" to `null` rather than to an empty
 * string, because clearing a textarea means "there is no description" and not
 * "the description is the empty string" -- and `system_prompt: null` is
 * explicitly a legitimate clear on the server side.
 */
function buildPatch(agent: Agent, draft: Draft): AgentPatch {
  const patch: AgentPatch = {};

  const name = draft.name.trim();
  if (name !== agent.name) patch.name = name;

  const description = draft.description.trim() === "" ? null : draft.description;
  if (description !== (agent.description ?? null)) patch.description = description;

  const prompt = draft.system_prompt.trim() === "" ? null : draft.system_prompt;
  if (prompt !== (agent.system_prompt ?? null)) patch.system_prompt = prompt;

  // Trimmed before comparing AND before sending: the server strips it too
  // (`GenerationModel` carries `strip_whitespace`), so an untrimmed draft
  // would read as dirty, save, come back normalised, and read as dirty again.
  const model = draft.generation_model.trim() === "" ? null : draft.generation_model.trim();
  if (model !== (agent.generation_model ?? null)) patch.generation_model = model;

  if (draft.chunk_size !== agent.chunk_size) patch.chunk_size = draft.chunk_size;
  if (draft.chunk_overlap !== agent.chunk_overlap) patch.chunk_overlap = draft.chunk_overlap;
  if (draft.splitter !== agent.splitter) {
    patch.splitter = draft.splitter as "markdown" | "recursive";
  }
  if (draft.retrieve_k !== agent.retrieve_k) patch.retrieve_k = draft.retrieve_k;
  if (draft.rerank_enabled !== agent.rerank_enabled) patch.rerank_enabled = draft.rerank_enabled;
  if (draft.rerank_top_n !== agent.rerank_top_n) patch.rerank_top_n = draft.rerank_top_n;
  if (draft.score_threshold !== agent.score_threshold) {
    patch.score_threshold = draft.score_threshold;
  }
  if (draft.max_rewrites !== agent.max_rewrites) patch.max_rewrites = draft.max_rewrites;
  if (draft.tools_enabled !== agent.tools_enabled) patch.tools_enabled = draft.tools_enabled;
  if (draft.max_tool_steps !== agent.max_tool_steps) patch.max_tool_steps = draft.max_tool_steps;
  if (draft.self_check_enabled !== agent.self_check_enabled)
    patch.self_check_enabled = draft.self_check_enabled;

  return patch;
}

export default function AgentSettingsSheet({
  agent,
  open,
  onClose,
  onSaved,
}: {
  agent: Agent;
  open: boolean;
  onClose: () => void;
  /** The PATCH response is a full `AgentOut`, the same shape `GET` returns, so
   *  the owner can swap its record straight in without a refetch. */
  onSaved: (updated: Agent) => void;
}) {
  const [draft, setDraft] = useState<Draft>(() => draftFrom(agent));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  const nameId = useId();
  const descriptionId = useId();
  const promptId = useId();
  const modelId = useId();
  // Whether the custom box is showing because the user picked "Other".
  const [customModel, setCustomModel] = useState(false);

  /**
   * Re-seed from the record when the sheet opens, or when it is pointed at a
   * different agent.
   *
   * **Keyed on `agent.id`, deliberately NOT on `agent`.** The first version
   * depended on the whole object and it was wrong twice over. The visible
   * symptom was cosmetic: saving replaces the record, a new object identity
   * re-ran this effect, and the "Saved." confirmation was wiped in the same
   * frame it appeared. The bug underneath was not cosmetic at all -- the owner
   * refetches this agent whenever the corpus changes, so an upload finishing
   * while the sheet was open would have silently discarded whatever the user had
   * typed into it.
   *
   * Re-seeding on open is still right: closing with unsaved edits and reopening
   * should show the saved values rather than a stale draft. A sheet with a
   * visible Save button that silently remembers an abandoned edit will write it
   * the next time the user changes something unrelated.
   *
   * This moves no focus. `Drawer` focuses its heading on open, and the wizard
   * has already paid for the alternative: a second effect touching focus fires a
   * blur that a "has this field been visited" flag reads as a real user
   * interaction, and the form opens already scolding you.
   */
  useEffect(() => {
    if (!open) return;
    setDraft(draftFrom(agent));
    setError(null);
    setSaved(false);
    // `agent` is read here and is intentionally absent from the dependency
    // list. There is no ESLint in this project, so there is no disable comment
    // to write -- this note is the only thing standing between the omission and
    // someone "fixing" it back to the bug described above.
  }, [open, agent.id]);

  // The box also shows itself for a value that is set but unlisted -- an agent
  // already on a custom model, or one whose model was set by direct SQL. Without
  // this the select would fall back to "Other" while the value stayed invisible,
  // and the first edit to any other field would look like it had cleared it.
  const isCustomValue = useMemo(
    () =>
      draft.generation_model !== "" &&
      !KNOWN_MODELS.some((m) => m.slug === draft.generation_model),
    [draft.generation_model],
  );

  const patch = useMemo(() => buildPatch(agent, draft), [agent, draft]);
  const dirtyCount = Object.keys(patch).length;

  /** The one rule duplicated client-side -- see the file docstring. Judged on
   *  the DRAFT, which is the merged config the server will judge. */
  const overlapProblem =
    draft.chunk_overlap >= draft.chunk_size
      ? "Overlap must be smaller than chunk size. The server will reject this."
      : null;

  function set<K extends keyof Draft>(key: K, value: Draft[K]) {
    setDraft((current) => ({ ...current, [key]: value }));
    setSaved(false);
  }

  async function save() {
    if (dirtyCount === 0 || saving) return;
    setSaving(true);
    setError(null);
    try {
      const updated = await agents.update(agent.id, patch);
      onSaved(updated);
      // Re-seeded from the RESPONSE rather than left as the draft. The server
      // normalises -- a name is stripped, a float is rounded -- and a draft that
      // still holds the pre-normalised value would read as one unsaved change
      // the moment the user touches anything else, and would then send it again.
      setDraft(draftFrom(updated));
      setSaved(true);
    } catch (cause) {
      // Rendered as-is. The server's `detail` is written for a human on every
      // one of the statuses this can return -- 409 on a name already used by
      // another of your agents, 422 on the merged chunk pair, 400 on an
      // embedding-model change -- and paraphrasing it here would mean keeping a
      // second copy of the server's reasons in sync with the first.
      setError(errorMessage(cause));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Drawer
      open={open}
      onClose={onClose}
      title="Agent settings"
      testId="agent-settings"
      width="lg"
    >
      {/*
        `noValidate` even though nothing here carries a constraint yet, for the
        reason the create wizard records: native constraint validation ABORTS the
        submit event, so adding a bare `required` later would silently stop
        `onSubmit` running and leave the handling beside it as dead code that
        looks like it works. The flag costs nothing now and removes the trap.
      */}
      <form
        noValidate
        onSubmit={(event) => {
          event.preventDefault();
          void save();
        }}
        className="mt-4 flex min-h-0 flex-1 flex-col gap-6"
      >
        <ErrorBanner error={error} />

        <Section
          title="Identity"
          note="Shown in the bar above and on the agents list."
        >
          <div>
            <label htmlFor={nameId} className="block text-xs font-medium text-slate-300">
              Name
            </label>
            <input
              id={nameId}
              data-testid="settings-name"
              value={draft.name}
              onChange={(event) => set("name", event.target.value)}
              className="mt-1 min-h-11 w-full rounded-md border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 outline-none focus:border-slate-500"
            />
          </div>

          <div>
            <label htmlFor={descriptionId} className="block text-xs font-medium text-slate-300">
              Description
            </label>
            <input
              id={descriptionId}
              data-testid="settings-description"
              value={draft.description}
              onChange={(event) => set("description", event.target.value)}
              className="mt-1 min-h-11 w-full rounded-md border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 outline-none focus:border-slate-500"
            />
          </div>
        </Section>

        <Section
          title="Takes effect on the next answer"
          note="Read at query time. Save, then ask a question and the change is in it."
        >
          <ParamSlider
            id="settings-retrieve-k"
            label="Retrieve k"
            help="How many chunks come back from the vector search before reranking."
            value={draft.retrieve_k}
            onChange={(next) => set("retrieve_k", next)}
            band={SLIDER_BAND.retrieve_k}
            bounds={API_BOUNDS.retrieve_k}
          />

          <Segmented
            legend="Rerank"
            help="A second pass that reorders the retrieved chunks by relevance. Costs about 800 ms."
            name="settings-rerank"
            testId="settings-rerank"
            value={draft.rerank_enabled ? "on" : "off"}
            options={[
              { value: "on", label: "On" },
              { value: "off", label: "Off" },
            ]}
            onChange={(next) => set("rerank_enabled", next === "on")}
          />

          <ParamSlider
            id="settings-rerank-top-n"
            label="Rerank top n"
            help="How many of the reranked chunks reach the model."
            value={draft.rerank_top_n}
            onChange={(next) => set("rerank_top_n", next)}
            band={SLIDER_BAND.rerank_top_n}
            bounds={API_BOUNDS.rerank_top_n}
            disabled={!draft.rerank_enabled}
          />

          <Segmented
            legend="Tools"
            help="Lets the agent search the corpus again or write and run Python mid-answer. Adds a few seconds when it does."
            name="settings-tools"
            testId="settings-tools"
            value={draft.tools_enabled ? "on" : "off"}
            options={[
              { value: "on", label: "On" },
              { value: "off", label: "Off" },
            ]}
            onChange={(next) => set("tools_enabled", next === "on")}
          />

          {/*
            The help text names the CHEAP case on purpose. The check is a free
            set operation against the citation ledger on almost every turn -- a
            marker the answer used that no retrieved passage carries -- and only
            pays for a model call when that test fires. Describing it as "checks
            every answer" would be true and would read as a per-turn cost the
            feature does not have, which is the kind of copy that gets a working
            control switched off.

            It is not disabled when `tools_enabled` is off, unlike the step
            slider above. The two are independent: an agent with no tools still
            drafts an answer that can cite something it was never given.
          */}
          <Segmented
            legend="Self-check"
            help="Checks the answer against its own citations before it settles, and redrafts if a claim is not carried by a retrieved passage. Costs nothing on a well-grounded answer."
            name="settings-self-check"
            testId="settings-self-check"
            value={draft.self_check_enabled ? "on" : "off"}
            options={[
              { value: "on", label: "On" },
              { value: "off", label: "Off" },
            ]}
            onChange={(next) => set("self_check_enabled", next === "on")}
          />

          {/*
            A <select>, not a `Segmented`. Segmented lays its options out in one
            `inline-flex` row that does not wrap, and model slugs are long -- three
            of them would push the sheet past the viewport and fail `ui_check.py`
            A7, which asserts zero horizontal overflow at 320px.

            The list is a SHORTLIST, not a whitelist: the API accepts any
            `author/model` id, and "Other" reveals a text box for one. Every entry
            here was measured on 2026-08-16 against the exact request this app
            sends -- plain generation, a tool-bound call, and
            `with_structured_output` -- because "it is on OpenRouter" does not mean
            "it works here". Gemini 3.7 Flash failed all three until
            `build_chat_model` stopped sending it `reasoning:{enabled:false}`,
            which it answers with a hard 400.
          */}
          <div>
            <label htmlFor={modelId} className="block text-xs font-medium text-slate-300">
              Generation model
            </label>
            <p className="mt-0.5 text-xs text-slate-400">
              Which model writes the answers. This changes behaviour, not just cost:
              DeepSeek searches the corpus again on its own judgement, Gemma does not
              and relies on the gap trigger to make it look.
            </p>
            <select
              id={modelId}
              data-testid="settings-generation-model"
              data-value={draft.generation_model}
              value={KNOWN_MODELS.some((m) => m.slug === draft.generation_model)
                ? draft.generation_model
                : draft.generation_model === ""
                  ? ""
                  : CUSTOM}
              onChange={(event) => {
                const next = event.target.value;
                // Switching to "Other" must not pre-fill the box with a slug the
                // user did not type, so it clears -- and an empty custom value is
                // indistinguishable from "server default", which is the safe
                // reading of a half-finished edit.
                set("generation_model", next === CUSTOM ? "" : next);
                setCustomModel(next === CUSTOM);
              }}
              className="mt-1 min-h-11 w-full rounded-md border border-slate-700 bg-slate-950 px-3 text-sm text-slate-100 outline-none focus:border-slate-500"
            >
              <option value="">Server default</option>
              {KNOWN_MODELS.map((entry) => (
                <option key={entry.slug} value={entry.slug}>
                  {entry.label}
                </option>
              ))}
              <option value={CUSTOM}>Other (type an OpenRouter id)…</option>
            </select>
            {(customModel || isCustomValue) && (
              <input
                data-testid="settings-generation-model-custom"
                value={draft.generation_model}
                onChange={(event) => set("generation_model", event.target.value)}
                placeholder="author/model"
                aria-label="Custom OpenRouter model id"
                className="mt-2 min-h-11 w-full rounded-md border border-slate-700 bg-slate-950 px-3 py-2 font-mono text-xs text-slate-100 outline-none focus:border-slate-500"
              />
            )}
          </div>

          <ParamSlider
            id="settings-max-tool-steps"
            label="Max tool steps"
            help="A ceiling, not a target. Most turns use none; the loop is closed and an answer forced when this runs out."
            value={draft.max_tool_steps}
            onChange={(next) => set("max_tool_steps", next)}
            band={SLIDER_BAND.max_tool_steps}
            bounds={API_BOUNDS.max_tool_steps}
            disabled={!draft.tools_enabled}
          />

          <div>
            <label htmlFor={promptId} className="block text-xs font-medium text-slate-300">
              System prompt
            </label>
            <p className="mt-0.5 text-xs text-slate-400">
              The grounding rule comes before the voice in every prompt here, and that order is
              what lets the agent be trusted when it says it does not know.
            </p>
            <textarea
              id={promptId}
              data-testid="settings-prompt"
              rows={10}
              value={draft.system_prompt}
              onChange={(event) => set("system_prompt", event.target.value)}
              className="mt-1 min-h-11 w-full resize-y rounded-md border border-slate-700 bg-slate-950 px-3 py-2 font-mono text-xs leading-relaxed text-slate-200 outline-none focus:border-slate-500"
            />
          </div>
        </Section>

        <Section
          title="Takes effect on the next upload"
          note="Read at ingest time. Documents already indexed keep the chunking they were ingested with -- re-upload to apply a change."
        >
          <ParamSlider
            id="settings-chunk-size"
            label="Chunk size"
            help="Tokens per chunk when a document is split."
            value={draft.chunk_size}
            onChange={(next) => set("chunk_size", next)}
            band={SLIDER_BAND.chunk_size}
            bounds={API_BOUNDS.chunk_size}
          />

          <ParamSlider
            id="settings-chunk-overlap"
            label="Chunk overlap"
            help="Tokens each chunk repeats from the one before, so a sentence split across a boundary is still retrievable."
            value={draft.chunk_overlap}
            onChange={(next) => set("chunk_overlap", next)}
            band={SLIDER_BAND.chunk_overlap}
            bounds={API_BOUNDS.chunk_overlap}
            warning={overlapProblem}
          />

          <Segmented
            legend="Splitter"
            help="Markdown splits on headings and keeps sections whole; recursive splits on paragraphs and sentences."
            name="settings-splitter"
            testId="settings-splitter"
            value={draft.splitter}
            options={[
              { value: "markdown", label: "Markdown" },
              { value: "recursive", label: "Recursive" },
            ]}
            onChange={(next) => set("splitter", next)}
          />
        </Section>

        <Section
          title="Recorded, but changes nothing today"
          note="Both are saved and both appear in the trace. Neither is read by any code path in this build, so changing them will not change an answer. They are shown rather than hidden because the trace prints score_threshold on every turn."
          testId="settings-inert"
        >
          <ParamSlider
            id="settings-score-threshold"
            label="Score threshold"
            help="Specified as the trigger for the Stage 2 rewrite loop. Measured on this corpus, on-topic questions scored 0.61-0.67 and off-topic 0.49-0.58, so 0.5 sits inside the overlap -- which is why the loop reads the model's own words instead. Refusal comes from the prompt, never from this number."
            value={draft.score_threshold}
            onChange={(next) => set("score_threshold", next)}
            band={SLIDER_BAND.score_threshold}
            bounds={API_BOUNDS.score_threshold}
            decimals={2}
          />

          <ParamSlider
            id="settings-max-rewrites"
            label="Max rewrites"
            help="Would bound the Stage 2 rewrite loop, which is not implemented. The only rewriter in this build is history contextualisation, which turns 'what is its power budget?' into a standalone question -- it is triggered by a pronoun, not by a score, and is not bounded by this."
            value={draft.max_rewrites}
            onChange={(next) => set("max_rewrites", next)}
            band={SLIDER_BAND.max_rewrites}
            bounds={API_BOUNDS.max_rewrites}
          />
        </Section>

        <Section
          title="Fixed for the life of this agent"
          note="The API refuses all of these. They are copied from the template when the agent is created."
          testId="settings-fixed"
        >
          <dl className="grid grid-cols-2 gap-x-6 gap-y-2 text-xs">
            <Fact label="Persona" value={agent.persona_role ?? "Custom agent"} />
            <Fact label="Category" value={agent.category ?? "ungrouped"} />
            <Fact label="Documents" value={agent.document_count} />
            <Fact label="Embedding model" value={agent.embedding_model ?? "unset"} />
          </dl>
          <p className="text-xs text-slate-400">
            The embedding model cannot change without re-ingesting: a namespace built by one
            model and queried with another returns confident nonsense rather than an error, so
            the API returns 400 rather than letting the record drift from the vectors.
          </p>
        </Section>

        {/*
          Sticky, inside the Drawer's own `overflow-y-auto` panel. This form is
          taller than the sheet at every viewport, and a Save button that has to
          be scrolled to is a Save button people do not press.

          **`-bottom-4`, not `bottom-0`, and the difference was visible.** A
          sticky offset resolves against the scroll container's PADDING box, and
          the Drawer panel carries `p-4` -- so `bottom-0` parked the bar 16px
          short of the panel's bottom edge and the form scrolled through the gap
          underneath it, which reads as a rendering bug. The negative margins
          (`-mx-4 -mb-4`) make the bar span the full width, but a margin does not
          move where sticky comes to rest; only the offset does. `-bottom-4`
          cancels exactly the padding that created the gap.

          The panel keeps its `p-4` regardless, because the global focus ring is
          `outline: 3px` at `outline-offset: 3px` and a control flush against the
          edge of a clipping box loses six pixels of its indicator.
        */}
        <div className="sticky -bottom-4 -mx-4 -mb-4 mt-auto flex flex-wrap items-center gap-3 border-t border-slate-800 bg-slate-900 px-4 py-3">
          <button
            type="submit"
            data-testid="settings-save"
            disabled={dirtyCount === 0 || saving}
            className="min-h-11 rounded-md bg-emerald-500 px-4 py-2 text-sm font-semibold text-emerald-950 transition hover:bg-emerald-400 disabled:opacity-50"
          >
            {saving ? "Saving…" : "Save changes"}
          </button>

          <button
            type="button"
            data-testid="settings-revert"
            disabled={dirtyCount === 0 || saving}
            onClick={() => {
              setDraft(draftFrom(agent));
              setSaved(false);
            }}
            className="min-h-11 rounded-md border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-300 transition hover:border-slate-600 disabled:opacity-50"
          >
            Revert
          </button>

          {/* One live region for both states, so a screen reader is not told
              about a change count and a save result by two separate
              announcements racing each other. */}
          <span
            data-testid="settings-status"
            role="status"
            aria-live="polite"
            className="text-xs text-slate-400"
          >
            {dirtyCount > 0
              ? `${dirtyCount} unsaved ${dirtyCount === 1 ? "change" : "changes"}`
              : saved
                ? "Saved."
                : "No changes."}
          </span>
        </div>
      </form>
    </Drawer>
  );
}

/** A titled group with a note explaining WHEN its fields take effect. The note
 *  is not decoration: it is the whole reason the sheet is grouped this way. */
function Section({
  title,
  note,
  children,
  testId,
}: {
  title: string;
  note: string;
  children: React.ReactNode;
  testId?: string;
}) {
  return (
    <section data-testid={testId} className="min-w-0 space-y-3">
      <div>
        <h3 className="text-xs font-medium tracking-wide text-slate-300 uppercase">{title}</h3>
        <p className="mt-1 text-xs leading-relaxed text-slate-400">{note}</p>
      </div>
      {children}
    </section>
  );
}
