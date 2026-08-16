/**
 * Documents tab: upload a file, watch it become chunks, delete it again.
 *
 * **This file is presentation only.** Fetching the list, polling it while ingest
 * runs in the background, uploading and deleting all live in
 * `lib/useAgentDocuments.ts`, because the same corpus is also rendered in a
 * narrow rail beside the chat and two copies of that machinery would be two
 * copies of a backoff timer. The reasoning behind the poll -- why it starts,
 * backs off and gives up -- is documented there, next to the code it governs.
 *
 * What is left here is the part that is genuinely about this screen: a
 * drop zone, and the same six facts rendered twice so a phone gets cards and a
 * laptop gets a table.
 *
 * Deleting a document removes its vectors from the namespace as well as its
 * rows, which is why it confirms.
 */

import { useRef, useState } from "react";
import { formatBytes, formatTimestamp } from "../lib/format.ts";
import {
  ACCEPTED_UPLOAD_TYPES,
  MAX_UPLOAD_BYTES,
  useAgentDocuments,
} from "../lib/useAgentDocuments.ts";
import {
  ConfirmDeleteButton,
  EmptyState,
  ErrorBanner,
  Spinner,
  StatusPill,
} from "../components/ui.tsx";

export default function AgentDocuments({
  agentId,
  onCorpusChanged,
}: {
  agentId: string;
  onCorpusChanged: () => void;
}) {
  const {
    documents,
    loading,
    error,
    setError,
    refresh,
    upload,
    remove,
    uploading,
    deletingId,
    indexing,
    indexingCount,
    stalled,
  } = useAgentDocuments(agentId, onCorpusChanged);

  const [file, setFile] = useState<File | null>(null);
  const [dragging, setDragging] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  /**
   * The picker's half of an upload: what to send, and what to reset afterwards.
   *
   * The empty-selection check stays here rather than in the hook because it is a
   * fact about this form -- the hook is handed a `File` and has nothing to
   * complain about. The reset is gated on the hook's return value for the same
   * reason it cannot read `error`: that state is one render behind.
   */
  async function submitUpload() {
    if (!file) {
      setError("Choose a file first.");
      return;
    }
    if (!(await upload(file))) return;
    setFile(null);
    // The <input> keeps its own FileList independently of React state, so
    // clearing state alone would leave the filename showing in the control
    // after a successful upload.
    if (fileInputRef.current) fileInputRef.current.value = "";
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
          accept={ACCEPTED_UPLOAD_TYPES}
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
            onClick={() => void submitUpload()}
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

          {indexing && !stalled && (
            <Spinner
              label={`Indexing ${indexingCount} of ${documents.length} — this list refreshes itself`}
            />
          )}

          {/* Both notices are gated on `indexing`, so a stall that resolves
              itself takes its own warning off the screen rather than leaving a
              worry about a corpus that is in fact finished. */}
          {indexing && stalled && (
            <div className="flex items-center gap-3 text-xs text-amber-300">
              <span>Stopped refreshing after 10 minutes.</span>
              <button
                type="button"
                data-testid="doc-refresh"
                onClick={() => void refresh()}
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
