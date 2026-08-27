/**
 * The two tuning warnings, which are pure functions and need no browser.
 *
 * The cases that earn their place are not "does it fire" -- both predicates are
 * one comparison -- but the two ways a warning is worse than no warning at all.
 *
 * The first is a message that names a control nobody can see. These sentences
 * pre-date the relabel that turned `chunk_overlap` into *Passage overlap*, and
 * for a while they still read "Overlap must be smaller than chunk size" while
 * pointing at two sliders spelled differently: the drift `TUNABLES` exists to
 * close, reappearing one line beneath the controls it had closed it for. So the
 * assertions below read the CURRENT labels out of `TUNABLES` and check the
 * message contains them, rather than matching the literal words "Passage
 * overlap". A test written the other way would go green on exactly the day the
 * message went wrong -- it would pin today's copy, and a relabel would move the
 * label and the test would be the only thing still agreeing with the old text.
 *
 * The second is blocking on something the server accepts. `shortlistWarning` is
 * advisory, so its OFF cases are the load-bearing ones: it must stay silent
 * with re-ranking switched off, and at the boundary where the numbers are equal
 * and the re-ranker gets precisely what it asked for.
 */

import { describe, expect, it } from "vitest";
import { overlapWarning, shortlistWarning, TUNABLES } from "./tunables.ts";

/** Both labels, in both cases, since the messages lower-case the second one. */
function mentions(message: string, label: string): boolean {
  return message.includes(label) || message.includes(label.toLowerCase());
}

describe("overlapWarning", () => {
  it("is silent while the overlap is smaller than the passage", () => {
    expect(overlapWarning(800, 120)).toBeNull();
    // The seeded 15% ratio, and the narrowest gap that is still legal.
    expect(overlapWarning(500, 75)).toBeNull();
    expect(overlapWarning(800, 799)).toBeNull();
  });

  it("fires on equality, because the server rejects equality too", () => {
    // A passage that repeats all of its predecessor IS its predecessor, which
    // is why the server's rule is `<` and not `<=`. Stating it one step earlier
    // than the 422 is the whole reason this predicate exists.
    expect(overlapWarning(800, 800)).not.toBeNull();
    expect(overlapWarning(800, 900)).not.toBeNull();
  });

  it("names both controls by their CURRENT labels, and carries both values", () => {
    const message = overlapWarning(800, 900);
    expect(message).not.toBeNull();
    // Read out of the table, never typed in: a relabel must move the message
    // with it, and a test spelling "Passage overlap" by hand would pass while
    // the message pointed at a control that no longer exists under that name.
    expect(mentions(message!, TUNABLES.chunk_overlap.label)).toBe(true);
    expect(mentions(message!, TUNABLES.chunk_size.label)).toBe(true);
    expect(message).toContain("900");
    expect(message).toContain("800");
  });
});

describe("shortlistWarning", () => {
  it("is silent when the shortlist can cover what was asked for", () => {
    expect(shortlistWarning(20, true, 8)).toBeNull();
    // Equal is the boundary and it is fine: the re-ranker hands over every
    // passage it was given, which is exactly the number requested.
    expect(shortlistWarning(8, true, 8)).toBeNull();
  });

  it("stays silent with re-ranking off, however the numbers sit", () => {
    // Nothing is being handed over, so the comparison describes nothing -- and
    // the slider the message would point at is disabled on both surfaces.
    expect(shortlistWarning(3, false, 8)).toBeNull();
  });

  it("names both controls by their CURRENT labels, and carries both values", () => {
    const message = shortlistWarning(3, true, 8);
    expect(message).not.toBeNull();
    expect(mentions(message!, TUNABLES.rerank_top_n.label)).toBe(true);
    expect(mentions(message!, TUNABLES.retrieve_k.label)).toBe(true);
    expect(message).toContain("3");
    expect(message).toContain("8");
    // The near-miss worth pinning: interpolating `.tag` instead of `.label`
    // would also "read the labels out of the table" and would put the raw
    // column names in front of a user, which is the vocabulary the plain tier
    // exists to spare them. It reads correct at the call site and wrong on the
    // screen, so nothing but an assertion catches it.
    expect(message).not.toContain(TUNABLES.rerank_top_n.tag);
    expect(message).not.toContain(TUNABLES.retrieve_k.tag);
  });
});
