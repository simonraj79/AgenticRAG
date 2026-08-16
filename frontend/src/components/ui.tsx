/**
 * The handful of primitives every view shares.
 *
 * Kept in one file rather than one-file-per-component because each is a dozen
 * lines and splitting them would make the import list longer than the code.
 *
 * The two tuning controls at the bottom break that "a dozen lines" rule, and
 * they are here for the other half of the reason: they are shared. They began
 * module-private inside `CreateAgentWizard`, and a second surface that edits the
 * same ten parameters -- an agent-settings sheet -- would otherwise have copied
 * them. A copy of `ParamSlider` in particular is not a copy of a control, it is
 * a copy of the blur/keystroke clamping asymmetry documented on it, and the
 * copy is where that reasoning would quietly stop being true.
 */

import { useEffect, useState } from "react";
import type { ReactNode, RefObject } from "react";

// --------------------------------------------------------------------------
// Feedback
// --------------------------------------------------------------------------

export function Spinner({ label }: { label?: string }) {
  return (
    <span className="inline-flex items-center gap-2 text-sm text-slate-400">
      <span
        className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-slate-600 border-t-slate-200"
        aria-hidden="true"
      />
      {label && <span>{label}</span>}
    </span>
  );
}

/**
 * Whatever the API actually said, shown verbatim.
 *
 * `role="alert"` so a screen reader announces it without the user hunting for
 * what changed, and `whitespace-pre-wrap` because FastAPI's 422 messages
 * concatenate several field errors and wrap badly otherwise.
 */
export function ErrorBanner({ error }: { error: string | null }) {
  if (!error) return null;
  return (
    <div
      role="alert"
      data-testid="error-banner"
      className="rounded-lg border border-rose-800/60 bg-rose-950/40 px-4 py-3 text-sm whitespace-pre-wrap text-rose-200"
    >
      {error}
    </div>
  );
}

/** Turns any thrown value into something printable. Never throws itself. */
export function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  return String(error);
}

/** The dashed "nothing here yet" panel, with room for the action that fixes it. */
export function EmptyState({
  title,
  detail,
  children,
}: {
  title: string;
  detail?: string;
  children?: ReactNode;
}) {
  return (
    <div className="rounded-xl border border-dashed border-slate-800 px-6 py-10 text-center">
      <p className="text-sm text-slate-300">{title}</p>
      {detail && <p className="mx-auto mt-1 max-w-md text-xs text-slate-400">{detail}</p>}
      {children && <div className="mt-4 flex justify-center">{children}</div>}
    </div>
  );
}

// --------------------------------------------------------------------------
// Status
// --------------------------------------------------------------------------

/**
 * `ready` and `indexed` are both mapped, and both are load-bearing.
 *
 * An agent settles on `ready`; a document settled on `indexed` before ingest
 * moved to a background job and on `ready` after. Rows written by either build
 * are in the same table, so the pill has to speak both vocabularies -- and so
 * does anything deciding whether a document has stopped moving (see
 * `TERMINAL_DOCUMENT_STATUSES` in AgentDocuments).
 */
const STATUS_STYLES: Record<string, string> = {
  ready: "border-emerald-800/60 bg-emerald-950/40 text-emerald-300",
  indexed: "border-emerald-800/60 bg-emerald-950/40 text-emerald-300",
  indexing: "border-amber-800/60 bg-amber-950/40 text-amber-300",
  processing: "border-amber-800/60 bg-amber-950/40 text-amber-300",
  pending: "border-slate-700 bg-slate-900 text-slate-400",
  empty: "border-slate-700 bg-slate-900 text-slate-400",
  failed: "border-rose-800/60 bg-rose-950/40 text-rose-300",
};

export function StatusPill({ status }: { status: string }) {
  const style = STATUS_STYLES[status] ?? "border-slate-700 bg-slate-900 text-slate-400";
  return (
    <span
      className={`inline-block rounded-full border px-2 py-0.5 text-xs font-medium ${style}`}
    >
      {status}
    </span>
  );
}

// --------------------------------------------------------------------------
// Personas
// --------------------------------------------------------------------------

/*
 * Every persona field is nullable in `lib/types.ts` and every component below
 * takes it that way. That is not defensive typing: `pedagogy` really is null on
 * the three original templates and on every agent created from them, and
 * `icon`, `persona_role` and `category` are null on any agent that predates the
 * persona columns. A card that assumes them renders a hole in the layout for
 * rows that exist right now.
 */

const ICON_SIZES: Record<"sm" | "md" | "lg", string> = {
  sm: "h-8 w-8 text-lg",
  md: "h-10 w-10 text-xl",
  lg: "h-12 w-12 text-2xl",
};

/**
 * The persona's glyph, or its initial when it has none.
 *
 * `aria-hidden` throughout: the icon always sits beside the name it stands for,
 * so announcing "light bulb" before "Feynman Explainer" adds a word and no
 * information. It is decoration for the eye, and it says so.
 */
export function PersonaIcon({
  icon,
  fallback,
  size = "md",
}: {
  icon?: string | null;
  fallback?: string | null;
  size?: "sm" | "md" | "lg";
}) {
  const glyph = icon?.trim() || (fallback?.trim()?.[0] ?? "?").toUpperCase();
  return (
    <span
      aria-hidden="true"
      className={`inline-flex shrink-0 items-center justify-center rounded-lg border border-slate-800 bg-slate-950 leading-none ${ICON_SIZES[size]}`}
    >
      {glyph}
    </span>
  );
}

/**
 * `category` is a plain String column, not an enum, precisely so a new grouping
 * needs a seed row rather than a migration. The cost of that choice lands here:
 * an unrecognised value must render as something neutral instead of indexing
 * into `undefined`, which is why the fallback is a real label ("ungrouped")
 * rather than a crash or an empty badge.
 */
const CATEGORY_LABELS: Record<string, string> = {
  explain: "Explain",
  practice: "Practice",
  assess: "Assess",
  reflect: "Reflect",
  general: "General",
};

const CATEGORY_STYLES: Record<string, string> = {
  explain: "border-sky-800/60 bg-sky-950/40 text-sky-300",
  practice: "border-violet-800/60 bg-violet-950/40 text-violet-300",
  assess: "border-amber-800/60 bg-amber-950/40 text-amber-300",
  reflect: "border-teal-800/60 bg-teal-950/40 text-teal-300",
  general: "border-slate-700 bg-slate-900 text-slate-400",
};

const UNGROUPED = "border-slate-700 bg-slate-900 text-slate-400";

export function CategoryBadge({ category }: { category?: string | null }) {
  const key = category ?? "";
  const label = CATEGORY_LABELS[key] ?? "ungrouped";
  const style = CATEGORY_STYLES[key] ?? UNGROUPED;
  return (
    <span
      className={`inline-block rounded-full border px-2 py-0.5 text-[0.65rem] font-medium tracking-wide uppercase ${style}`}
    >
      {label}
    </span>
  );
}

// --------------------------------------------------------------------------
// Data display
// --------------------------------------------------------------------------

/** One label/value pair inside a `<dl>`. Used by every parameter grid. */
export function Fact({ label, value }: { label: string; value: string | number }) {
  return (
    <div>
      <dt className="text-slate-400">{label}</dt>
      <dd className="mt-0.5 text-slate-200">{value}</dd>
    </div>
  );
}

/**
 * A collapsed panel for detail that is worth keeping on the page but not worth
 * leading with -- retrieval parameters, a system prompt.
 *
 * A native `<details>` rather than a `useState` toggle: it is open-able before
 * React hydrates, Ctrl+F finds text inside a closed one in Chrome, and the
 * disclosure semantics arrive without an `aria-expanded` to keep in sync.
 */
export function Reveal({
  summary,
  children,
  testId,
}: {
  summary: string;
  children: ReactNode;
  testId?: string;
}) {
  return (
    <details data-testid={testId} className="group rounded-lg border border-slate-800 bg-slate-950/60">
      {/*
        `min-h-11` plus `flex items-center`, not padding. A `<summary>` is the
        one interactive element in this file that is not a `<button>`, which is
        exactly how it stayed at ~36px while every button around it was brought
        to the 44px convention -- it was never in the audit's list because
        nothing about it looks like a control.

        The flex box is required, not decoration: `min-h-11` on its own grows the
        box and leaves the label sitting at the top of it, so the target gets
        bigger while the text appears to drift upward. The marker span keeps
        `inline-block` for its rotate transform.
      */}
      <summary className="flex min-h-11 cursor-pointer list-none items-center px-4 py-2.5 text-xs font-medium tracking-wide text-slate-400 uppercase transition select-none hover:text-slate-200">
        <span className="mr-2 inline-block transition group-open:rotate-90" aria-hidden="true">
          &rsaquo;
        </span>
        {summary}
      </summary>
      <div className="border-t border-slate-800 px-4 py-4">{children}</div>
    </details>
  );
}

// --------------------------------------------------------------------------
// Tuning controls
// --------------------------------------------------------------------------

/** The ten parameters a tuning surface can set. Mirrors `AgentTunables` on the
 *  server, minus `system_prompt`, which those surfaces show but do not send.
 *
 *  It lives here rather than in `lib/types.ts` because that file is types-only
 *  and is the transcribed API contract; this shape is the FORM's model, and it
 *  travels with the two controls below that read and write it. */
export type Tuning = {
  chunk_size: number;
  chunk_overlap: number;
  splitter: string;
  retrieve_k: number;
  rerank_enabled: boolean;
  rerank_top_n: number;
  score_threshold: number;
  max_rewrites: number;
  /** Whether the agent may call tools mid-answer -- search the corpus a second
   *  time, or write and run Python. The only parameter here that changes what
   *  the agent DOES rather than what it retrieves, which is why it is the one
   *  with a sentence about latency under it. */
  tools_enabled: boolean;
  /** Tool round-trips allowed in one turn before the loop is closed and an
   *  answer is forced. A ceiling, not a target: most turns use none. */
  max_tool_steps: number;
};

/**
 * What the slider can reach: the band worth dragging in, not the API's range.
 *
 * `SLIDER_BAND` and `API_BOUNDS` are separate objects because they answer
 * different questions: what is worth dragging to, and what will the server
 * accept. `chunk_size` accepts 64-8192, and a slider spanning that puts every
 * value any template actually uses (400, 500, 800) inside the first ninth of the
 * track, where it cannot be aimed. Each slider covers the band worth tuning in
 * and pairs with a number input that accepts the API's full range -- the slider
 * for the common case, the number for the case the slider cannot express.
 *
 * `max_tool_steps` is the exception that proves the rule -- its band IS the
 * API's range, because 0-8 is already small enough to aim at and there is no
 * useful value outside it. The number input beside it is then a readout rather
 * than an escape hatch, which is the right relationship for a ceiling.
 */
export const SLIDER_BAND = {
  chunk_size: { min: 128, max: 2048, step: 64 },
  chunk_overlap: { min: 0, max: 512, step: 8 },
  retrieve_k: { min: 1, max: 60, step: 1 },
  rerank_top_n: { min: 1, max: 20, step: 1 },
  score_threshold: { min: 0, max: 1, step: 0.01 },
  max_rewrites: { min: 0, max: 5, step: 1 },
  max_tool_steps: { min: 0, max: 8, step: 1 },
} as const;

/** What the server accepts (`AgentTunables`). The number input enforces these,
 *  so a value the slider cannot reach is still reachable, and a value the API
 *  would reject is caught here instead of as a 422 four steps later. */
export const API_BOUNDS = {
  chunk_size: { min: 64, max: 8192 },
  chunk_overlap: { min: 0, max: 4096 },
  retrieve_k: { min: 1, max: 100 },
  rerank_top_n: { min: 1, max: 100 },
  score_threshold: { min: 0, max: 1 },
  max_rewrites: { min: 0, max: 5 },
  max_tool_steps: { min: 0, max: 8 },
} as const;

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
 *
 * **Depends on `.gw-range` in `index.css`, which is not optional styling.** A
 * bare `accent-color` slider leaves a 4px hit area in an app whose convention is
 * a 44px minimum, so that class is a 44px transparent input with a 6px track
 * drawn inside it -- the touch target of this control lives in the stylesheet,
 * not in the class list below. Anything that changes or drops `.gw-range`
 * changes this control, and the two vendor pseudo-elements in it must stay in
 * separate rules (an unrecognised pseudo-element invalidates the whole rule and
 * would silently leave BOTH browsers unstyled).
 */
export function ParamSlider({
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
export function Segmented({
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
// Destructive actions
// --------------------------------------------------------------------------

/**
 * Delete, behind a confirmation that is itself a button.
 *
 * Three design notes worth the space:
 *
 * **Not `window.confirm`.** The native dialog blocks the JS event loop and sits
 * outside the DOM, so it is invisible to the accessibility tree and to any
 * browser automation that has not registered a dialog handler in advance. An
 * in-page confirm is inspectable, styleable, and testable.
 *
 * **The same button confirms.** Clicking once arms it and relabels it; clicking
 * again performs the delete. Keeping one element means one `data-testid` for
 * the whole interaction, and it matches the "click again to confirm" pattern
 * users already know. It disarms itself after `ARM_TIMEOUT_MS` so a forgotten
 * armed button cannot be triggered later by a stray click.
 *
 * **Arming is not the only guard.** A confirm step stops a mis-click on THIS
 * button; it does nothing about a mis-click aimed at the button beside it. That
 * is why callers place this away from the primary action rather than next to
 * it -- the two protections are against different mistakes and neither
 * substitutes for the other.
 */
const ARM_TIMEOUT_MS = 5000;

const DELETE_SIZES: Record<"sm" | "md", string> = {
  sm: "min-h-11 px-2.5 py-2 text-xs",
  md: "min-h-11 px-3 py-2 text-sm",
};

export function ConfirmDeleteButton({
  testId,
  label = "Delete",
  confirmLabel = "Click again to confirm",
  accessibleLabel,
  accessibleConfirmLabel,
  busy = false,
  size = "md",
  onConfirm,
}: {
  testId: string;
  label?: string;
  confirmLabel?: string;
  accessibleLabel?: string;
  accessibleConfirmLabel?: string;
  busy?: boolean;
  size?: "sm" | "md";
  onConfirm: () => void;
}) {
  const [armed, setArmed] = useState(false);

  useEffect(() => {
    if (!armed) return;
    const timer = window.setTimeout(() => setArmed(false), ARM_TIMEOUT_MS);
    return () => window.clearTimeout(timer);
  }, [armed]);

  return (
    <button
      type="button"
      data-testid={testId}
      disabled={busy}
      aria-label={
        armed
          ? (accessibleConfirmLabel ?? confirmLabel)
          : (accessibleLabel ?? label)
      }
      onClick={() => {
        if (armed) {
          setArmed(false);
          onConfirm();
        } else {
          setArmed(true);
        }
      }}
      className={`rounded-md border font-medium transition disabled:opacity-50 ${
        DELETE_SIZES[size]
      } ${
        armed
          ? "border-rose-500 bg-rose-600 text-white hover:bg-rose-500"
          : "border-slate-800 bg-slate-900 text-slate-400 hover:border-rose-800 hover:text-rose-300"
      }`}
    >
      {busy ? "Deleting…" : armed ? confirmLabel : label}
    </button>
  );
}
