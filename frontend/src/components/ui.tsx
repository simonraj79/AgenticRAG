/**
 * The handful of primitives every view shares.
 *
 * Kept in one file rather than one-file-per-component because each is a dozen
 * lines and splitting them would make the import list longer than the code.
 */

import { useEffect, useState } from "react";

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

// --------------------------------------------------------------------------
// Status
// --------------------------------------------------------------------------

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
// Destructive actions
// --------------------------------------------------------------------------

/**
 * Delete, behind a confirmation that is itself a button.
 *
 * Two design notes worth the space:
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
 */
const ARM_TIMEOUT_MS = 5000;

export function ConfirmDeleteButton({
  testId,
  label = "Delete",
  confirmLabel = "Click again to confirm",
  busy = false,
  onConfirm,
}: {
  testId: string;
  label?: string;
  confirmLabel?: string;
  busy?: boolean;
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
      aria-label={armed ? confirmLabel : label}
      onClick={() => {
        if (armed) {
          setArmed(false);
          onConfirm();
        } else {
          setArmed(true);
        }
      }}
      className={`rounded-md border px-3 py-1.5 text-sm font-medium transition disabled:opacity-50 ${
        armed
          ? "border-rose-500 bg-rose-600 text-white hover:bg-rose-500"
          : "border-slate-700 bg-slate-900 text-rose-300 hover:border-rose-800 hover:text-rose-200"
      }`}
    >
      {busy ? "Deleting…" : armed ? confirmLabel : label}
    </button>
  );
}
