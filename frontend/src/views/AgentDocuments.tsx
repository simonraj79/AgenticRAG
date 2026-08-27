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
import {
  BAD_TONE,
  BTN_PRIMARY,
  BTN_SECONDARY,
  BTN_SM,
  CARD,
  CARD_EMPTY,
  EYEBROW,
  HELP,
  NOTICE,
  WARN_TONE,
} from "../lib/styles.ts";
import DuplicatePrompt from "../components/DuplicatePrompt.tsx";

/**
 * The drag-active look SWAPS the resting string rather than appending to it.
 *
 * `border-line-strong` (inside `CARD_EMPTY`) and `border-accent` are both
 * border-colour utilities of equal specificity, so which one wins is decided by
 * their order in the GENERATED STYLESHEET rather than by their order in a
 * template literal -- the same coin-flip this codebase already documents for
 * `contents` / `hidden`. Two complete strings, one of which is `CARD_EMPTY`
 * unmodified, has no such ambiguity.
 */
const DROPZONE_ACTIVE = "rounded-lg border border-dashed border-accent bg-accent-soft";

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
    pendingDuplicate,
    confirmDuplicate,
    dismissDuplicate,
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
    resetPicker();
  }

  /**
   * Put the picker back to empty -- both halves of it.
   *
   * The <input> keeps its own FileList independently of React state, so clearing
   * state alone would leave the filename showing in the control. Clearing the
   * input matters for more than tidiness: an `<input type="file">` fires no
   * change event when the same file is picked twice, so a stale selection makes
   * re-picking that file look like a dead control.
   */
  function resetPicker() {
    setFile(null);
    if (fileInputRef.current) fileInputRef.current.value = "";
  }

  /**
   * The two answers to the duplicate prompt. The decision lives in the hook;
   * what is local here is the file input, which is a fact about this form.
   *
   * Both answers reset the picker, including Cancel: a "no" that leaves the file
   * selected leaves the form armed to ask the same question again, and leaves
   * the user in the no-change-event trap above if they try to re-pick it.
   */
  async function confirmDuplicateUpload() {
    if (await confirmDuplicate()) resetPicker();
  }

  function dismissDuplicateUpload() {
    dismissDuplicate();
    resetPicker();
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
        className={`${dragging ? DROPZONE_ACTIVE : CARD_EMPTY} p-6 transition`}
      >
        <p className="text-sm text-muted">Drop a file here, or pick one:</p>

        {/*
          A real, visible file input rather than a hidden one behind a styled
          button. Hidden inputs are awkward for keyboard users and invisible to
          automation that sets files on the element directly.

          The `file:` half is `BTN_PRIMARY`'s treatment respelled as pseudo-
          element variants -- ink fill, inverse label, the same radius, padding
          and type size. It cannot literally BE `BTN_PRIMARY`, because that
          string styles the element it sits on and this styles a pseudo-element
          inside it; what matters is that there is now ONE primary look on this
          screen rather than the two the audit found.
        */}
        <input
          ref={fileInputRef}
          data-testid="doc-file-input"
          type="file"
          accept={ACCEPTED_UPLOAD_TYPES}
          onChange={(event) => setFile(event.target.files?.[0] ?? null)}
          className="mt-3 block w-full text-sm text-muted file:mr-3 file:min-h-11 file:rounded-md file:border-0 file:bg-ink file:px-4 file:py-2 file:text-sm file:font-medium file:text-inverse file:transition hover:file:bg-ink-hover"
        />

        <p className={`${HELP} mt-2`}>
          Markdown, plain text or PDF, up to {MAX_UPLOAD_BYTES / (1024 * 1024)} MB. Text is
          chunked into Postgres and embedded into this agent&rsquo;s namespace; the original
          file is kept in private object storage.
        </p>

        <div className="mt-4 flex flex-wrap items-center gap-3">
          <button
            type="button"
            data-testid="doc-upload-submit"
            disabled={uploading || !file}
            onClick={() => void submitUpload()}
            className={BTN_PRIMARY}
          >
            {uploading ? "Uploading…" : "Upload and index"}
          </button>
          {file && !uploading && (
            <span className="min-w-0 text-xs text-muted">
              <span className="text-ink">{file.name}</span> ·{" "}
              <span className="font-mono tabular-nums">{formatBytes(file.size)}</span>
            </span>
          )}
          {uploading && <Spinner label="Sending — indexing then continues in the background" />}
        </div>
      </div>

      <ErrorBanner error={error} />

      {/* Beside the banner, never inside it: a duplicate is a question with an
          answer, not a failure to report, and `error` stays null while it is
          outstanding. */}
      {pendingDuplicate && (
        <DuplicatePrompt
          testId="doc-duplicate"
          message={pendingDuplicate.message}
          busy={uploading}
          onConfirm={() => void confirmDuplicateUpload()}
          onDismiss={dismissDuplicateUpload}
        />
      )}

      <div>
        <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
          <h3 className={EYEBROW}>Corpus ({documents.length})</h3>

          {indexing && !stalled && (
            <Spinner
              label={`Indexing ${indexingCount} of ${documents.length} — this list refreshes itself`}
            />
          )}

          {/* Both notices are gated on `indexing`, so a stall that resolves
              itself takes its own warning off the screen rather than leaving a
              worry about a corpus that is in fact finished. */}
          {indexing && stalled && (
            <div className={`${NOTICE} ${WARN_TONE} flex flex-wrap items-center gap-3`}>
              <span>Stopped refreshing after 10 minutes.</span>
              <button
                type="button"
                data-testid="doc-refresh"
                onClick={() => void refresh()}
                className={`${BTN_SECONDARY} ${BTN_SM}`}
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
                className={`${CARD} p-4`}
              >
                <div className="flex items-start justify-between gap-3">
                  <span className="min-w-0 flex-1 text-sm break-words text-ink">
                    {doc.filename}
                  </span>
                  <span data-testid="doc-card-status" data-status={doc.status}>
                    <StatusPill status={doc.status} />
                  </span>
                </div>

                {doc.error && (
                  <p
                    data-testid="doc-card-error"
                    className={`${NOTICE} ${BAD_TONE} mt-2 whitespace-pre-wrap`}
                  >
                    {doc.error}
                  </p>
                )}

                <p className="mt-2 text-xs text-muted">
                  <span className="font-mono tabular-nums">{doc.chunk_count}</span>{" "}
                  {doc.chunk_count === 1 ? "chunk" : "chunks"} ·{" "}
                  <span className="font-mono tabular-nums">{formatBytes(doc.byte_size)}</span> ·{" "}
                  {formatTimestamp(doc.created_at)}
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
          <div className={`${CARD} hidden overflow-x-auto sm:block`}>
            <table className="w-full text-left text-sm">
              <thead className="bg-sunken text-xs font-semibold text-faint">
                <tr>
                  <th className="px-4 py-2.5">Filename</th>
                  <th className="px-4 py-2.5">Status</th>
                  <th className="px-4 py-2.5">Chunks</th>
                  <th className="px-4 py-2.5">Size</th>
                  <th className="px-4 py-2.5">Uploaded</th>
                  <th className="px-4 py-2.5" />
                </tr>
              </thead>
              <tbody>
                {documents.map((doc) => (
                  <tr
                    key={doc.id}
                    data-testid="doc-row"
                    data-document-id={doc.id}
                    className="border-t border-line"
                  >
                    <td className="px-4 py-3">
                      {/* `truncate` needs a width to truncate AGAINST -- it is
                          `overflow-hidden` plus `whitespace-nowrap`, and a
                          table cell sizes to its content, so without a cap the
                          column simply grows and the ellipsis never appears.
                          `title` keeps the whole name reachable; the card list
                          above, which is the phone's copy, shows it in full. */}
                      <span title={doc.filename} className="block max-w-sm truncate text-ink">
                        {doc.filename}
                      </span>
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
                          className={`${NOTICE} ${BAD_TONE} mt-1.5 max-w-md whitespace-pre-wrap`}
                        >
                          {doc.error}
                        </p>
                      )}
                    </td>
                    <td className="px-4 py-3" data-testid="doc-status" data-status={doc.status}>
                      <StatusPill status={doc.status} />
                    </td>
                    <td className="px-4 py-3 font-mono text-xs tabular-nums text-muted">
                      {doc.chunk_count}
                    </td>
                    <td className="px-4 py-3 font-mono text-xs tabular-nums text-muted">
                      {formatBytes(doc.byte_size)}
                    </td>
                    <td className="px-4 py-3 text-xs text-muted">
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
