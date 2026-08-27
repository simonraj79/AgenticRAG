/**
 * Handouts, docked under the composer.
 *
 * **Why a dock and not the third column, and not a tab.**
 *
 * The third column had to go: `xl:grid-cols-[15rem_minmax(0,1fr)_22rem]` is a
 * narrow fixed left rail, an elastic middle and a wider fixed right rail, which
 * is NotebookLM's Sources | Chat | Studio silhouette at almost exactly its
 * proportions -- down to the same dock-on-desktop, drawer-on-mobile switch. It
 * was the single strongest signal in the codebase.
 *
 * A tab was the obvious replacement and it is the wrong one. A handout is
 * produced BY a turn -- either the user picked a recipe, or the agent wrote
 * Python mid-answer and it emitted a file -- and moving it to a tab costs the
 * adjacency that makes "chart what we just discussed" legible. It would also
 * break the `seed` path outright: tab panels here are mounted on demand, so a
 * handout produced mid-answer would reach an unmounted panel and appear only on
 * its own three-second poll, with nothing announcing it on a phone at all.
 *
 * A dock keeps the adjacency and removes the silhouette. It also fits the shape
 * of the content better than the column did: a wide short box suits a file list
 * and a one-row recipe control, where the tall narrow column needed the 2x2 card
 * grid that was itself the second NotebookLM signal.
 *
 * **`shrink-0`, so it takes from the thread and never from the composer.** The
 * composer being fully inside the viewport at 390x844 with the page at
 * `scrollTop = 0` is an acceptance criterion this must not break, and the order
 * of flex items is what guarantees it: thread (`flex-1 min-h-0`), composer, dock.
 *
 * **Never opened by an effect.** The constraint recorded in `AgentChat` still
 * holds and still bites: the conversation rename input cancels on blur by
 * design, so any code path that moves focus on the user's behalf silently
 * discards a rename in progress. A turn that produces a handout increments the
 * count; it does not open the dock. The count is the signal.
 */

import { useId } from "react";
import type { ReactNode } from "react";
import { BTN_QUIET, PILL_NEUTRAL } from "../lib/styles.ts";

export default function HandoutDock({
  count,
  open,
  onToggle,
  children,
}: {
  count: number;
  open: boolean;
  onToggle: () => void;
  children: ReactNode;
}) {
  const bodyId = useId();

  return (
    <div
      data-testid="handout-dock"
      data-open={open}
      className="shrink-0 border-t border-line"
    >
      <button
        type="button"
        data-testid="handout-dock-toggle"
        aria-expanded={open}
        aria-controls={bodyId}
        onClick={onToggle}
        // Quiet, and deliberately so. A handout is produced BY a turn; the dock
        // is a place to find one afterwards, not a thing competing with the
        // composer above it.
        //
        // `justify-start` overrides `BTN_QUIET`'s centring, and that override is
        // safe rather than lucky: Tailwind emits `.justify-center` before
        // `.justify-start`, so the later rule wins. The reverse pairing does NOT
        // hold -- `px-2` cannot narrow a `px-3` the same way -- so nothing here
        // tries to.
        className={`${BTN_QUIET} w-full justify-start text-left`}
      >
        {/* Rotates to point the way the panel will move. Decoration -- the state
            is carried by `aria-expanded`, and the global reduced-motion rule
            kills the transition without costing any information. */}
        <span
          aria-hidden="true"
          className={`inline-block text-xs transition-transform ${open ? "rotate-180" : ""}`}
        >
          &#9650;
        </span>
        <span className="font-medium text-ink">Handouts</span>
        {/* Inside the label rather than beside it, so the accessible name
            carries the number too -- while the dock is shut this count is the
            only signal that a handout exists at all. */}
        {/* `font-mono` because it is a COUNT -- the same rule every number in
            this app follows. `PILL_NEUTRAL` because it is a measurement rather
            than a state: nothing about a handout count is good or bad. */}
        <span className={`${PILL_NEUTRAL} font-mono`}>{count}</span>
      </button>

      {/*
        MOUNTED WHILE CLOSED, hidden with `hidden` rather than unmounted -- and
        this was got wrong once, in exactly the way worth recording.

        The first version rendered `{open && children}`, reasoning that a shut
        dock should not hold a live poll. It shipped, and the toggle read
        "Handouts 0" on a conversation whose answer said "made 1 handout": the
        count is reported UP by the panel's own list request, so unmounting the
        panel is unmounting the thing that produces the number. The count is the
        only signal a handout exists at all while the dock is shut -- so the
        optimisation deleted precisely the state it was protecting.

        The premise was wrong too. `HandoutsPanel` polls only while some row is
        still pending and stops the moment none are, so staying mounted costs ONE
        list request per conversation, not one every three seconds.

        `hidden` (i.e. `display: none`) rather than the Drawer's translate-and-
        `inert` pair, because nothing here animates and a `display: none` subtree
        is already out of the tab order -- `inert` would be belt with no braces
        to hold up.

        `min(45vh, 24rem)` rather than a percentage: a percentage height needs a
        definite parent, and this sits in a flex column whose height is itself
        derived. The viewport unit needs nothing and tracks a mobile browser
        collapsing its URL bar for free.
      */}
      <div
        id={bodyId}
        // `p-3` is not only spacing: the global focus ring is `outline: 3px`
        // at `outline-offset: 3px`, so a control flush against the edge of an
        // `overflow-y-auto` box loses six pixels of its ring to the clip.
        className={`max-h-[min(45vh,24rem)] overflow-y-auto border-t border-line p-3 ${
          open ? "" : "hidden"
        }`}
      >
        {children}
      </div>
    </div>
  );
}
