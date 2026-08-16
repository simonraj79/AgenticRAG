# Prompt structure — starting a model-decided feature

Companion to [loop.md](loop.md). That file is the pattern; this one is how to open a session
that will follow it.

**Why a template at all.** The pattern's central finding is that binding a tool is twenty
lines that work first time, and the model then declines to call it — so the expensive part is
designing a trigger, and the expensive *mistake* is discovering that after the loop is built.
That is exactly how the first build went. The blocks below exist to move four questions
before the code instead of after it, where each of them costs a paragraph rather than a
rewrite.

---

## The five blocks

| Block | Purpose | Why it earns its place |
|---|---|---|
| **1. Anchor** | Point at `loop.md`, forbid designing first | Otherwise the session starts with `bind_tools` and meets the real problem an hour later |
| **2. The feature** | What the user gets, in plain terms | The agent should not have to infer the goal from a spec, and a goal stated plainly is a goal you can test against |
| **3. Answer before code** | The five questions | Cheap on paper, expensive once implemented. This is the whole point of the template |
| **4. Non-negotiables** | Off-switch, trace, request shape, failure handling | These are the ones that fail **silently** — a missing `EVENT_TYPES` entry, a widened request, a swallowed exception |
| **5. Proof** | Measurements, not claims | "It works" was wrong twice in the first build, and both times a green test was the reason |

---

## Template

```
Read `new features/loop.md` first, then the relevant parts of CLAUDE.md.
Do not design anything before you have.

WHAT I WANT
<one or two sentences, plain terms, what the user gets>

ANSWER THESE BEFORE WRITING CODE (loop.md sections 1, 3, 5)
1. Is this a tool, a prompt change, or a plain code path?
   If it must run every time, it is not a tool.
2. Smallest possible args schema. What does it CLOSE OVER rather than accept?
   (tenant, tuning, model -- never arguments)
3. Assume the model will NOT call it. What deterministic signal, read off its
   own output, says this was needed and not used?
   Trigger on the ABSENCE OF THE OUTCOME, never the presence of an error.
4. If there's a detector: what does a false positive cost, and a false
   negative? Set strictness from that asymmetry, not from instinct.
5. What scenario makes the feature NECESSARY rather than merely present?
   A test that passes without exercising it is worse than no test.

NON-NEGOTIABLE
- With the feature off, output is byte-identical to today -- assert it
- New trace event types added to EVENT_TYPES before the first write
- Tool failures come back as ToolMessages, never exceptions
- Do not add any parameter to a tool-bound request (check endpoints first)
- ASCII in print(); extensions on TS imports; min-h-11 on controls

THEN
Plan it in `new features/`, build it, and prove it with a scenario in
`scripts/agentic_check.py`. Show me the measurements, not the claims.
If loop.md turns out to be wrong, edit it -- don't add a second copy.
```

---

## Worked example

The obvious next one: `langchain-mcp-adapters` is installed and still unimported, and
[00-IMPLEMENTATION-PLAN.md §8](00-IMPLEMENTATION-PLAN.md) records that adding a third tool is
a registry entry once the loop exists.

```
Read `new features/loop.md` first, then CLAUDE.md's OpenRouter section.

WHAT I WANT
A third tool that lets an agent look something up in an MCP server the owner
has configured, so a corpus can be supplemented by a live source.

ANSWER THESE BEFORE WRITING CODE
1-5 as above. In particular: what triggers it when the model won't reach for
it on its own -- and how is that different from the corpus gap trigger, given
both fire on "I don't know"?

NON-NEGOTIABLE
- as above, plus: the MCP server is per-agent config, never a tool argument
- an MCP server being down is a TOOL_ERROR, not a turn failure

THEN
Plan, build, prove. Scenario must show it firing AND show the corpus tool
still preferred when the answer is local.
```

**Two things in that example are worth copying into any version of it.**

The question in block 3 is *specific to the feature* rather than generic. Two tools that both
fire on "I don't know" need a rule for which one wins, and asking for it up front is cheaper
than watching them fight in a trace.

The last line names **the thing that must keep working**, not only the thing being added.
Scenarios S1 and S7 exist for exactly that reason: everything else checks the new feature
works; those two check it did not eat the old one. A prompt that only describes the addition
gets a suite that only tests the addition.

---

## Short form

For a small change — a detector tweak, a retry condition, one more marker:

```
Read `new features/loop.md` section 3 (T2, T3) before touching this.
<the change>
Tell me what a false positive costs and what a false negative costs before
you pick the strictness. Trigger on the missing outcome, not on an error.
```

Short does not mean skip the questions. The four-time recurrence of the marker-list bug — 
`"does not say"`, `"does not cover"`, `"does not state"`, and then the rate-limit detector
missing `TooManyRequestsError` — was four *small* changes, each of which looked too minor to
warrant asking what shape the phrase belonged to.

---

## When NOT to use this

The template front-loads questions about model judgement. If the model is not making a
decision, the questions have no answers and asking them produces confident noise.

- **Fixing a bug in an existing tool** — the pattern is already chosen; go straight to
  `loop.md §7` for where the code lives.
- **A pure UI change**, even to the Handouts panel. Use
  [05-ui-ux-overhaul.md](05-ui-ux-overhaul.md)'s acceptance criteria instead.
- **A threshold or a branch.** `loop.md §1` is explicit: if a number decides it, write the
  `if`. A tool call is roughly 1.6 s and a model round trip.
- **Infrastructure** — region, plan, migration, index. That is PRD §7 and CLAUDE.md, and
  several of those decisions are expensive to reverse.
