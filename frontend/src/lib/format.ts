/** Small display helpers. No logic lives here -- only rendering decisions. */

/** File sizes, as a human reads them. `null` renders as a dash, not "0 B",
 *  because "unknown" and "empty" are different facts about a document. */
export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return "—";
  if (bytes < 1024) return `${bytes} B`;

  const units = ["KB", "MB", "GB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(value < 10 ? 1 : 0)} ${units[unit]}`;
}

/**
 * Retrieval scores, to four places.
 *
 * Four, not two, because the numbers this project measured sit in a narrow
 * band -- on-topic questions scored 0.61-0.67 and off-topic ones 0.49-0.58,
 * with one off-topic question landing at 0.5765. Rounding to two decimals
 * would erase exactly the separation the Trace view exists to show.
 */
export function formatScore(score: number | null | undefined): string {
  if (score === null || score === undefined) return "—";
  return score.toFixed(4);
}

/** Milliseconds as seconds once the number stops being readable as ms. */
export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "—";
  if (ms < 1000) return `${ms} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
}

/** Timestamps in the viewer's locale. Invalid input renders as the raw string
 *  rather than "Invalid Date", which looks like a bug in the UI. */
export function formatTimestamp(value: string | null | undefined): string {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString();
}

/** JSONB payloads, indented. Anything non-serialisable falls back to String(). */
export function formatJson(value: unknown): string {
  if (value === null || value === undefined) return "";
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}
