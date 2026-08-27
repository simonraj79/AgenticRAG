/**
 * Create an agent, in four steps.
 *
 * **Why a wizard, when the old form fitted on one page.** It fitted, but only
 * in the sense that everything was present. The form led with eight persona
 * cards, which pushed the one genuinely required field -- the name -- 1113px
 * down a desktop page and just under three full screens down at 375px. The
 * field carried a bare HTML `required` and nothing else: no marker, no hint,
 * no inline message. So the first time anyone learned a name was needed was
 * the browser's own "Please fill out this field" tooltip, fired AFTER pressing
 * Create, on a control they had to scroll back up to find. Requiredness was
 * discoverable only by failing.
 *
 * Splitting the same fields across four steps fixes that by construction rather
 * than by decoration. Step 1 contains the required field and nothing else, so
 * it cannot be scrolled past, and the step rail says "1 of 4" before anything
 * is typed. Nothing was removed to achieve it -- the persona cards, the
 * parameters and the prompt are all still here, one step further in.
 *
 * **Order follows dependency, and it is the reason tuning is third.** Name is
 * free-standing. Persona sets the tuning defaults. Tuning is those defaults,
 * adjusted. Review reads all three back. Going backwards is always allowed;
 * going forwards past step 1 is not, until the name is real.
 *
 * **The parameters became editable here, and that is a new capability rather
 * than a new control.** They were previously read-only text in a `<details>`:
 * the create request carried `name`, `description` and `template_id`, and the
 * only way to set a tunable at all was a PATCH the frontend never made. So
 * "sliders instead of numbers" was not a swap -- `POST /api/agents` had to
 * learn to accept them first, which is why this ships with a backend change.
 * Doing it in one request rather than create-then-PATCH matters for a reason
 * specific to this app: there is no agent-settings UI anywhere, so an agent
 * created with the wrong parameters could not be corrected from the browser.
 *
 * **The sliders are deliberately narrower than the API's bounds.** `chunk_size`
 * accepts 64-8192, and a slider spanning that puts every value any template
 * actually uses (400, 500, 800) inside the first ninth of the track, where it
 * cannot be aimed. Each slider covers the band worth tuning in and pairs with a
 * number input that accepts the API's full range -- the slider for the common
 * case, the number for the case the slider cannot express. `SLIDER_BAND` and
 * `API_BOUNDS` are separate objects because they answer different questions:
 * what is worth dragging to, and what will the server accept. Both, and the two
 * controls that read them, now live in `components/ui.tsx` -- this is no longer
 * the only screen that edits these ten parameters, and the clamping rules on
 * `ParamSlider` are the kind of reasoning a second copy silently loses.
 *
 * **The system prompt stays read-only, alone among the advanced settings.** It
 * is the control that does the refusing (`app/db/seed.py`), the parameter grid
 * beside it is not, and an editable textarea for it belongs with a warning and
 * a diff rather than inside a create flow whose job is to get someone to a
 * working agent. It is shown in full, because a persona IS its prompt and
 * choosing one without reading it is choosing blind.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent, ReactNode, RefObject } from "react";
import { api } from "../lib/api.ts";
import type { Agent, Template } from "../lib/types.ts";
import {
  API_BOUNDS,
  CategoryBadge,
  ErrorBanner,
  Fact,
  ParamSlider,
  PersonaIcon,
  Reveal,
  SLIDER_BAND,
  Segmented,
  errorMessage,
} from "../components/ui.tsx";
import type { Tuning } from "../components/ui.tsx";
import { GROUPS, TUNABLES } from "../lib/tunables.ts";
import type { TunableGroup, TunableKey } from "../lib/tunables.ts";
import {
  ACCENT_TONE,
  BTN_PRIMARY,
  BTN_SECONDARY,
  BTN_SM,
  CARD,
  CARD_INTERACTIVE,
  EYEBROW,
  FIELD,
  FIELD_INVALID,
  FOCUS_PROXY,
  HELP,
  LABEL,
  NOTICE,
  PILL,
  WELL,
} from "../lib/styles.ts";

// --------------------------------------------------------------------------
// Shape of the tunables
// --------------------------------------------------------------------------

/**
 * Shown when no template is selectable at all -- the templates request failed,
 * or the table is empty.
 *
 * These duplicate the column defaults in `app/db/models.py`, and the duplication
 * is contained by never sending them: with "use the persona's tuning" left on,
 * the request carries no tunables and the server's own defaults apply. They are
 * a label for a number the user has not chosen, not a copy of it, so drift here
 * shows a stale figure rather than creating an agent configured from a stale
 * figure.
 */
const SERVER_DEFAULTS: Tuning = {
  chunk_size: 800,
  chunk_overlap: 120,
  splitter: "markdown",
  retrieve_k: 20,
  rerank_enabled: true,
  rerank_top_n: 3,
  score_threshold: 0.5,
  max_rewrites: 2,
  tools_enabled: true,
  max_tool_steps: 3,
};

const MAX_NAME_LENGTH = 128; // `agents.name` is String(128).

function tuningFrom(template: Template | null): Tuning {
  if (!template) return { ...SERVER_DEFAULTS };
  return {
    chunk_size: template.chunk_size,
    chunk_overlap: template.chunk_overlap,
    splitter: template.splitter,
    retrieve_k: template.retrieve_k,
    rerank_enabled: template.rerank_enabled,
    rerank_top_n: template.rerank_top_n,
    score_threshold: template.score_threshold,
    max_rewrites: template.max_rewrites,
    // Not carried on `Template`, and deliberately not added to it: the personas
    // were seeded before the tool loop existed and none of them holds an
    // opinion about it. A teaching persona is a claim about how to answer, not
    // about whether the agent may go and look something up a second time -- so
    // the agent-level default stands whichever card is chosen, and switching
    // persona never silently turns tools on or off.
    tools_enabled: SERVER_DEFAULTS.tools_enabled,
    max_tool_steps: SERVER_DEFAULTS.max_tool_steps,
  };
}

/**
 * What choosing this persona does to the settings, in one sentence.
 *
 * Read off the template row, so it cannot disagree with what the agent is
 * actually created with. The two numbers are the pair that matters and the pair
 * the labels are written to make legible together: how many passages reach the
 * answer, and how large a pool they were chosen from. `chunk_size` is third
 * because it is the one that only takes effect on upload.
 *
 * Deliberately not a rendering of all eight columns -- that already exists, on
 * the next step, grouped and explained. This is the sentence that lets someone
 * choose a card without going there.
 */
function personaSummary(template: Template): string {
  const passages = template.rerank_enabled
    ? `Answers from ${template.rerank_top_n} of ${template.retrieve_k} passages found`
    : `Answers from the top ${template.rerank_top_n} of ${template.retrieve_k} passages found`;
  return `${passages}, each about ${template.chunk_size} tokens.`;
}

function sameTuning(a: Tuning, b: Tuning): boolean {
  return (Object.keys(a) as (keyof Tuning)[]).every((key) => a[key] === b[key]);
}

// --------------------------------------------------------------------------
// Steps
// --------------------------------------------------------------------------

const STEPS = [
  { n: 1, title: "Name", blurb: "What this agent is called" },
  { n: 2, title: "Persona", blurb: "How it answers" },
  // "Settings", not "Tuning", and "how it searches and answers" rather than
  // "how it retrieves". Four of the ten controls on this step are not
  // retrieval at all, and one of them -- whether the agent may look something
  // up in the middle of writing -- changes what the agent DOES rather than
  // what it fetches. Filing that under "retrieval tuning" is the same category
  // error as the old labels: it describes the subsystem the code lives in
  // rather than the thing the user is deciding.
  { n: 3, title: "Settings", blurb: "How it searches and answers" },
  { n: 4, title: "Review", blurb: "Check and create" },
] as const;

type StepNumber = 1 | 2 | 3 | 4;

/** The step whose reset notice must be cleared on the way OUT.
 *
 *  Derived rather than written as the literal `3` at the two places that need
 *  it. The notice explains that changing persona has discarded customised
 *  tuning, and it was once wiped by the same Next click that carried the user
 *  to the only step that renders it -- a silent reset with its explanation
 *  deleted. A later reordering of STEPS would restore exactly that bug, and
 *  silently, because nothing about the literal says which step it meant. */
const SETTINGS_STEP: StepNumber = 3;

// --------------------------------------------------------------------------
// Rendering the ten settings
// --------------------------------------------------------------------------

/**
 * The ten keys in declaration order, and the members of one group.
 *
 * DERIVED from `TUNABLES` rather than written out as three lists. A hand-kept
 * list of "which settings are in the upload group" is a second statement of
 * something `tunables.ts` already says on every entry, and the copy that
 * drifts is never the one you are reading. Adding a parameter there puts it on
 * this screen automatically; putting it in the wrong group is then visible in
 * one place instead of two.
 */
const ORDERED_KEYS = Object.keys(TUNABLES) as TunableKey[];

function keysInGroup(group: TunableGroup): TunableKey[] {
  return ORDERED_KEYS.filter((key) => TUNABLES[key].group === group);
}

/**
 * A stored value as the user should read it.
 *
 * `format` is display-only and the stored value never changes: the column still
 * holds `"markdown"`, and the wire still carries `"markdown"`. What the reader
 * sees is *At headings*, because `recursive` and `markdown` describe the
 * library that does the splitting rather than what will happen to their file.
 *
 * The `?? String(value)` is not defensive padding -- `format` is optional in
 * the contract, and most parameters are plain numbers that need nothing.
 */
function displayValue(key: TunableKey, tuning: Tuning): string {
  const raw = tuning[key];
  return TUNABLES[key].format?.(raw) ?? String(raw);
}

/**
 * One editable setting, chosen by key.
 *
 * A switch rather than ten call sites, because the alternative was ten
 * near-identical blocks whose labels and help strings had already drifted from
 * the ones on the settings sheet. What varies between these parameters is the
 * CONTROL (a slider, or a two-option switch) and the guard rail; the words are
 * `tunables.ts`'s job now, and neither surface restates them.
 *
 * `splitter`, `rerank_enabled` and `tools_enabled` are the three that are not
 * numbers. Everything else is a `ParamSlider` reading its band from
 * `SLIDER_BAND` and its bounds from `API_BOUNDS` -- the split that lets
 * dragging stay inside the useful range while typing can still reach anything
 * the server accepts.
 */
function TuningControl({
  tunableKey,
  tuning,
  onEdit,
  overlapProblem,
  topNWarning,
  overlapRef,
}: {
  tunableKey: TunableKey;
  tuning: Tuning;
  onEdit: (patch: Partial<Tuning>) => void;
  overlapProblem: string | null;
  topNWarning: string | null;
  overlapRef: RefObject<HTMLInputElement | null>;
}) {
  const copy = TUNABLES[tunableKey];

  switch (tunableKey) {
    case "splitter":
      return (
        <Segmented
          testId="tuning-splitter"
          legend={copy.label}
          tag={copy.tag}
          name="tuning-splitter"
          value={tuning.splitter}
          onChange={(next) => onEdit({ splitter: next })}
          // `value` is what is stored, `label` is what is read. They differ
          // here on purpose and the stored side must not follow the label.
          options={[
            { value: "markdown", label: "At headings" },
            { value: "recursive", label: "At paragraphs" },
          ]}
          help={copy.help}
          detail={copy.detail}
        />
      );

    case "rerank_enabled":
      return (
        <Segmented
          testId="tuning-rerank"
          legend={copy.label}
          tag={copy.tag}
          name="tuning-rerank"
          value={tuning.rerank_enabled ? "on" : "off"}
          onChange={(next) => onEdit({ rerank_enabled: next === "on" })}
          options={[
            { value: "on", label: "On" },
            { value: "off", label: "Off" },
          ]}
          help={copy.help}
          detail={copy.detail}
        />
      );

    case "tools_enabled":
      return (
        <Segmented
          testId="tuning-tools"
          legend={copy.label}
          tag={copy.tag}
          name="tuning-tools"
          value={tuning.tools_enabled ? "on" : "off"}
          onChange={(next) => onEdit({ tools_enabled: next === "on" })}
          options={[
            { value: "on", label: "On" },
            { value: "off", label: "Off" },
          ]}
          help={copy.help}
          detail={copy.detail}
        />
      );

    case "score_threshold":
      return (
        <ParamSlider
          id="score-threshold"
          label={copy.label}
          tag={copy.tag}
          value={tuning.score_threshold}
          onChange={(next) =>
            onEdit({
              // Two decimals: 0.01 steps accumulate float error and the server
              // takes a float, so 0.6100000000000001 would be stored and then
              // shown.
              score_threshold: Math.round(next * 100) / 100,
            })
          }
          band={SLIDER_BAND.score_threshold}
          bounds={API_BOUNDS.score_threshold}
          decimals={2}
          help={copy.help}
          detail={copy.detail}
        />
      );

    case "chunk_overlap":
      return (
        <ParamSlider
          id="chunk-overlap"
          label={copy.label}
          tag={copy.tag}
          value={tuning.chunk_overlap}
          onChange={(next) => onEdit({ chunk_overlap: next })}
          band={SLIDER_BAND.chunk_overlap}
          bounds={API_BOUNDS.chunk_overlap}
          // The one server rule this form duplicates, and the only one. A
          // blocked Next sends focus here.
          warning={overlapProblem}
          numberRef={overlapRef}
          help={copy.help}
          detail={copy.detail}
        />
      );

    case "rerank_top_n":
      return (
        <ParamSlider
          id="rerank-top-n"
          label={copy.label}
          tag={copy.tag}
          value={tuning.rerank_top_n}
          onChange={(next) => onEdit({ rerank_top_n: next })}
          band={SLIDER_BAND.rerank_top_n}
          bounds={API_BOUNDS.rerank_top_n}
          disabled={!tuning.rerank_enabled}
          warning={topNWarning}
          help={copy.help}
          detail={copy.detail}
        />
      );

    case "max_tool_steps":
      return (
        <ParamSlider
          id="max-tool-steps"
          label={copy.label}
          tag={copy.tag}
          value={tuning.max_tool_steps}
          onChange={(next) => onEdit({ max_tool_steps: next })}
          band={SLIDER_BAND.max_tool_steps}
          bounds={API_BOUNDS.max_tool_steps}
          disabled={!tuning.tools_enabled}
          help={copy.help}
          detail={copy.detail}
        />
      );

    // The three remaining plain numbers. Grouped rather than repeated, because
    // the only thing that differs is which band and which key.
    case "chunk_size":
    case "retrieve_k":
    case "max_rewrites": {
      const ids = {
        chunk_size: "chunk-size",
        retrieve_k: "retrieve-k",
        max_rewrites: "max-rewrites",
      } as const;
      return (
        <ParamSlider
          id={ids[tunableKey]}
          label={copy.label}
          tag={copy.tag}
          value={tuning[tunableKey]}
          onChange={(next) => onEdit({ [tunableKey]: next } as Partial<Tuning>)}
          band={SLIDER_BAND[tunableKey]}
          bounds={API_BOUNDS[tunableKey]}
          help={copy.help}
          detail={copy.detail}
        />
      );
    }
  }
}

/**
 * The progress rail.
 *
 * A completed step is a button and an upcoming one is not -- going back to
 * change an answer is normal, skipping ahead over an unanswered required field
 * is the thing the whole component exists to prevent. `aria-current="step"`
 * marks the position, and the "Step N of 4" line above it is the same
 * information for a reader who cannot see that the second circle is green.
 *
 * Labels are hidden below `sm` for every step except the current one: four
 * words plus four circles do not fit 375px, and the current step's label is the
 * only one that answers "where am I".
 */
function StepRail({
  current,
  furthest,
  onJump,
}: {
  current: StepNumber;
  furthest: StepNumber;
  onJump: (step: StepNumber) => void;
}) {
  return (
    // `sticky top-0` inside the drawer's scrolling body, with an opaque
    // background and the panel's own horizontal padding restored by
    // `-mx-4 px-4`. This is the direct answer to "difficult to view the entire
    // process": the rail is the only thing that says where you are and the
    // only way back to an earlier step, and it used to be the FIRST thing to
    // scroll away -- on a step measured at 2.5 screens.
    //
    // Sticky rather than the drawer's `subheader` region, which would have
    // meant lifting `step`, `furthest` and `goTo` out of this component and
    // into the dashboard, so that a generic layout primitive could own a
    // wizard's navigation state. The rail reads that state and nothing else
    // does; keeping the two together is worth more than the region.
    <nav
      aria-label="Progress"
      data-testid="wizard-rail"
      className="sticky top-0 z-10 -mx-4 bg-surface px-4 pb-3"
    >
      <p className={EYEBROW}>
        Step {current} of {STEPS.length} · {STEPS[current - 1].blurb}
      </p>

      {/* `flex-wrap` with `gap-y-2`: the rail's intrinsic width computes to
          roughly 492px, which fitted the old 511px box within 1%. It is not a
          layout that should be one word away from overflowing, and a wrapped
          rail is legible where a clipped one is not. */}
      <ol className="mt-3 flex flex-wrap items-center gap-x-1.5 gap-y-2 @md:gap-x-2">
        {STEPS.map((step, index) => {
          const done = step.n < current;
          const active = step.n === current;
          const reachable = step.n <= furthest;

          // The rail reads as PROGRESS rather than as three decorated states:
          // a step behind you is filled in, the one you are on is outlined in
          // the same colour, and one you have not reached is a plain hairline.
          // Ink-filled would have been the alternative and is wrong here --
          // filled ink is the primary ACTION in this palette (Next, Create),
          // and the rail is a position indicator, not a button.
          const circle = (
            <span
              aria-hidden="true"
              className={`inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-full border text-xs font-semibold transition ${
                done
                  ? "border-accent bg-accent text-inverse"
                  : active
                    ? "border-accent bg-surface text-accent"
                    : "border-line bg-surface text-faint"
              }`}
            >
              {done ? "✓" : step.n}
            </span>
          );

          const label = (
            <span
              className={`text-xs font-medium whitespace-nowrap ${
                active ? "text-ink" : done ? "text-muted" : "text-faint"
              } ${active ? "" : "hidden @md:inline"}`}
            >
              {step.title}
            </span>
          );

          return (
            <li key={step.n} className="flex min-w-0 items-center gap-1.5 @md:gap-2">
              {reachable && !active ? (
                <button
                  type="button"
                  data-testid={`wizard-step-${step.n}`}
                  onClick={() => onJump(step.n)}
                  // Direction is computed, not assumed. Once step 4 has been
                  // reached the rail is the FORWARD navigation as well, and a
                  // fixed "Back to step 4: Review" announced a jump from step 1
                  // as its own opposite.
                  aria-label={`${step.n < current ? "Back to" : "Forward to"} step ${step.n}: ${step.title}`}
                  // `min-h-11` for the same reason every other control in this
                  // app carries it. The rail is the primary navigation of the
                  // flow that was rebuilt for mobile, and it was the smallest
                  // hit area on the screen at 36x36.
                  // `hover:bg-sunken`, the same hover every other quiet control
                  // in the app uses, rather than the `hover:opacity-80` this
                  // carried: fading a control is the disabled affordance in
                  // this design, so using it for hover said the opposite of
                  // what was meant.
                  className="flex min-h-11 min-w-11 items-center justify-center gap-2 rounded-md px-1 transition hover:bg-sunken"
                >
                  {circle}
                  {label}
                </button>
              ) : (
                <span
                  data-testid={`wizard-step-${step.n}`}
                  aria-current={active ? "step" : undefined}
                  className="flex min-h-11 min-w-11 items-center justify-center gap-2 px-1"
                >
                  {circle}
                  {label}
                </span>
              )}

              {index < STEPS.length - 1 && (
                <span
                  aria-hidden="true"
                  className={`h-px w-3 shrink-0 @md:w-8 ${done ? "bg-accent" : "bg-line"}`}
                />
              )}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}

// --------------------------------------------------------------------------
// Controls
// --------------------------------------------------------------------------

// `INPUT_CLASS` was defined here, and the two other files that used the
// identical string had copy-pasted it rather than imported it -- which is the
// specific finding `lib/styles.ts` exists to answer. It is now `FIELD`, in one
// place, and the invalid state that used to be spelled out inline at the one
// call site that needed it is `FIELD_INVALID` beside it.

// `ParamSlider` and `Segmented` were defined here and are now imported from
// `components/ui.tsx`, unchanged. They moved because the agent-settings sheet
// edits the same ten parameters and would otherwise have held a second copy --
// and the thing that would drift is not the markup but the comment on
// `ParamSlider` explaining why its lower bound clamps on blur and its upper
// bound on every keystroke.

// --------------------------------------------------------------------------
// The wizard
// --------------------------------------------------------------------------

export default function CreateAgentWizard({
  templates,
  existingNames,
  onCreated,
  initialNameRef,
}: {
  templates: Template[];
  /** Names the user already owns. `agents` is unique on (owner, name), so a
   *  collision is a 409 -- catching it on step 1 turns a failure at the end of
   *  a four-step flow into a hint before the flow starts. */
  existingNames: string[];
  onCreated: (agent: Agent) => void | Promise<void>;
  /** Shared with the modal drawer so the required field is focused on open. */
  initialNameRef?: RefObject<HTMLInputElement | null>;
}) {
  const [step, setStep] = useState<StepNumber>(1);
  const [furthest, setFurthest] = useState<StepNumber>(1);

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [nameTouched, setNameTouched] = useState(false);

  const [templateId, setTemplateId] = useState("");

  const [customizing, setCustomizing] = useState(false);
  const [tuning, setTuning] = useState<Tuning>(SERVER_DEFAULTS);
  const [resetNotice, setResetNotice] = useState<string | null>(null);
  // Whether the user has MOVED anything, which is not the same question as
  // whether `tuning` differs from `baseline` and cannot be derived from it. A
  // reset that lands on values the user never chose still differs from the next
  // persona's, so comparing values made "you have lost your customisations"
  // fire at somebody who had none. Only an actual edit sets this.
  const [tuningTouched, setTuningTouched] = useState(false);

  const [busy, setBusy] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  const ownNameRef = useRef<HTMLInputElement>(null);
  const nameRef = initialNameRef ?? ownNameRef;
  const headingRef = useRef<HTMLHeadingElement>(null);
  const overlapRef = useRef<HTMLInputElement>(null);

  const selected = templates.find((template) => template.id === templateId) ?? null;
  const baseline = useMemo(() => tuningFrom(selected), [selected]);

  // Default to the first template once they arrive. The server orders them and
  // its first entry is the general-purpose one -- the safe default for someone
  // who has not read any of the cards yet.
  useEffect(() => {
    if (templates.length > 0) {
      setTemplateId((current) => (current === "" ? templates[0].id : current));
    }
  }, [templates]);

  // The values the persona effect below needs to READ without being re-run
  // whenever they change. Tracking `tuning` as a dependency would restart the
  // effect on every slider drag; tracking `customizing` would fire a spurious
  // reset notice the instant the toggle flips. Written in an effect of their
  // own, declared FIRST so it commits before the one that reads it -- on the
  // render where the persona changed, that leaves `latest.current` holding the
  // tuning as it was BEFORE the change, which is exactly what has to be
  // compared against the new baseline.
  const latest = useRef({ tuningTouched, customizing });
  useEffect(() => {
    latest.current = { tuningTouched, customizing };
  });

  /** Every write to `tuning` the USER makes goes through here, so that "they
   *  moved something" is recorded at the one place it is true. The persona-sync
   *  effect writes `tuning` directly and deliberately does not come this way. */
  function editTuning(patch: Partial<Tuning>) {
    setTuning((current) => ({ ...current, ...patch }));
    setTuningTouched(true);
  }

  // Follow the persona while the user has not overridden anything. Once they
  // have, changing persona still wins -- it is the more specific choice, and
  // silently keeping a Socratic tutor's k on a quiz generator would be a
  // configuration nobody picked -- but it says so rather than doing it quietly.
  //
  // Both state updates are top-level in the effect. `setResetNotice` used to sit
  // INSIDE the `setTuning` updater, which reads naturally and is not allowed to:
  // an updater must be pure, StrictMode deliberately double-invokes it to expose
  // exactly that, and React is free to call it again at a time of its choosing.
  // It survived only because setting the same string twice is a no-op.
  //
  // The notice is gated on `tuningTouched`, NOT on the values differing. Those
  // come apart immediately after any reset: the tuning now holds persona A's
  // numbers, which of course differ from persona B's, so a value comparison
  // announced a loss to a user who had customised nothing and was simply
  // browsing personas with the toggle left on.
  useEffect(() => {
    const previous = latest.current;
    setTuning(baseline);
    setTuningTouched(false);
    if (previous.customizing && previous.tuningTouched) {
      // The persona named is `selected` from this render's closure -- the one
      // just chosen, which is what the sentence is about. The ref holds the
      // PREVIOUS render's flags, and using it here would credit the reset to the
      // persona the user just navigated away from.
      setResetNotice(
        selected
          ? `Tuning reset to ${selected.name}'s values.`
          : "Tuning reset to the server defaults.",
      );
    }
    setTuning(baseline);
    // `selected` is read but not tracked: `baseline` is a useMemo over exactly
    // it, so the two change together and listing both only invites a re-run on
    // an identity change that `sameTuning` already returns early on.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [baseline]);

  // Move focus into the new step on every transition. Without it a keyboard or
  // screen-reader user presses Next and the focus ring stays on a button whose
  // label just changed under them, with no announcement that the page content
  // was replaced.
  //
  // Exactly ONE element is focused per step, and on step 1 it is the name field
  // rather than the heading. Focusing the heading and then the input fired a
  // blur between the two, which under StrictMode's double-invoked effects
  // marked the name "touched" on first paint -- so the form opened already
  // showing "Give the agent a name", scolding the user before they had done
  // anything. Whether the input has been visited is a fact about the user, and
  // only a real blur is allowed to assert it.
  useEffect(() => {
    if (step === 1) nameRef.current?.focus();
    else headingRef.current?.focus();
  }, [step]);

  const trimmedName = name.trim();
  // Compared EXACTLY, because `UniqueConstraint("owner_user_id", "name")` is a
  // plain Postgres unique index and Postgres is case-sensitive: "Case Probe" and
  // "case probe" are two rows the server accepts side by side (verified). A
  // case-insensitive check here does not pre-empt a 409, it invents one -- and
  // because the same predicate gates both `advance` and `submit`, it left the
  // user with no way forward at all, told they already own an agent that does
  // not exist. A guard that mirrors a server constraint has to mirror its
  // collation too, or it is a different rule wearing the same words.
  const duplicate = existingNames.some((existing) => existing === trimmedName);

  const nameProblem: string | null = !trimmedName
    ? "Give the agent a name so you can find it later."
    : trimmedName.length > MAX_NAME_LENGTH
      ? `Names are at most ${MAX_NAME_LENGTH} characters; this one is ${trimmedName.length}.`
      : duplicate
        ? `You already have an agent named "${trimmedName}".`
        : null;

  // The server rejects equality too, and it 422s on the merged config, so this
  // is the same rule stated one step earlier rather than a stricter one.
  //
  // Both labels are INTERPOLATED from `tunables.ts` rather than written out.
  // This string named "Overlap" and "chunk size" after the relabel, so it was
  // the last place in the frontend still using the old vocabulary -- and it is
  // the worst possible place for it, because a message telling you which
  // control to go and fix has to name a control you can actually see. Reading
  // the labels means it cannot drift from them again.
  const overlapProblem =
    tuning.chunk_overlap >= tuning.chunk_size
      ? `${TUNABLES.chunk_overlap.label} (${tuning.chunk_overlap}) must be smaller than ${TUNABLES.chunk_size.label.toLowerCase()} (${tuning.chunk_size}).`
      : null;

  // Not an error: the server accepts it, and the reranker simply gets fewer
  // candidates than it was asked to return. Worth saying, not worth blocking.
  const topNWarning =
    tuning.rerank_enabled && tuning.rerank_top_n > tuning.retrieve_k
      ? `Only ${tuning.retrieve_k} passages are shortlisted, so the re-ranker cannot hand over ${tuning.rerank_top_n}.`
      : null;

  function problemFor(target: StepNumber): string | null {
    if (target === 1) return nameProblem;
    if (target === 3) return customizing ? overlapProblem : null;
    return null;
  }

  function goTo(target: StepNumber) {
    // Cleared on the way OUT of the tuning step, never on the way in. Clearing
    // it unconditionally meant it could never be read: changing persona sets the
    // notice while step 2 is on screen, and the Next that carries the user to
    // step 3 -- the only step that renders it -- was wiping it in the same
    // click. The visible result was a silent reset, which is the exact thing the
    // notice exists to prevent.
    if (step === SETTINGS_STEP && target !== SETTINGS_STEP) setResetNotice(null);
    // A failed create describes the attempt, not the form. Leaving step 4 with
    // it still set means it is waiting, unchanged, when the user comes back
    // having fixed the very thing it complains about.
    if (step === 4 && target !== 4) setSubmitError(null);
    setStep(target);
    setFurthest((current) => (target > current ? target : current));
  }

  function advance() {
    const problem = problemFor(step);
    if (problem) {
      // Every blocked step moves focus to the control that is blocking it. Step
      // 3 used to fall through and simply `return`, so pressing Next with a bad
      // overlap changed nothing whatsoever: the warning was already on screen
      // (it renders off the value, not off an attempt), the step did not
      // change, and focus stayed where it was. For anyone not watching the step
      // number, the button was indistinguishable from broken.
      if (step === 1) {
        setNameTouched(true);
        nameRef.current?.focus();
      } else if (step === 3) {
        overlapRef.current?.focus();
      }
      return;
    }
    if (step < 4) goTo((step + 1) as StepNumber);
  }

  async function submit(event: FormEvent) {
    event.preventDefault();

    // Enter anywhere in the form submits it, which is the behaviour worth
    // keeping -- it just has to mean "next" until the last step.
    if (step < 4) {
      advance();
      return;
    }

    // Re-checked rather than trusted from a button's disabled state: once step 4
    // has been reached the rail turns every earlier step into a button, so
    // "back to 1, clear the name, forward to 4, press Create" arrives here
    // without `advance` having run once.
    //
    // The problem is NOT written to `submitError`, and that is the fix for a
    // silent rejection rather than a style preference. `submitError` renders on
    // step 4 only, so setting it and navigating away in the same handler
    // produced a bounce to step 1 with nothing on screen to say why -- and left
    // the message primed to reappear, by then stale, the next time step 4 was
    // opened. Each destination states its own problem: step 1 through the
    // name field's inline error, step 3 through the footer message that is
    // already on screen whenever the overlap is wrong.
    const problem = nameProblem ?? (customizing ? overlapProblem : null);
    if (problem) {
      setSubmitError(null);
      if (nameProblem) {
        // Forces the inline error into view. `nameTouched` gates it so an
        // untouched field is not scolded on first paint, and pressing Create is
        // as clear an assertion that the user is done with it as a blur.
        setNameTouched(true);
        goTo(1);
      } else {
        goTo(3);
      }
      return;
    }

    setBusy(true);
    setSubmitError(null);
    try {
      const agent = await api<Agent>("/api/agents", {
        method: "POST",
        json: {
          name: trimmedName,
          description: description.trim() || null,
          // Omitted rather than sent as "" -- the column is a nullable FK and an
          // empty string is not a valid UUID, so it would 422 instead of
          // meaning "server defaults".
          template_id: templateId || null,
          // Sent only when the user actually moved something. Left off, the
          // server copies the template's values and stays the one authority on
          // what an untouched agent is configured with.
          ...(customizing ? tuning : {}),
        },
      });
      await onCreated(agent);
    } catch (cause) {
      // Stays on step 4 rather than routing by what the message says. A 409
      // (duplicate name, pre-empted on step 1 but still reachable from another
      // tab or a name created since this page loaded) would argue for jumping
      // to step 1, and deciding that by substring-matching the server's prose
      // breaks the first time the wording changes -- silently, into a bounce to
      // the wrong step or none at all. The error belongs on the screen where the
      // attempt was made, next to the Edit button that goes back to the name.
      setSubmitError(errorMessage(cause));
    } finally {
      setBusy(false);
    }
  }

  // Keep the untouched empty field calm on first paint, but do not hide a
  // duplicate or overlong value behind a blur the now-disabled Next button can
  // never cause. Once somebody has typed a value, explain immediately why the
  // flow cannot advance.
  const showNameProblem = Boolean(nameProblem && (nameTouched || trimmedName.length > 0));

  return (
    <form
      onSubmit={submit}
      // `noValidate` suppresses the browser's validation bubble, and the
      // `required` attribute stays on the input. The two are not in tension:
      // `required` is the semantic that reaches the accessibility tree, while
      // the bubble is the interaction, and the bubble is the specific thing this
      // wizard replaces. Left on, it fires "Please fill out this field" in a
      // tooltip positioned over the inline message that says the same thing in
      // this app's own words -- and, because native validation aborts the submit
      // event, it also prevents `advance()` from ever running, so the step's own
      // error handling would be dead code that appears to work.
      noValidate
      data-testid="create-agent-wizard"
      data-step={step}
      className="flex min-h-full flex-col"
    >
      <StepRail current={step} furthest={furthest} onJump={goTo} />

      <div className="mt-5 flex-1 border-t border-line pt-5">
        {/* -------------------------------------------------- Step 1: Name */}
        {step === 1 && (
          <section aria-labelledby="wizard-heading">
            <h2
              id="wizard-heading"
              ref={headingRef}
              tabIndex={-1}
              className="text-lg font-semibold tracking-tight text-ink outline-none"
            >
              Name this agent
            </h2>
            <p className="mt-1.5 max-w-prose text-sm text-muted">
              One agent is one corpus, one persona and one isolated vector namespace. The
              name is how you will tell this one apart from the others on the dashboard.
            </p>

            <div className="mt-6 max-w-prose">
              <div className="flex items-baseline justify-between gap-3">
                <label className={LABEL} htmlFor="agent-name">
                  Name{" "}
                  {/* The accent, on the one field the whole flow is gated by.
                      It is the same colour the citation markers use, and that
                      is deliberate rather than a reuse of convenience: both say
                      "this is the load-bearing thing on the page". */}
                  <span className={`${PILL} ${ACCENT_TONE} tracking-[0.04em] uppercase`}>
                    Required
                  </span>
                </label>
                <span
                  className={`font-mono text-xs tabular-nums ${
                    trimmedName.length > MAX_NAME_LENGTH ? "text-bad" : "text-muted"
                  }`}
                >
                  {trimmedName.length}/{MAX_NAME_LENGTH}
                </span>
              </div>

              <input
                id="agent-name"
                data-testid="agent-name-input"
                ref={nameRef}
                required
                value={name}
                onChange={(event) => setName(event.target.value)}
                onBlur={() => setNameTouched(true)}
                aria-required="true"
                aria-invalid={showNameProblem ? true : undefined}
                aria-describedby={showNameProblem ? "agent-name-error" : "agent-name-hint"}
                placeholder="Topic 10 Lecture"
                className={`${FIELD} mt-2 ${showNameProblem ? FIELD_INVALID : ""}`}
              />

              {showNameProblem ? (
                <p
                  id="agent-name-error"
                  role="alert"
                  data-testid="agent-name-error"
                  className="mt-2 text-xs text-bad"
                >
                  {nameProblem}
                </p>
              ) : (
                <p id="agent-name-hint" className={`mt-2 ${HELP}`}>
                  Required. Name it after the material it will answer from -- a topic, a
                  module, a handbook.
                </p>
              )}

              <div className="mt-5">
                <label className={LABEL} htmlFor="agent-description">
                  Description <span className="font-normal text-muted">(optional)</span>
                </label>
                <input
                  id="agent-description"
                  data-testid="agent-description-input"
                  value={description}
                  onChange={(event) => setDescription(event.target.value)}
                  placeholder="What this agent knows about"
                  className={`${FIELD} mt-2`}
                />
                <p className={`mt-2 ${HELP}`}>
                  Shown under the name on the dashboard card.
                </p>
              </div>
            </div>
          </section>
        )}

        {/* ----------------------------------------------- Step 2: Persona */}
        {step === 2 && (
          <section aria-labelledby="wizard-heading">
            <h2
              id="wizard-heading"
              ref={headingRef}
              tabIndex={-1}
              className="text-lg font-semibold tracking-tight text-ink outline-none"
            >
              Choose a teaching persona
            </h2>
            <p className="mt-1.5 max-w-prose text-sm text-muted">
              The persona decides how the agent answers -- what it asks back, what it
              withholds, how it refuses. It never changes what the agent may answer{" "}
              <em>from</em>: every persona is bound to this agent&rsquo;s documents alone.
            </p>

            {templates.length === 0 ? (
              // The one string a user only ever sees when something has already
              // gone wrong, and it was the one place the internal word
              // "templates" leaked into the product. It now says what happened
              // and what will happen instead, in the words the rest of the flow
              // uses.
              <p className="mt-6 max-w-prose text-sm text-muted">
                The personas could not be loaded just now. You can still create the
                agent &mdash; it will use Groundwork&rsquo;s standard settings, and you can
                choose a persona later in its settings.
              </p>
            ) : (
              <div
                // Keyed to the CONTAINER, not the window. `lg:grid-cols-3`
                // asked "is the viewport at least 1024px" -- got yes on any
                // desktop -- and laid three cards into a 511px panel at 159px
                // each, with six of nine descriptions clamped and the category
                // badge pushed outside the card border. The panel is what
                // decides how many cards fit, and now it is the panel that is
                // asked. Tailwind v4's container scale is its own set of
                // numbers, so these are not the `sm`/`lg` names renamed:
                // @2xl is 42rem and @4xl is 56rem, chosen so no card track
                // falls below 260px at any panel width.
                className="mt-6 grid gap-3 @2xl:grid-cols-2 @4xl:grid-cols-3"
                // The only radio group in the app that had no group semantics,
                // while `Segmented` -- a two-option control of far less
                // consequence -- gets it right. Without it these are nine
                // unrelated radios rather than one choice with nine options,
                // so nothing announces "1 of 9" and nothing carries the
                // question being asked.
                role="radiogroup"
                aria-labelledby="wizard-heading"
              >
                {templates.map((template) => {
                  const active = template.id === templateId;
                  return (
                    <label
                      key={template.id}
                      data-testid="template-card"
                      data-template-slug={template.slug}
                      data-selected={active}
                      // Selection is an accent border and an accent-soft fill --
                      // the same pair `ROW_ACTIVE` uses for a selected
                      // conversation, so "this one is chosen" looks the same
                      // wherever it is said. No `focus-within:ring`: the real
                      // radio inside is focusable and the global
                      // `:focus-visible` rule in `index.css` draws its ring with
                      // `!important`, so a second ring here would only be a
                      // second thing to keep in step.
                      // `FOCUS_PROXY` is not decoration and the comment that
                      // used to sit here was wrong. The real radio below is
                      // `sr-only` -- absolutely positioned, 1px,
                      // `clip: rect(0,0,0,0)` -- so the global
                      // `:focus-visible` outline in `index.css` was being
                      // painted on a clipped 1px box and was invisible. A
                      // keyboard user had no way to tell which of nine personas
                      // they were on. `has-[:focus-visible]` moves the ring
                      // onto the element you can actually see, using the same
                      // token and offset as the global rule so a proxied ring
                      // and a real one are the same object to the eye.
                      className={`${CARD_INTERACTIVE} ${FOCUS_PROXY} flex cursor-pointer flex-col p-4 ${
                        active ? "border-accent bg-accent-soft" : ""
                      }`}
                    >
                      {/*
                        A real radio, visually hidden rather than replaced.
                        Arrow-key navigation within the group, the checked
                        semantics and the label-click target all come free from
                        the native control.
                      */}
                      <input
                        type="radio"
                        name="agent-template"
                        className="sr-only"
                        value={template.id}
                        checked={active}
                        onChange={() => setTemplateId(template.id)}
                        // Without this the accessible name of each radio is
                        // everything the <label> wraps: the name, the category
                        // badge, the role, the description AND the whole
                        // pedagogy paragraph. Nine radios each announcing a
                        // paragraph is not a choice a screen-reader user can
                        // move through. The description is still reachable --
                        // it is text inside the group -- it is just no longer
                        // the option's name.
                        aria-label={template.name}
                      />

                      <div className="flex items-start gap-3">
                        <PersonaIcon icon={template.icon} fallback={template.name} size="sm" />
                        <div className="min-w-0 flex-1">
                          <div className="flex items-start justify-between gap-2">
                            <span className="text-sm font-semibold text-ink">
                              {template.name}
                            </span>
                            <CategoryBadge category={template.category} />
                          </div>
                          {template.persona_role && (
                            <span className={`mt-1 block ${EYEBROW}`}>
                              {template.persona_role}
                            </span>
                          )}
                        </div>
                      </div>

                      {template.description && (
                        <p className="mt-3 text-sm text-muted">{template.description}</p>
                      )}

                      {/* Derived from this template's own row, never a second
                          table. It is the answer to "what does choosing this
                          persona actually DO to the settings" -- which the
                          wizard previously answered one step later, in a
                          four-column grid of column names. Derived means it
                          cannot drift from what the agent is created with. */}
                      <p
                        data-testid="template-summary"
                        className={`mt-3 ${HELP}`}
                      >
                        {personaSummary(template)}
                      </p>

                      {template.pedagogy && (
                        <p
                          className={`mt-3 border-t border-line pt-3 ${HELP} ${
                            active ? "" : "line-clamp-3"
                          }`}
                        >
                          <span className="font-medium text-ink">Teaching approach: </span>
                          {template.pedagogy}
                        </p>
                      )}
                    </label>
                  );
                })}
              </div>
            )}
          </section>
        )}

        {/* ---------------------------------------------- Step 3: Settings */}
        {step === 3 && (
          <section aria-labelledby="wizard-heading">
            <h2
              id="wizard-heading"
              ref={headingRef}
              tabIndex={-1}
              className="text-lg font-semibold tracking-tight text-ink outline-none"
            >
              Search and answer settings
            </h2>
            <p className="mt-1.5 max-w-prose text-sm text-muted">
              These values are <em>copied</em> onto the agent when it is created. Editing
              the persona later will not re-tune an agent you already built, and neither
              will editing these afterwards re-split documents you have already uploaded.
            </p>
            {/* The single most useful sentence on the step, and it was missing.
                The reason to open Customize is usually not that the defaults
                are wrong -- it is the fear of being locked into them. Saying
                plainly that nothing here is permanent removes the pressure to
                get it right now, which is the whole job of a creation flow. */}
            <p className={`mt-2 ${HELP}`}>
              Every setting here can be changed later, in the agent&rsquo;s own settings.
            </p>

            <div className="mt-5">
              <Segmented
                testId="tuning-mode"
                // "Parameters" named the data structure. This names the
                // decision -- and the options name the two things the user is
                // actually choosing between, rather than one of them being a
                // persona name that looks like it belongs on the previous step.
                legend="Settings for this agent"
                name="tuning-mode"
                value={customizing ? "custom" : "template"}
                onChange={(next) => {
                  setResetNotice(null);
                  const custom = next === "custom";
                  setCustomizing(custom);
                  // Switching OFF discards the custom values rather than
                  // parking them. Everything on steps 3 and 4 renders `tuning`,
                  // and the request sends the persona's values in this mode, so
                  // leaving edits behind meant the review step displayed a
                  // configuration the agent was not going to be created with --
                  // under the caption "Unchanged from the persona". Measured:
                  // chunk_size dragged to 2048, toggled off, reviewed as 2048,
                  // created as 500. A confirmation step that renders
                  // confidently and wrongly is worse than none.
                  if (!custom) {
                    setTuning(baseline);
                    setTuningTouched(false);
                  }
                }}
                options={[
                  {
                    value: "template",
                    label: selected
                      ? `Use ${selected.name}'s settings`
                      : "Use the standard settings",
                  },
                  { value: "custom", label: "Set them myself" },
                ]}
                help={
                  customizing
                    ? "Your values are used instead of the persona's. The persona's own instructions are unaffected."
                    : selected
                      ? `${selected.name} comes with the settings below. This is the recommended choice.`
                      : "The agent is created with Groundwork's standard settings, shown below."
                }
              />
            </div>

            {resetNotice && (
              <p
                role="status"
                data-testid="tuning-reset-notice"
                // `ACCENT_TONE`, not `WARN_TONE`. This reports something that
                // already happened and is recoverable by going back one step --
                // it is information, and dressing it as caution would spend the
                // warning tone on a non-problem and blunt it where it is real.
                className={`${NOTICE} ${ACCENT_TONE} mt-4`}
              >
                {resetNotice}
              </p>
            )}

            {/*
              Grouped by WHEN each setting takes effect, in both modes, using
              the same three headings the agent-settings sheet already uses.
              Ten controls in one flat column say that all ten are the same kind
              of thing, and they are not: three of them do nothing until you
              upload a document, two of them do nothing at all, and the rest are
              read on the next question. That distinction is the most useful
              thing anyone learns on this step, and the old layout hid it.
            */}
            <div data-testid="tuning-groups" className="mt-6 space-y-6">
              {GROUPS.map((group) => {
                const keys = keysInGroup(group.id);
                return (
                  <section
                    key={group.id}
                    data-testid={`tuning-group-${group.id}`}
                    className={`${WELL} p-4`}
                  >
                    <h3 className="text-sm font-semibold text-ink">{group.title}</h3>
                    <p className={`mt-1 max-w-prose ${HELP}`}>{group.blurb}</p>

                    {!customizing ? (
                      <dl
                        data-testid={`tuning-facts-${group.id}`}
                        // `auto-fit` with a real minimum, not a fixed column
                        // count. Every Fact now carries a sentence of
                        // explanation as well as a value, so a four-column
                        // grid at 101px -- which is what `sm:grid-cols-4`
                        // resolved to inside the old panel -- has nowhere to
                        // put it. The track decides how many fit; the box
                        // decides the track.
                        className="mt-4 grid grid-cols-[repeat(auto-fit,minmax(min(14rem,100%),1fr))] gap-x-6 gap-y-4"
                      >
                        {keys.map((key) => (
                          <Fact
                            key={key}
                            label={TUNABLES[key].label}
                            tag={TUNABLES[key].tag}
                            value={displayValue(key, tuning)}
                            raw={tuning[key]}
                            help={TUNABLES[key].help}
                          />
                        ))}
                      </dl>
                    ) : (
                      <div
                        data-testid={`tuning-controls-${group.id}`}
                        // Same `auto-fit` reasoning as the facts above, with a
                        // wider floor: a slider needs room for its track, its
                        // number field and a sentence, and 20rem is where that
                        // stops being cramped. It also means the controls pair
                        // up when the panel is wide and stack when it is not,
                        // WITHOUT a breakpoint -- which is the bug this whole
                        // change set is about. `sm:grid-cols-2` asked how wide
                        // the WINDOW was and got the wrong answer inside a
                        // 511px box.
                        className="mt-4 grid grid-cols-[repeat(auto-fit,minmax(min(20rem,100%),1fr))] gap-x-6 gap-y-6"
                      >
                        {keys.map((key) => (
                          <TuningControl
                            key={key}
                            tunableKey={key}
                            tuning={tuning}
                            onEdit={editTuning}
                            overlapProblem={overlapProblem}
                            topNWarning={topNWarning}
                            overlapRef={overlapRef}
                          />
                        ))}
                      </div>
                    )}
                  </section>
                );
              })}
            </div>

            {selected?.system_prompt && (
              <div className="mt-6">
                <Reveal summary="The persona's instructions (read-only)" testId="template-prompt">
                  <p className={`mb-3 max-w-prose ${HELP}`}>
                    Copied onto the agent as written. This is the control that makes the
                    agent say &ldquo;the material does not cover that&rdquo; rather than
                    guess, so it is shown here in full. To change it, choose a different
                    persona.
                  </p>
                  {/* A prompt is machine text, so mono on a well -- the same
                      treatment the settings sheet gives the editable copy of
                      the same string. */}
                  <pre
                    className={`${WELL} max-h-64 overflow-y-auto p-3 font-mono text-xs leading-relaxed whitespace-pre-wrap text-ink`}
                  >
                    {selected.system_prompt}
                  </pre>
                </Reveal>
              </div>
            )}
          </section>
        )}

        {/* ------------------------------------------------ Step 4: Review */}
        {step === 4 && (
          <section aria-labelledby="wizard-heading">
            <h2
              id="wizard-heading"
              ref={headingRef}
              tabIndex={-1}
              className="text-lg font-semibold tracking-tight text-ink outline-none"
            >
              Review and create
            </h2>
            <p className="mt-1.5 max-w-prose text-sm text-muted">
              Nothing is created until you press the button. The next screen asks for the
              documents this agent answers from -- it has none until you upload them.
            </p>

            <div className="mt-6 space-y-3">
              <ReviewRow step={1} label="Name" onEdit={goTo}>
                <p className="text-sm font-semibold text-ink">{trimmedName || "—"}</p>
                {description.trim() && (
                  <p className="mt-1 text-sm text-muted">{description.trim()}</p>
                )}
              </ReviewRow>

              <ReviewRow step={2} label="Persona" onEdit={goTo}>
                {selected ? (
                  <div className="flex items-start gap-3">
                    <PersonaIcon icon={selected.icon} fallback={selected.name} size="sm" />
                    <div className="min-w-0">
                      <p className="text-sm font-semibold text-ink">{selected.name}</p>
                      {selected.persona_role && (
                        <p className={`mt-1 ${EYEBROW}`}>{selected.persona_role}</p>
                      )}
                    </div>
                  </div>
                ) : (
                  <p className="text-sm text-muted">
                    No persona chosen &mdash; standard settings
                  </p>
                )}
              </ReviewRow>

              <ReviewRow
                step={3}
                // The provenance goes in the LABEL rather than in a caption
                // underneath. "Unchanged from the persona" sat below a grid of
                // ten values and had to be read after them to know what they
                // were; the label is read first, which is when the question is
                // being asked.
                label={
                  customizing
                    ? "Settings (yours)"
                    : selected
                      ? `Settings (from ${selected.name})`
                      : "Settings (standard)"
                }
                onEdit={goTo}
              >
                <dl
                  data-testid="review-parameters"
                  className="grid grid-cols-[repeat(auto-fit,minmax(min(11rem,100%),1fr))] gap-x-6 gap-y-3 text-xs"
                >
                  {ORDERED_KEYS.map((key) => (
                    <Fact
                      key={key}
                      label={TUNABLES[key].label}
                      tag={TUNABLES[key].tag}
                      value={displayValue(key, tuning)}
                      raw={tuning[key]}
                    />
                  ))}
                </dl>
                {/* No `help` on these Facts, unlike step 3's. This is the last
                    screen before an irreversible-feeling button and its job is
                    to be scannable; the explanations are one Edit click away,
                    on the step that exists to carry them. */}
              </ReviewRow>

              {/* The review step was a subset of the agent presented as the
                  whole of it. These two are editable in the settings sheet and
                  appear nowhere in this flow, so a user who read every screen
                  still did not know they existed. Naming them here is the
                  honest minimum; making them creation-time choices is a real
                  decision and a separate one. */}
              <p className={`${HELP} px-1`}>
                Two more settings &mdash; self-check and the generation model &mdash; use
                their defaults, and can be changed in the agent&rsquo;s settings once it
                exists.
              </p>
            </div>

            {submitError && (
              <div className="mt-5">
                <ErrorBanner error={submitError} />
              </div>
            )}
          </section>
        )}
      </div>

      {/* ------------------------------------------------------- Controls */}
      {/* `bg-surface`, opaque, rather than the translucent panel plus
          `backdrop-blur` this used to carry. The step content scrolls UNDER
          this bar, so the blur was not decoration -- it was standing in for an
          opaque background, and doing it at the cost of a compositor layer and
          a rule the token layer already states.

          `-bottom-4`, not `bottom-0`. `sticky` resolves its offset against the
          scroll container's PADDING box, and this bar lives inside `Drawer`'s
          `p-4` panel -- so `bottom-0` parks it 16px short of the panel edge and
          the wizard's steps scroll visibly through the gap underneath it, which
          reads as a rendering fault rather than a spacing one. The `-mx-4 -mb-4`
          beside it makes the bar span the full width; it does NOT move where the
          bar comes to rest. Only the offset does. `AgentSettingsSheet` already
          had this right, and the two are the same control in two places. */}
      <div className="sticky -bottom-4 z-10 -mx-4 -mb-4 mt-6 flex flex-wrap items-center gap-3 border-t border-line bg-surface px-4 pt-4 pb-[max(1rem,env(safe-area-inset-bottom))]">
        {step > 1 && (
          <button
            type="button"
            data-testid="wizard-back"
            onClick={() => goTo((step - 1) as StepNumber)}
            className={BTN_SECONDARY}
          >
            Back
          </button>
        )}

        {step < 4 ? (
          <button
            type="submit"
            data-testid="wizard-next"
            // Step 1 is the only required identity field in the flow. Its
            // persistent helper/error text explains the gate, so disabling the
            // action prevents a mobile user from tapping a control that cannot
            // advance and then hunting above the fold for the reason.
            disabled={step === 1 && nameProblem !== null}
            // `BTN_PRIMARY` already carries `disabled:opacity-45` and
            // `disabled:cursor-not-allowed`, which is the whole of the "you
            // cannot press this yet" treatment. The old string repainted the
            // disabled button in a different colour pair as well, which made a
            // blocked Next read as a THIRD kind of button rather than as this
            // one, unavailable.
            className={BTN_PRIMARY}
          >
            {/* Looked up rather than indexed: `step < 4` does not narrow a
                numeric literal union in TypeScript, so `STEPS[step]` is an
                out-of-range access as far as the compiler is concerned. */}
            Next: {STEPS.find((entry) => entry.n === step + 1)?.title}
          </button>
        ) : (
          <button
            type="submit"
            data-testid="agent-create-submit"
            disabled={busy}
            className={BTN_PRIMARY}
          >
            {busy ? "Creating…" : "Create agent"}
          </button>
        )}

        {/*
          No `role="alert"` on this one: the text is driven by the VALUE, so
          during a drag it re-renders on every step of the slider and an
          assertive region would interrupt for the length of the whole gesture.
          The same sentence is already beside the field, in that input's
          `aria-describedby`, and a blocked Next now moves focus there.
        */}
        {step === SETTINGS_STEP && overlapProblem && customizing && (
          <span data-testid="wizard-step-problem" className="text-xs text-bad">
            {overlapProblem}
          </span>
        )}
      </div>
    </form>
  );
}

/** One line of the review, with the step it came from one click away. */
function ReviewRow({
  step,
  label,
  onEdit,
  children,
}: {
  step: StepNumber;
  label: string;
  onEdit: (step: StepNumber) => void;
  children: ReactNode;
}) {
  return (
    <div
      data-testid={`review-row-${step}`}
      className={`${CARD} p-4`}
    >
      <div className="flex items-start justify-between gap-3">
        <span className={EYEBROW}>{label}</span>
        <button
          type="button"
          data-testid={`review-edit-${step}`}
          onClick={() => onEdit(step)}
          // The primary correction affordance on the review step, so it gets the
          // same 44px minimum as every other control rather than the 26x42 it
          // had -- which `BTN_SECONDARY` carries on its shared base, along with
          // `min-w-11`, because height alone is not a touch target.
          className={`${BTN_SECONDARY} ${BTN_SM} min-w-11 shrink-0`}
        >
          Edit
        </button>
      </div>
      <div className="mt-2">{children}</div>
    </div>
  );
}
