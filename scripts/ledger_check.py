"""Standalone check of the ContextLedger marker contract. No database, no API.

`ContextLedger` owns the `[n]` numbering for a whole turn, and three invariants
hang off it. If any one of them breaks, the answer still renders and every
citation in it points at the wrong source -- the worst failure shape in this
codebase, because it still looks like provenance.

    1. A chunk retrieved twice keeps ONE marker, not two.
    2. `merge` returns markers for EXISTING entries too, so a tool result can
       say "these are chunks [2] and [5]" rather than presenting old material as
       new. Without it the model searches in circles and the citation list
       disagrees with the answer text.
    3. Marker order IS list order, because `ask.run_turn` builds
       `AskOut.citations` by enumerating `AnswerResult.documents` from 1.

Six cases, all synthetic -- a fake `Retrieval` costs nothing and this is the one
piece of the agent loop whose correctness does not need a model.

Usage:
    backend/.venv/Scripts/python.exe scripts/ledger_check.py

Exits 1 if anything fails.

ASCII only in print(). The Windows console codepage mangles em-dashes and
section signs, and it has broken three throwaway scripts in this repo already.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from langchain_core.documents import Document  # noqa: E402

from app.rag.agent_loop import ContextLedger  # noqa: E402
from app.rag.retriever import (  # noqa: E402
    META_CHUNK_ID,
    META_CHUNK_INDEX,
    META_FILENAME,
    RERANK_SCORE_KEY,
    Retrieval,
)

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" -- {detail}" if detail else ""))
    if not condition:
        failures.append(name)


def doc(chunk_id: str, *, filename: str = "power.md", index: int = 0,
        text: str | None = None, rerank: float | None = None) -> Document:
    metadata = {
        META_CHUNK_ID: chunk_id,
        META_FILENAME: filename,
        META_CHUNK_INDEX: index,
    }
    if rerank is not None:
        metadata[RERANK_SCORE_KEY] = rerank
    return Document(page_content=text or f"body of {chunk_id}", metadata=metadata)


def retrieval(*pairs: tuple[Document, float], reranked: bool = False) -> Retrieval:
    """A `Retrieval` whose `documents` is its `scored` list, in order."""
    return Retrieval(
        query="q",
        documents=[d for d, _ in pairs],
        scored=list(pairs),
        reranked=reranked,
    )


a, b, c, d = (doc(f"1111111{i}") for i in range(4))

# ---------------------------------------------------------------- case 1
# Seeding assigns 1..N in retrieval order.
ledger = ContextLedger.seed(retrieval((a, 0.71), (b, 0.64), (c, 0.58)))
check(
    "1. seed assigns contiguous 1-based markers in order",
    [e.marker for e in ledger.entries] == [1, 2, 3]
    and ledger.documents == [a, b, c],
    f"markers={[e.marker for e in ledger.entries]}",
)

# ---------------------------------------------------------------- case 2
# The same chunk merged again keeps its marker and adds no entry.
markers = ledger.merge(retrieval((b, 0.66), (a, 0.61)))
check(
    "2. a chunk merged twice keeps ONE marker",
    len(ledger) == 3,
    f"len={len(ledger)}",
)
check(
    "3. merge returns markers for EXISTING entries, in retrieval order",
    markers == [2, 1],
    f"markers={markers}",
)

# ---------------------------------------------------------------- case 4
# A genuinely new chunk is numbered after everything already held, and mixed
# results come back in the order the search returned them.
markers = ledger.merge(retrieval((d, 0.69), (a, 0.55)))
check(
    "4. a new chunk continues the numbering; old ones keep theirs",
    markers == [4, 1] and len(ledger) == 4,
    f"markers={markers}, len={len(ledger)}",
)

# ---------------------------------------------------------------- case 5
# Marker order IS list order. This is what ask.run_turn enumerates from 1.
check(
    "5. marker order == documents order",
    [e.marker for e in ledger.entries] == list(range(1, len(ledger) + 1))
    and ledger.documents == [a, b, c, d],
)

# ---------------------------------------------------------------- case 6
# Identity is `chunk_id`, not object identity: a re-retrieved chunk arrives as a
# NEW Document object every time (Pinecone rebuilds it, and CohereRerank
# deepcopies the metadata), so an identity-based ledger would double every
# marker on the second search and nothing downstream would notice.
same_id_new_object = doc("11111110", text="rebuilt by pinecone")
markers = ledger.merge(retrieval((same_id_new_object, 0.72),))
check(
    "6. dedup is on chunk_id metadata, not object identity",
    markers == [1] and len(ledger) == 4,
    f"markers={markers}, len={len(ledger)}",
)

# ---------------------------------------------------------------- case 7
# Similarity comes from `scored` (pre-rerank) and is joined by chunk id, never by
# index -- reranking reorders and truncates, so position i of `documents` is not
# position i of `scored` once it has run.
reranked = Retrieval(
    query="q",
    documents=[doc("22222221", index=9, rerank=0.93)],
    scored=[(doc("22222220", index=8), 0.80), (doc("22222221", index=9), 0.42)],
    reranked=True,
)
fresh = ContextLedger.seed(reranked)
entry = fresh.entries[0]
check(
    "7. similarity is joined by chunk id across the rerank reorder",
    entry.similarity == 0.42 and entry.rerank == 0.93,
    f"similarity={entry.similarity}, rerank={entry.rerank}",
)

# ---------------------------------------------------------------- case 8
# A vector with no chunk_id still gets a marker (it can ground an answer even
# though ask._chunk_uuid drops it from the citation list) and still dedupes, on
# a hash of its own text.
anon = Document(page_content="orphan chunk", metadata={})
anon_again = Document(page_content="orphan chunk", metadata={})
orphan = ContextLedger.seed(retrieval((anon, 0.5)))
markers = orphan.merge(retrieval((anon_again, 0.5),))
check(
    "8. a chunk with no chunk_id dedupes on its text",
    markers == [1] and len(orphan) == 1,
    f"markers={markers}, len={len(orphan)}",
)

# ---------------------------------------------------------------- case 9
# format_context renders "[n] filename#chunk_index" through pipeline's renderer.
rendered = ledger.format_context()
check(
    "9. format_context numbers blocks with the ledger's markers",
    rendered.startswith("[1] power.md#0\n") and "[4] power.md#0" in rendered,
    f"first line={rendered.splitlines()[0]!r}",
)

# The classic path must be untouched: no markers argument, no numbering.
from app.rag.pipeline import format_context  # noqa: E402

plain = format_context([a, b])
check(
    "10. pipeline.format_context is unchanged without markers",
    plain == "[power.md]\nbody of 11111110\n\n---\n\n[power.md]\nbody of 11111111",
    repr(plain[:40]),
)

print()
if failures:
    print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
    sys.exit(1)
print("All ledger checks passed.")
