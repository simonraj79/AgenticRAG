/**
 * The golden set: the ten questions this agent is measured against.
 *
 * Three things about this editor are load-bearing rather than decorative.
 *
 * **Provenance is shown, always.** Every row carries a badge saying whether a
 * model wrote the question or a human vetted it. A set the model wrote for
 * itself and nobody reviewed is a weaker test than the same set after a human
 * corrected it, and once they are rows in a table the two are indistinguishable
 * -- so `source` is on screen, and saving an edit flips it to "edited"
 * server-side. That flip IS the value of the edit feature; hiding the badge
 * would leave the feature with nothing to show for itself.
 *
 * **A missing reference answer is warned about, loudly.** Ragas computes
 * `context_recall` by checking the reference answer against the retrieved
 * context. With no reference there is nothing to check, so the metric comes
 * back null -- a quarter of the scorecard silently absent, on a run that
 * otherwise looks complete. The warning names the consequence, not the field.
 *
 * **Refusal questions are a feature of the set, not a mistake in it.** A golden
 * set deliberately contains questions the corpus cannot answer; declining them
 * is the correct outcome (PRD section 4.4), and it is enforced by the system
 * prompt rather than by `score_threshold`. They are scored pass/fail on
 * behaviour and excluded from the four metric means -- see Scorecard, where
 * that exclusion is explained where the numbers are read.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { evaluation } from "../lib/api.ts";
import type { GoldenQuestion, GoldenSetFileQuestion } from "../lib/types.ts";
import { ConfirmDeleteButton, EmptyState, ErrorBanner, Spinner, errorMessage } from "./ui.tsx";
import {
  ACCENT_TONE,
  BTN_PRIMARY,
  BTN_SECONDARY,
  BTN_SM,
  CARD,
  EYEBROW,
  FIELD,
  HELP,
  LABEL,
  NEUTRAL_TONE,
  NOTICE,
  PILL,
  PILL_NEUTRAL,
  TEXTAREA,
  TEXTAREA_MONO,
  WARN_TONE,
  WELL,
} from "../lib/styles.ts";

/**
 * `source` is a plain `String(16)` column, not an enum, so that adding a
 * provenance costs a seed row rather than a migration. The cost of that choice
 * lands here: an unrecognised value must render as something neutral instead of
 * indexing into `undefined`, which is why the fallback below is a real label.
 */
const SOURCE_LABELS: Record<string, string> = {
  ai_suggested: "model wrote",
  edited: "human edited",
  manual: "human wrote",
  imported: "imported",
};

/**
 * Provenance, in the three tones the design has -- not the five hues this map
 * used to hold. Amber/emerald/sky/violet/slate was a fifth private colour scheme
 * competing with four others for the same small set of meanings; the question a
 * reader actually asks of this badge is "has a human been through this row",
 * which has two answers and a caveat.
 */
const SOURCE_STYLES: Record<string, string> = {
  // The one value that means "not yet vetted". A warn pill is a prompt to read
  // the question, not a neutral label.
  ai_suggested: `${PILL} ${WARN_TONE}`,
  // The accent means "evidence, or a way to reach some" -- a human has been
  // through this row, which is what makes it worth grading against.
  edited: `${PILL} ${ACCENT_TONE}`,
  manual: PILL_NEUTRAL,
  imported: PILL_NEUTRAL,
};

const UNKNOWN_SOURCE = PILL_NEUTRAL;

/** Suggestion runs as a background job, so the list is polled until it grows.
 *  Ten questions written from retrieved chunks is one generation call, but a
 *  cold model plus a long corpus can take a while. */
const SUGGEST_POLL_FIRST_MS = 1_500;
const SUGGEST_POLL_MAX_MS = 5_000;
const SUGGEST_POLL_GROWTH = 1.3;
const SUGGEST_GIVE_UP_MS = 3 * 60 * 1_000;

type Draft = {
  question: string;
  reference_answer: string;
  expected_behaviour: string;
  is_active: boolean;
};

const EMPTY_DRAFT: Draft = {
  question: "",
  reference_answer: "",
  expected_behaviour: "answer",
  is_active: true,
};

export default function GoldenSetEditor({
  agentId,
  onQuestionsChanged,
  runInFlight = false,
}: {
  agentId: string;
  /**
   * Hands the current set up so the parent can gate "Run evaluation" on there
   * being active questions. Read through a ref inside, so a caller that passes
   * a fresh arrow function every render cannot turn this into a fetch loop.
   */
  onQuestionsChanged?: (questions: GoldenQuestion[]) => void;
  /** A run reads the golden set once, at its start. Editing mid-run is allowed
   *  -- it just will not affect the run in flight, and the note says so. */
  runInFlight?: boolean;
}) {
  const [questions, setQuestions] = useState<GoldenQuestion[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT);
  const [savingId, setSavingId] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  const [newDraft, setNewDraft] = useState<Draft>(EMPTY_DRAFT);
  const [adding, setAdding] = useState(false);

  const [suggesting, setSuggesting] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [importText, setImportText] = useState("");
  const [importing, setImporting] = useState(false);

  // False after unmount, checked by the suggest poll loop -- which can still be
  // sleeping minutes after the user has navigated away.
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const notify = useRef(onQuestionsChanged);
  useEffect(() => {
    notify.current = onQuestionsChanged;
  });

  const applyRows = useCallback((rows: GoldenQuestion[]) => {
    // Sorted here as well as server-side. The API orders by `order_index`, but
    // a row created moments ago and merged into local state has not been
    // through that ordering, and a question that jumps to the top of the list
    // after a save looks like the save wrote the wrong row.
    const ordered = [...rows].sort(
      (a, b) => a.order_index - b.order_index || a.created_at.localeCompare(b.created_at),
    );
    setQuestions(ordered);
    notify.current?.(ordered);
  }, []);

  const load = useCallback(async () => {
    const rows = await evaluation.goldenSet(agentId);
    applyRows(rows);
    return rows;
  }, [agentId, applyRows]);

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

  const activeCount = questions.filter((row) => row.is_active).length;
  // "answer" questions with no reference answer. Counted rather than merely
  // flagged per row, because the consequence is aggregate: one missing
  // reference weakens `context_recall`, five of ten makes it meaningless.
  const missingReference = questions.filter(
    (row) => row.is_active && normaliseBehaviour(row.expected_behaviour) === "answer" && !row.reference_answer?.trim(),
  ).length;
  const nextOrderIndex = questions.reduce((highest, row) => Math.max(highest, row.order_index), -1) + 1;

  function beginEdit(row: GoldenQuestion): void {
    setError(null);
    setNotice(null);
    setEditingId(row.id);
    setDraft({
      question: row.question,
      reference_answer: row.reference_answer ?? "",
      expected_behaviour: normaliseBehaviour(row.expected_behaviour),
      is_active: row.is_active,
    });
  }

  async function saveEdit(id: string): Promise<void> {
    const question = draft.question.trim();
    if (!question) {
      setError("A question cannot be empty.");
      return;
    }

    setSavingId(id);
    setError(null);
    try {
      const saved = await evaluation.updateQuestion(id, {
        question,
        // Empty string is sent as null, not as "". A zero-length reference
        // answer would satisfy any "is it present" check on the server and then
        // give Ragas nothing to compare against -- null is the honest value and
        // it is what the missing-reference warning counts.
        reference_answer: draft.reference_answer.trim() || null,
        expected_behaviour: draft.expected_behaviour,
        is_active: draft.is_active,
      });
      // The saved row is the authority on `source`: the server flips
      // "ai_suggested" to "edited" on write, and re-reading our own draft would
      // leave the badge claiming a model still owns a question a human just
      // vetted.
      applyRows(questions.map((row) => (row.id === id ? saved : row)));
      setEditingId(null);
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setSavingId(null);
    }
  }

  async function addQuestion(): Promise<void> {
    const question = newDraft.question.trim();
    if (!question) {
      setError("Type a question first.");
      return;
    }

    setAdding(true);
    setError(null);
    try {
      const created = await evaluation.createQuestion(agentId, {
        question,
        reference_answer: newDraft.reference_answer.trim() || null,
        expected_behaviour: newDraft.expected_behaviour,
        is_active: newDraft.is_active,
        order_index: nextOrderIndex,
      });
      applyRows([...questions, created]);
      setNewDraft(EMPTY_DRAFT);
      setNotice(null);
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setAdding(false);
    }
  }

  async function remove(id: string): Promise<void> {
    setDeletingId(id);
    setError(null);
    try {
      await evaluation.deleteQuestion(id);
      applyRows(questions.filter((row) => row.id !== id));
      if (editingId === id) setEditingId(null);
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setDeletingId(null);
    }
  }

  /**
   * Ask for ten suggestions, then watch for them.
   *
   * The endpoint answers 202 and writes the rows out of band, so "did it work"
   * is something this client observes rather than awaits. The loop is bounded
   * and it gives up out loud: a suggestion job that dies without writing
   * anything would otherwise leave a spinner running until the tab closes,
   * looking busy forever.
   */
  async function suggest(): Promise<void> {
    setSuggesting(true);
    setError(null);
    setNotice(null);

    const before = questions.length;
    try {
      await evaluation.suggestQuestions(agentId);

      const deadline = Date.now() + SUGGEST_GIVE_UP_MS;
      let delay = SUGGEST_POLL_FIRST_MS;

      while (Date.now() < deadline) {
        await sleep(delay);
        if (!alive.current) return;

        try {
          const rows = await load();
          if (rows.length > before) {
            setNotice(
              `Added ${rows.length - before} suggested ${
                rows.length - before === 1 ? "question" : "questions"
              }. Read them before you trust a score built on them -- edit one and its badge changes to "human edited".`,
            );
            return;
          }
        } catch {
          // A failed poll is not a failed suggestion: the server is still
          // working. Replacing the banner with a transient network blip would
          // report the wrong problem. Persistent failure ends at the notice
          // below.
        }
        delay = Math.min(Math.round(delay * SUGGEST_POLL_GROWTH), SUGGEST_POLL_MAX_MS);
      }

      setNotice(
        "Stopped waiting after three minutes. The suggestion job may still finish -- reload the tab to check.",
      );
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      if (alive.current) setSuggesting(false);
    }
  }

  async function importQuestions(text: string): Promise<void> {
    setImporting(true);
    setError(null);
    setNotice(null);
    try {
      const parsed = parseGoldenSetFile(text);
      const created = await evaluation.importQuestions(agentId, parsed);
      // Re-read rather than merging the response: import may renumber
      // `order_index` across the whole set, and a merged list would show the
      // new questions in whatever order the response happened to carry.
      await load();
      setImportText("");
      setNotice(`Imported ${created.length} ${created.length === 1 ? "question" : "questions"}.`);
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setImporting(false);
    }
  }

  function onFileChosen(file: File | null): void {
    if (!file) return;
    // Read into the textarea rather than posting straight off: the file is
    // hand-editable by design, so a bad one should be visible and fixable in
    // place instead of bouncing off the server with the contents thrown away.
    file
      .text()
      .then((text) => {
        setImportText(text);
        setNotice(`Loaded ${file.name}. Review it, then press Import.`);
      })
      .catch((cause: unknown) => setError(errorMessage(cause)));
  }

  return (
    <section className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h3 className={EYEBROW}>Golden set ({questions.length})</h3>
          <p className="mt-1 text-xs text-muted">
            {activeCount} active {activeCount === 1 ? "question" : "questions"} will be run.
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            data-testid="golden-suggest"
            disabled={suggesting}
            onClick={() => void suggest()}
            className={BTN_SECONDARY}
          >
            {suggesting ? "Suggesting…" : "Suggest 10 questions"}
          </button>

          {/*
            A BUTTON, not a link, and that is forced rather than stylistic. The
            endpoint sets `Content-Disposition: attachment` and an `<a href>`
            used to be enough -- the session cookie rode along because it was
            issued `SameSite=None`. A browser NAVIGATION cannot carry an
            `Authorization` header, so once identity moved to a bearer token
            that anchor authenticated nobody and the export 401d for exactly
            the third-party-cookie users the move was made for.

            `exportGoldenSet` fetches with credentials and saves the blob,
            which works under the bearer token AND the old cookie.
          */}
          <button
            type="button"
            data-testid="golden-export"
            disabled={exporting}
            onClick={() => {
              setExporting(true);
              setError(null);
              evaluation
                .exportGoldenSet(agentId)
                .catch((cause) => setError(errorMessage(cause)))
                .finally(() => setExporting(false));
            }}
            className={BTN_SECONDARY}
          >
            {exporting ? "Preparing..." : "Export JSON"}
          </button>
        </div>
      </div>

      <p className={HELP}>
        Suggestions are written from this agent&rsquo;s own indexed chunks, so they can only
        ask what its corpus actually contains. That is the point and also the limit: a set
        drawn from the corpus cannot, on its own, test what the corpus is missing. Add a
        question the documents <em>cannot</em> answer and set it to{" "}
        <span className="font-medium text-ink">refuse</span> — declining is a correct outcome,
        and it is the only way to measure whether this agent stays inside its evidence.
      </p>

      {runInFlight && (
        <p className={`${NOTICE} ${NEUTRAL_TONE}`}>
          A run is in flight. It read the golden set when it started, so edits made now
          apply to the next run rather than to that one.
        </p>
      )}

      <ErrorBanner error={error} />

      {notice && <p className={`${NOTICE} ${NEUTRAL_TONE}`}>{notice}</p>}

      {missingReference > 0 && (
        /*
          Named for its consequence rather than for the empty field. "Reference
          answer is blank" is a form-validation message; "context_recall cannot
          be computed" is the reason anybody should care, and it is the fact
          that makes a complete-looking scorecard quietly a quarter empty.
        */
        <p data-testid="golden-reference-warning" className={`${NOTICE} ${WARN_TONE}`}>
          {missingReference} active{" "}
          {missingReference === 1 ? "question has" : "questions have"} no reference answer.
          Ragas scores <span className="font-medium">context_recall</span> by checking a
          reference answer against the retrieved context, so those rows come back null and
          a quarter of the scorecard goes missing without saying so. Refusal questions do
          not need one.
        </p>
      )}

      {loading && <Spinner label="Loading golden set" />}

      {!loading && questions.length === 0 && (
        <EmptyState
          title="No golden questions yet."
          detail="Suggest ten from the corpus, import a set you have already written, or add one by hand below. Nothing can be scored until this list has an active question in it."
        />
      )}

      {questions.length > 0 && (
        <ol className="space-y-2">
          {questions.map((row, index) => {
            const editing = editingId === row.id;
            const behaviour = normaliseBehaviour(row.expected_behaviour);
            const needsReference =
              row.is_active && behaviour === "answer" && !row.reference_answer?.trim();

            return (
              <li
                key={row.id}
                data-testid="golden-row"
                data-question-id={row.id}
                data-source={row.source}
                data-behaviour={behaviour}
                className={`${CARD} p-4 ${row.is_active ? "" : "opacity-70"}`}
              >
                {editing ? (
                  <div className="space-y-3">
                    <label className={`${LABEL} block`}>
                      Question
                      <textarea
                        autoFocus
                        rows={2}
                        value={draft.question}
                        onChange={(event) =>
                          setDraft({ ...draft, question: event.target.value })
                        }
                        className={`${TEXTAREA} mt-1`}
                      />
                    </label>

                    <label className={`${LABEL} block`}>
                      Reference answer{" "}
                      {draft.expected_behaviour === "answer" && (
                        <span className="font-normal text-warn">
                          — needed for context_recall
                        </span>
                      )}
                      <textarea
                        rows={3}
                        value={draft.reference_answer}
                        onChange={(event) =>
                          setDraft({ ...draft, reference_answer: event.target.value })
                        }
                        placeholder={
                          draft.expected_behaviour === "refuse"
                            ? "Not needed — this question is expected to be declined."
                            : "What a correct answer contains. Ragas checks the retrieved context against this."
                        }
                        className={`${TEXTAREA} mt-1`}
                      />
                    </label>

                    <div className="flex flex-wrap items-center gap-4">
                      <label className={LABEL}>
                        Expected behaviour
                        <select
                          value={draft.expected_behaviour}
                          onChange={(event) =>
                            setDraft({ ...draft, expected_behaviour: event.target.value })
                          }
                          className={`${FIELD} ml-2 w-auto`}
                        >
                          <option value="answer">answer</option>
                          <option value="refuse">refuse</option>
                        </select>
                      </label>

                      <label className="flex items-center gap-2 text-sm text-muted">
                        <input
                          type="checkbox"
                          checked={draft.is_active}
                          onChange={(event) =>
                            setDraft({ ...draft, is_active: event.target.checked })
                          }
                          className="h-4 w-4 accent-accent"
                        />
                        Active (retire without deleting history)
                      </label>
                    </div>

                    <div className="flex items-center gap-2">
                      <button
                        type="button"
                        data-testid="golden-save"
                        disabled={savingId === row.id}
                        onClick={() => void saveEdit(row.id)}
                        className={BTN_PRIMARY}
                      >
                        {savingId === row.id ? "Saving…" : "Save"}
                      </button>
                      <button
                        type="button"
                        onClick={() => setEditingId(null)}
                        className={BTN_SECONDARY}
                      >
                        Cancel
                      </button>
                      <span className="text-xs text-faint">
                        Saving marks this question &ldquo;human edited&rdquo;.
                      </span>
                    </div>
                  </div>
                ) : (
                  <div className="flex items-start gap-3">
                    <span className="mt-0.5 w-5 shrink-0 text-right font-mono text-xs text-faint">
                      {index + 1}
                    </span>

                    <div className="min-w-0 flex-1">
                      <p className="text-sm break-words text-ink">{row.question}</p>

                      <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                        <SourceBadge source={row.source} />
                        <BehaviourBadge behaviour={behaviour} />
                        {!row.is_active && (
                          <span className={`${PILL_NEUTRAL} tracking-[0.04em] uppercase`}>
                            inactive
                          </span>
                        )}
                      </div>

                      {/*
                        Serif, because a reference answer is corpus-derived prose
                        -- the thing the scorecard is graded against, and the one
                        text on this surface that has to be read rather than
                        scanned. Sans everywhere else here is the harness talking
                        about it.
                      */}
                      {row.reference_answer && (
                        <p className="mt-2 border-l-2 border-accent-line pl-3 font-serif text-sm leading-relaxed break-words text-muted">
                          <span className="font-sans text-xs text-faint">Reference: </span>
                          {row.reference_answer}
                        </p>
                      )}

                      {needsReference && (
                        <p className="mt-2 text-xs text-warn">
                          No reference answer — context_recall will be null for this row.
                        </p>
                      )}
                    </div>

                    <div className="flex shrink-0 items-center gap-1.5">
                      <button
                        type="button"
                        data-testid="golden-edit"
                        onClick={() => beginEdit(row)}
                        // `min-w-11` as well as `min-h-11`: "Edit" is four
                        // characters at `text-xs`, so height alone would leave
                        // a 44x28 target -- the note at CreateAgentWizard's
                        // ReviewRow makes the same point about the same word.
                        className={`${BTN_SECONDARY} ${BTN_SM} min-w-11`}
                      >
                        Edit
                      </button>
                      <ConfirmDeleteButton
                        testId="golden-delete"
                        label="Delete"
                        confirmLabel="Confirm"
                        size="sm"
                        busy={deletingId === row.id}
                        onConfirm={() => void remove(row.id)}
                      />
                    </div>
                  </div>
                )}
              </li>
            );
          })}
        </ol>
      )}

      <div className={`${CARD} p-5`}>
        <h4 className={EYEBROW}>Add a question</h4>

        <textarea
          rows={2}
          data-testid="golden-add-question"
          value={newDraft.question}
          onChange={(event) => setNewDraft({ ...newDraft, question: event.target.value })}
          placeholder="A question this agent should be able to answer — or one it should decline."
          className={`${TEXTAREA} mt-2`}
        />

        <textarea
          rows={2}
          data-testid="golden-add-reference"
          value={newDraft.reference_answer}
          onChange={(event) =>
            setNewDraft({ ...newDraft, reference_answer: event.target.value })
          }
          placeholder={
            newDraft.expected_behaviour === "refuse"
              ? "Reference answer not needed for a refusal question."
              : "Reference answer — without one, context_recall cannot be computed."
          }
          className={`${TEXTAREA} mt-2`}
        />

        <div className="mt-3 flex flex-wrap items-center gap-4">
          <label className={LABEL}>
            Expected behaviour
            <select
              data-testid="golden-add-behaviour"
              value={newDraft.expected_behaviour}
              onChange={(event) =>
                setNewDraft({ ...newDraft, expected_behaviour: event.target.value })
              }
              className={`${FIELD} ml-2 w-auto`}
            >
              <option value="answer">answer</option>
              <option value="refuse">refuse</option>
            </select>
          </label>

          <button
            type="button"
            data-testid="golden-add"
            disabled={adding || newDraft.question.trim() === ""}
            onClick={() => void addQuestion()}
            className={BTN_SECONDARY}
          >
            {adding ? "Adding…" : "Add question"}
          </button>
        </div>
      </div>

      <div className={`${CARD} p-5`}>
        <h4 className={EYEBROW}>Import a golden set</h4>
        <p className="mt-1 text-xs text-muted">
          Paste or load a JSON file. A bare list works, the exported{" "}
          <code className={`${WELL} px-1 py-0.5 font-mono text-ink`}>
            {"{ questions: [...] }"}
          </code>{" "}
          wrapper works, and unknown keys are ignored — this file is meant to be edited in a
          text editor, so it is not rejected on a technicality.
        </p>

        <input
          type="file"
          accept=".json,application/json"
          data-testid="golden-import-file"
          onChange={(event) => onFileChosen(event.target.files?.[0] ?? null)}
          className="mt-3 block w-full text-sm text-muted file:mr-3 file:rounded-md file:border-0 file:bg-ink file:px-3 file:py-1.5 file:text-sm file:font-medium file:text-inverse hover:file:bg-ink-hover"
        />

        <textarea
          rows={4}
          data-testid="golden-import-text"
          value={importText}
          onChange={(event) => setImportText(event.target.value)}
          placeholder='{"questions": [{"question": "...", "reference_answer": "...", "expected_behaviour": "answer"}]}'
          className={`${TEXTAREA_MONO} mt-3`}
        />

        <button
          type="button"
          data-testid="golden-import"
          disabled={importing || importText.trim() === ""}
          onClick={() => void importQuestions(importText)}
          className={`${BTN_SECONDARY} mt-3`}
        >
          {importing ? "Importing…" : "Import"}
        </button>
      </div>
    </section>
  );
}

// --------------------------------------------------------------------------
// Helpers
// --------------------------------------------------------------------------

function SourceBadge({ source }: { source: string }) {
  const label = SOURCE_LABELS[source] ?? source ?? "unknown";
  const style = SOURCE_STYLES[source] ?? UNKNOWN_SOURCE;
  return (
    <span data-testid="golden-source" className={`${style} tracking-[0.04em] uppercase`}>
      {label}
    </span>
  );
}

/**
 * The accent, not a warning colour.
 *
 * A planted gap is a deliberate property of the set -- PRD section 4.4 -- and the
 * only way to measure whether this agent stays inside its evidence. The old
 * fuchsia badge made the row look like a mistake somebody had left in.
 */
function BehaviourBadge({ behaviour }: { behaviour: string }) {
  const refuse = behaviour === "refuse";
  return (
    <span
      className={`${refuse ? `${PILL} ${ACCENT_TONE}` : PILL_NEUTRAL} tracking-[0.04em] uppercase`}
    >
      {refuse ? "expect refusal" : "expect answer"}
    </span>
  );
}

/**
 * "refuse" or "answer", from whatever the column happens to hold.
 *
 * `expected_behaviour` is a `String(16)`, not an enum, so an unrecognised value
 * is possible and must not be allowed to mean "refusal" by accident: treating
 * a typo as a refusal question would silently exclude it from every metric mean
 * and score it pass/fail on the wrong test. Anything that is not recognisably a
 * refusal degrades to "answer", which is the value that gets a row scored.
 */
export function normaliseBehaviour(value: string | null | undefined): string {
  return typeof value === "string" && value.trim().toLowerCase().startsWith("refus")
    ? "refuse"
    : "answer";
}

/**
 * Read a hand-editable golden-set file.
 *
 * Tolerant on purpose, in three specific ways, because the file's whole reason
 * to exist is that a human can open it in a text editor: a bare list is
 * accepted as well as the `{questions: [...]}` wrapper; unknown keys are
 * dropped rather than rejected; and a missing `expected_behaviour` defaults to
 * "answer".
 *
 * Strict on exactly one thing: a question with no text is refused, and the
 * message names the entry's position. Silently dropping it would import nine
 * questions from a file of ten and call it a success.
 */
export function parseGoldenSetFile(raw: string): GoldenSetFileQuestion[] {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch (cause) {
    throw new Error(`That is not valid JSON: ${String(cause)}`);
  }

  const list = Array.isArray(parsed)
    ? parsed
    : (parsed as { questions?: unknown })?.questions;

  if (!Array.isArray(list)) {
    throw new Error(
      'Expected a list of questions, or an object with a "questions" list. Got ' +
        `${typeof parsed}.`,
    );
  }

  return list.map((entry, index) => {
    const row = entry as Record<string, unknown>;
    const question = typeof row?.question === "string" ? row.question.trim() : "";
    if (!question) {
      throw new Error(`Entry ${index + 1} has no "question" text.`);
    }

    const reference =
      typeof row.reference_answer === "string" ? row.reference_answer.trim() : "";

    return {
      question,
      reference_answer: reference || null,
      expected_behaviour: normaliseBehaviour(
        typeof row.expected_behaviour === "string" ? row.expected_behaviour : null,
      ),
    };
  });
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}
