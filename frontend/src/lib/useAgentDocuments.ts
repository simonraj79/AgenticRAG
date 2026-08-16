/**
 * One agent's corpus: the list, the poll that watches it settle, and the upload
 * and delete that change it.
 *
 * Extracted from `views/AgentDocuments.tsx` when the same list gained a second
 * presentation -- a narrow rail beside the chat, so a user can see what the
 * agent is grounded in while talking to it. The two renderings are genuinely
 * different; the fetching is not, and a second copy of it would be a second
 * copy of the backoff timer below, hitting one endpoint on two schedules and
 * drifting apart the first time only one of them was fixed.
 *
 * **Ingest is asynchronous.** `POST /api/agents/{id}/documents` answers
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
 * **`onCorpusChanged` must be stable across renders, and moving this code into
 * a shared hook turns that from an internal detail into a contract.** The poll
 * effect depends on the callback's identity, so a fresh arrow function per
 * render tears the timer down and rebuilds it at the SHORTEST interval every
 * time the caller re-renders -- a backoff that never backs off, and rule 2
 * above silently undone. It is a `useCallback` in `AgentDetail` for exactly
 * this reason, and the comment there says so. Any second consumer owes the same
 * thing; nothing enforces it, which is why it is stated here, where whoever
 * writes that consumer will be reading.
 */

import { useCallback, useEffect, useState } from "react";
import { ApiError, api } from "./api.ts";
import { formatBytes } from "./format.ts";
import type { DocumentRow } from "./types.ts";
import { errorMessage } from "../components/ui.tsx";

/** Mirrors `app.rag.ingest.SUPPORTED_SUFFIXES`. Advisory only -- the backend is
 *  the authority and rejects anything else with a message naming the set. */
export const ACCEPTED_UPLOAD_TYPES = ".md,.markdown,.txt,.pdf";

/** Mirrors `app.api.documents.MAX_UPLOAD_BYTES`. Checked here as well so a
 *  rejection costs a message instead of a multi-minute upload, but the server
 *  is what enforces it -- a client-side limit is a courtesy, not a control. */
export const MAX_UPLOAD_BYTES = 50 * 1024 * 1024;

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

/**
 * A duplicate upload the server refused, held until the user answers it.
 *
 * The backend dedups on a **SHA-256 of the bytes** (`app/api/documents.py`), so
 * this is raised for the same content under a different name and is NOT raised
 * for an edited file, nor for a re-upload after a `failed` ingest -- the
 * predicate is agent + hash + status "ready", and a stuck row is deliberately
 * not a bar to its own fix. That narrows who ever sees this prompt: someone who
 * genuinely wants the same bytes indexed twice, which in this workshop is
 * re-chunking after changing `chunk_size`.
 *
 * The `File` is kept rather than its bytes. A `File` is a handle onto the OS
 * file, so the retry re-reads it at confirm time -- if the user moved or deleted
 * it in between, the retry's fetch rejects and `api()` reports "Cannot reach the
 * API", which names the wrong cause. That is a known limit, and the reason the
 * prompt expires rather than waiting forever. Snapshotting the bytes instead
 * would put a 50 MB ArrayBuffer in React state at the upload cap, and resident
 * bytes are already the real ceiling on concurrent uploads (`app/rag/jobs.py`).
 *
 * `message` is the server's `detail` string, verbatim, because it names the file
 * already in the corpus and **nothing on this client can derive that name**:
 * `DocumentRow` carries no `content_hash` on purpose, and structuring the 409
 * body instead would fall out of `readError`'s string branch and dump raw JSON
 * on the screen.
 */
export type PendingDuplicate = { file: File; message: string };

/**
 * How long the duplicate prompt stays answerable.
 *
 * It expires for the same reason `ConfirmDeleteButton` disarms itself, and the
 * rail is what makes that mandatory rather than tidy: `SourceRail` stays MOUNTED
 * behind the Threads tab so its ingest poll survives a tab switch, so an
 * unanswered prompt would otherwise sit off-screen indefinitely and come back
 * armed for a user who has forgotten what it asked. Longer than that button's
 * 5 s because this one is a sentence to read and a decision to make; short
 * enough that the `File` handle above is unlikely to have moved underneath it.
 */
const DUPLICATE_PROMPT_MS = 60_000;

const POLL_FIRST_MS = 1_200;
const POLL_MAX_MS = 8_000;
const POLL_GROWTH = 1.5;
/** Long, because the ceiling it guards against is a 50 MB PDF's worth of
 *  embedding calls, not a slow network. */
const POLL_GIVE_UP_MS = 10 * 60 * 1_000;

/** Everything a presentation of the corpus needs, and nothing about how it
 *  looks. Both consumers render from exactly this. */
export type AgentCorpus = {
  documents: DocumentRow[];
  loading: boolean;
  error: string | null;
  setError: (error: string | null) => void;
  /** Refetch now, tell the owner, and restart the poll with a fresh deadline.
   *  What the stall notice's "Check again" is wired to. */
  refresh: () => Promise<void>;
  /**
   * Send one file. Resolves **true** when the server accepted it, false when it
   * was rejected -- by the size pre-check below or by the API.
   *
   * The boolean is the one deviation from "errors live in `error`": a caller
   * has to clear its own file input on success and must not on failure, and
   * `error` read straight after the await is the value from the render that
   * created the handler, not the one this call just set.
   *
   * `force` maps to `?force=true`, which is how the backend's duplicate-content
   * 409 is overridden. **`confirmDuplicate` is its only caller**, and no view
   * calls `upload(file, true)` directly: forcing is the answer to a refusal the
   * user has read, never a flag a form can set. See `pendingDuplicate`.
   */
  upload: (file: File, force?: boolean) => Promise<boolean>;
  /**
   * The upload the server refused as already-in-the-corpus, or null.
   *
   * Set instead of `error`, so a conflict never lands in the red banner: it is
   * a question with an answer, not a failure to report. Both surfaces render it
   * through the same `DuplicatePrompt`; neither owns it, for the same reason
   * neither owns the poll -- the rail has no `File` in state at all (its `File`
   * exists only inside the change handler's scope), so "keep the chosen file in
   * the component" is a design only one of the two could implement.
   */
  pendingDuplicate: PendingDuplicate | null;
  /**
   * Re-send the refused file with `?force=true`. Resolves like `upload`.
   *
   * **Must stay an explicit user action.** Force disables BOTH dedup layers --
   * the route's pre-check and ingest's own backstop -- so a second forced upload
   * of the same bytes really does write a second document, a second chunk set
   * and a second set of vectors into the namespace, at full embedding cost.
   * Nothing behind it would catch a retry the user did not ask for.
   */
  confirmDuplicate: () => Promise<boolean>;
  /** Drop the prompt unanswered. The caller also clears its own file input --
   *  see the note at each call site. */
  dismissDuplicate: () => void;
  remove: (documentId: string) => Promise<void>;
  uploading: boolean;
  /** The row whose delete is in flight, for a per-row busy state. */
  deletingId: string | null;
  /** True while any document is in a non-terminal status, i.e. the poll is live. */
  indexing: boolean;
  /** How many, for copy that counts them. Derived here rather than by each
   *  consumer, so `TERMINAL_DOCUMENT_STATUSES` stays private to this file. */
  indexingCount: number;
  /** The poll hit `POLL_GIVE_UP_MS` and stopped. Still `indexing`. */
  stalled: boolean;
};

export function useAgentDocuments(
  agentId: string,
  onCorpusChanged?: () => void,
): AgentCorpus {
  const [documents, setDocuments] = useState<DocumentRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [stalled, setStalled] = useState(false);
  /** Bumped to restart the poll loop after it has given up. Without it, "Check
   *  again" would clear the stall notice and refresh once, leaving the list
   *  claiming to refresh itself while nothing was scheduled to. */
  const [pollEpoch, setPollEpoch] = useState(0);

  const [uploading, setUploading] = useState(false);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [pendingDuplicate, setPendingDuplicate] = useState<PendingDuplicate | null>(null);

  /**
   * The prompt expires. See `DUPLICATE_PROMPT_MS` for why that is mandatory
   * rather than polite -- the rail outlives the tab it is on.
   *
   * Depends on the object identity, so a second conflict restarts the clock
   * rather than inheriting what was left of the first one's.
   */
  useEffect(() => {
    if (!pendingDuplicate) return;
    const timer = window.setTimeout(() => setPendingDuplicate(null), DUPLICATE_PROMPT_MS);
    return () => window.clearTimeout(timer);
  }, [pendingDuplicate]);

  /**
   * A conflict belongs to the agent that raised it.
   *
   * `useAgentDocuments` keeps its state when `agentId` changes in place -- only
   * `load` is memoised on it -- so without this a prompt raised against agent A
   * could be confirmed against agent B, force-writing a file into a corpus that
   * never objected to it. That is a wrong write, not a stale render, which is
   * why it is cleared here rather than left to the next upload.
   */
  useEffect(() => {
    setPendingDuplicate(null);
  }, [agentId]);

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

  const indexingCount = documents.filter(
    (doc) => !TERMINAL_DOCUMENT_STATUSES.has(doc.status),
  ).length;
  const indexing = indexingCount > 0;

  /**
   * The poll loop.
   *
   * Depends on `indexing` as a BOOLEAN rather than on `documents`: the array
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
    if (!indexing) return;

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
          onCorpusChanged?.();
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
  }, [indexing, pollEpoch, load, onCorpusChanged]);

  const upload = useCallback(
    async (file: File, force = false): Promise<boolean> => {
      // Any new upload supersedes an unanswered conflict: the question was
      // about a file the user has now stopped talking about. Cleared before the
      // size check, because a rejection is also a new answer.
      setPendingDuplicate(null);
      if (file.size > MAX_UPLOAD_BYTES) {
        setError(
          `${file.name} is ${formatBytes(file.size)}. The limit is ` +
            `${MAX_UPLOAD_BYTES / (1024 * 1024)} MB.`,
        );
        return false;
      }

      setUploading(true);
      setError(null);
      try {
        const form = new FormData();
        // The field name is "file" and the backend reads exactly that.
        form.append("file", file);
        // `?force=true` is the documented override for the 409 the backend
        // raises when this exact CONTENT is already in the corpus -- it dedups
        // on a SHA-256 of the bytes, not on the filename, so renaming a file
        // does not get past it and re-uploading an edited file does not trip
        // it. The flag was written before anything set it, so that the retry
        // would be a second ARGUMENT rather than a second upload path; that
        // retry is `confirmDuplicate` below, and it is still the only caller.
        const query = force ? "?force=true" : "";
        // No Content-Type header: the browser must set it itself so the multipart
        // boundary matches the body it generated. See lib/api.ts.
        await api<DocumentRow>(`/api/agents/${agentId}/documents${query}`, {
          method: "POST",
          body: form,
        });
        // The row lands as `pending`; storing it starts the poll loop above. The
        // parent is told now because the document COUNT is already correct --
        // only the status and chunk total are still in flight.
        await load();
        onCorpusChanged?.();
        return true;
      } catch (cause) {
        /*
         * Branch on the STATUS, never on the text. `ApiError` carries it for
         * exactly this, and the alternative -- matching "already in the corpus"
         * -- is a marker list, which this repo has had wrong three times in a
         * different module.
         *
         * Sound only because it is scoped to THIS call. 409 is not "duplicate"
         * globally on this API: deleting a document mid-index answers 409, and
         * so does a handout at quota. `POST /api/agents/{id}/documents` raises
         * it at exactly one line and for exactly one reason, so the branch
         * belongs here and never in a shared interceptor.
         *
         * Not the shape loop.md T2 forbids. The trigger is not "an error
         * occurred", it is the server stating the outcome directly -- no
         * corpus entry was created, because one with these bytes exists -- from
         * a single unambiguous site, before it writes anything. Triggering on
         * `error !== null` would fire on 413, 415, 422 and a dead backend, none
         * of which force fixes. The complement of T2 applies to the retry as
         * well: its success signal is the row appearing in `documents`, not the
         * absence of a throw.
         *
         * `!force` closes the loop: a forced upload that still 409s has not
         * been refused for duplicate content, and re-offering the same button
         * would ask the user to retry the retry.
         */
        if (!force && cause instanceof ApiError && cause.status === 409) {
          setPendingDuplicate({ file, message: cause.message });
          return false;
        }
        setError(errorMessage(cause));
        return false;
      } finally {
        setUploading(false);
      }
    },
    [agentId, load, onCorpusChanged],
  );

  /**
   * The retry, and the only thing that ever forces.
   *
   * Clears the prompt BEFORE awaiting rather than after. `uploading` already
   * disables the button, but the two states are set in the same render pass and
   * a forced double-submit is the one mistake here that damages the corpus --
   * force disables the route's dedup AND ingest's, so there is no backstop
   * behind it, unlike the unforced race that `ingest.py` catches.
   */
  const confirmDuplicate = useCallback(async (): Promise<boolean> => {
    if (!pendingDuplicate) return false;
    const { file } = pendingDuplicate;
    setPendingDuplicate(null);
    return upload(file, true);
  }, [pendingDuplicate, upload]);

  const dismissDuplicate = useCallback(() => setPendingDuplicate(null), []);

  const remove = useCallback(
    async (documentId: string) => {
      setDeletingId(documentId);
      setError(null);
      try {
        await api(`/api/agents/${agentId}/documents/${documentId}`, { method: "DELETE" });
        await load();
        onCorpusChanged?.();
      } catch (cause) {
        setError(errorMessage(cause));
      } finally {
        setDeletingId(null);
      }
    },
    [agentId, load, onCorpusChanged],
  );

  const refresh = useCallback(async () => {
    setError(null);
    try {
      await load();
      onCorpusChanged?.();
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      // Last, and unconditionally: if anything is still unsettled this restarts
      // the loop with a fresh deadline, and if everything has settled the
      // effect's own guard keeps it stopped.
      setPollEpoch((epoch) => epoch + 1);
    }
  }, [load, onCorpusChanged]);

  return {
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
  };
}
