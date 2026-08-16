/**
 * The corpus, in the rail, while you are talking to the agent.
 *
 * **This is the half of the redesign that adds something rather than moving it.**
 * The corpus used to live behind a tab, so the answer to "what is this agent
 * actually grounded in?" required leaving the conversation. Measured on the
 * Documents tab at 1440x900: the panel began at 576px, the upload dropzone at
 * 634px and the corpus table at 833px, which put **one of two rows below the
 * fold** -- with two documents in the corpus. The upload control, used once per
 * file, outranked the list, which is the reason you opened the tab.
 *
 * Here the order is inverted: the list is the rail, and uploading is a control
 * at the top of it.
 *
 * **It shares one hook with the full Documents view**, so there is one backoff
 * timer, one upload path and one delete path rather than two that drift. That
 * matters more than it sounds -- the poll's backoff is only correct because its
 * callback identity is stable, and two copies would be two chances to get that
 * wrong.
 *
 * **Why this is not NotebookLM's Sources panel**, given it is a source list on
 * the left of a chat: theirs is a permanent panel of checkboxes that scope which
 * sources the next question may use, sitting beside a separate Studio panel. This
 * is one of two things the rail can show, it has no per-source selection, and the
 * scoping it would express does not exist in this product -- an agent retrieves
 * over its whole namespace, always. What is on screen here is provenance, not a
 * filter: filename, ingest status, and **the chunk count**, which is the number
 * that changes when you change `chunk_size` and re-upload. That number is the
 * workshop exercise made visible, and it is a column NotebookLM has no reason to
 * have.
 */

import { useRef, useState } from "react";
import {
  ACCEPTED_UPLOAD_TYPES,
  MAX_UPLOAD_BYTES,
  useAgentDocuments,
} from "../lib/useAgentDocuments.ts";
import { ConfirmDeleteButton, ErrorBanner, Spinner, StatusPill } from "./ui.tsx";
import DuplicatePrompt from "./DuplicatePrompt.tsx";
import { formatBytes } from "../lib/format.ts";

export default function SourceRail({
  agentId,
  onCorpusChanged,
}: {
  agentId: string;
  onCorpusChanged?: () => void;
}) {
  const corpus = useAgentDocuments(agentId, onCorpusChanged);
  const fileRef = useRef<HTMLInputElement>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);

  async function onPick(file: File | undefined) {
    if (!file) return;
    const ok = await corpus.upload(file);
    // Cleared only on success, and only from the boolean the hook returns --
    // reading `corpus.error` here would read the value from the render that
    // built this handler, which is one render stale.
    if (ok) clearPicker();
  }

  /** An `<input type="file">` fires no change event when the same file is picked
   *  twice, so a selection left behind after the upload is resolved makes
   *  re-picking that file look like a dead control. */
  function clearPicker() {
    if (fileRef.current) fileRef.current.value = "";
  }

  /**
   * The two answers to the duplicate prompt.
   *
   * The decision -- which file, is it still live, is a retry in flight -- is the
   * hook's, and is shared with the Documents view. What is local is this
   * surface's file input, which is the one thing the hook cannot reach. Cancel
   * clears it too: a "no" that leaves the file selected drops the user straight
   * into the no-change-event trap above.
   */
  async function confirmDuplicate() {
    if (await corpus.confirmDuplicate()) clearPicker();
  }

  function dismissDuplicate() {
    corpus.dismissDuplicate();
    clearPicker();
  }

  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-2">
      {/*
        The label IS the button. A bare `<input type="file">` renders a native
        control this app cannot size, and `min-h-11` is the convention -- so the
        input is visually hidden and a styled label drives it, which is the
        standard technique and keeps the accessible name and the click target on
        one element.
      */}
      <label
        className="flex min-h-11 cursor-pointer items-center justify-center gap-2 rounded-md border border-dashed border-slate-700 bg-slate-900 px-3 text-sm text-slate-300 transition hover:border-slate-600 hover:text-slate-100"
        title={`Markdown, plain text or PDF, up to ${formatBytes(MAX_UPLOAD_BYTES)}`}
      >
        <input
          ref={fileRef}
          type="file"
          data-testid="rail-file-input"
          accept={ACCEPTED_UPLOAD_TYPES}
          disabled={corpus.uploading}
          onChange={(event) => void onPick(event.target.files?.[0])}
          className="sr-only"
        />
        {corpus.uploading ? <Spinner label="Uploading" /> : <span>+ Add source</span>}
      </label>

      <ErrorBanner error={corpus.error} />

      {/* `shrink-0` because this sits above a flex-1 list that would otherwise
          compress it -- and because the prompt going unread is the failure this
          rail is most exposed to: it stays MOUNTED behind the Threads tab, so an
          unanswered conflict survives off-screen until it expires. */}
      {corpus.pendingDuplicate && (
        <div className="min-w-0 shrink-0">
          <DuplicatePrompt
            testId="rail-duplicate"
            message={corpus.pendingDuplicate.message}
            busy={corpus.uploading}
            onConfirm={() => void confirmDuplicate()}
            onDismiss={dismissDuplicate}
          />
        </div>
      )}

      {corpus.loading && <Spinner label="Loading corpus" />}

      {!corpus.loading && corpus.documents.length === 0 && (
        <p className="rounded-md border border-dashed border-slate-800 px-3 py-6 text-center text-xs leading-relaxed text-slate-400">
          No sources yet. This agent will refuse every question until it has one — which is
          the correct answer, not a failure.
        </p>
      )}

      <ul className="min-h-0 flex-1 space-y-1 overflow-y-auto" data-testid="rail-source-list">
        {corpus.documents.map((document) => {
          const expanded = document.id === expandedId;
          return (
            <li
              key={document.id}
              data-testid="rail-source"
              data-document-id={document.id}
              data-status={document.status}
              className="rounded-md border border-slate-800 bg-slate-900/40"
            >
              <button
                type="button"
                aria-expanded={expanded}
                onClick={() => setExpandedId((current) => (current === document.id ? null : document.id))}
                className="flex min-h-11 w-full min-w-0 flex-col items-start justify-center gap-0.5 px-3 py-2 text-left transition hover:bg-slate-900"
              >
                <span className="w-full truncate text-sm text-slate-200">{document.filename}</span>
                <span className="flex items-center gap-2 text-xs text-slate-400">
                  <StatusPill status={document.status} />
                  {/* The number the workshop exercise moves. */}
                  <span>
                    {document.chunk_count} {document.chunk_count === 1 ? "chunk" : "chunks"}
                  </span>
                </span>
              </button>

              {expanded && (
                <div className="space-y-2 border-t border-slate-800 px-3 py-2">
                  <dl className="space-y-1 text-xs text-slate-400">
                    <div className="flex justify-between gap-2">
                      <dt>Size</dt>
                      <dd className="text-slate-300">
                        {document.byte_size === null ? "--" : formatBytes(document.byte_size)}
                      </dd>
                    </div>
                    <div className="flex justify-between gap-2">
                      <dt>Type</dt>
                      <dd className="truncate text-slate-300">{document.mime_type ?? "--"}</dd>
                    </div>
                  </dl>

                  {/* Ingest failures have no column on `documents`; the API reads
                      them back out of `audit_log` and returns them here. Showing
                      it is the only way a failed row explains itself. */}
                  {document.error && (
                    <p
                      data-testid="rail-source-error"
                      className="rounded border border-rose-900/60 bg-rose-950/30 px-2 py-1 text-xs break-words text-rose-300"
                    >
                      {document.error}
                    </p>
                  )}

                  <ConfirmDeleteButton
                    testId="rail-source-delete"
                    label="Delete"
                    confirmLabel="Confirm"
                    accessibleLabel={`Delete ${document.filename}`}
                    accessibleConfirmLabel={`Confirm deletion of ${document.filename}`}
                    busy={corpus.deletingId === document.id}
                    size="sm"
                    onConfirm={() => void corpus.remove(document.id)}
                  />
                </div>
              )}
            </li>
          );
        })}
      </ul>

      {corpus.indexing && (
        <p className="shrink-0 text-xs text-slate-400">
          {corpus.indexingCount} still indexing…
        </p>
      )}

      {corpus.stalled && (
        <button
          type="button"
          data-testid="rail-recheck"
          onClick={() => void corpus.refresh()}
          className="min-h-11 shrink-0 rounded-md border border-amber-800/60 bg-amber-950/20 px-3 text-xs text-amber-300 transition hover:border-amber-600"
        >
          Stopped watching. Check again
        </button>
      )}
    </div>
  );
}
