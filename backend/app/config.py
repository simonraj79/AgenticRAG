"""Application settings, loaded from environment.

Reads the repo-root .env in local development. On Render, the same names are
set as service environment variables and no .env file exists.
"""

from __future__ import annotations

import ssl
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# The only two roads `app/rag/retriever.get_embeddings` implements. Module level
# rather than a class attribute because pydantic v2 raises
# `PydanticUserError: A non-annotated attribute was detected` on a bare constant
# in a model body, and annotating it would make it a settable field.
EMBEDDING_ROUTES = ("openrouter", "google")

# The only two roads `app/storage.py` implements, and the same shape as
# EMBEDDING_ROUTES above for the same reason -- a route that falls through to a
# rollback on a typo is the failure this subsystem cannot report.
STORAGE_ROUTES = ("r2", "postgres")

# The four values `storage_route == "r2"` cannot work without. Named here so the
# validator and `scripts/create_r2_bucket.py` check one list rather than two.
R2_REQUIRED_FIELDS = (
    "r2_account_id",
    "r2_access_key_id",
    "r2_secret_access_key",
    "r2_bucket",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Database ---
    database_url: str = ""

    # --- Google OAuth ---
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    oauth_redirect_uri: str = "http://localhost:8000/api/auth/google/callback"
    session_secret_key: str = "dev-only-change-me"

    # Two settings that exist only to gate POST /api/auth/dev-login, the
    # authentication bypass a browser test needs because a real Google sign-in
    # ends at a human-only password prompt. Full reasoning in app/auth/routes.py.
    #
    # `environment` is informational everywhere else; only the exact string
    # "development" unlocks the bypass, so an unset or misspelled value fails
    # closed. Set ENVIRONMENT=production on Render.
    environment: str = "development"

    # Off unless a local .env turns it on deliberately. This repository is
    # public and deploys straight to production, so the flag defaults to the
    # safe value and never inherits one. It is the weakest of the three gates on
    # its own -- the loopback check is what holds if this one is set by mistake.
    dev_auth_enabled: bool = False

    # --- Frontend ---
    frontend_url: str = "http://localhost:5173"

    # --- Uploads and ingest ---
    # `app/api/documents.py` argued against exactly this setting, and it was
    # right: "a limit that can be raised by an environment variable will be
    # raised by an environment variable, and the reason it is low is a property
    # of how ingest works, not of where the service is deployed." The property it
    # meant is that ingest ran INLINE -- the request held a worker, a database
    # connection and the whole file in memory while it split, embedded and
    # upserted. Under that design a large file did not fail cleanly, it timed
    # out, and a mysterious timeout is the failure a size cap exists to prevent.
    # The 10 MB constant was not sizing the workload (the whole workshop corpus
    # is ~1.4 MB); it was protecting the request.
    #
    # So the cap and the blocking had to move together, and neither alone.
    # Raising the limit while ingest still ran in the request would have made the
    # failure worse rather than merely larger. `ingest_in_background` is the half
    # that moved ingest onto a background job with its own session
    # (`app/rag/jobs.py`), and only once that landed did this number become a
    # property of the deployment -- memory, embedding spend, how long a user will
    # watch a status badge -- rather than the thing holding the request timeout
    # up. Set `ingest_in_background=false` and this number is dangerous again;
    # they are one decision with two names, not two independent knobs.
    max_upload_mb: int = 50
    ingest_in_background: bool = True

    # --- Models / vector store (used from the RAG layer) ---
    # The EMBEDDING key, and only while `embedding_route == "google"`. That route
    # is now the ROLLBACK rather than the default -- see `embedding_route` below --
    # so this key is optional in the shipped configuration.
    #
    # **It is kept documented, kept in `.env.example` and kept in
    # `create_render_services.BACKEND_SECRET_KEYS` anyway.** Deleting a variable
    # from the template while it is still set on the deployed service is exactly
    # the drift CLAUDE.md records under "Render's env vars DRIFT from `.env`", and
    # the rollback needs it present the day it is needed rather than the day
    # someone notices it is gone.
    #
    # The clause that has NOT changed and is the reason `embedding_model` keeps
    # its value: the Pinecone index was written in `gemini-embedding-2`'s space,
    # and querying it through any other EMBEDDER returns confident nonsense rather
    # than an error. Another gateway to the SAME model is not another embedder --
    # measured, see `embedding_route`.
    gemini_api_key: str = ""
    pinecone_api_key: str = ""
    pinecone_index_name: str = "agentic-rag-ntu"
    cohere_api_key: str = ""

    # --- OpenRouter (every chat model) ---
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    # Only providers that support every parameter in the request are eligible to
    # serve it. OFF is the OpenRouter default and it is the dangerous one: an
    # unsupported parameter is silently DROPPED, so a `function_calling` request
    # routed to a provider without tool support comes back as prose rather than
    # as an error. Measured from the live endpoint list on 2026-08-15:
    # `google/gemma-4-31b-it` has 18 endpoints and at least two of them (a
    # DeepInfra tier and a Together tier) list no `tools`/`tool_choice`.
    #
    # The escape hatch exists because the failure this causes -- "no allowed
    # providers" when nothing satisfies the whole parameter set -- must be
    # answerable without a code edit.
    openrouter_require_parameters: bool = True

    # Bounds one HTTP call. Distinct from `METRIC_TIMEOUT_S`, which bounds a
    # whole judged metric: without this, one hung socket consumes that entire
    # budget and is then reported as a metric timeout, which CLAUDE.md already
    # records as ambiguous between a hang and a rate limit.
    openrouter_timeout_s: float = 120.0

    # Attribution headers. Optional, no user data -- these identify the app to
    # OpenRouter's leaderboards, nothing more.
    openrouter_app_url: str = "https://github.com/simonraj79/ClaudeRAGAgent"
    openrouter_app_title: str = "Groundwork"

    # ----------------------------------------------------------------------
    # Metering -- see `new features/14-admin-observability/PLAN.md` section 3.1
    # ----------------------------------------------------------------------
    # The master switch. OFF must mean "byte-identical to the code that shipped
    # before metering existed", not "similar to it": with this false,
    # `build_chat_model` returns a plain `ChatOpenAI` rather than the subclass.
    # That is what makes the regression claim checkable instead of careful --
    # `metering_check.py` case 7 asserts the TYPE, `llm_check.py` case 31 asserts
    # the request body.
    metering_enabled: bool = True

    # Re-raise a metering fault instead of swallowing it.
    #
    # **False in production and true in harnesses, and the asymmetry is the whole
    # design.** A user asked a question and the model answered it; if the
    # ACCOUNTING then fails, the accounting is what should fail -- a 500 on a
    # working answer is a worse outcome than a missing row. But a swallow with no
    # strict mode anywhere is how a meter records nothing for a month and reports
    # success, which is the exact failure class CLAUDE.md has now caught six
    # times. Strict mode is where that bug goes red.
    metering_strict: bool = False

    # Dollars per Cohere search unit, for the ONE cost centre that reports units
    # and not cost. Zero means DO NOT ESTIMATE, which is the default and the
    # honest position: units are recorded either way, and a rerank call then
    # shows up under `calls` but not under `priced_calls`.
    #
    # It is off by default because a hardcoded price is a number nobody
    # re-checks, and this repository already has that failure on this exact
    # provider -- a Cohere key silently downgraded to a trial tier, discovered
    # only under load, having looked identical the whole time. Set it from
    # Cohere's current published rerank price if a dollar figure is wanted; the
    # result lands in `api_usage.estimated_cost`, never in `cost_usd`.
    cohere_search_unit_usd: float = 0.0

    # Comma-separated emails promoted to `users.role = 'admin'`.
    #
    # **Read at PROMOTION time only -- never on a request path.** CLAUDE.md's
    # rule is "key on `sub`, never `email`", because Google reassigns emails
    # within a Workspace domain. Authorisation still reads `users.role` off a row
    # found by `google_sub` (`app/auth/deps.py:69`, unchanged); this setting only
    # decides whose role gets set, once, by a migration a human reviews.
    #
    # It must match EVERY row with that email, not one. `admin@example.com` is
    # two user rows -- `dev|admin@example.com` from the dev-login shim and the
    # real Google `sub` -- and promoting only the second one would leave the
    # admin console unreachable from `dev-login`, i.e. untestable by anything
    # that is not a human at a Google consent screen. That is the precise hole
    # `dev-login` exists to fill.
    admin_emails: str = ""

    @property
    def admin_email_list(self) -> list[str]:
        """Lower-cased, de-blanked. The only reader is the promotion path."""
        return [e.strip().lower() for e in self.admin_emails.split(",") if e.strip()]

    # Generation AND the agent loop AND handouts all read this (via
    # `agent.generation_model or settings.generation_model`). Moved off
    # `google/gemma-4-31b-it` on 2026-08-16, and the reason is one measurement
    # rather than a preference.
    #
    # `new features/loop.md` T1 is built on Gemma refusing to INITIATE a search:
    # 0 tool calls under `tool_choice="auto"`, 0 under a bare prompt, 0 under
    # "You MUST call search_corpus", 0 under `tool_choice="any"`. The same probe
    # against this model, same one-chunk context, same two-part question, same
    # refusal-first persona prompt:
    #
    #   tool_choice="auto", full grounding prompt   ->  5/5 self-initiated
    #   tool_choice="any"                           ->  honoured (Gemma ignored it)
    #   with_structured_output(function_calling)    ->  3/3 parsed
    #   astream with search_corpus bound            ->  148 chunks, first at 0.55s
    #
    # Generation latency p50 6.05s against Gemma's measured 13.2s, on a route
    # where CLAUDE.md puts generation at 89 percent of the turn.
    #
    # **T1 is now MODEL-DEPENDENT, not repealed.** `agents.generation_model`
    # overrides this per agent, so an operator can point one back at Gemma and the
    # gap trigger becomes load-bearing again. That is why none of the trigger
    # machinery was deleted -- see `app/rag/agent_loop.py`.
    generation_model: str = "deepseek/deepseek-v4-flash-0731"

    # Thinking OFF for generation, and this default is load-bearing in BOTH
    # directions -- read the second half before changing it.
    #
    # Off, because it is expensive and buys nothing here. This model reports
    # `reasoning.default_enabled = true, default_effort = "high"`, and measured
    # 2026-08-16 that consumed 60-79 percent of billed output tokens (out
    # [118, 296, 371], reasoning [70, 198, 293]) at the completion rate. Grounded
    # generation is not a problem that rewards a chain of thought: the material is
    # already in the prompt, and the task is to render and cite it. This is the
    # same argument `ragas_judge_reasoning_effort` makes for the judge.
    #
    # **The half that will bite someone later.** Turning thinking off does NOT cost
    # tool use -- but only because `TOOL_GUIDANCE`'s final paragraph is still
    # there. Measured 2026-08-16, 6 trials per cell, "did it search unprompted":
    #
    #                        guidance paragraph      no guidance paragraph
    #   reasoning on              6/6                      6/6
    #   reasoning off             6/6                      2/6
    #
    # The paragraph and the thinking are REDUNDANT WITH EACH OTHER, and either one
    # alone holds the behaviour. So the Gemma-era paragraph is not dead weight to
    # be tidied away now that the model is better -- it is what makes this setting
    # affordable. Delete both and tool use silently drops to a third, with nothing
    # raising: `new features/loop.md` T2 exactly.
    #
    # Off also measurably reduced redundant work: 2.00 -> 1.50 search calls per
    # step, and p50 3.27s -> 1.07s on the bound call.
    #
    # **It is one setting because the measurement refused to justify two.** The
    # expectation going in was that handouts would want thinking ON -- writing
    # matplotlib and python-pptx source is the task class reasoning is supposed to
    # help, and it runs in a background job where latency is not user-facing. The
    # head-to-head says otherwise. 6 chart recipes per arm, scoring the T2 outcome
    # (`chart.png` present on the FIRST attempt, never "nothing raised"):
    #
    #   reasoning on   ->  5/6 first try,  p50 30.4s
    #   reasoning off  ->  6/6 first try,  p50  8.1s
    #
    # Off was better on both axes and 3.7x faster, so `handouts/jobs._model_for`
    # reads this same setting rather than a second one. A separate knob would have
    # to be justified by a measurement that says the two paths want different
    # values, and no such measurement exists.
    generation_reasoning: bool = False

    # Stage 2's rewrite decision must come back as a typed object. The PRD hedged
    # this to Gemini Flash because structured output is undocumented for Gemma,
    # and said to collapse to one model if it turned out to work. Measured
    # 2026-08-15, 5 trials per configuration:
    #
    #   gemma  raw google-genai response_schema  T=1.0   4/5   ~2.2s
    #   gemma  raw google-genai response_schema  T=0.2   5/5   ~2.2s
    #   gemma  langchain function_calling        T=1.0   5/5   ~3.5s
    #   gemma  langchain json_mode               T=1.0   5/5   ~2.6s
    #   flash  langchain function_calling        T=1.0   5/5   ~2.3s
    #
    # Gemma emits schema-correct JSON but occasionally wraps it in a markdown
    # fence at its recommended temperature. `response.parsed` is strict and
    # returns None on that; LangChain's parser strips the fence, which is the
    # entire difference between the failing row and the passing ones -- not a
    # different API capability. `function_calling` sidesteps the text channel
    # altogether, so it cannot be broken by a stray fence, and it is the path
    # Gemma's model card actually documents.
    #
    # So: collapsed to one model. Flash remains one env var away.
    #
    # Those trials ran against the Gemini API directly. The conclusion survives
    # the move to OpenRouter but the reasoning gains a second half: OpenRouter
    # drops parameters the routed provider does not support, and
    # `function_calling` IS two such parameters (`tools`, `tool_choice`). See
    # `openrouter_require_parameters` above -- structured output is now correct
    # because of that flag, not only because of this method name.
    #
    # **DELIBERATELY NOT MOVED WITH `generation_model` ON 2026-08-16.** This is
    # now the only Gemma call site left, which is exactly the sort of asymmetry a
    # later reader tidies away, so the measurement that produced it is recorded
    # here. Head-to-head on what this call site actually needs -- does the typed
    # object come back, and does it rewrite the right questions -- 9 trials each,
    # two coreference cases plus one already-standalone question that must be
    # left alone:
    #
    #   google/gemma-4-31b-it            parsed 9/9   correct 9/9   p50 1.02s
    #   deepseek, reasoning off          parsed 9/9   correct 9/9   p50 1.58s
    #   deepseek, reasoning default      parsed 9/9   correct 8/9   p50 0.85s
    #
    # Gemma is not worse at this, and it is faster than the arm that matched it.
    # CLAUDE.md measures a follow-up already paying 3.8s for contextualisation
    # before the question is embedded, so 0.5s of added latency on every
    # conversational turn is a real cost bought for nothing but tidiness.
    #
    # A regression here would also be INVISIBLE: `contextualize_question` swallows
    # every exception and degrades to Stage 1, so a rewriter that stopped working
    # would surface only as quietly worse retrieval. Do not move this without
    # re-running that table.
    decision_model: str = "google/gemma-4-31b-it"

    # --- The question rewriter ---
    #
    # It runs on EVERY turn as of 2026-08-16, first turns included, so it is a
    # plain code path rather than a trigger (`new features/loop.md` section 6,
    # item 1: "if it must run every time, call it yourself"). It used to return
    # immediately when there was no history, on the reasoning that a first turn
    # has no references to resolve -- true, and too narrow, because a typo and a
    # piece of shorthand are not references, and either one is enough to put a
    # first turn's vector somewhere the corpus is not.
    #
    # **What the widened step does NOT do is expand acronyms.** That bullet was
    # built here, fabricated ("Ka-band (Kurtz-band)" in 2 of 5 trials; "LS&T" ->
    # "Link System and Telemetry", which moved retrieval to the wrong file), was
    # narrowed to a conditional version that measured 5/5 both directions, and
    # was then removed anyway -- the value was the first-turn case, where nothing
    # has spelled anything out, so a recoverability gate fires almost never.
    # `CONTEXTUALIZE_SYSTEM_PROMPT` in `app/rag/pipeline.py` now forbids it
    # outright, and carries the full measurement.
    #
    # **This flag exists for the reason loop.md S4 asks for one.** "With the
    # feature off the output is byte-identical to before" has to be expressible,
    # and a rewriter regression is otherwise invisible: `contextualize_question`
    # swallows every exception by design and degrades to Stage 1, so a rewriter
    # that stopped working shows up only as quietly worse retrieval.
    #
    # False restores the pre-2026-08-16 behaviour exactly: first turns embedded
    # verbatim, only follow-ups contextualised.
    rewrite_every_turn: bool = True

    # **Eval turns opt OUT, and that is a measurement decision rather than a
    # default.** `app/eval/jobs.py` creates one fresh Conversation per golden
    # question precisely so the question reaches the embedder verbatim -- that is
    # what a golden set means. A rewriter that runs on every turn silently removes
    # that guarantee: every EVAL.md baseline stops being comparable, and the
    # golden-question editor keeps displaying a question that is not the one that
    # was asked.
    #
    # Turn this on only together with a full re-run of the baselines AND an editor
    # that shows the rewritten string.
    eval_rewrite_questions: bool = False

    # The model that DRAFTS the golden set (§3.6.1). Split out of
    # `decision_model`, which it used to share by accident rather than by
    # argument: the rewriter is a mechanical pronoun dereference on every
    # conversational turn, where cost and latency dominate, while this runs once
    # per agent and produces the measuring instrument every later scorecard is
    # read through. Those want different models and now have them.
    #
    # Flash, on a head-to-head over the real corpus (10 questions each, same
    # prompt, same sample). Two differences decided it:
    #
    # 1. REFUSAL QUESTIONS. PRD 3.6.1 calls these "the single largest
    #    determinant of whether the set measures anything" -- they must be
    #    plausible neighbours of the corpus, not absurdities. Gemma asked which
    #    launch vehicle was used; Flash asked what propellant the thrusters use
    #    and what the six modules are individually named. Both of Flash's hinge
    #    on a detail the corpus RAISES and then does not complete, which is a
    #    tighter probe of grounding than a fact it never mentions.
    #
    # 2. REFERENCE ANSWERS. `LLMContextRecall` decomposes the reference into
    #    claims and checks each against the retrieved contexts, so a reference
    #    of "Nineteen" (8 characters, Gemma's) gives it almost nothing to work
    #    with. Flash's equivalent -- "The permanent crew complement is eleven,
    #    which expands to nineteen during handover weeks" -- decomposes into
    #    several attributable claims. Two of the four metrics read this field.
    #
    # Flash was also 5.8 s against 11.8 s, which is not why.
    #
    # **MOVED OFF FLASH 2026-08-16, to buy the independence the paragraph below
    # used to record as an accepted cost.** Flash is `ragas_judge_model`, so while
    # it also drafted the set the judge was grading against reference answers it
    # had written itself. That touches context precision and context recall --
    # faithfulness and answer relevance never read `reference` -- and it was
    # tolerable only while both context metrics sat pinned at 1.0 on a
    # single-chunk corpus, i.e. only while they were not measuring anything.
    # `minimax/minimax-m3` is a third vendor, so `self_judged` is now false
    # structurally rather than nominally.
    #
    # **The measured cost, recorded because the next reader will otherwise undo
    # the mitigation.** Pooled over 8 runs, same corpus, same prompt:
    #
    #                              MiniMax M3      Flash
    #   reference answer, median      24 chars     95 chars
    #   references under 20 chars          43%           0%
    #   shortest seen             '14 knots', '31 hours'
    #   p50                            ~9.8 s       ~4.9 s
    #   refusal probes            hit every planted gap (as good as Flash)
    #
    # That short-reference number is the SAME defect point 2 above rejected Gemma
    # for ("Nineteen", 8 characters). It is mitigated in `app/eval/generate.py` by
    # a `reference_answer` field description that demands a full sentence naming
    # the subject -- **that mitigation is load-bearing, not cosmetic, and the
    # pooled median must be re-measured rather than assumed if either the model or
    # that description changes.** `scripts/goldenset_check.py` is the harness.
    #
    # Reasoning is default-ON here with `mandatory=false`, measured at 93-99.98%
    # of completion tokens, which is why `generate.py` passes `reasoning=False`.
    # Without it the call hits `finish_reason=length` against MAX_OUTPUT_TOKENS.
    #
    # Set back to `google/gemini-3.7-flash` to trade independence for a longer
    # reference answer and half the latency.
    golden_set_model: str = "minimax/minimax-m3"

    structured_output_method: str = "function_calling"

    # **The vector space and the road to it are two different facts, and only one
    # of them changed on 2026-08-16.**
    #
    # `embedding_model` names the SPACE. It is stamped onto `agents.embedding_model`
    # and `ingestion_runs.embedding_model` and is the only durable statement of
    # what a stored vector MEANS, so it must not move while the space does not.
    # Changing it to the OpenRouter slug was the obvious edit and it is the wrong
    # one: it would leave old rows saying one string and new rows another for
    # BYTE-IDENTICAL vectors, disarm the mismatch detector `app/rag/ingest.py`
    # exists to provide, make `app/eval/metrics_guide.py`'s "check the agent's
    # embedding_model matches the index" advice manufacture a false alarm, and
    # show two spellings side by side in the settings sheet.
    #
    # Verified 2026-08-16, and both call shapes were checked on purpose:
    # langchain-google-genai 4.3.4 injects a `task_type` the constructor never
    # sets -- `RETRIEVAL_DOCUMENT` on `embed_documents`, `RETRIEVAL_QUERY` on
    # `embed_query` -- while the OpenRouter route sends neither. The index is
    # WRITTEN with one and QUERIED with the other, so proving only one shape
    # would have left the other unverified.
    #
    #   embed_documents (Google) vs embed_query (Google)   cosine 1.000000000
    #   embed_documents (Google) vs OpenRouter             cosine 1.000000000
    #   embed_query     (Google) vs OpenRouter             cosine 1.000000000
    #   cross-string control                               cosine 0.616
    #
    # One space, one string. `task_type` is inert for this model, which is what
    # CLAUDE.md always said of the constructor and is now also known of the wire.
    embedding_model: str = "models/gemini-embedding-2"
    embedding_dimension: int = 768

    # `embedding_route` names the ROAD. "openrouter" | "google".
    #
    # An explicit setting rather than something inferred from which key happens to
    # be set, because a wrong route is the one failure this subsystem cannot
    # report: it returns confident nonsense rather than an error, and nothing else
    # in the system records which provider wrote a vector.
    #
    # "google" is the rollback and it still works -- `langchain-google-genai` stays
    # installed for exactly that reason and for no other.
    embedding_route: str = "openrouter"

    @field_validator("embedding_route")
    @classmethod
    def _validate_embedding_route(cls, value: str) -> str:
        """Reject a route this code does not implement, at load.

        **The paragraph above claims this setting is explicit "because a wrong
        route is the one failure this subsystem cannot report". Free text did not
        deliver that claim.** `retriever.get_embeddings` branches on
        `== "openrouter"` and falls through to Google on ELSE, so every
        misspelling -- `EMBEDDING_ROUTE=openroute`, `Openrouter`, an empty string
        from a cleared Render variable -- selects the rollback silently. The
        setting that exists to make the road explicit was picking a different
        road on a typo.

        And the failure that typo produces is not even a route error. With
        `gemini_api_key` optional as of 2026-08-16 it surfaces as a Google auth
        failure at RETRIEVAL time, one layer down from the cause; and where that
        key does happen to be set, it does not surface at all -- the wrong
        gateway answers, and CLAUDE.md's "confident nonsense rather than an
        error" is the outcome. Both valid values are named in the message
        because the reader of this exception is someone who has just mistyped
        one of them.

        This fires at construction, which is where an environment variable is
        read. It is deliberately not `validate_assignment`: `scripts/embed_check.py`
        assigns this attribute to probe both branches in one process, and turning
        every assignment into a validated write would be a wider behaviour change
        than the defect being fixed here.
        """
        if value not in EMBEDDING_ROUTES:
            raise ValueError(
                "embedding_route must be exactly "
                f"{' or '.join(repr(route) for route in EMBEDDING_ROUTES)}, "
                f"got {value!r}. Anything else silently selects the Google "
                "rollback in rag/retriever.get_embeddings."
            )
        return value

    # The same model, spelled the way each gateway resolves it. `models/` is the
    # Gemini-API-native prefix; OpenRouter wants `author/model` like every chat
    # slug. Two spellings of one space, which is why this is a separate setting
    # rather than an edit to `embedding_model`.
    openrouter_embedding_model: str = "google/gemini-embedding-2"

    # Texts per embeddings HTTP request. **A hard provider ceiling, not a tuning
    # knob**: OpenRouter's Google backend answers 101 inputs with
    # `400 ... at most 100 requests can be in one batch`.
    #
    # This is the change whose absence passes every small test. `langchain-google-genai`
    # re-batched at 100 internally (`_DEFAULT_BATCH_SIZE`); `OpenAIEmbeddings`
    # defaults to 1000 and does not, and `app/rag/ingest.py` hands a whole
    # document's chunks to `store.add_texts`, which forwards up to 1000 of them in
    # one call. Any document over 100 chunks ingests fine today and 400s without
    # this. A probe with 25 strings succeeds in one request and proves nothing.
    #
    # NOT `agents.chunk_size` (characters per text) and NOT langchain-pinecone's
    # `embedding_chunk_size`. Three different things named chunk_size sit on one
    # call stack; this is the innermost.
    embedding_batch_size: int = 100

    # PRD section 2 says "Cohere rerank-v3". That is a family, not a model id --
    # the API rejects it. The live ids are rerank-english-v3.0,
    # rerank-multilingual-v3.0, rerank-v3.5, rerank-v4.0-fast and
    # rerank-v4.0-pro; v3.5 is the multilingual v3 successor and the closest
    # real id to what the workshop specifies.
    rerank_model: str = "rerank-v3.5"

    # Gemma 4's model card gives one "standardized sampling configuration across
    # all use cases": temperature 1.0, top_p 0.95, top_k 64. That reads oddly for
    # grounded RAG, where the instinct is temperature 0 -- but the instinct is
    # borrowed from models calibrated differently, and Gemma degenerates into
    # repetition when sampling is squeezed far below what it was tuned for. Start
    # at the documented values and change them only against a measurement.
    generation_temperature: float = 1.0
    generation_top_p: float = 0.95
    generation_top_k: int = 64
    generation_max_tokens: int = 2048

    # --- Evaluation (Stage 3) ---
    # The model that GRADES the answers. PRD 2.1 specifies Gemini Flash Lite for
    # this and the reason is worth stating plainly: with the default below, the
    # judge is the same model as `generation_model`, so the run is
    # self-assessment. `faithfulness` asks "is this answer supported by these
    # contexts?" of the very model that wrote the answer, and LLM-as-judge setups
    # are known to score their own output generously (self-preference bias). The
    # number is not meaningless -- it is measured against the retrieved contexts,
    # not against taste -- but it is not independent either.
    #
    # **It is no longer `gemma-4-31b-it`, and the split was earned by
    # measurement rather than by principle.** Two runs of the ten-question
    # golden set were self-judged, and both named faithfulness as the weakest
    # metric -- the one number the scorecard exists to point at. Replaying
    # identical turns showed that pointer was substantially judge error:
    #
    #   answer copied VERBATIM from its context   gemma 0.000   flash 0.667
    #   495-char answer                           gemma 165.0s  flash 10.3s
    #   1551-char answer                          gemma 196.3s  flash 28.9s
    #
    # A word-for-word copy of the context scoring zero is the judge failing, not
    # the generator drifting; and at 165-196 s per call against a 180 s ceiling,
    # WHICH rows survived was luck rather than a property of the answers. Answer
    # relevance was stable across both judges (0.813 vs 0.811), so this was
    # specific to faithfulness and to latency, not general judge quality.
    #
    # What stays true regardless of which model is set: `eval_runs` records
    # `judge_model` and `generation_model` separately per run (never read back
    # from `agents.generation_model`, which can change after a run), and the
    # scorecard says so when the two are equal.
    ragas_judge_model: str = "google/gemini-3.7-flash"

    # Gemini 3.7 Flash has `reasoning.mandatory = true`: thinking cannot be
    # turned off, only turned down (high / medium / low, default medium), and
    # reasoning tokens bill at the completion rate.
    #
    # `low` because of what the judge actually does. Faithfulness decomposes an
    # answer into atomic statements and asks whether each follows from the
    # contexts; context precision and recall are the same shape. That is natural
    # language inference against text already in the prompt -- not a problem that
    # rewards a long chain of thought -- and the entire reason for leaving Gemma
    # was that judged calls were too slow, so buying latency back with reasoning
    # depth would undo the move. Raise it if a judged score looks wrong in a way
    # that is not explained by the metric's own denominator.
    ragas_judge_reasoning_effort: str = "low"

    # How many judge calls may be in flight at once, within one question.
    #
    # This existed because of the Gemini free tier. The constraint moved with the
    # provider -- OpenRouter bills credits rather than refusing, and the 429 now
    # comes from whichever upstream provider was routed to -- but the value did
    # not, because the shape of the burst is unchanged and the cost of being
    # wrong is asymmetric in the same direction. Each scored question costs
    # FOUR judged metrics, and three of them are themselves multi-call
    # (faithfulness generates statements then verdicts them; answer relevance
    # generates `strictness` questions from the answer). A ten-question run is
    # therefore tens of judge requests on top of ten generations, and a 429 is
    # the most likely way it fails -- as a burst, not as a total. Two is slow and
    # boring on purpose: the run is a background job, so the cost of being
    # conservative is a progress bar that moves less quickly, while the cost of
    # being greedy is a scorecard full of nulls that looks like a broken judge.
    ragas_max_concurrency: int = 2

    # --- Agentic tools (Stage 4) ---
    #
    # Two gates, not one. This setting is an OPERATOR kill switch that turns the
    # loop off everywhere without touching data; `agents.tools_enabled` is the
    # per-agent choice. Both must be true. That split matters because the column
    # is backfilled to false for every agent that existed before tools shipped,
    # so an agent whose scorecard is already recorded in EVAL.md keeps behaving
    # exactly as it was measured -- and a single global flag could not express
    # "on for new agents, off for measured ones".
    agent_tools_enabled: bool = True

    # Tool round trips per turn before the loop is closed and an answer is
    # forced. Three, not eight, and the number comes from the latency budget
    # rather than from taste: a search costs roughly 1.6 s with reranking on
    # (embed 365 ms + Pinecone 394 ms + Cohere ~830 ms, measured), against a
    # persona turn of about 6.3 s since the move to OpenRouter. Three steps is
    # enough room for a genuinely multi-part question and not enough to explore.
    # The loop always returns an answer when it runs out; `stopped_reason`
    # records that it did.
    agent_max_tool_steps: int = 3

    # --- Code interpreter sandbox ---
    #
    # See new features/02-code-interpreter.md section 5 for what these do and do
    # NOT protect against. The short version: this is a hardened subprocess, not
    # a container, and the strongest control is not a limit here at all -- it is
    # that the child is spawned with an environment stripped of every secret.
    sandbox_timeout_s: float = 30.0

    # stdout + stderr returned to the MODEL, each capped separately. A runaway
    # print loop is a real failure mode, and an untruncated one is worse than the
    # bug: it lands in the next request's context and can cost more than the turn.
    sandbox_max_output_chars: int = 8_000

    # Per-file and per-run artifact ceilings. Exceeding either fails the whole
    # run and returns ZERO artifacts rather than a partial set -- half a deck
    # would be reported to the model as a success.
    sandbox_max_artifact_bytes: int = 5_242_880  # 5 MB
    sandbox_max_total_bytes: int = 15_728_640  # 15 MB

    # RLIMIT_AS for the child, POSIX only. Render is Linux so production gets it;
    # Windows development does not, and the sandbox logs that rather than
    # pretending otherwise. 768 MB is matplotlib plus a figure with headroom.
    sandbox_memory_mb: int = 768

    # --- Object storage (Cloudflare R2) ---
    #
    # Which store holds BYTES. Structured data is always Postgres; this setting
    # only ever decides where a handout file or an original upload lives.
    #
    # "postgres" is the rollback and it still works, because this change set
    # deliberately did NOT drop `handouts.content` -- the blue/green rule the
    # repo already applies to a Pinecone index migration, where the old index
    # stays queryable until a human has confirmed the new one. A route that
    # cannot be reversed is a deletion with extra steps.
    #
    # R2 rather than S3 for one measured reason: egress is $0. Every handout
    # download is egress, and for a workshop where attendees download decks that
    # dominates the storage cost either way. Reached over the S3 PROTOCOL -- see
    # `app/storage.py`; nothing here calls AWS and no AWS_* variable is read.
    storage_route: str = "r2"

    r2_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""

    # Namespaced because the account is SHARED -- it already holds
    # `mindfulspeak-uploads` from an unrelated project, and the API token is
    # account-wide. A generic name like "media" would be a collision waiting for
    # the second project that wants one.
    r2_bucket: str = "groundwork-media"

    # Blank means "derive it from the account id", which is the documented form
    # `https://<account>.r2.cloudflarestorage.com`. Explicit wins, so a
    # jurisdiction-specific endpoint (eu, fedramp) needs no code change.
    r2_endpoint: str = ""

    # Presigned URL lifetime. Five minutes, and both bounds are real: long
    # enough that a slow mobile connection still STARTS the download, short
    # enough that the URL sitting in browser history is dead before anyone reads
    # it back. This is the only control that survives the move -- the route used
    # to send `Cache-Control: private, no-store` and a presigned URL cannot
    # reproduce it, because the capability IS the URL. Measured 2026-08-17: an
    # expired URL returns 403, so expiry is enforced by R2 rather than advisory.
    r2_presign_ttl_s: int = 300

    @field_validator("storage_route")
    @classmethod
    def _validate_storage_route(cls, value: str) -> str:
        """Reject a route this code does not implement, at load.

        Same shape as `_validate_embedding_route` above, and adopted for the
        same reason rather than by analogy: `app/storage.py` branches on
        `== "r2"`, so every misspelling selects the Postgres rollback silently,
        and the tell would be bytes quietly continuing to accumulate in a column
        this change set exists to stop using. Nothing would error.
        """
        if value not in STORAGE_ROUTES:
            raise ValueError(
                "storage_route must be exactly "
                f"{' or '.join(repr(route) for route in STORAGE_ROUTES)}, "
                f"got {value!r}. Anything else silently selects the Postgres "
                "rollback in app/storage.py."
            )
        return value

    @field_validator("r2_bucket")
    @classmethod
    def _require_r2_config(cls, value: str, info) -> str:
        """Fail at LOAD when the route is R2 and a credential is missing.

        **This is the one required-secret gate in the file, and it is here
        because there was no pattern for one.** Every other secret is declared
        `str = ""` with no runtime check, which is right for them: an absent
        `OPENROUTER_API_KEY` fails the very next model call, loudly, naming
        itself. Storage does not behave that way. A blank credential under
        `storage_route="r2"` would let the app boot, let a handout job run, let
        the bytes be generated -- and fail at the PUT, inside a background job,
        surfacing as a handout stuck at `failed` with a message about
        credentials that nobody reading the panel can act on.

        `r2_bucket` carries the validator rather than one of the secrets because
        it is declared last of the four and pydantic validates in declaration
        order, so `info.data` holds the other three by the time this runs. That
        is an ordering dependency, and it is why the fields are not sorted
        alphabetically. Moving `r2_bucket` above them silently disarms this.
        """
        route = info.data.get("storage_route")
        if route != "r2":
            return value

        merged = dict(info.data)
        merged["r2_bucket"] = value
        missing = [name for name in R2_REQUIRED_FIELDS if not merged.get(name)]
        if missing:
            raise ValueError(
                f"storage_route='r2' needs {', '.join(m.upper() for m in missing)} "
                "set. Set them, or set STORAGE_ROUTE=postgres to keep bytes in "
                "the database."
            )
        return value

    @property
    def r2_endpoint_url(self) -> str:
        """The S3 endpoint, explicit or derived. Empty when unconfigured."""
        if self.r2_endpoint:
            return self.r2_endpoint.rstrip("/")
        if not self.r2_account_id:
            return ""
        return f"https://{self.r2_account_id}.r2.cloudflarestorage.com"

    # --- Handouts ---
    #
    # A quota, and as of the object-storage change set it is a POLICY rather
    # than a storage bound -- which is a change of meaning, not of value. It
    # used to read "bytes live in Postgres, so this is a storage bound"; with
    # bytes in R2 that reason is dead, and a comment giving a dead reason is
    # worse than none. The number stays at 200 because the argument that
    # survives is the one about eviction: reaching the cap REFUSES the new
    # handout and nothing is ever deleted, because a panel that silently drops
    # the deck you downloaded last week to make room for a chart is worse than
    # one that says no.
    handout_max_per_agent: int = 200

    # The output cap for a code-writing generation call, deliberately above
    # `generation_max_tokens` (2,048). That default sizes an ANSWER -- a few
    # paragraphs with citations. A python-pptx script for an eight-slide deck is
    # a long, repetitive program.
    #
    # A SETTING RATHER THAN THE MODULE CONSTANT IT WAS (`handouts/jobs.py`'s
    # `CODE_MAX_TOKENS`), because the truncation retry has to be able to raise
    # it: a program cut off at 4,096 tokens fails as a syntax error, is retried
    # at the SAME 4,096, produces the same length and fails identically. That is
    # `PLAN.md` R7, and it FIRED LIVE, twice, while the layer-2 scenarios were
    # being written -- with the slide floor forced to 40 the model inflated the
    # deck until it ran out of room and came back "Python syntax error on line
    # 322: unterminated string literal". A retry at the same budget is a retry
    # that cannot succeed.
    #
    # WHY 4,096 IS THE FIRST ATTEMPT'S VALUE. Measured on real decks: a correct
    # six-slide program is ~2,000-3,000 characters, i.e. roughly 600-900 tokens
    # of Python. 4,096 is already four to six times the honest program, so a run
    # that exhausts it is INFLATING rather than merely long -- which is what the
    # retry multiplier in `handouts/jobs.py` is sized against, and why it is a
    # multiplier of 2 rather than 4. The reasoning for that number lives beside
    # it, with the note that the provider ceiling is NOT the binding constraint
    # (checked 2026-08-17: `max_completion_tokens` is 393,216 on
    # `deepseek/deepseek-v4-flash-0731` and 262,144 on `google/gemma-4-31b-it`).
    handout_code_max_tokens: int = 4_096

    # Open the produced file before calling the handout `ready`.
    #
    # Measured 2026-08-17 against this venv's python-pptx 1.0.2: a
    # `Presentation()` with ZERO slides saves as 27,387 bytes starting `PK`, and
    # `b"PK\x03\x04 this is not a real pptx"` is 28 bytes that also start `PK`.
    # Both cleared every assertion in the repository -- `agentic_check.py`'s
    # `status == "ready" and byte_size > 0`, and `sandbox_check.py` case 3's
    # `PK` + `>= 10_000` bytes -- and both became downloadable handouts. Nothing
    # between the model's `prs.save()` and the user's Downloads folder had ever
    # opened the bytes.
    #
    # This flag is NOT a product option. It exists so that the regression
    # assertion can be executed: with it off, `_problem` must return values
    # identical to today's for the same `(SandboxResult, SandboxArtifact)` pair,
    # which is `scripts/deck_check.py` case 25 and PLAN.md 3.6 R-a. Handout
    # bytes cannot be compared between runs -- generation is at temperature 1.0
    # and a .pptx is a zip carrying timestamps -- so the pure function is the
    # only place "byte-identical with the feature off" can actually be asserted.
    handout_validate_artifacts: bool = True

    # The slide floor below which a deck is sent back to the model.
    #
    # THREE, NOT FIVE, and the difference is the whole risk of this feature.
    # `DECK_PROMPT` asks for five to eight slides but carries an honest-shrink
    # rule telling the model to use only what the material supports, so a thin
    # corpus SHOULD produce a short deck. A floor of 5 would fail exactly that
    # correct behaviour -- the `refusal_pass = 0/2` defect, where a measurement
    # punished the thing the prompt exists to produce and the scorecard then
    # advised deleting it. A floor of 3 still fails the empty and single-slide
    # decks that motivated the feature.
    #
    # The asymmetry that sets it (`loop.md` T3): this number feeds a RETRY, not
    # a refusal. A false positive costs one extra generation call and one extra
    # subprocess; a false negative ships a deck that does not open. Strictness
    # follows that, so it leans permissive.
    #
    # MEASURED 2026-08-17, `scripts/deck_rate_check.py`, n=16 decks over two
    # retrieval budgets. Slide counts were [5,5,6,6,6,6,7,7,8,9] at the hostile
    # fixture budget (rerank_top_n=2) and [6,6,7,7,7,7] at the shipped one
    # (rerank_top_n=10). **The minimum honest deck is 5**, so a floor of 3 sits
    # two slides below the worst real case at the most starved configuration in
    # the repo, and cannot fire on an honest shrink.
    #
    # Deliberately NOT raised to 4 or 5 despite the headroom. This is a floor on
    # "somebody made a deck", not a score of "the deck is good", and the audit
    # that produced it found the opposite mistake far more expensive: a metric
    # tuned to punish the behaviour a prompt exists to produce, which then
    # recommends deleting the pedagogy (`refusal_pass = 0/2`, PRD open item 16).
    handout_deck_min_slides: int = 3

    # The longest a single paragraph on a slide may be before the deck is sent
    # back. A character count, and deliberately a crude one.
    #
    # python-pptx's only text-fitting API is `fit_text`, and `pptx/text/fonts.py`
    # returns font directories for darwin and win32 and otherwise raises
    # `OSError("unsupported operating system")`. It works on this Windows box and
    # would kill every deck on Render, with a message that says nothing about
    # fonts -- green locally, dead in production. Character count is not a good
    # proxy; it is the only honest one available. `deck_check.py` case 14 asserts
    # the symbol appears nowhere under `app/handouts`.
    #
    # MEASURED 2026-08-17, `scripts/deck_rate_check.py`, and the measurement
    # moved this number -- it was 240 by instinct and 240 was nearly wrong.
    #
    #   rerank_top_n=2  (n=296 bullets)   p50  69   p99 109   max 114
    #   rerank_top_n=10 (n=181 bullets)   p50  72   p95 127   max 235
    #
    # Widening the deck's retrieval budget (feature 03) made bullets LONGER, and
    # one real bullet landed within 2% of the old threshold. At any scale that
    # fires on a legitimate deck -- twice, and then fails the handout outright.
    # An interaction between two features of this change set that neither
    # feature's own plan anticipated, and it was only visible because the two
    # budgets were measured separately.
    #
    # 400 clears the observed maximum by 1.7x while still catching the shape this
    # exists for: a wall of prose pasted onto one slide. THE TRUE OVERFLOW POINT
    # IS UNMEASURED and cannot be measured here -- knowing it means rendering the
    # deck, which needs LibreOffice or a font stack the sandbox deliberately does
    # not have (`fit_text` is the trap above; PLAN.md section 7 keeps rendering
    # out of scope). So this is a bound on the model's observed behaviour, not on
    # the geometry, and it should be re-read whenever the retrieval budget moves
    # again -- as it just did.
    handout_deck_max_bullet_chars: int = 400

    @property
    def async_database_url(self) -> str:
        """SQLAlchemy async URL.

        Render hands out `postgresql://...`, which SQLAlchemy maps to psycopg2.
        We use asyncpg, so the driver has to be named explicitly. asyncpg also
        rejects libpq-style query params such as `sslmode`, so they are stripped
        here -- asyncpg negotiates TLS on its own.
        """
        url = self.database_url
        if not url:
            return ""

        parts = urlsplit(url)
        scheme = parts.scheme
        if scheme in ("postgres", "postgresql"):
            scheme = "postgresql+asyncpg"

        drop = {"sslmode", "channel_binding", "gssencmode", "target_session_attrs"}
        query = "&".join(
            kv
            for kv in parts.query.split("&")
            if kv and kv.split("=", 1)[0] not in drop
        )
        return urlunsplit((scheme, parts.netloc, parts.path, query, parts.fragment))

    @property
    def db_connect_args(self) -> dict:
        """asyncpg connect args.

        Two separate Render quirks meet here.

        1. Render Postgres refuses non-TLS connections with
           `InvalidAuthorizationSpecificationError: SSL/TLS required`. asyncpg
           does not read libpq's `sslmode`, so TLS must be requested here --
           stripping sslmode from the URL (see async_database_url) is only half
           the fix and on its own produces exactly that error.

        2. The INTERNAL endpoint presents a self-signed certificate, so a
           verifying context fails with
           `SSLCertVerificationError: self-signed certificate`. The EXTERNAL
           endpoint has a normal public certificate and verifies fine.

        Internal hostnames have no dots (`dpg-xxx-a`); external ones are FQDNs
        (`dpg-xxx-a.singapore-postgres.render.com`). We verify when we can and
        fall back to encrypted-but-unverified on the private network, where the
        traffic never leaves Render.
        """
        if not self.database_url:
            return {}

        host = urlsplit(self.database_url).hostname or ""
        ctx = ssl.create_default_context()
        if "." not in host:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return {"ssl": ctx}

    @property
    def cors_origins(self) -> list[str]:
        origins = {self.frontend_url.rstrip("/")}
        origins.add("http://localhost:5173")
        return sorted(o for o in origins if o)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
