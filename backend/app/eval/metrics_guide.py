"""Aggregation, and the mapping from a weak metric to the knob that fixes it.

A scorecard that does not tell you what to do next is a dashboard, not a
measurement.

That sentence is the whole reason this module exists as something other than
four calls to `statistics.mean`. PRD 4.4 asks for eval-driven development: run
the golden set, read the weakest number, change one thing, run it again. The
middle step is the one that is usually missing -- four floats between 0 and 1
tell a workshop attendee that something is wrong and nothing about which of the
eight per-agent parameters to touch. `investment_for` is that step written down,
and every entry in it names a column on `agents` rather than offering advice.

**Two aggregation rules make the numbers mean what they appear to mean**, and
both are easy to get wrong in a way that still renders:

1. Refusal rows are excluded from the metric means. See `summarise`.
2. A metric with no scored rows is None, not 0.0. A run where every judge call
   timed out must not report perfect-zero faithfulness and point the user at
   their system prompt; it must report that it measured nothing.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import BaseModel, Field

# The four metric keys, in the order the scorecard shows them. These strings are
# a contract in three places at once -- `eval_results` columns, the keys of the
# dicts `ragas_runner.score_samples` returns, and the keys inside
# `eval_runs.summary` -- and nothing in the database will catch a typo, because
# JSONB has no shape. Defined once here and imported everywhere else.
METRIC_KEYS: tuple[str, ...] = (
    "faithfulness",
    "answer_relevance",
    "context_precision",
    "context_recall",
)

# What each metric is called in the UI. Not decoration: "context_precision" and
# "context_recall" are near-anagrams of each other at a glance, and the whole
# exercise turns on the reader noticing which of the two is lower.
METRIC_LABELS: dict[str, str] = {
    "faithfulness": "Faithfulness",
    "answer_relevance": "Answer relevance",
    "context_precision": "Context precision",
    "context_recall": "Context recall",
}


class RunSummary(BaseModel):
    """The shape of `eval_runs.summary`.

    `eval_runs.summary` is JSONB with no enforced shape, so this model IS the
    schema -- the runner writes through it and the API reads back through it,
    which is the only thing standing between a renamed key and a scorecard that
    silently renders blanks. Do not write the column from a hand-built dict.

    Every metric field is optional and None means "not measured", which is a
    different fact from 0.0 and must survive to the UI as a different fact.
    """

    faithfulness: float | None = None
    answer_relevance: float | None = None
    context_precision: float | None = None
    context_recall: float | None = None

    # The lowest of the four, and its value. Null when nothing was scored.
    weakest_metric: str | None = None
    weakest_score: float | None = None

    # How many rows the means above actually rest on. A mean over two rows and a
    # mean over ten render identically and are not equally trustworthy, so the
    # denominator travels with the numbers rather than being inferable from a
    # separate query.
    scored_count: int = 0
    # Every active question in the run, refusals and failures included.
    total_count: int = 0
    # Rows that produced no usable metric at all -- a judge timeout, a
    # generation error. Counted separately from refusals, which are a success.
    error_count: int = 0

    # The refusal tally, reported instead of averaged. See `summarise`.
    refusal_pass: int = 0
    refusal_total: int = 0

    # True when the model that wrote the answers also graded them. Stored rather
    # than derived so the scorecard can caveat itself without the reader having
    # to compare two model names by eye.
    self_judged: bool = False

    # The advice for `weakest_metric`, resolved at write time so a stored
    # scorecard is self-contained: `{headline, why, actions: [...]}`.
    investment: dict[str, Any] | None = None

    # Populated only when `summarise` had nothing to work with -- an empty
    # golden set, or every row failed. Rendered instead of the numbers.
    note: str | None = Field(default=None)

    # The trajectory rubric (change set 16): whether the agent DID the right
    # thing, beside the four metrics above on whether its ANSWER was faithful.
    # None means the second scoring pass did not run -- this run predates it, or
    # `EVAL_TRAJECTORY_ENABLED` was off.
    #
    # **DECLARED HERE RATHER THAN MERGED INTO THE DICT, and the docstring above
    # is why.** `eval_runs.summary` is JSONB with no enforced shape, so this
    # model IS the schema: the runner writes through it and the API reads back
    # through it. Pydantic's default is `extra="ignore"`, so a key written by a
    # hand-built dict is stored happily and then **silently dropped on the way
    # out** -- the column holds it, the API never returns it, and nothing raises
    # at either end. That is exactly the failure this docstring warns about, and
    # it was live for the length of one commit: the job wrote
    # `{**run.summary, "trajectory": ...}`, S35 read the column directly and went
    # green, and `GET /api/eval-runs/{id}` returned a summary with the block
    # missing. Found by reading the model, not by a failing assertion.
    # `agent_metrics_check.py` case 38 is the round-trip that now guards it.
    trajectory: dict[str, Any] | None = None


# --------------------------------------------------------------------------
# The mapping. This is the deliverable.
# --------------------------------------------------------------------------

# Keyed by metric. Each entry answers "the score is low -- which knob?" and the
# knobs are real: `agents.system_prompt`, `agents.generation_model`,
# `agents.rerank_top_n`, `agents.retrieve_k`, `agents.chunk_size`,
# `agents.chunk_overlap` are all editable per agent (PRD 4.2), which is what
# makes the loop closeable inside the app instead of in a config file.
#
# The split that matters and is not obvious: faithfulness and answer relevance
# are GENERATION failures, context precision and context recall are RETRIEVAL
# failures. Half of these four metrics being low means the retriever is fine and
# the prompt is not, and reaching for `retrieve_k` in that case makes the run
# slower and changes nothing.
_INVESTMENTS: dict[str, dict[str, Any]] = {
    "faithfulness": {
        "headline": "The answers are drifting from the retrieved text.",
        "why": (
            "Faithfulness asks whether every claim in the answer is supported by "
            "the chunks that were in the prompt. A low score means the model is "
            "adding material the context did not give it -- from its own training "
            "data, or by over-elaborating a thin context. This is a GENERATION "
            "problem: the retriever found something and the generator went past "
            "it. Raising retrieve_k here makes runs slower and does not help."
        ),
        "actions": [
            "Tighten the grounding clause in the agent's system prompt. "
            "'Answer only from the CONTEXT below' has to be present and early; "
            "a persona prepended in front of it competes with it.",
            "Check whether the persona is padding. A prompt that asks for an "
            "analogy and a worked example makes the model write about ten times "
            "as much text, and the extra text is the part with nothing behind "
            "it. Shorten the persona in the agent's system prompt.",
            "Try a stronger generation model (the agent's generation_model). "
            "Instruction-following on 'do not go beyond the context' is exactly "
            "where a larger model earns its cost.",
        ],
    },
    "answer_relevance": {
        "headline": "The answers are grounded but are not answering the question.",
        "why": (
            "Answer relevance works backwards: it asks the judge to invent the "
            "questions this answer would be a good answer to, and compares them "
            "to the one actually asked. A low score with healthy faithfulness "
            "means the model is reciting the context rather than using it -- "
            "correct material, wrong shape. This is a prompt problem, not a "
            "retrieval one."
        ),
        "actions": [
            "Make the system prompt ask for the answer first. A persona that "
            "opens with a restatement of the question, a preamble or a "
            "disclaimer buries the part being scored.",
            "Reduce persona verbosity in the agent's system prompt. Long "
            "answers dilute: the more surrounding material, the more questions "
            "the answer looks like a reply to.",
            "Read three low-scoring answers next to their questions before "
            "changing anything. This metric is the one most often low because "
            "the golden question itself is vague.",
        ],
    },
    "context_precision": {
        "headline": "Retrieval is putting junk in the top-n.",
        "why": (
            "Context precision asks how much of what reached the prompt was "
            "actually relevant, weighted towards the top. A low score means the "
            "final context set is padded with chunks that do not bear on the "
            "question -- which costs tokens, costs latency, and gives the "
            "generator material to drift into. The reranker and the size of the "
            "final set are the levers."
        ),
        "actions": [
            "Lower the agent's rerank_top_n. Fewer, better chunks is the direct "
            "fix, and it is the cheapest thing on this list to try.",
            "Confirm rerank_enabled is on. Precision is what reranking buys; "
            "without it the top-n is whatever the embedding put there.",
            "Lower retrieve_k. A smaller candidate pool gives the reranker less "
            "opportunity to promote something plausible but irrelevant.",
            "Revisit chunk_size. Chunks that are too large carry unrelated "
            "material along with the relevant sentence, and the judge counts "
            "that whole chunk as imprecise.",
        ],
    },
    "context_recall": {
        "headline": "Retrieval is missing text the answer needed.",
        "why": (
            "Context recall compares the reference answer against what was "
            "retrieved: how much of what the answer SHOULD have said was even "
            "available in the context. A low score means the corpus probably "
            "contains the answer and the search did not surface it -- so no "
            "amount of prompt work will fix it. This is the one metric that is "
            "worth checking the corpus over, not just the parameters."
        ),
        "actions": [
            "Raise the agent's retrieve_k, then rerank_top_n. Recall is the one "
            "failure a bigger candidate pool genuinely fixes.",
            "Increase chunk_size and chunk_overlap. A fact split across a chunk "
            "boundary is retrievable from neither half; overlap is what stops "
            "that. Both require re-ingesting the documents to take effect.",
            "Check that the agent's embedding_model matches the model the index "
            "was built with. A mismatch returns confident nonsense rather than "
            "an error, and it looks exactly like poor recall.",
            "Check the document is actually in the corpus and ingested to "
            "'ready'. Recall cannot find what was never indexed.",
        ],
    },
}


def investment_for(metric: str) -> dict[str, Any]:
    """What to do about a weak `metric`: `{metric, label, headline, why, actions}`.

    Returns a filled-in entry for an unrecognised metric rather than raising.
    This is read on the way to rendering a scorecard, and a run that finished
    should not fail to display because a metric was renamed -- the honest
    outcome is "the numbers, and no advice", not a 500.
    """
    entry = _INVESTMENTS.get(metric)
    if entry is None:
        return {
            "metric": metric,
            "label": METRIC_LABELS.get(metric, metric),
            "headline": "No guidance recorded for this metric.",
            "why": "",
            "actions": [],
        }
    return {
        "metric": metric,
        "label": METRIC_LABELS.get(metric, metric),
        "headline": entry["headline"],
        "why": entry["why"],
        "actions": list(entry["actions"]),
    }


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------

def _mean(values: list[float]) -> float | None:
    """Mean, or None for an empty list. Never 0.0 for 'nothing to average'."""
    if not values:
        return None
    return sum(values) / len(values)


def summarise(
    results: Iterable[Mapping[str, Any]],
    *,
    self_judged: bool = False,
    trajectory: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Roll per-question results up into `eval_runs.summary`.

    Each item needs `expected_behaviour` ("answer" or "refuse"), `behaviour_ok`
    (bool | None), `error` (str | None) and the four metric keys with float or
    None values -- which is exactly what `ragas_runner.score_samples` returns.

    ------------------------------------------------------------------
    REFUSAL ROWS ARE EXCLUDED FROM ALL FOUR MEANS. This is the least
    obvious and most consequential decision in Stage 3.

    The golden set deliberately contains questions the corpus cannot answer
    (`golden_questions.expected_behaviour = 'refuse'`), and PRD 4.4 is explicit
    that refusing them is a CORRECT outcome. But Ragas has nothing to grade on
    such a row: a correct refusal retrieved nothing useful, and its answer
    deliberately does not follow from the context, so faithfulness and context
    recall score near zero for behaving perfectly.

    Averaging those zeros in does not merely add noise, it inverts the signal.
    The scorecard would drop as a *reward* for correct refusals, and
    `weakest_metric` -- the entire point of the exercise -- would then point at
    whichever metric refusals punish hardest rather than at the real weakness.
    An agent that refuses well would be told to loosen its grounding prompt.

    So refusal rows are scored as a separate pass/fail on `behaviour_ok` and
    reported as `refusal_pass / refusal_total`. `scored_count` says how many
    rows the means actually rest on. If you ever add a metric here, decide which
    side of this line it falls on before you add it to METRIC_KEYS.
    ------------------------------------------------------------------

    Returns a plain dict (what goes into JSONB), built through `RunSummary` so
    the keys cannot drift from what the API and the scorecard UI read back.
    """
    rows = list(results)

    collected: dict[str, list[float]] = {key: [] for key in METRIC_KEYS}
    scored_count = 0
    error_count = 0
    refusal_pass = 0
    refusal_total = 0

    for row in rows:
        if row.get("error"):
            # Counted, then still examined below: a row can fail one metric and
            # still carry a usable behaviour verdict, and a refusal question that
            # errored during generation is a genuine refusal failure rather than
            # a row to quietly drop.
            error_count += 1

        if row.get("expected_behaviour") == "refuse":
            refusal_total += 1
            if row.get("behaviour_ok"):
                refusal_pass += 1
            # No `continue` needed for correctness -- the runner does not score
            # refusal rows, so their metric values are already None -- but the
            # rule is enforced here as well as there. This function must produce
            # the right means even if it is handed rows from somewhere else.
            continue

        values = [row.get(key) for key in METRIC_KEYS]
        if any(value is not None for value in values):
            scored_count += 1
        for key in METRIC_KEYS:
            value = row.get(key)
            if value is not None:
                collected[key].append(float(value))

    means: dict[str, float | None] = {
        key: _mean(values) for key, values in collected.items()
    }

    # The weakest is the lowest MEASURED metric. A metric with no rows behind it
    # is not "the worst" -- it is unknown, and telling someone to invest in a
    # number that was never computed is worse than telling them nothing.
    measured = {key: value for key, value in means.items() if value is not None}
    weakest_metric = min(measured, key=lambda k: measured[k]) if measured else None
    weakest_score = measured[weakest_metric] if weakest_metric else None

    note: str | None = None
    if not rows:
        note = "No active golden questions -- nothing was scored."
    elif not measured:
        if refusal_total == len(rows):
            note = (
                "Every question in this set expects a refusal, so there is "
                "nothing for the metrics to grade. See the refusal tally."
            )
        else:
            note = (
                "No metric produced a score. Check the run's per-question "
                "errors -- this usually means the judge model was unreachable "
                "or rate limited, not that the agent scored zero."
            )

    summary = RunSummary(
        **means,
        weakest_metric=weakest_metric,
        weakest_score=weakest_score,
        scored_count=scored_count,
        total_count=len(rows),
        error_count=error_count,
        refusal_pass=refusal_pass,
        refusal_total=refusal_total,
        self_judged=self_judged,
        investment=investment_for(weakest_metric) if weakest_metric else None,
        note=note,
        # Passed IN rather than merged on afterwards. `RunSummary` is the
        # schema for a JSONB column with no enforced shape, and pydantic's
        # default `extra="ignore"` means a key added to the returned dict is
        # stored and then silently dropped when the API reads it back. One
        # writer, through the model.
        trajectory=trajectory,
    )
    return summary.model_dump()
