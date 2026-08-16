"""Layer 1.5 harness for `app/rag/retriever.get_embeddings`. Network, no DB.

WHY THIS FILE EXISTS.

Moving embeddings from `langchain-google-genai` to OpenRouter changed the ROAD
and not the SPACE: same model, same 768 dimensions, same vectors already sitting
in Pinecone. That claim is the entire justification for shipping the swap without
a re-ingest, and it is the one claim in this subsystem that cannot report its own
failure. CLAUDE.md states the shape flatly -- "indexing with one model and
querying with another returns confident nonsense rather than an error, because
matching dimensions do not imply a shared vector space." A wrong route does not
raise. It retrieves, ranks badly, and every downstream check stays green.

So this harness asserts the OUTCOME (the two routes land on one vector) rather
than the absence of an error, per `new features/loop.md` T2. Nothing here passes
because a call succeeded.

WHAT EACH CASE COST, AND WHY NONE OF THEM IS DECORATIVE.

`get_embeddings` now carries four kwargs on the OpenRouter branch that all read
like style and are all a different 400, each visible only after the previous one
is fixed: `encoding_format` (openai-python injects base64 unasked),
`check_embedding_ctx_length=False` (the default tiktoken-encodes and sends arrays
of integers), `chunk_size` (a hard 100-input provider ceiling) and `dimensions`
(omit it and 3072 comes back). Cases 5 and 6 are the two that keep the silent
ones honest:

  * Case 5 sends 101 texts. It is the ONLY case in this file that can catch a
    missing `chunk_size`, because 25 or 100 texts fit in one request, succeed,
    and certify a broken configuration. `app/rag/ingest.py` hands a whole
    document's chunks to `store.add_texts` in one call, so the first document
    over 100 chunks is where a passing 25-text probe would have been paid for.
  * Case 6 asserts 768 and an L2 norm of 1.0. The first half catches a dropped
    `dimensions`; the second catches anyone re-adding the manual normalisation
    that `gemini-embedding-001` needed and `gemini-embedding-2` does not, which
    would double-normalise and degrade cosine silently.

Case 2 exists because `embed_query` is a genuinely separate code path on the
Google side -- langchain-google-genai 4.3.4 injects `RETRIEVAL_QUERY` there and
`RETRIEVAL_DOCUMENT` on documents (embeddings.py:420, :486) while the OpenRouter
route sends neither. Probing only one shape would leave a query/document space
split undetected, and that split is exactly the confident-nonsense failure above.
Case 3 is the control: it proves 1.000000 is not something this metric hands out
for free.

Both routes are exercised through the production `get_embeddings` rather than by
constructing clients here. A harness that builds its own client certifies a
configuration the application does not use.

    backend/.venv/Scripts/python.exe scripts/embed_check.py

Needs OPENROUTER_API_KEY, and makes roughly 115 embedding calls' worth of input
across six requests. No database, no Pinecone, no writes.

GEMINI_API_KEY IS NO LONGER REQUIRED TO RUN, AND ITS ABSENCE IS REPORTED RATHER
THAN RAISED. The swap made that key optional -- `embedding_route` defaults to
"openrouter" and Google is the rollback -- so this harness used to be unrunnable
in precisely the configuration it recommends: it constructed the Google embedder
unconditionally and died with a traceback out of `GoogleGenerativeAIEmbeddings`,
which reads as a broken harness rather than as a missing key.

Without that key the Google arm is skipped and case 0 goes **[FAIL]**, not
`[skip]`. That is deliberate and it is the T2 rule again: cases 1-3 ARE the
premise -- one space, two roads -- so a run that cannot compare the two routes
has not verified the claim this file exists for, and a green run would assert
that it had. Cases 4-6 are OpenRouter-only and still run and still mean what
they say, so a keyless run is a partial measurement that says so out loud.

Nothing here executes on import: everything is inside `main()`, so importing
this module neither spends embedding calls nor mutates `settings`. It mutates
`settings.embedding_route` while running (that is the only way to see both
branches of an `lru_cache`d constructor in one process) and restores it in a
`finally`.

ASCII in print(), exit 1 on any failure -- `new features/06-test-plan.md`.
"""

from __future__ import annotations

import asyncio
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from langchain_core.embeddings import Embeddings  # noqa: E402

from app.config import settings  # noqa: E402
from app.rag.retriever import get_embeddings  # noqa: E402

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "[ok]  " if condition else "[FAIL]"
    print(f"{status} {name}" + (f" -- {detail}" if detail else ""))
    if not condition:
        failures.append(name)


def ascii_safe(text: str) -> str:
    """Force text to ASCII for the Windows console.

    Provider error strings are data, not literals, and CLAUDE.md records three
    throwaway scripts already broken by exactly that -- a cp1252 UnicodeEncodeError
    sourced in text this file never wrote.
    """
    return text.encode("ascii", "replace").decode("ascii")


def embedder(route: str) -> Embeddings:
    """The PRODUCTION constructor, once per route.

    `settings.embedding_route` is toggled and the cache cleared rather than
    building an `OpenAIEmbeddings` here by hand, because a harness that
    constructs its own client proves that OpenRouter can embed and proves
    nothing about what the application sends. `get_embeddings` is
    `@lru_cache(maxsize=1)`, so `cache_clear()` is the only way to see both
    branches inside one process.
    """
    settings.embedding_route = route
    get_embeddings.cache_clear()
    return get_embeddings()


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity, computed here rather than trusted from a library.

    Deliberately NOT a dot product: these vectors are expected to be unit-norm
    and case 6 is what establishes that. Assuming it in the metric would make
    case 6 unable to fail.
    """
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb)


def l2(v: list[float]) -> float:
    return math.sqrt(sum(x * x for x in v))


# Three strings with no shared vocabulary, so the case-3 control has room to be
# a real control. The corpus subject matter is irrelevant -- what matters is that
# TEXTS[0] and TEXTS[1] are about genuinely different things.
TEXTS = [
    "The solar array generates 4.2 kW at end of life.",
    "Handover weeks expand the permanent crew complement to nineteen.",
    "Collision avoidance triggers at a probability of one in ten thousand.",
]

# 101, not 100 and not 25. One over the provider's hard batch ceiling is the
# smallest input that can distinguish a configured `chunk_size` from a missing
# one; anything at or below 100 succeeds either way.
BATCH_N = 101
BATCH = [f"Telemetry frame {i} reports nominal subsystem status." for i in range(BATCH_N)]

def main() -> int:
    """Every probe, every assertion, and the settings restore.

    **A function rather than module scope, and that is a correctness fix rather
    than a tidy-up.** At module scope `import embed_check` -- from a future
    harness, a REPL, or anything that wanted `cosine` or `TEXTS` -- spent ~115
    embedding calls and, worse, left `settings.embedding_route` mutated for the
    rest of that process. The `finally` below restored it only because the whole
    file was one try block; an importer that raised anywhere else in its own code
    would still have inherited the last route probed. A harness that changes the
    configuration of the program importing it is the same class of silent fault
    this file exists to catch.
    """
    print("=" * 74)
    print("get_embeddings -- two routes, one space")
    print("=" * 74)
    print(f"  model (space)  : {settings.embedding_model} @ {settings.embedding_dimension}d")
    print(f"  openrouter slug: {settings.openrouter_embedding_model}")
    print(f"  batch ceiling  : {settings.embedding_batch_size} inputs per request")
    print(f"  configured     : embedding_route={settings.embedding_route}")
    print(f"  gemini key     : {'present' if settings.gemini_api_key else 'ABSENT'}")

    live_route = settings.embedding_route

    # **The rollback arm is conditional now, because its key is.** `gemini_api_key`
    # became optional when "openrouter" became the default route, so the shipped
    # configuration is one in which `GoogleGenerativeAIEmbeddings` cannot be
    # constructed. It used to be built unconditionally, one line below the banner
    # and outside every `check()`, so the recommended configuration produced a raw
    # traceback instead of a result line -- the harness proving the swap could not
    # run in the state the swap recommends.
    google_available = bool(settings.gemini_api_key)

    try:
        openrouter = embedder("openrouter")
        google = embedder("google") if google_available else None

        o_docs = openrouter.embed_documents(TEXTS)
        o_query = openrouter.embed_query(TEXTS[0])
        o_aquery = asyncio.run(openrouter.aembed_query(TEXTS[0]))

        g_docs: list[list[float]] = []
        g_query: list[float] = []
        if google is not None:
            g_docs = google.embed_documents(TEXTS)
            g_query = google.embed_query(TEXTS[0])

        # -----------------------------------------------------------------------
        # 0. Can the premise be measured at all?
        # -----------------------------------------------------------------------
        # **[FAIL] and not [skip], on purpose.** Cases 1-3 are the entire claim of
        # this file -- two roads, one space -- and without the Google key they do
        # not run. A skipped premise that exits 0 asserts the premise held, which
        # is `new features/loop.md` T2 arriving in the harness rather than in the
        # feature: green on the ABSENCE of evidence. The rest of the suite still
        # runs and still means what it says, so this reads as "partially measured"
        # rather than as "broken".
        #
        # This is NOT the `[rate]` case CLAUDE.md argues should stay green. A rate
        # limit means "the thing was measurable and the provider said wait"; this
        # means "the thing was never measured".
        check(
            "0. both routes are constructible (the rollback needs GEMINI_API_KEY)",
            google_available,
            "present"
            if google_available
            else "GEMINI_API_KEY is empty, so the google rollback route cannot be "
            "verified -- cases 1-3, which ARE the one-space premise, did not run. "
            "Set it to measure them; cases 4-6 below are OpenRouter-only and did.",
        )

        # -----------------------------------------------------------------------
        # 1-2. The premise, on both call shapes.
        # -----------------------------------------------------------------------
        if google is not None:
            print("\n-- the premise: same vector out of both gateways --")
            doc_cosines = [cosine(g, o) for g, o in zip(g_docs, o_docs)]
            for i, c in enumerate(doc_cosines):
                check(
                    f"1.{i + 1} embed_documents agrees on string {i + 1}",
                    c >= 0.9999,
                    f"cosine {c:.6f}",
                )

            c_query = cosine(g_query, o_query)
            check(
                "2. embed_query agrees (Google injects RETRIEVAL_QUERY here, "
                "OpenRouter does not)",
                c_query >= 0.9999,
                f"cosine {c_query:.6f}",
            )

            # -------------------------------------------------------------------
            # 3. The control. Without it, case 1 could be measuring a constant.
            # -------------------------------------------------------------------
            print("\n-- the control: 1.000000 is not free on this metric --")
            c_control = cosine(o_docs[0], g_docs[1])
            check(
                "3. different strings, opposite routes, are NOT the same vector",
                c_control < 0.9,
                f"cosine {c_control:.6f}",
            )
        else:
            print("\n-- the premise: NOT MEASURED, no GEMINI_API_KEY (see case 0) --")

        # -----------------------------------------------------------------------
        # 4. Async is a separate code path and it is the production hot path.
        # -----------------------------------------------------------------------
        print("\n-- async, which retriever.aretrieve actually calls --")
        c_async = cosine(o_query, o_aquery)
        check(
            "4. aembed_query matches embed_query (base.py:778-787 is its own path)",
            c_async >= 0.9999,
            f"cosine {c_async:.6f}",
        )

        # -----------------------------------------------------------------------
        # 5. THE ONE THAT SMALL PROBES CANNOT CATCH. Do not soften this to 100.
        # -----------------------------------------------------------------------
        print("\n-- the batch ceiling: 101 inputs, one over the provider's hard limit --")
        batch_error = ""
        o_batch: list[list[float]] = []
        try:
            o_batch = openrouter.embed_documents(BATCH)
        except Exception as exc:  # a 400 here IS the failure -- report it as case 5
            batch_error = f"{type(exc).__name__}: {ascii_safe(str(exc))[:180]}"
        check(
            f"5. {BATCH_N} texts return {BATCH_N} vectors (catches a missing chunk_size)",
            len(o_batch) == BATCH_N,
            batch_error or f"got {len(o_batch)} vectors",
        )

        # -----------------------------------------------------------------------
        # 6. Shape and norm, over every vector this run produced.
        # -----------------------------------------------------------------------
        print("\n-- shape and norm: a dropped `dimensions` returns 3072 --")
        # Every vector this run produced, both routes and both call shapes, plus
        # the 101-text batch -- so a per-request dimension drift cannot hide behind
        # an average or behind a spot check of the first vector. The Google
        # vectors are simply absent when that route was not built; this case is
        # about the OpenRouter request shape either way.
        all_vectors = [
            *g_docs,
            *o_docs,
            *([g_query] if google is not None else []),
            o_query,
            o_aquery,
            *o_batch,
        ]
        wrong_dim = [
            len(v) for v in all_vectors if len(v) != settings.embedding_dimension
        ]
        check(
            f"6a. every vector is {settings.embedding_dimension} long",
            not wrong_dim,
            f"{len(all_vectors)} vectors checked"
            if not wrong_dim
            else f"{len(wrong_dim)} wrong, e.g. {wrong_dim[0]}d",
        )
        norms = [l2(v) for v in all_vectors]
        off = [n for n in norms if abs(n - 1.0) > 1e-3]
        check(
            "6b. every vector is L2-normalised (re-added manual normalisation "
            "shows up here)",
            not off,
            f"min {min(norms):.6f} max {max(norms):.6f}" if norms else "no vectors",
        )
    finally:
        # Leave the process's settings as the .env found them. This script mutates
        # a setting to see both branches, and anything imported after it -- or a
        # future caller that imports this module -- must not inherit the last route
        # probed.
        settings.embedding_route = live_route
        get_embeddings.cache_clear()

    print("\n" + "=" * 74)
    if failures:
        print(f"{len(failures)} FAILED:")
        for name in failures:
            print(f"  - {name}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
