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
import {
  BAD_TONE,
  BTN_SECONDARY,
  BTN_SM,
  CARD,
  CARD_EMPTY,
  EYEBROW,
  NEUTRAL_TONE,
  NOTICE,
  OK_TONE,
  PILL,
  PROSE,
  WARN_TONE,
  WELL,
} from "../lib/styles.ts";

/**
 * The hero card and the weakest metric card, spelled out rather than composed.
 *
 * `CARD` is `border-line`; these two want `border-line-strong`, and appending
 * one border-colour utility to another leaves the winner to be decided by their
 * order in the GENERATED STYLESHEET rather than by their order in a template
 * literal -- the coin-flip this codebase already documents for `contents` /
 * `hidden`. One complete string has no such ambiguity.
 *
 * The stronger EDGE is how prominence is expressed here, and that is the point
 * of the change: the weakest metric used to be the largest card on the page in
 * the WARNING hue, so the page's central FINDING read as a fault. It is not a
 * fault. It is the answer to "what should I work on next", which is what this
 * whole screen exists to produce.
 */
const CARD_STRONG = "rounded-lg border border-line-strong bg-surface";

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
          <h3 className="text-lg font-semibold tracking-tight text-ink">Scorecard</h3>
          <p className="mt-1 text-xs text-muted">
            {formatTimestamp(run.started_at ?? null)}
            {run.finished_at ? ` — finished ${formatTimestamp(run.finished_at)}` : ""}
          </p>
        </div>
        <p className="text-xs text-muted">
          <span className="font-mono tabular-nums text-ink">{run.results.length}</span>{" "}
          {run.results.length === 1 ? "question" : "questions"} in this run
        </p>
      </div>

      {run.notes && (
        <p className={`${WELL} px-3 py-2 text-xs text-ink`}>
          <span className="text-muted">What changed: </span>
          {run.notes}
        </p>
      )}

      {run.error && (
        // Run-level failure, distinct from a single question's error below. This
        // is the reason a run ended with no summary at all.
        <p className={`${NOTICE} ${BAD_TONE} text-sm whitespace-pre-wrap`}>
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
          className={`${CARD_STRONG} p-5`}
        >
          <p className={EYEBROW}>Weakest metric — your next investment</p>
          <p className="mt-2 flex flex-wrap items-baseline gap-3">
            <span className="text-2xl font-semibold tracking-tight text-ink">
              {weakest.label}
            </span>
            <span className="font-mono text-2xl tabular-nums text-ink">
              {formatMetric(summary.weakest_score)}
            </span>
            {/*
              The ONE tinted thing on this card, and it names the band rather
              than the mood. A weakest metric of 0.91 is a good scorecard, and
              the old card said "warning" about it in 40 square centimetres of
              amber. Tone follows the band, so the alarming colour appears only
              when the number is actually alarming.
            */}
            <span className={`${PILL} ${bandTone(summary.weakest_score)}`}>
              {bandLabel(summary.weakest_score)}
            </span>
          </p>
          <p className="mt-3 text-sm font-medium text-ink">{weakest.investmentFor}</p>
          <p className="mt-2 max-w-3xl text-sm leading-relaxed text-muted">{weakest.why}</p>
          <ul className="mt-3 space-y-1.5">
            {weakest.knobs.map((knob) => (
              <li key={knob} className="flex gap-2 text-sm text-muted">
                <span aria-hidden="true" className="text-accent">
                  &rsaquo;
                </span>
                <span>{knob}</span>
              </li>
            ))}
          </ul>
          <p className="mt-4 border-t border-line pt-3 text-xs leading-relaxed text-muted">
            Change one thing, re-run the same golden set, and write what you changed in the
            notes. Two runs that differ in one parameter are an experiment; two that differ
            in five are a story.
          </p>
        </div>
      )}

      {!summary && (
        <p className={`${WELL} px-4 py-3 text-sm text-muted`}>
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
        <div className={`${CARD} p-5`}>
          <div className="flex flex-wrap items-center gap-x-6 gap-y-3 text-sm text-muted">
            <span className="inline-flex flex-wrap items-center gap-2">
              Means rest on
              <span data-testid="scored-count" className="font-mono tabular-nums text-ink">
                {summary.scored_count}
              </span>
              scored {summary.scored_count === 1 ? "question" : "questions"}
            </span>

            <span className="inline-flex flex-wrap items-center gap-2">
              Refusals
              <span
                data-testid="refusal-tally"
                className={`${PILL} font-mono tabular-nums ${
                  summary.refusal_total > 0 && summary.refusal_pass < summary.refusal_total
                    ? BAD_TONE
                    : OK_TONE
                }`}
              >
                {summary.refusal_pass} / {summary.refusal_total}
              </span>
              correctly declined
            </span>
          </div>

          <p className="mt-3 text-xs leading-relaxed text-muted">
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
      <div className={`${CARD} p-5 text-sm`}>
        <div className="flex flex-wrap gap-x-8 gap-y-3">
          <span className="inline-flex flex-wrap items-center gap-2 text-muted">
            Answers written by
            <span
              data-testid="generation-model"
              className={`${WELL} inline-block px-2 py-0.5 font-mono text-xs text-ink`}
            >
              {run.generation_model ?? "unrecorded"}
            </span>
          </span>
          <span className="inline-flex flex-wrap items-center gap-2 text-muted">
            Judged by
            <span
              data-testid="judge-model"
              className={`${WELL} inline-block px-2 py-0.5 font-mono text-xs text-ink`}
            >
              {run.judge_model ?? "unrecorded"}
            </span>
          </span>
        </div>

        {run.judge_is_generator ? (
          <p data-testid="self-judged" className={`${NOTICE} ${WARN_TONE} mt-3`}>
            The judge and the generator are the same model, so{" "}
            <span className="font-medium">faithfulness is self-assessed</span>: this run
            asked the model whether its own answer followed from its own context. Treat the
            number as a smoke test rather than as an independent measurement, and point{" "}
            <code className={`${WELL} px-1 font-mono`}>EVAL_JUDGE_MODEL</code> at a different
            model before quoting it.
          </p>
        ) : (
          /*
            The reassuring half, which had no rendering at all before -- an
            independent judge was communicated by the ABSENCE of a warning,
            which is not communication. This is the single property that makes
            the four numbers above quotable, and it is the one thing on a
            scorecard worth stating in the affirmative.
          */
          <p className={`${NOTICE} ${OK_TONE} mt-3`}>
            The judge is a different model from the one that answered, so{" "}
            <span className="font-medium">faithfulness is measured independently</span>{" "}
            rather than self-assessed. This is what makes the four numbers above worth
            quoting outside this page.
          </p>
        )}
      </div>

      <div>
        <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
          <h4 className={EYEBROW}>Per question</h4>
          {failing.length > 0 && (
            /*
              `min-h-11` on the LABEL, not on the box: a 44px checkbox would be
              a different control, and the label is what the finger lands on.

              The input itself is `sr-only` and the box is drawn by the span
              beside it, which is the same arrangement `Segmented` uses in
              `ui.tsx`. That is not styling preference -- a native checkbox is
              14px, and `ui_check.py` A8 measures every `input` on the page and
              fails anything under 43.5px. It does not currently drive the
              Evaluate tab, so this control was one coverage change away from
              reding a suite it had always violated. `sr-only` is in A8's own
              exclusion list, so the assertion now measures the 44px label,
              which is the thing a finger actually hits.

              The real checkbox is still the state: it keeps the checked
              semantics, the keyboard behaviour and the accessible name from the
              label wrapping it. Only the painting moved.
            */
            <label className="flex min-h-11 cursor-pointer items-center gap-2 text-xs text-muted">
              <input
                type="checkbox"
                checked={showOnlyFailures}
                onChange={(event) => setShowOnlyFailures(event.target.checked)}
                className="peer sr-only"
              />
              {/* `text-transparent` -> `peer-checked:text-inverse` rather than
                  toggling the tick's own opacity: Tailwind's `peer-*` selector
                  matches following SIBLINGS, not their descendants, so a rule
                  aimed at the svg inside would never fire. Colouring the box and
                  letting `currentColor` carry the tick keeps it to one rule. */}
              <span
                aria-hidden="true"
                className="flex h-4 w-4 shrink-0 items-center justify-center rounded-sm border border-line-strong bg-field text-transparent transition peer-checked:border-accent peer-checked:bg-accent peer-checked:text-inverse"
              >
                <svg width="10" height="10" viewBox="0 0 12 12" fill="none">
                  <path
                    d="M2.5 6.2l2.3 2.3L9.5 3.8"
                    stroke="currentColor"
                    strokeWidth="2"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  />
                </svg>
              </span>
              Show only the {failing.length} {failing.length === 1 ? "row" : "rows"} that
              need attention
            </label>
          )}
        </div>

        {run.results.length === 0 ? (
          <p className={`${CARD_EMPTY} px-4 py-8 text-center text-sm text-muted`}>
            No per-question results yet.
          </p>
        ) : (
          <div className={`${CARD} overflow-x-auto`}>
            <table className="w-full text-left text-sm">
              <thead className="bg-sunken text-xs font-semibold text-faint">
                <tr>
                  <th className="px-3 py-2.5">Question</th>
                  <th className="px-3 py-2.5">Expected</th>
                  <th className="px-3 py-2.5">Behaviour</th>
                  <th className="px-3 py-2.5 text-right" title="Faithfulness">
                    Faith
                  </th>
                  <th className="px-3 py-2.5 text-right" title="Answer relevance">
                    Rel
                  </th>
                  <th className="px-3 py-2.5 text-right" title="Context precision">
                    Prec
                  </th>
                  <th className="px-3 py-2.5 text-right" title="Context recall">
                    Rec
                  </th>
                  <th className="px-3 py-2.5" />
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
      // The weakest card echoes the hero's stronger EDGE rather than taking a
      // hue. Same reasoning as `CARD_STRONG`: this is emphasis, not alarm.
      className={`${weakest ? CARD_STRONG : CARD} p-5`}
    >
      <p className={EYEBROW}>{metric.label}</p>

      <p className="mt-2 font-mono text-3xl font-semibold tabular-nums text-ink">
        {scored ? (
          value.toFixed(2)
        ) : (
          /*
            Not "0.00", and not "—". Null means the metric could not be
            computed -- most often a missing reference answer, which makes
            context_recall uncomputable while everything else scores fine. Zero
            would claim the opposite: that it WAS computed and the answer was
            unsupported.

            Set in SANS, because "not scored" is the harness speaking about the
            absence of a measurement; everything mono on this page IS one.
          */
          <span className="font-sans text-base text-muted italic">not scored</span>
        )}
      </p>

      {/* The bar is drawn only for a real number, for the same reason. An
          empty track next to "not scored" reads as a zero. */}
      <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-sunken">
        {scored && (
          <div
            className={`h-full rounded-full ${barColour(value)}`}
            style={{ width: `${Math.max(0, Math.min(1, value)) * 100}%` }}
          />
        )}
      </div>

      <p className="mt-3 text-xs leading-relaxed text-muted">{metric.measures}</p>
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
        className={`border-t border-line ${failed ? "bg-bad-soft" : ""}`}
      >
        <td className="max-w-md px-3 py-3">
          <span className="text-ink">{result.question}</span>
          {result.error && (
            // One question failing inside an otherwise good run. Shown in the
            // row rather than at the top, because it voids this row's numbers
            // and nothing else's.
            <p className={`${NOTICE} ${BAD_TONE} mt-1.5 whitespace-pre-wrap`}>
              {result.error}
            </p>
          )}
        </td>

        <td className="px-3 py-3 text-xs text-muted">{refusalRow ? "refuse" : "answer"}</td>

        <td className="px-3 py-3">
          <BehaviourResult ok={result.behaviour_ok} refused={result.refused} />
        </td>

        {refusalRow ? (
          /*
            One cell across all four metric columns, saying why they are empty.
            Four dashes would read as "the judge failed on this row"; this row
            was never sent to the judge at all, and that is by design.
          */
          <td colSpan={4} className="px-3 py-3 text-center text-xs text-muted italic">
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
            className={`${BTN_SECONDARY} ${BTN_SM}`}
          >
            {open ? "Hide" : "Answer"}
          </button>
        </td>
      </tr>

      {open && (
        <tr className="border-t border-line bg-sunken">
          <td colSpan={8} className="px-3 py-3">
            <p className={EYEBROW}>Answer given</p>
            {/*
              Serif, because this is the only text on the page that came out of
              the corpus -- the model's own answer, assembled from the user's
              documents. Everything around it is the harness talking ABOUT that
              answer, and is set in sans. `PROSE` also supplies the 65ch measure,
              which is what makes a long answer readable rather than a wall.
            */}
            <p className={`${PROSE} mt-1.5 whitespace-pre-wrap`}>
              {result.answer?.trim() || "(no answer recorded)"}
            </p>
            {result.refused && (
              <p className={`${NOTICE} ${NEUTRAL_TONE} mt-3`}>
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
      className={`px-3 py-3 text-right font-mono text-xs tabular-nums ${
        scored ? textColour(value) : "text-faint"
      }`}
    >
      {scored ? value.toFixed(2) : "—"}
    </td>
  );
}

function BehaviourResult({ ok, refused }: { ok: boolean | null; refused: boolean }) {
  if (ok === null || ok === undefined) {
    return <span className={`${PILL} ${NEUTRAL_TONE}`}>unknown</span>;
  }
  return (
    <span className={`${PILL} ${ok ? OK_TONE : BAD_TONE}`}>
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

/**
 * The three bands, and the ONE place their boundaries are written.
 *
 * 0.80 and 0.60 are unchanged from the original -- this is a re-mapping of hue
 * onto the state tokens, not a re-calibration. `barColour`, `textColour` and
 * `bandTone` must agree, or a bar and the number beside it would disagree about
 * the same value, which is the sort of thing nobody notices and nobody trusts
 * once they do.
 */
function barColour(value: number): string {
  if (value >= 0.8) return "bg-ok";
  if (value >= 0.6) return "bg-warn";
  return "bg-bad";
}

function textColour(value: number): string {
  if (value >= 0.8) return "text-ok";
  if (value >= 0.6) return "text-warn";
  return "text-bad";
}

/**
 * The band as a WORD, for the one pill on the weakest-metric card.
 *
 * A hero card that says "0.91" and is tinted like a warning is telling the
 * reader two different things, and the tint is the louder one. Naming the band
 * lets the number carry the finding and the colour carry only the severity --
 * which, on a good run, is none.
 */
function bandLabel(value: number | null | undefined): string {
  if (value === null || value === undefined) return "not scored";
  if (value >= 0.8) return "strong";
  if (value >= 0.6) return "moderate";
  return "weak";
}

function bandTone(value: number | null | undefined): string {
  if (value === null || value === undefined) return NEUTRAL_TONE;
  if (value >= 0.8) return OK_TONE;
  if (value >= 0.6) return WARN_TONE;
  return BAD_TONE;
}
