/**
 * The jsdom half of the create-agent wizard's coverage.
 *
 * These cases are deliberately about COPY and STATE, never about layout. jsdom
 * has no layout engine at all -- every width is zero and every grid resolves to
 * nothing -- so the cramping this change set exists to fix is structurally
 * invisible here. It was measured at 511px of content, 159px persona cards and
 * a 31px text collision while a green two-case suite ran in this file. Anything
 * geometric belongs in `scripts/wizard_check.py`, which drives a real browser.
 *
 * What jsdom IS good for is the thing a browser harness is slow at: asserting
 * that every parameter carries the words it is supposed to, on the surface the
 * user actually lands on, in three seconds rather than ninety.
 */

import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import CreateAgentWizard from "./CreateAgentWizard.tsx";
import { TUNABLES } from "../lib/tunables.ts";
import type { Template } from "../lib/types.ts";

/** One persona, with values distinct from the server defaults so a rendered
 *  number can only have come from here. `pedagogy` is null on purpose: three
 *  seeded templates predate that column, and a card that assumes it renders a
 *  hole. */
const TEMPLATE: Template = {
  id: "t1",
  slug: "lecture-qa",
  name: "Lecture Q&A",
  description: "The PRD default.",
  chunk_size: 640,
  chunk_overlap: 96,
  splitter: "markdown",
  retrieve_k: 17,
  rerank_enabled: true,
  rerank_top_n: 4,
  score_threshold: 0.55,
  max_rewrites: 1,
  persona_role: "Teaching assistant",
  pedagogy: null,
  icon: null,
  category: "general",
  system_prompt: "Ground every answer in the retrieved context.",
};

/** Walk to a step. Next is pressed rather than the rail clicked, because the
 *  rail only becomes navigable for steps already reached. */
function advanceTo(step: 2 | 3 | 4) {
  fireEvent.change(screen.getByTestId("agent-name-input"), {
    target: { value: "Topic 10" },
  });
  fireEvent.click(screen.getByTestId("wizard-next"));
  if (step === 2) return;
  fireEvent.click(screen.getByTestId("wizard-next"));
  if (step === 3) return;
  fireEvent.click(screen.getByTestId("wizard-next"));
}

describe("CreateAgentWizard", () => {
  it("focuses the required name and gates the next step until it is valid", () => {
    render(
      <CreateAgentWizard templates={[]} existingNames={["Existing"]} onCreated={vi.fn()} />,
    );

    const name = screen.getByTestId("agent-name-input");
    const next = screen.getByTestId("wizard-next");

    expect(name).toHaveFocus();
    expect(next).toBeDisabled();
    expect(screen.getByText(/Required\. Name it after the material/)).toBeVisible();

    fireEvent.change(name, { target: { value: "New agent" } });
    expect(next).toBeEnabled();

    fireEvent.change(name, { target: { value: "Existing" } });
    expect(next).toBeDisabled();
    expect(screen.getByRole("alert")).toHaveTextContent("already have an agent");
  });

  /**
   * CW2, and the case that matters most of the ten.
   *
   * The DEFAULT tuning mode is where every user lands, and before this change
   * set it rendered all ten parameters as bare label/value pairs with no
   * explanation anywhere in the flow -- the sliders that carried the help text
   * were behind a mode nobody had to open. So the surface the user reads was
   * the one surface with nothing to read.
   *
   * The 40-character floor is what stops a later rewrite satisfying this by
   * repeating the label as a caption: "Passage size, in tokens" is 23.
   */
  it("explains every parameter in the mode the user lands in", () => {
    render(
      <CreateAgentWizard templates={[TEMPLATE]} existingNames={[]} onCreated={vi.fn()} />,
    );
    advanceTo(3);

    // Not `getByTestId("tuning-sliders")` -- the point is that this is the
    // untouched, default surface.
    expect(screen.getByTestId("tuning-facts-answer")).toBeInTheDocument();

    for (const key of Object.keys(TUNABLES) as (keyof typeof TUNABLES)[]) {
      const copy = TUNABLES[key];
      const cell = document.querySelector(`[data-tunable="${copy.tag}"]`);
      expect(cell, `${key} is not rendered in the default mode`).not.toBeNull();

      // The plain-English label, and the real column name beside it. Both,
      // because this app teaches retrieval and dropping the column name hands
      // the reader two vocabularies while telling them about one.
      expect(cell!.textContent, `${key} label`).toContain(copy.label);
      expect(cell!.textContent, `${key} tag`).toContain(copy.tag);

      const help = within(cell as HTMLElement).getByTestId("fact-help");
      expect(
        (help.textContent ?? "").trim().length,
        `${key} explanation is too short to be one`,
      ).toBeGreaterThanOrEqual(40);
    }
  });

  /**
   * The persona IS the preset, so the card has to say what choosing it does.
   *
   * Asserts the numbers are DERIVED from the template row rather than held in a
   * second table: the fixture's 4-of-17 is a combination no seeded persona uses,
   * so a hardcoded summary cannot pass.
   */
  it("summarises what each persona does to the settings, from its own row", () => {
    render(
      <CreateAgentWizard templates={[TEMPLATE]} existingNames={[]} onCreated={vi.fn()} />,
    );
    advanceTo(2);

    const summary = screen.getByTestId("template-summary");
    expect(summary.textContent).toContain("4");
    expect(summary.textContent).toContain("17");
    expect(summary.textContent).toContain("640");
  });

  /**
   * The one place a display label and a stored value are allowed to differ, and
   * the one place it would be expensive to get wrong.
   *
   * `recursive` tells a user nothing about what will happen to their file, so
   * the option reads *At paragraphs*. The column must still receive
   * `"recursive"` -- a relabel that reached the wire would change how every
   * future upload is split.
   */
  it("relabels the splitter without changing what is stored", () => {
    render(
      <CreateAgentWizard templates={[TEMPLATE]} existingNames={[]} onCreated={vi.fn()} />,
    );
    advanceTo(3);
    fireEvent.click(screen.getByLabelText("Set them myself"));

    const splitter = screen.getByTestId("tuning-splitter");
    expect(splitter).toHaveAttribute("data-value", "markdown");
    expect(within(splitter).getByText("At headings")).toBeVisible();
    expect(within(splitter).getByText("At paragraphs")).toBeVisible();
    expect(within(splitter).queryByText("recursive")).toBeNull();

    fireEvent.click(screen.getByLabelText("At paragraphs"));
    expect(screen.getByTestId("tuning-splitter")).toHaveAttribute(
      "data-value",
      "recursive",
    );
  });

  /**
   * The two parameters the wizard used to lie about.
   *
   * `score_threshold`'s help said a low score made a question "a candidate for
   * rewriting" and `max_rewrites`' said "0 turns rewriting off". Neither is
   * read by any code path: `ask.py`'s own section header is
   * "# 4. Score check -- OBSERVABILITY ONLY" and both values reach exactly one
   * consumer, a trace payload. A label a user can act on that does nothing is
   * worse than an opaque one, because they believe they changed the system.
   *
   * They stay VISIBLE. The trace panel prints `score_threshold` every turn, so
   * hiding it would leave the reader meeting the number there with no
   * explanation at all.
   */
  it("files the two inert parameters honestly instead of hiding them", () => {
    render(
      <CreateAgentWizard templates={[TEMPLATE]} existingNames={[]} onCreated={vi.fn()} />,
    );
    advanceTo(3);

    const inert = screen.getByTestId("tuning-group-inert");
    expect(inert).toHaveTextContent(/changes nothing today/i);

    for (const tag of ["score_threshold", "max_rewrites"]) {
      const cell = inert.querySelector(`[data-tunable="${tag}"]`);
      expect(cell, `${tag} must stay on screen`).not.toBeNull();
      expect(cell!.textContent).not.toMatch(/candidate for rewriting|turns rewriting off/i);
    }
  });

  /**
   * The review step is the last screen before the button, so what it shows has
   * to be what gets sent.
   *
   * `raw` is asserted rather than the rendered text, because the rendered text
   * is formatted on purpose ("640 tokens", "At headings"). A case that
   * string-matched the formatted value would break the next time a unit was
   * pluralised, and would fail as though data had been lost.
   */
  it("reviews the persona's own values, unformatted underneath", () => {
    render(
      <CreateAgentWizard templates={[TEMPLATE]} existingNames={[]} onCreated={vi.fn()} />,
    );
    advanceTo(4);

    const review = screen.getByTestId("review-parameters");
    const raw = (tag: string) =>
      review.querySelector(`[data-tunable="${tag}"]`)?.getAttribute("data-value");

    expect(raw("chunk_size")).toBe("640");
    expect(raw("retrieve_k")).toBe("17");
    expect(raw("rerank_top_n")).toBe("4");
    expect(raw("splitter")).toBe("markdown");
    expect(raw("score_threshold")).toBe("0.55");
  });
});
