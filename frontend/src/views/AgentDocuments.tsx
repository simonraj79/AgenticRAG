/**
 * Documents tab: upload a file, watch it become chunks, delete it again.
 *
 * **Ingest is synchronous.** `POST /api/agents/{id}/documents` loads, chunks,
 * embeds and upserts to Pinecone before it responds, so the request stays open
 * for several seconds on anything larger than a short note. There is no job
 * queue and no polling. That makes the spinner load-bearing rather than
 * decorative: without visible progress the page looks frozen for the exact
 * duration of the work, and "the upload hangs" becomes the bug report.
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
  ErrorBanner,
  Spinner,
  StatusPill,
  errorMessage,
} from "../components/ui.tsx";

/** Mirrors `app.rag.ingest.SUPPORTED_SUFFIXES`. Advisory only -- the backend is
 *  the authority and rejects anything else with a message naming the set. */
const ACCEPTED = ".md,.markdown,.txt,.pdf";

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

  const [file, setFile] = useState<File | null>(null);
  const [dragging, setDragging] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    setDocuments(await api<DocumentRow[]>(`/api/agents/${agentId}/documents`));
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

  async function upload() {
    if (!file) {
      setError("Choose a file first.");
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
      await load();
      // The agent's document_count and status both change on ingest; the parent
      // owns that record and has to refetch it.
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

        <p className="mt-2 text-xs text-slate-500">
          Markdown, plain text or PDF. Original files are not stored — text is chunked
          into Postgres and embedded into this agent&rsquo;s namespace.
        </p>

        <div className="mt-4 flex items-center gap-3">
          <button
            type="button"
            data-testid="doc-upload-submit"
            disabled={uploading || !file}
            onClick={() => void upload()}
            className="rounded-md bg-emerald-500 px-4 py-2 text-sm font-semibold text-emerald-950 transition hover:bg-emerald-400 disabled:opacity-50"
          >
            {uploading ? "Uploading…" : "Upload and index"}
          </button>
          {file && !uploading && (
            <span className="text-xs text-slate-400">
              {file.name} · {formatBytes(file.size)}
            </span>
          )}
          {uploading && <Spinner label="Chunking, embedding and upserting — a few seconds" />}
        </div>
      </div>

      <ErrorBanner error={error} />

      <div>
        <h3 className="mb-3 text-sm font-medium tracking-wide text-slate-400 uppercase">
          Corpus ({documents.length})
        </h3>

        {loading && <Spinner label="Loading documents" />}

        {!loading && documents.length === 0 && (
          <p className="rounded-lg border border-dashed border-slate-800 px-4 py-8 text-center text-sm text-slate-500">
            No documents yet. This agent will refuse every question until it has some.
          </p>
        )}

        {documents.length > 0 && (
          <div className="overflow-x-auto rounded-lg border border-slate-800">
            <table className="w-full text-left text-sm">
              <thead className="bg-slate-900/60 text-xs tracking-wide text-slate-500 uppercase">
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
                    <td className="px-4 py-3 text-slate-200">{doc.filename}</td>
                    <td className="px-4 py-3">
                      <StatusPill status={doc.status} />
                    </td>
                    <td className="px-4 py-3 text-slate-300">{doc.chunk_count}</td>
                    <td className="px-4 py-3 text-slate-400">{formatBytes(doc.byte_size)}</td>
                    <td className="px-4 py-3 text-xs text-slate-500">
                      {formatTimestamp(doc.created_at)}
                    </td>
                    <td className="px-4 py-3 text-right">
                      <ConfirmDeleteButton
                        testId="doc-delete"
                        label="Delete"
                        confirmLabel="Delete + drop vectors?"
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
