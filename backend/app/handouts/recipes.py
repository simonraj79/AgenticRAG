"""The four recipes, their prompts, and the material a handout is grounded in.

`new features/04-handouts-panel.md` sections 2.3 and 2.4.

------------------------------------------------------------------
WHY THE PROMPTS ARE SHAPED LIKE `app/db/personas.py`.

Every prompt below states the grounding rule FIRST, before the format, before
the voice, before anything about matplotlib. That ordering is copied from
`personas.py` deliberately and for the reason that module gives at length: a
confident, well-formatted output reads as more trustworthy than a blunt refusal,
and a *file* is the worst offender in the whole product. A fabricated paragraph
invites scepticism; a fabricated bar chart with labelled axes and a units
annotation looks like somebody measured something. A slide deck implies a
reviewer. A CSV implies a source.

So a handout is the one place this system could hallucinate with the most
authority and the least friction, and the prompts are written against exactly
that: never invent a figure, produce the smaller honest artefact, and say what
the material does not cover.
------------------------------------------------------------------

THE `sheet` RECIPE DOES NOT USE THE SANDBOX, AND THAT IS THE POINT.

Three reasons, in the order they matter:

1. It is the recipe most people will press. A study sheet is what a learner
   actually wants out of a lecture corpus; a chart is what a demo wants.
2. It is by far the cheapest -- one model call, no subprocess, no matplotlib
   import, no harvest. Seconds rather than tens of seconds.
3. **The panel still does something useful when the sandbox does not work.**
   matplotlib and python-pptx are real installs that fail on fresh machines and
   on constrained hosts; `run_python` is a subprocess spawn that a locked-down
   environment can refuse outright. If all four recipes went through the
   sandbox, one bad install would turn the whole feature into four buttons that
   all fail the same way.

Its markdown lands in BOTH `content` (as UTF-8 bytes, so the download route has
something to send and the file behaves like every other handout) and
`preview_text` (so the panel renders it inline without a second request). Two
copies of the same string, on purpose: `content` is deferred and the panel must
never load it to show a preview.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Agent
from app.db.models import Query as QueryRow
from app.rag.pipeline import format_context
from app.rag.retriever import META_CHUNK_ID, aretrieve

# --------------------------------------------------------------------------
# Caps on what reaches the prompt
# --------------------------------------------------------------------------
#
# A handout prompt carries the retrieved corpus AND the recent thread, so it is
# the largest prompt this project builds -- larger than a generation turn, which
# carries only the first. These caps exist so that "make a deck about the whole
# module" cannot quietly become a request that costs more than it can spend, and
# they are characters rather than tokens because the material is already bounded
# by `agent.retrieve_k` upstream; this is a backstop, not a budget.
#
# ------------------------------------------------------------------
# 12,000 -> 36,000 ON 2026-08-17, AND THE OLD VALUE WAS ABOUT TO TURN THIS FIX
# INTO A FIX THAT SHIPPED AS NOTHING.
#
# The sentence above -- "a backstop, not a budget" -- is the whole test, and
# 12,000 stopped passing it the moment the deck's `rerank_top_n` went to 10.
# Measured 2026-08-17, this repo's own markdown through
# `app.rag.ingest._prepare_chunks` at the production default `chunk_size=800`:
#
#     PRD.md   37 chunks   p50 2,380   p90 3,201   max 3,316 characters
#     EVAL.md  17 chunks   p50 2,428   p90 3,099   max 3,113 characters
#
# `chunk_size` is TOKENS, not characters -- the splitter is
# `RecursiveCharacterTextSplitter.from_tiktoken_encoder` -- and reading it as
# characters is how 12,000 ever looked roomy. A retrieved chunk is ~2,400
# characters. Ten of them are 22,000-33,000, so the old cap deleted the back
# SIX with no error, no warning and no log line: the k=40 Pinecone query would
# have been paid for, the ten-document rerank paid for, the prompt assembled,
# and `_truncate` would then have thrown most of it away. A widening that
# widens nothing, wearing a green harness -- the exact defect
# `new features/12-robust-handouts/PLAN.md` exists to correct, reproduced
# inside its own fix.
#
# 36,000 clears ten MAX-size chunks (10 x 3,316 plus filename tags and
# separators is ~33,500) with margin, which is what makes it a backstop again
# rather than the budget. `scripts/deck_check.py` case 32 pins the arithmetic
# and re-runs it against whatever `RECIPES["deck"].rerank_top_n` says, so
# raising the budget without raising the cap goes red instead of going quiet.
#
# TWO HONEST CONSEQUENCES BEYOND THE DECK, because this constant is shared:
#
#   1. `chart`, `table` and `sheet` are UNAFFECTED IN PRACTICE, and that is
#      measured rather than hoped: at the agent default `rerank_top_n = 3` the
#      worst case is 3 x 3,316 = ~10,000 characters, which never reached 12,000
#      either. Raising a cap nothing was hitting changes nothing.
#   2. An agent with `rerank_enabled = False` is a real change. It gets
#      `retrieve_k` documents unreranked -- 20 by default, ~48,000 characters --
#      so it WAS being truncated at 12,000 and is now truncated at 36,000. That
#      is three times the material in every handout for the Stage 1 teaching
#      configuration. It is the same direction as this fix and it is still a
#      change; it is written down here rather than discovered later.
# ------------------------------------------------------------------
MAX_CONTEXT_CHARS = 36_000
MAX_CONVERSATION_CHARS = 6_000
# Per answer, before the whole-conversation cap. A persona answer runs to 1,800
# characters (CLAUDE.md measures exactly that), so six of them would be the
# entire conversation budget and the corpus would be crowded out of its own
# handout.
MAX_ANSWER_CHARS = 1_200

# How many prior turns reach a handout. Six, matching `pipeline.HISTORY_TURNS`,
# and for a related reason: "chart what we just discussed" means the last few
# turns, and turn seven of a long thread changes the artefact almost never while
# costing prompt space every time.
CONVERSATION_TURNS = 6


# --------------------------------------------------------------------------
# Recipes
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Recipe:
    """One thing the panel can make.

    `prompt` is a template carrying three placeholders -- `{brief}`, `{context}`
    and `{conversation}` -- and it is rendered by `render` below rather than by
    `str.format`. See that function for why.
    """

    key: str
    label: str
    # One line, shown under the button in the panel. Written for somebody who
    # has never used the product, which is why none of them says "generate".
    blurb: str
    kind: str
    extension: str
    mime_type: str
    uses_sandbox: bool
    prompt: str

    # ------------------------------------------------------------------
    # HOW MUCH CORPUS THIS RECIPE IS ALLOWED. `None` means "the agent's own
    # value", which is today's behaviour exactly, so the recipes that leave
    # both unset are untouched BY CONSTRUCTION rather than by care.
    #
    # These are PER-CALL OVERRIDES on `aretrieve(agent, question, *, k, top_n)`
    # and are never written onto the `Agent` row. The prohibition is the
    # load-bearing half and `retriever.aretrieve` gives the whole argument:
    # setting `agent.retrieve_k = 40` for the duration of a job is the obvious
    # implementation and is a data-loss bug, because the `Agent` is a live ORM
    # object inside a session that gets committed -- so a value chosen for one
    # handout would be flushed into the operator's saved configuration and
    # silently become the agent's permanent setting.
    #
    # THIS IS A BUDGET, NOT A SECOND RETRIEVER. The retriever is still
    # constructed in exactly one place (`app/rag/retriever.py`), which is what
    # keeps a handout and an answer agreeing about what the corpus says. A
    # handout retrieving DIFFERENTLY would break that; retrieving MORE does
    # not.
    retrieve_k: int | None = None
    rerank_top_n: int | None = None

    @property
    def output_filename(self) -> str:
        """What the sandbox code is told to write, and what the job looks for.

        Derived rather than stored so it cannot drift from `key`/`extension` --
        but note the prompts spell the same name out literally, because a model
        follows a name it can read in the instruction far more reliably than one
        interpolated in. `_primary_artifact` in `jobs.py` therefore treats this
        as a preference and falls back to matching on the extension: the model
        writing `figure.png` instead of `chart.png` is a naming miss, not a
        failed run, and re-running it would cost 20 seconds to fix nothing.
        """
        return f"{self.key}{self.extension}"


# The system turn. Deliberately about FORMAT ONLY -- the grounding rules live at
# the top of each recipe prompt, where the task instruction can be read against
# them, and repeating them here would create two copies to keep in step.
#
# "Reply with the artefact itself" is the load-bearing half. Gemma will happily
# preface code with "Here's a script that..." and follow it with an explanation,
# and both end up inside the file that gets executed.
SYSTEM_PREAMBLE = """\
You produce teaching materials from supplied course material, and nothing else.

Follow the instructions in the message exactly. Reply with the artefact itself \
-- no preamble, no explanation, no commentary before or after it."""


# The block every sandbox recipe opens with. Restated in each prompt rather than
# concatenated in code, so that a recipe can be read as one instruction.
#
# "There is no filesystem and no network" is the sentence that matters most. The
# reflex for a data task is `pd.read_csv("data.csv")`, and a model that reaches
# for it fails on a `FileNotFoundError` that costs a whole retry to discover.
# Saying it once, in capitals, ahead of the task, is worth more than the retry.
_SANDBOX_RULES = """\
HOW YOUR CODE RUNS. Read this before writing a line:
- Your entire reply is saved as one Python file and executed. Reply with code \
and nothing else: no prose, no explanation, no markdown fence.
- THERE IS NO FILESYSTEM AND NO NETWORK. You cannot read a CSV, open a path, \
download anything or query anything. Every number, label and string you use \
must be written into the code as a LITERAL, copied out of the MATERIAL below.
- Imports are restricted to: matplotlib, numpy, pandas, pptx, math, statistics, \
datetime, decimal, json, csv, io, re, textwrap, string, itertools, functools, \
collections, pathlib, base64. Anything else -- os, sys, requests, subprocess -- \
is refused before the code runs.
- eval, exec, compile, getattr, setattr and any attribute starting and ending \
with double underscores are refused for the same reason.
- Keep it short and straight-line. There is a wall-clock limit and a memory \
limit, and a clever solution that times out produces nothing at all."""


_GROUNDING_RULES = """\
GROUNDING COMES FIRST. It outranks every instruction below, and no amount of \
polish is worth breaking it:
- Use only what is in the MATERIAL. Do not use prior knowledge and do not \
complete a half-covered idea from what you know about the subject generally.
- NEVER INVENT A NUMBER. Not a figure, not a percentage, not a date, not a \
count, not a unit. If the MATERIAL does not state it, it does not go in.
- A well-formatted artefact reads as though somebody checked it. A chart with \
labelled axes, a deck with clean bullets and a table with a header row all \
carry authority the MATERIAL may not have earned, which is exactly why \
fabricating here is worse than fabricating in prose.
- If the MATERIAL supports only part of the brief, make the smaller honest \
version and say what is missing. That is a correct outcome, not a failure."""


CHART_PROMPT = f"""\
{_GROUNDING_RULES}

{_SANDBOX_RULES}

- Write EXACTLY ONE file, into the current directory, named `chart.png`. Do not \
write any other file.
- matplotlib is already on a non-interactive backend, so never call \
`plt.show()`. Finish with \
`plt.savefig("chart.png", dpi=150, bbox_inches="tight")`.
- You may `print()` one short line saying what the chart shows. It becomes the \
caption under the chart in the panel, so write it for a reader, not for a log.

WHAT TO MAKE. One clear chart answering the brief:
- Choose the form the data justifies: bars for categories, a line for a series \
over time, a horizontal bar chart when the labels are long, a scatter for two \
measured quantities. No pie chart with more than four slices, and no chart at \
all with fewer than two data points.
- Title it with what it shows. Label both axes. Give units wherever the \
MATERIAL gives them, and no units where it does not.
- If the MATERIAL supports three data points, plot three. A sparse honest chart \
is the goal; padding it to look fuller is fabrication with a legend on it.
- Do not add a trend line, a projection, a total or an average unless the \
MATERIAL states that value.

BRIEF: {{brief}}

MATERIAL (retrieved from this agent's corpus):
{{context}}
{{conversation}}"""


DECK_PROMPT = f"""\
{_GROUNDING_RULES}

{_SANDBOX_RULES}

- Write EXACTLY ONE file, into the current directory, named `deck.pptx`, using \
python-pptx (`from pptx import Presentation`). Do not write any other file.
- Do not use images, icons, charts, custom fonts or template files. There are \
no files to load them from and no network to fetch them over; each one is a \
crash, not a downgrade.
- Set 16:9 with `prs.slide_width` and `prs.slide_height` in `Inches`. Use \
`prs.slide_layouts[0]` for the title slide and `prs.slide_layouts[1]` for \
content slides -- those two exist in the default template; higher indices vary.
- Finish with `prs.save("deck.pptx")`.
- You may `print()` one short line describing the deck. It becomes the caption \
in the panel.

WHAT TO MAKE. A short deck answering the brief:
- Five to eight slides. A title slide, then ONE idea per slide.
- Each content slide gets a heading of at most eight words and three to five \
bullets. Each bullet is a complete claim taken from the MATERIAL, not a \
fragment and not a topic label.
- End a bullet with the source filename in square brackets when it came from \
one, exactly as an answer would cite it.
- If the MATERIAL only supports four slides, make four. A deck padded to a \
round number is padded with invention.
- Do not write a "Conclusions", "Next steps" or "Recommendations" slide unless \
the MATERIAL contains conclusions, next steps or recommendations. Those are the \
slides a model writes from nowhere.

BRIEF: {{brief}}

MATERIAL (retrieved from this agent's corpus):
{{context}}
{{conversation}}"""


TABLE_PROMPT = f"""\
{_GROUNDING_RULES}

{_SANDBOX_RULES}

- Write EXACTLY ONE file, into the current directory, named `table.csv`. Do not \
write any other file.
- Use the `csv` module or `pandas.DataFrame.to_csv("table.csv", index=False)`. \
Write UTF-8. With `csv.writer`, open the file with `newline=""` or every row \
gets a blank line after it.
- You may `print()` one short line saying what the table contains. It becomes \
the caption in the panel.

WHAT TO MAKE. One tidy table answering the brief:
- One header row of short, specific column names, then one row per item.
- EVERY CELL MUST BE TRACEABLE TO THE MATERIAL. Leave a cell empty rather than \
estimating, interpolating or rounding something the MATERIAL did not state. An \
empty cell is information; a plausible one is not.
- Do not add a computed column -- a total, a percentage, a difference, a rank -- \
unless the MATERIAL states that value. Deriving it is arithmetic the reader did \
not ask for and cannot check against the source.
- Add a final `source` column naming the file each row came from where the \
MATERIAL says which file that was.
- Rows come from the MATERIAL, so the table is as long as the MATERIAL is. Ten \
real rows beat twenty with ten guesses in them.

BRIEF: {{brief}}

MATERIAL (retrieved from this agent's corpus):
{{context}}
{{conversation}}"""


# No sandbox. The reply IS the artefact -- see the module docstring for why this
# one is deliberately the cheap path.
SHEET_PROMPT = f"""\
{_GROUNDING_RULES}

HOW TO REPLY. Your entire reply is saved as the study sheet:
- Write GitHub-flavoured Markdown and nothing else. No preamble, no sign-off, \
and do not wrap the whole document in a code fence.
- Cite the source filename in square brackets after each claim you took from \
it, exactly as an answer would.

WHAT TO MAKE. A study sheet answering the brief, in this shape:
- An `#` H1 title naming what the sheet covers.
- Two sentences at most saying what it covers and who it is for.
- `##` sections following the structure of the MATERIAL -- its order, not an \
order you think is better -- each with tight bullets a learner could revise from.
- A `## Key terms` section: term, then a one-line definition, defined the way \
the MATERIAL defines it. Omit a term the MATERIAL uses but never defines rather \
than defining it yourself.
- A `## Check yourself` section: three to five questions the MATERIAL actually \
answers. Do not write the answers -- a question a learner can look up in the \
sheet is worth more than one they can read the answer to.
- A `## Not covered here` section naming, one line each, anything the brief \
asked for that the MATERIAL does not contain. Omit this section entirely when \
nothing is missing. This is the most useful section on the page when it is \
needed: a learner who cannot see where the material stopped cannot tell what \
they still have to go and find.

BRIEF: {{brief}}

MATERIAL (retrieved from this agent's corpus):
{{context}}
{{conversation}}"""


# WHY ONLY THE DECK CARRIES A RETRIEVAL BUDGET.
#
# `03-deck-material-budget.md` proposes widening `table` too -- "likely also
# benefits; a table of three rows is the same defect" -- and that was not built,
# because the two are not the same defect and reading the prompts side by side
# is what separates them.
#
# The deck's defect is a CONTRADICTION BETWEEN TWO NUMBERS: `DECK_PROMPT` says
# "five to eight slides" and the pipeline supplied three chunks. That is
# measurable, it is stated in the prompt, and it is what makes the starvation
# invisible -- the honest-shrink rule resolves the contradiction in the
# direction that looks correct.
#
# `TABLE_PROMPT` names no size at all. It says the opposite: "Rows come from the
# MATERIAL, so the table is as long as the MATERIAL is." There is no
# contradiction to fix, so widening it would be a number set from instinct, and
# this repo's standing rule is that a default carries the measurement that chose
# it. It would also not be free: every extra chunk is more marginal material
# under a prompt whose central instruction is EVERY CELL MUST BE TRACEABLE, and
# a row is a claim. Widen it when `deck_rate_check.py`-style evidence exists for
# tables, not before.
#
# `sheet` is the same argument and is the one the feature file did not raise,
# which is itself the tell: if three chunks were too thin for a table they would
# be too thin for the study sheet, which is the most-pressed button in the
# panel. Nobody has measured either.
#
# So the asymmetry is deliberate and it is asserted rather than assumed --
# `scripts/deck_check.py` case 30 fails if any of the three quietly acquires a
# budget, and fails if the deck quietly loses one.
RECIPES: dict[str, Recipe] = {
    "chart": Recipe(
        key="chart",
        label="Chart",
        blurb="Plot figures from the material as a labelled chart.",
        kind="chart",
        extension=".png",
        mime_type="image/png",
        uses_sandbox=True,
        prompt=CHART_PROMPT,
    ),
    "deck": Recipe(
        key="deck",
        label="Slide deck",
        blurb="Turn a topic into five to eight slides you can present.",
        kind="deck",
        extension=".pptx",
        mime_type=(
            "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        ),
        uses_sandbox=True,
        prompt=DECK_PROMPT,
        # THE ONLY RECIPE WHOSE PROMPT NAMES A SIZE, AND THEREFORE THE ONLY ONE
        # WITH A MEASURABLE CONTRADICTION TO FIX. `DECK_PROMPT` asks for "five
        # to eight slides, one idea per slide" while the pipeline handed the
        # model the agent's default 20 -> 3. Eight slides at one idea each need
        # roughly eight distinct pieces of material; three chunks cannot carry
        # eight ideas, and the prompt's own honest-shrink rule then converts the
        # starvation into a four-slide deck that LOOKS CORRECT. That is a
        # retrieval defect wearing a prompt defect's clothes, and no validator
        # can see it -- from the artefact alone a thin deck IS an honest shrink.
        #
        # PROVISIONAL, AND SAYING SO IS NOT A HEDGE.
        # `new features/12-robust-handouts/03-deck-material-budget.md` proposes
        # both numbers and says to set them from measurement rather than
        # instinct. `scripts/deck_rate_check.py` is the script that settles
        # them -- it already produces the deck slide-count distribution -- and
        # that feature file's criterion A6 is to run it as TWO ARMS: top_n=3
        # against this value, n >= 6 each, arm order ALTERNATED, because this
        # repo has one recorded case of a measurement reporting confidently
        # about its own loop order rather than about its subject. The second arm
        # does not exist yet, so until it does these two numbers are an
        # argument, not a finding.
        #
        # The argument, for whoever re-tunes them: this is the widest per-call
        # budget in the codebase and it is deliberately next to the second
        # widest, `quiz-generator`'s 40 -> 8 (`app/db/specialists.py:163-166`),
        # which carries the same stated reason -- "items must spread across the
        # corpus rather than cluster on one passage". A deck is that shape
        # exactly: eight slides are eight places in the corpus, not one passage
        # read closely. It costs Pinecone time at k=40 and prompt tokens at
        # top_n=10; reranking is ~830 ms and `top_n` does not change it.
        retrieve_k=40,
        rerank_top_n=10,
    ),
    "table": Recipe(
        key="table",
        label="Table",
        blurb="Pull the facts into a spreadsheet you can open.",
        kind="table",
        extension=".csv",
        mime_type="text/csv",
        uses_sandbox=True,
        prompt=TABLE_PROMPT,
    ),
    "sheet": Recipe(
        key="sheet",
        label="Study sheet",
        blurb="A one-page revision sheet, with questions to test yourself.",
        kind="sheet",
        extension=".md",
        mime_type="text/markdown",
        # See the module docstring. This is the recipe that keeps the panel
        # working when matplotlib does not.
        uses_sandbox=False,
        prompt=SHEET_PROMPT,
    ),
}


def render(recipe: Recipe, *, brief: str, material: "Material") -> str:
    """Fill a recipe's three placeholders. Substitution, never `str.format`.

    `str.format` would parse EVERY brace in the prompt, and these prompts are
    full of Python source: `plt.savefig(...)` is safe today, and the first
    prompt that gains an f-string, a dict literal or a set comprehension turns
    into `KeyError: 'brief'` at generation time. `pipeline.py` records the same
    trap arriving from the other direction -- a persona containing a brace makes
    `ChatPromptTemplate` demand a variable nobody declared -- and the fix there
    was likewise to stop treating the prompt as a template.

    The substituted VALUES are never re-scanned, so a brace in the brief, in a
    retrieved chunk or in a stored answer is inert either way. Only the prompt
    itself was ever at risk.
    """
    return (
        recipe.prompt.replace("{brief}", brief)
        .replace("{context}", material.context or "(nothing was retrieved)")
        .replace("{conversation}", material.conversation_block)
    )


# --------------------------------------------------------------------------
# Filenames
# --------------------------------------------------------------------------

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def provisional_filename(recipe: Recipe, brief: str) -> str:
    """A filename for the row the route inserts, before any bytes exist.

    `handouts.filename` is NOT NULL and the panel shows the row while it is
    still `pending`, so something has to be there from the first insert. This is
    that something: a slug of the brief plus the recipe's extension.

    The job overwrites it with the name the model's code actually wrote, which
    is why `app/api/handouts.py` sanitises on the way out rather than here --
    the value in this column is user-derived at insert time and model-derived
    afterwards, and only one guard can cover both. It is placed at the boundary
    the string escapes through.
    """
    slug = _SLUG_RE.sub("-", brief.lower()).strip("-")[:48].strip("-")
    return f"{slug or recipe.key}{recipe.extension}"


def derive_title(brief: str) -> str:
    """The panel's label for a handout: the brief's first line, capped.

    `handouts.title` is `String(200)`. A brief is capped at 1,000 characters by
    the request schema, so this truncation is real rather than theoretical, and
    it happens here so that the route and the job cannot disagree about it.
    """
    first_line = brief.strip().splitlines()[0] if brief.strip() else ""
    title = first_line.strip() or "Handout"
    return title[:200]


# --------------------------------------------------------------------------
# Material
# --------------------------------------------------------------------------


@dataclass
class Material:
    """What a handout is allowed to be made out of.

    `context` is the corpus half and `conversation` the thread half; see
    `gather_material` for why both are needed. `chunk_ids` is provenance and
    goes into `handouts.meta`, so a chart can be traced back to the chunks that
    produced it exactly as an answer can be traced through `query_chunks`.
    """

    context: str = ""
    conversation: str = ""
    chunk_ids: list[str] = field(default_factory=list)
    turn_count: int = 0

    @property
    def is_empty(self) -> bool:
        """True when there is nothing to ground a handout in.

        The job refuses rather than generating from an empty MATERIAL. A model
        handed no material and a brief writes a beautifully formatted artefact
        entirely out of parametric memory, and it is indistinguishable from a
        grounded one -- which is the single worst outcome this feature can
        produce.
        """
        return not self.context.strip() and not self.conversation.strip()

    @property
    def conversation_block(self) -> str:
        """The conversation half, ready to append, or "" when there is none.

        Returns the empty string rather than a "no conversation" placeholder:
        the prompts end with `{conversation}`, so an empty thread simply ends
        the prompt at the corpus, and there is no sentence for the model to read
        as an instruction about a thread that does not exist.
        """
        if not self.conversation.strip():
            return ""
        return (
            "\n\nRECENT CONVERSATION (the user's own thread with this agent; "
            "treat the answers below as material too):\n" + self.conversation
        )


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n... [truncated]"


async def _recent_answers(
    db: AsyncSession,
    agent_id: uuid.UUID,
    conversation_id: uuid.UUID,
    limit: int = CONVERSATION_TURNS,
) -> tuple[str, int]:
    """The last few answered turns of one thread, oldest first.

    **Filtered on `agent_id` as well as `conversation_id`, and that is not
    belt-and-braces.** `conversation_id` reaches this module from a request
    body; only the agent has been through `owned_agent`. `app/api/handouts.py`
    checks the pair before it inserts anything, but this function is called from
    a background job with two bare ids and no route in front of it, so it does
    its own scoping -- the same rule `app/rag/jobs.py` follows when it loads a
    document on the `(id, agent_id)` pair rather than on the id alone. Reading
    an unscoped conversation here would put another tenant's answers into this
    tenant's slide deck.

    Newest-first in SQL and reversed in Python: the LIMIT has to take the most
    RECENT turns, and the prompt has to read in the order they happened.

    `answer.isnot(None)`: a `queries` row exists before its answer does, and a
    refused turn stores the refusal as its answer -- which is material worth
    having, because "the material does not cover X" is exactly the kind of thing
    a study sheet's "Not covered here" section should pick up.
    """
    rows = await db.execute(
        select(QueryRow.question, QueryRow.answer)
        .where(
            QueryRow.conversation_id == conversation_id,
            QueryRow.agent_id == agent_id,
            QueryRow.answer.isnot(None),
        )
        .order_by(QueryRow.created_at.desc())
        .limit(limit)
    )
    turns = list(rows.all())
    turns.reverse()

    blocks = [
        f"Q: {question.strip()}\nA: {_truncate((answer or '').strip(), MAX_ANSWER_CHARS)}"
        for question, answer in turns
    ]
    return _truncate("\n\n".join(blocks), MAX_CONVERSATION_CHARS), len(blocks)


async def gather_material(
    db: AsyncSession,
    agent: Agent,
    brief: str,
    conversation_id: uuid.UUID | None,
    *,
    recipe: Recipe,
) -> Material:
    """Everything a handout is grounded in: the corpus, and the thread.

    **Both halves are needed, and each covers a case the other cannot.**

    The corpus half is `aretrieve` -- the same seam every answer goes through,
    because a handout retrieving differently from an answer would make the two
    disagree about what the corpus says. It is what makes "make me a deck about
    the power budget" work from a freshly opened panel with no conversation at
    all, which is the common case: the panel is a place you go to make
    something, not a place you go after a chat.

    **`recipe` is required and keyword-only, and both of those are the point.**
    It supplies `retrieve_k`/`rerank_top_n`, which are `None` for three of the
    four recipes and therefore mean "the agent's own value" -- today's behaviour
    exactly. A default of `recipe=None` would have read as harmless and would
    have silently reproduced the starvation this parameter exists to fix for
    every caller added after it; keyword-only stops the fifth argument being
    quietly bound to something else. It is more material through the SAME
    retriever, not a second retrieval path: nothing here calls
    `similarity_search()` and nothing writes a value back onto the `Agent` row.

    The conversation half is what the panel exists for. "Chart what we just
    discussed" has no useful retrieval query in it -- the brief is four words of
    deixis -- and the numbers the user means are in the answers above, already
    retrieved, already reranked, already cited. Retrieving the brief a second
    time would find nothing and the chart would be made from thin air.

    Neither half is a fallback for the other. They are concatenated and the
    model is told both are material.

    `chunk_ids` is recorded whether or not the corpus half turns out to be the
    useful one, because provenance is about what the handout was ALLOWED to see,
    not about what it happened to use. **A wider budget makes that distinction
    matter more, not less**: the gap between what was retrieved and what the
    model actually put on a slide is now large enough to mislead somebody
    reading `meta["chunk_ids"]` as a list of sources. It is not. It is the list
    of chunks the model could have used.
    """
    # OVERRIDES, NEVER WRITES -- `pipeline.py` passes the routed specialist's
    # width through the same two arguments for the same reason. `None` on both
    # is the identity case and is what the other three recipes send.
    retrieval = await aretrieve(
        agent, brief, k=recipe.retrieve_k, top_n=recipe.rerank_top_n
    )

    chunk_ids = [
        str(chunk_id)
        for doc in retrieval.documents
        if (chunk_id := doc.metadata.get(META_CHUNK_ID)) is not None
    ]

    # `format_context` rather than a local join, and imported rather than
    # copied. It is what tags each passage with its filename, which is what
    # makes the "cite the source filename" instruction in every prompt above
    # answerable -- a second implementation here would drift, and the symptom
    # would be handouts that cite nothing while answers cite correctly.
    context = _truncate(format_context(retrieval.documents), MAX_CONTEXT_CHARS)

    conversation = ""
    turn_count = 0
    if conversation_id is not None:
        conversation, turn_count = await _recent_answers(
            db, agent.id, conversation_id
        )

    return Material(
        context=context,
        conversation=conversation,
        chunk_ids=chunk_ids,
        turn_count=turn_count,
    )
