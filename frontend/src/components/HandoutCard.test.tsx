/**
 * The failed handout card, in jsdom. Acceptance criterion A7 of
 * `new features/12-robust-handouts/04-failure-legibility.md`.
 *
 * The feature under test is one small chip naming what KIND of failure a row
 * suffered, beside the prose that was already there. So the assertions come in
 * pairs, and the pairing is the point: a chip that renders for `"timeout"` is
 * also asserted absent for `null` and for a value this build does not know,
 * because a chip that renders for everything and a chip that renders for
 * nothing both pass a single positive case.
 *
 * **The second half matters more than the first.** `error_kind` is additive and
 * nullable (`PLAN.md` section 3.4): every handout row written before this
 * change reads `null`, and every one of them must render exactly as it did
 * yesterday. That is the regression this feature is one careless `??` away
 * from, and it is invisible to every error-shaped check -- nothing throws and
 * the card renders perfectly, wearing a chip that says `null`.
 *
 * Layout facts -- that the chip sits on the same line as the error text, that
 * it does not push the retry button off a 375px viewport -- are not here.
 * jsdom computes no layout and would pass those assertions while lying;
 * `scripts/ui_check.py` measures them against a real engine.
 */

import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import HandoutCard from "./HandoutCard.tsx";
import { handouts } from "../lib/api.ts";
import type { Handout, HandoutDetail } from "../lib/types.ts";

const CREATED_AT = "2026-08-17T09:00:00.000Z";
const NOW = Date.parse(CREATED_AT) + 90_000;

/** The prose a timed-out deck actually carries, quoted from `sandbox.py:580`.
 *  Real text rather than "boom", so the "is the verbatim error still there"
 *  assertions are about the string the user reads. */
const TIMEOUT_ERROR =
  "The code ran for longer than 30s and was stopped. No files were kept.";

/** A `failed` deck by default -- the only status that renders an error at all,
 *  so every case here starts from one and overrides what it is about. */
function handout(overrides: Partial<Handout> = {}): Handout {
  return {
    id: "6f0e1c9a-4d2b-4a51-9a3f-2f7c1b8e5d04",
    kind: "deck",
    title: "Ka-band link budget",
    filename: "deck.pptx",
    mime_type:
      "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    byte_size: 0,
    status: "failed",
    origin: "recipe",
    error: TIMEOUT_ERROR,
    error_kind: null,
    attempts: null,
    conversation_id: null,
    query_id: null,
    created_at: CREATED_AT,
    ...overrides,
  };
}

/** Mounted inside a `<ul>`, as the panel mounts it. A bare `<li>` under the
 *  container renders fine and logs a nesting warning, and a test that prints a
 *  React error on every run teaches its reader to ignore them. */
function show(overrides: Partial<Handout> = {}) {
  return render(
    <ul>
      <HandoutCard
        agentId="8c1d2e3f-0a4b-4c5d-8e9f-0a1b2c3d4e5f"
        handout={handout(overrides)}
        now={NOW}
        onDelete={vi.fn()}
        onRetry={vi.fn()}
      />
    </ul>,
  );
}

const chip = () => screen.queryByTestId("handout-error-kind");
const errorText = () => screen.getByTestId("handout-error");

/**
 * The six values a handout row can carry, from `PLAN.md` section 3.4: the five
 * the sandbox already computes (`sandbox.py:214`) plus `"invalid"`, minted by
 * feature 02 for an artefact that was produced and does not open.
 *
 * Written out here rather than imported from the component, deliberately. A
 * test that iterates the component's own map asserts that the map equals
 * itself; this one fails if a value is dropped from it, which is the whole of
 * A6's frontend half.
 */
const ERROR_KINDS = ["import", "syntax", "timeout", "runtime", "output", "invalid"];

describe("HandoutCard", () => {
  it("names the failure class as a chip beside the error text", () => {
    show({ error_kind: "timeout" });

    expect(chip()).toBeVisible();
    expect(chip()).toHaveTextContent(/timed out/i);
    // Beside, never instead of. The prose says what happened; the chip says
    // what class it belongs to, and "ask for less" follows from the second.
    expect(errorText()).toHaveTextContent(TIMEOUT_ERROR);
  });

  it("gives every kind the backend can record a label of its own", () => {
    for (const kind of ERROR_KINDS) {
      const view = show({ error_kind: kind });
      expect(chip(), `no chip for error_kind="${kind}"`).toBeVisible();
      // A non-empty label, and not the raw slug echoed back: the chip exists to
      // say something a workshop attendee can act on.
      expect(chip()?.textContent?.trim()).toBeTruthy();
      view.unmount();
    }
  });

  it("renders NO chip for a row that recorded no kind, and changes nothing else", () => {
    // Every handout written before this change reads `null` here. This is the
    // regression case: the row must look exactly as it did yesterday.
    show({ error_kind: null });

    expect(chip()).not.toBeInTheDocument();
    expect(errorText()).toHaveTextContent(TIMEOUT_ERROR);
    expect(screen.getByTestId("handout-retry")).toBeVisible();
    expect(screen.getByTestId("handout-card")).toHaveAttribute("data-status", "failed");
  });

  it("renders NO chip for a kind this build has never heard of", () => {
    // Deliberately NOT the `??` fallback the other two label maps use. `kind`
    // and `origin` are what the row IS, so an unrecognised value is still worth
    // showing raw; an unclassified failure is a slug with no meaning to a
    // reader, sitting next to prose that already says what went wrong. And an
    // unmapped value is a backend bug that `deck_check.py` case 42 catches --
    // papering over it here is how it would stop being caught.
    show({ error_kind: "quantum" });

    expect(chip()).not.toBeInTheDocument();
    expect(screen.queryByText("quantum")).not.toBeInTheDocument();
  });

  it("still falls back to the recorded-no-reason string when there is no error text", () => {
    show({ error: null, error_kind: null });

    expect(errorText()).toHaveTextContent("The job failed without recording a reason.");
    expect(chip()).not.toBeInTheDocument();
  });

  it("shows the chip and the fallback string together", () => {
    // The two are independent: a job can classify a failure and still record no
    // prose for it, and losing either one would be a silent downgrade.
    show({ error: null, error_kind: "runtime" });

    expect(chip()).toBeVisible();
    expect(errorText()).toHaveTextContent("The job failed without recording a reason.");
  });

  it("shows no chip on a row that did not fail", () => {
    // A rescued run (`attempts: 2`) can carry the kind its FIRST attempt hit.
    // The chip belongs to the red card; on a `ready` row it would announce a
    // failure the user is looking at the successful output of.
    show({ status: "ready", byte_size: 27_387, error: null, error_kind: "runtime", attempts: 2 });

    expect(chip()).not.toBeInTheDocument();
    expect(screen.queryByTestId("handout-error")).not.toBeInTheDocument();
    expect(screen.getByTestId("handout-download")).toBeVisible();
  });
});

/**
 * Acceptance criterion A8 of
 * `new features/12-robust-handouts/05-deck-outline-preview.md`.
 *
 * The backend half is seven cases in `scripts/deck_check.py` (50-56) and it can
 * prove the outline is CORRECT. It cannot prove the outline SURVIVES: the field
 * is fetched on first open, rendered through `Markdown`, and a multi-line string
 * is exactly the shape a renderer collapses without erroring. Nothing throws
 * either way -- `loop.md` T2, arriving in a component.
 *
 * **The pairing is again the point.** `preview_text` was already rendered here
 * before this feature; what changed is what is written into it. So the second
 * case asserts the `null` row -- every `chart` handout, and every deck written
 * before this change -- still renders exactly as it did, which is the half a
 * careless `??` or a "no preview yet" placeholder would break invisibly.
 *
 * Layout facts are not here, for the reason the file docstring gives: jsdom
 * computes no layout and would pass those assertions while lying.
 */
describe("HandoutCard preview", () => {
  // Spies on a module-level object, so they outlive the test that set them.
  afterEach(() => vi.restoreAllMocks());

  /** The outline `validate.outline` writes for the honest three-slide fixture,
   *  quoted from `deck_check.py` case 50 rather than invented. A test whose
   *  input is "line one\nline two" measures a renderer; this one measures the
   *  string the product actually stores. */
  const DECK_OUTLINE = "3 slides\n1. Ka-band downlink\n2. Link margin\n3. Handover";
  const SOURCE = 'from pptx import Presentation\nprs.save("deck.pptx")';

  /** A `ready` deck, plus the two fields that only the detail fetch carries. */
  function detail(overrides: Partial<HandoutDetail> = {}): HandoutDetail {
    return {
      ...handout({ status: "ready", byte_size: 27_387, error: null }),
      preview_text: DECK_OUTLINE,
      source_code: SOURCE,
      ...overrides,
    };
  }

  /**
   * Render a ready deck and open its disclosure.
   *
   * The click goes on the `<summary>` because that is what the component
   * listens for: `Reveal` is a native `<details>` with no `onToggle`, and the
   * DOM `toggle` event does not bubble, so `HandoutCard` catches the click on a
   * wrapper and tests `closest("summary")`. Clicking anything else opens the
   * disclosure and never fetches -- which is a passing test over a panel that
   * shows nothing.
   */
  async function openReveal(overrides: Partial<HandoutDetail> = {}) {
    vi.spyOn(handouts, "load").mockResolvedValue(detail(overrides));
    show({ status: "ready", byte_size: 27_387, error: null });

    const reveal = screen.getByTestId("handout-reveal");
    fireEvent.click(reveal.querySelector("summary") as HTMLElement);
    // Awaited on the disclosure's own content, not on the spy: the assertion
    // worth making is that the fetch reached the DOM, and `toHaveBeenCalled`
    // passes on a response that was thrown away.
    await screen.findByTestId("handout-source");
    return reveal;
  }

  it("shows a FAILED handout its code, which is when it is most worth reading", async () => {
    // The backend now carries both attempts through the raise
    // (`HandoutFailure.source_code`) so a failed row stores the code it tried.
    // The card gated the whole disclosure on `status === "ready"` and would have
    // shipped that fix invisible: stored, returned by `HandoutDetail`, never
    // rendered. Two attempts joined by ATTEMPT_SEPARATOR is exactly what a
    // person opens a failed handout to read.
    const BOTH = `${SOURCE}

# ATTEMPT 2
${SOURCE}`;
    vi.spyOn(handouts, "load").mockResolvedValue(
      detail({ status: "failed", error: "The generated code did not run.", source_code: BOTH }),
    );
    show({ status: "failed", error: "The generated code did not run.", error_kind: "syntax" });

    const reveal = screen.getByTestId("handout-reveal");
    fireEvent.click(reveal.querySelector("summary") as HTMLElement);
    const source = await screen.findByTestId("handout-source");
    expect(source).toHaveTextContent("ATTEMPT 2");

    // The error text and its chip are still there. The disclosure is additional
    // to them, never a replacement -- a reader needs the reason AND the code.
    expect(screen.getByTestId("handout-error")).toHaveTextContent("did not run");
    expect(screen.getByTestId("handout-error-kind")).toBeVisible();
  });

  it("renders NO disclosure while a handout is still pending", () => {
    // The other half, and the reason `terminal` is the gate rather than no gate
    // at all: a running row has nothing to fetch, and the spinner already says
    // so. A disclosure here would open onto an error.
    show({ status: "pending", byte_size: 0, error: null, error_kind: null });
    expect(screen.queryByTestId("handout-reveal")).toBeNull();
  });

  it("renders a multi-line deck outline inside the Reveal", async () => {
    const reveal = await openReveal();

    const preview = within(reveal).getByTestId("handout-preview");
    // Every line, and the count line. `toHaveTextContent` normalises
    // whitespace, so this says "all four are present in order" and
    // deliberately does not say how they are laid out -- `Markdown` renders
    // `1. ...` as an ordered list, and asserting on the list markup would pin
    // the renderer rather than the product.
    expect(preview).toHaveTextContent("3 slides");
    expect(preview).toHaveTextContent("Ka-band downlink");
    expect(preview).toHaveTextContent("Link margin");
    expect(preview).toHaveTextContent("Handover");
    // The code block is still there, and still separate. `source_code` is what
    // a user reads to see the retry (both attempts are joined into it), and an
    // outline that crowded it out would trade one disclosure for another.
    expect(within(reveal).getByTestId("handout-source")).toHaveTextContent(
      "from pptx import Presentation",
    );
  });

  it("renders the Reveal exactly as today for a handout with no preview text", async () => {
    // `null` is every chart, and every deck written before this change. The
    // regression this feature is one placeholder away from.
    const reveal = await openReveal({ preview_text: null });

    expect(within(reveal).queryByTestId("handout-preview")).not.toBeInTheDocument();
    expect(within(reveal).getByTestId("handout-source")).toHaveTextContent(
      "from pptx import Presentation",
    );
    // And the "recorded nothing" line stays for the row that really has
    // nothing, rather than being shown beside a code block.
    expect(
      screen.queryByText("This handout recorded no source code or preview text."),
    ).not.toBeInTheDocument();
  });
});
