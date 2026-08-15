"""The judge: four Ragas metrics over already-answered turns.

Takes turns that have already been asked and answered by the real pipeline and
returns one dict of scores per turn. It does not retrieve, does not generate and
does not touch the database -- `app/eval/jobs.py` does all three and calls this.
That split is what makes the judge testable without a corpus and what keeps a
rate-limited judge from being able to corrupt a `queries` row.

------------------------------------------------------------------
THREE THINGS HERE WERE ESTABLISHED BY READING THE INSTALLED PACKAGE,
NOT BY FOLLOWING A TUTORIAL. All three are load-bearing.

1. **`ragas.metrics.collections` cannot be used with a LangChain model.**
   Importing from `ragas.metrics` emits a DeprecationWarning pointing at
   `ragas.metrics.collections`, and following it literally fails twice over: the
   class names differ (`ResponseRelevancy` -> `AnswerRelevancy`,
   `LLMContextRecall` -> `ContextRecall`) AND the new base class actively
   rejects the wrapper this project needs --

       ValueError: Collections metrics only support modern InstructorLLM.
                   Found: LangchainLLMWrapper.

   The collections metrics take an `InstructorBaseRagasLLM`, which for Gemini
   means routing through `instructor.from_genai()`. Ragas' own source carries a
   warning that that path sends invalid safety settings to Google
   (HARM_CATEGORY_JAILBREAK, instructor issue #1658). So the deprecated import
   is the working one in 0.4.3, and the DeprecationWarning below is suppressed
   deliberately rather than left to alarm someone into "fixing" it.

2. **The LangChain wrapper is what makes Gemma survive as a judge.** CLAUDE.md
   records that Gemma emits schema-correct JSON but sometimes wraps it in a
   markdown fence, and that a strict parser answers a fence with `None` rather
   than an exception. Ragas parses judge output in two different ways depending
   on what it was handed: a bare LangChain model takes a path that calls
   `model_validate_json()` on the raw text (a fence breaks it), while a
   `LangchainLLMWrapper` takes the `BaseRagasLLM` path through
   `ragas.prompt.utils.extract_json`, which explicitly looks for "```json",
   walks the balanced braces after it, and falls back to a one-shot
   fix-the-format LLM retry if that still will not parse.

   Verified offline against ragas 0.4.3 with a fake model returning fenced JSON:
   all four metrics scored correctly. So wrapping is not ceremony -- passing the
   `ChatGoogleGenerativeAI` in unwrapped would silently change the parser and
   turn every Gemma fence into a NaN.

3. **`evaluate()` is not used.** It is deprecated in 0.4.3, it is synchronous,
   and it re-enters the event loop through nest_asyncio -- which is exactly the
   wrong shape inside a FastAPI background task on Render's single uvicorn
   worker. It is also all-or-nothing: one batch call, no per-question progress
   for a run that takes several minutes, and its `raise_exceptions=False`
   returns NaN without saying which metric failed or why.
   `metric.single_turn_ascore()` is the same work with the failure boundary
   drawn per metric, which is what lets one bad row land in
   `eval_results.error` instead of voiding the scorecard.
------------------------------------------------------------------
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import warnings
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

# Set BEFORE ragas is imported. Ragas posts usage analytics to a third-party
# endpoint with a blocking `requests.post`, which on a single-worker deployment
# stalls the event loop for up to a second per metric -- and this repository is
# public and its PRD (section 7) is explicit about what leaves the process.
# `setdefault`, so an operator who wants it can still turn it back on.
os.environ.setdefault("RAGAS_DO_NOT_TRACK", "true")

from langchain_google_genai import ChatGoogleGenerativeAI  # noqa: E402

from app.config import settings  # noqa: E402
from app.eval.metrics_guide import METRIC_KEYS  # noqa: E402
from app.rag.retriever import get_embeddings  # noqa: E402

# See point 1 in the module docstring. The warning is suppressed at the import
# itself rather than globally, so a DeprecationWarning from anywhere else in the
# process still surfaces.
with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    from ragas import SingleTurnSample
    from ragas.embeddings import LangchainEmbeddingsWrapper
    from ragas.llms import LangchainLLMWrapper
    from ragas.metrics import (
        Faithfulness,
        LLMContextPrecisionWithReference,
        LLMContextRecall,
        ResponseRelevancy,
    )

log = logging.getLogger("uvicorn.error")

# Per-metric ceiling. A judged metric is several chained LLM calls, and without
# a bound a single hung request would hold a background job open indefinitely
# while `progress_done` sat still -- indistinguishable, from the UI, from a
# crashed worker. 180 s is generous against CLAUDE.md's measured 13.2 s
# generation; it exists to catch a hang, not to hurry a slow judge.
METRIC_TIMEOUT_S = 180.0


@dataclass
class EvalTurn:
    """One already-answered question, ready to be judged.

    Plain data on purpose -- no ORM objects. `app/eval/jobs.py` builds these from
    rows it has already committed, and keeping the judge ignorant of the session
    means a scoring failure can never leave a half-written turn behind.
    """

    question: str
    answer: str
    # The FINAL context set -- the chunk text that was actually in the prompt,
    # read back from `query_chunks`. Not the pre-rerank candidates: context
    # precision over the discarded twenty measures the retriever's recall rather
    # than the answer's grounding, and collapses by construction.
    contexts: list[str] = field(default_factory=list)
    # `golden_questions.reference_answer`. Nullable in the schema, and two of the
    # four metrics genuinely cannot run without it -- see `_metrics_for`.
    reference: str | None = None
    # "answer" or "refuse", from `golden_questions.expected_behaviour`.
    expected_behaviour: str = "answer"
    # What the pipeline actually did, from `queries.refused`.
    refused: bool = False
    # Carried through untouched so the caller can line results up with rows.
    golden_question_id: Any | None = None


@lru_cache(maxsize=4)
def get_judge(model: str) -> LangchainLLMWrapper:
    """The judge model, wrapped. One instance per model name.

    Cached because a ten-question run constructs this forty times otherwise, and
    because `lru_cache` on the model name is what lets `ragas_judge_model` be
    changed by env var without a second construction site appearing.

    Sampling is NOT left at the Gemma model card's defaults, and this is the one
    place in the project that departs from them. `app/rag/pipeline.py` explains
    why the card's `temperature=1.0` is right for generation: Gemma is
    calibrated for it and squeezing it down risks repetition loops. A judge is a
    different job. It emits a handful of tokens of structured verdict, not
    prose, so there is no loop to fall into -- and a scorecard that returns
    different numbers for the same answers on consecutive runs cannot be used to
    tell whether a prompt change helped, which is the entire point of Stage 3.
    0.1 rather than 0 keeps it inside a sampling regime the model has seen.
    """
    return LangchainLLMWrapper(
        ChatGoogleGenerativeAI(
            model=model,
            google_api_key=settings.gemini_api_key,
            temperature=0.1,
            # `top_k`/`top_p` left at the model defaults deliberately: pinning
            # temperature already gets the determinism, and the model card gives
            # these two as a set, so changing one without measuring the others is
            # how a repetition loop is invited back in.
        )
    )


@lru_cache(maxsize=1)
def get_judge_embeddings() -> LangchainEmbeddingsWrapper:
    """The embedding model, wrapped, for answer relevance.

    **Ragas needs a judge LLM *and* an embedding model, and defaults to OpenAI
    for both.** Leave either unset and the failure is
    `OPENAI_API_KEY not set` from deep inside a metric -- which reads like a
    missing dependency rather than a missing argument. Nothing in this project
    calls OpenAI; `langchain-openai` is only present because
    `langchain-pinecone` depends on it.

    Built on `app.rag.retriever.get_embeddings`, not on a second
    `GoogleGenerativeAIEmbeddings`. That function is the one place the model,
    the API key and `output_dimensionality` are configured together, and answer
    relevance compares generated questions against the original in embedding
    space -- so it should be measuring distances in the same space the corpus
    was indexed in, not in a lookalike built from the same env vars.
    """
    return LangchainEmbeddingsWrapper(get_embeddings())


def _metrics_for(turn: EvalTurn, judge: LangchainLLMWrapper) -> dict[str, Any]:
    """The metrics that can actually be computed for this turn.

    Two of the four need `reference`: `LLMContextRecall` compares the reference
    answer against what was retrieved, and `LLMContextPrecisionWithReference`
    judges relevance against it. CLAUDE.md records this as the reason
    `golden_questions.reference_answer` is not decorative. Without one they do
    not fail loudly -- they would be scored against an empty string -- so they
    are omitted, and the key comes back None meaning "not measured".

    Constructed per turn rather than cached. The metric objects are cheap
    (they hold a prompt and a reference to the judge, which IS cached), and
    sharing one mutable metric instance across concurrent scoring is a race
    nobody would find from the symptom.
    """
    metrics: dict[str, Any] = {
        "faithfulness": Faithfulness(llm=judge),
        # **Answer relevance is a cosine similarity and can come back
        # NEGATIVE.** It is the only one of the four not bounded at zero: the
        # judge writes questions the answer would suit and this is their mean
        # similarity to the real one, so an answer about something else scores
        # below zero rather than at it. Not clamped, because a clamp would
        # silently turn "actively off-topic" into "merely unrelated" -- but a
        # negative mean on a scorecard reads like a bug, so it is written down
        # here and not left to be rediscovered.
        #
        # **`strictness=1` is required, not a tuning choice.** The default is 3,
        # and it does not mean three calls -- it asks the judge for three
        # CANDIDATES in one request, i.e. `candidate_count=3`. Gemma on the
        # Gemini API rejects that outright:
        #
        #     400 INVALID_ARGUMENT: Multiple candidates is not enabled for
        #     this model
        #
        # Measured 2026-08-15: this failed 7 of 8 scored questions on the first
        # real run, and the failure is per-metric, so the run still "completed"
        # with a full scorecard -- three metrics populated and answer_relevance
        # almost entirely null. A metric that silently declines to measure is
        # far worse than one that crashes the run, because the scorecard still
        # renders and still points at a weakest metric.
        #
        # The cost of 1 is a noisier score: the mean is over one generated
        # question rather than three. Raise it only against a judge model that
        # supports multiple candidates -- Gemini Flash does.
        "answer_relevance": ResponseRelevancy(
            llm=judge, embeddings=get_judge_embeddings(), strictness=1
        ),
    }
    if turn.reference:
        metrics["context_precision"] = LLMContextPrecisionWithReference(llm=judge)
        metrics["context_recall"] = LLMContextRecall(llm=judge)
    return metrics


def _clean(value: Any) -> float | None:
    """A JSON-safe float, or None.

    **NaN is converted here, on purpose, rather than left for the caller.**
    Ragas returns `float('nan')` for a metric it could not compute, and NaN is
    not JSON-serialisable: left alone it survives the ORM write into a JSONB
    column and only explodes at the API boundary, in a serialiser, with a
    traceback that names neither the metric nor the question. Converting at the
    point where the cause is still visible turns a 500 into a null cell.
    """
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def behaviour_ok(turn: EvalTurn) -> bool:
    """Did the agent do what `expected_behaviour` asked?

    Not derivable from the four floats, which is why `eval_results.behaviour_ok`
    is its own column: a correct refusal is a success case Ragas has nothing to
    grade, so without this the record of a passed refusal question is four NULLs
    -- indistinguishable from a row that crashed.
    """
    if turn.expected_behaviour == "refuse":
        return turn.refused
    return not turn.refused


async def score_samples(
    samples: list[EvalTurn],
    *,
    judge_model: str | None = None,
    max_concurrency: int | None = None,
    timeout_s: float = METRIC_TIMEOUT_S,
) -> list[dict[str, Any]]:
    """Judge each turn. Returns one dict per turn, in the order given.

    Each dict carries the four metric keys (float or None), plus
    `behaviour_ok`, `expected_behaviour`, `golden_question_id`, `scored` and
    `error` -- the shape `metrics_guide.summarise` consumes and
    `eval_results` stores.

    **Never raises for a bad row.** A metric that fails records its reason in
    `error` and leaves that metric None; the surrounding run carries on. That is
    the difference between `eval_results.error` (this question went wrong) and
    `eval_runs.error` (the run ended without a summary), and collapsing them
    would let one judge timeout void a scorecard.

    **Refusal rows are not scored at all**, and skipping them is a saving as
    well as a correctness rule: they are the rows whose metrics would be
    meaningless (see `metrics_guide.summarise`), and not asking for them saves
    four judged calls per refusal question on a rate-limited free tier.
    """
    model = judge_model or settings.ragas_judge_model
    limit = max_concurrency or settings.ragas_max_concurrency
    judge = get_judge(model)

    # Bounds the judge calls in flight across this whole call, so scoring two
    # questions at once cannot double the burst that `ragas_max_concurrency` was
    # set to prevent. Created here rather than at module scope: a semaphore
    # binds to the event loop it was first awaited on, and a module-level one
    # would be shared across loops in tests and reloads.
    gate = asyncio.Semaphore(max(1, limit))

    async def score_one(turn: EvalTurn) -> dict[str, Any]:
        row: dict[str, Any] = {
            "golden_question_id": turn.golden_question_id,
            "expected_behaviour": turn.expected_behaviour,
            "behaviour_ok": behaviour_ok(turn),
            "scored": False,
            "error": None,
            **{key: None for key in METRIC_KEYS},
        }

        if turn.expected_behaviour == "refuse":
            # Pass/fail on `behaviour_ok` only. See the module docstring for
            # metrics_guide and PRD 4.4: refusing is the correct outcome here,
            # and grading it on faithfulness would punish the agent for it.
            return row

        if not turn.contexts:
            # Every one of the four metrics reads the contexts. With none, they
            # would all return 0.0 and the scorecard would read as a grounding
            # catastrophe rather than as an empty retrieval. Say so instead.
            row["error"] = "No retrieved contexts to score against."
            return row

        sample = SingleTurnSample(
            user_input=turn.question,
            retrieved_contexts=list(turn.contexts),
            response=turn.answer,
            reference=turn.reference,
        )

        failures: list[str] = []

        async def run_metric(name: str, metric: Any) -> None:
            async with gate:
                try:
                    value = await metric.single_turn_ascore(sample, timeout=timeout_s)
                except asyncio.TimeoutError:
                    failures.append(f"{name}: timed out after {timeout_s:.0f}s")
                    return
                except Exception as exc:  # noqa: BLE001 - deliberately broad
                    # Quota, transport, a verdict the parser could not repair --
                    # every failure mode of a judged metric has the same correct
                    # response, which is to record it and leave the other three
                    # metrics alone. Enumerating them would only mean the
                    # unenumerated one takes down the row.
                    failures.append(f"{name}: {exc.__class__.__name__}: {exc}")
                    return
            row[name] = _clean(value)

        # `return_exceptions=True` as a belt to the try/except braces above: an
        # exception escaping here would cancel the sibling tasks, so three
        # perfectly good metrics would be lost to the fourth one failing.
        await asyncio.gather(
            *(run_metric(name, metric) for name, metric in _metrics_for(turn, judge).items()),
            return_exceptions=True,
        )

        row["scored"] = any(row[key] is not None for key in METRIC_KEYS)
        if failures:
            row["error"] = "; ".join(failures)
            log.warning(
                "Eval scoring: %s metric(s) failed for question %s -- %s",
                len(failures),
                turn.golden_question_id,
                row["error"],
            )
        return row

    # Sequential across turns; concurrent within one. The caller
    # (`app/eval/jobs.py`) answers questions one at a time anyway, because the
    # answers share a database session and an AsyncSession is not safe to use
    # from two tasks at once -- so parallelising here would buy nothing and
    # would make the burst against the Gemini quota harder to reason about.
    return [await score_one(turn) for turn in samples]
