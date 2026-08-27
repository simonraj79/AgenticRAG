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
 *
 * Everything visual here now comes from `lib/styles.ts` and the token layer in
 * `index.css`. No component in this file names a colour.
 */

import { useEffect, useState } from "react";
import type { ReactNode, RefObject } from "react";
import {
  BAD_TONE,
  BTN_DANGER,
  BTN_SECONDARY,
  BTN_SM,
  CARD_EMPTY,
  FIELD,
  FOCUS_PROXY,
  HELP,
  NEUTRAL_TONE,
  NOTICE,
  OK_TONE,
  PILL,
  PILL_NEUTRAL,
  WARN_TONE,
} from "../lib/styles.ts";

// --------------------------------------------------------------------------
// Feedback
// --------------------------------------------------------------------------

export function Spinner({ label }: { label?: string }) {
  return (
    <span className="inline-flex items-center gap-2 text-sm text-muted">
      <span
        className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-line-strong border-t-accent"
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
      className={`${NOTICE} ${BAD_TONE} text-sm whitespace-pre-wrap`}
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
    <div className={`${CARD_EMPTY} px-6 py-10 text-center`}>
      <p className="text-sm font-medium text-ink">{title}</p>
      {detail && <p className="mx-auto mt-1.5 max-w-md text-xs text-muted">{detail}</p>}
      {children && <div className="mt-5 flex justify-center">{children}</div>}
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
 *
 * A STATE, so it is a filled tinted pill. See the note on `PILL` in
 * `lib/styles.ts` for why that is a contract rather than a look: the taxonomy
 * badges below deliberately do NOT fill, which is what lets both families reuse
 * hues without becoming ambiguous.
 */
const STATUS_TONES: Record<string, string> = {
  ready: OK_TONE,
  indexed: OK_TONE,
  indexing: WARN_TONE,
  processing: WARN_TONE,
  pending: NEUTRAL_TONE,
  empty: NEUTRAL_TONE,
  failed: BAD_TONE,
};

export function StatusPill({ status }: { status: string }) {
  return (
    <span className={`${PILL} ${STATUS_TONES[status] ?? NEUTRAL_TONE}`}>{status}</span>
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
  sm: "h-8 w-8 text-base",
  md: "h-10 w-10 text-lg",
  lg: "h-12 w-12 text-xl",
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
      className={`inline-flex shrink-0 items-center justify-center rounded-md border border-line bg-sunken leading-none ${ICON_SIZES[size]}`}
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
 *
 * TAXONOMY, so: a neutral pill with a coloured dot, never a filled one. That is
 * what lets `assess` sit on a gold that is near the `warn` state and `reflect`
 * on a teal near `ok` without either being ambiguous -- the shapes differ, so
 * the hues are free to be close. Before this split, `border-amber-800/60  (palette-check: ignore -- quoting the old design, not a class)
 * bg-amber-950/40 text-amber-300` meant the `indexing` status AND the `assess`  (palette-check: ignore -- quoting the old design, not a class)
 * category AND `ai_suggested` provenance AND a `running` eval, all at once.
 */
const CATEGORY_LABELS: Record<string, string> = {
  explain: "Explain",
  practice: "Practice",
  assess: "Assess",
  reflect: "Reflect",
  general: "General",
};

const CATEGORY_DOTS: Record<string, string> = {
  explain: "bg-cat-explain",
  practice: "bg-cat-practice",
  assess: "bg-cat-assess",
  reflect: "bg-cat-reflect",
  general: "bg-faint",
};

export function CategoryBadge({ category }: { category?: string | null }) {
  const key = category ?? "";
  const label = CATEGORY_LABELS[key] ?? "ungrouped";
  const dot = CATEGORY_DOTS[key] ?? "bg-faint";
  return (
    <span className={`${PILL_NEUTRAL} text-[0.6875rem] tracking-[0.04em] uppercase`}>
      <span aria-hidden="true" className={`h-1.5 w-1.5 shrink-0 rounded-full ${dot}`} />
      {label}
    </span>
  );
}

// --------------------------------------------------------------------------
// Data display
// --------------------------------------------------------------------------

/**
 * One label/value pair inside a `<dl>`. Used by every parameter grid.
 *
 * **`help` exists because the mode nobody has to opt into was the only one that
 * explained nothing.** The wizard's tuning step has two modes, and the one the
 * user LANDS in renders all ten parameters through this component -- so every
 * sentence of explanation in that step is attached to `ParamSlider`, which only
 * appears once you have opted into customising. Read the flow end to end and
 * the default path shows ten database column names and their values and never
 * says what one of them is. The slot was missing, so the copy had nowhere to go
 * and was simply absent; nothing about that reads as a defect in the markup.
 *
 * A second `<dd>` rather than a sentence folded into the first, and that is a
 * correctness point rather than a style one: a `<dt>` may carry more than one
 * `<dd>`, and "what the value is" and "what it means" genuinely are two
 * descriptions of one term. Putting the prose inside the value's `<dd>` would
 * also put it inside `font-mono`, which is this app's measurement face.
 *
 * Optional, because the other caller is not a parameter grid at all --
 * `AgentSettingsSheet`'s "Fixed for the life of this agent" block states four
 * facts ABOUT the agent (persona, category, document count, embedding model),
 * and a sentence under each would be padding rather than teaching.
 */
export function Fact({
  label,
  tag,
  value,
  raw,
  help,
}: {
  label: string;
  /** The real column name, kept beside the plain-English label rather than
   *  replaced by it.
   *
   *  This product exists to TEACH retrieval, so a user who reads "Shortlist
   *  size" here and meets `retrieve_k` in the trace panel, in EVAL.md or in
   *  the API has been handed two vocabularies and told about one. The tag is
   *  the join between them. Quiet, because it is the second thing to read --
   *  never the first, which is what it used to be. */
  tag?: string;
  value: string | number;
  /** The stored value behind the displayed one.
   *
   *  `value` is what the user reads and is formatted -- "400 tokens", "At
   *  headings". This is what is actually on the column, published the same way
   *  `ParamSlider` publishes `data-value`, so a harness can assert that what
   *  the review step SHOWS is what the row RECORDS without having to reverse
   *  the display copy. A check that string-matches formatted text is a check
   *  that breaks the next time a unit is pluralised, and it fails in the
   *  direction that looks like a data bug. */
  raw?: string | number | boolean;
  /** One sentence, sitting under the value. See the note above. */
  help?: ReactNode;
}) {
  return (
    <div data-tunable={tag} data-value={raw === undefined ? undefined : String(raw)}>
      <dt className="text-xs text-muted">
        {label}
        {tag && (
          <span className="ml-1.5 font-mono text-[0.6875rem] text-faint">{tag}</span>
        )}
      </dt>
      <dd className="mt-0.5 font-mono text-sm text-ink">{value}</dd>
      {/*
        A `data-testid` rather than a positional selector, and deliberately not
        a unique one -- `querySelectorAll` is the intended read. A harness
        asserting "every parameter in this mode carries a visible explanation"
        has to find the explanations without knowing how many columns the grid
        resolved to, and the grid's shape is exactly what the container-query
        rework is changing underneath it.
      */}
      {help && (
        <dd data-testid="fact-help" className={`mt-1 ${HELP}`}>
          {help}
        </dd>
      )}
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
 *
 * **It must stay a `<details>`/`<summary>` pair.** `HandoutCard.test.tsx`
 * reaches through `reveal.querySelector("summary")` in four cases, and
 * `useFocusTrap`'s `FOCUSABLE` list names `summary` so a disclosure inside a
 * drawer stays reachable. A custom accordion breaks both.
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
    <details data-testid={testId} className="group rounded-md border border-line bg-surface">
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
      <summary className="flex min-h-11 cursor-pointer list-none items-center gap-2 px-3.5 py-2.5 text-[0.6875rem] font-semibold tracking-[0.08em] text-faint uppercase transition select-none hover:text-ink">
        <span
          className="inline-block text-sm transition group-open:rotate-90"
          aria-hidden="true"
        >
          &rsaquo;
        </span>
        {summary}
      </summary>
      <div className="border-t border-line px-3.5 py-4">{children}</div>
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
  tag,
  help,
  detail,
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
  /** The real column name, kept beside the label. See the note on `Fact`. */
  tag?: string;
  help: ReactNode;
  /**
   * The measured facts that used to be crammed into `help`, moved down one
   * tier rather than deleted.
   *
   * The longest `help` string on the tuning step is 296 characters, which is a
   * paragraph pretending to be a caption -- and a caption nobody reads is a
   * worse outcome than a short one plus a disclosure, because the numbers in
   * those strings (830 ms of rerank latency, the 0.61-0.67 / 0.49-0.58 score
   * bands) are the only place this product states what it measured. Nothing is
   * dropped; it stops being the first thing in the way.
   */
  detail?: ReactNode;
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
      {/* `min-w-0` on the label: a flex item's automatic minimum size is its
          min-content, so a long label plus its mono tag refuses to wrap and
          shoves the number field out of the row instead. */}
      <div className="flex items-baseline justify-between gap-3">
        <label className="min-w-0 text-sm font-medium text-ink" htmlFor={`${id}-range`}>
          {label}
          {tag && (
            <span className="ml-1.5 font-mono text-[0.6875rem] font-normal text-faint">
              {tag}
            </span>
          )}
        </label>
        {/*
          The width lives on a WRAPPER, and that is a bug fix rather than a
          nesting preference.

          `FIELD` carries `w-full`. Written as `${FIELD} w-24`, the two are
          width utilities of EQUAL specificity, so which one wins is decided by
          their order in the generated stylesheet and not by the order of the
          string -- and `w-full` was winning. Measured at a 320px viewport: this
          input rendered 242px wide instead of 96px and hung 152px past its own
          column. At 1440px it was the same defect with more room to hide in,
          which is what put the value "800" on top of the neighbouring
          "Overlap" label.

          This repo has the identical trap written down for `contents` vs
          `hidden`. The lesson there is the lesson here: do not try to out-rank
          a utility whose specificity you tie, because the fix is invisible in
          the class list and one refactor from silently reverting. Remove the
          conflict instead -- inside a 6rem box, `w-full` IS 6rem.
        */}
        <span className="w-24 shrink-0">
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
            className={`${FIELD} px-2 text-right font-mono disabled:opacity-50`}
          />
        </span>
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
        className="gw-range mt-0.5"
      />

      <p id={`${id}-help`} className={HELP}>
        {help}
      </p>

      {warning && (
        <p
          id={`${id}-warning`}
          data-testid={`param-${id}-warning`}
          className={`${NOTICE} ${WARN_TONE} mt-2`}
        >
          {warning}
        </p>
      )}

      {/*
        Below the warning, not between it and the help text, and the ordering is
        load-bearing. The warning is about the value the slider is on RIGHT NOW
        -- `rerank_top_n` above `retrieve_k`, an overlap at half the chunk size
        -- so it has to stay within a glance of the control that caused it. A
        collapsed disclosure between them is only 44px of separation while shut
        and an unbounded amount once opened, which would push a live warning off
        screen at exactly the moment the user is reading about the parameter.

        Deliberately NOT joined to `aria-describedby`. The point of moving this
        material down a tier is that it is not read on the way past; wiring it
        into the description would have a screen reader announce the whole
        paragraph on every focus, which is the 296-character caption again with
        extra steps. It is a `<summary>`, so it is in the tab order and reachable
        on purpose -- see the note on `Reveal`, and `focusableWithin`, which
        keeps Tab out of it while it is shut.
      */}
      {detail && (
        // The wrapper carries the spacing because `Reveal` takes no
        // `className` -- one fixed look for every disclosure in the app is the
        // reason it is a primitive, and a margin is not a reason to open that.
        <div className="mt-2">
          <Reveal summary="Why this matters" testId={`param-${id}-detail`}>
            <div className={HELP}>{detail}</div>
          </Reveal>
        </div>
      )}

      {decimals > 0 && <span className="sr-only">{value.toFixed(decimals)}</span>}
    </div>
  );
}

/**
 * A two-option segmented control. Two radios that look like one switch.
 *
 * **`options` already separates what is STORED from what is READ** -- each
 * option is `{ value, label }`, the value goes on the wire and the label goes
 * on screen. Nothing in this component needs changing to say *At headings*
 * while sending `"markdown"`; a caller rendering a raw column value is passing
 * it as its own label, which is a call-site decision, not a limitation here.
 *
 * Two things this component got wrong, both fixed below, both of the shape this
 * repo keeps rediscovering -- the markup looks right and the thing you wanted
 * did not happen:
 *
 * **The help text was orphaned.** Nine instances across two files rendered a
 * sentence under the control that no assistive technology could connect to it:
 * the `<p>` had no `id` and nothing referenced it. `ParamSlider` had done this
 * correctly since it was written, which is exactly why it went unnoticed here
 * -- the two controls sit in the same column, look equally finished, and only
 * one of them is described.
 *
 * **The focus ring was painted on a clipped box.** See `FOCUS_PROXY`: the real
 * radio is `sr-only`, so the global `:focus-visible` outline was landing on a
 * 1px `clip: rect(0,0,0,0)` element and a keyboard user could not see which
 * option they were on.
 */
export function Segmented({
  legend,
  tag,
  help,
  detail,
  name,
  value,
  options,
  onChange,
  disabled = false,
  testId,
}: {
  legend: string;
  /** The real column name, kept beside the legend. See the note on `Fact`. */
  tag?: string;
  help: ReactNode;
  /** The second tier, for parity with `ParamSlider` -- see the note on `detail`
   *  there. A `Segmented` legend is two words and its help one sentence, so
   *  anything measured about the option (what reranking costs, what the
   *  splitter does to a heading) has the same nowhere-to-go problem. */
  detail?: ReactNode;
  name: string;
  value: string;
  /** `value` is stored, `label` is displayed. They are allowed to differ, and
   *  on this step they should: the column says `"markdown"`, the user reads
   *  *At headings*. */
  options: { value: string; label: string }[];
  onChange: (next: string) => void;
  disabled?: boolean;
  testId: string;
}) {
  /*
    Derived from `name` rather than from a new `id` prop, because `name` is
    already required to be unique: it is the radio group's name, and two groups
    sharing it would be one group and a bug well before it was an id collision.
  */
  const helpId = `${name}-help`;

  return (
    /*
      `aria-describedby` on the FIELDSET, not on each radio. The sentence
      describes the group -- what reranking is, what the splitter does -- not
      the option under the cursor, and a description on each input is
      re-announced on every arrow-key step through the group. `ParamSlider`
      puts it on the control because there the control IS the group; here the
      `<fieldset>`/`<legend>` pair is what maps to a group, so that is what
      carries the description.
    */
    <fieldset
      data-testid={testId}
      data-value={value}
      disabled={disabled}
      aria-describedby={helpId}
    >
      <legend className="text-sm font-medium text-ink">
        {legend}
        {tag && (
          <span className="ml-1.5 font-mono text-[0.6875rem] font-normal text-faint">
            {tag}
          </span>
        )}
      </legend>
      <div className="mt-2 inline-flex rounded-md border border-line-strong bg-sunken p-1">
        {options.map((option) => {
          const active = option.value === value;
          return (
            <label
              key={option.value}
              data-testid={`${testId}-${option.value}`}
              data-selected={active}
              className={`flex min-h-11 cursor-pointer items-center rounded-sm px-3.5 text-sm font-medium transition ${FOCUS_PROXY} ${
                active
                  ? "bg-surface text-ink shadow-xs"
                  : "text-muted hover:text-ink"
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
      <p id={helpId} className={`${HELP} mt-2`}>
        {help}
      </p>

      {/* Same tier and same reasoning as `ParamSlider`'s, minus the ordering
          argument -- a `Segmented` has no value-driven warning to stay next
          to, so the disclosure simply follows the help text. */}
      {detail && (
        <div className="mt-2">
          <Reveal summary="Why this matters" testId={`${testId}-detail`}>
            <div className={HELP}>{detail}</div>
          </Reveal>
        </div>
      )}
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
 *
 * Resting state is an ordinary secondary button that turns red only on hover,
 * and the filled red belongs exclusively to the ARMED state. A delete that is
 * red before it has been confirmed teaches people to click through red.
 */
const ARM_TIMEOUT_MS = 5000;

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
      className={`${armed ? BTN_DANGER : `${BTN_SECONDARY} text-muted hover:border-bad-line hover:bg-bad-soft hover:text-bad`} ${
        size === "sm" ? BTN_SM : ""
      }`}
    >
      {busy ? "Deleting…" : armed ? confirmLabel : label}
    </button>
  );
}
