/**
 * The mention-matching logic, which is pure and needs no browser.
 *
 * The cases worth having here are the ones where "it looks like it works" and
 * "it works" come apart: an `@` inside an email address, a token the roster
 * does not contain, and a roster that omits a specialist this file knows
 * about. Each of those renders perfectly while doing the wrong thing, which is
 * the failure shape this repository keeps meeting.
 */

import { describe, expect, it } from "vitest";
import {
  applyMention,
  filterSpecialists,
  findMentionToken,
  resolveSpecialist,
  rosterFor,
  specialistLabel,
  SPECIALISTS,
} from "./specialists.ts";

const ALL = SPECIALISTS.map((entry) => entry.slug);

describe("resolveSpecialist", () => {
  it("accepts a slug, an alias, a leading @ and any case", () => {
    expect(resolveSpecialist("feynman-explainer")?.slug).toBe("feynman-explainer");
    expect(resolveSpecialist("feynman")?.slug).toBe("feynman-explainer");
    expect(resolveSpecialist("@Polya")?.slug).toBe("polya-coach");
    expect(resolveSpecialist("GIBBS")?.slug).toBe("reflective-coach");
  });

  it("returns null for anything the roster does not name", () => {
    // The rule that keeps "what is @risk here" from becoming a routing event.
    expect(resolveSpecialist("risk")).toBeNull();
    expect(resolveSpecialist("@")).toBeNull();
    expect(resolveSpecialist("")).toBeNull();
  });
});

describe("specialistLabel", () => {
  it("writes the article for a known slug and never invents one", () => {
    expect(specialistLabel("feynman-explainer")).toBe("the Explainer");
    expect(specialistLabel("polya-coach")).toBe("the Problem coach");
    // Naming what the server actually said beats a fluent sentence about a
    // persona this client has never heard of.
    expect(specialistLabel("lecture-qa")).toBe("lecture-qa");
  });
});

describe("rosterFor", () => {
  it("is empty for the classic agent, which is what switches the popup off", () => {
    expect(rosterFor(null)).toEqual([]);
    expect(rosterFor(undefined)).toEqual([]);
    expect(rosterFor([])).toEqual([]);
  });

  it("keeps this file's order and drops slugs it cannot spell", () => {
    const roster = rosterFor(["quiz-generator", "feynman-explainer", "nonesuch"]);
    expect(roster.map((entry) => entry.slug)).toEqual([
      "feynman-explainer",
      "quiz-generator",
    ]);
  });
});

describe("findMentionToken", () => {
  it("opens at the start of the input and after whitespace", () => {
    expect(findMentionToken("@fey", 4)).toEqual({ start: 0, end: 4, query: "fey" });
    expect(findMentionToken("explain @pol", 12)).toEqual({ start: 8, end: 12, query: "pol" });
    // A bare @ is a valid, empty query -- that is how somebody who does not
    // know the names discovers them.
    expect(findMentionToken("@", 1)).toEqual({ start: 0, end: 1, query: "" });
  });

  it("does NOT open on an @ inside a word", () => {
    // An email address is the case this rule exists for.
    expect(findMentionToken("simon@example.com", 13)).toBeNull();
    expect(findMentionToken("a@b", 3)).toBeNull();
  });

  it("is closed once the caret leaves the token", () => {
    expect(findMentionToken("@feynman ", 9)).toBeNull();
    expect(findMentionToken("@feynman.", 9)).toBeNull();
    expect(findMentionToken("plain question", 14)).toBeNull();
  });

  it("runs `end` past the caret so a mid-token accept replaces the whole word", () => {
    // Caret parked after "fey" in "@feynman".
    expect(findMentionToken("@feynman", 4)).toEqual({ start: 0, end: 8, query: "fey" });
  });
});

describe("filterSpecialists", () => {
  const roster = rosterFor(ALL);

  it("shows everything for an empty query", () => {
    expect(filterSpecialists(roster, "")).toHaveLength(5);
  });

  it("matches slug, role and alias", () => {
    expect(filterSpecialists(roster, "quiz").map((s) => s.slug)).toEqual(["quiz-generator"]);
    expect(filterSpecialists(roster, "problem").map((s) => s.slug)).toEqual(["polya-coach"]);
    expect(filterSpecialists(roster, "socrates").map((s) => s.slug)).toEqual(["socratic-tutor"]);
  });

  it("returns nothing for a token the roster does not carry", () => {
    // `[]` is what closes the popup, leaving "@risk" as literal text.
    expect(filterSpecialists(roster, "risk")).toEqual([]);
  });

  it("never offers a specialist the agent's roster omits", () => {
    const narrow = rosterFor(["socratic-tutor"]);
    expect(filterSpecialists(narrow, "").map((s) => s.slug)).toEqual(["socratic-tutor"]);
    expect(filterSpecialists(narrow, "feynman")).toEqual([]);
  });
});

describe("applyMention", () => {
  it("replaces the partial token and leaves a trailing space", () => {
    const token = findMentionToken("@fey", 4);
    expect(token).not.toBeNull();
    const next = applyMention("@fey", token!, "feynman-explainer");
    expect(next.text).toBe("@feynman-explainer ");
    expect(next.caret).toBe(next.text.length);
    // The trailing space is what closes the popup: the caret now sits after a
    // non-token character, so Enter goes back to sending.
    expect(findMentionToken(next.text, next.caret)).toBeNull();
  });

  it("keeps the text on either side and does not double the space", () => {
    const text = "please @pol this for me";
    const token = findMentionToken(text, 11);
    const next = applyMention(text, token!, "polya-coach");
    expect(next.text).toBe("please @polya-coach this for me");
    // Past the space that was already there. A caret left inside the finished
    // token reopens the popup, and then Enter accepts instead of sending.
    expect(next.text.slice(next.caret)).toBe("this for me");
    expect(findMentionToken(next.text, next.caret)).toBeNull();
  });
});
