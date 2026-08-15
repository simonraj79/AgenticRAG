/**
 * Documents tab: upload a file, watch it become chunks, delete it again.
 *
 * **Ingest is asynchronous.** `POST /api/agents/{id}/documents` now answers
 * **202 Accepted** with a `pending` row and does the loading, chunking,
 * embedding and upserting afterwards, moving the row pending -> processing ->
 * ready | failed. The upload request therefore returns in milliseconds and
 * finishing is something the client has to observe rather than await.
 *
 * That makes the polling below the load-bearing part of this file, and it has
 * three rules that are easy to get wrong:
 *
 * 1. **It starts only when something is unsettled** and stops the moment
 *    nothing is. A permanent `setInterval` would keep a tab that nobody is
 *    looking at hitting the API every few seconds for as long as the browser is
 *    open, which is the version of this feature that gets noticed on a bill.
 * 2. **It backs off.** A 50 MB PDF is minutes of embedding calls; asking every
 *    second for four minutes is 240 requests to learn one fact.
 * 3. **It gives up and says so.** An ingest that dies without writing `failed`
 *    would otherwise poll until the tab closes, looking busy forever.
 *
 * Deleting a document removes its vectors from the namespace as well as its
 * rows, which is why it confirms.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../lib/api.ts";
import type { DocumentRow } from "../lib/types.ts";
import { formatBytes, formatTimestamp } from "../lib/format.ts";
import {
  ConfirmDeleteButton,
  EmptyState,
  ErrorBanner,
  Spinner,
  StatusPill,
  errorMessage,
} from "../components/ui.tsx";

/** Mirrors `app.rag.ingest.SUPPORTED_SUFFIXES`. Advisory only -- the backend is
 *  the authority and rejects anything else with a message naming the set. */
const ACCEPTED = ".md,.markdown,.txt,.pdf";

/** Mirrors `app.api.documents.MAX_UPLOAD_BYTES`. Checked here as well so a
 *  rejection costs a message instead of a multi-minute upload, but the server
 *  is what enforces it -- a client-side limit is a courtesy, not a control. */
const MAX_UPLOAD_BYTES = 50 * 1024 * 1024;

/**
 * The statuses that mean "this row has stopped moving".
 *
 * `indexed` is here alongside `ready` deliberately: it is the value the
 * synchronous ingest path wrote, and rows carrying it exist in databases that
 * predate the background job. Omitting it would leave those rows looking
 * unsettled forever, and the poll loop below would never terminate on a corpus
 * uploaded last week.
 */
const TERMINAL_DOCUMENT_STATUSES = new Set(["ready", "indexed", "failed"]);

const POLL_FIRST_MS = 1_200;
const POLL_MAX_MS = 8_000;
const POLL_GROWTH = 1.5;
/** Long, because the ceiling it guards against is a 50 MB PDF's worth of
 *  embedding calls, not a slow network. */
const POLL_GIVE_UP_MS = 10 * 60 * 1_000;

export default function AgentDocuments({
  agentId,
  onCorpusChanged,
}: {
  agentId: string;
  onCorpusChanged: () => void;
}) {
  const [documents, setDocuments] = useState<DocumentRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [stalled, setStalled] = useState(false);
  /** Bumped to restart the poll loop after it has given up. Without it, "Check
   *  again" would clear the stall notice and refresh once, leaving the list
   *  claiming to refresh itself while nothing was scheduled to. */
  const [pollEpoch, setPollEpoch] = useState(0);

  const [file, setFile] = useState<File | null>(null);
  const [dragging, setDragging] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Returns the rows as well as storing them, so the poll loop can decide
  // whether to schedule another tick without waiting a render for state.
  const load = useCallback(async () => {
    const rows = await api<DocumentRow[]>(`/api/agents/${agentId}/documents`);
    setDocuments(rows);
    return rows;
  }, [agentId]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    load()
      .catch((cause: unknown) => {
        if (!cancelled) setError(errorMessage(cause));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [load]);

  const settlingCount = documents.filter(
    (doc) => !TERMINAL_DOCUMENT_STATUSES.has(doc.status),
  ).length;
  const settling = settlingCount > 0;

  /**
   * The poll loop.
   *
   * Depends on `settling` as a BOOLEAN rather than on `documents`: the array
   * gets a new identity on every tick, so depending on it would tear the effect
   * down and rebuild it each time, resetting the backoff to its shortest
   * interval and turning the whole thing back into a fixed 1.2 s interval. The
   * boolean only changes when the answer to "is anything still moving" changes,
   * which is exactly when this loop should start or stop.
   *
   * `onCorpusChanged` fires once, on the transition to fully settled, because
   * that is when the agent's own `status` and chunk totals change. Calling it
   * every tick would refetch the parent's agent record several times to learn
   * nothing.
   */
  useEffect(() => {
    if (!settling) return;

    setStalled(false);
    let cancelled = false;
    let timer = 0;
    let delay = POLL_FIRST_MS;
    const deadline = Date.now() + POLL_GIVE_UP_MS;

    const tick = async () => {
      if (cancelled) return;
      try {
        const rows = await load();
        if (cancelled) return;
        if (rows.every((doc) => TERMINAL_DOCUMENT_STATUSES.has(doc.status))) {
          onCorpusChanged();
          return;
        }
      } catch {
        // Swallowed on purpose, and only here. A failed poll is not a failed
        // ingest -- the server is still working -- so replacing the page's
        // error banner with a transient network blip would report the wrong
        // problem. Persistent failure surfaces as the stall notice below.
      }
      if (cancelled) return;
      if (Date.now() >= deadline) {
        setStalled(true);
        return;
      }
      delay = Math.min(Math.round(delay * POLL_GROWTH), POLL_MAX_MS);
      timer = window.setTimeout(() => void tick(), delay);
    };

    timer = window.setTimeout(() => void tick(), delay);

    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [settling, pollEpoch, load, onCorpusChanged]);

  async function upload() {
    if (!file) {
      setError("Choose a file first.");
      return;
    }
    if (file.size > MAX_UPLOAD_BYTES) {
      setError(
        `${file.name} is ${formatBytes(file.size)}. The limit is ` +
          `${MAX_UPLOAD_BYTES / (1024 * 1024)} MB.`,
      );
      return;
    }

    setUploading(true);
    setError(null);
    try {
      const form = new FormData();
      // The field name is "file" and the backend reads exactly that.
      form.append("file", file);
      // No Content-Type header: the browser must set it itself so the multipart
      // boundary matches the body it generated. See lib/api.ts.
      await api<DocumentRow>(`/api/agents/${agentId}/documents`, {
        method: "POST",
        body: form,
      });
      setFile(null);
      // The <input> keeps its own FileList independently of React state, so
      // clearing state alone would leave the filename showing in the control
      // after a successful upload.
      if (fileInputRef.current) fileInputRef.current.value = "";
      // The row lands as `pending`; storing it starts the poll loop above. The
      // parent is told now because the document COUNT is already correct --
      // only the status and chunk total are still in flight.
      await load();
      onCorpusChanged();
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setUploading(false);
    }
  }

  async function remove(documentId: string) {
    setDeletingId(documentId);
    setError(null);
    try {
      await api(`/api/agents/${agentId}/documents/${documentId}`, { method: "DELETE" });
      await load();
      onCorpusChanged();
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setDeletingId(null);
    }
  }

  async function refreshNow() {
    setError(null);
    try {
      await load();
      onCorpusChanged();
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      // Last, and unconditionally: if anything is still unsettled this restarts
      // the loop with a fresh deadline, and if everything has settled the
      // effect's own guard keeps it stopped.
      setPollEpoch((epoch) => epoch + 1);
    }
  }

  return (
    <div className="space-y-6">
      <div
        onDragOver={(event) => {
          // Both handlers must preventDefault or the browser navigates to the
          // dropped file instead of firing onDrop -- and it does so on
          // dragover, not on drop, which is the counter-intuitive half.
          event.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => {
          event.preventDefault();
          setDragging(false);
          const dropped = event.dataTransfer.files[0];
          if (dropped) setFile(dropped);
        }}
        className={`rounded-xl border-2 border-dashed p-6 transition ${
          dragging ? "border-emerald-500 bg-emerald-950/20" : "border-slate-800 bg-slate-900/40"
        }`}
      >
        <p className="text-sm text-slate-300">Drop a file here, or pick one:</p>

        {/*
          A real, visible file input rather than a hidden one behind a styled
          button. Hidden inputs are awkward for keyboard users and invisible to
          automation that sets files on the element directly.
        */}
        <input
          ref={fileInputRef}
          data-testid="doc-file-input"
          type="file"
          accept={ACCEPTED}
          onChange={(event) => setFile(event.target.files?.[0] ?? null)}
          className="mt-3 block w-full text-sm text-slate-400 file:mr-3 file:rounded-md file:border-0 file:bg-slate-100 file:px-3 file:py-1.5 file:text-sm file:font-medium file:text-slate-900 hover:file:bg-white"
        />

        <p className="mt-2 text-xs text-slate-400">
          Markdown, plain text or PDF, up to {MAX_UPLOAD_BYTES / (1024 * 1024)} MB. Original
          files are not stored — text is chunked into Postgres and embedded into this
          agent&rsquo;s namespace.
        </p>

        <div className="mt-4 flex items-center gap-3">
          <button
            type="button"
            data-testid="doc-upload-submit"
            disabled={uploading || !file}
            onClick={() => void upload()}
            className="min-h-11 rounded-md bg-emerald-500 px-4 py-2 text-sm font-semibold text-emerald-950 transition hover:bg-emerald-400 disabled:opacity-50"
          >
            {uploading ? "Uploading…" : "Upload and index"}
          </button>
          {file && !uploading && (
            <span className="text-xs text-slate-400">
              {file.name} · {formatBytes(file.size)}
            </span>
          )}
          {uploading && <Spinner label="Sending — indexing then continues in the background" />}
        </div>
      </div>

      <ErrorBanner error={error} />

      <div>
        <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
          <h3 className="text-sm font-medium tracking-wide text-slate-400 uppercase">
            Corpus ({documents.length})
          </h3>

          {settling && !stalled && (
            <Spinner
              label={`Indexing ${settlingCount} of ${documents.length} — this list refreshes itself`}
            />
          )}

          {/* Both notices are gated on `settling`, so a stall that resolves
              itself takes its own warning off the screen rather than leaving a
              worry about a corpus that is in fact finished. */}
          {settling && stalled && (
            <div className="flex items-center gap-3 text-xs text-amber-300">
              <span>Stopped refreshing after 10 minutes.</span>
              <button
                type="button"
                data-testid="doc-refresh"
                onClick={() => void refreshNow()}
                className="min-h-11 rounded-md border border-slate-700 bg-slate-900 px-2.5 py-1 font-medium text-slate-300 transition hover:border-slate-600"
              >
                Check again
              </button>
            </div>
          )}
        </div>

        {loading && <Spinner label="Loading documents" />}

        {!loading && documents.length === 0 && (
          <EmptyState
            title="No documents yet."
            detail="This agent will refuse every question until it has some — which is the correct behaviour, not a bug."
          />
        )}

        {/*
          The same six facts, rendered twice: a card list below `sm`, the table
          at `sm` and up.

          That duplication is the honest cost of a table on a phone, and it is
          cheaper than the alternatives. Six columns at 375px do not reflow --
          they scroll sideways, and the delete button, being last, sits off the
          right edge until the user discovers a horizontal scroll they were
          given no affordance for. Turning the rows into blocks with CSS keeps
          one copy of the markup and produces a `<table>` whose semantics no
          longer match what is on screen, with the header row either repeated
          per cell or lost. So: two blocks, one source of data, and the pair
          must be edited together -- a column added to one and not the other is
          a fact the phone silently does not show.

          The card list carries its OWN test ids rather than reusing the row
          ones. Both copies are always in the DOM (one is display:none, which a
          locator still matches), so sharing `doc-row` would make every
          `getByTestId("doc-row")` resolve to two elements and throw under
          Playwright's strict mode.
        */}
        {documents.length > 0 && (
          <ul data-testid="doc-card-list" className="space-y-2 sm:hidden">
            {documents.map((doc) => (
              <li
                key={doc.id}
                data-testid="doc-card"
                data-document-id={doc.id}
                className="rounded-lg border border-slate-800 bg-slate-900/40 p-3"
              >
                <div className="flex items-start justify-between gap-3">
                  <span className="min-w-0 flex-1 break-words text-sm text-slate-200">
                    {doc.filename}
                  </span>
                  <span data-testid="doc-card-status" data-status={doc.status}>
                    <StatusPill status={doc.status} />
                  </span>
                </div>

                {doc.error && (
                  <p
                    data-testid="doc-card-error"
                    className="mt-2 text-xs whitespace-pre-wrap text-rose-300"
                  >
                    {doc.error}
                  </p>
                )}

                <p className="mt-2 text-xs text-slate-400">
                  {doc.chunk_count} {doc.chunk_count === 1 ? "chunk" : "chunks"} ·{" "}
                  {formatBytes(doc.byte_size)} · {formatTimestamp(doc.created_at)}
                </p>

                {/* First-class on the card rather than tucked into a corner:
                    on the narrow viewport this is the only delete there is. */}
                <div className="mt-3">
                  <ConfirmDeleteButton
                    testId="doc-card-delete"
                    label="Delete"
                    confirmLabel="Delete + drop vectors?"
                    size="sm"
                    busy={deletingId === doc.id}
                    onConfirm={() => void remove(doc.id)}
                  />
                </div>
              </li>
            ))}
          </ul>
        )}

        {documents.length > 0 && (
          <div className="hidden overflow-x-auto rounded-lg border border-slate-800 sm:block">
            <table className="w-full text-left text-sm">
              <thead className="bg-slate-900/60 text-xs tracking-wide text-slate-400 uppercase">
                <tr>
                  <th className="px-4 py-2 font-medium">Filename</th>
                  <th className="px-4 py-2 font-medium">Status</th>
                  <th className="px-4 py-2 font-medium">Chunks</th>
                  <th className="px-4 py-2 font-medium">Size</th>
                  <th className="px-4 py-2 font-medium">Uploaded</th>
                  <th className="px-4 py-2" />
                </tr>
              </thead>
              <tbody>
                {documents.map((doc) => (
                  <tr
                    key={doc.id}
                    data-testid="doc-row"
                    data-document-id={doc.id}
                    className="border-t border-slate-800"
                  >
                    <td className="px-4 py-3">
                      <span className="text-slate-200">{doc.filename}</span>
                      {/*
                        The reason a row failed, in the row that failed. Ingest
                        runs out of band now, so this string is the only account
                        the user gets of what went wrong -- an unsupported
                        encoding, a PDF with no extractable text, a provider
                        rejecting the batch. Hiding it behind a status pill
                        would turn a fixable problem into a mystery.
                      */}
                      {doc.error && (
                        <p
                          data-testid="doc-error"
                          className="mt-1 max-w-md text-xs whitespace-pre-wrap text-rose-300"
                        >
                          {doc.error}
                        </p>
                      )}
                    </td>
                    <td className="px-4 py-3" data-testid="doc-status" data-status={doc.status}>
                      <StatusPill status={doc.status} />
                    </td>
                    <td className="px-4 py-3 text-slate-300">{doc.chunk_count}</td>
                    <td className="px-4 py-3 text-slate-400">{formatBytes(doc.byte_size)}</td>
                    <td className="px-4 py-3 text-xs text-slate-400">
                      {formatTimestamp(doc.created_at)}
                    </td>
                    <td className="px-4 py-3 text-right">
                      <ConfirmDeleteButton
                        testId="doc-delete"
                        label="Delete"
                        confirmLabel="Delete + drop vectors?"
                        size="sm"
                        busy={deletingId === doc.id}
                        onConfirm={() => void remove(doc.id)}
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
