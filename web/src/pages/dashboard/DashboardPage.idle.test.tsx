/**
 * Dashboard idle ↔ active wiring for the Start-roast affordance (#158).
 *
 * Asserts the page shows the Start form ONLY when health reports no active run, and
 * the live dashboard once a run is active. The child hooks are mocked to isolate the
 * page's idle-detection branch (the form + the stream hook have their own specs).
 */

import { cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ConnectionStatus } from "@/hooks/useRoastStream";
import { DashboardPage } from "./DashboardPage";

// --- Mocks for the read-only foundation hooks the page consumes. ---
const healthState = {
  data: undefined as { active_run_id: string | null; mcp_child?: string } | undefined,
  isSuccess: false,
};

vi.mock("@/hooks/queries", async () => {
  const actual = await vi.importActual<typeof import("@/hooks/queries")>("@/hooks/queries");
  return {
    ...actual,
    useHealth: () => healthState,
    useRoast: () => ({ data: undefined }),
  };
});

const streamState: {
  status: ConnectionStatus;
  phase: string | null;
  telemetry: unknown;
  enabledActions: unknown;
  frames: unknown[];
  frameCount: number;
} = {
  status: "connecting",
  phase: null,
  telemetry: null,
  enabledActions: null,
  frames: [],
  frameCount: 0,
};

vi.mock("@/hooks/useRoastStream", () => ({
  useRoastStream: () => streamState,
}));

// The view-model folds frames; for this wiring test an empty view is enough.
vi.mock("./useDashboardEvents", () => ({
  useDashboardEvents: () => ({
    points: [],
    markers: [],
    fault: null,
    firstCrack: null,
    chargeGuidance: null,
    recovery: null,
    latestAdvisory: null,
    advisoryHistory: [],
    advisoryPaused: false,
    safetyTrail: [],
  }),
}));

function renderPage() {
  const client = new QueryClient();
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <DashboardPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(cleanup);
beforeEach(() => {
  healthState.data = undefined;
  healthState.isSuccess = false;
});

describe("DashboardPage idle/active wiring (#158)", () => {
  it("does not show the Start form before health has resolved", () => {
    healthState.isSuccess = false;
    healthState.data = undefined;
    renderPage();
    expect(screen.queryByTestId("start-roast-form")).toBeNull();
  });

  it("shows the Start form when health reports no active run", () => {
    healthState.isSuccess = true;
    healthState.data = { active_run_id: null };
    renderPage();
    expect(screen.getByTestId("dashboard-idle")).toBeInTheDocument();
    expect(screen.getByTestId("start-roast-form")).toBeInTheDocument();
    // The live dashboard is NOT mounted in the idle branch.
    expect(screen.queryByTestId("dashboard")).toBeNull();
    // The idle header shows a neutral label, not the "connecting" stream indicator
    // (there is no run to connect to) — #160 review item 3.
    expect(screen.getByTestId("idle-indicator")).toHaveTextContent(/no active roast/i);
  });

  it("shows the live dashboard (not the form) when a run is active", () => {
    healthState.isSuccess = true;
    healthState.data = { active_run_id: "run-123", mcp_child: "running" };
    renderPage();
    expect(screen.getByTestId("dashboard")).toBeInTheDocument();
    expect(screen.queryByTestId("start-roast-form")).toBeNull();
  });
});
