/**
 * The user-facing copy for the ten tunable parameters, in one place.
 *
 * **Both tuning surfaces read this file** -- `CreateAgentWizard` and
 * `AgentSettingsSheet` -- and that is the whole reason it exists. The two used
 * to carry their own wording for the same ten columns, so `retrieve_k` was
 * "How many chunks Pinecone returns for the reranker to choose from" in one and
 * "How many chunks come back from the vector search before reranking" in the
 * other. A contract stated twice drifts, and the copy that drifted is never the
 * one you are reading.
 *
 * ## Three tiers, and the tiering IS the fix
 *
 * The strings this replaces measured 151-296 characters against a ~232px
 * column: four to ten wrapped lines of prose under a one-line control. Nothing
 * in them was wrong -- they are dense with real measurements -- but a paragraph
 * under every slider is a paragraph nobody reads, so the measurements were
 * paid for and then hidden by their own volume.
 *
 * So each parameter says the same things at three depths:
 *
 *   `label` + `tag`  what it is, in two to four plain words, with the real
 *                    column name beside it.
 *   `help`           ONE sentence, hard capped at 110 characters, always
 *                    visible, sitting under the control.
 *   `detail`         every measured fact the old `help` carried, behind a
 *                    "Why this matters" disclosure.
 *
 * **Nothing was deleted in the re-tier.** The 0.61-0.67 band, the 8,192-token
 * ceiling, the ~830 ms rerank hop, the 1.5-2.0 searches per step: every one of
 * them is still here, one tier down. If a fact leaves `detail` it leaves the
 * product, and each of these cost a measurement to learn.
 *
 * ## Why `tag` survives
 *
 * The obvious simplification is to show "Passage size" and drop `chunk_size`.
 * It is the wrong one. This app exists to TEACH retrieval-augmented generation,
 * the trace panel prints these column names on every turn, and EVAL.md and the
 * API talk in nothing else -- so hiding the vocabulary does not spare the user
 * the jargon, it just makes them meet it somewhere with no label attached.
 * Both, quietly: the plain label to read, the column name to carry across.
 *
 * ## Why `group` is "when", not "what"
 *
 * Grouping by subsystem (indexing / retrieval / generation) names the code.
 * Grouping by WHEN a change takes effect answers the question a user actually
 * has in front of a slider -- "if I move this, when do I see it?" -- and it is
 * the answer that prevents the real mistake: changing `chunk_size` and then
 * asking a question, expecting a different answer from documents that were
 * indexed under the old value and did not move. `AgentSettingsSheet` already
 * grouped this way and it is the surface that got the narrow column right, so
 * the wizard adopts its sections rather than inventing a third arrangement.
 *
 * ## The `inert` group is a correctness fix, not a demotion
 *
 * `score_threshold` and `max_rewrites` are saved, sent, and printed in the
 * trace, and read by no code path in this build. Verified rather than assumed:
 * `score_threshold`'s only consumers are `backend/app/api/ask.py:1195` and
 * `:1614`, both trace payloads, under a section header that reads
 * `# 4. Score check -- OBSERVABILITY ONLY`; `max_rewrites`' only consumer
 * outside schemas and seeds is `ask.py:1197`, the same trace payload.
 *
 * The copy they replace told the user that a low score makes a question "a
 * candidate for rewriting" and that "0 turns rewriting off". Both are false,
 * and the second is the worse kind of false: a user who follows it believes
 * they disabled a subsystem that is still running. Neither control is deleted
 * -- `07-workspace-shell.md` settled that, and the argument holds: the trace
 * prints `score_threshold` on every turn, so hiding it here only moves the
 * unexplained number somewhere with no explanation at all.
 *
 * ## Plain strings, not JSX
 *
 * `help` and `detail` are typed `ReactNode` and are, today, all strings. This
 * is a `.ts` file, so JSX is not available in it -- and that constraint is the
 * right one anyway: this file owns the WORDS and the two consuming components
 * own the rendering. The old copy set `gemini-embedding-2` in a mono span and
 * "rewriting" in an `<em>`, which put presentation in a copy deck and meant the
 * two surfaces could style the same sentence differently. The type stays
 * `ReactNode` so a consumer may pass a richer node later without a change here.
 *
 * The 110-character cap on `help` is a house rule, not a type -- TypeScript
 * cannot count a string's length -- so it is asserted in `scripts/wizard_check.py`.
 * If you lengthen a `help` string, that harness is what goes red.
 *
 * ## `format` is display-only
 *
 * **THE STORED VALUE NEVER CHANGES.** `splitter` stays `"markdown"` /
 * `"recursive"` on the wire, in the trace and in the API; only what is drawn on
 * the screen becomes *At headings* / *At paragraphs*. `format` is optional, so
 * every call site wants `TUNABLES[key].format?.(value) ?? String(value)`.
 */

import type { ReactNode } from "react";

export type TunableKey =
  | "chunk_size"
  | "chunk_overlap"
  | "splitter"
  | "retrieve_k"
  | "rerank_enabled"
  | "rerank_top_n"
  | "score_threshold"
  | "max_rewrites"
  | "tools_enabled"
  | "max_tool_steps";

/** WHEN a parameter takes effect. The grouping is the teaching. */
export type TunableGroup = "answer" | "upload" | "inert";

export type TunableCopy = {
  key: TunableKey;
  /** Plain English, 2-4 words. What the user reads. */
  label: string;
  /** The real column name, kept as a quiet mono tag. This app teaches RAG;
   *  hiding the vocabulary is a loss, not a simplification. */
  tag: string;
  group: TunableGroup;
  /** ONE sentence, hard cap 110 characters. Sits under the control. */
  help: ReactNode;
  /** Every measured fact that used to be crammed into `help`. Nothing is
   *  deleted; it moves down one tier, behind "Why this matters". */
  detail: ReactNode;
  /** Display-only. Never changes the stored value. */
  format?: (value: string | number | boolean) => string;
};

// --------------------------------------------------------------------------
// Display formatters
// --------------------------------------------------------------------------

/**
 * `1 token`, `800 tokens`. Every unit on this page pluralises with a bare "s",
 * so one helper covers all four.
 *
 * The guard matters more than the plural does. A number input hands back a
 * string while it is being typed, and `String(value)` for a half-typed field
 * would render "NaN tokens" beside a control the user is mid-way through
 * using. An unparseable value renders as itself instead -- the same degradation
 * rule `specialists.ts` uses for an unknown slug: show what is there, never
 * throw and never invent.
 */
function counted(value: string | number | boolean, unit: string): string {
  const amount = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(amount)) return String(value);
  return `${amount} ${unit}${amount === 1 ? "" : "s"}`;
}

/**
 * `0.5` -> `0.50`.
 *
 * The slider steps in hundredths and the wizard rounds to two places before
 * storing, precisely so 0.01 steps cannot accumulate into a stored
 * `0.6100000000000001`. Printing `0.5` beside a control that moves in
 * hundredths reads as though the hundredths digit is not there.
 */
function twoDecimals(value: string | number | boolean): string {
  const amount = typeof value === "number" ? value : Number(value);
  return Number.isFinite(amount) ? amount.toFixed(2) : String(value);
}

/**
 * On / Off.
 *
 * **Both spellings arrive here.** `Tuning` stores a boolean, while `Segmented`
 * models the same control as the strings "on" and "off" -- and `Boolean("off")`
 * is TRUE, so a bare truthiness test renders a switched-off control as "On".
 * That is a silent wrong answer in a readout whose only job is to say which way
 * the switch is set, so the string is compared and never coerced.
 */
function onOff(value: string | number | boolean): string {
  if (typeof value === "string") {
    return value === "on" || value === "true" ? "On" : "Off";
  }
  return value ? "On" : "Off";
}

/**
 * The two splitter values, in words.
 *
 * An unrecognised value renders as itself. The column is free text on the
 * server and a third strategy is one seed row away; falling back to the raw
 * string means such a row shows up as `semantic` rather than as a blank space
 * where a label should be.
 */
const SPLITTER_LABELS: Record<string, string> = {
  markdown: "At headings",
  recursive: "At paragraphs",
};

function splitterLabel(value: string | number | boolean): string {
  const stored = String(value);
  return SPLITTER_LABELS[stored] ?? stored;
}

// --------------------------------------------------------------------------
// The ten
// --------------------------------------------------------------------------

/**
 * Keyed by column name, and each entry repeats its own key.
 *
 * The repetition is not redundancy waiting to drift: the mapped type below
 * pins `key` to the property it sits under, so an entry filed against the
 * wrong key is a compile error rather than a label that quietly describes the
 * neighbouring slider. It is there because both surfaces iterate -- a section
 * renders `GROUPS` and then filters this table -- and an iterated entry that
 * does not know which parameter it is has to be re-paired by hand at every
 * call site.
 */
export const TUNABLES: { [K in TunableKey]: TunableCopy & { key: K } } = {
  // ----------------------------------------------------------------------
  // Takes effect on the next upload
  // ----------------------------------------------------------------------

  chunk_size: {
    key: "chunk_size",
    // "Passage" rather than "chunk" throughout the plain-English tier, and it
    // has to be the SAME word in every one of the six places a passage is
    // mentioned -- shortlist size, passages used, overlap. Alternating between
    // "chunk", "passage" and "section" is what made the old copy read as three
    // unrelated subsystems.
    label: "Passage size",
    tag: "chunk_size",
    group: "upload",
    help: "How much text goes into one searchable passage, counted in tokens rather than characters.",
    // Tokens-not-characters leads, because it is the one thing here that is
    // actively misleading if assumed: 800 looks like a sentence and is closer
    // to three paragraphs.
    detail:
      "Tokens, not characters -- big enough to hold a whole idea, small enough that retrieval stays precise. On this project's own documents, the default of 800 tokens measured around 2,400 characters at the median, so a passage is a good deal larger than the number suggests. It also uses about a tenth of the embedding model's 8,192-token ceiling; a passage that crosses that ceiling has its tail truncated when it is embedded and lost, with nothing raised anywhere. Changing this affects new uploads only: documents already indexed keep the size they were split at, so re-upload one to apply a change.",
    format: (value) => counted(value, "token"),
  },

  chunk_overlap: {
    key: "chunk_overlap",
    label: "Passage overlap",
    tag: "chunk_overlap",
    group: "upload",
    help: "How much each passage repeats from the one before it, so nothing is lost at a boundary.",
    detail:
      "Tokens repeated between neighbouring passages, so a fact that straddles a boundary is still retrievable from one side of it. Every seeded persona uses 15% of its passage size -- 120 tokens against 800, 75 against 500 -- which is the ratio to keep if you move either number. The overlap must stay smaller than the passage size; the server rejects a value that is not, because a passage that repeats all of its predecessor is its predecessor. Like passage size, it is read at ingest time and applies to new uploads only.",
    format: (value) => counted(value, "token"),
  },

  splitter: {
    key: "splitter",
    // The only label here phrased as a question's answer rather than a noun.
    // "Splitter" names the class that does it; a user choosing between two
    // options wants to know what the choice is ABOUT, and the choice is about
    // where the cut lands.
    label: "Where to cut documents",
    tag: "splitter",
    group: "upload",
    help: "Whether a document is cut at its headings or at its paragraphs.",
    detail:
      "At headings keeps a heading attached to the body beneath it, which is what stops a slide's title being cut away from the content it introduces -- the reason it is the default for a slide-heavy corpus. At paragraphs splits on blank lines and sentences and ignores structure, which is the better choice for prose that has no headings to cut at. The stored values are markdown and recursive, and those are the words the trace and the API use.",
    format: splitterLabel,
  },

  // ----------------------------------------------------------------------
  // Takes effect on the next answer
  // ----------------------------------------------------------------------

  retrieve_k: {
    key: "retrieve_k",
    // "Shortlist size" and "Passages used" are a deliberate pair: a pool, and
    // then a selection from it. The relationship between the two is the single
    // most useful thing to understand about retrieval here, and under the old
    // labels -- "Retrieve k" and "Rerank top n" -- it was visible only to
    // someone who already knew it.
    label: "Shortlist size",
    tag: "retrieve_k",
    group: "answer",
    help: "How many passages the search shortlists for the re-ranker to choose from.",
    detail:
      "The pool, not the selection: nothing here reaches the model directly, and Passages used decides what does. A bigger pool is the one thing that genuinely fixes poor recall, because a passage that was never shortlisted cannot be re-ranked into an answer. It costs re-ranking latency -- measured at about 830 ms, the only cross-Pacific round trip in a turn, against generation at 13.2 s for the same turn. The seeded personas span 12 to 40, chosen per persona rather than tuned to one number.",
    format: (value) => counted(value, "passage"),
  },

  rerank_enabled: {
    key: "rerank_enabled",
    label: "Re-rank results",
    tag: "rerank_enabled",
    group: "answer",
    help: "A second pass that reorders the shortlist by how well each passage answers the question.",
    detail:
      "Cohere's rerank-v3.5 reads the question and each shortlisted passage together, which the vector search cannot do -- the search compares the question to a passage as two points in space, and this compares them as two pieces of language. Precision is what it buys: with it off, the passages that reach the model are whatever the embedding happened to put at the top. It costs about 830 ms, roughly a twentieth of what generation costs on the same turn, so it is rarely the thing worth switching off for speed.",
    format: onOff,
  },

  rerank_top_n: {
    key: "rerank_top_n",
    label: "Passages used",
    tag: "rerank_top_n",
    group: "answer",
    help: "How many of the shortlisted passages actually reach the model.",
    detail:
      "The operative number: it bounds how many separate places in the corpus can contribute to one answer. It cannot exceed the shortlist -- asking for 8 out of a shortlist of 3 gets 3 -- so raise Shortlist size first. Widening it is not free in either direction: more passages dilute a focused answer, and they make 'the material does not cover that' harder to say honestly, because a gap is visible across 3 focused passages and invisible across 8 loosely related ones where something always looks close enough. The seeded personas range from 3 to 8, and the narrowest of them is the explainer.",
    format: (value) => counted(value, "passage"),
  },

  // ----------------------------------------------------------------------
  // Recorded, but changes nothing today
  // ----------------------------------------------------------------------

  score_threshold: {
    key: "score_threshold",
    // "Match-score line", not "Score threshold". "Threshold" is a word that
    // promises a branch -- something happens on the other side of it -- and
    // nothing happens on the other side of this one. A line drawn on a chart is
    // what it actually is.
    label: "Match-score line",
    tag: "score_threshold",
    group: "inert",
    // The old sentence, "Below this top similarity score the question becomes a
    // candidate for rewriting", describes a loop that was specified, measured,
    // and dropped. Replaced with what is true rather than softened, because a
    // hedge ("mostly advisory") would leave the reader believing there is some
    // remaining circumstance in which it fires. There is not.
    help: "Saved with the agent and printed in the trace. Nothing in this build reads it.",
    detail:
      "It was specified as the trigger for the Stage 2 rewrite loop: below this top similarity score, rewrite the question and search again. It was then measured on a real corpus and dropped. On-topic questions scored 0.61-0.67 and off-topic ones 0.49-0.58, so the default of 0.5 sits INSIDE the overlap rather than above it -- and the off-topic question 'What is the refund policy for this course?' scored 0.5765, comfortably above the line, and was refused correctly anyway. What refuses is the persona's prompt, never this number, and what decides whether to search again is the agent reading the passages it was given. The value is still stored, still sent, and still printed under 'score check' on every turn's trace, which is why it is shown here rather than hidden: a number that appears in the trace and nowhere else is a number with no explanation attached.",
    format: twoDecimals,
  },

  max_rewrites: {
    key: "max_rewrites",
    label: "Rewrite limit",
    tag: "max_rewrites",
    group: "inert",
    help: "Saved with the agent and printed in the trace. No code path in this build reads it.",
    // The sentence this replaces, "0 turns rewriting off", is the more damaging
    // of the two false statements: a user who acts on it believes they switched
    // off a subsystem that is still running, and every subsequent observation
    // gets interpreted through that belief.
    detail:
      "It would have been the ceiling on the Stage 2 rewrite loop -- the loop whose trigger, the match-score line, was measured and dropped. Setting it to 0 does not turn rewriting off, which is what this control used to claim. The one rewriter in this build runs on every single question: it repairs typos and shorthand, and turns 'what is its power budget?' into a standalone question by reading the conversation above it. It is switched by a server-wide setting with no per-agent path, so nothing on this page can reach it. The ceiling that does bind a loop today is the look-up limit, which is the same argument one loop further out.",
    format: (value) => counted(value, "rewrite"),
  },

  // ----------------------------------------------------------------------
  // Takes effect on the next answer (agentic)
  // ----------------------------------------------------------------------

  tools_enabled: {
    key: "tools_enabled",
    // The only label here that names an action rather than a quantity, because
    // this is the only parameter that changes what the agent DOES rather than
    // what it retrieves. "Tools" named the mechanism and told a user nothing
    // about what switching it on would change.
    label: "Look things up mid-answer",
    tag: "tools_enabled",
    group: "answer",
    help: "Lets the agent search again, or run Python, in the middle of writing an answer.",
    // Naming the cost and its limit in one breath. Half of this sentence --
    // "and nothing to the turns where it does not" -- is what stops the other
    // half reading as an argument against switching it on.
    detail:
      "With it off, the agent works from a single pass of retrieval and answers with whatever that returned. With it on, an answer that has found half of what it needs can go and look for the rest. That also makes a refusal stronger rather than weaker: 'I searched and it is not there' is a better answer than 'it was not in the passage I happened to be given'. It adds a few seconds to the turns that use it and nothing to the turns that do not, because generation is about 89% of a turn and a turn that stops to look something up generates twice.",
    format: onOff,
  },

  max_tool_steps: {
    key: "max_tool_steps",
    label: "Look-up limit",
    tag: "max_tool_steps",
    group: "answer",
    help: "How many times the agent may stop and look something up before it must answer.",
    detail:
      "A ceiling, not a target: most turns use none of it. Cost blowout is one of the four named agentic failure modes and an unbounded loop is precisely how it is reached -- every step is a fresh model call carrying the whole accumulated transcript, so the cost of another step rises while its value falls. It bounds STEPS, not searches: the model in this build issues 1.5 to 2.0 searches per step, so a limit of 3 has been measured producing 6 retrievals in one turn. Setting it to 0 leaves the tools bound but unreachable, which is a slower way of saying off.",
    format: (value) => counted(value, "step"),
  },
};

/**
 * The three sections, in the order both surfaces render them.
 *
 * The titles are `AgentSettingsSheet`'s existing section headings, verbatim, so
 * a user who tunes an agent after creating one meets the same three sentences
 * in the same three places rather than learning the arrangement twice.
 *
 * `answer` leads because it is the group whose effect is immediate and
 * therefore the group worth experimenting with; `upload` follows because its
 * changes are invisible until a document is re-ingested, which is the thing to
 * say before someone moves a slider and waits; `inert` is last because a
 * section that changes nothing has no claim on a reader's attention until the
 * ones that do have had it.
 */
export const GROUPS: { id: TunableGroup; title: string; blurb: string }[] = [
  {
    id: "answer",
    title: "Takes effect on the next answer",
    blurb:
      "Read when a question is asked, so a change here is in the very next answer.",
  },
  {
    id: "upload",
    title: "Takes effect on the next upload",
    blurb:
      "Read when a document is indexed. Anything already uploaded keeps the passages it was split into -- re-upload it to apply a change.",
  },
  {
    id: "inert",
    // Not "Advanced", and not hidden. Both would imply the controls do
    // something a beginner should not touch, when what they do is nothing.
    title: "Recorded, but changes nothing today",
    blurb:
      "Saved with the agent and printed in every turn's trace, and read by no code path in this build. Shown rather than hidden for exactly that reason: they appear in the trace, so a user who could not find them here would meet them there with no explanation at all.",
  },
];
