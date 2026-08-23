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

Case 6 is the one to read first if this file ever goes red in production: it is
the difference between a broken meter and a broken product.

**What this file structurally CANNOT check.** It builds synthetic LLMResults, so
it asserts what the meter does with a shape, never that OpenRouter still sends
that shape. `scripts/route_check.py --live` is the other half and asserts
`cost > 0` off a real call. Run both when either changes.

    backend/.venv/Scripts/python.exe scripts/metering_check.py

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


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "[ok]  " if condition else "[FAIL]"
    print(f"{status} {name}" + (f" -- {detail}" if detail else ""))
    if not condition:
        failures.append(name)


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

# Fixpoint. `build_chat_model` is the seed because it is the single chokepoint
# every chat call passes through -- the same property that let one callback meter
# eight call sites is what makes one seed enough here.
_unmetered = {n for n in _defs if "build_chat_model" in _calls[n]} - _scoped
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
    "12. no entry point reaches build_chat_model outside a meter_as scope (R4)",
    not _leaks,
    f"unmetered: {_leaks}" if _leaks else f"{len(_scoped)} scoped functions cover the graph",
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

print("\n" + "=" * 74)
if failures:
    print(f"{len(failures)} FAILED:")
    for name in failures:
        print(f"  - {name}")
    sys.exit(1)
print("all checks passed")
