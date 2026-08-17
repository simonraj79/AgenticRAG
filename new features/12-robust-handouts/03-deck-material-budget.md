# 03 — The deck material budget

A retrieval change, not a prompt change. It is in this folder because it is the reason decks are
thin, and no amount of validation fixes a deck that never had the material.

---

## What the user gets

A deck built from enough of the corpus to actually have five to eight slides in it. Today
`DECK_PROMPT` asks for *"five to eight slides, one idea per slide"* and the pipeline hands the
model **three chunks**.

---

## The defect

`PLAN.md` §1.6a. `gather_material` is one unoverridden call (`recipes.py:552`):

```python
    retrieval = await aretrieve(agent, brief)
```

with `agent.retrieve_k = 20` compressed by the reranker to `agent.rerank_top_n = 3`
(`models.py:127-129`). All four recipes take that identical budget.

The reason this is invisible is the honest-shrink rule the deck prompt correctly carries
(`recipes.py:233-234`): told to use only what the material supports, the model produces a
**four-slide deck that looks correct**. Retrieval starvation is wearing a prompt defect's
clothes, and feature 02's validator would read it as an honest shrink — correctly, because from
the artefact alone it *is* one.

This is why 02 and 03 are separate features and why 03 cannot be replaced by lowering
`handout_deck_min_slides`.

---

## Technical detail

The override already exists and is already used elsewhere for exactly this reason
(`retriever.py:278-303`):

```python
async def aretrieve(agent, question, *, k: int | None = None, top_n: int | None = None)
```

`pipeline.py:986-992` passes per-call values expressly so nothing writes them onto the agent row.
Same pattern here.

### A. `Recipe` gains two optional fields

`PLAN.md` §3.2 owns the contract. `None` on both is the identity case, so the three non-deck
recipes are untouched **by construction**, not by care.

### B. `gather_material` threads them

```python
    retrieval = await aretrieve(
        agent, brief, k=recipe.retrieve_k, top_n=recipe.rerank_top_n
    )
```

`gather_material`'s signature gains `recipe: Recipe`. Its two call sites are in `jobs.py`.

### C. The numbers — set from measurement, not instinct

**Do not pick these before feature 01 §G has produced the deck first-attempt rate and slide-count
distribution.** The plan's placeholder reasoning, to be confirmed or replaced:

- `deck`: `retrieve_k=40, rerank_top_n=10`. Eight slides at one idea each need roughly eight
  distinct pieces of material. `MAX_CONTEXT_CHARS = 12_000` (`recipes.py:69`) currently almost
  never binds at 3 chunks; at 10 it starts to, which is the backstop doing its job.
- `table`: likely also benefits — a table of three rows is the same defect.
- `chart`, `sheet`: leave `None`. A chart wants a few numbers, not a wide sweep.

**Check `MAX_CONTEXT_CHARS` against the new width before shipping.** If 10 chunks routinely
truncate, the deck is being starved a second way and the cap is the thing to move.

### D. Two costs, both real

- **Latency.** Reranking is ~830 ms and is unchanged by `top_n`; `retrieve_k` widening costs
  Pinecone time (~394 ms at k=20). The larger cost is **prompt length** — more context is more
  input tokens on a call that is already 89% of the turn.
- **Cohere.** `agentic_check.py` makes ~20 rerank calls per suite run and each recipe retrieves.
  The key is now a production key (12/12 rapid calls, no 429), so this should not bite — **but if
  `[rate]` rows reappear, suspect the key before the code.**

---

## What this does NOT do

**It does not run the brief through the question rewriter.** `gather_material` never calls
`contextualize_question` (`pipeline.py:494`), so a typo or shorthand in a brief reaches the
embedder unrepaired — which is precisely the failure `REWRITE_EVERY_TURN=true` exists to prevent
for questions. Wiring it is a few lines. **A brief is not a question**, the rewriter's prompt is
measured on questions (`rewrite_check.py`), and its failure mode is silent —
`contextualize_question` swallows every exception and degrades to Stage 1. It needs its own
measurement, so it needs its own feature file. `PLAN.md` §7 records it as a new PRD open item
rather than building it.

**It does not give a handout a second retrieval or a search tool.** One namespace per agent,
`SearchCorpusArgs` with one field, and the retriever constructed in exactly one place —
`PLAN.md` §1.8. A budget is not a second retriever.

---

## Contracts consumed

By reference into [`PLAN.md`](PLAN.md): §3.2 (the two `Recipe` fields), §3.3 (no migration — these
are code, not configuration, and not per-agent columns), §3.6 (R-b: the rendered prompt must be
byte-identical, because this feature changes *material* and must not change *wording*), §5 R8
(arm order in any before/after measurement).

---

## Acceptance criteria

| # | Criterion |
|---|---|
| **A1** | `scripts/deck_check.py` **case 30**: `RECIPES["chart"].retrieve_k is None and RECIPES["chart"].rerank_top_n is None` — the identity case for the three untouched recipes, asserted rather than assumed |
| **A2** | `scripts/deck_check.py` **case 31**: `render(RECIPES["deck"], brief=..., material=...)` is **byte-identical** to the committed fixture string — `PLAN.md` §3.6 **R-b**. This feature changes material, never wording |
| **A3** | `scripts/deck_check.py` **case 32**: a `Material` built from 10 chunks serialises under `MAX_CONTEXT_CHARS`, and one built from 10 deliberately-long chunks is truncated at the cap rather than silently over-running |
| **A4** | `scripts/agentic_check.py` **S29** (new): with the fixture agent's `rerank_top_n` forced to **1** — the scenario owns it and restores in a `finally` — a deck request still produces `>= handout_deck_min_slides` slides **or** ends `failed` with a material-shaped error. Never a silently-thin `ready` deck |
| **A5** | `scripts/agentic_check.py` **S30** (new): the RETRIEVE trace payload for a deck job records the **effective** `k`/`top_n`, and they are the recipe's, not the agent's. Without this, A4 could pass with the override silently ignored |
| **A6** | Measured, recorded in `PLAN.md` §8: deck slide-count distribution at `rerank_top_n=3` versus the new value, n ≥ 6 per arm, **arm order alternated** (R8) |

**A4 is the one that makes the feature necessary**, and it is written the `loop.md` §5 way — the
scenario **starves retrieval itself** rather than relying on the fixture's defaults happening to
be hostile enough. That is the exact lesson S3 taught by passing twice while proving nothing.

**A5 exists because A4 alone is satisfiable by an override that never took effect.** A deck that
clears the floor because the corpus is easy proves nothing about whether `k` moved.

---

## What must keep working

- **`PLAN.md` §3.6 R-b, asserted as A2.** The prompt text does not change in this feature.
- **The retriever is still constructed in exactly one place** (`retriever.py`, `CLAUDE.md:170-172`).
  This feature passes arguments to `aretrieve`; it does not call `similarity_search()` and does not
  add a second retrieval path. A handout retrieving *differently* from an answer would make the two
  disagree about what the corpus says; retrieving *more* does not.
- **`chart`, `sheet` and `table` behave exactly as today** unless their fields are set — A1.
- `Material.is_empty` still refuses (`recipes.py:440-450`). A wider budget must not turn "no
  material" into "a little material".
- `meta["chunk_ids"]` still records what the model was **allowed** to see, explicitly not what it
  used (`recipes.py:548-550`). A wider budget makes that distinction more important, not less.
- The `agentic_check.py` fixture's hostile defaults (`chunk_size=250, retrieve_k=3,
  rerank_top_n=2`, `:206-243`) are **not** relaxed to make this pass. S29 owns its own conditions.
