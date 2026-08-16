import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import EmptyAgentWorkspace from "./EmptyAgentWorkspace.tsx";

describe("EmptyAgentWorkspace", () => {
  it("replaces the unusable composer with a source-first action", () => {
    const onAddSource = vi.fn();

    render(<EmptyAgentWorkspace onAddSource={onAddSource} />);

    expect(screen.getByRole("heading", { name: "Add a source before you ask" })).toBeVisible();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Add your first source" }));
    expect(onAddSource).toHaveBeenCalledOnce();
  });
});
