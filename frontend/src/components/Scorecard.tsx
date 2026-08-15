/**
 * One scorecard: four numbers, and what to do about the lowest one.
 *
 * The view is arranged around a single claim from PRD section 4.4 — *find your
 * weakest metric, that points at your next investment* — so the weakest metric
 * is the hero of the page and the four cards are the evidence for it, not the
 * other way round. A grid of four numbers with no verdict is a dashboard;
 * naming the weakest one and the knobs that move it is the deliverable.
 *
 * Three rules this component exists to enforce:
 *
 * **Null is not zero.** Null means "nothing could be scored"; zero means
 * "scored, and the answer was unsupported by its context". They are opposite
 * facts and rendering the first as the second is the easiest way to make a
 * measurement lie — so null renders as "not scored", never as 0.00, and never
 * as a full-width bar at the left edge.
 *
 * **Refusal rows are excluded from the means, and the page says so.** The
 * golden set deliberately contains questions the corpus cannot answer. A
 * correct refusal has no useful retrieved context and an answer that
 * deliberately does not follow from it, so faithfulness and context_recall
 * score near zero for behaving perfectly. Averaging them in would drag the
 * scorecard down as a *reward* for correct refusals, and the weakest-metric
 * pointer would then aim at whichever metric refusals punish hardest rather
 * than at the real weakness. They are scored pass/fail on `behaviour_ok` and
 * reported separately; `scored_count` says how many rows the means rest on.
 *
 * **Provenance is on the card.** Which model answered and which model judged
 * are both shown, and when they are the same model the page says plainly that
 * faithfulness is self-assessed. A number whose provenance is hidden is worse
 * than no number.
 */

import { useState } from "react";
import type { EvalResult, EvalRunDetail } from "../lib/types.ts";
import { formatTimestamp } from "../lib/format.ts";

/**
 * What each metric measures, and where the next hour of work goes if it is the
 * weakest one.
 *
 * The knobs are deliberately the agent's OWN parameters (`retrieve_k`,
 * `rerank_enabled`, `rerank_top_n`, `chunk_size`, `system_prompt`,
 * `max_rewrites`) rather than generic advice, because those are the fields the
 * workshop attendee can actually change and re-run against the same golden set.
 * "Improve your retrieval" is not an instruction; "turn on rerank and raise k"
 * is.
 */
type MetricKey = "faithfulness" | "answer_relevance" | "context_precision" | "context_recall";

type MetricSpec = {
  key: MetricKey;
  label: string;
  /** The question the number answers, in one line. */
  measures: string;
  /** The headline shown when this metric is the weakest one. */
  investmentFor: string;
  /** Why a low score here means what it means. */
  why: string;
  /** The specific knobs to turn, in the order worth trying them. */
  knobs: string[];
};

const METRICS: MetricSpec[] = [
  {
    key: "faithfulness",
    label: "Faithfulness",
    measures: "Is every claim in the answer supported by the chunks that were retrieved?",
    investmentFor: "Invest in grounding, not in retrieval.",
    why:
      "The right context was found and the model answered past it — filling gaps from its " +
      "own weights instead of declining. This is the metric that catches confident " +
      "invention, and it is the one a workshop demo is most often quietly failing.",
    knobs: [
      "Tighten the system prompt: forbid outside knowledge in as many words, and require every claim to rest on the retrieved text.",
      "Lower rerank_top_n. Fewer, stronger chunks in the window leaves less unrelated material for the model to blend in.",
      "Try a different generation model. Faithfulness is the metric a weaker model gives up first.",
    ],
  },
  {
    key: "answer_relevance",
    label: "Answer relevance",
    measures: "Does the answer address the question that was actually asked?",
    investmentFor: "Invest in the question, not in the corpus.",
    why:
      "A faithful answer to the wrong question scores well on faithfulness and badly here. " +
      "Under a coaching persona this usually means the answer wandered into teaching; on a " +
      "follow-up it usually means the history rewrite grabbed the wrong antecedent.",
    knobs: [
      "Tighten the system prompt: a persona asked for an analogy and a worked example will answer at length and drift.",
      "Raise max_rewrites so a vague question is reformulated before it is embedded.",
      'Open a failing row below and read what was actually searched for. "You asked X, I searched Y" is where a bad rewrite becomes visible.',
    ],
  },
  {
    key: "context_precision",
    label: "Context precision",
    measures: "Are the chunks that matter ranked above the ones that do not?",
    investmentFor: "Invest in ranking, not in adding documents.",
    why:
      "The useful chunk was retrieved but buried under near-misses. Adding more documents " +
      "makes this worse, not better — the corpus is not the problem, the ordering is.",
    knobs: [
      "Turn on rerank_enabled. This is precisely the metric Cohere rerank exists to move: 20 candidates in, the best 3 out.",
      "Raise retrieve_k so the reranker has more candidates to choose between.",
      "Lower rerank_top_n so only the strongest chunks reach the prompt.",
    ],
  },
  {
    key: "context_recall",
    label: "Context recall",
    measures: "Did retrieval find everything the reference answer needs?",
    investmentFor: "Invest in chunking and coverage.",
    why:
      "Part of the answer never reached the model. This is the one metric no amount of " +
      "prompting can fix, and the only one a missing source file can cause — which is why " +
      "it needs a reference answer to be computable at all.",
    knobs: [
      "Raise chunk_size or chunk_overlap. An answer split across a chunk boundary is retrieved half.",
      "Raise retrieve_k. The chunk exists and simply never made the cut.",
      "Upload the document that covers the gap, then re-run. If recall is low and the corpus genuinely lacks the material, the honest fix is more corpus.",
    ],
  },
];

const METRIC_BY_KEY: Record<string, MetricSpec> = Object.fromEntries(
  METRICS.map((metric) => [metric.key, metric]),
);

export default function Scorecard({ run }: { run: EvalRunDetail }) {
  const [showOnlyFailures, setShowOnlyFailures] = useState(false);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const summary = run.summary;
  const weakest = summary?.weakest_metric ? METRIC_BY_KEY[summary.weakest_metric] : undefined;

  const failing = run.results.filter(isFailing);
  const rows = showOnlyFailures ? failing : run.results;

  function toggle(id: string): void {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  return (
    <section data-testid="scorecard" className="space-y-5">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <div>
          <h3 className="text-sm font-medium tracking-wide text-slate-400 uppercase">
            Scorecard
          </h3>
          <p className="mt-1 text-xs text-slate-500">
            {formatTimestamp(run.started_at ?? null)}
            {run.finished_at ? ` — finished ${formatTimestamp(run.finished_at)}` : ""}
          </p>
        </div>
        <p className="text-xs text-slate-500">
          {run.results.length} {run.results.length === 1 ? "question" : "questions"} in this run
        </p>
      </div>

      {run.notes && (
        <p className="rounded-lg border border-slate-800 bg-slate-900/40 px-3 py-2 text-xs text-slate-300">
          <span className="text-slate-500">What changed: </span>
          {run.notes}
        </p>
      )}

      {run.error && (
        // Run-level failure, distinct from a single question's error below. This
        // is the reason a run ended with no summary at all.
        <p className="rounded-lg border border-rose-800/60 bg-rose-950/40 px-3 py-2 text-sm whitespace-pre-wrap text-rose-200">
          The run failed: {run.error}
        </p>
      )}

      {/*
        The hero. Placed above the four cards deliberately: the cards are the
        evidence, this is the finding. A user who reads only one thing on this
        page should read this.
      */}
      {weakest && summary && (
        <div
          data-testid="weakest-metric"
          data-metric={weakest.key}
          className="rounded-xl border border-amber-700/50 bg-amber-950/20 p-5"
        >
          <p className="text-[0.65rem] font-medium tracking-widest text-amber-500/80 uppercase">
            Weakest metric — your next investment
          </p>
          <p className="mt-2 flex flex-wrap items-baseline gap-3">
            <span className="text-2xl font-semibold text-amber-100">{weakest.label}</span>
            <span className="font-mono text-2xl text-amber-300">
              {formatMetric(summary.weakest_score)}
            </span>
          </p>
          <p className="mt-3 text-sm font-medium text-amber-100">{weakest.investmentFor}</p>
          <p className="mt-2 max-w-3xl text-sm leading-relaxed text-amber-200/80">
            {weakest.why}
          </p>
          <ul className="mt-3 space-y-1.5">
            {weakest.knobs.map((knob) => (
              <li key={knob} className="flex gap-2 text-sm text-amber-100/90">
                <span aria-hidden="true" className="text-amber-500">
                  &rsaquo;
                </span>
                <span>{knob}</span>
              </li>
            ))}
          </ul>
          <p className="mt-4 border-t border-amber-800/40 pt-3 text-xs text-amber-200/60">
            Change one thing, re-run the same golden set, and write what you changed in the
            notes. Two runs that differ in one parameter are an experiment; two that differ
            in five are a story.
          </p>
        </div>
      )}

      {!summary && (
        <p className="rounded-lg border border-slate-800 bg-slate-900/40 px-4 py-3 text-sm text-slate-400">
          {run.status === "failed"
            ? "This run has no summary because it did not finish."
            : "No summary yet — the aggregate is written once every question has been scored."}
        </p>
      )}

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {METRICS.map((metric) => (
          <MetricCard
            key={metric.key}
            metric={metric}
            value={summary ? summary[metric.key] : null}
            weakest={summary?.weakest_metric === metric.key}
          />
        ))}
      </div>

      {summary && (
        <div className="rounded-lg border border-slate-800 bg-slate-900/30 p-4">
          <div className="flex flex-wrap items-center gap-x-6 gap-y-2 text-sm">
            <span className="text-slate-300">
              <span className="text-slate-500">Means rest on </span>
              <span data-testid="scored-count" className="font-medium text-slate-100">
                {summary.scored_count}
              </span>
              <span className="text-slate-500">
                {" "}
                scored {summary.scored_count === 1 ? "question" : "questions"}
              </span>
            </span>

            <span className="text-slate-300">
              <span className="text-slate-500">Refusals </span>
              <span
                data-testid="refusal-tally"
                className={`font-medium ${
                  summary.refusal_total > 0 && summary.refusal_pass < summary.refusal_total
                    ? "text-rose-300"
                    : "text-emerald-300"
                }`}
              >
                {summary.refusal_pass} / {summary.refusal_total}
              </span>
              <span className="text-slate-500"> correctly declined</span>
            </span>
          </div>

          <p className="mt-2 text-xs leading-relaxed text-slate-500">
            Refusal questions are graded pass/fail on behaviour and excluded from all four
            means: a correct refusal has no useful context and an answer that deliberately
            does not follow from it, so scoring it would punish the agent for being right.
          </p>
        </div>
      )}

      {/*
        Provenance. Two model names and one sentence, because a scorecard whose
        judge is also its author is a different kind of evidence from one whose
        judge is independent -- and that distinction is invisible unless it is
        stated.
      */}
      <div className="rounded-lg border border-slate-800 bg-slate-900/30 p-4 text-sm">
        <div className="flex flex-wrap gap-x-8 gap-y-2">
          <span className="text-slate-400">
            Answers written by{" "}
            <span data-testid="generation-model" className="font-mono text-xs text-slate-200">
              {run.generation_model ?? "unrecorded"}
            </span>
          </span>
          <span className="text-slate-400">
            Judged by{" "}
            <span data-testid="judge-model" className="font-mono text-xs text-slate-200">
              {run.judge_model ?? "unrecorded"}
            </span>
          </span>
        </div>

        {run.judge_is_generator && (
          <p
            data-testid="self-judged"
            className="mt-3 rounded-md border border-amber-800/60 bg-amber-950/30 px-3 py-2 text-xs leading-relaxed text-amber-200"
          >
            The judge and the generator are the same model, so{" "}
            <span className="font-medium">faithfulness is self-assessed</span>: this run
            asked the model whether its own answer followed from its own context. Treat the
            number as a smoke test rather than as an independent measurement, and point{" "}
            <code className="rounded bg-amber-950/60 px-1 font-mono">EVAL_JUDGE_MODEL</code>{" "}
            at a different model before quoting it.
          </p>
        )}
      </div>

      <div>
        <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
          <h4 className="text-xs font-medium tracking-wide text-slate-400 uppercase">
            Per question
          </h4>
          {failing.length > 0 && (
            <label className="flex items-center gap-2 text-xs text-slate-400">
              <input
                type="checkbox"
                checked={showOnlyFailures}
                onChange={(event) => setShowOnlyFailures(event.target.checked)}
                className="h-3.5 w-3.5 accent-rose-500"
              />
              Show only the {failing.length} {failing.length === 1 ? "row" : "rows"} that
              need attention
            </label>
          )}
        </div>

        {run.results.length === 0 ? (
          <p className="rounded-lg border border-dashed border-slate-800 px-4 py-8 text-center text-sm text-slate-500">
            No per-question results yet.
          </p>
        ) : (
          <div className="overflow-x-auto rounded-lg border border-slate-800">
            <table className="w-full text-left text-sm">
              <thead className="bg-slate-900/60 text-xs tracking-wide text-slate-500 uppercase">
                <tr>
                  <th className="px-3 py-2 font-medium">Question</th>
                  <th className="px-3 py-2 font-medium">Expected</th>
                  <th className="px-3 py-2 font-medium">Behaviour</th>
                  <th className="px-3 py-2 text-right font-medium" title="Faithfulness">
                    Faith
                  </th>
                  <th className="px-3 py-2 text-right font-medium" title="Answer relevance">
                    Rel
                  </th>
                  <th className="px-3 py-2 text-right font-medium" title="Context precision">
                    Prec
                  </th>
                  <th className="px-3 py-2 text-right font-medium" title="Context recall">
                    Rec
                  </th>
                  <th className="px-3 py-2" />
                </tr>
              </thead>
              <tbody>
                {rows.map((result) => {
                  const open = expanded.has(result.id);
                  const failed = isFailing(result);
                  const refusalRow = result.expected_behaviour === "refuse";

                  return (
                    <ResultRow
                      key={result.id}
                      result={result}
                      open={open}
                      failed={failed}
                      refusalRow={refusalRow}
                      onToggle={() => toggle(result.id)}
                    />
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </section>
  );
}

// --------------------------------------------------------------------------
// Pieces
// --------------------------------------------------------------------------

function MetricCard({
  metric,
  value,
  weakest,
}: {
  metric: MetricSpec;
  value: number | null;
  weakest: boolean;
}) {
  const scored = value !== null && value !== undefined;

  return (
    <div
      data-testid="metric-card"
      data-metric={metric.key}
      data-value={scored ? value.toFixed(2) : "null"}
      className={`rounded-xl border p-4 ${
        weakest
          ? "border-amber-700/60 bg-amber-950/20"
          : "border-slate-800 bg-slate-900/40"
      }`}
    >
      <p className="text-xs font-medium tracking-wide text-slate-400 uppercase">
        {metric.label}
      </p>

      <p className="mt-2 font-mono text-3xl text-slate-100">
        {scored ? (
          value.toFixed(2)
        ) : (
          /*
            Not "0.00", and not "—". Null means the metric could not be
            computed -- most often a missing reference answer, which makes
            context_recall uncomputable while everything else scores fine. Zero
            would claim the opposite: that it WAS computed and the answer was
            unsupported.
          */
          <span className="text-base text-slate-500 italic">not scored</span>
        )}
      </p>

      {/* The bar is drawn only for a real number, for the same reason. An
          empty track next to "not scored" reads as a zero. */}
      <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-slate-800">
        {scored && (
          <div
            className={`h-full rounded-full ${barColour(value)}`}
            style={{ width: `${Math.max(0, Math.min(1, value)) * 100}%` }}
          />
        )}
      </div>

      <p className="mt-3 text-xs leading-relaxed text-slate-500">{metric.measures}</p>
    </div>
  );
}

function ResultRow({
  result,
  open,
  failed,
  refusalRow,
  onToggle,
}: {
  result: EvalResult;
  open: boolean;
  failed: boolean;
  refusalRow: boolean;
  onToggle: () => void;
}) {
  return (
    <>
      <tr
        data-testid="eval-result-row"
        data-behaviour-ok={String(result.behaviour_ok)}
        data-failed={String(failed)}
        className={`border-t border-slate-800 ${failed ? "bg-rose-950/20" : ""}`}
      >
        <td className="max-w-md px-3 py-3">
          <span className={failed ? "text-rose-100" : "text-slate-200"}>{result.question}</span>
          {result.error && (
            // One question failing inside an otherwise good run. Shown in the
            // row rather than at the top, because it voids this row's numbers
            // and nothing else's.
            <p className="mt-1 text-xs whitespace-pre-wrap text-rose-300">{result.error}</p>
          )}
        </td>

        <td className="px-3 py-3 text-xs text-slate-400">
          {refusalRow ? "refuse" : "answer"}
        </td>

        <td className="px-3 py-3">
          <BehaviourResult ok={result.behaviour_ok} refused={result.refused} />
        </td>

        {refusalRow ? (
          /*
            One cell across all four metric columns, saying why they are empty.
            Four dashes would read as "the judge failed on this row"; this row
            was never sent to the judge at all, and that is by design.
          */
          <td colSpan={4} className="px-3 py-3 text-center text-xs text-slate-500 italic">
            graded pass/fail — excluded from the means
          </td>
        ) : (
          <>
            <ScoreCell value={result.faithfulness} />
            <ScoreCell value={result.answer_relevance} />
            <ScoreCell value={result.context_precision} />
            <ScoreCell value={result.context_recall} />
          </>
        )}

        <td className="px-3 py-3 text-right">
          <button
            type="button"
            aria-expanded={open}
            onClick={onToggle}
            className="rounded-md border border-slate-700 bg-slate-900 px-2.5 py-1 text-xs text-slate-300 transition hover:border-slate-600"
          >
            {open ? "Hide" : "Answer"}
          </button>
        </td>
      </tr>

      {open && (
        <tr className="border-t border-slate-800/60 bg-slate-950/40">
          <td colSpan={8} className="px-3 py-3">
            <p className="text-xs tracking-wide text-slate-500 uppercase">Answer given</p>
            <p className="mt-1.5 max-w-4xl text-sm leading-relaxed whitespace-pre-wrap text-slate-300">
              {result.answer?.trim() || "(no answer recorded)"}
            </p>
            {result.refused && (
              <p className="mt-2 text-xs text-amber-300">
                The agent declined. For a refusal question that is the correct outcome; for
                an answer question it means the retrieved context did not support one.
              </p>
            )}
          </td>
        </tr>
      )}
    </>
  );
}

function ScoreCell({ value }: { value: number | null }) {
  const scored = value !== null && value !== undefined;
  return (
    <td
      className={`px-3 py-3 text-right font-mono text-xs ${
        scored ? textColour(value) : "text-slate-600"
      }`}
    >
      {scored ? value.toFixed(2) : "—"}
    </td>
  );
}

function BehaviourResult({ ok, refused }: { ok: boolean | null; refused: boolean }) {
  if (ok === null || ok === undefined) {
    return <span className="text-xs text-slate-500">unknown</span>;
  }
  return (
    <span
      className={`inline-block rounded-full border px-2 py-0.5 text-[0.65rem] font-medium tracking-wide uppercase ${
        ok
          ? "border-emerald-800/60 bg-emerald-950/40 text-emerald-300"
          : "border-rose-800/60 bg-rose-950/40 text-rose-300"
      }`}
    >
      {ok ? "as expected" : refused ? "refused" : "answered"}
    </span>
  );
}

// --------------------------------------------------------------------------
// Helpers
// --------------------------------------------------------------------------

/**
 * A row worth looking at first: the agent did the opposite of what the question
 * expected, or the question errored.
 *
 * Deliberately not "any low score". A 0.55 faithfulness is a finding for the
 * whole run, which the weakest-metric hero already reports; a `behaviour_ok:
 * false` is a specific question that went wrong and can be read.
 */
function isFailing(result: EvalResult): boolean {
  return result.behaviour_ok === false || Boolean(result.error);
}

function formatMetric(value: number | null | undefined): string {
  return value === null || value === undefined ? "not scored" : value.toFixed(2);
}

function barColour(value: number): string {
  if (value >= 0.8) return "bg-emerald-500";
  if (value >= 0.6) return "bg-amber-500";
  return "bg-rose-500";
}

function textColour(value: number): string {
  if (value >= 0.8) return "text-emerald-300";
  if (value >= 0.6) return "text-amber-300";
  return "text-rose-300";
}
