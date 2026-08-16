/**
 * `@mention` autocomplete for the composer.
 *
 * There was no combobox, listbox or `aria-activedescendant` anywhere in `src/`
 * before this: the composer was a plain `<textarea>` whose entire key handling
 * was Enter-sends / Shift+Enter-newlines. Three constraints from
 * `scripts/ui_check.py` shape what is here, and each one is a defect this
 * repository has already paid for once:
 *
 * **A6 asserts exactly one scrollable region inside the chat column, so this
 * list never scrolls.** There are five specialists and there will not be
 * fifty; the list is capped by the roster rather than by a viewport, and it
 * carries no `overflow` property at all -- not even `overflow-hidden`, which
 * would be safe today and is exactly the line a future reader would "fix" into
 * `overflow-y-auto` while tidying. The rows round their own corners instead.
 *
 * **Closed means `display: none`, never `visibility: hidden`.** The panel stays
 * mounted so `aria-controls` on the textarea always resolves, and A8's control
 * sweep skips zero-height elements -- a `visibility: hidden` row is still 44px
 * tall to `getBoundingClientRect`, so the two spellings are not
 * interchangeable here. The `flex` / `hidden` swap is the one this codebase
 * already uses for the conversation rail, and it is deliberately not the
 * `contents` / `hidden` pair: both are display utilities of equal specificity,
 * so which one won would depend on their order in the generated stylesheet.
 *
 * **It opens UPWARD.** `absolute bottom-full` inside the textarea's own
 * relative wrapper. Downward would push the composer's box taller or overlay
 * the handout dock, and A5 asserts the composer is fully inside the viewport at
 * 390x844 -- a popup that grows the form is the same class of failure as the
 * header disclosures that collapsed the thread pane to 24px.
 *
 * No portal: `Drawer.tsx` records why this app has none, and an absolutely
 * positioned panel two elements from its anchor does not need one.
 *
 * **`Enter` still sends when this is shut**, which is the one behaviour that
 * must not regress. `handleKeyDown` returns `false` for every key it did not
 * consume, and it consumes nothing at all while `open` is false -- so the
 * composer's existing Enter-to-send and Shift+Enter-newline are untouched on
 * an agent with no roster, which is every agent that existed before routing.
 */

import { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent, RefObject } from "react";
import type { Specialist } from "../lib/specialists.ts";
import {
  applyMention,
  filterSpecialists,
  findMentionToken,
  rosterFor,
} from "../lib/specialists.ts";

/**
 * Everything the composer and the panel need, produced by one hook so the two
 * cannot disagree about which row is active.
 *
 * The keyboard contract lives in `handleKeyDown` rather than in the panel,
 * because the keys are pressed in the TEXTAREA -- the panel never holds focus,
 * which is the whole reason `aria-activedescendant` exists.
 */
export type MentionState = {
  /** False on any agent with no roster, and false whenever the caret is not
   *  inside an `@token` that matches something. */
  open: boolean;
  options: Specialist[];
  /** Index into `options`. Always in range while `open`: an `activeSlug` that
   *  has just been filtered out falls back to the first row rather than to
   *  nothing, so Enter always has a target. */
  activeIndex: number;
  listboxId: string;
  optionId: (slug: string) => string;
  /** For the textarea. `undefined` rather than `""` when shut -- an
   *  `aria-activedescendant` pointing at nothing is worse than none. */
  activeDescendant: string | undefined;
  /**
   * Call FIRST from the textarea's `onKeyDown`. Returns true when the popup
   * consumed the key, in which case the caller must do nothing else -- that
   * return value is what keeps Enter-to-send intact.
   */
  handleKeyDown: (event: KeyboardEvent<HTMLTextAreaElement>) => boolean;
  /** Call from `onChange`, `onClick` and `onKeyUp` so the token follows the
   *  caret rather than the end of the string. */
  noteCaret: (element: HTMLTextAreaElement) => void;
  /** Accept a suggestion. Safe to call from a mouse handler; it re-reads the
   *  token rather than trusting a closure. */
  pick: (specialist: Specialist) => void;
  /** Hover moves the active row, matching what the arrow keys do. */
  setActive: (slug: string) => void;
};

export function useMentions({
  roster,
  value,
  setValue,
  inputRef,
}: {
  /** `Agent.specialists` verbatim. Null or empty is a classic agent and
   *  switches the whole mechanism off. */
  roster: readonly string[] | null | undefined;
  value: string;
  setValue: (next: string) => void;
  inputRef: RefObject<HTMLTextAreaElement | null>;
}): MentionState {
  const listboxId = useId();
  const [caret, setCaret] = useState(0);
  /**
   * The `token.start` the user pressed Escape on, or null.
   *
   * Keyed on the position rather than a bare boolean so Escape dismisses THIS
   * mention and not the mechanism: typing a space and a fresh `@` gives a new
   * start and the popup returns, while continuing to type inside the token the
   * user just dismissed leaves it shut. A boolean would need a second rule to
   * decide when to clear itself, and every candidate for that rule reopens the
   * popup on the keystroke after Escape.
   */
  const [dismissedAt, setDismissedAt] = useState<number | null>(null);
  const [activeSlug, setActiveSlug] = useState<string | null>(null);
  /** Where to park the caret after the next commit. See the layout effect. */
  const pendingCaret = useRef<number | null>(null);

  const specialists = useMemo(() => rosterFor(roster), [roster]);

  const token = useMemo(
    () => (specialists.length === 0 ? null : findMentionToken(value, caret)),
    [specialists.length, value, caret],
  );

  const options = useMemo(
    () => (token === null ? [] : filterSpecialists(specialists, token.query)),
    [specialists, token],
  );

  const open = token !== null && options.length > 0 && dismissedAt !== token.start;

  // `-1` from `findIndex` becomes 0, which is the fallback that matters: the
  // active row was filtered out by the character just typed, and the first
  // remaining row is the right thing for Enter to accept.
  const found = options.findIndex((entry) => entry.slug === activeSlug);
  const activeIndex = found === -1 ? 0 : found;
  const active = options[activeIndex] ?? null;

  // Leaving the token clears the dismissal, so a later `@` typed at the very
  // same index still opens. Without this, Escape would permanently poison one
  // character position in the composer.
  useEffect(() => {
    if (token === null && dismissedAt !== null) setDismissedAt(null);
  }, [token, dismissedAt]);

  /*
    The caret is moved AFTER the commit that rewrites the value, never inside
    the handler that requests it.

    The textarea is controlled, so at the moment `pick` runs the DOM still
    holds the old string and `setSelectionRange` would be clamped to its
    length. A layout effect with no dependency array runs after every commit
    and costs one null check; `requestAnimationFrame` would also work in a
    browser and is not reliably scheduled under jsdom, which is where the
    keyboard contract is tested.
  */
  useLayoutEffect(() => {
    const target = pendingCaret.current;
    if (target === null) return;
    pendingCaret.current = null;
    const element = inputRef.current;
    if (!element) return;
    element.focus();
    element.setSelectionRange(target, target);
  });

  const noteCaret = useCallback((element: HTMLTextAreaElement) => {
    setCaret(element.selectionStart ?? element.value.length);
  }, []);

  const pick = useCallback(
    (specialist: Specialist) => {
      // Re-read rather than closing over `token`: a mouse handler can fire a
      // frame after the state it was rendered from.
      const current = findMentionToken(value, caret);
      if (current === null) return;
      const next = applyMention(value, current, specialist.slug);
      setValue(next.text);
      setCaret(next.caret);
      pendingCaret.current = next.caret;
      setDismissedAt(null);
      setActiveSlug(null);
    },
    [caret, setValue, value],
  );

  const setActive = useCallback((slug: string) => setActiveSlug(slug), []);

  const handleKeyDown = useCallback(
    (event: KeyboardEvent<HTMLTextAreaElement>): boolean => {
      if (!open || token === null) return false;

      switch (event.key) {
        case "ArrowDown": {
          event.preventDefault();
          setActiveSlug(options[(activeIndex + 1) % options.length]?.slug ?? null);
          return true;
        }
        case "ArrowUp": {
          event.preventDefault();
          setActiveSlug(
            options[(activeIndex - 1 + options.length) % options.length]?.slug ?? null,
          );
          return true;
        }
        case "Enter":
        case "Tab": {
          // Shift falls through on purpose. Shift+Enter is the composer's
          // newline and Shift+Tab is backwards focus; taking either would mean
          // the popup had removed an escape hatch rather than added a
          // shortcut, and both close it anyway by moving the caret out of the
          // token.
          if (event.shiftKey || active === null) return false;
          event.preventDefault();
          pick(active);
          return true;
        }
        case "Escape": {
          event.preventDefault();
          setDismissedAt(token.start);
          setActiveSlug(null);
          // The textarea never lost focus -- the panel holds none by design --
          // so this is a no-op in the ordinary case and the guarantee in the
          // one where something else took it.
          inputRef.current?.focus();
          return true;
        }
        default:
          return false;
      }
    },
    [active, activeIndex, inputRef, open, options, pick, token],
  );

  const optionId = useCallback((slug: string) => `${listboxId}-${slug}`, [listboxId]);

  return {
    open,
    options,
    activeIndex,
    listboxId,
    optionId,
    activeDescendant: open && active ? optionId(active.slug) : undefined,
    handleKeyDown,
    noteCaret,
    pick,
    setActive,
  };
}

/**
 * The list itself. Anchored above the composer, and mounted whether or not it
 * is open -- see the header note.
 *
 * Rows are `role="option"` and not buttons, per the combobox pattern: focus
 * stays in the textarea and the active row is named by
 * `aria-activedescendant`. They carry `min-h-11` anyway, because A8's
 * threshold is about fingers rather than about tag names.
 */
export default function MentionPopup({ state }: { state: MentionState }) {
  const { open, options, activeIndex, listboxId, optionId, pick, setActive } = state;

  return (
    <div
      data-testid="mention-popup"
      className={`${
        open ? "flex" : "hidden"
      } absolute inset-x-0 bottom-full z-30 mb-2 flex-col rounded-lg border border-violet-900/70 bg-slate-950 p-1 shadow-lg shadow-slate-950/60`}
    >
      <p className="px-2 pt-1 pb-1.5 text-[11px] text-slate-500">
        Answer as a specialist. Enter or Tab accepts, Esc closes.
      </p>
      <ul id={listboxId} role="listbox" aria-label="Specialists" className="min-w-0">
        {options.map((specialist, index) => {
          const activeRow = index === activeIndex;
          return (
            <li
              key={specialist.slug}
              id={optionId(specialist.slug)}
              role="option"
              aria-selected={activeRow}
              data-testid="mention-option"
              // `onMouseDown` with the default prevented, never `onClick`: a
              // click would blur the textarea first, and a blur that lands
              // before the insertion loses the caret the insertion is
              // addressed to.
              onMouseDown={(event) => {
                event.preventDefault();
                pick(specialist);
              }}
              onMouseEnter={() => setActive(specialist.slug)}
              className={`flex min-h-11 min-w-0 cursor-pointer items-center gap-2 rounded-md px-2 transition ${
                activeRow
                  ? "bg-violet-950/60 text-violet-100"
                  : "text-slate-300 hover:bg-slate-900"
              }`}
            >
              <span aria-hidden="true" className="shrink-0 text-base leading-none">
                {specialist.icon}
              </span>
              <span className="min-w-0 flex-1 truncate text-sm">{specialist.role}</span>
              {/*
                `min-w-0 truncate` and deliberately NOT `shrink-0`. The role
                carries `flex-1`, so its basis is 0 and it contributes nothing
                to shrinking -- which means a `shrink-0` slug would be the one
                thing in this row that could push it past 320px, and A7 asserts
                zero horizontal overflow there. Letting it truncate makes the
                row arithmetically incapable of overflowing at any width.
              */}
              <span className="min-w-0 truncate font-mono text-[11px] text-slate-500">
                @{specialist.slug}
              </span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
