/**
 * The theme control.
 *
 * **One button that cycles, rather than three that select**, and the reason is
 * the 320px nav. Three icon buttons at the app's 44px tap-target convention are
 * 132px of a bar that already holds a wordmark, an account and (for admins) a
 * link -- it is the widest thing that would be added to the narrowest row in the
 * product. Cycling is normally the worse pattern because the state is invisible;
 * here it is not, because the glyph and the label both name the CURRENT choice,
 * the cycle is three long, and every step is reversible in two clicks.
 *
 * The accessible name carries both halves -- what it is now and what pressing
 * does next -- because a button labelled only "Light" tells a screen-reader user
 * the state and hides the action, and one labelled only "Switch to dark" hides
 * the state. `aria-live` is deliberately absent: the button is the thing that
 * changed and it is already focused, so a live region would double-announce.
 *
 * `system` sits between the two explicit ends so the cycle reads
 * light -> system -> dark -> light rather than jumping past the middle.
 */

import { useEffect, useState } from "react";
import {
  applyThemeChoice,
  readThemeChoice,
  THEME_CHOICES,
  THEME_LABELS,
  watchSystemTheme,
  type ThemeChoice,
} from "../lib/theme.ts";

const NEXT_CHOICE: Record<ThemeChoice, ThemeChoice> = {
  light: "system",
  system: "dark",
  dark: "light",
};

export default function ThemeToggle() {
  /*
    Seeded from storage rather than from a constant, so the button's label is
    right on the first frame. The CLASS was already applied before paint by the
    inline script in index.html -- this state is only the toggle's own view of
    the same fact, and the two are seeded from one source so they cannot start
    out disagreeing.
  */
  const [choice, setChoice] = useState<ThemeChoice>(() => readThemeChoice());

  /*
    Re-apply on mount, and re-subscribe whenever the choice changes.

    The subscription is what makes "system" mean anything after load: without it
    a user on system who changes their OS theme keeps the theme they had when
    the tab opened. `watchSystemTheme` returns a no-op unsubscribe for the two
    explicit choices, so there is no branch here -- the effect body is the same
    for all three, which is what keeps the cleanup correct.
  */
  useEffect(() => {
    applyThemeChoice(choice);
    return watchSystemTheme(choice, () => applyThemeChoice(choice));
  }, [choice]);

  const next = NEXT_CHOICE[choice];

  return (
    <button
      type="button"
      data-testid="theme-toggle"
      data-theme-choice={choice}
      onClick={() => setChoice(next)}
      title={`Theme: ${THEME_LABELS[choice]}`}
      aria-label={`Theme: ${THEME_LABELS[choice]}. Switch to ${THEME_LABELS[next].toLowerCase()}.`}
      className="inline-flex min-h-11 min-w-11 items-center justify-center rounded-md text-muted transition hover:bg-sunken hover:text-ink"
    >
      <ThemeGlyph choice={choice} />
    </button>
  );
}

/**
 * Sun, monitor, moon. Inline SVG so the page makes no third-party request and
 * the control cannot be a render-blocking fetch -- the same reasoning as the
 * wordmark and the Google mark on the login page.
 *
 * `currentColor` throughout, so the glyph inherits the button's hover and focus
 * states instead of needing its own.
 */
function ThemeGlyph({ choice }: { choice: ThemeChoice }) {
  const common = {
    width: 17,
    height: 17,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.7,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    "aria-hidden": true,
  };

  if (choice === "light") {
    return (
      <svg {...common}>
        <circle cx="12" cy="12" r="4" />
        <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
      </svg>
    );
  }

  if (choice === "dark") {
    return (
      <svg {...common}>
        <path d="M20 14.5A8.5 8.5 0 0 1 9.5 4a8.5 8.5 0 1 0 10.5 10.5z" />
      </svg>
    );
  }

  return (
    <svg {...common}>
      <rect x="2.5" y="4" width="19" height="12.5" rx="1.8" />
      <path d="M8.5 20.5h7M12 16.5v4" />
    </svg>
  );
}

/** Re-exported so a caller can render the choices without importing the lib
 *  module directly -- keeps `lib/theme.ts` a single-consumer module. */
export { THEME_CHOICES };
