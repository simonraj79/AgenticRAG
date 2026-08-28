"""Layer 1 harness for `app/metering/`. No network, no DB, no model -- instant.

WHY THIS FILE EXISTS, and why it was written BEFORE the feature.

Metering is the shape that goes green while recording nothing. `0.0` is a number,
a sum over zero rows is `0.0`, and "the callback did not raise" is true of a
callback that extracted nothing at all. `new features/loop.md` T2 names that
failure -- trigger on the ABSENCE of the outcome you wanted, never on the
presence of an error -- and CLAUDE.md records a green suite being wrong SEVEN
times. So every case below asserts a VALUE (a cost, a token count, a provider
name, a list length), never that nothing threw.

The five things it pins, and what each would cost if it were loose:

  1. **The streamed-usage recovery is a LIST.** langchain merges `generation_info`
     across chunks with `merge_dicts`, which concatenates lists and RAISES on two
     unequal scalars. Measured on this route: `finish_reason` merges to
     `"stopstop"` and `model_name` to a doubled slug. So a bare
     `generation_info["cost"] = 9.1e-07` works on every model tested today and
     kills a LIVE TURN the first time a provider emits two usage frames. Case 2
     is that provider, simulated.

  2. **Cost survives on both paths.** `ainvoke` puts it in `llm_output`;
     `astream` puts it nowhere langchain keeps -- `_create_usage_metadata`
     (langchain_openai/chat_models/base.py:4175) drops `cost`, `provider` and
     the `gen-` id on the floor. Cases 3 and 4 are the two shapes.

  3. **Attribution is per-CALL-TIME, not per-construction-time.** `get_router`,
     `get_critic` and `get_contextualizer` are module-level singletons, so a
     model object outlives the user who first built it. Case 5 runs two
     concurrent asyncio tasks and asserts they do not cross-attribute.

  4. **A metering fault must never kill a turn.** Case 6 asserts the swallow --
     and asserts that `metering_strict` still re-raises, so a swallowed bug is
     caught somewhere rather than nowhere.

  5. **Nested collections must not double-count.** Case 8, added after the bug
     it describes was found by wiring `app/eval/jobs.py`: an inner collection
     that shared the outer list meant `run_turn` and the eval job would each
     persist the same generation calls, reporting roughly DOUBLE the real spend
     with no error and two plausible totals.

  6. **A turn that RAISES must not discard the spend it already paid for.**
     `run_turn` HAD no `try` and no `finally`, so any raise between `_run_turn`'s
     `queries` flush and its single `await db.commit()` threw the whole buffer
     away -- and `LoggingSink` returns as soon as a bucket accepts the record, so
     there was no log line either. The spend is ABSENT, not unattributed. Cases
     13-17b pin the seam that fixes it and, above all, the guard that stops it
     firing on the SUCCESS path too, which would double-count every normal turn.
     Anchors are function names rather than line numbers throughout this block:
     the fix moved every one of the numbers the first draft cited.

Case 6 is the one to read first if this file ever goes red in production: it is
the difference between a broken meter and a broken product.

**What this file structurally CANNOT check.** It builds synthetic LLMResults, so
it asserts what the meter does with a shape, never that OpenRouter still sends
that shape. `scripts/route_check.py --live` is the other half and asserts
`cost > 0` off a real call. Run both when either changes.

    backend/.venv/Scripts/python.exe scripts/metering_check.py
    backend/.venv/Scripts/python.exe scripts/metering_check.py --db    # real SQL

`--db` exists because a layer-1 harness cannot prove a query RUNS, only that it
was written. Cases 13-17b read source and drive a fake session; D1-D4 execute
against the real schema, where a NOT NULL, a foreign key or a coverage aggregate
can disagree with all three, and D4 proves the run cleaned up after itself.

**`--db` WRITES, and it writes to production.** D1 commits one `api_usage` row
through the real seam, against a user and agent this harness creates and then
deletes -- never `select(User).limit(1)`, which is a real person. See the fixture
paragraph in the `--db` block for what that cost when it was one.

ASCII in print(), exit 1 on any failure -- `new features/06-test-plan.md`.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from langchain_core.messages import AIMessageChunk  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, LLMResult  # noqa: E402

from app.metering.chat import MeteredChatOpenAI  # noqa: E402
from app.metering.context import meter_as  # noqa: E402
from app.metering.meter import USAGE_KEY, UsageMeter  # noqa: E402

failures: list[str] = []
unmeasured: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "[ok]  " if condition else "[FAIL]"
    print(f"{status} {name}" + (f" -- {detail}" if detail else ""))
    if not condition:
        failures.append(name)


def not_measured(name: str, detail: str) -> None:
    """A third state, and it is loud on purpose.

    `storage_check.py:18-22` is the authority: a row that could not be measured
    must never print green, and must not fail the suite either. Everything above
    case 12 is pure offline arithmetic and can always be measured; `--db` cannot
    -- no database reachable, or no suitable row to measure against -- and a
    quiet skip there is a guard that is simply absent on a fresh laptop.
    """
    print(f"[warn] {name} -- {detail}  <- NOT MEASURED")
    unmeasured.append(f"{name}: {detail}")


# The usage frame OpenRouter actually sends, copied verbatim off the wire
# 2026-08-20 (`scripts/` probe, deepseek-v4-flash through this repo's own
# extra_body). Do not "tidy" it -- the point is that it is not invented.
WIRE_USAGE = {
    "prompt_tokens": 9,
    "completion_tokens": 2,
    "total_tokens": 11,
    "cost": 9.1e-07,
    "is_byok": False,
    "prompt_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
    "completion_tokens_details": {"reasoning_tokens": 0},
}


def usage_chunk(**over) -> dict:
    """A usage-only SSE frame: `choices` empty, `usage` present."""
    return {
        "id": over.pop("id", "gen-1787192108-jhrAdQzrbN5XK5tOcX54"),
        "provider": over.pop("provider", "Relace"),
        "model": "deepseek/deepseek-v4-flash-0731",
        "choices": [],
        "usage": {**WIRE_USAGE, **over},
    }


def convert(model: MeteredChatOpenAI, chunk: dict) -> ChatGenerationChunk | None:
    return model._convert_chunk_to_generation_chunk(chunk, AIMessageChunk, None)


MODEL = MeteredChatOpenAI(
    model="deepseek/deepseek-v4-flash-0731", api_key="sk-not-used", base_url="http://x"
)

print("=" * 74)
print("metering -- what the meter does with a shape, decided offline")
print("=" * 74)

# ---------------------------------------------------------------------------
print("\n-- 1-2. the streamed recovery, and the merge that must not raise --")
# ---------------------------------------------------------------------------
gen = convert(MODEL, usage_chunk())
info = dict((gen.generation_info if gen else None) or {})
recovered = info.get(USAGE_KEY)

check(
    "1. a usage-only frame yields openrouter_usage as a list of exactly 1",
    isinstance(recovered, list)
    and len(recovered) == 1
    and recovered[0]["usage"]["cost"] == 9.1e-07
    and recovered[0]["provider"] == "Relace"
    and str(recovered[0]["id"]).startswith("gen-"),
    f"recovered={recovered!r}",
)

# R2 in one line. Two frames with DIFFERENT costs is what `merge_dicts` raises
# on when the value is a bare float. Merging is what streaming does to every
# chunk, so this is not a hypothetical shape -- it is the shape.
a = convert(MODEL, usage_chunk(cost=9.1e-07))
b = convert(MODEL, usage_chunk(cost=4.0e-06))
try:
    merged = a + b
    merged_info = dict(merged.generation_info or {})
    merged_list = merged_info.get(USAGE_KEY) or []
    ok2 = len(merged_list) == 2 and {r["usage"]["cost"] for r in merged_list} == {
        9.1e-07,
        4.0e-06,
    }
    detail2 = f"len={len(merged_list)}"
except Exception as exc:  # noqa: BLE001 -- the failure this case exists for
    ok2 = False
    detail2 = f"RAISED {type(exc).__name__}: {exc}"

check(
    "2. two usage frames merge to a list of 2 and do NOT raise (R2)",
    ok2,
    detail2,
)

# ---------------------------------------------------------------------------
print("\n-- 3-4. cost survives on both paths --")
# ---------------------------------------------------------------------------
meter = UsageMeter(sink=None)

ainvoke_result = LLMResult(
    generations=[
        [
            ChatGeneration(
                message=AIMessageChunk(
                    content="OK",
                    usage_metadata={
                        "input_tokens": 282,
                        "output_tokens": 2,
                        "total_tokens": 284,
                    },
                ),
                generation_info={"finish_reason": "stop"},
            )
        ]
    ],
    llm_output={
        "token_usage": {**WIRE_USAGE, "prompt_tokens": 282, "completion_tokens": 2,
                        "cost": 2.002e-05},
        "model_name": "deepseek/deepseek-v4-flash-0731",
        "id": "gen-1787192071-KIaFFGUATkNOWgRJaEQh",
    },
)
rec3 = meter.extract(ainvoke_result)
check(
    "3. ainvoke shape -> cost and tokens off llm_output",
    rec3 is not None
    and rec3.cost_usd == 2.002e-05
    and rec3.prompt_tokens == 282
    and rec3.completion_tokens == 2
    and rec3.generation_id == "gen-1787192071-KIaFFGUATkNOWgRJaEQh",
    f"rec={rec3}",
)

# The streamed shape: llm_output is EMPTY (measured -- `llm_output_keys: []`),
# so everything must come out of generation_info.
astream_result = LLMResult(
    generations=[
        [
            ChatGeneration(
                message=AIMessageChunk(
                    content="OK",
                    usage_metadata={
                        "input_tokens": 9,
                        "output_tokens": 2,
                        "total_tokens": 11,
                    },
                ),
                generation_info=info,  # what case 1 recovered
            )
        ]
    ],
    llm_output={},
)
rec4 = meter.extract(astream_result)
check(
    "4. astream shape -> cost AND served_provider off generation_info",
    rec4 is not None
    and rec4.cost_usd == 9.1e-07
    and rec4.served_provider == "Relace"
    and rec4.prompt_tokens == 9,
    f"rec={rec4}",
)

# A turn with no usage anywhere must be distinguishable from a turn that cost
# zero. `cost_usd is None` means NOT MEASURED; 0.0 would mean free. EVAL.md's
# scored_count trap, arriving in a new module.
bare = LLMResult(
    generations=[[ChatGeneration(message=AIMessageChunk(content="OK"))]], llm_output={}
)
rec_bare = meter.extract(bare)
check(
    "4b. no usage anywhere -> cost is None (not measured), never 0.0",
    rec_bare is None or rec_bare.cost_usd is None,
    f"rec={rec_bare}",
)

# The model slug on a STREAMED record. Found the expensive way: the first build
# refused to read `generation_info["model_name"]` because streaming doubles it,
# and left the field null on every generation turn -- i.e. on exactly the rows
# the admin console groups by model. Every layer-1 case above still passed,
# because none of them asked what the model was.
#
# The slug now comes from the meter's constructor, where `build_chat_model`
# knows it. This case is the tripwire for both halves: present, and NOT doubled.
meter_named = UsageMeter(sink=None, model="deepseek/deepseek-v4-flash-0731")
rec_stream_model = meter_named.extract(astream_result)
check(
    "4c. a STREAMED record carries the model slug, exactly once",
    rec_stream_model is not None
    and rec_stream_model.model == "deepseek/deepseek-v4-flash-0731",
    f"model={rec_stream_model.model!r}",
)

# The artefact this guards against, asserted directly rather than described. If
# anyone re-adds a fallback to generation_info["model_name"], this goes red.
doubled = LLMResult(
    generations=[
        [
            ChatGeneration(
                message=AIMessageChunk(content="OK"),
                generation_info={
                    "model_name": "deepseek/deepseek-v4-flash-0731"
                    "deepseek/deepseek-v4-flash-0731"
                },
            )
        ]
    ],
    llm_output={},
)
rec_doubled = UsageMeter(sink=None).extract(doubled)
check(
    "4d. and never picks up the doubled slug streaming produces",
    rec_doubled is not None and (rec_doubled.model or "").count("/") <= 1,
    f"model={rec_doubled.model!r}",
)

# ---------------------------------------------------------------------------
print("\n-- 5. attribution is bound at CALL time, not construction time --")
# ---------------------------------------------------------------------------
import uuid as _uuid  # noqa: E402

ALICE, BOB = _uuid.uuid4(), _uuid.uuid4()


async def _concurrent() -> list:
    seen: list = []

    async def turn(user_id, kind, delay):
        with meter_as(user_id=user_id, call_kind=kind):
            # Yield control mid-scope. If the scope were process-global rather
            # than task-local, the other task's value would be read back here.
            await asyncio.sleep(delay)
            rec = meter.extract(ainvoke_result)
            seen.append((rec.user_id, rec.call_kind))

    await asyncio.gather(
        turn(ALICE, "generation", 0.02), turn(BOB, "judge", 0.01)
    )
    return seen

pairs = asyncio.run(_concurrent())
check(
    "5. two concurrent scopes do not cross-attribute (PLAN 3.3)",
    sorted(pairs, key=lambda p: str(p[0]))
    == sorted([(ALICE, "generation"), (BOB, "judge")], key=lambda p: str(p[0])),
    f"pairs={pairs}",
)

# ---------------------------------------------------------------------------
print("\n-- 6. a metering fault must not kill a turn (R1) --")
# ---------------------------------------------------------------------------


class Exploding:
    def record(self, rec):  # noqa: ANN001
        raise RuntimeError("sink is down")


async def _swallow(strict: bool) -> str:
    m = UsageMeter(sink=Exploding(), strict=strict)
    try:
        await m.on_llm_end(ainvoke_result)
        return "returned"
    except Exception as exc:  # noqa: BLE001
        return f"raised:{type(exc).__name__}"


check(
    "6. sink failure is swallowed when metering_strict is false",
    asyncio.run(_swallow(False)) == "returned",
)
check(
    "6b. and RE-RAISES when metering_strict is true, so it is caught somewhere",
    asyncio.run(_swallow(True)).startswith("raised:"),
)

# ---------------------------------------------------------------------------
print("\n-- 7. the off switch is a real off switch --")
# ---------------------------------------------------------------------------
# PLAN 3.1: with metering disabled, `build_chat_model` must return a plain
# ChatOpenAI -- the object that shipped, not a subclass that happens to behave.
# `llm_check.py` case 31 owns the request BODY half of this; this is the type.
from app.config import settings  # noqa: E402
from app.rag.llm import build_chat_model  # noqa: E402
from langchain_openai import ChatOpenAI  # noqa: E402

_was = settings.metering_enabled
try:
    settings.metering_enabled = False
    off = build_chat_model("deepseek/deepseek-v4-flash-0731")
    settings.metering_enabled = True
    on = build_chat_model("deepseek/deepseek-v4-flash-0731")
finally:
    settings.metering_enabled = _was

check(
    "7. metering off -> plain ChatOpenAI; on -> MeteredChatOpenAI",
    type(off) is ChatOpenAI and isinstance(on, MeteredChatOpenAI),
    f"off={type(off).__name__} on={type(on).__name__}",
)

# ---------------------------------------------------------------------------
print("\n-- 8. nested collections do not double-count --")
# ---------------------------------------------------------------------------
# THE BUG THIS EXISTS FOR, caught while wiring the eval job and not by any case
# above it. `collect_usage` originally yielded the OUTER list to an inner block,
# so that a helper wrapping itself defensively could not lose a turn's spend.
# That reasoning is backwards once two owners both persist:
#
#     eval job         opens a collection, runs 10 golden questions
#       run_turn       opens its own, persists everything it finds (with query_id)
#     eval job         persists the same list AGAIN (without query_id)
#
# Every generation call written twice, the console reporting roughly double the
# real spend, no error anywhere -- and both totals plausible, which is what would
# have made it survive. The rule is now: whoever opens a collection persists what
# it collects, and nobody else does.
from app.metering.context import collect_usage  # noqa: E402

_outer_seen = []
with collect_usage() as outer:
    outer.append("outer-1")
    with collect_usage() as inner:
        inner.append("inner-1")
        _inner_len = len(inner)
    outer.append("outer-2")
    _outer_seen = list(outer)

check(
    "8. an inner collection gets its own bucket",
    _inner_len == 1,
    f"inner held {_inner_len} record(s); 2 means it inherited the outer list",
)
check(
    "8b. and its records do NOT leak outward (no double-count)",
    _outer_seen == ["outer-1", "outer-2"],
    f"outer={_outer_seen}",
)

# The sink must land in the INNERMOST collection, or the ownership rule is
# decorative: a record produced inside run_turn has to be persisted by run_turn.
_sink = __import__("app.metering.meter", fromlist=["LoggingSink"]).LoggingSink()
with collect_usage() as outer2:
    with collect_usage() as inner2:
        _sink.record(rec3)
    _inner_got = len(inner2)
    _outer_got = len(outer2)

check(
    "8c. the sink writes to the innermost active collection",
    _inner_got == 1 and _outer_got == 0,
    f"inner={_inner_got} outer={_outer_got}",
)

# ---------------------------------------------------------------------------
print("\n-- 9. embeddings: the wrapper, and the seam it depends on --")
# ---------------------------------------------------------------------------
# **Embedding cost is REPORTED, not estimated**, and an earlier version of this
# feature shipped saying the opposite. Measured on the wire:
#
#     usage    Usage(prompt_tokens=7, total_tokens=7, cost=1.4e-06, ...)
#     provider "Google AI Studio"
#     id       "gen-emb-1787199865-..."
#
# All three survive into the openai SDK's response object -- `cost` on
# `usage.model_extra`, `provider`/`id` on the response's. Nothing drops them the
# way `_create_usage_metadata` drops chat cost; what was missing was a reader.
#
# The seam is `self.client`, NOT `embed_documents`. `check_embedding_ctx_length`
# selects between a plain batching loop and `_get_len_safe_embeddings`, and BOTH
# call `client.create(...)` -- so wrapping the client survives a flip of that
# flag, which CLAUDE.md documents as one 400 away from being reconsidered.
from app.metering.embeddings import meter_embeddings  # noqa: E402
from app.metering.meter import LoggingSink  # noqa: E402
from app.metering.rerank import meter_rerank  # noqa: E402
from app.metering.store import to_row  # noqa: E402


class _FakeUsage:
    def model_dump(self):
        return {"prompt_tokens": 7, "total_tokens": 7, "cost": 1.4e-06}


class _FakeEmbResponse:
    usage = _FakeUsage()
    provider = "Google AI Studio"
    id = "gen-emb-test"


class _FakeResource:
    def __init__(self): self.seen = 0
    def create(self, **kw):
        self.seen += 1
        return _FakeEmbResponse()


class _FakeEmbeddings:
    def __init__(self): self.client = _FakeResource(); self.async_client = None


captured: list = []


class _Collect:
    def record(self, rec): captured.append(rec)


_fake = meter_embeddings(
    _FakeEmbeddings(), model="google/gemini-embedding-2", sink=_Collect()
)
_fake.client.create(input=["a", "b", "c"])
_emb_rec = captured[-1] if captured else None

check(
    "9. an embeddings response yields a record with REPORTED cost",
    _emb_rec is not None
    and _emb_rec.cost_usd == 1.4e-06
    and _emb_rec.cost_is_estimated is False
    and _emb_rec.prompt_tokens == 7,
    f"rec={_emb_rec}",
)
check(
    "9b. kind is 'embedding' and the served provider is recovered",
    _emb_rec is not None
    and _emb_rec.call_kind == "embedding"
    and _emb_rec.served_provider == "Google AI Studio",
    f"kind={_emb_rec.call_kind} prov={_emb_rec.served_provider}",
)
check(
    "9c. units counts the inputs, so a batch is not one unit",
    _emb_rec is not None and _emb_rec.detail.get("units") == 3,
    f"units={_emb_rec.detail.get('units')}",
)

# The kind must OVERRIDE the ambient scope. An embed_query inside a turn is
# scoped `generation` by `run_turn`; filing it there would hide the retrieval
# half of the bill inside the answer half.
captured.clear()
with meter_as(user_id=ALICE, call_kind="generation"):
    _fake.client.create(input=["x"])
check(
    "9d. the kind overrides the ambient scope but attribution is inherited",
    captured
    and captured[-1].call_kind == "embedding"
    and captured[-1].user_id == ALICE,
    f"kind={captured[-1].call_kind if captured else None}",
)

# THE SEAM ITSELF. `OpenAIEmbeddings` sets model_config extra='forbid', so this
# is verified rather than assumed -- if a langchain-openai release froze the
# model or renamed the field, metering would silently stop and every case above
# would still pass against its fake.
from langchain_openai import OpenAIEmbeddings  # noqa: E402

_real = OpenAIEmbeddings(model="x", api_key="sk-not-used", base_url="http://x")
try:
    _before = type(_real.client).__name__
    meter_embeddings(_real, model="x", sink=_Collect())
    _seam_ok = type(_real.client).__name__ == "_MeteredResource"
    _seam_detail = f"{_before} -> {type(_real.client).__name__}"
except Exception as exc:  # noqa: BLE001 -- the regression this case exists for
    _seam_ok = False
    _seam_detail = f"RAISED {type(exc).__name__}: {exc}"

check(
    "9e. the real OpenAIEmbeddings still accepts a wrapped client (extra=forbid)",
    _seam_ok,
    _seam_detail,
)

# ---------------------------------------------------------------------------
print("\n-- 10. rerank: units are measured, a dollar figure is not invented --")
# ---------------------------------------------------------------------------
# Cohere reports `meta.billed_units.search_units` and NO cost. So units are a
# measurement and any dollar figure is arithmetic over a published price -- which
# is a number nobody re-checks. This repo already has that failure on this exact
# provider: a Cohere key silently downgraded to a trial tier, discovered only
# under load, identical-looking the whole time.


class _FakeBilled:
    search_units = 1.0


class _FakeMeta:
    billed_units = _FakeBilled()


class _FakeRerankResponse:
    meta = _FakeMeta()


class _FakeRerankClient:
    def rerank(self, **kw):
        return _FakeRerankResponse()


class _FakeCompressor:
    def __init__(self): self.client = _FakeRerankClient(); self.async_client = None


captured.clear()
_rr = meter_rerank(
    _FakeCompressor(), model="rerank-v3.5", sink=_Collect(), unit_price_usd=0.0
)
_rr.client.rerank(query="q", documents=["a", "b", "c"])
_rr_rec = captured[-1]

check(
    "10. rerank records MEASURED search units",
    _rr_rec.call_kind == "rerank" and _rr_rec.detail.get("units") == 1,
    f"units={_rr_rec.detail.get('units')}",
)
check(
    "10b. and never claims a reported cost -- Cohere does not send one",
    _rr_rec.cost_usd is None,
    f"cost_usd={_rr_rec.cost_usd}",
)
check(
    "10c. with the price unset, NO estimate is invented",
    _rr_rec.cost_is_estimated is False
    and _rr_rec.detail.get("estimated_cost") is None,
)

captured.clear()
_rr2 = meter_rerank(
    _FakeCompressor(), model="rerank-v3.5", sink=_Collect(), unit_price_usd=0.002
)
_rr2.client.rerank(query="q", documents=["a"])
_rr2_rec = captured[-1]
_rr2_row = to_row(_rr2_rec)

check(
    "10d. with a price set, the estimate lands in estimated_cost",
    _rr2_row.estimated_cost == 0.002 and _rr2_row.cost_is_estimated is True,
    f"estimated_cost={_rr2_row.estimated_cost}",
)
check(
    "10e. and NEVER in cost_usd -- a guess must not sit in the reported column",
    _rr2_row.cost_usd is None,
    f"cost_usd={_rr2_row.cost_usd}",
)
check(
    "10f. rows route to the right provider/operation pair",
    (to_row(_emb_rec).provider, to_row(_emb_rec).operation) == ("openrouter", "embedding")
    and (_rr2_row.provider, _rr2_row.operation) == ("cohere", "rerank"),
)

# ---------------------------------------------------------------------------
print("\n-- 11-12. coverage: is every call site actually INSIDE a scope? --")
# ---------------------------------------------------------------------------
# R4 in `new features/14-admin-observability/PLAN.md` -- "rows written with
# user_id = NULL because context was not set" -- whose mitigation was written as
# "a case asserting every call site is covered by a meter_as", and was NOT built.
# The gap it names had already happened by the time anyone looked: the golden-set
# drafter reaches `build_chat_model` through a BackgroundTask that opens no
# scope, so its spend landed as `user_id = NULL, call_kind = "unknown"` while
# `"goldenset"` sat in CALL_KINDS with nothing setting it.
#
# WHY THE TEN CASES ABOVE COULD NOT SEE IT. Every one of them opens its own
# `meter_as` and then asserts attribution survived -- so the harness only ever
# tested call sites it wrote ITSELF, never the one the APPLICATION forgot. That
# is the seventh-green-suite failure one layer up, and it generalises past this
# repo: a harness cannot prove instrumentation is COMPLETE, only that the
# instrumentation it was handed works. These two cases read the application's own
# source instead of a shape this file invented.
import ast  # noqa: E402

from app.metering.context import CALL_KINDS  # noqa: E402

_APP = ROOT / "backend" / "app"
_TREES = {p: ast.parse(p.read_text(encoding="utf-8")) for p in sorted(_APP.rglob("*.py"))}


def _rel(path: Path) -> str:
    return path.relative_to(_APP).as_posix()


def _kw_str(call: ast.Call, name: str) -> str | None:
    """The value of `name=` on this call, but only when it is a plain string."""
    for kw in call.keywords:
        if kw.arg == name and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            return kw.value.value
    return None


# -- 11. every declared kind is reachable ----------------------------------
# Derived from the source, never a second list beside CALL_KINDS -- a hardcoded
# expectation here would be the "contract stated twice" that build.md forbids,
# and the copy that drifted is never the one you are reading.
#
# `unknown` is exempt and the exemption is the point: it is the dataclass default
# for a call made OUTSIDE any scope, so a call site passing it explicitly would
# be asserting it does not know who it is working for. It must stay unreachable.
_kinds_set: dict[str, list[str]] = {}
for _path, _tree in _TREES.items():
    for _node in ast.walk(_tree):
        if isinstance(_node, ast.Call):
            _k = _kw_str(_node, "call_kind")
            if _k:
                _kinds_set.setdefault(_k, []).append(f"{_rel(_path)}:{_node.lineno}")

_declared = set(CALL_KINDS) - {"unknown"}
_used = set(_kinds_set)

check(
    "11a. every kind in CALL_KINDS is set by some call site",
    _declared <= _used,
    f"declared but never set: {sorted(_declared - _used) or 'none'}",
)
check(
    "11b. no call site sets a kind CALL_KINDS does not declare",
    _used <= set(CALL_KINDS),
    f"undeclared: {sorted(_used - set(CALL_KINDS)) or 'none'}",
)

# -- 12. no entry point reaches a model call outside a scope ---------------
# The property, stated so it can be false: a function is UNMETERED when it can
# reach `build_chat_model` along some path that passes through no `meter_as`.
# Opening a scope covers everything below it, which is why `run_turn` clears its
# routes -- the wrap is one frame under the handler, not in it.
#
# Names are resolved bare, ignoring the module. That is deliberate and it is the
# conservative direction: two same-named functions merge into one node, so a call
# graph edge may be invented but never lost, and this case can therefore report a
# false ALARM but not a false all-clear.
_defs: dict[str, list[tuple[Path, ast.AST]]] = {}
for _path, _tree in _TREES.items():
    for _node in ast.walk(_tree):
        if isinstance(_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _defs.setdefault(_node.name, []).append((_path, _node))


def _callee_names(node: ast.AST) -> set[str]:
    out = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            fn = sub.func
            if isinstance(fn, ast.Name):
                out.add(fn.id)
            elif isinstance(fn, ast.Attribute):
                out.add(fn.attr)
    return out


_calls = {name: set().union(*(_callee_names(n) for _, n in ns)) for name, ns in _defs.items()}
_scoped = {
    name
    for name, ns in _defs.items()
    if any("meter_as" in _callee_names(n) for _, n in ns)
}

# Fixpoint. `build_chat_model` WAS the whole seed because it was the single
# chokepoint every chat call passes through -- the same property that let one
# callback meter eight call sites is what made one seed enough.
#
# **CHANGE SET 18 BROKE THAT PROPERTY, AND THE BREAK IS INVISIBLE HERE BY
# CONSTRUCTION.** The ADK runtime reaches `build_chat_model` from
# `OpenRouterAdkLlm._chat`, which is called by `generate_content_async`, which
# NOTHING IN THIS REPOSITORY CALLS -- ADK's `Runner` does. So the call graph this
# walk builds from our own source stops dead one edge short, `run_agent_loop_adk`
# is never marked as reaching the chokepoint, `_leaks` stays empty, and this case
# prints green over a runtime it never examined.
#
# That is the eighth green-suite failure repeating exactly: a harness cannot prove
# instrumentation is COMPLETE, only that the instrumentation it was handed works.
#
# The seed is therefore a SET, and `build_adk_model` is in it because it is the
# one function our source DOES call on the way into the ADK runtime. Case 12c
# below asserts the set is complete against the source rather than trusting this
# comment.
_SEED = {"build_chat_model", "build_adk_model"}
_unmetered = {n for n in _defs if _SEED & _calls[n]} - _scoped
while True:
    grown = {
        n
        for n in _defs
        if n not in _scoped and (_calls[n] & _unmetered)
    } | _unmetered
    if grown == _unmetered:
        break
    _unmetered = grown

_ROUTE_DECORATORS = {"get", "post", "put", "patch", "delete"}


def _is_entry(node: ast.AST) -> bool:
    """A place a unit of work BEGINS: an HTTP handler or a background job."""
    if node.name.endswith("_job"):
        return True
    for dec in getattr(node, "decorator_list", []):
        fn = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(fn, ast.Attribute) and fn.attr in _ROUTE_DECORATORS:
            return True
    return False


_leaks = sorted(
    f"{_rel(p)}:{n.lineno} {n.name}()"
    for name, ns in _defs.items()
    for p, n in ns
    if _is_entry(n) and name in _unmetered
)

check(
    "12. no entry point reaches a model builder outside a meter_as scope (R4)",
    not _leaks,
    f"unmetered: {_leaks}" if _leaks else f"{len(_scoped)} scoped functions cover the graph",
)

# --- 12b: the ADK builder is actually IN the graph this walk covers -----------
#
# Without this, `_SEED` could name a function that does not exist -- a typo, or a
# rename -- and case 12 would go green for the same reason it went green before
# the name was added: nothing reaches a seed that is not there.
check(
    "12b. build_adk_model is a real function this walk found",
    "build_adk_model" in _defs,
    f"defs={'build_adk_model' in _defs}",
)
check(
    "12b. the ADK loop reaches a seeded model builder",
    "build_adk_model" in _calls.get("run_agent_loop_adk", set()),
    f"callees={sorted(_calls.get('run_agent_loop_adk', set()) & _SEED)}",
)

# --- 12c: the seed set is DERIVED from source, never handed in ---------------
#
# The rule this closes is the one case 12 was written for and did not itself
# obey: coverage is a property of the application's call graph, so a case
# asserting it must READ the application's source rather than a list a human
# maintains. A third runtime added tomorrow with its own client construction
# would otherwise sit outside every seed with nothing to say so.
_CLIENT_CTORS = {"ChatOpenAI", "MeteredChatOpenAI", "OpenRouterAdkLlm"}
_constructors = {
    name
    for name, callees in _calls.items()
    if _CLIENT_CTORS & callees
}
# `build_chat_model` constructs ChatOpenAI/MeteredChatOpenAI; `build_adk_model`
# constructs OpenRouterAdkLlm. Anything ELSE that constructs a client directly is
# a chokepoint nobody seeded.
_unseeded = sorted(_constructors - _SEED - {"_chat", "_runnable"})
check(
    "12c. no function constructs a chat client outside the seeded builders",
    not _unseeded,
    f"unseeded constructors: {_unseeded}" if _unseeded else f"seed={sorted(_SEED)}",
)

# ---------------------------------------------------------------------------
print("\n-- 13-17b. a turn that RAISES must not discard the spend it paid for --")
# ---------------------------------------------------------------------------
# `new features/15-failure-paths/02-failed-turn-metering.md`.
#
# THE DEFECT, and why nothing above could see it. Every case from 1 to 12 asks
# what the meter does with a record it was handed. None of them asks whether the
# record ever reaches a row, and `run_turn` (`app/api/ask.py`) is where it did
# NOT: the wrapper opened `collect_usage()` and returned straight out of
# `_run_turn` with no `try`, no `except` and no `finally` anywhere in it. The
# buffer was drained once, by `_run_turn`'s `persist` call, which only `add()`s
# -- the commit that makes those rows real is further down the same function. So
# ANY raise between the `queries` flush and that commit threw away every record
# the turn had already paid OpenRouter for.
#
# Anchors here are FUNCTION NAMES, not line numbers, and that is a correction:
# this comment shipped citing `ask.py:727`, `:1395` and `:1410`, and the fix it
# describes moved all three within the hour. A citation the next edit invalidates
# is worse than none, because it reads as precise.
#
# And it is silent in the strongest sense available: `LoggingSink.record`
# (`meter.py:124-136`) returns the moment `emit_record` accepts the record into
# the turn's bucket, so a failed turn produces neither a row NOR a log line. The
# spend is not unattributed. It is absent. That is `loop.md` T2 in its purest
# form -- there is no error to trigger on, only an outcome that did not happen.
#
# Four sites in this repo already do the right thing (`rag/jobs.py:230`,
# `eval/jobs.py:299`, `handouts/jobs.py:947`, `api/eval.py:1086`), all four with
# the identical `finally` shape. So 13 is not asking for an invention; it is
# asking why one of five sites is different.

import uuid as _u13  # noqa: E402

from app.metering import store as metering_store  # noqa: E402
from app.metering.meter import UsageRecord as _UR  # noqa: E402

_COLLECT = "collect_usage"


def _name_of(func_node) -> str | None:  # noqa: ANN001
    if isinstance(func_node, ast.Name):
        return func_node.id
    if isinstance(func_node, ast.Attribute):
        return func_node.attr
    return None


def _parent_map(tree: ast.AST) -> dict:
    out = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            out[child] = node
    return out


def _own_nodes(fn: ast.AST):
    """Every node inside `fn` that is NOT inside a nested function.

    Crossing into a nested def would credit the outer function with an inner
    function's `finally`. This case asserts a path MUST exist, so any looseness
    here is a false ALL-CLEAR rather than a false alarm -- the direction that
    does damage. Case 12 resolves names bare across the whole app precisely
    because its polarity is the opposite one, and it says so.
    """
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        stack.extend(ast.iter_child_nodes(node))


def _calls_in(statements) -> list:  # noqa: ANN001
    out = []
    for stmt in statements:
        for sub in ast.walk(stmt):
            if isinstance(sub, ast.Call):
                out.append(sub)
    return out


# WHICH NAMES COUNT AS DRAINING A BUFFER -- derived from `app/metering/store.py`,
# never listed here. This used to read `{"persist", "persist_quietly"}`, and that
# hardcoded set is the "contract stated twice" build.md forbids: it went on
# accepting `persist_quietly` after the turn's call site stopped calling it, so a
# future `finally` could satisfy case 13 by reaching a function with zero callers.
# Deriving it means store.py deleting that function is a one-line change over
# there that this file follows for free -- and a new writer added there is
# accepted on the day it lands rather than on the day someone remembers.
#
# A writer is a function in store.py that reaches `db.add`, directly or through
# another such function. The polarity is safe: if the derivation ever came back
# EMPTY, `_persist_scope` would resolve nothing and case 13 would go RED rather
# than vacuously green. 13c prints the set and asserts it is not empty anyway.
_STORE_PATH = _APP / "metering" / "store.py"
_store_defs = {
    n.name: n
    for n in ast.walk(_TREES[_STORE_PATH])
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
}
_PERSIST_NAMES = {
    name
    for name, node in _store_defs.items()
    if any(_name_of(c.func) == "add" for c in _calls_in(node.body))
}
while True:
    _grown = _PERSIST_NAMES | {
        name for name, node in _store_defs.items() if _callee_names(node) & _PERSIST_NAMES
    }
    if _grown == _PERSIST_NAMES:
        break
    _PERSIST_NAMES = _grown


def _persist_scope(finalbody, module_defs) -> list | None:  # noqa: ANN001
    """The statements that actually reach a persist call, or None.

    Resolution is ONE hop and SAME MODULE only. That is the tightest rule that
    still admits a named helper -- which `run_turn` needs, because a seam a
    harness can execute without a database has to be a function with a name,
    not eight lines inlined into a `finally`.
    """
    if any(_name_of(c.func) in _PERSIST_NAMES for c in _calls_in(finalbody)):
        return list(finalbody)
    for call in _calls_in(finalbody):
        helper = module_defs.get(_name_of(call.func) or "")
        if helper is not None and any(
            _name_of(c.func) in _PERSIST_NAMES for c in _calls_in(helper.body)
        ):
            return list(finalbody) + list(helper.body)
    return None


# Every place a buffer is OPENED. This is the denominator, and deriving it from
# the source is the whole point: a hardcoded list of five files would go green
# the day someone adds a sixth `collect_usage()` with no `finally` under it.
_collect_sites: list[tuple[Path, ast.AST, int]] = []
for _path, _tree in _TREES.items():
    _par = _parent_map(_tree)
    for _node in ast.walk(_tree):
        if isinstance(_node, ast.Call) and _name_of(_node.func) == _COLLECT:
            _fn = _par.get(_node)
            while _fn is not None and not isinstance(
                _fn, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                _fn = _par.get(_fn)
            if _fn is not None:
                _collect_sites.append((_path, _fn, _node.lineno))

_guarded: list[tuple[str, list]] = []
_unguarded: list[str] = []
for _path, _fn, _lineno in _collect_sites:
    _module_defs = {
        n.name: n
        for n in ast.walk(_TREES[_path])
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    _scope = None
    for _node in _own_nodes(_fn):
        if isinstance(_node, ast.Try) and _node.finalbody:
            _scope = _persist_scope(_node.finalbody, _module_defs)
            if _scope:
                break
    _label = f"{_rel(_path)}:{_lineno} {_fn.name}()"
    if _scope:
        _guarded.append((_label, _scope))
    else:
        _unguarded.append(_label)

# An EMPTY capture is not a passing capture -- S11's trap, and `storage_check.py`
# 78b inherits the same one. If the walk ever finds zero `collect_usage()` calls
# (a rename, a moved import) then "none of them is unguarded" is true and means
# nothing. The count is part of the assertion, not part of the detail.
check(
    "13. every collect_usage() site drains its buffer from a finally",
    bool(_collect_sites) and not _unguarded,
    f"{len(_collect_sites)} site(s), unguarded: {_unguarded}"
    if _unguarded or not _collect_sites
    else f"all {len(_collect_sites)} sites guarded",
)

# -- 13b. and the finally has SUBSTANCE, not just a shape ------------------
# R13: an AST case is satisfied by the shape. `finally: pass` passes 13. So does
# a `finally` that opens a session and persists an empty list. 13b is the paired
# substance check and it asserts two things about every site, both derived from
# source rather than from a list of five filenames:
#
#   * it opens a session of its OWN. The turn's session is being rolled back --
#     that is the whole premise -- so writing through it writes nothing.
#   * it passes NO `query_id=`. The `queries` row was flushed inside the
#     transaction that just died, so naming its id from a surviving session is a
#     foreign-key violation. D2 executes that insert and watches the database
#     refuse it.
#
# THE DENOMINATOR IS `_collect_sites`, NOT `_guarded`, and that is deliberate.
# Scoring 13b over only the sites that already have a `finally` would mean
# DELETING a `finally` makes 13b easier to pass -- a test that gets greener as
# the feature is removed, which is the failure this repo has a scar for.
_no_session: list[str] = []
_stamped: list[str] = []
for _label, _scope in _guarded:
    _scope_calls = _calls_in(_scope)
    if not any(_name_of(c.func) == "SessionLocal" for c in _scope_calls):
        _no_session.append(_label)
    for _c in _scope_calls:
        if _name_of(_c.func) in _PERSIST_NAMES and any(
            k.arg == "query_id" for k in _c.keywords
        ):
            _stamped.append(_label)

check(
    "13b. each of those opens its OWN session and stamps no query_id",
    len(_guarded) == len(_collect_sites)
    and bool(_guarded)
    and not _no_session
    and not _stamped,
    f"covered {len(_guarded)}/{len(_collect_sites)}; "
    f"no own session: {_no_session or 'none'}; stamps query_id: {_stamped or 'none'}",
)

# -- 13c. the drain names are DERIVED, and the swallow is stated once ------
# Two halves of one rule -- build.md's "a shared contract stated twice drifts,
# and the copy that drifted is never the one you are reading" -- and this file
# broke it in both directions at once.
#
#   * `_PERSIST_NAMES` was a literal `{"persist", "persist_quietly"}` beside a
#     store.py that had stopped being called. It is derived above now.
#   * `store.persist_quietly`'s swallow was copied into `_run_turn` verbatim,
#     log string included: TWO modules emitting the byte-identical warning
#     "could not persist usage for this turn", so an operator grepping it lands
#     on the wrong call site half the time. The turn's copy now names its own
#     consequence instead.
#
# The second half is derived too: the strings come out of store.py's own
# `log.warning` calls rather than being typed here, so a reworded message
# carries this case with it. And the scan is scoped to `backend/app`, which is
# why quoting that string in the comment above does not fail the case -- the
# `deck_check.py` case-14 trap, where looking for the thing was doing the thing.
_store_log_strings = [
    c.args[0].value
    for c in _calls_in(_TREES[_STORE_PATH].body)
    if _name_of(c.func) in {"warning", "info", "error", "exception"}
    and c.args
    and isinstance(c.args[0], ast.Constant)
    and isinstance(c.args[0].value, str)
]
_restated = {
    text: sorted(_rel(q) for q, _ in _TREES.items() if text in q.read_text(encoding="utf-8"))
    for text in _store_log_strings
}
_dupes = {t: mods for t, mods in _restated.items() if len(mods) > 1}

check(
    "13c. drain names come from store.py, and its log lines are not restated",
    bool(_PERSIST_NAMES)
    and "persist" in _PERSIST_NAMES
    and bool(_store_log_strings)
    and not _dupes,
    f"derived writers={sorted(_PERSIST_NAMES)}; "
    f"{len(_store_log_strings)} store.py log line(s), restated elsewhere: "
    f"{ {t[:34]: m for t, m in _dupes.items()} or 'none'}",
)

# -- 13d. a module whose failure handlers LOG has a logger to log with -----
# `app/api/ask.py` used `log` before it had one. `log.exception(...)` in the
# artefact-storage handler resolved to no binding anywhere in the module, so a
# bucket hiccup -- the one thing that handler exists to survive -- would have
# raised NameError and killed the whole turn. The seam below needs the same
# binding for the same reason, and a swallow that cannot say what it swallowed
# is a silence rather than a guard.
#
# Two halves, because either alone is passed by a wrong build. The EXECUTABLE
# half proves this module's `log` is a real Logger that answers the three
# methods its handlers call -- an AST case is satisfied by `log = None`. The
# STRUCTURAL half is the one that generalises: every module on this feature's
# failure paths that reads `log.<something>` must bind `log` at module level, so
# the next module written with a handler and no logger goes red here rather than
# on the branch nobody reaches.
import logging as _logging  # noqa: E402

import app.api.ask as _ask_logmod  # noqa: E402

_ask_log = getattr(_ask_logmod, "log", None)
_log_ok = isinstance(_ask_log, _logging.Logger) and all(
    callable(getattr(_ask_log, m, None)) for m in ("info", "warning", "exception")
)

_LOG_MODULES = [
    q for q in _TREES
    if _rel(q) == "api/ask.py" or _rel(q).startswith("metering/")
]
_unbound: list[str] = []
_log_users = 0
for _q in _LOG_MODULES:
    _tree = _TREES[_q]
    _used = {
        n.value.id
        for n in ast.walk(_tree)
        if isinstance(n, ast.Attribute)
        and isinstance(n.value, ast.Name)
        and n.value.id in {"log", "logger"}
    }
    if not _used:
        continue
    _log_users += 1
    _bound: set[str] = set()
    for _n in _tree.body:
        if isinstance(_n, ast.Assign):
            _bound |= {t.id for t in _n.targets if isinstance(t, ast.Name)}
        elif isinstance(_n, (ast.Import, ast.ImportFrom)):
            _bound |= {a.asname or a.name.split(".")[0] for a in _n.names}
    if _used - _bound:
        _unbound.append(f"{_rel(_q)} uses {sorted(_used - _bound)}")

check(
    "13d. every failure-path module binds the logger its handlers call",
    _log_ok and _log_users > 0 and not _unbound,
    f"ask.log={_ask_log.name if isinstance(_ask_log, _logging.Logger) else _ask_log}; "
    f"{_log_users}/{len(_LOG_MODULES)} module(s) log; unbound: {_unbound or 'none'}",
)

# -- 14. the seam, EXECUTED ------------------------------------------------
# 13 and 13b read source. Neither can tell you a row was produced, and CLAUDE.md
# is explicit that a layer-1 harness cannot prove a query runs, only that it was
# written. These three run the seam.
#
# Defensive import with a sentinel, exactly as `storage_check.py:52-64` does it
# and for exactly its reason: a bare `from app.api.ask import ...` before the
# symbol exists aborts this file with a traceback and prints ZERO failures --
# green-by-abort, which is worse than red.
try:
    from app.api.ask import _TurnReceipt, _persist_orphaned_usage  # noqa: E402

    _SEAM_WHY = ""
except Exception as _exc:  # noqa: BLE001 -- the point is to keep running
    _SEAM_WHY = f"app.api.ask has no failure-path seam yet ({_exc.__class__.__name__}: {_exc})"
    _TurnReceipt = None
    _persist_orphaned_usage = None


class _FakeMeterSession:
    """Records what the seam ADDED and whether it COMMITTED. Nothing else."""

    def __init__(self, rows: list, log: list) -> None:
        self._rows, self._log = rows, log

    def add(self, row) -> None:  # noqa: ANN001
        self._rows.append(row)

    async def commit(self) -> None:
        self._log.append("commit")

    async def rollback(self) -> None:
        self._log.append("rollback")

    async def close(self) -> None:
        self._log.append("close")


class _FakeSessionFactory:
    """Stands in for `app.db.session.SessionLocal`: called, then entered.

    `opened` is asserted as well as `rows`, because "wrote no rows" and "did not
    open a second connection" are different claims and PLAN section 6 R3 is about
    the second one. `run_turn`'s callers hold the turn session open across the
    new `finally`, which none of the four precedents do. 14c reads `opened` on
    the durable path; 14e reads it with an EMPTY buffer, which is the leg the
    `not records` clause exists for and which nothing executed until it was
    written -- every case before it passed n=3.
    """

    def __init__(self) -> None:
        self.rows: list = []
        self.log: list = []
        self.opened = 0

    def __call__(self):  # noqa: ANN204
        self.opened += 1
        return self

    async def __aenter__(self):  # noqa: ANN204
        return _FakeMeterSession(self.rows, self.log)

    async def __aexit__(self, *exc) -> bool:  # noqa: ANN002
        return False


def _orphan_records(n: int = 3) -> list:
    """A turn's buffer: the calls a question really does make before it dies.

    A rewrite, an embedding and a rerank -- the three that are already paid for
    by the time generation raises, which is the shape of the money this feature
    exists to stop losing.
    """
    kinds = ("rewrite", "embedding", "rerank")
    return [
        _UR(
            call_kind=kinds[i % len(kinds)],
            user_id=_u13.uuid4(),
            agent_id=_u13.uuid4(),
            model="deepseek/deepseek-v4-flash-0731",
            served_provider="Relace",
            generation_id=f"gen-orphan-{i}",
            prompt_tokens=100 + i,
            completion_tokens=10 + i,
            cost_usd=1.0e-06 * (i + 1),
        )
        for i in range(n)
    ]


async def _drive_seam(receipt, n: int = 3):  # noqa: ANN001
    """Run the seam against a fake session. Returns (rows_added, sessions_opened)."""
    import app.api.ask as _ask_mod

    fake = _FakeSessionFactory()
    had = hasattr(_ask_mod, "SessionLocal")
    original = getattr(_ask_mod, "SessionLocal", None)
    _ask_mod.SessionLocal = fake
    try:
        await _persist_orphaned_usage(_orphan_records(n), receipt)
    finally:
        if had:
            _ask_mod.SessionLocal = original
        else:
            delattr(_ask_mod, "SessionLocal")
    return fake.rows, fake.opened


_N14 = 3
if _SEAM_WHY:
    # RED, not skipped. The seam's absence IS the defect, so every one of these
    # must fail, and must fail naming the missing symbol rather than a stack
    # trace.
    for _id in ("14a. the seam turns N buffered records into N api_usage rows",
                "14b. and every one of those rows carries query_id = NULL",
                "14c. the seam writes IFF the buffer is not already durable (R1)",
                "14d. rows added then rolled back by a FAILING commit are rewritten",
                "14e. an empty buffer opens no second pool connection (R3)",
                "17a. a CancelledError inside the seam does not escape it",
                "17b. and a genuine cancellation is re-armed, not swallowed"):
        check(_id, False, _SEAM_WHY)
else:
    _raised_rows, _raised_opened = asyncio.run(
        _drive_seam(_TurnReceipt(rows_written=None, committed=False), _N14)
    )
    check(
        "14a. the seam turns N buffered records into N api_usage rows",
        len(_raised_rows) == _N14,
        f"buffered {_N14}, wrote {len(_raised_rows)}",
    )
    # Paired with 14a above it: a build that writes NOTHING would satisfy "every
    # row has query_id None" vacuously, and case 15 proves the unit is capable of
    # producing a non-NULL, so the NULL here is a choice rather than an inability.
    check(
        "14b. and every one of those rows carries query_id = NULL",
        bool(_raised_rows) and all(r.query_id is None for r in _raised_rows),
        f"query_ids={[r.query_id for r in _raised_rows]}",
    )

    # THE DOUBLE-WRITE GUARD. This is the most important case in the file.
    #
    # A `finally` that fires on the success path too records every normal turn's
    # spend twice, and `collect_usage`'s docstring in `context.py` is this repo's
    # record of that exact
    # bug: the eval-job / run_turn double-count, roughly double the real spend,
    # no error anywhere, both totals plausible. Case 13 is passed PERFECTLY by a
    # `finally` that always persists -- it cannot see this at all.
    #
    # Three of the four receipt states are driven here; 14d below drives the
    # fourth, and the split is not tidiness -- mutate `durable` to drop
    # `committed` and all three of these stay correct while 14d goes red.
    #
    # A plain boolean cannot express the third state at all:
    # `persist_quietly` returns (None, None) both when it had nothing to
    # write and when it raised and was swallowed (the two `return None, None`
    # in `store.persist` and `store.persist_quietly`),
    # so "the commit returned" alone would mark a buffer durable that was never
    # written -- reproducing the original hole THROUGH the fix.
    _ok_rows, _ok_opened = asyncio.run(
        _drive_seam(_TurnReceipt(rows_written=_N14, committed=True), _N14)
    )
    _swallowed_rows, _ = asyncio.run(
        _drive_seam(_TurnReceipt(rows_written=None, committed=True), _N14)
    )
    check(
        "14c. the seam writes IFF the buffer is not already durable (R1)",
        len(_raised_rows) == _N14
        and len(_ok_rows) == 0
        and _ok_opened == 0
        and len(_swallowed_rows) == _N14,
        f"raised->{len(_raised_rows)} committed->{len(_ok_rows)} "
        f"(sessions opened {_ok_opened}) swallowed-then-committed->{len(_swallowed_rows)}",
    )

    # -- 14d. THE FOURTH RECEIPT STATE, and the only one that pins `committed` --
    #
    # `(rows_written=N, committed=False)`: the persist ADDED rows and then
    # `await db.commit()` RAISED, rolling them back. That is PLAN section 3.5's
    # headline case -- the exact failure the feature was built for -- and the
    # receipt comment called its three-row table exhaustive without it.
    #
    # It is driven separately from 14c rather than folded into it, because the
    # two halves of `durable` need two different cases. Mutate
    # `durable` to `bool(self.rows_written)` (drop `committed`) and 14c stays
    # GREEN on all three of its states while this one goes red: the seam would
    # decline to rewrite rows that had just been rolled back, silently, on the
    # one path that matters. Measured: deleting `receipt.committed = True`
    # altogether left cases 13/13b/14a/14b/14c/15 and admin_check 5/5b/5c/5d all
    # green while every successful turn double-wrote. Case 16 below reads the
    # source for that mutation; this one reads the behaviour.
    _rolled_rows, _rolled_opened = asyncio.run(
        _drive_seam(_TurnReceipt(rows_written=_N14, committed=False), _N14)
    )
    check(
        "14d. rows added then rolled back by a FAILING commit are rewritten",
        len(_rolled_rows) == _N14 and _rolled_opened == 1,
        f"persist-then-failed-commit->{len(_rolled_rows)} rows "
        f"(sessions opened {_rolled_opened}, must be 1)",
    )

    # -- 14e. an EMPTY buffer opens NO second connection -----------------------
    # R3 is the pool: `run_turn`'s callers hold the turn session open across this
    # `finally`, which none of the four precedents do, and the 404 storm is
    # precisely when every turn wants the extra connection at once. The seam's
    # `not records` clause is the whole mitigation -- and every case above passes
    # n=3, so it was never executed by anything.
    #
    # Both non-durable receipts are driven with an EMPTY buffer, because both are
    # ordinary rather than exotic. `(0, True)` is any successful turn with
    # metering off (`persist` got an empty list, so `rows_written = 0` and
    # `bool(0)` is False). `(None, False)` is a turn that died before its first
    # model call -- which, during the storm this feature exists for, is EVERY
    # turn. Delete `not records` and both open a connection and commit an empty
    # transaction, with nothing raising and no row to show for it.
    _off_rows, _off_opened = asyncio.run(
        _drive_seam(_TurnReceipt(rows_written=0, committed=True), 0)
    )
    _early_rows, _early_opened = asyncio.run(
        _drive_seam(_TurnReceipt(rows_written=None, committed=False), 0)
    )
    check(
        "14e. an empty buffer opens no second pool connection (R3)",
        not _off_rows
        and _off_opened == 0
        and not _early_rows
        and _early_opened == 0,
        f"metering-off success: {len(_off_rows)} rows / {_off_opened} sessions; "
        f"raised before the first call: {len(_early_rows)} rows / {_early_opened} sessions",
    )

# -- 15. the NULL is produced at the unit, and is a choice ------------------
# What makes 14b non-vacuous. `to_row` (store.py:41) reads
# `query_id=record.query_id or query_id`, and no `meter_as` call site in the repo
# sets `UsageRecord.query_id` -- so calling `persist` with no `query_id=` kwarg
# yields NULL for free. Both halves are asserted: omitting it gives NULL, and
# passing it gives a value, so the NULL is a decision rather than an inability.
_rec15 = _orphan_records(1)[0]
_row15_bare = metering_store.to_row(_rec15)
_row15_stamped = metering_store.to_row(_rec15, query_id=_u13.uuid4())

_meter_as_stamps = [
    f"{_rel(p)}:{n.lineno}"
    for p, t in _TREES.items()
    for n in ast.walk(t)
    if isinstance(n, ast.Call)
    and _name_of(n.func) == "meter_as"
    and any(k.arg == "query_id" for k in n.keywords)
]

check(
    "15. to_row omits query_id -> NULL, stamps it -> a value, and no meter_as sets it",
    _row15_bare.query_id is None
    and _row15_stamped.query_id is not None
    and not _meter_as_stamps,
    f"bare={_row15_bare.query_id} stamped={'set' if _row15_stamped.query_id else 'None'} "
    f"meter_as sites passing query_id: {_meter_as_stamps or 'none'}",
)

# ---------------------------------------------------------------------------
# 16-16b. THE RECEIPT'S TWO WRITERS, PINNED WHERE THEY ARE WRITTEN
# ---------------------------------------------------------------------------
# 14a-14e drive the seam through a receipt they construct themselves, which is
# what makes them independent of how `_run_turn` fills one in -- and is exactly
# why none of them can see the receipt being filled in WRONGLY. Measured: delete
# `receipt.committed = True` from `_run_turn` and cases 13/13b/14a/14b/14c/15 and
# `admin_check.py` 5/5b/5c/5d ALL STAY GREEN while every successful turn writes
# its usage twice -- once attributed inside the transaction, once unattributed
# from the seam, because `durable` is `committed and rows_written` and the first
# half is now never set. That is this repository's named double-count scar
# (`collect_usage`'s docstring in `context.py`) reproduced THROUGH the fix, with
# no error, no log line
# and two plausible totals.
#
# The comment at that line used to say no automated case could hold it, "because
# both positions are syntactically fine and every offline case constructs its own
# receipt". The premise is true and the conclusion does not follow: both
# positions are syntactically fine and they are not the SAME position, and an AST
# case reads position for a living. So the criterion that was carried as the
# manual check M2 is automated here, and M2 survives only as the mutation that
# proves 16 discriminates.
#
# Polarity, stated because it is the trap: an empty capture must FAIL. If the
# walk finds no `await db.commit()` at all -- renamed, moved into a helper, the
# function renamed -- then "the statement after it is the flag" is vacuously true
# of nothing. `bool(_commits)` is part of the assertion, never of the detail.
_ASK_PATH = _APP / "api" / "ask.py"
_ask_tree = _TREES[_ASK_PATH]
_ask_parents = _parent_map(_ask_tree)
_run_turn_fns = [
    n
    for n in ast.walk(_ask_tree)
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "_run_turn"
]


def _stmt_lists(fn):  # noqa: ANN001, ANN202
    """Every statement LIST inside `fn`, nested functions excluded.

    Statement lists rather than `Try` bodies specifically: a hoist that also
    moved the commit out of its `try` would otherwise walk out of view, and the
    property being asserted -- these two statements are adjacent, in this order
    -- does not depend on the handler existing.
    """
    for node in [fn, *_own_nodes(fn)]:
        for field in ("body", "orelse", "finalbody"):
            seq = getattr(node, field, None)
            if isinstance(seq, list) and seq and all(isinstance(x, ast.stmt) for x in seq):
                yield seq


def _is_await_commit(stmt) -> bool:  # noqa: ANN001
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Await)
        and isinstance(stmt.value.value, ast.Call)
        and _name_of(stmt.value.value.func) == "commit"
    )


def _assigns_receipt(stmt, field: str) -> bool:  # noqa: ANN001
    return isinstance(stmt, ast.Assign) and any(
        isinstance(t, ast.Attribute)
        and t.attr == field
        and isinstance(t.value, ast.Name)
        and t.value.id == "receipt"
        for t in stmt.targets
    )


_commits: list[tuple[list, int]] = []
_flag_sites: list[int] = []
_rows_sites: list[ast.Assign] = []
for _fn in _run_turn_fns:
    for _seq in _stmt_lists(_fn):
        for _i, _stmt in enumerate(_seq):
            if _is_await_commit(_stmt):
                _commits.append((_seq, _i))
            if _assigns_receipt(_stmt, "committed"):
                _flag_sites.append(_stmt.lineno)
            if _assigns_receipt(_stmt, "rows_written"):
                _rows_sites.append(_stmt)

_after_commit = [
    (
        _seq[_i].lineno,
        _i + 1 < len(_seq)
        and _assigns_receipt(_seq[_i + 1], "committed")
        and isinstance(_seq[_i + 1].value, ast.Constant)
        and _seq[_i + 1].value.value is True,
    )
    for _seq, _i in _commits
]

check(
    "16. receipt.committed is set on the line after await db.commit() returns (R2)",
    len(_commits) == 1
    and all(ok for _, ok in _after_commit)
    and len(_flag_sites) == 1,
    f"{len(_commits)} commit(s) in _run_turn at {[ln for ln, _ in _after_commit] or 'NONE'}; "
    f"next statement is the flag: {[ok for _, ok in _after_commit] or 'nothing to check'}; "
    f"receipt.committed assigned at {_flag_sites or 'NOWHERE'} (must be exactly one line)",
)

# -- 16b. and rows_written is written only where the persist SUCCEEDED -----
# The other writer, and the other half of the third receipt state. If the
# `except` around the persist also set `rows_written`, a swallowed persist would
# report rows it never added, the buffer would read as durable on a turn that
# wrote nothing, and the seam would decline -- the ORIGINAL hole, reached through
# the fix. `store.persist_quietly` returning `(None, None)` for two different
# reasons is precisely why the count is taken from the records handed over rather
# than from the writer's return.
_rows_in_handler = []
for _stmt in _rows_sites:
    _node = _stmt
    while _node is not None and _node not in _run_turn_fns:
        if isinstance(_node, ast.ExceptHandler):
            _rows_in_handler.append(_stmt.lineno)
            break
        _node = _ask_parents.get(_node)

_rows_follows_persist = []
for _fn in _run_turn_fns:
    for _seq in _stmt_lists(_fn):
        _p = [i for i, st in enumerate(_seq) if any(
            _name_of(c.func) in _PERSIST_NAMES for c in _calls_in([st])
        )]
        _r = [i for i, st in enumerate(_seq) if _assigns_receipt(st, "rows_written")]
        if _p and _r:
            _rows_follows_persist.append(min(_r) > min(_p))

check(
    "16b. receipt.rows_written is set only where the persist SUCCEEDED",
    len(_rows_sites) == 1
    and not _rows_in_handler
    and _rows_follows_persist == [True],
    f"assigned at {[st.lineno for st in _rows_sites] or 'NOWHERE'} "
    f"(must be exactly one); inside an except handler: {_rows_in_handler or 'no'}; "
    f"follows the persist call in the same block: {_rows_follows_persist or 'NOT FOUND'}",
)

# ---------------------------------------------------------------------------
# 17a-17b. THE SEAM RUNS IN A `finally`, SO WHAT IT RAISES REPLACES THE ERROR
# ---------------------------------------------------------------------------
# `_run_turn`'s commit handler deletes staged R2 keys and RE-RAISES the error
# that killed the turn. The seam runs after that raise, in `run_turn`'s
# `finally`, so anything escaping it replaces a `404 No endpoints found` with an
# accounting error and the caller is told the meter broke.
#
# `except Exception` did not say that, and the exception it misses is the one
# that matters: `asyncio.CancelledError` is BaseException-only (verified on this
# venv, Python 3.12.10), so a uvicorn shutdown or `--reload` landing inside the
# seam's single DB round trip did exactly the forbidden thing. Measured before
# the fix by driving the seam with a session factory that raises it: a turn that
# died of RuntimeError('404 No endpoints found...') reported `CancelledError` to
# its caller.
#
# TWO cases, because either alone is passed by a wrong build. 17a alone is
# passed by a seam that eats cancellation whole -- correct for this frame,
# and a server that declines to shut down. 17b alone is passed by a seam that
# lets the cancellation through unchanged. The pair is containment AND re-arm.
if not _SEAM_WHY:

    class _CancelOnEnter:
        """A session factory that dies the way a shutdown does.

        `opened` is counted so 17a asserts the seam really TRIED -- a build that
        returned 0 without attempting anything would otherwise pass it.
        """

        def __init__(self) -> None:
            self.opened = 0

        def __call__(self):  # noqa: ANN204
            self.opened += 1
            return self

        async def __aenter__(self):  # noqa: ANN204
            raise asyncio.CancelledError("shutdown mid-seam")

        async def __aexit__(self, *exc) -> bool:  # noqa: ANN002
            return False

    class _HangOnEnter:
        """Holds the seam inside its await so a REAL cancel can be delivered.

        17a's factory raises `CancelledError` without anyone asking for a
        cancel, which is the spurious case; this one is the genuine one, and the
        seam must tell them apart -- `task.cancelling()` is the only thing that
        can.
        """

        def __init__(self) -> None:
            self.entered = asyncio.Event()

        def __call__(self):  # noqa: ANN204
            return self

        async def __aenter__(self):  # noqa: ANN204
            self.entered.set()
            await asyncio.Event().wait()

        async def __aexit__(self, *exc) -> bool:  # noqa: ANN002
            return False

    async def _cancel_cases():  # noqa: ANN202
        import app.api.ask as _ask_cancelmod

        original = _ask_cancelmod.SessionLocal
        # Both cases swallow on purpose and the swallow logs `exc_info=True`, so
        # without this the harness prints two tracebacks that read as failures.
        _ask_cancelmod.log.disabled = True
        seen: list = []
        try:
            spurious = _CancelOnEnter()
            _ask_cancelmod.SessionLocal = spurious
            try:
                a_out = (
                    "returned",
                    await _persist_orphaned_usage(_orphan_records(1), _TurnReceipt()),
                )
            except BaseException as exc:  # noqa: BLE001 -- the escape IS the result
                a_out = ("raised", exc.__class__.__name__)

            hang = _HangOnEnter()
            _ask_cancelmod.SessionLocal = hang

            async def _body() -> None:
                try:
                    seen.append(
                        (
                            "returned",
                            await _persist_orphaned_usage(
                                _orphan_records(1), _TurnReceipt()
                            ),
                        )
                    )
                except BaseException as exc:  # noqa: BLE001
                    seen.append(("raised", exc.__class__.__name__))

            task = asyncio.create_task(_body())
            await hang.entered.wait()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return a_out, spurious.opened, (seen[0] if seen else ("never ran", "-")), task.cancelled()
        finally:
            _ask_cancelmod.SessionLocal = original
            _ask_cancelmod.log.disabled = False

    _a_out, _a_opened, _b_out, _b_cancelled = asyncio.run(_cancel_cases())

    check(
        "17a. a CancelledError inside the seam does not escape it",
        _a_out == ("returned", 0) and _a_opened == 1,
        f"seam {_a_out[0]} {_a_out[1]} after {_a_opened} attempt(s) to open a session "
        f"(raised -> the accounting replaced the turn's own error)",
    )
    check(
        "17b. and a genuine cancellation is re-armed, not swallowed",
        _b_out[0] == "returned" and _b_cancelled,
        f"seam {_b_out[0]} {_b_out[1]}; task ended cancelled: {_b_cancelled} "
        f"(False -> a shutdown this turn quietly declined)",
    )

# ---------------------------------------------------------------------------
# --live -- the other half, and the ONLY half that can fail for the real reason.
#
# Everything above builds synthetic LLMResults, so it asserts what the meter does
# with a SHAPE, never that OpenRouter still sends that shape. A provider that
# stopped returning `cost` would leave all eleven cases green and the console
# empty. That is `loop.md` T2 stated as precisely as this repo knows how to state
# it, so the live cases assert VALUES -- cost > 0, tokens > 0, a provider name --
# and never "the call succeeded".
#
# Two real calls: one streamed (the path that loses cost without the subclass)
# and one not. Costs a fraction of a cent.
# ---------------------------------------------------------------------------
if "--live" in sys.argv:
    import uuid as _u

    from langchain_core.messages import HumanMessage  # noqa: E402
    from langchain_core.tools import tool  # noqa: E402

    from app.metering.meter import UsageRecord  # noqa: E402

    print("\n" + "=" * 74)
    print("--live: what OpenRouter actually sent, through build_chat_model")
    print("=" * 74)

    @tool
    def search_corpus(query: str) -> str:
        """Search the course corpus."""
        return "no results"

    seen: list[UsageRecord] = []

    class Collect:
        def record(self, rec: UsageRecord) -> None:
            seen.append(rec)

    async def _live() -> None:
        model = build_chat_model(
            settings.generation_model,
            temperature=settings.generation_temperature,
            top_k=settings.generation_top_k,
            max_tokens=48,
            reasoning=settings.generation_reasoning,
        )
        for cb in model.callbacks or []:
            if isinstance(cb, UsageMeter):
                cb.sink = Collect()
        # Tools bound, because that is the request generation actually sends and
        # CLAUDE.md records tool binding narrowing routing to an intersection.
        bound = model.bind_tools([search_corpus])
        messages = [HumanMessage("Reply with exactly: OK")]
        user, agent = _u.uuid4(), _u.uuid4()

        with meter_as(user_id=user, agent_id=agent, call_kind="generation"):
            acc = None
            async for piece in bound.astream(messages):
                acc = piece if acc is None else acc + piece
        with meter_as(user_id=user, agent_id=agent, call_kind="critic"):
            await bound.ainvoke(messages)
        return user, agent

    user_id, agent_id = asyncio.run(_live())

    for rec in seen:
        print(
            f"  {rec.call_kind:11s} tokens={rec.prompt_tokens}/{rec.completion_tokens} "
            f"cost={rec.cost_usd} provider={rec.served_provider} model={rec.model}"
        )

    streamed = next((r for r in seen if r.call_kind == "generation"), None)
    invoked = next((r for r in seen if r.call_kind == "critic"), None)

    check("L1. two live calls produced two records", len(seen) == 2, f"n={len(seen)}")
    check(
        "L2. the STREAMED call recorded a cost GREATER THAN ZERO",
        streamed is not None and (streamed.cost_usd or 0) > 0,
        f"cost={streamed.cost_usd if streamed else None}",
    )
    check(
        "L3. the streamed call recorded the SERVED provider (llm_check cannot)",
        bool(streamed and streamed.served_provider),
        f"provider={streamed.served_provider if streamed else None}",
    )
    check(
        "L4. the non-streamed call recorded a cost greater than zero",
        invoked is not None and (invoked.cost_usd or 0) > 0,
        f"cost={invoked.cost_usd if invoked else None}",
    )
    check(
        "L5. both records carry tokens greater than zero",
        all((r.prompt_tokens or 0) > 0 for r in seen),
    )
    check(
        "L6. attribution survived from meter_as to both records",
        all(r.user_id == user_id and r.agent_id == agent_id for r in seen),
    )
    check(
        "L7. the model slug is present and not doubled",
        all(r.model and r.model.count("/") == 1 for r in seen),
        f"models={[r.model for r in seen]}",
    )

    # PRINTED, never asserted -- the same discipline `route_check.py` applies to
    # its provider distribution. Two identical requests have been measured
    # costing 2.002e-05 and 5.684e-06, a 3.5x spread, because they landed on
    # different endpoints. That is not a defect to assert against; it is the
    # measurement that makes a local price table the WRONG answer, and it is
    # worth seeing every time this runs.
    costs = [r.cost_usd for r in seen if r.cost_usd]
    if len(costs) == 2 and min(costs) > 0:
        print(f"\n  cost spread this run: {max(costs) / min(costs):.2f}x "
              f"({costs[0]:.3g} vs {costs[1]:.3g}) -- provider variance, not a bug.")

# ---------------------------------------------------------------------------
# --db -- real SQL, against the real schema.
#
# CLAUDE.md's rule, learned the seventh time a green suite was wrong: a layer-1
# harness cannot prove a query RUNS, only that it was written. Cases 13-17b above
# read source and drive a fake session, so between them they would be perfectly
# green against a column that does not exist, a NOT NULL that rejects the row, a
# foreign key that refuses the insert, or a coverage aggregate that reads NULL
# `query_id`s as covered. These three execute.
#
# THREE STATES, not two, exactly as `storage_check.py:18-22` requires: a case
# that could not be measured -- no database reachable, no suitable row -- must
# never print green and must not fail the suite either.
#
# **This is the only mode in this file that WRITES, and it writes to production.**
# D1 commits one `api_usage` row because the seam it executes opens its own
# session and commits; D2 and D3 flush and roll back. Three rules make that safe,
# and all three are the fixture paragraph below: the row names a user this
# harness created rather than a real one, everything it wrote is deleted in a
# `finally`, and the deletion is re-read from a second session as case D4 rather
# than assumed by a `[warn]` nobody fails on.
#
#     backend/.venv/Scripts/python.exe scripts/metering_check.py --db
# ---------------------------------------------------------------------------
if "--db" in sys.argv:
    import uuid as _udb  # noqa: E402

    from sqlalchemy import delete as _sa_delete  # noqa: E402
    from sqlalchemy import func as _sa_func  # noqa: E402
    from sqlalchemy import select as _sa_select  # noqa: E402
    from sqlalchemy.exc import IntegrityError as _IntegrityError  # noqa: E402

    from app.db.models import Agent as _AgentRow  # noqa: E402
    from app.db.models import ApiUsage as _ApiUsage  # noqa: E402
    from app.db.models import Query as _QueryRow  # noqa: E402
    from app.db.models import User as _UserRow  # noqa: E402
    from app.db.session import SessionLocal as _RealSessionLocal  # noqa: E402

    print("\n" + "=" * 74)
    print("--db: what the real schema does with these rows")
    print("=" * 74)
    print("  D1 COMMITS one marked api_usage row against its OWN fixture user,")
    print("  then D4 deletes it and VERIFIES the deletion from a second session.")

    # PRINT THE TARGET. A green --db run against nothing at all is the vacuous
    # pass `storage_check.py` R9 is written about; the tell is the absence of a
    # printed subject, so printing it is mandatory rather than nice.
    try:
        _host = settings.async_database_url.split("@")[-1].split("/")[0]
    except Exception:  # noqa: BLE001
        _host = "unknown"
    print(f"  database: {_host}")

    # Findable by name if a row ever leaks -- PLAN section 3.6, and the same
    # convention `seed_download_fixture.py` uses for its titles. `generation_id`
    # is the right column for it: it is free text, it is indexed by nothing that
    # matters, and a stray `gen-harness-` row is unmistakable in the console.
    _MARK = f"gen-harness-D1-{_udb.uuid4().hex[:12]}"

    # ----------------------------------------------------------------------
    # THE FIXTURE THIS HARNESS OWNS. D1 is the only case in this file that
    # COMMITS, and `DATABASE_URL` points at the live Render Postgres holding
    # real users' data.
    #
    # It used to read `select(User).limit(1)` and `select(Agent).limit(1)`,
    # which is a REAL PERSON and, worse, an agent that person may not even own
    # -- so a committed `cost_usd` of 5.338e-05 landed on their name in the very
    # accounting table this feature exists to make trustworthy. Two rows out of
    # 15 in `users` are dev-login twins of one human (CLAUDE.md, "ONE PERSON IS
    # TWO USER ROWS"), so "it is only one row" was never a safe reading either.
    #
    # `slice_check.py` (slice-check@localhost) and `ui_check.py`
    # (ui-check@groundwork.local) already establish the convention: create your
    # own subject, key it on a `google_sub` no Google login can produce, say
    # loudly what you made, and delete it. `dev|` is the prefix the auth shim
    # reserves; `metering-check-local` cannot collide with either that or a real
    # `sub`, which is 21 digits.
    _FIXTURE_SUB = "metering-check-local"
    _FIXTURE_EMAIL = "metering-check@groundwork.local"
    _FIXTURE_AGENT = "Metering Check"
    # What this run created, so cleanup asserts against reality rather than
    # against an assumption. Empty means the run never got far enough to write.
    _created: list[str] = []

    async def _fixture(db) -> tuple:  # noqa: ANN001
        """Get-or-create the harness's own user and agent. Idempotent.

        Committed, because D1's whole point is that the seam opens its own
        session: the row it writes has to name ids that a SECOND connection can
        see. Both are deleted again in `_db_main`'s finally, and D4 verifies it.
        """
        user = (
            await db.execute(
                _sa_select(_UserRow).where(_UserRow.google_sub == _FIXTURE_SUB)
            )
        ).scalar_one_or_none()
        if user is None:
            user = _UserRow(
                id=_udb.uuid4(),
                google_sub=_FIXTURE_SUB,
                email=_FIXTURE_EMAIL,
                name="Metering Check",
                role="user",
            )
            db.add(user)
            await db.flush()

        agent = (
            await db.execute(
                _sa_select(_AgentRow).where(
                    _AgentRow.owner_user_id == user.id,
                    _AgentRow.name == _FIXTURE_AGENT,
                )
            )
        ).scalar_one_or_none()
        if agent is None:
            agent = _AgentRow(
                id=_udb.uuid4(),
                owner_user_id=user.id,
                name=_FIXTURE_AGENT,
                description="Throwaway agent owned by scripts/metering_check.py --db.",
            )
            db.add(agent)
            await db.flush()

        await db.commit()
        _created.append("fixture")
        return user.id, agent.id

    async def _numerator(db) -> int:  # noqa: ANN001
        """The admin console's coverage NUMERATOR, `admin.py:273-277`.

        Copied in shape rather than imported, because importing the route would
        assert the route agrees with itself. This is the arithmetic D3 is about.
        """
        stmt = _sa_select(_sa_func.count(_sa_func.distinct(_ApiUsage.query_id))).where(
            _ApiUsage.query_id.is_not(None)
        )
        return int((await db.execute(stmt)).scalar() or 0)

    async def _db_cases() -> None:
        # ---- D1. the seam, committed, read back from a SECOND session -----
        # The case that would catch a NOT NULL, a mistyped column or a mapper
        # that quietly refuses the row -- none of which 13-17b can see.
        if _SEAM_WHY:
            check("D1. the seam writes an unattributed row the schema accepts", False, _SEAM_WHY)
        else:
            async with _RealSessionLocal() as db:
                # THE HARNESS'S OWN SUBJECT, never `select(User).limit(1)`.
                # Fabricating a bare uuid4 is not an option either -- `user_id`
                # and `agent_id` are foreign keys, so an id naming nobody is
                # rejected, which is D2's subject in a different column and is
                # not what D1 measures. So the ids have to be real AND ours.
                _uid, _aid = await _fixture(db)

            print(
                f"  CREATED (and deleted again below): user {_FIXTURE_EMAIL} "
                f"[{_uid}], agent '{_FIXTURE_AGENT}' [{_aid}]"
            )
            print(f"  ONE api_usage row will be COMMITTED and removed, marked {_MARK}")

            rec = _UR(
                call_kind="rewrite",
                user_id=_uid,
                agent_id=_aid,
                model="deepseek/deepseek-v4-flash-0731",
                served_provider="Relace",
                generation_id=_MARK,
                prompt_tokens=493,
                completion_tokens=12,
                cost_usd=5.338e-05,
            )

            # NO fake session here. The seam opens `SessionLocal` itself and this
            # is the one case where that must be the real one.
            await _persist_orphaned_usage(
                [rec], _TurnReceipt(rows_written=None, committed=False)
            )
            # BEFORE the read-back, so a failure between the two still reaches
            # cleanup. `_created` is what D4 asserts against.
            _created.append("usage")

            async with _RealSessionLocal() as db2:
                row = (
                    await db2.execute(
                        _sa_select(_ApiUsage).where(_ApiUsage.generation_id == _MARK)
                    )
                ).scalar_one_or_none()

            check(
                "D1. the seam writes an unattributed row the schema accepts",
                row is not None
                and row.query_id is None
                # Attribution SURVIVED while the query id did not, which is the
                # whole shape of a recovered row: the money is still the right
                # user's and the right agent's, it just belongs to no turn.
                # Only assertable now that the ids are the harness's own.
                and row.user_id == _uid
                and row.agent_id == _aid
                and row.cost_usd == 5.338e-05
                and row.call_kind == "rewrite",
                f"row={'present' if row is not None else 'ABSENT'} "
                f"query_id={getattr(row, 'query_id', '-')} "
                f"user={getattr(row, 'user_id', '-')} cost={getattr(row, 'cost_usd', '-')}",
            )

        # ---- D2. the foreign key is real, not assumed ---------------------
        # PLAN section 3.3 reason 1: the `queries` row was flushed inside the
        # transaction that just died, so writing its id from the surviving
        # session is a referential-integrity violation. That is the argument the
        # whole `query_id = NULL` contract rests on, and an argument is not a
        # measurement. This insert is expected to be REFUSED.
        #
        # **The premise is asserted before the refusal, and it was not.** An
        # earlier version set `_rejected` on ANY exception and printed only the
        # class name, so a NOT NULL or a CHECK violation on an unrelated column
        # would have passed it as "the foreign key refused" -- the row would be
        # rejected for a reason that says nothing about `query_id`, and the
        # contract this case exists to measure would go unmeasured while reading
        # green. Two narrowings: the three attribution columns must be NULLABLE
        # (so the only thing left to refuse a value is the FK), and the error
        # must be an `IntegrityError` that names a foreign key.
        _cols = _ApiUsage.__table__.c
        _nullable = {n: bool(_cols[n].nullable) for n in ("user_id", "agent_id", "query_id")}
        _phantom = _udb.uuid4()
        _rejected, _detail2 = False, ""
        async with _RealSessionLocal() as db:
            try:
                db.add(
                    _ApiUsage(
                        provider="openrouter",
                        operation="chat",
                        call_kind="rewrite",
                        query_id=_phantom,
                        cost_usd=1e-06,
                        cost_is_estimated=False,
                        generation_id=f"{_MARK}-d2",
                    )
                )
                await db.flush()
                _detail2 = "the insert SUCCEEDED -- there is no foreign key"
            except Exception as exc:  # noqa: BLE001 -- the refusal IS the result
                _text = str(exc).lower()
                _rejected = isinstance(exc, _IntegrityError) and (
                    "foreign key" in _text or "foreignkeyviolation" in _text
                )
                _detail2 = f"{exc.__class__.__name__}: {ascii(str(exc).splitlines()[0])[:120]}"
            finally:
                await db.rollback()

        check(
            "D2. an api_usage row naming an uncommitted queries.id is REJECTED",
            _rejected and all(_nullable.values()),
            f"{_detail2}; nullable columns (so the FK is what refused): {_nullable}",
        )

        # ---- D3. coverage, in BOTH directions -----------------------------
        # A check that a number did not move is also passed by a query that is
        # broken, so the second half is not optional. Both legs are flushed and
        # then ROLLED BACK -- nothing here is committed, so there is nothing to
        # clean up and no window in which a stranger sees a harness row.
        async with _RealSessionLocal() as db:
            base = await _numerator(db)
            metering_store.persist(
                db,
                [
                    _UR(
                        call_kind="embedding",
                        model="google/gemini-embedding-2",
                        generation_id=f"{_MARK}-d3a",
                        prompt_tokens=7,
                        cost_usd=1.4e-06,
                    )
                ],
            )
            await db.flush()
            after_null = await _numerator(db)
            await db.rollback()

        async with _RealSessionLocal() as db:
            # A committed query that nothing has metered yet -- adding a row for
            # one already in the set would move the DISTINCT count by zero and
            # look like a failure of the fix rather than of the fixture.
            covered = _sa_select(_ApiUsage.query_id).where(_ApiUsage.query_id.is_not(None))
            uncovered = (
                await db.execute(
                    _sa_select(_QueryRow.id).where(_QueryRow.id.not_in(covered)).limit(1)
                )
            ).scalar()

            if uncovered is None:
                not_measured(
                    "D3. an unattributed row leaves coverage flat, an attributed one raises it",
                    "no committed queries row without api_usage; the second half cannot be "
                    "measured, so the first half alone would only prove a number did not move",
                )
            else:
                base2 = await _numerator(db)
                db.add(
                    metering_store.to_row(
                        _UR(
                            call_kind="generation",
                            model="deepseek/deepseek-v4-flash-0731",
                            generation_id=f"{_MARK}-d3b",
                            prompt_tokens=100,
                            cost_usd=2.0e-06,
                        ),
                        query_id=uncovered,
                    )
                )
                await db.flush()
                after_real = await _numerator(db)
                await db.rollback()

                check(
                    "D3. an unattributed row leaves coverage flat, an attributed one raises it",
                    after_null == base and after_real == base2 + 1,
                    f"unattributed {base}->{after_null} (must not move); "
                    f"attributed {base2}->{after_real} (must be +1)",
                )

    async def _db_main() -> None:
        """Run the cases, then remove everything this run created and PROVE it.

        **Cleanup used to be best-effort and silent**: one `except Exception:
        print("[warn] could not clean up ...")` that neither failed the run nor
        reached `not_measured`, so a cleanup that did not happen left invented
        money in the accounting table while the suite printed "all checks
        passed". A harness that can corrupt the data its own feature is about is
        worse than the defect it tests, so the deletion is now VERIFIED by a
        fresh session and its result is a case (D4) rather than a warning.
        """
        try:
            await _db_cases()
        finally:
            if not _created:
                # Never got as far as writing anything -- the outer handler
                # below reports why. Asserting a clean database here would be
                # asserting nothing.
                print("  nothing was created this run; no cleanup needed")
            else:
                _left, _why = None, ""
                try:
                    async with _RealSessionLocal() as db:
                        await db.execute(
                            _sa_delete(_ApiUsage).where(
                                _ApiUsage.generation_id.like(f"{_MARK}%")
                            )
                        )
                        # Bulk DELETEs, never `await db.delete(user)`. The ORM
                        # path would lazy-load `User.agents` and `User.sessions`
                        # to de-associate them, and a lazy load inside an async
                        # session raises MissingGreenlet from a validator --
                        # a traceback naming neither this line nor the column.
                        # Agents first: `agents.owner_user_id` is NOT NULL.
                        _fx_id = (
                            await db.execute(
                                _sa_select(_UserRow.id).where(
                                    _UserRow.google_sub == _FIXTURE_SUB
                                )
                            )
                        ).scalar()
                        if _fx_id is not None:
                            await db.execute(
                                _sa_delete(_AgentRow).where(
                                    _AgentRow.owner_user_id == _fx_id
                                )
                            )
                            await db.execute(
                                _sa_delete(_UserRow).where(_UserRow.id == _fx_id)
                            )
                        await db.commit()

                    # A SECOND session, because a delete that was rolled back
                    # still looks deleted from inside the transaction that made
                    # it. This is the same reason D1 reads its row back from
                    # `db2` rather than from the session that wrote it.
                    async with _RealSessionLocal() as db2:
                        _rows = int(
                            (
                                await db2.execute(
                                    _sa_select(_sa_func.count()).select_from(_ApiUsage).where(
                                        _ApiUsage.generation_id.like(f"{_MARK}%")
                                    )
                                )
                            ).scalar()
                            or 0
                        )
                        _user_left = (
                            await db2.execute(
                                _sa_select(_UserRow.id).where(
                                    _UserRow.google_sub == _FIXTURE_SUB
                                )
                            )
                        ).scalar()
                    _left = (_rows, _user_left)
                except Exception as exc:  # noqa: BLE001 -- reported as a FAILURE
                    _why = f"{exc.__class__.__name__}: {ascii(str(exc))[:120]}"

                check(
                    "D4. the harness removed everything it created (verified)",
                    _left == (0, None),
                    (
                        f"api_usage rows matching {_MARK}: {_left[0]}; "
                        f"fixture user left: {_left[1] or 'none'}"
                        if _left is not None
                        else f"CLEANUP FAILED, {_why} -- remove by hand: "
                        f"DELETE FROM api_usage WHERE generation_id LIKE '{_MARK}%'; "
                        f"then the user with google_sub='{_FIXTURE_SUB}'"
                    ),
                )

    try:
        asyncio.run(_db_main())
    except Exception as _dberr:  # noqa: BLE001
        # NOT MEASURED, loudly, and with the one command that decides it.
        # CLAUDE.md: `ConnectionDoesNotExistError` means "your IP changed" far
        # more often than it means anything else, and the traceback names no
        # firewall. A quiet skip here would make a fresh laptop look green.
        not_measured(
            "D1/D2/D3/D4 -- the whole --db block",
            f"{_dberr.__class__.__name__}: {_dberr}. "
            "Check the allow-list before the traceback: curl -s https://api.ipify.org",
        )

print("\n" + "=" * 74)
if unmeasured:
    # Printed BEFORE the verdict and never folded into it. A suite that goes red
    # because a firewall said no teaches its reader to ignore red; a suite that
    # hides an unmeasured guard teaches them to trust green.
    print(f"{len(unmeasured)} NOT MEASURED (guards that did not run):")
    for name in unmeasured:
        print(f"  - {name}")
if failures:
    print(f"{len(failures)} FAILED:")
    for name in failures:
        print(f"  - {name}")
    sys.exit(1)
print("all checks passed" + (" (see NOT MEASURED above)" if unmeasured else ""))
