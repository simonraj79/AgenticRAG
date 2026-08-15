/**
 * The handful of primitives every view shares.
 *
 * Kept in one file rather than one-file-per-component because each is a dozen
 * lines and splitting them would make the import list longer than the code.
 */

import { useEffect, useState } from "react";
import type { ReactNode } from "react";

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
