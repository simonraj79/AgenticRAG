"""The model's own machinery, kept off the user's screen. One owner, two runtimes.

MOVED here from `agent_loop.py` on 2026-08-28, unchanged, because a second runtime
(`app/adk/`) needs the identical guard and CLAUDE.md's standing rule is that **a
contract stated twice drifts, and the copy that drifted is never the one you are
reading**. `agent_loop.py` re-exports these three names, so every existing import
and `scripts/refusal_check.py` cases 27-33 keep resolving.

------------------------------------------------------------------
WHY THIS EXISTS AT ALL, AND WHY IT IS NOT PARANOIA.

Measured 2026-08-16 in the browser, `deepseek/deepseek-v4-flash-0731`, on a turn
that spent its whole step budget. When the budget runs out the loop re-invokes
with `tool_choice="none"` -- and this model still WANTED to search, so it
expressed that the only way left to it:

    Let me try searching for the thermal rejection budget document ...
    <|DSML|tool_calls> <|DSML|invoke name="search_corpus"> ...

(with U+FF5C FULLWIDTH VERTICAL LINE, not ASCII `|` -- DeepSeek's special-token
delimiter, which is exactly why it survives every provider-side parser that looks
for the ASCII form.)

**Nothing raised. The turn succeeded. The user got machinery in their answer.**
`new features/loop.md` T2 in a place nobody had looked: the assertion "did the
turn produce an answer" was true, and `agentic_check.py` S6 asserts precisely
that -- `bool(out.answer)` -- so the suite stayed green through it.

Truncating from the first sentinel rather than excising a matched block is
deliberate. The markup is a *continuation* the model never finished, so there is
no reliable closing token to match, and anything after the sentinel is machinery
by construction. Prose before it is kept, because it usually reads as a normal
closing sentence.

Scoped to the sentinel rather than to `<` so that a legitimate answer discussing
HTML or XML is untouched: U+FF5C does not appear in English prose, and does not
appear in this project's corpora at all.
------------------------------------------------------------------
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger("uvicorn.error")

# U+FF5C FULLWIDTH VERTICAL LINE. Do not "normalise" this to an ASCII pipe: the
# whole reason the sentinel reaches the content channel is that it is NOT the
# ASCII form, so a well-meaning cleanup here disarms the guard silently.
_LEAKED_TOOL_MARKUP = re.compile("<｜")


def _emit_until_markup(piece: str, emitted: list[str]) -> bool:
    """Whether this streamed piece may still go to the screen.

    False once leaked markup has appeared, and false for every piece after it --
    a stream cannot be un-read, so the gate latches rather than filtering.

    **Checks the JOIN of everything so far, not the piece.** `<｜` is two
    characters and a stream splits wherever the provider's buffer happened to
    end, so a sentinel arriving as `"<"` then `"｜"` is invisible to any per-chunk
    test. That is the same class of bug as `sentences()` in `refusal.py` refusing
    to split on a lone newline: generated text is chunked by the transport, and a
    matcher that assumes the transport's boundaries are meaningful will miss on a
    schedule nobody can reproduce.

    The cost of the join is one string build per token on a path that is already
    doing an HTTP write per token.

    Residual, stated rather than hidden: if a chunk ends exactly on the `<`, that
    one character has already been written and cannot be recalled -- only the
    `｜` and everything after it is suppressed. Holding a one-character tail back
    on every token would fix it and would delay every legitimate answer's last
    character behind the next chunk. A stray `<` is the cheaper defect.
    """
    if _LEAKED_TOOL_MARKUP.search("".join(emitted)):
        return False
    return not _LEAKED_TOOL_MARKUP.search("".join(emitted) + piece)


def _strip_leaked_tool_markup(text: str) -> str:
    """Text up to the first leaked special-token sentinel.

    Returns the input unchanged when there is none, which is every turn on a
    model whose markup the provider parses properly -- including every Gemma turn
    this project ever took.
    """
    match = _LEAKED_TOOL_MARKUP.search(text)
    if match is None:
        return text
    log.warning(
        "Stripped leaked tool-call markup from a model answer (%d chars removed).",
        len(text) - match.start(),
    )
    return text[: match.start()].rstrip()


__all__ = [
    "_LEAKED_TOOL_MARKUP",
    "_emit_until_markup",
    "_strip_leaked_tool_markup",
]
