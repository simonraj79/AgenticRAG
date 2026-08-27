/**
 * "This file is already in the corpus" -- and the button that overrides it.
 *
 * The backend dedups uploads on a SHA-256 of the CONTENT and answers 409 without
 * writing anything. That refusal is right almost every time, and wrong exactly
 * once: when the user means to index the same bytes twice, which in this
 * workshop is re-chunking a file after changing `chunk_size`. So it needs an
 * answer, not just a report.
 *
 * **One component for two surfaces**, because the state behind it is one piece
 * of state. `useAgentDocuments` holds the refused `File` and the server's
 * message; the Documents view and the source rail each render this from those
 * values and neither owns a conflict of its own. Everything about this component
 * is presentation: it knows nothing about files, agents or HTTP.
 *
 * Three shapes it deliberately is not:
 *
 * - **Not a modal.** A dialog over a 17rem rail is a different component with a
 *   focus trap and a scroll lock, and neither surface has a modal today. An
 *   inline block is what both layouts already accept.
 * - **Not `ConfirmDeleteButton`.** That control arms on the first click, so the
 *   question is only readable after committing to it -- fine for a delete the
 *   user already intends, wrong for a question they have not been asked yet. It
 *   also renders destructive, and forcing an upload is additive. Its self-disarm
 *   timer is borrowed, though; it lives on the state, in the hook.
 * - **Not routed through `ErrorBanner`.** That is `role="alert"`, i.e.
 *   assertive, and it would now contain controls the user has to reach. This is
 *   `role="status"` -- polite -- with the buttons in normal DOM order right
 *   after the text, and **focus is never moved programmatically**: an effect
 *   that moves focus can forge a blur, which is a fact about the user that only
 *   the user may assert (see the StrictMode note in CLAUDE.md).
 *
 * **The message is the server's `detail`, verbatim.** It names the file already
 * in the corpus, which nothing on the client can derive -- `DocumentRow` carries
 * no `content_hash`, on purpose. It ends by naming `?force=true`, the mechanism
 * this button is a shortcut for; kept rather than reworded, because this is a
 * teaching artifact and the API's own words are the point (see `lib/api.ts`).
 *
 * **Layout is stacked, and that is the rail's constraint rather than a
 * preference.** At 17rem, a sentence and two buttons on one line do not fit, and
 * the rail column is `min-w-0`, so a flex row would squeeze the text to nothing
 * rather than wrap. Text above, controls in a wrapping row below, `break-words`
 * because a filename may offer no break opportunity, and every control at the
 * 44px minimum (`scripts/ui_check.py` A7 asserts zero horizontal overflow at
 * 320px and A8 asserts the 44px floor).
 *
 * **A caution, not a failure, so it is `NOTICE` + `WARN_TONE`** -- the same
 * strip a pending eval or an indexing corpus uses, at paragraph scale. The
 * server refused an upload and is asking a question; nothing has gone wrong.
 * The confirm is `BTN_SECONDARY` and the cancel `BTN_QUIET`, which is the right
 * way round for a prompt whose safe answer is "no": neither reads as the
 * affirmative action of the page, and the one that writes data is the one that
 * looks like a control.
 */

import { BTN_QUIET, BTN_SECONDARY, BTN_SM, NOTICE, WARN_TONE } from "../lib/styles.ts";

export default function DuplicatePrompt({
  testId,
  message,
  busy,
  onConfirm,
  onDismiss,
}: {
  /** Prefix for this surface's ids -- "doc-duplicate" or "rail-duplicate".
   *  Per-surface rather than shared, because both trees can be mounted at once
   *  and a shared id resolves to two elements under Playwright's strict mode. */
  testId: string;
  message: string;
  /** True while an upload is in flight. Disables confirm: force turns off BOTH
   *  layers of server-side dedup, so a double-submit really does write two
   *  copies of the document, its chunks and its vectors. */
  busy: boolean;
  onConfirm: () => void;
  onDismiss: () => void;
}) {
  return (
    <div data-testid={`${testId}-prompt`} className={`${NOTICE} ${WARN_TONE} min-w-0`}>
      {/* Size, leading and colour all come from the strip. `break-words` is the
          one thing left, and it is here rather than in `NOTICE` because a
          filename with no break opportunity is this message's problem and not
          every notice's. */}
      <p role="status" className="break-words">
        {message}
      </p>

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <button
          type="button"
          data-testid={`${testId}-confirm`}
          disabled={busy}
          onClick={onConfirm}
          className={`${BTN_SECONDARY} ${BTN_SM}`}
        >
          {busy ? "Uploading…" : "Upload it again anyway"}
        </button>

        <button
          type="button"
          data-testid={`${testId}-dismiss`}
          onClick={onDismiss}
          className={`${BTN_QUIET} ${BTN_SM}`}
        >
          Cancel
        </button>
      </div>
    </div>
  );
}
