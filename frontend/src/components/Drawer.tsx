/**
 * The app's first modal surface. The focus trap, the Escape handler, the scroll
 * lock and focus restore are no longer here: they live in
 * `../lib/useFocusTrap.ts`, so the agent-settings sheet gets the identical,
 * already-debugged behaviour rather than a second copy of it. The rationale for
 * each of those four mechanisms moved with the code -- read it there.
 *
 * Two constraints from this repository shape what is left in this file, and
 * each one is a bug that has already been paid for somewhere:
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
 * **`z-40`, and the number is not arbitrary.** The sticky nav is `z-20` and
 * carries `backdrop-blur`, which creates its own stacking context; 40 clears it
 * with room to spare. No portal is involved -- `index.html` has a single
 * `#root` and `fixed inset-0` escapes the view tree's layout without one, so
 * adding a portal root would buy nothing and cost a second place to look.
 */

import { useId, useRef } from "react";
import type { ReactNode, RefObject } from "react";
import { useFocusTrap } from "../lib/useFocusTrap.ts";

/**
 * Written out as whole class strings rather than composed from a fragment,
 * because Tailwind scans source text for candidates and never sees a class
 * assembled at runtime.
 */
const WIDTH_CLASS = {
  md: "sm:w-[26rem]",
  lg: "sm:w-[34rem]",
} as const;

export function Drawer({
  open,
  onClose,
  title,
  children,
  testId,
  width = "md",
  initialFocusRef: requestedInitialFocusRef,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  testId?: string;
  width?: "md" | "lg";
  /** Optional first control for task-focused drawers such as a wizard. */
  initialFocusRef?: RefObject<HTMLElement | null>;
}) {
  const headingId = useId();
  const panelRef = useRef<HTMLDivElement>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const initialFocusRef = requestedInitialFocusRef ?? headingRef;

  // Escape, the Tab cycle, focus in and back out, and the body scroll lock.
  // The heading is what receives focus on open -- see the `tabIndex={-1}` note
  // on it below.
  useFocusTrap({ open, onClose, panelRef, initialFocusRef });

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
        id={testId}
        aria-modal="true"
        aria-labelledby={headingId}
        data-testid={testId ?? "drawer"}
        // `p-4` is not only spacing: the global focus ring is `outline: 3px`
        // with `outline-offset: 3px`, so a control flush against the edge of an
        // `overflow-y-auto` box loses six pixels of its ring to the clip. The
        // padding is what keeps the indicator whole.
        className={`absolute inset-y-0 right-0 flex h-full w-full flex-col overflow-y-auto border-l border-slate-800 bg-slate-900 p-4 shadow-2xl transition-transform duration-200 ease-out ${WIDTH_CLASS[width]} ${
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
