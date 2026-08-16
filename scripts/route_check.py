"""Layer 2 harness: what OPENROUTER DID with the request, not what this repo asked.

WHY THIS FILE EXISTS, AND WHY `llm_check.py` STRUCTURALLY CANNOT DO ITS JOB.

`llm_check.py` names the hole in its own docstring: "it asserts what the repo put
in the request, never what OpenRouter did with it. A pin that is silently ignored
looks exactly like a pin that worked." Everything it checks is a property of a
dict this process built, so every case is decidable offline -- and offline is
precisely where the interesting failure hides.

That failure is not hypothetical. `app/rag/llm.py` carries a NO_GO recorded on
2026-08-16: `provider.order: ["DeepSeek"]` returned **http=200 on 3/3 calls and
was served by BAIDU every time**. Not an error, not a warning, not a dropped
field -- the preference was accepted and ignored, and the only thing in the whole
exchange that said so was the response's top-level `provider` name, which nothing
in this project read. `loop.md` T2 exactly: the error-shaped check passes while
the outcome silently did not happen. A misspelled provider name, an unreachable
one, and a working pin are three states this repo currently cannot tell apart.

So this harness exists to read the one field that distinguishes them, and to fail
if that field ever stops being reachable.

WHAT IT ASSERTS, AND THE ONE THING IT DELIBERATELY DOES NOT.

It asserts only that **a served-provider name was recoverable at all**. It never
asserts WHICH provider. `allow_fallbacks` is on by default and OpenRouter
load-balances across eligible endpoints, so a different provider between two runs
is the system working correctly -- and a suite that goes red for that teaches its
reader to ignore red, which is the same argument that put `[rate]` rather than
`[FAIL]` in `agentic_check.py`.

**The first two live runs settle that argument rather than merely arguing it.**
Four calls each, identical model, identical parameters, minutes apart:

    run 1   OpenInference, Together, GMICloud   (control: Baidu)
    run 2   Mancer 2, Baidu, Relace             (control: AtlasCloud)

Eight draws, **seven distinct provider names, one repeat.** An earlier probe had
already drawn Sail Research and SiliconFlow, so nine names in ten calls. Any
assertion naming a provider would have gone red on a healthy system inside a
single run. That width is also what makes the NO_GO legible: on a pool this
scattered, a preference that was silently ignored is indistinguishable from a
preference that lost a coin toss -- which is exactly why the served name has to
be READ rather than inferred from a 200.

The distribution is PRINTED instead, for a human. It is the number that tells you
whether a future `provider.order` did anything.

THE THREE MECHANISMS, AND WHICH ONE ACTUALLY WORKS. This was the probe, and the
answer is a finding rather than a detail:

  A. `response_metadata` from langchain-openai -- **DOES NOT CARRY IT.**
     `_create_chat_result` builds `llm_output` from a fixed whitelist
     (base.py:1873, langchain-openai 1.5.1): token_usage, model_provider,
     model_name, system_fingerprint, id, service_tier. OpenRouter's top-level
     `provider` is not in it and is discarded before any caller sees it.
     **`model_provider` is the decoy** -- it is the hard-coded string `"openai"`
     (base.py:1875), naming the client PROTOCOL, not the endpoint that served the
     tokens. Reading it and believing it is how this check gets written wrong.

  B. A direct httpx call -- **CARRIES IT**, as top-level `provider`. Run here as
     a CONTROL rather than as the mechanism: it is what separates "the gateway
     does not report the provider" from "the client throws it away", and only the
     second of those is fixable here. It also means a future langchain-openai
     that starts surfacing the field will be visible rather than assumed.

  C. `GET /api/v1/generation?id=<id>` -- **CARRIES IT**, as `provider_name`, plus
     `total_cost`, `native_tokens_cached`, `cache_discount` and per-endpoint
     `provider_responses`. This is the mechanism used for the three real calls,
     because `response_metadata["id"]` survives langchain's whitelist and is the
     join key. **It LAGS**: measured 2026-08-16, the record 404s for seconds
     after the call it describes has already returned -- 6 consecutive 404s at
     1.5 s apart before the first 200 on the worst observed case, 1 to 3 on a
     warm one. Same shape as Pinecone's `describe_index_stats` lagging a write,
     and the same remedy: poll, never read once. A single read would have
     concluded the endpoint does not carry the field at all.

WHAT IT MEASURES, AND ONE THING IT DOES NOT. The three generations go through
`app/rag/llm.build_chat_model` with the shipped sampling parameters, because
routing is decided by the parameter SET under `require_parameters` -- a harness
that builds its own request measures the routing of a request the app never
sends. Only `max_tokens` differs (16, to keep this cheap), and `max_tokens` is
advertised everywhere so it changes cost rather than eligibility. What is NOT
reproduced: `agent_loop.py` binds tools on all three of its model invocations, so
a real generation turn routes on a strictly narrower set than this file measures.
Read the distribution here as an upper bound on the eligible pool.

IT MAKES REAL, BILLED CALLS, so it is gated behind an explicit flag. With no
flag it prints the plan and exits 0.

    backend/.venv/Scripts/python.exe scripts/route_check.py            # plan only
    backend/.venv/Scripts/python.exe scripts/route_check.py --live     # 4 calls

Needs OPENROUTER_API_KEY. No database, no Pinecone, no writes.

ASCII in print(), exit 1 on any failure -- `new features/06-test-plan.md`.
"""

from __future__ import annotations

import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))

import httpx  # noqa: E402

# IMPORTED, not copied. `agentic_check.py` owns the rate-limit phrase list and
# its comment records that list being wrong once already, in the same way
# `app/rag/refusal.py`'s markers were wrong four times -- a substring test
# matching one spelling of a phrase. CLAUDE.md's remedy for that class of bug is
# structural (one list, many readers), so this file reads the sibling rather than
# starting a second copy that will drift.
from agentic_check import is_rate_limited  # noqa: E402

from app.config import settings  # noqa: E402
from app.rag.llm import build_chat_model, openrouter_slug  # noqa: E402

LIVE = "--live" in sys.argv

MODEL = settings.generation_model
SLUG = openrouter_slug(MODEL)

# Three DIFFERENT prompts, deliberately. Identical prompts invite a response
# cache to answer the second and third from the first, which would report one
# endpoint's name three times and look exactly like a successful pin.
PROMPTS = (
    "Reply with the single word: alpha",
    "Reply with the single word: bravo",
    "Reply with the single word: charlie",
)

# Small enough to be free in practice; the subject is the route, not the answer.
MAX_TOKENS = 16

# The generation record is written asynchronously behind the response. Measured
# 2026-08-16: up to 6 consecutive 404s at 1.5 s before the first 200 (1 to 3 on a
# warm lookup), so a ceiling of 12 leaves roughly 2x headroom on the worst case
# observed. A `[FAIL]` here means the served provider became unreachable, which is
# the one thing this file exists to notice, so the budget is set well above the
# measured lag rather than beside it.
LOOKUP_ATTEMPTS = 12
LOOKUP_INTERVAL_S = 1.5

failures: list[str] = []
rate_rows: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "[ok]  " if condition else "[FAIL]"
    print(f"{status} {name}" + (f" -- {detail}" if detail else ""))
    if not condition:
        failures.append(name)


def rate(name: str, detail: str = "") -> None:
    """The provider refused. Unmeasured -- never a defect, and never a pass.

    Same three-state convention as `agentic_check.py`: a red row that means "wait
    sixty seconds" sends its reader to debug working code.
    """
    print(f"[rate] {name}" + (f" -- {detail}" if detail else ""))
    rate_rows.append(name)


def ascii_safe(text: str) -> str:
    """Force text to ASCII for the Windows console.

    Provider names and error strings are DATA, not literals in this file, and
    CLAUDE.md records three throwaway scripts already broken by exactly that -- a
    cp1252 UnicodeEncodeError sourced in text the script never wrote.
    """
    return text.encode("ascii", "replace").decode("ascii")


print("=" * 74)
print("route_check.py -- what OpenRouter DID, not what this repo asked for")
print("=" * 74)
print(f"model     {ascii_safe(SLUG)}")
print(f"gateway   {settings.openrouter_base_url}")
print(f"require_parameters={settings.openrouter_require_parameters}")

if not LIVE:
    print("\n-- plan only; --live not passed, nothing was called --")
    print("With --live this would make 4 real, billed OpenRouter calls:")
    print(f"  3 generations through app.rag.llm.build_chat_model, max_tokens={MAX_TOKENS},")
    print("    with the shipped sampling parameters, then GET /generation?id=<id>")
    print("    per call to read back the SERVED provider (langchain drops it).")
    print("  1 direct httpx call carrying the identical extra_body, as the control")
    print("    that proves the gateway sends `provider` and the client discards it.")
    print("It asserts only that a served-provider name was RECOVERABLE.")
    print("It never asserts WHICH provider: allow_fallbacks means a fallback is")
    print("the system working, and the distribution is printed for a human.")
    print("\nRe-run with --live to measure.")
    sys.exit(0)

if not settings.openrouter_api_key:
    # An environment fault, not a defect -- but unlike a rate limit it will never
    # resolve by waiting, and exiting 0 would report a suite that measured
    # nothing as passing. Loud, and non-zero.
    print("\nERROR: OPENROUTER_API_KEY is not set. --live cannot measure anything.")
    sys.exit(1)

AUTH = {"Authorization": f"Bearer {settings.openrouter_api_key}"}


def lookup_generation(gen_id: str) -> tuple[dict | None, int, str]:
    """Mechanism C. Poll `GET /generation?id=` until the record exists.

    Returns (data, attempts_used, note). A 404 is "not written yet" rather than
    "no such generation", which is why only a non-404 error stops the loop.
    """
    for attempt in range(1, LOOKUP_ATTEMPTS + 1):
        try:
            resp = httpx.get(
                f"{settings.openrouter_base_url}/generation",
                params={"id": gen_id},
                headers=AUTH,
                timeout=30.0,
            )
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            return None, attempt, ascii_safe(f"{type(exc).__name__}: {exc}")[:200]
        if resp.status_code == 200:
            return (resp.json() or {}).get("data") or {}, attempt, ""
        if resp.status_code != 404:
            return None, attempt, ascii_safe(f"HTTP {resp.status_code} {resp.text}")[:200]
        time.sleep(LOOKUP_INTERVAL_S)
    return None, LOOKUP_ATTEMPTS, f"still 404 after {LOOKUP_ATTEMPTS} attempts"


# ---------------------------------------------------------------------------
# Mechanism A -- three real generations through the repo's own constructor.
# ---------------------------------------------------------------------------
print("\n-- mechanism A: three generations via build_chat_model --")

llm = build_chat_model(
    MODEL,
    temperature=settings.generation_temperature,
    top_p=settings.generation_top_p,
    top_k=settings.generation_top_k,
    max_tokens=MAX_TOKENS,
    reasoning=settings.generation_reasoning,
)

# The control below reuses THIS dict rather than rebuilding one by hand, so the
# two mechanisms cannot drift into measuring different requests.
SENT_EXTRA_BODY = dict(getattr(llm, "extra_body", None) or {})
print(f"extra_body sent: {ascii_safe(str(SENT_EXTRA_BODY))}")

records: list[dict] = []
for index, prompt in enumerate(PROMPTS, start=1):
    label = f"call {index}"
    try:
        message = llm.invoke(prompt)
    except Exception as exc:  # noqa: BLE001 - classified, not raised
        text = ascii_safe(f"{type(exc).__name__}: {exc}")
        if is_rate_limited(text):
            rate(label, text[:200])
        else:
            check(f"{label} completed", False, text[:200])
        continue

    meta = dict(message.response_metadata or {})
    usage = dict(meta.get("token_usage") or {})
    prompt_details = dict(usage.get("prompt_tokens_details") or {})
    records.append(
        {
            "label": label,
            "id": meta.get("id"),
            "meta_keys": sorted(meta),
            "model_provider": meta.get("model_provider"),
            "model_name": meta.get("model_name"),
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "cached_tokens": prompt_details.get("cached_tokens"),
            "cache_write_tokens": prompt_details.get("cache_write_tokens"),
            "cost": usage.get("cost"),
        }
    )

if not records:
    print("\nNo generation completed. Nothing could be measured.")
    if rate_rows:
        print(f"{len(rate_rows)} row(s) UNMEASURED -- the provider refused. Re-run in a minute.")
        sys.exit(0)
    sys.exit(1)

first = records[0]
print(f"response_metadata keys: {first['meta_keys']}")
print(
    "no key here names the SERVED provider -- `model_provider` is the hard-coded "
    f"string {first['model_provider']!r} (langchain-openai base.py:1875), which "
    "names the client PROTOCOL, not the endpoint that produced the tokens."
)

check(
    "1. every completed generation carries an id (mechanism C's only join key)",
    all(bool(r["id"]) for r in records),
    f"n={len(records)}, ids={[r['id'] for r in records]}",
)

# ---------------------------------------------------------------------------
# Mechanism B -- the control. Proves the field is on the wire.
# ---------------------------------------------------------------------------
print("\n-- mechanism B: direct call, same extra_body, as a control --")
direct_provider = None
control_refused = False
try:
    direct = httpx.post(
        f"{settings.openrouter_base_url}/chat/completions",
        headers={**AUTH, "Content-Type": "application/json"},
        json={
            "model": SLUG,
            "messages": [{"role": "user", "content": "Reply with the single word: delta"}],
            "temperature": settings.generation_temperature,
            "top_p": settings.generation_top_p,
            **SENT_EXTRA_BODY,
        },
        timeout=settings.openrouter_timeout_s,
    )
    if direct.status_code == 200:
        payload = direct.json() or {}
        direct_provider = payload.get("provider")
        direct_usage = dict(payload.get("usage") or {})
        direct_cached = dict(direct_usage.get("prompt_tokens_details") or {})
        print(f"top-level keys: {sorted(payload)}")
        print(
            f"provider={ascii_safe(str(direct_provider))} "
            f"cached_tokens={direct_cached.get('cached_tokens')} "
            f"cost={direct_usage.get('cost')}"
        )
    else:
        text = ascii_safe(f"HTTP {direct.status_code} {direct.text}")[:200]
        control_refused = is_rate_limited(text)
        if control_refused:
            rate("mechanism B control", text)
        else:
            check("mechanism B control call succeeded", False, text)
except Exception as exc:  # noqa: BLE001 - classified, not raised
    text = ascii_safe(f"{type(exc).__name__}: {exc}")[:200]
    control_refused = is_rate_limited(text)
    if control_refused:
        rate("mechanism B control", text)
    else:
        check("mechanism B control call succeeded", False, text)

# Skipped only when the control was REFUSED. A control that ran and returned no
# provider is the finding this case exists for, so it must still go red.
if not control_refused:
    check(
        "2. OpenRouter DOES send a top-level provider, so the drop is the client's",
        isinstance(direct_provider, str) and bool(direct_provider.strip()),
        f"provider={ascii_safe(str(direct_provider))}",
    )

# ---------------------------------------------------------------------------
# Mechanism C -- read the served provider back for each langchain call.
# ---------------------------------------------------------------------------
print("\n-- mechanism C: GET /generation?id= (it lags; this polls) --")
served: list[str] = []
for record in records:
    data, attempts, note = lookup_generation(str(record["id"]))
    if data is None:
        text = note
        if is_rate_limited(text):
            rate(f"{record['label']} lookup", text)
        else:
            check(f"{record['label']} lookup returned a record", False, text)
        continue
    name = data.get("provider_name") or ""
    record["served_provider"] = name
    record["native_tokens_cached"] = data.get("native_tokens_cached")
    record["cache_discount"] = data.get("cache_discount")
    record["total_cost"] = data.get("total_cost")
    record["attempts"] = attempts
    if isinstance(name, str) and name.strip():
        served.append(name)
    print(
        f"{record['label']}: provider={ascii_safe(str(name))} "
        f"(after {attempts} lookup attempt(s)) "
        f"native_tokens_cached={data.get('native_tokens_cached')} "
        f"cache_discount={data.get('cache_discount')} "
        f"total_cost={data.get('total_cost')}"
    )

check(
    "3. a served-provider name was recoverable for every completed generation",
    len(served) == len(records),
    f"{len(served)} of {len(records)} recovered",
)
check(
    "4. every recovered name is a non-empty string",
    all(isinstance(n, str) and n.strip() for n in served),
    f"names={[ascii_safe(n) for n in served]}",
)

# ---------------------------------------------------------------------------
# The cache counters. NOT asserted: they are a fact about the workload, not
# about this repo's code. They are printed because they are the only number that
# could ever justify revisiting the provider-pin NO_GO in `app/rag/llm.py` --
# the first-party DeepSeek endpoint costs 2.2x more per uncached token and only
# pays at a high implicit-hit rate, measured at exactly zero. Retrieved chunks
# sit INSIDE the prompt and change every query, so a stable cacheable prefix is
# a hypothesis rather than a property. If these ever stop reading 0, re-open it.
# ---------------------------------------------------------------------------
print("\n-- cache counters (printed, never asserted) --")
for record in records:
    print(
        f"{record['label']}: prompt_tokens={record['prompt_tokens']} "
        f"cached_tokens={record['cached_tokens']} "
        f"cache_write_tokens={record['cache_write_tokens']} "
        f"cost={record['cost']}"
    )

# ---------------------------------------------------------------------------
# The distribution, and the mechanism report. Both are findings for a human.
# ---------------------------------------------------------------------------
print("\n-- served-provider distribution (PRINTED, deliberately not asserted) --")
distribution = Counter(ascii_safe(n) for n in served)
for name, count in distribution.most_common():
    print(f"  {count} / {len(served)}  {name}")
if direct_provider:
    print(f"  (control, direct call: {ascii_safe(str(direct_provider))})")
print(
    "A different name between runs is allow_fallbacks working. Asserting one "
    "would go red on a healthy system and teach its reader to ignore red."
)

print("\n-- which mechanism recovered the served provider --")
print("  A  response_metadata (langchain-openai)  : NO  -- dropped by llm_output whitelist")
print(f"  B  direct httpx call, top-level provider : {'YES' if direct_provider else 'NO'}")
print(f"  C  GET /generation?id=                   : {'YES' if served else 'NO'}")
print("  C is the mechanism this file uses, because it is the only one that can")
print("  name the provider for a call the APPLICATION made rather than one the")
print("  harness made for itself.")

print("\n" + "=" * 74)
if failures:
    print(f"{len(failures)} FAILED:")
    for name in failures:
        print(f"  - {name}")
    sys.exit(1)
if rate_rows:
    print(f"{len(rate_rows)} row(s) UNMEASURED -- the provider refused, not a defect.")
    print("Treat them as unmeasured, never as passing, and re-run in a minute.")
print("all checks passed")
