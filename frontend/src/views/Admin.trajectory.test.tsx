/**
 * The admin Trajectory tab -- change set 16.
 *
 * **These are the first tests the admin console has.** `04-admin-console.md`'s
 * only acceptance criterion for the console was "the existing tests stay green",
 * so nothing in `Admin.tsx` was ever exercised -- and that file is precisely
 * where "not measured" was collapsed into `0` twice, after every backend harness
 * was green.
 *
 * Every case here is a PAIR, and that is the point rather than thoroughness. A
 * panel that renders "not measured" for everything passes the unmeasured case on
 * its own; a panel that renders a number for everything passes the measured case
 * on its own. Only asserting both directions rules out a constant -- the same
 * discipline `route_specialist_check` cases 25/26 use, and the reason the
 * `0.000`-scoring judge in EVAL.md survived so long is that nobody wrote the
 * pair.
 *
 * Layout facts are not here. jsdom computes no layout and would pass those
 * assertions while lying; `scripts/ui_check.py` measures them against a real
 * engine.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { AdminTrajectory } from "../lib/types.ts";

let payload: AdminTrajectory;

vi.mock("../lib/api.ts", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api.ts")>();
  return {
    ...actual,
    admin: {
      ...actual.admin,
      // Only the tab under test is driven. Every other tab's loader is stubbed
      // to an empty shape so that mounting `Admin` does not leave unhandled
      // rejections that fail an unrelated case later in the file.
      overview: vi.fn(async () => ({}) as never),
      users: vi.fn(async () => []),
      agents: vi.fn(async () => []),
      conversations: vi.fn(async () => []),
      spend: vi.fn(async () => ({}) as never),
      evalRuns: vi.fn(async () => []),
      audit: vi.fn(async () => []),
      account: vi.fn(async () => ({}) as never),
      trajectory: vi.fn(async () => payload),
    },
  };
});

const { default: Admin } = await import("./Admin.tsx");

function trajectory(overrides: Partial<AdminTrajectory> = {}): AdminTrajectory {
  return {
    days: 30,
    turns: 9,
    goal_accuracy: { value: 7 / 9, measured: 9, total: 9 },
    tool_use_ok: { value: 0.5, measured: 4, total: 9 },
    calls_per_step: { value: 1.75, measured: 6, total: 9 },
    searched: 6,
    gap_forced: 2,
    budget_exhausted: 1,
    run_config: { tools_on: 3, tools_off: 1, not_recorded: 2 },
    agents: [
      {
        agent_name: "Kestrel Feynman",
        owner_email: "someone@example.com",
        turns: 9,
        goal_ok: 7,
        goal_measured: 9,
        tool_ok: 2,
        tool_measured: 4,
        searched: 6,
        gap_forced: 2,
      },
    ],
    ...overrides,
  };
}

async function openTrajectoryTab() {
  render(<Admin onBack={() => {}} />);
  fireEvent.click(screen.getByRole("tab", { name: "Trajectory" }));
  await waitFor(() => expect(screen.getByTestId("trajectory-panel")).toBeVisible());
}

describe("Admin -> Trajectory", () => {
  beforeEach(() => {
    payload = trajectory();
  });

  it("renders a binary metric as a pass rate, never as a mean", async () => {
    await openTrajectoryTab();

    // Scoped to the CARD. "7 / 9" also appears in the agent row below, and a
    // bare `getByText` matched both -- an ambiguity that would have resolved
    // itself the moment a second agent existed, i.e. in production rather than
    // here.
    const card = screen
      .getAllByTestId("trajectory-metric-card")
      .find((el) => el.dataset.metric === "goal_accuracy");
    expect(card).toBeDefined();
    // 7 of 9, NOT "0.78". `goal_accuracy` returns 1 or 0 per turn, so a decimal
    // would invite a reader to compare it with a faithfulness mean, which it is
    // not commensurable with.
    expect(card).toHaveTextContent("7 / 9");
    expect(screen.queryByText("0.78")).not.toBeInTheDocument();
  });

  it("renders 'not measured' rather than a zero when nothing was graded", async () => {
    payload = trajectory({
      goal_accuracy: { value: null, measured: 0, total: 9 },
    });
    await openTrajectoryTab();

    expect(screen.getByTestId("trajectory-unmeasured")).toHaveTextContent(
      "not measured",
    );
    // The failure this guards: a mean over nothing rendering as a bad score, and
    // sending a reader to fix an agent that was never measured.
    expect(screen.queryByText("0 / 0")).not.toBeInTheDocument();
  });

  it("keeps 'not recorded' distinct from tools being off", async () => {
    await openTrajectoryTab();

    const strip = screen.getByTestId("trajectory-not-recorded");
    expect(strip).toHaveTextContent("not recorded: 2");
    expect(screen.getByText("tools off: 1")).toBeVisible();
  });

  it("shows zero not-recorded runs without claiming tools were off", async () => {
    payload = trajectory({
      run_config: { tools_on: 4, tools_off: 0, not_recorded: 0 },
    });
    await openTrajectoryTab();

    expect(screen.getByTestId("trajectory-not-recorded")).toHaveTextContent(
      "not recorded: 0",
    );
    expect(screen.getByText("tools off: 0")).toBeVisible();
  });

  it("lists one row per agent with its own numerator and denominator", async () => {
    await openTrajectoryTab();

    const rows = screen.getAllByTestId("trajectory-row");
    expect(rows).toHaveLength(1);
    expect(rows[0]).toHaveTextContent("Kestrel Feynman");
    // The agent's own tool-use rate, 2 of 4 -- not the global one, and not a
    // percentage. A per-agent panel that showed the aggregate would look correct
    // on a single-agent fixture, which is why the numbers differ here.
    expect(rows[0]).toHaveTextContent("2 / 4");
  });

  it("says nothing is recorded rather than rendering an empty table", async () => {
    payload = trajectory({ agents: [] });
    await openTrajectoryTab();

    expect(screen.getByText(/Nothing recorded yet in this window/)).toBeVisible();
    expect(screen.queryAllByTestId("trajectory-row")).toHaveLength(0);
  });
});
