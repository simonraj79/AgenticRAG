/**
 * Theme choice: the runtime half of the mechanism started in `index.html`.
 *
 * The split is deliberate and the two halves are not interchangeable. The
 * inline script in `index.html` resolves the stored choice into a class on
 * <html> BEFORE first paint -- it has to be there, because the bundle does not
 * exist yet and anything applied later is a visible flash. This module owns
 * everything after that: reading the choice back for the toggle, writing a new
 * one, and keeping "system" honest when the OS flips while the tab is open.
 *
 * `STORAGE_KEY` is therefore duplicated across exactly two files. That is the
 * cost of pre-paint resolution and it is worth paying once; it is not worth
 * paying twice, so nothing else may read this key directly.
 */

/**
 * What the user asked for -- NOT what is on screen.
 *
 * `"system"` is a real third state rather than the absence of a choice, and
 * collapsing it to a boolean is the bug this type exists to prevent: a user on
 * "system" who is currently dark, stored as `"dark"`, stops following their OS
 * at sunrise and never finds out why.
 */
export type ThemeChoice = "light" | "dark" | "system";

/** Duplicated in `index.html`. Change both or neither. */
const STORAGE_KEY = "gw-theme";

const DARK_QUERY = "(prefers-color-scheme: dark)";

function isChoice(value: unknown): value is ThemeChoice {
  return value === "light" || value === "dark" || value === "system";
}

/**
 * The stored choice, defaulting to `"system"`.
 *
 * Every access to `localStorage` in this module is wrapped, because it THROWS
 * rather than returning null when site data is blocked -- in a locked-down
 * Chrome profile, in some embedded webviews, and in the thumbnail renderers
 * that screenshot a page. An unguarded read here would take the whole app down
 * on first render, and the failure would look nothing like a storage problem.
 */
export function readThemeChoice(): ThemeChoice {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    return isChoice(stored) ? stored : "system";
  } catch {
    return "system";
  }
}

/** Whether a given choice renders dark right now. */
export function resolvesDark(choice: ThemeChoice): boolean {
  if (choice === "dark") return true;
  if (choice === "light") return false;
  try {
    return window.matchMedia(DARK_QUERY).matches;
  } catch {
    return false;
  }
}

/**
 * Put the choice on screen and remember it.
 *
 * The class is the single source of truth for what is rendered -- `index.css`
 * keys `@custom-variant dark` on it -- so this function is the only place
 * allowed to add or remove it. Storage is written second and its failure is
 * swallowed: a theme that applies but does not persist is a far better outcome
 * than a theme that neither applies nor persists because the write threw first.
 */
export function applyThemeChoice(choice: ThemeChoice): void {
  document.documentElement.classList.toggle("dark", resolvesDark(choice));
  try {
    localStorage.setItem(STORAGE_KEY, choice);
  } catch {
    /* Nothing to do. The class above already landed. */
  }
}

/**
 * Follow the OS while the choice is `"system"`.
 *
 * Returns its own unsubscribe. Callers pass the CURRENT choice and re-subscribe
 * when it changes, rather than this module holding state -- one owner of the
 * choice (the toggle component), one owner of the class (the function above).
 *
 * `addEventListener` on a MediaQueryList is the modern form; Safari before 14
 * has only `addListener`. Both are attempted because the fallback costs three
 * lines and the failure mode without it is silent -- the toggle keeps working
 * and only "system" quietly stops tracking, which nobody reports as a bug.
 */
export function watchSystemTheme(
  choice: ThemeChoice,
  onChange: () => void,
): () => void {
  if (choice !== "system") return () => {};

  let query: MediaQueryList;
  try {
    query = window.matchMedia(DARK_QUERY);
  } catch {
    return () => {};
  }

  if (typeof query.addEventListener === "function") {
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  }

  // Deprecated, and the only option on older Safari.
  query.addListener(onChange);
  return () => query.removeListener(onChange);
}

/** In choice order, for a three-way control. `system` sits in the middle so the
 *  two explicit ends read as opposites of each other rather than of it. */
export const THEME_CHOICES: readonly ThemeChoice[] = ["light", "system", "dark"];

export const THEME_LABELS: Record<ThemeChoice, string> = {
  light: "Light",
  system: "System",
  dark: "Dark",
};
