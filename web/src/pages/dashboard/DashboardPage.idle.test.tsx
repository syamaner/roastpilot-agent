/**
 * Dashboard idle ↔ active wiring for the Start-roast affordance (#158).
 *
 * Asserts the page shows the Start form ONLY when health reports no active run, and
 * the live dashboard once a run is active. The child hooks are mocked to isolate the
 * page's idle-detection branch (the form + the stream hook have their own specs).
 */

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ConnectionStatus } from "@/hooks/useRoastStream";
import { DashboardPage } from "./DashboardPage";

// Spy on the typed REST client so the acknowledge-fault POST can be asserted (#206).
// `vi.hoisted` lets the mock fn exist before the hoisted `vi.mock` factory runs.
const operatorActionMock = vi.hoisted(() =>
  vi.fn(async () => ({
    action: "acknowledge_fault",
    result: "accepted" as const,
    reason: "acknowledged",
    queued: true,
  })),
);
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, api: { ...actual.api, operatorAction: operatorActionMock } };
});

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
// `fault` is mutable so the #124 sticky-faulted-pin behavior can be exercised.
const viewState: { fault: unknown } = { fault: null };
vi.mock("./useDashboardEvents", () => ({
  useDashboardEvents: () => ({
    points: [],
    markers: [],
    fault: viewState.fault,
    firstCrack: null,
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
  viewState.fault = null;
  streamState.enabledActions = null;
  operatorActionMock.mockClear();
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

describe("DashboardPage faulted-run sticky banner (#124)", () => {
  it("keeps the faulted dashboard when active_run_id goes null on a refetch", () => {
    // A run is active and has faulted (the SSE fault frame is in the view).
    healthState.isSuccess = true;
    healthState.data = { active_run_id: "run-fault", mcp_child: "stopped" };
    viewState.fault = { reason: "env ceiling exceeded" };
    const { rerender } = renderPage();
    expect(screen.getByTestId("dashboard")).toBeInTheDocument();

    // The fault finalizes the run server-side and a health refetch (reconnect)
    // reports no active run. The dashboard must NOT collapse to the idle form —
    // the fault banner stays until the operator acknowledges it (#124).
    healthState.data = { active_run_id: null };
    rerender(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter>
          <DashboardPage />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(screen.getByTestId("dashboard")).toBeInTheDocument();
    expect(screen.queryByTestId("start-roast-form")).toBeNull();
  });

  it("acknowledges the fault by POSTing acknowledge_fault, then returns to idle", async () => {
    // Faulted, with a live active run (post-#206 a fault stays operable until ack).
    healthState.isSuccess = true;
    healthState.data = { active_run_id: "run-fault", mcp_child: "stopped" };
    viewState.fault = { reason: "env ceiling exceeded" };
    // #117: the affordance is driven by the server's enabled_actions mirror — the
    // faulted-phase SSE frame carries acknowledge_fault.
    streamState.enabledActions = ["acknowledge_fault", "emergency_stop"];
    renderPage();
    // The server finalises the run on acknowledgement → health reports no active run.
    healthState.data = { active_run_id: null };
    fireEvent.click(screen.getByTestId("fault-acknowledge"));
    // #206: the affordance dispatches the genuine acknowledge_fault control action.
    await waitFor(() =>
      expect(operatorActionMock).toHaveBeenCalledWith("run-fault", {
        action: "acknowledge_fault",
      }),
    );
    // Acknowledging clears the pin → no active run → idle Start form.
    expect(screen.getByTestId("start-roast-form")).toBeInTheDocument();
    expect(screen.queryByTestId("dashboard")).toBeNull();
  });

  it("hides the acknowledge affordance when the server does not enable acknowledge_fault (#117)", () => {
    // A fault is shown, but the server's enabled_actions mirror does NOT include
    // acknowledge_fault (e.g. a non-faulted phase). The banner must NOT render the
    // affordance — render-from-server, no client-side fault-only gate (D25).
    healthState.isSuccess = true;
    healthState.data = { active_run_id: "run-fault", mcp_child: "stopped" };
    viewState.fault = { reason: "env ceiling exceeded" };
    streamState.enabledActions = ["emergency_stop"]; // acknowledge_fault absent
    renderPage();
    expect(screen.getByTestId("fault-banner")).toBeInTheDocument();
    expect(screen.queryByTestId("fault-acknowledge")).toBeNull();
  });
});
