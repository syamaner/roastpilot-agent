/**
 * State-aware `/` (#324): HomeGate routes on SERVER state — active run → the live
 * dashboard, idle → the home hub, and it holds (neither) until health resolves so
 * the hub never flashes before the active run is known. Phase is never inferred:
 * the decision reads `useHealth().active_run_id` only. The dashboard + home bodies
 * are mocked to isolate the branch (each has its own spec).
 */

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { HomeGate } from "./HomeGate";

const healthState: {
  data: { active_run_id: string | null } | undefined;
  isSuccess: boolean;
  isError: boolean;
} = { data: undefined, isSuccess: false, isError: false };

vi.mock("@/hooks/queries", () => ({
  useHealth: () => healthState,
}));

// Stub the two destinations so the test asserts the BRANCH, not their internals.
vi.mock("@/pages/dashboard/DashboardPage", () => ({
  DashboardPage: () => <div data-testid="dashboard-stub" />,
}));
vi.mock("./HomePage", () => ({
  HomePage: () => <div data-testid="home-stub" />,
}));

afterEach(cleanup);
// Reset error flag between tests so the error-state case doesn't bleed into others.
beforeEach(() => {
  healthState.isError = false;
});

describe("HomeGate state-aware `/` (#324)", () => {
  it("renders neither destination until health resolves", () => {
    healthState.isSuccess = false;
    healthState.data = undefined;
    render(<HomeGate />);
    expect(screen.getByTestId("home-gate-loading")).toBeInTheDocument();
    expect(screen.queryByTestId("home-stub")).toBeNull();
    expect(screen.queryByTestId("dashboard-stub")).toBeNull();
  });

  it("renders the home hub when the server reports no active run", () => {
    healthState.isSuccess = true;
    healthState.data = { active_run_id: null };
    render(<HomeGate />);
    expect(screen.getByTestId("home-stub")).toBeInTheDocument();
    expect(screen.queryByTestId("dashboard-stub")).toBeNull();
  });

  it("renders the live dashboard when the server reports an active run", () => {
    healthState.isSuccess = true;
    healthState.data = { active_run_id: "run-7" };
    render(<HomeGate />);
    expect(screen.getByTestId("dashboard-stub")).toBeInTheDocument();
    expect(screen.queryByTestId("home-stub")).toBeNull();
  });

  it("falls back to the home hub when the health fetch errors", () => {
    healthState.isSuccess = false;
    healthState.isError = true;
    healthState.data = undefined;
    render(<HomeGate />);
    // The hub is shown (active run is unknown — treat as idle); the blank loading
    // div must not persist so the operator is never stuck on an empty screen.
    expect(screen.getByTestId("home-stub")).toBeInTheDocument();
    expect(screen.queryByTestId("home-gate-loading")).toBeNull();
    expect(screen.queryByTestId("dashboard-stub")).toBeNull();
  });
});
