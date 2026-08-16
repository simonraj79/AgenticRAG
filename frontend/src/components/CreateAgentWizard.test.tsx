import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import CreateAgentWizard from "./CreateAgentWizard.tsx";

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
});
