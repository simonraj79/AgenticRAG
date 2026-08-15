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
 * `API_BOUNDS` below are separate objects because they answer different
 * questions: what is worth dragging to, and what will the server accept.
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
  CategoryBadge,
  ErrorBanner,
  Fact,
  PersonaIcon,
  Reveal,
  errorMessage,
} from "../components/ui.tsx";

// --------------------------------------------------------------------------
// Shape of the tunables
// --------------------------------------------------------------------------

/** The eight parameters the wizard can set. Mirrors `AgentTunables` on the
 *  server, minus `system_prompt`, which this flow shows but does not send. */
export type Tuning = {
  chunk_size: number;
  chunk_overlap: number;
  splitter: string;
  retrieve_k: number;
  rerank_enabled: boolean;
  rerank_top_n: number;
  score_threshold: number;
  max_rewrites: number;
};

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
};

/** What the slider can reach: the band worth dragging in, not the API's range. */
const SLIDER_BAND = {
  chunk_size: { min: 128, max: 2048, step: 64 },
  chunk_overlap: { min: 0, max: 512, step: 8 },
  retrieve_k: { min: 1, max: 60, step: 1 },
  rerank_top_n: { min: 1, max: 20, step: 1 },
  score_threshold: { min: 0, max: 1, step: 0.01 },
  max_rewrites: { min: 0, max: 5, step: 1 },
} as const;

/** What the server accepts (`AgentTunables`). The number input enforces these,
 *  so a value the slider cannot reach is still reachable, and a value the API
 *  would reject is caught here instead of as a 422 four steps later. */
const API_BOUNDS = {
  chunk_size: { min: 64, max: 8192 },
  chunk_overlap: { min: 0, max: 4096 },
  retrieve_k: { min: 1, max: 100 },
  rerank_top_n: { min: 1, max: 100 },
  score_threshold: { min: 0, max: 1 },
  max_rewrites: { min: 0, max: 5 },
} as const;

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
  };
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
  { n: 3, title: "Tuning", blurb: "How it retrieves" },
  { n: 4, title: "Review", blurb: "Check and create" },
] as const;

type StepNumber = 1 | 2 | 3 | 4;

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
    <nav aria-label="Progress" data-testid="wizard-rail">
      <p className="text-xs font-medium tracking-wide text-slate-400 uppercase">
        Step {current} of {STEPS.length} · {STEPS[current - 1].blurb}
      </p>

      <ol className="mt-3 flex items-center gap-1.5 sm:gap-2">
        {STEPS.map((step, index) => {
          const done = step.n < current;
          const active = step.n === current;
          const reachable = step.n <= furthest;

          const circle = (
            <span
              aria-hidden="true"
              className={`inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-full border text-xs font-semibold transition ${
                active
                  ? "border-emerald-400 bg-emerald-500 text-emerald-950"
                  : done
                    ? "border-emerald-800/70 bg-emerald-950/50 text-emerald-300"
                    : "border-slate-700 bg-slate-900 text-slate-400"
              }`}
            >
              {done ? "✓" : step.n}
            </span>
          );

          const label = (
            <span
              className={`text-xs font-medium whitespace-nowrap ${
                active ? "text-slate-100" : done ? "text-slate-300" : "text-slate-400"
              } ${active ? "" : "hidden sm:inline"}`}
            >
              {step.title}
            </span>
          );

          return (
            <li key={step.n} className="flex min-w-0 items-center gap-1.5 sm:gap-2">
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
                  className="flex min-h-11 items-center gap-2 rounded-md px-1 transition hover:opacity-80"
                >
                  {circle}
                  {label}
                </button>
              ) : (
                <span
                  data-testid={`wizard-step-${step.n}`}
                  aria-current={active ? "step" : undefined}
                  className="flex min-h-11 items-center gap-2 px-1"
                >
                  {circle}
                  {label}
                </span>
              )}

              {index < STEPS.length - 1 && (
                <span
                  aria-hidden="true"
                  className={`h-px w-3 shrink-0 sm:w-8 ${
                    done ? "bg-emerald-800/70" : "bg-slate-800"
                  }`}
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

const INPUT_CLASS =
  "min-h-11 w-full rounded-md border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 outline-none focus:border-slate-500";

/**
 * One tunable: a slider for reaching a value, a number input for naming one.
 *
 * Both drive the same state, so they cannot disagree. The slider carries
 * `SLIDER_BAND`, the number carries `API_BOUNDS`, and the gap between them is
 * the point -- dragging cannot leave the useful band, typing can go anywhere
 * the server allows.
 *
 * **The lower bound is enforced on blur, the upper bound on every keystroke, and
 * that asymmetry is the whole trick.** A half-typed number is a legal
 * intermediate state, and clamping it up to the minimum as it is typed makes
 * most in-band values unreachable: with `chunk_size` (min 64), typing `400` goes
 * `4` -> clamped to `64` -> `640` -> `6400`. Every value whose leading digits
 * fall below the minimum is impossible to type -- `100`, `128`, `256`, `400`,
 * `500`, two of which are values the seeded personas actually use. So a value
 * below the minimum is allowed to exist while the field has focus and is raised
 * on blur, when the user has finished saying what they meant.
 *
 * The upper bound has no such problem and stays on every keystroke: digits are
 * typed left to right, so a prefix of a too-large number is smaller, not larger,
 * and clamping it down can never block a value on the way to a legal one.
 *
 * The empty string is ignored rather than parsed, so clearing the field to
 * retype it does not snap the slider to zero between two keystrokes.
 */
function ParamSlider({
  id,
  label,
  help,
  value,
  onChange,
  band,
  bounds,
  decimals = 0,
  disabled = false,
  warning,
  numberRef,
}: {
  id: string;
  label: string;
  help: ReactNode;
  value: number;
  onChange: (next: number) => void;
  band: { min: number; max: number; step: number };
  bounds: { min: number; max: number };
  decimals?: number;
  disabled?: boolean;
  warning?: string | null;
  /** So a blocked Next can send focus to the control that is blocking it. */
  numberRef?: RefObject<HTMLInputElement | null>;
}) {
  function commit(raw: string) {
    const parsed = Number(raw);
    if (raw.trim() === "" || Number.isNaN(parsed)) return;
    onChange(Math.min(bounds.max, parsed));
  }

  /** Raise a still-too-small value once the user has stopped typing it. */
  function settle() {
    if (value < bounds.min) onChange(bounds.min);
  }

  return (
    <div data-testid={`param-${id}`} data-value={value}>
      <div className="flex items-baseline justify-between gap-3">
        <label className="text-xs font-medium text-slate-300" htmlFor={`${id}-range`}>
          {label}
        </label>
        <input
          id={`${id}-number`}
          type="number"
          ref={numberRef}
          data-testid={`param-${id}-number`}
          aria-label={`${label}, exact value`}
          disabled={disabled}
          value={value}
          min={bounds.min}
          max={bounds.max}
          step={band.step}
          onChange={(event) => commit(event.target.value)}
          onBlur={settle}
          className="min-h-11 w-24 shrink-0 rounded-md border border-slate-700 bg-slate-950 px-2 py-1 text-right font-mono text-sm text-slate-100 outline-none focus:border-slate-500 disabled:opacity-50"
        />
      </div>

      <input
        id={`${id}-range`}
        type="range"
        data-testid={`param-${id}-range`}
        disabled={disabled}
        value={value}
        min={band.min}
        max={band.max}
        step={band.step}
        onChange={(event) => onChange(Number(event.target.value))}
        // The number input beside it is the accessible readout, and it is a real
        // focusable control rather than an aria-valuetext, so the slider only
        // needs to point at its own label. The warning joins the description
        // when there is one rather than living in a live region: a value-driven
        // message re-renders on every step of a drag, and an assertive region
        // would interrupt continuously for the length of it. Described-by is
        // announced on focus, which is when it is wanted.
        aria-describedby={warning ? `${id}-help ${id}-warning` : `${id}-help`}
        className="gw-range mt-1"
      />

      <p id={`${id}-help`} className="text-xs leading-relaxed text-slate-400">
        {help}
      </p>

      {warning && (
        <p
          id={`${id}-warning`}
          data-testid={`param-${id}-warning`}
          className="mt-1.5 rounded-md border border-amber-800/60 bg-amber-950/30 px-2.5 py-1.5 text-xs text-amber-200"
        >
          {warning}
        </p>
      )}

      {decimals > 0 && <span className="sr-only">{value.toFixed(decimals)}</span>}
    </div>
  );
}

/** A two-option segmented control. Two radios that look like one switch. */
function Segmented({
  legend,
  help,
  name,
  value,
  options,
  onChange,
  disabled = false,
  testId,
}: {
  legend: string;
  help: ReactNode;
  name: string;
  value: string;
  options: { value: string; label: string }[];
  onChange: (next: string) => void;
  disabled?: boolean;
  testId: string;
}) {
  return (
    <fieldset data-testid={testId} data-value={value} disabled={disabled}>
      <legend className="text-xs font-medium text-slate-300">{legend}</legend>
      <div className="mt-2 inline-flex rounded-md border border-slate-700 bg-slate-950 p-1">
        {options.map((option) => {
          const active = option.value === value;
          return (
            <label
              key={option.value}
              data-testid={`${testId}-${option.value}`}
              data-selected={active}
              className={`min-h-11 cursor-pointer rounded px-3 py-2 text-sm transition focus-within:ring-2 focus-within:ring-emerald-500/60 ${
                active
                  ? "bg-emerald-500 font-medium text-emerald-950"
                  : "text-slate-300 hover:text-slate-100"
              } ${disabled ? "cursor-not-allowed opacity-50" : ""}`}
            >
              <input
                type="radio"
                name={name}
                className="sr-only"
                value={option.value}
                checked={active}
                onChange={() => onChange(option.value)}
              />
              {option.label}
            </label>
          );
        })}
      </div>
      <p className="mt-2 text-xs leading-relaxed text-slate-400">{help}</p>
    </fieldset>
  );
}

// --------------------------------------------------------------------------
// The wizard
// --------------------------------------------------------------------------

export default function CreateAgentWizard({
  templates,
  existingNames,
  onCreated,
}: {
  templates: Template[];
  /** Names the user already owns. `agents` is unique on (owner, name), so a
   *  collision is a 409 -- catching it on step 1 turns a failure at the end of
   *  a four-step flow into a hint before the flow starts. */
  existingNames: string[];
  onCreated: (agent: Agent) => void | Promise<void>;
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

  const nameRef = useRef<HTMLInputElement>(null);
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
  const overlapProblem =
    tuning.chunk_overlap >= tuning.chunk_size
      ? `Overlap (${tuning.chunk_overlap}) must be smaller than chunk size (${tuning.chunk_size}).`
      : null;

  // Not an error: the server accepts it, and the reranker simply gets fewer
  // candidates than it was asked to return. Worth saying, not worth blocking.
  const topNWarning =
    tuning.rerank_enabled && tuning.rerank_top_n > tuning.retrieve_k
      ? `Only ${tuning.retrieve_k} chunks are retrieved, so the reranker cannot return ${tuning.rerank_top_n}.`
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
    if (step === 3 && target !== 3) setResetNotice(null);
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

  const showNameProblem = nameTouched && nameProblem;

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
      className="rounded-xl border border-slate-800 bg-slate-900/50 p-5"
    >
      <StepRail current={step} furthest={furthest} onJump={goTo} />

      <div className="mt-6 border-t border-slate-800 pt-6">
        {/* -------------------------------------------------- Step 1: Name */}
        {step === 1 && (
          <section aria-labelledby="wizard-heading">
            <h2
              id="wizard-heading"
              ref={headingRef}
              tabIndex={-1}
              className="text-lg font-semibold text-slate-100 outline-none"
            >
              Name this agent
            </h2>
            <p className="mt-1 max-w-2xl text-sm text-slate-400">
              One agent is one corpus, one persona and one isolated vector namespace. The
              name is how you will tell this one apart from the others on the dashboard.
            </p>

            <div className="mt-6 max-w-xl">
              <div className="flex items-baseline justify-between gap-3">
                <label className="text-sm font-medium text-slate-200" htmlFor="agent-name">
                  Name{" "}
                  <span className="rounded border border-emerald-800/70 bg-emerald-950/40 px-1.5 py-0.5 text-[0.65rem] font-semibold tracking-wide text-emerald-300 uppercase">
                    Required
                  </span>
                </label>
                <span
                  className={`font-mono text-xs ${
                    trimmedName.length > MAX_NAME_LENGTH ? "text-rose-300" : "text-slate-400"
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
                className={`mt-2 ${INPUT_CLASS} ${
                  showNameProblem ? "border-rose-700 focus:border-rose-500" : ""
                }`}
              />

              {showNameProblem ? (
                <p
                  id="agent-name-error"
                  role="alert"
                  data-testid="agent-name-error"
                  className="mt-2 text-xs text-rose-300"
                >
                  {nameProblem}
                </p>
              ) : (
                <p id="agent-name-hint" className="mt-2 text-xs text-slate-400">
                  Name it after the material it will answer from -- a topic, a module, a
                  handbook.
                </p>
              )}

              <div className="mt-5">
                <label
                  className="text-sm font-medium text-slate-200"
                  htmlFor="agent-description"
                >
                  Description <span className="font-normal text-slate-400">(optional)</span>
                </label>
                <input
                  id="agent-description"
                  data-testid="agent-description-input"
                  value={description}
                  onChange={(event) => setDescription(event.target.value)}
                  placeholder="What this agent knows about"
                  className={`mt-2 ${INPUT_CLASS}`}
                />
                <p className="mt-2 text-xs text-slate-400">
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
              className="text-lg font-semibold text-slate-100 outline-none"
            >
              Choose a teaching persona
            </h2>
            <p className="mt-1 max-w-2xl text-sm text-slate-400">
              The persona decides how the agent answers -- what it asks back, what it
              withholds, how it refuses. It never changes what the agent may answer{" "}
              <em>from</em>: every persona is bound to this agent&rsquo;s documents alone.
            </p>

            {templates.length === 0 ? (
              <p className="mt-6 text-sm text-slate-400">
                No templates loaded. The agent will be created with the server&rsquo;s
                default parameters.
              </p>
            ) : (
              <div className="mt-6 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
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
                      />

                      <div className="flex items-start gap-3">
                        <PersonaIcon icon={template.icon} fallback={template.name} size="sm" />
                        <div className="min-w-0 flex-1">
                          <div className="flex items-start justify-between gap-2">
                            <span className="font-medium text-slate-100">{template.name}</span>
                            <CategoryBadge category={template.category} />
                          </div>
                          {template.persona_role && (
                            <span className="mt-0.5 block text-xs tracking-wide text-slate-400 uppercase">
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
                          className={`mt-3 border-t border-slate-800 pt-3 text-xs leading-relaxed text-slate-400 ${
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
          </section>
        )}

        {/* ------------------------------------------------ Step 3: Tuning */}
        {step === 3 && (
          <section aria-labelledby="wizard-heading">
            <h2
              id="wizard-heading"
              ref={headingRef}
              tabIndex={-1}
              className="text-lg font-semibold text-slate-100 outline-none"
            >
              Retrieval tuning
            </h2>
            <p className="mt-1 max-w-2xl text-sm text-slate-400">
              These values are <em>copied</em> onto the agent when it is created. Editing
              the persona later will not re-tune an agent you already built, and neither
              will editing these afterwards re-chunk documents you have already uploaded.
            </p>

            <div className="mt-5">
              <Segmented
                testId="tuning-mode"
                legend="Parameters"
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
                  { value: "template", label: selected ? `Use ${selected.name}` : "Use defaults" },
                  { value: "custom", label: "Customize" },
                ]}
                help={
                  customizing
                    ? "Your values are sent with the create request. The persona's system prompt is unaffected."
                    : selected
                      ? `The agent is created with ${selected.name}'s values, shown below.`
                      : "The agent is created with the server's default parameters."
                }
              />
            </div>

            {resetNotice && (
              <p
                role="status"
                data-testid="tuning-reset-notice"
                className="mt-4 rounded-md border border-sky-800/60 bg-sky-950/30 px-3 py-2 text-xs text-sky-200"
              >
                {resetNotice}
              </p>
            )}

            {!customizing ? (
              <dl
                data-testid="template-parameters"
                className="mt-5 grid grid-cols-2 gap-x-6 gap-y-3 rounded-lg border border-slate-800 bg-slate-950/60 p-4 text-xs sm:grid-cols-4"
              >
                <Fact label="Chunk size" value={tuning.chunk_size} />
                <Fact label="Overlap" value={tuning.chunk_overlap} />
                <Fact label="Splitter" value={tuning.splitter} />
                <Fact label="Retrieve k" value={tuning.retrieve_k} />
                <Fact label="Rerank" value={tuning.rerank_enabled ? "on" : "off"} />
                <Fact label="Rerank top n" value={tuning.rerank_top_n} />
                <Fact label="Score threshold" value={tuning.score_threshold} />
                <Fact label="Max rewrites" value={tuning.max_rewrites} />
              </dl>
            ) : (
              <div data-testid="tuning-sliders" className="mt-5 space-y-6">
                <div className="grid gap-6 sm:grid-cols-2">
                  <ParamSlider
                    id="chunk-size"
                    label="Chunk size"
                    value={tuning.chunk_size}
                    onChange={(next) => editTuning({ chunk_size: next })}
                    band={SLIDER_BAND.chunk_size}
                    bounds={API_BOUNDS.chunk_size}
                    help={
                      <>
                        Tokens per chunk, not characters. Big enough to hold a whole idea,
                        small enough that retrieval stays precise. 800 uses a tenth of{" "}
                        <span className="font-mono">gemini-embedding-2</span>&rsquo;s
                        8,192-token ceiling; past that ceiling the tail of a chunk is
                        truncated at embed time and lost silently. Applies to new uploads.
                      </>
                    }
                  />

                  <ParamSlider
                    id="chunk-overlap"
                    label="Overlap"
                    value={tuning.chunk_overlap}
                    onChange={(next) => editTuning({ chunk_overlap: next })}
                    band={SLIDER_BAND.chunk_overlap}
                    bounds={API_BOUNDS.chunk_overlap}
                    warning={overlapProblem}
                    numberRef={overlapRef}
                    help={
                      <>
                        Tokens repeated between neighbouring chunks, so a fact that straddles
                        a boundary is still retrievable from one side. Every persona uses 15%
                        of its chunk size. Applies to new uploads.
                      </>
                    }
                  />
                </div>

                <Segmented
                  testId="tuning-splitter"
                  legend="Splitter"
                  name="tuning-splitter"
                  value={tuning.splitter}
                  onChange={(next) => editTuning({ splitter: next })}
                  options={[
                    { value: "markdown", label: "markdown" },
                    { value: "recursive", label: "recursive" },
                  ]}
                  help="markdown keeps a heading attached to the body beneath it, which is what stops a slide's title being cut away from its content. recursive splits on blank lines and sentences and ignores structure."
                />

                <div className="grid gap-6 sm:grid-cols-2">
                  <ParamSlider
                    id="retrieve-k"
                    label="Retrieve k"
                    value={tuning.retrieve_k}
                    onChange={(next) => editTuning({ retrieve_k: next })}
                    band={SLIDER_BAND.retrieve_k}
                    bounds={API_BOUNDS.retrieve_k}
                    help="How many chunks Pinecone returns for the reranker to choose from. A bigger pool is the one thing that genuinely fixes poor recall; it also costs rerank latency, measured at about 830 ms."
                  />

                  <ParamSlider
                    id="rerank-top-n"
                    label="Rerank top n"
                    value={tuning.rerank_top_n}
                    onChange={(next) => editTuning({ rerank_top_n: next })}
                    band={SLIDER_BAND.rerank_top_n}
                    bounds={API_BOUNDS.rerank_top_n}
                    disabled={!tuning.rerank_enabled}
                    warning={topNWarning}
                    help="How many chunks actually reach the prompt. This is the operative number: it bounds how many separate places in the corpus can contribute to one answer."
                  />
                </div>

                <Segmented
                  testId="tuning-rerank"
                  legend="Rerank"
                  name="tuning-rerank"
                  value={tuning.rerank_enabled ? "on" : "off"}
                  onChange={(next) =>
                    editTuning({ rerank_enabled: next === "on" })
                  }
                  options={[
                    { value: "on", label: "On" },
                    { value: "off", label: "Off" },
                  ]}
                  help="Cohere rerank-v3.5 reorders the retrieved candidates by relevance to the question. Precision is what reranking buys; without it the top-n is whatever the embedding happened to put there."
                />

                <div className="grid gap-6 sm:grid-cols-2">
                  <ParamSlider
                    id="score-threshold"
                    label="Score threshold"
                    value={tuning.score_threshold}
                    onChange={(next) =>
                      editTuning({
                        // Two decimals: 0.01 steps accumulate float error and the
                        // server takes a float, so 0.6100000000000001 would be
                        // stored and then shown.
                        score_threshold: Math.round(next * 100) / 100,
                      })
                    }
                    band={SLIDER_BAND.score_threshold}
                    bounds={API_BOUNDS.score_threshold}
                    decimals={2}
                    help={
                      <>
                        Below this top similarity score the question becomes a candidate for
                        rewriting. It governs <em>rewriting</em>, not refusing -- refusal
                        comes from the system prompt. Measured on one corpus, on-topic
                        questions scored 0.61-0.67 and off-topic ones 0.49-0.58, so 0.5 sits
                        inside the noise rather than above it.
                      </>
                    }
                  />

                  <ParamSlider
                    id="max-rewrites"
                    label="Max rewrites"
                    value={tuning.max_rewrites}
                    onChange={(next) => editTuning({ max_rewrites: next })}
                    band={SLIDER_BAND.max_rewrites}
                    bounds={API_BOUNDS.max_rewrites}
                    help="Ceiling on the rewrite loop. Cost blowout is one of the four named agentic failure modes, and an unbounded rewrite loop is precisely how it happens. 0 turns rewriting off."
                  />
                </div>
              </div>
            )}

            {selected?.system_prompt && (
              <div className="mt-5">
                <Reveal summary="System prompt (read-only)" testId="template-prompt">
                  <p className="mb-3 text-xs text-slate-400">
                    The persona&rsquo;s prompt, copied onto the agent as-is. It is the
                    control that makes the agent refuse rather than guess, so it is shown
                    here in full and changed by choosing a different persona.
                  </p>
                  <pre className="max-h-64 overflow-y-auto text-xs leading-relaxed whitespace-pre-wrap text-slate-400">
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
              className="text-lg font-semibold text-slate-100 outline-none"
            >
              Review and create
            </h2>
            <p className="mt-1 max-w-2xl text-sm text-slate-400">
              Nothing is created until you press the button. The next screen asks for the
              documents this agent answers from -- it has none until you upload them.
            </p>

            <div className="mt-6 space-y-3">
              <ReviewRow step={1} label="Name" onEdit={goTo}>
                <p className="font-medium text-slate-100">{trimmedName || "—"}</p>
                {description.trim() && (
                  <p className="mt-1 text-sm text-slate-400">{description.trim()}</p>
                )}
              </ReviewRow>

              <ReviewRow step={2} label="Persona" onEdit={goTo}>
                {selected ? (
                  <div className="flex items-start gap-3">
                    <PersonaIcon icon={selected.icon} fallback={selected.name} size="sm" />
                    <div className="min-w-0">
                      <p className="font-medium text-slate-100">{selected.name}</p>
                      {selected.persona_role && (
                        <p className="text-xs tracking-wide text-slate-400 uppercase">
                          {selected.persona_role}
                        </p>
                      )}
                    </div>
                  </div>
                ) : (
                  <p className="text-slate-300">No persona -- server defaults</p>
                )}
              </ReviewRow>

              <ReviewRow
                step={3}
                label={customizing ? "Tuning (customized)" : "Tuning"}
                onEdit={goTo}
              >
                <dl
                  data-testid="review-parameters"
                  className="grid grid-cols-2 gap-x-6 gap-y-2 text-xs sm:grid-cols-4"
                >
                  <Fact label="Chunk size" value={tuning.chunk_size} />
                  <Fact label="Overlap" value={tuning.chunk_overlap} />
                  <Fact label="Splitter" value={tuning.splitter} />
                  <Fact label="Retrieve k" value={tuning.retrieve_k} />
                  <Fact label="Rerank" value={tuning.rerank_enabled ? "on" : "off"} />
                  <Fact label="Rerank top n" value={tuning.rerank_top_n} />
                  <Fact label="Score threshold" value={tuning.score_threshold} />
                  <Fact label="Max rewrites" value={tuning.max_rewrites} />
                </dl>
                {!customizing && (
                  <p className="mt-2 text-xs text-slate-400">
                    Unchanged from the persona.
                  </p>
                )}
              </ReviewRow>
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
      <div className="mt-6 flex flex-wrap items-center gap-3 border-t border-slate-800 pt-5">
        {step > 1 && (
          <button
            type="button"
            data-testid="wizard-back"
            onClick={() => goTo((step - 1) as StepNumber)}
            className="min-h-11 rounded-md border border-slate-700 bg-slate-900 px-4 py-2 text-sm text-slate-300 transition hover:border-slate-600"
          >
            Back
          </button>
        )}

        {step < 4 ? (
          <button
            type="submit"
            data-testid="wizard-next"
            // Deliberately NOT disabled when the step is incomplete. A dead
            // button says "no" without saying why, and the reason is exactly
            // what the user is missing; pressing it focuses the field and names
            // the problem instead.
            className="min-h-11 rounded-md bg-emerald-500 px-4 py-2 text-sm font-semibold text-emerald-950 transition hover:bg-emerald-400"
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
            className="min-h-11 rounded-md bg-emerald-500 px-4 py-2 text-sm font-semibold text-emerald-950 transition hover:bg-emerald-400 disabled:opacity-50"
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
        {step === 3 && overlapProblem && customizing && (
          <span data-testid="wizard-step-problem" className="text-xs text-rose-300">
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
      className="rounded-lg border border-slate-800 bg-slate-950/60 p-4"
    >
      <div className="flex items-start justify-between gap-3">
        <span className="text-xs font-medium tracking-wide text-slate-400 uppercase">
          {label}
        </span>
        <button
          type="button"
          data-testid={`review-edit-${step}`}
          onClick={() => onEdit(step)}
          // The primary correction affordance on the review step, so it gets the
          // same 44px minimum as every other control rather than the 26x42 it
          // had. `min-w-11` too: height alone is not a touch target.
          className="min-h-11 min-w-11 shrink-0 rounded-md border border-slate-800 px-3 py-1 text-xs text-slate-400 transition hover:border-slate-600 hover:text-slate-200"
        >
          Edit
        </button>
      </div>
      <div className="mt-2">{children}</div>
    </div>
  );
}
