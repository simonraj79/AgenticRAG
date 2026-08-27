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
 * **The three headings are now `GROUPS`, not three strings typed here.** The
 * create wizard renders the same three bands out of the same array, so someone
 * who tunes an agent after making one meets the same sentence in the same place
 * instead of learning the arrangement twice. This file cannot ITERATE `GROUPS`
 * the way the wizard does -- see the note on `GROUP` below -- but it can stop
 * restating what that array already says.
 *
 * **The words for all ten parameters come from `lib/tunables.ts`.** Every label,
 * column tag, one-line help and "Why this matters" detail for the ten is read
 * from that table; none of it is written here any more. The two surfaces had
 * drifted exactly as far as two independently maintained copies do, and none of
 * it looked like a defect from inside either file: `retrieve_k` was "How many
 * chunks come back from the vector search before reranking" here and "How many
 * chunks Pinecone returns for the reranker to choose from" there; the overlap
 * was "Chunk overlap" here and "Overlap" there; the splitter's options were
 * capitalised here and lower case there; and the Cohere rerank hop -- ONE
 * measurement, ~830 ms -- was quoted here as 800 ms against reranking and there
 * as 830 ms against `retrieve_k`. A number measured once and printed twice
 * differently is the drift arriving in the material the workshop teaches from.
 *
 * The splitter is the one place the split between stored and displayed is load
 * bearing: the option labels became *At headings* / *At paragraphs* and the
 * values stayed `markdown` / `recursive`, which is what the column, the PATCH
 * body, the trace and EVAL.md still say.
 *
 * **Four controls in these same sections are deliberately NOT in that table**,
 * because they are not among the ten: `self_check_enabled`, `generation_model`,
 * `system_prompt` and the identity fields keep their copy here. `tunables.ts` is
 * the contract for the parameters BOTH surfaces edit -- the wizard offers none
 * of these four -- and widening it to serve one screen would turn a shared
 * contract into a copy deck.
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
import { GROUPS, TUNABLES } from "../lib/tunables.ts";
import type { TunableGroup } from "../lib/tunables.ts";
import {
  BTN_PRIMARY,
  BTN_SECONDARY,
  EYEBROW,
  FIELD,
  HELP,
  LABEL,
  TEXTAREA_MONO,
} from "../lib/styles.ts";

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

/**
 * `GROUPS`, keyed by id.
 *
 * **This sheet cannot map over `GROUPS` the way the wizard does, and the four
 * fields that are not tunables are the reason.** The generation-model select,
 * the self-check switch and the system prompt all sit INSIDE the answer band in
 * a deliberate order, and `Identity` and `Fixed for the life of this agent` have
 * no `GROUPS` entry at all -- so an iteration over the three groups would either
 * drop them or invent two more groups to hold them. The three sections stay
 * written out and take their WORDS from here: one voice with the wizard, without
 * claiming the two surfaces render the same list.
 */
const GROUP = Object.fromEntries(GROUPS.map((group) => [group.id, group])) as Record<
  TunableGroup,
  (typeof GROUPS)[number]
>;

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

  /**
   * The one rule duplicated client-side -- see the file docstring. Judged on the
   * DRAFT, which is the merged config the server will judge.
   *
   * The two labels are interpolated rather than retyped. This sentence read
   * "Overlap must be smaller than chunk size" while the controls it points at
   * now read *Passage overlap* and *Passage size* -- the drift `tunables.ts`
   * exists to close, reappearing one line under the two controls it closed it
   * for. Uncased on purpose: naming a control inside a warning only works if the
   * words match the label character for character, so the reader can pair them
   * by sight rather than by inference.
   */
  const overlapProblem =
    draft.chunk_overlap >= draft.chunk_size
      ? `${TUNABLES.chunk_overlap.label} must be smaller than ${TUNABLES.chunk_size.label}. The server will reject this.`
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
            <label htmlFor={nameId} className={`block ${LABEL}`}>
              Name
            </label>
            <input
              id={nameId}
              data-testid="settings-name"
              value={draft.name}
              onChange={(event) => set("name", event.target.value)}
              className={`${FIELD} mt-1.5`}
            />
          </div>

          <div>
            <label htmlFor={descriptionId} className={`block ${LABEL}`}>
              Description
            </label>
            <input
              id={descriptionId}
              data-testid="settings-description"
              value={draft.description}
              onChange={(event) => set("description", event.target.value)}
              className={`${FIELD} mt-1.5`}
            />
          </div>
        </Section>

        <Section title={GROUP.answer.title} note={GROUP.answer.blurb}>
          <ParamSlider
            id="settings-retrieve-k"
            label={TUNABLES.retrieve_k.label}
            tag={TUNABLES.retrieve_k.tag}
            help={TUNABLES.retrieve_k.help}
            detail={TUNABLES.retrieve_k.detail}
            value={draft.retrieve_k}
            onChange={(next) => set("retrieve_k", next)}
            band={SLIDER_BAND.retrieve_k}
            bounds={API_BOUNDS.retrieve_k}
          />

          <Segmented
            legend={TUNABLES.rerank_enabled.label}
            tag={TUNABLES.rerank_enabled.tag}
            help={TUNABLES.rerank_enabled.help}
            detail={TUNABLES.rerank_enabled.detail}
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
            label={TUNABLES.rerank_top_n.label}
            tag={TUNABLES.rerank_top_n.tag}
            help={TUNABLES.rerank_top_n.help}
            detail={TUNABLES.rerank_top_n.detail}
            value={draft.rerank_top_n}
            onChange={(next) => set("rerank_top_n", next)}
            band={SLIDER_BAND.rerank_top_n}
            bounds={API_BOUNDS.rerank_top_n}
            disabled={!draft.rerank_enabled}
          />

          <Segmented
            legend={TUNABLES.tools_enabled.label}
            tag={TUNABLES.tools_enabled.tag}
            help={TUNABLES.tools_enabled.help}
            detail={TUNABLES.tools_enabled.detail}
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

            Hand-written, unlike the ten around it, and that is correct rather
            than an oversight: `self_check_enabled` is not a `TunableKey`. The
            create wizard does not offer this switch, so an eleventh entry in
            `tunables.ts` would be a string with exactly one reader living in a
            file whose whole justification is having two.
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
            <label htmlFor={modelId} className={`block ${LABEL}`}>
              Generation model
            </label>
            <p className={`mt-1 ${HELP}`}>
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
              className={`${FIELD} mt-1.5`}
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
                // A model slug is an identifier, so it is set in mono -- the
                // same family the trace panel prints it in.
                className={`${FIELD} mt-2 font-mono text-xs`}
              />
            )}
          </div>

          <ParamSlider
            id="settings-max-tool-steps"
            label={TUNABLES.max_tool_steps.label}
            tag={TUNABLES.max_tool_steps.tag}
            help={TUNABLES.max_tool_steps.help}
            detail={TUNABLES.max_tool_steps.detail}
            value={draft.max_tool_steps}
            onChange={(next) => set("max_tool_steps", next)}
            band={SLIDER_BAND.max_tool_steps}
            bounds={API_BOUNDS.max_tool_steps}
            disabled={!draft.tools_enabled}
          />

          <div>
            <label htmlFor={promptId} className={`block ${LABEL}`}>
              System prompt
            </label>
            <p className={`mt-1 ${HELP}`}>
              The grounding rule comes before the voice in every prompt here, and that order is
              what lets the agent be trusted when it says it does not know.
            </p>
            {/* `TEXTAREA_MONO`, not `TEXTAREA`: a system prompt is machine text
                read by structure -- paragraph order, blank lines, the position
                of the grounding clause -- and a proportional face hides all
                three. */}
            <textarea
              id={promptId}
              data-testid="settings-prompt"
              rows={10}
              value={draft.system_prompt}
              onChange={(event) => set("system_prompt", event.target.value)}
              className={`${TEXTAREA_MONO} mt-1.5`}
            />
          </div>
        </Section>

        <Section title={GROUP.upload.title} note={GROUP.upload.blurb}>
          <ParamSlider
            id="settings-chunk-size"
            label={TUNABLES.chunk_size.label}
            tag={TUNABLES.chunk_size.tag}
            help={TUNABLES.chunk_size.help}
            detail={TUNABLES.chunk_size.detail}
            value={draft.chunk_size}
            onChange={(next) => set("chunk_size", next)}
            band={SLIDER_BAND.chunk_size}
            bounds={API_BOUNDS.chunk_size}
          />

          <ParamSlider
            id="settings-chunk-overlap"
            label={TUNABLES.chunk_overlap.label}
            tag={TUNABLES.chunk_overlap.tag}
            help={TUNABLES.chunk_overlap.help}
            detail={TUNABLES.chunk_overlap.detail}
            value={draft.chunk_overlap}
            onChange={(next) => set("chunk_overlap", next)}
            band={SLIDER_BAND.chunk_overlap}
            bounds={API_BOUNDS.chunk_overlap}
            warning={overlapProblem}
          />

          <Segmented
            legend={TUNABLES.splitter.label}
            tag={TUNABLES.splitter.tag}
            help={TUNABLES.splitter.help}
            detail={TUNABLES.splitter.detail}
            name="settings-splitter"
            testId="settings-splitter"
            value={draft.splitter}
            // `value` is STORED and `label` is READ, and only the label moved.
            // The column, the PATCH body, the trace and EVAL.md all still say
            // `markdown` / `recursive`; "Markdown" and "Recursive" named the
            // splitter class, which says nothing about what happens to a file.
            // `buildPatch` casts `draft.splitter` to those two literals, so a
            // display label leaking into `value` would arrive as a 422 from the
            // server rather than as a compile error here.
            options={[
              { value: "markdown", label: "At headings" },
              { value: "recursive", label: "At paragraphs" },
            ]}
            onChange={(next) => set("splitter", next)}
          />
        </Section>

        <Section
          title={GROUP.inert.title}
          note={GROUP.inert.blurb}
          testId="settings-inert"
        >
          <ParamSlider
            id="settings-score-threshold"
            label={TUNABLES.score_threshold.label}
            tag={TUNABLES.score_threshold.tag}
            help={TUNABLES.score_threshold.help}
            detail={TUNABLES.score_threshold.detail}
            value={draft.score_threshold}
            onChange={(next) => set("score_threshold", next)}
            band={SLIDER_BAND.score_threshold}
            bounds={API_BOUNDS.score_threshold}
            decimals={2}
          />

          <ParamSlider
            id="settings-max-rewrites"
            label={TUNABLES.max_rewrites.label}
            tag={TUNABLES.max_rewrites.tag}
            help={TUNABLES.max_rewrites.help}
            detail={TUNABLES.max_rewrites.detail}
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
          <p className={HELP}>
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
          drawn outside the box (`outline: 2px` at `outline-offset: 2px`) and a
          control flush against the edge of a clipping box loses four pixels of
          its indicator.

          `bg-surface` is required rather than decorative: the form scrolls
          UNDER this bar, so a transparent one would show the fields sliding
          through the buttons.
        */}
        <div className="sticky -bottom-4 -mx-4 -mb-4 mt-auto flex flex-wrap items-center gap-3 border-t border-line bg-surface px-4 py-3">
          <button
            type="submit"
            data-testid="settings-save"
            disabled={dirtyCount === 0 || saving}
            className={BTN_PRIMARY}
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
            className={BTN_SECONDARY}
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
            className="text-xs text-muted"
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
    // The hairline is the grouping. Ten controls in one column say "all of
    // these do the same kind of thing", which is the exact misreading the
    // grouping exists to prevent -- so a rule separates each band, and the
    // `first:` variants keep one from being drawn above the top band (or, when
    // the error banner is present, above the banner it would collide with).
    <section
      data-testid={testId}
      className="min-w-0 space-y-3 border-t border-line pt-6 first:border-t-0 first:pt-0"
    >
      <div>
        <h3 className={EYEBROW}>{title}</h3>
        <p className={`mt-1.5 ${HELP}`}>{note}</p>
      </div>
      {children}
    </section>
  );
}
