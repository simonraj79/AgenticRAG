/**
 * The composer's keyboard contract, in jsdom.
 *
 * `npm test` covered neither the chat surface nor the composer before this,
 * and the popup adds the first key handling that can INTERCEPT a keystroke.
 * So the assertions here are about behaviour and not styling: what happens to
 * Enter, in each of the states Enter can arrive in.
 *
 * **The single most important one is that Enter still sends when the popup is
 * shut**, on an agent with a roster as well as on one without. That is the
 * regression the whole feature is one careless `preventDefault` away from, and
 * it is invisible to every error-shaped check -- nothing throws, no console
 * error, the page renders perfectly and the button simply stops working.
 *
 * Browser-only facts -- that the closed panel is `display: none`, that rows
 * are 44px, that nothing scrolls inside the chat column -- stay in
 * `scripts/ui_check.py`, which measures them against a real layout engine.
 * jsdom computes no layout and would pass those assertions while lying.
 */

import { useRef, useState } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import MentionPopup, { useMentions } from "./MentionPopup.tsx";

/**
 * The composer, reduced to the parts under test.
 *
 * It wires the textarea to `useMentions` exactly as `AgentChat` does --
 * including the order of the three guards in `onKeyDown` -- because the order
 * IS the contract: `isComposing` first, then the popup's refusal, then
 * Enter-to-send. A harness that reordered them would pass while the real
 * composer failed.
 */
function Composer({
  roster,
  onSend,
}: {
  roster: string[] | null;
  onSend: () => void;
}) {
  const [value, setValue] = useState("");
  const input = useRef<HTMLTextAreaElement | null>(null);
  const mentions = useMentions({ roster, value, setValue, inputRef: input });

  return (
    <div className="relative">
      <MentionPopup state={mentions} />
      <textarea
        ref={input}
        aria-label="Question"
        value={value}
        onChange={(event) => {
          setValue(event.target.value);
          mentions.noteCaret(event.target);
        }}
        onKeyUp={(event) => mentions.noteCaret(event.currentTarget)}
        onKeyDown={(event) => {
          if (event.nativeEvent.isComposing) return;
          if (mentions.handleKeyDown(event)) return;
          if (event.key !== "Enter" || event.shiftKey) return;
          event.preventDefault();
          onSend();
        }}
        aria-expanded={mentions.open ? true : undefined}
        aria-controls={mentions.open ? mentions.listboxId : undefined}
        aria-activedescendant={mentions.activeDescendant}
      />
    </div>
  );
}

const ROSTER = [
  "feynman-explainer",
  "socratic-tutor",
  "polya-coach",
  "quiz-generator",
  "reflective-coach",
];

function setup(roster: string[] | null = ROSTER) {
  const onSend = vi.fn();
  render(<Composer roster={roster} onSend={onSend} />);
  const textarea = screen.getByRole("textbox", { name: "Question" }) as HTMLTextAreaElement;
  return { onSend, textarea };
}

/** Type a whole string, the way the composer sees it: one change event whose
 *  caret sits at the end. */
function type(textarea: HTMLTextAreaElement, value: string) {
  fireEvent.change(textarea, { target: { value } });
  textarea.setSelectionRange(value.length, value.length);
  fireEvent.keyUp(textarea, { key: "a" });
}

const popup = () => screen.getByTestId("mention-popup");
const options = () => screen.queryAllByTestId("mention-option");

describe("MentionPopup", () => {
  it("stays mounted and hidden when there is nothing to suggest", () => {
    setup();
    // Mounted so `aria-controls` always resolves, hidden with `display: none`
    // rather than `visibility: hidden` -- a visible-but-transparent row is
    // still 44px tall to the layout checks.
    expect(popup()).toBeInTheDocument();
    expect(popup()).toHaveClass("hidden");
    expect(options()).toHaveLength(0);
  });

  it("opens on a bare @ at a word boundary and lists the whole roster", () => {
    const { textarea } = setup();
    type(textarea, "@");

    expect(popup()).toHaveClass("flex");
    expect(options()).toHaveLength(5);
    expect(textarea).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("listbox", { name: "Specialists" })).toBeInTheDocument();
  });

  it("does not open on an @ inside a word", () => {
    const { textarea } = setup();
    type(textarea, "mail simon@example");

    expect(popup()).toHaveClass("hidden");
    expect(textarea).not.toHaveAttribute("aria-expanded");
  });

  it("filters on slug, alias and role, and closes when nothing matches", () => {
    const { textarea } = setup();

    type(textarea, "@quiz");
    expect(options()).toHaveLength(1);
    expect(options()[0]).toHaveTextContent("Quiz writer");

    type(textarea, "@problem");
    expect(options()[0]).toHaveTextContent("Problem coach");

    // "what is @risk here" must stay literal text, not become a routing event.
    type(textarea, "what is @risk");
    expect(popup()).toHaveClass("hidden");
  });

  it("offers only what the agent's roster carries", () => {
    const { textarea } = setup(["socratic-tutor"]);
    type(textarea, "@");

    expect(options()).toHaveLength(1);
    expect(options()[0]).toHaveTextContent("Socratic tutor");
  });

  it("never opens for an agent with no roster", () => {
    const { textarea } = setup(null);
    type(textarea, "@");

    expect(popup()).toHaveClass("hidden");
    expect(options()).toHaveLength(0);
  });

  // ------------------------------------------------------------------
  // The keyboard contract
  // ------------------------------------------------------------------

  it("SENDS on Enter when the popup is shut, roster or no roster", () => {
    const { onSend, textarea } = setup();
    type(textarea, "what is the link budget?");

    fireEvent.keyDown(textarea, { key: "Enter" });
    expect(onSend).toHaveBeenCalledOnce();
  });

  it("sends on Enter after a mention has been completed", () => {
    // The trailing space is what closes the popup. If it did not, this Enter
    // would accept a suggestion instead of sending -- which is the whole
    // reason `applyMention` steps the caret past a space.
    const { onSend, textarea } = setup();
    type(textarea, "@feynman-explainer explain the link budget");

    fireEvent.keyDown(textarea, { key: "Enter" });
    expect(onSend).toHaveBeenCalledOnce();
  });

  it("does NOT send on Enter while the popup is open -- it accepts", () => {
    const { onSend, textarea } = setup();
    type(textarea, "@fey");

    fireEvent.keyDown(textarea, { key: "Enter" });
    expect(onSend).not.toHaveBeenCalled();
    expect(textarea).toHaveValue("@feynman-explainer ");
    expect(popup()).toHaveClass("hidden");
  });

  it("leaves Shift+Enter alone in both states", () => {
    const { onSend, textarea } = setup();

    type(textarea, "@fey");
    fireEvent.keyDown(textarea, { key: "Enter", shiftKey: true });
    // Neither accepted nor sent: Shift+Enter is the composer's newline and the
    // popup must not take an escape hatch away.
    expect(onSend).not.toHaveBeenCalled();
    expect(textarea).toHaveValue("@fey");

    type(textarea, "plain question");
    fireEvent.keyDown(textarea, { key: "Enter", shiftKey: true });
    expect(onSend).not.toHaveBeenCalled();
  });

  it("respects the IME guard, so Enter neither accepts nor sends mid-composition", () => {
    const { onSend, textarea } = setup();
    type(textarea, "@fey");

    fireEvent.keyDown(textarea, { key: "Enter", isComposing: true });
    expect(onSend).not.toHaveBeenCalled();
    expect(textarea).toHaveValue("@fey");
  });

  it("moves the active option with the arrow keys, wrapping in both directions", () => {
    const { textarea } = setup();
    type(textarea, "@");

    const selected = () =>
      options().findIndex((row) => row.getAttribute("aria-selected") === "true");

    expect(selected()).toBe(0);
    expect(textarea.getAttribute("aria-activedescendant")).toBe(options()[0]?.id);

    fireEvent.keyDown(textarea, { key: "ArrowDown" });
    expect(selected()).toBe(1);
    expect(textarea.getAttribute("aria-activedescendant")).toBe(options()[1]?.id);

    fireEvent.keyDown(textarea, { key: "ArrowUp" });
    fireEvent.keyDown(textarea, { key: "ArrowUp" });
    // Wrapped off the top to the last row.
    expect(selected()).toBe(4);

    fireEvent.keyDown(textarea, { key: "ArrowDown" });
    expect(selected()).toBe(0);
  });

  it("accepts the ACTIVE option, not the first one", () => {
    const { textarea } = setup();
    type(textarea, "@");

    fireEvent.keyDown(textarea, { key: "ArrowDown" });
    fireEvent.keyDown(textarea, { key: "ArrowDown" });
    fireEvent.keyDown(textarea, { key: "Enter" });

    expect(textarea).toHaveValue("@polya-coach ");
  });

  it("accepts on Tab and leaves Shift+Tab for focus", () => {
    const { textarea } = setup();

    type(textarea, "@soc");
    fireEvent.keyDown(textarea, { key: "Tab" });
    expect(textarea).toHaveValue("@socratic-tutor ");

    type(textarea, "@soc");
    fireEvent.keyDown(textarea, { key: "Tab", shiftKey: true });
    expect(textarea).toHaveValue("@soc");
  });

  it("closes on Escape, keeps focus on the textarea, and Enter then sends", () => {
    const { onSend, textarea } = setup();
    textarea.focus();
    type(textarea, "@fey");
    expect(popup()).toHaveClass("flex");

    fireEvent.keyDown(textarea, { key: "Escape" });
    expect(popup()).toHaveClass("hidden");
    expect(document.activeElement).toBe(textarea);

    fireEvent.keyDown(textarea, { key: "Enter" });
    expect(onSend).toHaveBeenCalledOnce();
  });

  it("reopens for a NEW mention after an Escape", () => {
    // Escape dismisses this mention, not the mechanism -- so a fresh `@`
    // further along the line still opens.
    const { textarea } = setup();
    type(textarea, "@fey");
    fireEvent.keyDown(textarea, { key: "Escape" });
    expect(popup()).toHaveClass("hidden");

    type(textarea, "@fey and @pol");
    expect(popup()).toHaveClass("flex");
    expect(options()[0]).toHaveTextContent("Problem coach");
  });

  it("inserts the mention at the caret, keeping the text around it", () => {
    const { textarea } = setup();
    fireEvent.change(textarea, { target: { value: "please @pol this for me" } });
    textarea.setSelectionRange(11, 11);
    fireEvent.keyUp(textarea, { key: "l" });

    expect(popup()).toHaveClass("flex");
    fireEvent.keyDown(textarea, { key: "Enter" });
    expect(textarea).toHaveValue("please @polya-coach this for me");
  });

  it("accepts a click without stealing the caret first", () => {
    const { textarea } = setup();
    type(textarea, "@");

    // `mousedown` with the default prevented, never `click`: a click blurs the
    // textarea before the insertion, and the insertion is addressed to a caret
    // in that textarea.
    fireEvent.mouseDown(options()[3]!);
    expect(textarea).toHaveValue("@quiz-generator ");
  });
});
