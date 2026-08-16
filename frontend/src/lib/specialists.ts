/**
 * The five teaching personas an orchestrator agent can route a turn to.
 *
 * **This duplicates `backend/app/db/specialists.py`, deliberately.** The popup
 * in the composer has to filter as the user types, and a round trip per
 * keystroke to learn which of five fixed strings match is a request for a
 * constant -- the same argument that keeps `HandoutRecipe` client-side. What
 * crosses the wire is the roster (`Agent.specialists`, a list of slugs) and
 * nothing else.
 *
 * **The SLUGS are the contract.** A slug is what `@mention` parsing matches
 * server-side and what every `ROUTE` and `DELEGATE` payload carries, so a slug
 * that disagrees with `specialists.py` silently stops resolving -- the pill
 * renders bare and the popup inserts a mention nothing honours. `role` and
 * `icon` are local copy and may be reworded here alone; `heading` is a
 * verbatim mirror of the backend's section title and may not. If any of them
 * disagree, the backend is right and this file is stale.
 *
 * A slug NOT in this table is not an error either. Every read here degrades:
 * an unknown slug renders as itself rather than throwing, because the roster
 * is a database column and a sixth persona must not break the composer.
 */

export type Specialist = {
  /** The wire contract. Matched by the server's mention parser and carried in
   *  every ROUTE and DELEGATE payload. */
  slug: string;
  /** What this persona is called in prose: "Explainer", "Problem coach". */
  role: string;
  /** One emoji. Decoration -- every render site marks it `aria-hidden`,
   *  because it always sits beside the role it stands for. */
  icon: string;
  /** What a user may type after `@` instead of the slug, because nobody types
   *  a slug. Lowercase, matched case-insensitively, prefix-matched while
   *  filtering. */
  aliases: readonly string[];
  /**
   * The `##` heading the backend writes above this persona's section when a
   * turn carries two `@mentions` -- "Explained simply", not "Explainer".
   *
   * Deliberately NOT the role: the role names who answered and this names what
   * the section is, and a learner reading two stacked sections wants the
   * second. Nothing in the client renders it today; it is here so the two
   * halves of one string live in one obvious pair, and so a future renderer
   * that wants to tie a route pill to a section in the answer has the text
   * without re-deriving it from prose.
   */
  heading: string;
};

export const SPECIALISTS: readonly Specialist[] = [
  {
    slug: "feynman-explainer",
    role: "Explainer",
    icon: "\u{1F4A1}",
    aliases: ["feynman", "explain", "explainer", "simple"],
    heading: "Explained simply",
  },
  {
    slug: "socratic-tutor",
    role: "Socratic tutor",
    icon: "\u{1F989}",
    aliases: ["socratic", "socrates", "tutor", "ask"],
    heading: "Think it through",
  },
  {
    slug: "polya-coach",
    role: "Problem coach",
    icon: "\u{1F9ED}",
    aliases: ["polya", "coach", "solve", "problem"],
    heading: "Working it out",
  },
  {
    slug: "quiz-generator",
    role: "Quiz writer",
    icon: "\u{1F4DD}",
    aliases: ["quiz", "test", "practice", "questions"],
    heading: "Practice questions",
  },
  {
    slug: "reflective-coach",
    role: "Reflection guide",
    icon: "\u{1FA9E}",
    aliases: ["reflect", "reflective", "gibbs", "review"],
    heading: "Reflecting on it",
  },
];

const BY_SLUG = new Map(SPECIALISTS.map((entry) => [entry.slug, entry]));

/**
 * The persona a token names, or `null`.
 *
 * Accepts the slug or any alias, with or without a leading `@`, in any case --
 * `"@Feynman"`, `"feynman"` and `"feynman-explainer"` all resolve. Anything
 * else returns null and the caller leaves the text alone, which is what keeps
 * `"what is @risk here"` from becoming a routing event.
 */
export function resolveSpecialist(token: string): Specialist | null {
  const key = token.trim().replace(/^@/, "").toLowerCase();
  if (key === "") return null;
  const bySlug = BY_SLUG.get(key);
  if (bySlug) return bySlug;
  return SPECIALISTS.find((entry) => entry.aliases.includes(key)) ?? null;
}

/**
 * "the Explainer", for a sentence like "Routed to the Explainer".
 *
 * An unknown slug comes back verbatim and WITHOUT the article, so the line
 * reads "Routed to lecture-qa" rather than inventing "the lecture-qa". Naming
 * the thing the server actually said is more useful than a fluent sentence
 * about something this file has never heard of.
 */
export function specialistLabel(slug: string): string {
  const specialist = BY_SLUG.get(slug);
  return specialist ? `the ${specialist.role}` : slug;
}

/**
 * The agent's roster as specialists, in the order this file declares them.
 *
 * Order is this file's rather than the column's on purpose: the popup is a
 * fixed five-row list a user learns the shape of, and a list that reorders
 * because an operator edited a JSON array is a list nobody builds muscle
 * memory for. Slugs the roster names but this file does not know are dropped
 * -- a mention the popup cannot spell is one the user cannot type.
 *
 * Returns `[]` for `null`, which is the classic agent: no roster, no popup.
 */
export function rosterFor(slugs: readonly string[] | null | undefined): Specialist[] {
  if (!slugs || slugs.length === 0) return [];
  const wanted = new Set(slugs);
  return SPECIALISTS.filter((entry) => wanted.has(entry.slug));
}

/**
 * The partial `@token` the caret sits inside, or `null`.
 *
 * **The word-boundary rule is the whole point and it is what an email address
 * fails.** An `@` opens the popup only at the start of the input or after
 * whitespace, so `simon@example.com` never does -- the walk back from the
 * caret reaches the `@`, sees `n` before it, and gives up. Punctuation ends a
 * token too, so `@feynman.` is closed rather than a four-character query.
 *
 * `end` runs past the caret to the end of the word, so accepting a suggestion
 * with the caret parked mid-token replaces the whole token instead of leaving
 * its tail stranded. `query` is only what is BEFORE the caret, because that is
 * what the user has typed to narrow the list.
 *
 * **This is deliberately NARROWER than the server's `_MENTION` regex**, which
 * accepts any non-word, non-`@` character before the sigil and tolerates `@@`.
 * Narrow is the safe direction for a popup: the worst it costs is a suggestion
 * list that does not appear after an opening bracket, where the server would
 * still have honoured the mention the user typed by hand. Wider would be the
 * expensive direction -- a popup that offers to insert a mention nothing
 * parses.
 */
export type MentionToken = { start: number; end: number; query: string };

/** Slug characters. `-` is in it because every slug carries one. */
const TOKEN_CHAR = /[A-Za-z0-9_-]/;

export function findMentionToken(text: string, caret: number): MentionToken | null {
  const at = Math.max(0, Math.min(caret, text.length));

  let start = at - 1;
  while (start >= 0) {
    const character = text.charAt(start);
    if (character === "@") break;
    if (!TOKEN_CHAR.test(character)) return null;
    start -= 1;
  }
  if (start < 0) return null;

  // Start of input, or preceded by whitespace. Anything else is an `@` inside
  // a word.
  if (start > 0 && !/\s/.test(text.charAt(start - 1))) return null;

  let end = at;
  while (end < text.length && TOKEN_CHAR.test(text.charAt(end))) end += 1;

  return { start, end, query: text.slice(start + 1, at) };
}

/**
 * The roster narrowed to what the user has typed.
 *
 * An empty query matches everything, so a bare `@` shows the whole roster --
 * which is how somebody who does not know the names discovers them. Slug and
 * role match on a substring; aliases match on a prefix, because an alias is
 * already a short word and a substring rule there makes `"as"` pull in
 * `"ask"` for no benefit.
 */
export function filterSpecialists(
  roster: readonly Specialist[],
  query: string,
): Specialist[] {
  const needle = query.trim().toLowerCase();
  if (needle === "") return [...roster];
  return roster.filter(
    (entry) =>
      entry.slug.includes(needle) ||
      entry.role.toLowerCase().includes(needle) ||
      entry.aliases.some((alias) => alias.startsWith(needle)),
  );
}

/**
 * The composer's text with `token` replaced by `@slug `, and where the caret
 * belongs afterwards.
 *
 * **The caret must come to rest past a space, and that is a correctness rule
 * rather than a nicety.** A caret left inside the completed token reopens the
 * popup over a mention that is already finished, and the next Enter would
 * accept instead of sending -- the one behaviour that must not regress. So a
 * space is appended when the text does not already supply one, and the caret
 * steps over the existing space when it does. Appending unconditionally would
 * double the space on every mention typed into the middle of a sentence.
 */
export function applyMention(
  text: string,
  token: MentionToken,
  slug: string,
): { text: string; caret: number } {
  const rest = text.slice(token.end);
  const alreadySpaced = /^\s/.test(rest);
  const inserted = alreadySpaced ? `@${slug}` : `@${slug} `;
  return {
    text: `${text.slice(0, token.start)}${inserted}${rest}`,
    caret: token.start + inserted.length + (alreadySpaced ? 1 : 0),
  };
}
