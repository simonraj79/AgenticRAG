/**
 * The four primitives every modal surface in this app needs: a focus trap, an
 * Escape handler, a scroll lock and focus restore. An audit found none of them
 * anywhere in this codebase. They are written once, here, rather than four
 * times badly in four panels -- `Drawer` was the first surface to need them and
 * the agent-settings sheet is the second, and a second copy of already-debugged
 * focus machinery is exactly the thing worth ruling out structurally.
 *
 * One constraint from this repository shapes the implementation, and it is a
 * bug that has already been paid for somewhere:
 *
 * **Focus exactly one element per transition.** StrictMode double-invokes
 * effects, and a step-change effect in the create-agent wizard that focused a
 * heading and then an input fired a blur between the two on the second
 * invocation -- which forged a "this field has been visited" flag and made the
 * form scold the user before they had typed anything. This hook focuses
 * `initialFocusRef` and nothing else, ever.
 */

import { useEffect, useRef } from "react";
import type { RefObject } from "react";

/**
 * What Tab may land on inside the panel, in document order.
 *
 * `[tabindex="-1"]` is excluded deliberately -- that is programmatic focus, and
 * the heading a caller passes as `initialFocusRef` carries it precisely so it
 * can be focused on open without joining the tab cycle. `details > summary` is
 * here because `Reveal` is a native `<details>` and its summary is focusable;
 * miss it and Tab skips every disclosure the panel contains.
 */
const FOCUSABLE = [
  "a[href]",
  "area[href]",
  "button:not([disabled])",
  "input:not([disabled]):not([type='hidden'])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "summary",
  "iframe",
  "audio[controls]",
  "video[controls]",
  "[contenteditable]:not([contenteditable='false'])",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

/**
 * Visible focusable descendants.
 *
 * `getClientRects().length` rather than `offsetParent !== null`: the second is
 * null for any `position: fixed` element regardless of visibility, and this
 * whole subtree lives under a fixed wrapper. A collapsed `<details>` hides its
 * contents by rendering no boxes, so this is also what stops Tab disappearing
 * into a closed disclosure.
 */
export function focusableWithin(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
    (element) => element.getClientRects().length > 0,
  );
}

export function useFocusTrap({
  open,
  onClose,
  panelRef,
  initialFocusRef,
  lockScroll = true,
}: {
  open: boolean;
  onClose: () => void;
  panelRef: RefObject<HTMLElement | null>;
  /** Focused when the surface opens. Must be focusable (tabindex -1 is fine). */
  initialFocusRef: RefObject<HTMLElement | null>;
  /** Default true. A modal sheet locks; a non-modal popover would not. */
  lockScroll?: boolean;
}): void {
  // Whatever had focus at the moment the surface opened -- almost always the
  // toggle button that opened it. Stored rather than assumed, because the
  // drawer can also be opened by a chip inside an answer bubble, and returning
  // focus to the wrong control is how a keyboard user loses their place.
  const returnFocusRef = useRef<HTMLElement | null>(null);

  // ---- Escape, and the focus trap -------------------------------------
  //
  // One listener for both, on `document` rather than on the panel: Escape has
  // to work even when focus has somehow escaped the panel, and that is exactly
  // the case a trap exists to recover from. Added only while open and removed
  // in cleanup, so a page with six closed drawers has zero listeners.
  useEffect(() => {
    if (!open) return;

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
        return;
      }

      if (event.key !== "Tab") return;

      const panel = panelRef.current;
      if (!panel) return;

      const items = focusableWithin(panel);
      if (items.length === 0) {
        // Cannot happen while the close button is rendered, but a trap that
        // assumes it has somewhere to send focus is a trap that throws.
        event.preventDefault();
        initialFocusRef.current?.focus();
        return;
      }

      // Tab is handled here in full rather than only at the two ends. Letting
      // the browser move focus natively in the middle and intercepting only the
      // wrap looks equivalent and is not: focus starts on the heading, which is
      // `tabindex="-1"` and so is in no native cycle at all, and Shift+Tab from
      // there would walk straight out of the dialog into the page behind it.
      const index = items.indexOf(document.activeElement as HTMLElement);
      event.preventDefault();

      if (index === -1) {
        // On the heading, or focus has left the panel entirely.
        (event.shiftKey ? items[items.length - 1] : items[0]).focus();
        return;
      }

      const next = event.shiftKey
        ? (index - 1 + items.length) % items.length
        : (index + 1) % items.length;
      items[next].focus();
    }

    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [open, onClose, panelRef, initialFocusRef]);

  // ---- Focus in on open, back out on close ----------------------------
  //
  // Its own effect, and it moves exactly one element's worth of focus. See the
  // StrictMode note in the file docstring: this is the effect shape that bug
  // came from, so it does one thing.
  //
  // Under StrictMode the sequence is run -> cleanup -> run. That is correct
  // here rather than merely tolerated: the cleanup restores focus to the opener
  // before the second run reads `document.activeElement`, so the ref ends up
  // holding the opener either way instead of holding the heading.
  useEffect(() => {
    if (!open) return;

    returnFocusRef.current = document.activeElement as HTMLElement | null;
    initialFocusRef.current?.focus();

    return () => {
      const target = returnFocusRef.current;
      returnFocusRef.current = null;
      // `isConnected` because the opener may have unmounted while the surface
      // was open -- a toggle that is `xl:hidden` disappears on a resize past
      // the breakpoint. Focusing a detached node silently sends focus to
      // `<body>`, which is worse than leaving it where it is.
      if (target?.isConnected) target.focus();
    };
  }, [open, initialFocusRef, returnFocusRef]);

  // ---- Scroll lock ----------------------------------------------------
  //
  // The previous value is saved and put back, not reset to "". They are the
  // same today, and they stop being the same the moment anything else -- a
  // second drawer, a future modal -- locks the body first. Restoring a value
  // you assumed rather than the one you replaced is how a page ends up
  // permanently unscrollable with nothing in the DOM to blame.
  useEffect(() => {
    if (!open || !lockScroll) return;
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previous;
    };
  }, [open, lockScroll]);
}
