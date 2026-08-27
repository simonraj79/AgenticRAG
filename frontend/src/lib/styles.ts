/**
 * The class strings every surface shares.
 *
 * This file exists because of what the design audit found. One `INPUT_CLASS`
 * constant existed, in `CreateAgentWizard`, and the two other files that use
 * the identical string had **copy-pasted** it rather than imported it. Around
 * it: two competing primary-button looks with nothing distinguishing when each
 * applied, three different borders on the secondary button, **five** different
 * tab patterns, four padding variants of one pill, and the section-label
 * treatment repeated ~20 times inline in four different spellings of the same
 * four utilities.
 *
 * None of that is carelessness -- it is the predictable result of a design
 * language that lives only as inline strings. There was nowhere to put the
 * decision, so it was retaken every time.
 *
 * So: a component names a ROLE (`BTN_PRIMARY`, `FIELD`), never a look. Two
 * rules keep it that way.
 *
 *   1. **A colour is always a token.** `bg-surface`, `text-muted`,
 *      `border-line` -- never `bg-white`, `text-slate-400`. The token layer in  (palette-check: ignore -- quoting the old design, not a class)
 *      `index.css` is what makes light and dark one codebase instead of two,
 *      and a raw palette utility silently opts out of it: it looks right in
 *      whichever theme it was written in and is unreadable in the other, with
 *      nothing raising. `scripts/palette_check.py` fails the build on one.
 *
 *   2. **Compose, do not fork.** Need a wider primary button? `${BTN_PRIMARY}
 *      w-full`. Need a different colour? That is a new constant here, with a
 *      sentence saying when to use it -- because a fork with no name is how
 *      this file's absence produced five tab patterns.
 *
 * Strings rather than a `cva`-style function on purpose: this codebase has no
 * class-variance dependency, the variants are few and flat, and a plain
 * exported constant is greppable in a way a generated string is not.
 */

// --------------------------------------------------------------------------
// Interactive
// --------------------------------------------------------------------------

/**
 * `min-h-11` is the 44px tap-target contract, asserted at three viewports by
 * `ui_check.py` A8 and again by `mention_popup_check.py`. It is on the shared
 * base rather than on each variant so a new variant cannot forget it.
 *
 * `inline-flex items-center justify-center` is not decoration either: a bare
 * `min-h-11` grows the box and leaves the label sitting at the top of it, so
 * the target gets bigger while the text appears to drift upward.
 */
const BTN_BASE =
  "inline-flex min-h-11 items-center justify-center gap-2 rounded-md text-sm font-medium transition disabled:cursor-not-allowed disabled:opacity-45";

/**
 * The affirmative action -- one per surface, at most.
 *
 * Filled with INK, not with the accent, and that is the load-bearing choice in
 * this palette. The accent means "evidence, or a way to reach some": citation
 * markers, source links, retrieval scores. Spending it on Send and Save too
 * would make it mean "interactive", which is to say nothing. Ink is unmissable
 * against paper, inverts correctly in the dark, and leaves the one chromatic
 * colour free to carry the one claim this product makes.
 */
export const BTN_PRIMARY = `${BTN_BASE} bg-ink px-4 py-2 text-inverse hover:bg-ink-hover`;

/** Everything else that is a real action. Bordered so it reads as a control on
 *  a page whose structure is already hairlines. */
export const BTN_SECONDARY = `${BTN_BASE} border border-line-strong bg-surface px-3 py-2 text-ink hover:bg-sunken`;

/** Tertiary: a control that should not compete for attention until pointed at.
 *  Toolbar actions, dismissals, "show more". */
export const BTN_QUIET = `${BTN_BASE} px-3 py-2 text-muted hover:bg-sunken hover:text-ink`;

/**
 * Confirmed destruction only -- the ARMED half of `ConfirmDeleteButton`, never
 * a resting state. A delete that looks like this before it has been confirmed
 * teaches people to click through red, which is the opposite of the point.
 *
 * `text-inverse` rather than `text-white`: in the dark theme `--gw-bad` is a  (palette-check: ignore -- quoting the old design, not a class)
 * light rose and white-on-rose fails contrast. The token inverts with it.
 */
export const BTN_DANGER = `${BTN_BASE} bg-bad px-3 py-2 text-inverse hover:opacity-90`;

/** Icon-only. `min-w-11` squares off the 44px target, which a text button gets
 *  from its own label and an icon button does not. */
export const BTN_ICON = `${BTN_BASE} min-w-11 px-0 text-muted hover:bg-sunken hover:text-ink`;

/** Compact variants, for dense rows (tables, list items) where a 44px-tall
 *  button is still required but a 44px-wide one would not fit. The height stays;
 *  only the horizontal padding and type size move. */
export const BTN_SM = "px-2.5 text-xs";

/**
 * The focus ring for a control whose real input is `sr-only`.
 *
 * **This is a bug fix, not a convenience, and the bug is invisible by
 * construction.** The pattern it repairs is a `<label>` styled as a card or a
 * segment wrapping a visually-hidden radio -- the persona picker and every
 * `Segmented` option. `sr-only` is `position: absolute` at 1px with
 * `clip: rect(0,0,0,0)`, so the global `:focus-visible` ring in `index.css`
 * lands on a clipped 1px box and is **painted where nothing is drawn**. The
 * radio genuinely has focus; the keyboard user simply cannot see which option
 * it is on. Nothing throws, no contrast check fires, and the markup looks
 * correct -- the ring is present in the stylesheet and absent from the screen.
 *
 * The repair is to move the ring to the element that IS drawn. `:has()` is what
 * makes that possible without JavaScript and without a `focus-within` fallback:
 * `focus-within` would also fire on a mouse click, which is the whole reason
 * `:focus-visible` exists and the reason this app draws no ring for pointer
 * users.
 *
 * Composed onto the VISIBLE label, never onto the input:
 *
 *     className={`${SEGMENT_LOOK} ${FOCUS_PROXY}`}
 *
 * The offset matches the global rule so a proxied ring and a real one are the
 * same object to the eye, and `outline-focus` is the same token, so it inverts
 * with the theme like everything else here.
 */
export const FOCUS_PROXY =
  "has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-offset-2 has-[:focus-visible]:outline-focus";

// --------------------------------------------------------------------------
// Form fields
// --------------------------------------------------------------------------

/**
 * The one input treatment. Replaces `INPUT_CLASS` and its two hand-copied
 * twins, plus the three mutually different `<select>` looks the audit found
 * (one of which had no focus style at all).
 *
 * `bg-field` rather than `bg-surface` or `bg-sunken` -- see the note on
 * `--gw-field` in `index.css`. The relationship that makes a field read as a
 * field inverts between themes, and one of those two tokens is invisible in one
 * theme or the other.
 *
 * `outline-none` is kept and is NOT dead: the global `:focus-visible` ring in
 * `index.css` carries `!important` and wins, so this suppresses only the
 * browser's own default outline while the border shift below supplies the
 * mouse-focus affordance that the ring deliberately does not.
 */
export const FIELD =
  "min-h-11 w-full rounded-md border border-line-strong bg-field px-3 py-2 text-sm text-ink transition outline-none placeholder:text-faint focus:border-accent";

/** A field the user has been told is wrong. Pair with `aria-invalid`. */
export const FIELD_INVALID = "border-bad focus:border-bad";

/** Multi-line. `resize-y` only -- horizontal resize breaks the grid columns it
 *  sits in, and there is no case here where widening a textarea helps. */
export const TEXTAREA = `${FIELD} resize-y`;

/** Long machine text: a system prompt, an imported golden set, a code block
 *  being edited. Mono and a step down, because these are read by structure. */
export const TEXTAREA_MONO = `${TEXTAREA} font-mono text-xs leading-relaxed`;

// --------------------------------------------------------------------------
// Containers
// --------------------------------------------------------------------------

/**
 * The panel. One radius and one border weight for every card-like object in the
 * app -- the audit found `rounded-lg` and `rounded-xl` both doing this job, on
 * the same kind of object, in different files (an Admin tile against a
 * Scorecard metric card).
 *
 * Padding is deliberately NOT included. A card in a dense table row and a card
 * that is a whole page section want different padding, and baking one in is how
 * `p-3`/`p-4`/`p-5`/`p-6` all ended up in use with no rule.
 */
const CARD_SHAPE = "rounded-lg border border-line";

/** Radius and border WIDTH only -- no border colour and no background, so a
 *  card that dresses its own selected state has nothing to fight. */
const CARD_SHAPE_BARE = "rounded-lg border";

export const CARD = `${CARD_SHAPE} bg-surface`;

/** A card the user can click or focus as a whole. */
export const CARD_INTERACTIVE = `${CARD} transition hover:border-line-strong`;

/**
 * The same card, MINUS its background, for the one case that needs to supply
 * its own.
 *
 * This exists because `${CARD} border-accent bg-accent-soft` does not do what
 * it reads like -- and it is BOTH declarations, which is why the card looked
 * entirely unselected rather than half-dressed.
 * Both are background utilities of equal specificity, so the winner is whichever
 * Tailwind emitted later -- measured in `dist`: `.bg-accent-soft` at byte 18350
 * and `.bg-surface` at 19770, so the shared one wins and the local one is
 * silently discarded. The selected persona card had carried `bg-accent-soft`
 * since it was written, under a comment describing "an accent border and an
 * accent-soft fill". Neither had ever rendered. The fill was found by eye; the
 * BORDER was found only because the regression case asserted both halves
 * separately -- a single "does it look selected" assertion would have gone
 * green on the half that was fixed first.
 *
 * The tempting fix is `bg-accent-soft!`. It wins today, is invisible in the
 * class list, and is one refactor from reverting with nothing to warn the next
 * reader that a tie exists. Removing the conflict is what `insights.md` 22c
 * says to do, and this is the removal: a card that supplies its own background
 * never inherits a competing one.
 *
 * `ROW_ACTIVE` does NOT need this -- the rows it dresses carry no background of
 * their own, which is why the same pair works there and is what made the card
 * look like it should.
 */
export const CARD_INTERACTIVE_UNFILLED = `${CARD_SHAPE_BARE} transition hover:border-line-strong`;

/** The nothing-here-yet panel. Dashed, so emptiness reads as a state rather
 *  than as a container that failed to load. */
export const CARD_EMPTY = "rounded-lg border border-dashed border-line-strong";

/** A recessed well: a code block, a preview, a read-only payload. */
export const WELL = "rounded-md border border-line bg-sunken";

// --------------------------------------------------------------------------
// Typography
// --------------------------------------------------------------------------

/**
 * The small caps-and-tracking section label, ~20 uses in 4 different spellings
 * of the same four utilities. One spelling now.
 *
 * 11px is a deliberate single value: the audit found ~10-11px written three
 * ways (`text-[0.65rem]`, `text-[10px]`, `text-[11px]`) with no rationale
 * separating them.
 */
export const EYEBROW =
  "text-[0.6875rem] font-semibold tracking-[0.08em] text-faint uppercase";

/** A form control's own label. */
export const LABEL = "text-sm font-medium text-ink";

/** The sentence under a control explaining what it does. */
export const HELP = "text-xs leading-relaxed text-muted";

/**
 * The reading surface for anything that came out of the corpus.
 *
 * Serif is a provenance signal, not a flourish: sans is the harness speaking,
 * serif is the answer it assembled from the user's documents. See the font
 * block in `index.css`. `.gw-prose` supplies the measure, the leading and the
 * descendant rules that `react-markdown`'s bare elements cannot be given a
 * class on.
 */
export const PROSE = "gw-prose";

// --------------------------------------------------------------------------
// Pills
// --------------------------------------------------------------------------

/**
 * The shared pill geometry. Every badge in the app is this plus a colour pair.
 *
 * Two shapes exist and the difference is semantic, not decorative:
 *
 *   **STATE** -- `PILL` plus one of the `*_TONE` strings below. A filled,
 *   tinted pill. Says what something IS right now: ready, indexing, failed.
 *
 *   **TAXONOMY** -- `PILL_NEUTRAL` plus a coloured dot. Says which GROUP
 *   something belongs to: a persona category, a trace event kind.
 *
 * Keeping them structurally different is what let the palette shrink from 11
 * hues to 4 plus 3. The old scheme had one class string -- `border-amber-800/60  (palette-check: ignore -- quoting the old design, not a class)
 * bg-amber-950/40 text-amber-300` -- simultaneously meaning the `indexing`  (palette-check: ignore -- quoting the old design, not a class)
 * status, the `assess` category, the `ai_suggested` provenance and the `running`
 * eval state, because every badge competed for the same small set of hues. Form
 * now separates the two families, so hue no longer has to.
 */
export const PILL =
  "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium whitespace-nowrap";

export const PILL_NEUTRAL = `${PILL} border-line bg-sunken text-muted`;

export const OK_TONE = "border-ok-line bg-ok-soft text-ok";
export const WARN_TONE = "border-warn-line bg-warn-soft text-warn";
export const BAD_TONE = "border-bad-line bg-bad-soft text-bad";
export const ACCENT_TONE = "border-accent-line bg-accent-soft text-accent";
export const NEUTRAL_TONE = "border-line bg-sunken text-muted";

/** A notice strip: the same tones at paragraph scale. Compose with a tone. */
export const NOTICE = "rounded-md border px-3 py-2 text-xs leading-relaxed";

// --------------------------------------------------------------------------
// Navigation
// --------------------------------------------------------------------------

/**
 * ONE tab treatment, for all of them.
 *
 * The audit found five: the agent view switcher, the chat rail tabs, Admin's
 * emerald underline, the conversation list row, and the nav's admin pill --
 * five looks for "this one is selected" in a single product.
 *
 * Callers append `TAB_ACTIVE` or `TAB_INACTIVE`. Selection must ALSO be carried
 * by `aria-current="page"` or `aria-selected`, never by the class alone -- the
 * class is how it looks selected, the attribute is how it IS selected.
 */
export const TAB = "min-h-11 rounded-md px-3 text-sm font-medium transition";
export const TAB_ACTIVE = "bg-sunken text-ink";
export const TAB_INACTIVE = "text-muted hover:bg-sunken hover:text-ink";

/** A row in a vertical list that can be selected -- a conversation, an eval
 *  run, a source. Left-aligned and full-width, unlike `TAB`. */
export const ROW = "w-full rounded-md border px-3 py-2 text-left transition";
export const ROW_ACTIVE = "border-accent-line bg-accent-soft";
export const ROW_INACTIVE = "border-transparent hover:bg-sunken";

/** A text link inside prose or a caption. The accent earns its meaning here:
 *  a link IS a way to reach a source. */
export const LINK =
  "text-accent underline decoration-accent-line underline-offset-2 transition hover:decoration-accent";
