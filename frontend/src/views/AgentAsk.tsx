/**
 * Ask tab: one question, one grounded answer, and the chunks that produced it.
 *
 * **The latency warning next to the button is not padding.** This project
 * measured a round trip at roughly 14.8 s: embed 365 ms, Pinecone k=20 394 ms,
 * Cohere rerank ~830 ms, and Gemma generation 13.2 s -- 89% of the total. A UI
 * that gives no hint of that reads as hung at about the four-second mark, and
 * "the Ask button does nothing" becomes the failure report for a system that is
 * working exactly as designed. Saying the number up front turns a bug report
 * into an expectation.
 *
 * Note also which hop is NOT the problem: the PRD flagged Cohere as the only
 * Singapore -> US round trip and worried about it. It costs a twentieth of what
 * generation does.
 */

import { useState } from "react";
import type { FormEvent } from "react";
import { api } from "../lib/api.ts";
import type { AskResult } from "../lib/types.ts";
import { formatDuration, formatScore } from "../lib/format.ts";
import { ErrorBanner, Spinner, errorMessage } from "../components/ui.tsx";

export default function AgentAsk({
  agentId,
  onAnswered,
}: {
  agentId: string;
  /** Hands the new query_id up so the Trace tab can show this turn's timeline. */
  onAnswered: (queryId: string) => void;
}) {
  const [question, setQuestion] = useState("");
  const [result, setResult] = useState<AskResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const trimmed = question.trim();
    if (!trimmed) {
      setError("Type a question first.");
      return;
    }

    setBusy(true);
    setError(null);
    // Cleared rather than left in place: a stale answer sitting under a spinner
    // for fifteen seconds is indistinguishable from the new one having arrived.
    setResult(null);
    try {
      const answer = await api<AskResult>(`/api/agents/${agentId}/ask`, {
        method: "POST",
        json: { question: trimmed },
      });
      setResult(answer);
      onAnswered(answer.query_id);
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-6">
      <form onSubmit={submit} className="rounded-xl border border-slate-800 bg-slate-900/50 p-5">
        <label className="block text-xs font-medium text-slate-400" htmlFor="ask-question">
          Question
        </label>
        <textarea
          id="ask-question"
          data-testid="ask-input"
          rows={3}
          value={question}
          onChange={(event) => setQuestion(event.target.value)}
          placeholder="What does the lecture say about chunk overlap?"
          className="mt-1 w-full resize-y rounded-md border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 outline-none focus:border-slate-500"
        />

        <div className="mt-4 flex flex-wrap items-center gap-4">
          <button
            type="submit"
            data-testid="ask-submit"
            disabled={busy}
            className="rounded-md bg-emerald-500 px-4 py-2 text-sm font-semibold text-emerald-950 transition hover:bg-emerald-400 disabled:opacity-50"
          >
            {busy ? "Thinking…" : "Ask"}
          </button>

          <p className="text-xs text-amber-300/90">
            Answers take roughly <strong>10–15 seconds</strong>. Generation is about 89% of
            that; retrieval is under a second. The page is not stuck.
          </p>
        </div>

        {busy && (
          <div className="mt-4">
            <Spinner label="Retrieving, reranking, generating…" />
          </div>
        )}
      </form>

      <ErrorBanner error={error} />

      {result && (
        <>
          <section className="rounded-xl border border-slate-800 bg-slate-900/50 p-5">
            <div className="mb-3 flex flex-wrap items-center gap-3">
              <h3 className="text-sm font-medium tracking-wide text-slate-400 uppercase">
                Answer
              </h3>
              {result.refused && (
                <span className="rounded-full border border-amber-800/60 bg-amber-950/40 px-2 py-0.5 text-xs font-medium text-amber-300">
                  refused
                </span>
              )}
              <span className="text-xs text-slate-500">
                {formatDuration(result.latency_ms)}
                {result.model_used ? ` · ${result.model_used}` : ""}
              </span>
            </div>

            {/*
              The testid sits on the answer text itself rather than the card, so
              its textContent is the answer and nothing else -- an assertion on
              the card would also match the latency and the model name.
              `whitespace-pre-wrap` because the model returns newline-separated
              prose and no markdown renderer is installed; adding one would be a
              build dependency for a formatting nicety.
            */}
            <p
              data-testid="ask-answer"
              className="text-sm leading-relaxed whitespace-pre-wrap text-slate-100"
            >
              {result.answer}
            </p>

            {result.refused && (
              // Spelled out because a refusal looks like a failure and is not
              // one. The system prompt forbids answering outside the retrieved
              // context, and declining is the behaviour the golden set scores.
              <p className="mt-4 border-t border-slate-800 pt-3 text-xs text-slate-500">
                The agent declined because the retrieved context did not support an
                answer. That is a correct outcome, not an error.
              </p>
            )}
          </section>

          <section>
            <h3 className="mb-3 text-sm font-medium tracking-wide text-slate-400 uppercase">
              Citations ({result.citations.length})
            </h3>

            {result.citations.length === 0 && (
              <p className="rounded-lg border border-dashed border-slate-800 px-4 py-6 text-center text-sm text-slate-500">
                Nothing was retrieved for this question.
              </p>
            )}

            <ol className="space-y-3">
              {result.citations.map((citation) => (
                <li
                  key={`${citation.chunk_id}-${citation.rank}`}
                  data-testid="ask-citation"
                  className="rounded-lg border border-slate-800 bg-slate-900/40 p-4"
                >
                  <div className="flex flex-wrap items-baseline justify-between gap-2">
                    <span className="text-sm font-medium text-slate-200">
                      <span className="mr-2 text-slate-500">#{citation.rank}</span>
                      {citation.filename}
                      <span className="ml-2 text-xs text-slate-500">
                        chunk {citation.chunk_index}
                      </span>
                    </span>

                    {/*
                      Both scores, side by side. Similarity is what the embedding
                      ranked; rerank is what Cohere thought of that ranking. The
                      gap between the two columns IS the Stage 2 demo -- if
                      reranking never reorders anything, it is not earning its
                      round trip.
                    */}
                    <span className="font-mono text-xs text-slate-400">
                      sim {formatScore(citation.similarity_score)} · rerank{" "}
                      {formatScore(citation.rerank_score)}
                    </span>
                  </div>

                  <p className="mt-2 text-xs leading-relaxed text-slate-400">
                    {citation.text_preview}
                  </p>
                </li>
              ))}
            </ol>
          </section>
        </>
      )}
    </div>
  );
}
