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
 * is always mounted, and `open` swaps one whole class string for another.
 * Nothing waits for anything.
 *
 * **`z-40`, and the number is not arbitrary.** The sticky nav is `z-20` and
 * carries `backdrop-blur`, which creates its own stacking context; 40 clears it
 * with room to spare. No portal is involved -- `index.html` has a single
 * `#root` and `fixed inset-0` escapes the view tree's layout without one, so
 * adding a portal root would buy nothing and cost a second place to look.
 *
 * **Two placements, one primitive.** A right-hand sheet is the right shape for
 * editing something already on screen; it is the wrong shape for a four-step
 * task, because a 34rem edge panel is 511px of content on a 1440px monitor and
 * the same 511px on a 2560px one. `placement` is a layout switch and nothing
 * else -- the focus trap, Escape, the scroll lock, `inert`, the backdrop and
 * the sibling geometry below are shared verbatim, so a centred dialog cannot
 * drift away from behaviour the right-hand one has already had debugged.
 *
 * **Three regions, and the BODY is the scroll container, not the panel.** The
 * header (title, close) and the optional subheader (a step rail) are `shrink-0`
 * and never move; everything else scrolls inside the body. That is what stops a
 * long step scrolling away the only thing that says where you are and the only
 * way back. Read the comment on the body element before touching its margins:
 * they are load-bearing for the sticky footers two different children park at
 * its bottom edge.
 */

import { useId, useRef } from "react";
import type { ReactNode, RefObject } from "react";
import { useFocusTrap } from "../lib/useFocusTrap.ts";
import { BTN_ICON } from "../lib/styles.ts";

/**
 * Written out as whole class strings rather than composed from a fragment,
 * because Tailwind scans source text for candidates and never sees a class
 * assembled at runtime.
 *
 * `xl` is a new token rather than a wider `lg`: `AgentSettingsSheet` shares
 * `lg`, and its single-column form is the one surface that got 511px RIGHT.
 * Widening it in place would move a measured-good layout to fix a different
 * file's problem. The `min()` keeps a 60rem dialog off the edges of a 1024px
 * laptop, where a flat 60rem would be wider than the window.
 */
const WIDTH_CLASS = {
  md: "sm:w-[26rem]",
  lg: "sm:w-[34rem]",
  xl: "sm:w-[min(60rem,calc(100vw-2rem))]",
} as const;

/**
 * The placement-dependent halves, split out for the same Tailwind reason.
 *
 * The centred panel is sized `h-fit` with `max-h` as a CAP. That is not the
 * `calc(100dvh - top)` shape that once collapsed the chat pane to 24px with
 * nothing thrown: there is no measured complement here, and there must not
 * become one. The floor is the content's own intrinsic height, so the panel can
 * never be shorter than what is inside it; the cap only ever takes height away,
 * and the body's `min-h-0 flex-1 overflow-y-auto` absorbs the difference.
 */
const PLACEMENT_CLASS = {
  right: "inset-y-0 right-0 h-full w-full border-l border-line transition-transform",
  center:
    "inset-0 m-auto h-fit max-h-[min(56rem,90dvh)] w-full rounded-lg border border-line transition-[opacity,transform]",
} as const;

/**
 * Closed is `pointer-events-none` on the panel as well as on the wrapper. The
 * wrapper already carries it, and repeating it here is not redundancy: a
 * centred panel that is merely transparent still sits over the middle of the
 * page, so this is the half that has to be right, and stating both means a
 * future edit to one cannot silently unarm the other.
 */
const PLACEMENT_CLOSED_CLASS = {
  right: "translate-x-full pointer-events-none",
  center: "scale-95 opacity-0 pointer-events-none",
} as const;

const PLACEMENT_OPEN_CLASS = {
  right: "translate-x-0",
  center: "scale-100 opacity-100",
} as const;

export function Drawer({
  open,
  onClose,
  title,
  subheader,
  children,
  testId,
  width = "md",
  placement = "right",
  initialFocusRef: requestedInitialFocusRef,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  /**
   * An optional second fixed region under the title -- a step rail, a filter
   * row. It is deliberately given no margin of its own: a region that imposed
   * one would have to be fought with a negative margin by the first caller
   * whose rail wants to sit flush under the heading.
   */
  subheader?: ReactNode;
  children: ReactNode;
  testId?: string;
  width?: "md" | "lg" | "xl";
  /** `right` for editing something already on screen; `center` for a task. */
  placement?: "right" | "center";
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
        // `bg-ink/40` is one of the two places in the app where alpha is
        // permitted (the other is `disabled:opacity-45`). A backdrop is the one
        // element whose job IS to let the page show through, so it cannot come
        // off the opaque surface ladder -- and the ink token means it dims
        // correctly in both themes rather than only in the one it was written
        // in.
        className={`absolute inset-0 bg-ink/40 transition-opacity duration-200 ${
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
        data-placement={placement}
        // `p-4` is not only spacing: the global focus ring in `index.css` is
        // drawn OUTSIDE the box (`outline: 2px` at `outline-offset: 2px`), so a
        // control flush against the edge of a clipping box loses four pixels of
        // its ring. The padding is what keeps the indicator whole -- and since
        // it is the body below that clips now, the body reproduces this padding
        // exactly rather than inheriting it.
        // `overflow-clip` on the PANEL, and both halves of that are load
        // bearing: that it clips, and that it is `clip` rather than `hidden`. The scrolling body below clips its own content, but the
        // panel was `overflow: visible` and still reported 2,012px of
        // scrollable overflow against an 808px client height -- which
        // propagated to the drawer ROOT, whose own `overflow-hidden` makes it
        // a scrollport that is invisible to the user and fully scrollable to
        // SCRIPT. Measured: root `scrollTop` 70 with `scrollHeight` 2058, and
        // a centred panel dragged from top 45 to top -25 with its own title
        // clipped off the screen.
        //
        // The trigger is not exotic. `scrollIntoView` does it, and so does
        // moving FOCUS to a control low in a tall panel, because the browser
        // scrolls every ancestor scrollport to reveal the focused element --
        // so ordinary keyboard use reaches it.
        //
        // `overflow-hidden` fixes the root and MOVES the bug rather than
        // removing it: hidden clips AND creates a scroll container, so the
        // panel then accepted `scrollTop = 300` and slid its own heading from
        // y=-8 to y=-238. `overflow: clip` clips WITHOUT establishing a
        // scrollport, so there is nothing left to scroll at either level. The
        // scrolling body inside is unaffected -- it owns `overflow-y-auto`,
        // and `sticky` still resolves against it.
        //
        // `shadow-xl` rather than a hairline alone. Elevation in this design is
        // normally a rule plus a surface change and shadows are near-absent --
        // a drawer is one of the three things that genuinely floats above the
        // page, so it is one of the three that gets one.
        className={`absolute flex flex-col overflow-clip bg-surface p-4 shadow-xl duration-200 ease-out ${PLACEMENT_CLASS[placement]} ${WIDTH_CLASS[width]} ${
          open ? PLACEMENT_OPEN_CLASS[placement] : PLACEMENT_CLOSED_CLASS[placement]
        }`}
      >
        {/* Region 1, fixed. `shrink-0` because the body is the flex item that
            gives: without it, a tall body squeezes the close button. */}
        <div className="flex shrink-0 items-start justify-between gap-3">
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
            className="flex min-h-11 items-center py-2 text-lg font-semibold tracking-tight text-ink"
          >
            {title}
          </h2>

          <button
            type="button"
            onClick={onClose}
            aria-label={`Close ${title}`}
            data-testid={testId ? `${testId}-close` : "drawer-close"}
            // `BTN_ICON` carries `min-w-11` as well as `min-h-11`. A 44px-tall
            // button 20px wide is not a 44px tap target, and an icon button is
            // exactly where that gets missed -- which is why the width lives on
            // the shared string rather than being remembered here.
            className={`${BTN_ICON} shrink-0`}
          >
            <span aria-hidden="true" className="text-lg leading-none">
              &times;
            </span>
          </button>
        </div>

        {/* Region 2, also fixed, and rendered only when a caller has one -- so a
            drawer without a rail keeps exactly the header-then-body rhythm it
            had before this region existed. */}
        {subheader && (
          <div
            data-testid={testId ? `${testId}-subheader` : "drawer-subheader"}
            className="shrink-0"
          >
            {subheader}
          </div>
        )}

        {/*
          Region 3, and the only one that scrolls.

          **The negative margins are load-bearing, and they are not spacing.**
          `sticky` resolves its offset against the SCROLL CONTAINER's padding
          box. Moving `overflow-y-auto` off the panel and onto this element
          moves that reference -- so `-mx-4 -mb-4` here, against `px-4 pb-4`,
          reproduces the panel's padding box exactly: this element's padding box
          lands on the panel's padding box left, right and bottom, and its
          content box lands on the panel's content box. Both children that park
          a `sticky -bottom-4` action bar at the bottom edge
          (`CreateAgentWizard`, `AgentSettingsSheet`) therefore still resolve
          flush against it, unchanged and unaware the scroller moved. Delete
          either half and the settings sheet's Save bar parks 16px short of the
          edge with the form scrolling visibly through the gap underneath it --
          a bug this repo has already shipped, diagnosed and fixed once.

          `min-h-0` because a flex item's default `min-height: auto` refuses to
          shrink below its content, which would push the bottom of a long form
          out of reach rather than scrolling it.

          `@container` (native in Tailwind v4, no plugin) is the containment
          context the wizard's grids are keyed to. It is what lets a card grid
          ask how wide the PANEL is instead of how wide the window is -- the
          question that laid three 159px cards into 511px because the window
          happened to be 1440.
        */}
        <div className="@container -mx-4 -mb-4 mt-4 min-h-0 flex-1 overflow-y-auto px-4 pb-4">
          {children}
        </div>
      </div>
    </div>
  );
}

export default Drawer;
