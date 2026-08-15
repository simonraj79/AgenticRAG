/**
 * The app's first modal surface, and therefore the first place four primitives
 * exist at all: a focus trap, an Escape handler, a scroll lock and focus
 * restore. An audit found none of them anywhere in this codebase. They are
 * written once, here, rather than four times badly in four panels.
 *
 * Three constraints from this repository shape the implementation, and each one
 * is a bug that has already been paid for somewhere:
 *
 * **Never depend on `transitionend`.** `index.css` kills every transition with
 * `transition-duration: 0.01ms !important` under `prefers-reduced-motion`, so a
 * close gated on a transition callback would either fire immediately or, on a
 * browser that skips the event entirely for a zero-length transition, never --
 * leaving the drawer permanently half-open for exactly the users who asked for
 * less motion. Visibility here is state and animation is decoration: the panel
 * is always mounted, and `open` swaps `translate-x-0` for `translate-x-full`.
 * Nothing waits for anything.
 *
 * **Focus exactly one element per transition.** StrictMode double-invokes
 * effects, and a step-change effect in the create-agent wizard that focused a
 * heading and then an input fired a blur between the two on the second
 * invocation -- which forged a "this field has been visited" flag and made the
 * form scold the user before they had typed anything. The drawer focuses the
 * heading and nothing else, ever.
 *
 * **`z-40`, and the number is not arbitrary.** The sticky nav is `z-20` and
 * carries `backdrop-blur`, which creates its own stacking context; 40 clears it
 * with room to spare. No portal is involved -- `index.html` has a single
 * `#root` and `fixed inset-0` escapes the view tree's layout without one, so
 * adding a portal root would buy nothing and cost a second place to look.
 */

import { useEffect, useId, useRef } from "react";
import type { ReactNode } from "react";

/**
 * What Tab may land on inside the panel, in document order.
 *
 * `[tabindex="-1"]` is excluded deliberately -- that is programmatic focus, and
 * the heading below carries it precisely so it can be focused on open without
 * joining the tab cycle. `details > summary` is here because `Reveal` is a
 * native `<details>` and its summary is focusable; miss it and Tab skips every
 * disclosure the panel contains.
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
function focusableWithin(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
    (element) => element.getClientRects().length > 0,
  );
}

export function Drawer({
  open,
  onClose,
  title,
  children,
  testId,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  testId?: string;
}) {
  const headingId = useId();
  const panelRef = useRef<HTMLDivElement>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);
  // Whatever had focus at the moment the drawer opened -- almost always the
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
        headingRef.current?.focus();
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
  }, [open, onClose]);

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
    headingRef.current?.focus();

    return () => {
      const target = returnFocusRef.current;
      returnFocusRef.current = null;
      // `isConnected` because the opener may have unmounted while the drawer
      // was open -- a toggle that is `xl:hidden` disappears on a resize past
      // the breakpoint. Focusing a detached node silently sends focus to
      // `<body>`, which is worse than leaving it where it is.
      if (target?.isConnected) target.focus();
    };
  }, [open]);

  // ---- Scroll lock ----------------------------------------------------
  //
  // The previous value is saved and put back, not reset to "". They are the
  // same today, and they stop being the same the moment anything else -- a
  // second drawer, a future modal -- locks the body first. Restoring a value
  // you assumed rather than the one you replaced is how a page ends up
  // permanently unscrollable with nothing in the DOM to blame.
  useEffect(() => {
    if (!open) return;
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previous;
    };
  }, [open]);

  return (
    <div
      // `overflow-hidden` so the panel parked at `translate-x-full` cannot
      // widen the page. A fixed element's overflow does not reach the document
      // scroll area in current browsers, but "zero horizontal scroll at 320px"
      // is a hard requirement and not worth resting on that.
      className={`fixed inset-0 z-40 overflow-hidden ${open ? "" : "pointer-events-none"}`}
      // Both attributes, on purpose. `inert` is the correct primitive -- it
      // removes the subtree from the tab order as well as from the
      // accessibility tree, which matters because the panel stays mounted while
      // closed and its buttons would otherwise still be tabbable off-screen.
      // `aria-hidden` covers browsers that do not implement `inert` yet, where
      // at least a screen reader does not read out a hidden panel.
      inert={!open}
      aria-hidden={open ? undefined : true}
    >
      {/*
        The backdrop is a SIBLING of the panel, never a wrapper around it.
        Wrapped, every click inside the panel would also hit the backdrop's
        handler on the way up and close the drawer -- so the fix would be an
        event.target check on the wrapper, which is the version that breaks
        quietly the first time something inside the panel stops propagation.
        Siblings make the geometry do the work.
      */}
      <div
        aria-hidden="true"
        data-testid={testId ? `${testId}-backdrop` : "drawer-backdrop"}
        onClick={onClose}
        className={`absolute inset-0 bg-slate-950/70 transition-opacity duration-200 ${
          open ? "opacity-100" : "opacity-0"
        }`}
      />

      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={headingId}
        data-testid={testId ?? "drawer"}
        // `p-4` is not only spacing: the global focus ring is `outline: 3px`
        // with `outline-offset: 3px`, so a control flush against the edge of an
        // `overflow-y-auto` box loses six pixels of its ring to the clip. The
        // padding is what keeps the indicator whole.
        className={`absolute inset-y-0 right-0 flex h-full w-full flex-col overflow-y-auto border-l border-slate-800 bg-slate-900 p-4 shadow-2xl transition-transform duration-200 ease-out sm:w-[26rem] ${
          open ? "translate-x-0" : "translate-x-full pointer-events-none"
        }`}
      >
        <div className="flex items-start justify-between gap-3">
          {/*
            `tabIndex={-1}` makes this focusable without putting it in the tab
            cycle, which is what lets the drawer move focus in on open while
            still being the thing `aria-labelledby` names. It is the only
            element focused on a transition.
          */}
          <h2
            id={headingId}
            ref={headingRef}
            tabIndex={-1}
            className="min-h-11 py-2 text-sm font-semibold text-slate-100"
          >
            {title}
          </h2>

          <button
            type="button"
            onClick={onClose}
            aria-label={`Close ${title}`}
            data-testid={testId ? `${testId}-close` : "drawer-close"}
            // `min-w-11` as well as `min-h-11`. A 44px-tall button 20px wide is
            // not a 44px tap target, and an icon button is exactly where that
            // gets missed.
            className="min-h-11 min-w-11 shrink-0 rounded-md border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-300 transition hover:border-slate-600 hover:text-slate-100"
          >
            <span aria-hidden="true">&times;</span>
          </button>
        </div>

        {/* `min-h-0` so a long child scrolls the panel rather than growing past
            it -- a flex item's default `min-height: auto` refuses to shrink
            below its content and would push the bottom of the list out of
            reach. */}
        <div className="mt-4 min-h-0 flex-1">{children}</div>
      </div>
    </div>
  );
}

export default Drawer;
