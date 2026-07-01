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
import { roastKeys } from "@/hooks/queries";
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

// The run snapshot (`useRoast`) — mutable so the #329 hydrate-onto-faulted case can
// supply a faulted `agent_phase` + `fault_reason` + `enabled_actions` (the restore /
// reload path that has NO live `fault` SSE frame).
const roastState: { data: unknown } = { data: undefined };

// A minimal RoastProfile for the snapshot fixtures — the page reads the charge band
// off `detail.data.profile`, so the snapshot must carry a profile to render.
const SNAPSHOT_PROFILE = {
  name: "Test",
  bean_origin: "Test",
  bean_varietal: null,
  bean_weight_grams: 250,
  charge_guidance_min_c: 170,
  charge_guidance_max_c: 200,
  initial_heat_percent: 80,
  initial_fan_percent: 30,
  target_drop_temp_c: 195,
  target_development_percent: 20,
};

// Stub the bean-profile library hooks too (#303): un-stubbed they pass through to
// the real impl and fire a real (failing) fetch in jsdom that only "passes" by
// timing luck. The list returns the fixtures so the idle Start form's dropdown is
// wired with real data; the mutations are no-op resolved mutateAsync stubs. The
// stub is defined inside the (hoisted) factory so it isn't referenced before init.
vi.mock("@/hooks/queries", async () => {
  const actual = await vi.importActual<typeof import("@/hooks/queries")>("@/hooks/queries");
  const { FIXTURE_BEAN_PROFILES } =
    await vi.importActual<typeof import("./beanProfileFixture")>("./beanProfileFixture");
  const noopMutation = () => ({ mutateAsync: vi.fn(async () => undefined) });
  return {
    ...actual,
    useHealth: () => healthState,
    useRoast: () => roastState,
    useBeanProfiles: () => ({ data: { profiles: FIXTURE_BEAN_PROFILES }, isLoading: false }),
    useCreateBeanProfile: noopMutation,
    useUpdateBeanProfile: noopMutation,
    useDeleteBeanProfile: noopMutation,
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

// `useFrameDrain` is called twice by DashboardPage:
//   1. useDashboardEvents (via the real impl, already mocked below)
//   2. The P2-1 run_completed → health invalidation drain (the one being tested here)
// We stub it so:
//   a. The common wiring tests get a no-op (frameCount is 0; the callback never fires).
//   b. TEST 1 can capture the registered callback and invoke it to assert the invalidation.
type DrainCb = (frame: { event: string }) => void;
let capturedDrainCallback: DrainCb | null = null;
vi.mock("@/hooks/useRoastStream", () => ({
  useRoastStream: () => streamState,
  useFrameDrain: (_frames: unknown, _frameCount: unknown, cb: DrainCb) => {
    // Capture the LAST registered callback. DashboardPage calls this hook once for
    // the P2-1 health-drain; useDashboardEvents' internal call is already stubbed
    // out above (useDashboardEvents mock), so only the P2-1 call reaches this stub.
    capturedDrainCallback = cb;
  },
}));

// The view-model folds frames; for this wiring test an empty view is enough.
// `fault` is mutable so the #124 sticky-faulted-pin behavior can be exercised.
// `snapshotFault` is the REAL helper (the page uses it for the #329 restore/reload
// banner) — only `useDashboardEvents` is stubbed; the pure synthesizer is genuine.
const viewState: { fault: unknown } = { fault: null };
vi.mock("./useDashboardEvents", async () => {
  const actual =
    await vi.importActual<typeof import("./useDashboardEvents")>("./useDashboardEvents");
  return {
    ...actual,
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
  };
});

function renderPage() {
  const client = new QueryClient();
  const result = render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <DashboardPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...result, client };
}

afterEach(cleanup);
beforeEach(() => {
  healthState.data = undefined;
  healthState.isSuccess = false;
  roastState.data = undefined;
  viewState.fault = null;
  streamState.enabledActions = null;
  streamState.phase = null;
  capturedDrainCallback = null;
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

  it("wires the bean-profile library into the idle Start form (#303)", () => {
    healthState.isSuccess = true;
    healthState.data = { active_run_id: null };
    renderPage();
    // The page passes useBeanProfiles' list (+ the CRUD mutations) to StartRoastForm,
    // so the saved-profile dropdown renders with the library — incl. the Koke seed.
    expect(screen.getByTestId("bean-profile-picker")).toBeInTheDocument();
    expect(screen.getByTestId("bean-profile-select")).toHaveTextContent(
      "Ethiopia Yirgacheffe Koke (Natural)",
    );
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

describe("DashboardPage restored/reloaded fault (#329)", () => {
  it("renders the FaultBanner + ACKNOWLEDGE from the hydrated snapshot, with NO live fault frame", () => {
    // The boot-onto-faulted / reload-while-faulted case: SSE never replays the
    // one-shot `fault` frame, so `view.fault` is null — but the snapshot hydrates
    // the faulted phase + reason + enabled_actions. The banner (and the ACKNOWLEDGE
    // affordance it hosts) must render from that server snapshot, not be stranded.
    healthState.isSuccess = true;
    healthState.data = { active_run_id: "run-fault", mcp_child: "stopped" };
    viewState.fault = null; // NO live fault frame (the bug condition)
    streamState.phase = "faulted"; // hydrated from the snapshot's agent_phase
    streamState.enabledActions = ["acknowledge_fault", "emergency_stop"];
    roastState.data = {
      id: "run-fault",
      agent_phase: "faulted",
      fault_reason: "env ceiling exceeded",
      enabled_actions: ["acknowledge_fault", "emergency_stop"],
      profile: SNAPSHOT_PROFILE,
    };
    renderPage();
    const banner = screen.getByTestId("fault-banner");
    expect(banner).toBeInTheDocument();
    // The reason comes from the snapshot's fault_reason (server-provided).
    expect(screen.getByTestId("fault-reason")).toHaveTextContent("env ceiling exceeded");
    // The operator is NOT stranded — the acknowledge affordance renders.
    expect(screen.getByTestId("fault-acknowledge")).toBeInTheDocument();
  });

  it("acknowledges a snapshot-restored fault (no live frame) via the real acknowledge_fault action", async () => {
    // The whole point of #329: the restored-fault ACKNOWLEDGE must dispatch the same
    // genuine control action as the live path, clearing the run.
    healthState.isSuccess = true;
    healthState.data = { active_run_id: "run-fault", mcp_child: "stopped" };
    viewState.fault = null;
    streamState.phase = "faulted";
    streamState.enabledActions = ["acknowledge_fault", "emergency_stop"];
    roastState.data = {
      id: "run-fault",
      agent_phase: "faulted",
      fault_reason: "env ceiling exceeded",
      enabled_actions: ["acknowledge_fault", "emergency_stop"],
      profile: SNAPSHOT_PROFILE,
    };
    renderPage();
    healthState.data = { active_run_id: null }; // server finalises on ack
    fireEvent.click(screen.getByTestId("fault-acknowledge"));
    await waitFor(() =>
      expect(operatorActionMock).toHaveBeenCalledWith("run-fault", {
        action: "acknowledge_fault",
      }),
    );
    expect(screen.getByTestId("start-roast-form")).toBeInTheDocument();
  });

  it("shows NO fault banner on a non-faulted hydrate (snapshot fallback is faulted-only)", () => {
    // A normal active run hydrates a non-faulted phase → the snapshot fallback must
    // NOT synthesize a fault (no false banner). Render-from-server: phase gates it.
    healthState.isSuccess = true;
    healthState.data = { active_run_id: "run-ok", mcp_child: "running" };
    viewState.fault = null;
    streamState.phase = "development";
    streamState.enabledActions = ["drop_beans", "emergency_stop"];
    roastState.data = {
      id: "run-ok",
      agent_phase: "development",
      fault_reason: null,
      enabled_actions: ["drop_beans", "emergency_stop"],
      profile: SNAPSHOT_PROFILE,
    };
    renderPage();
    expect(screen.getByTestId("dashboard")).toBeInTheDocument();
    expect(screen.queryByTestId("fault-banner")).toBeNull();
  });
});

describe("DashboardPage P2-1 drain callback — run_completed → health invalidation (#423)", () => {
  it("invoking the drain callback with run_completed invalidates roastKeys.health", async () => {
    // DashboardPage registers a useFrameDrain callback for the P2-1 health-invalidation.
    // The stub above captures it; here we invoke it directly to assert the behaviour,
    // bypassing the SSE buffer so the test is deterministic regardless of frame-buffer
    // timing. This tests the CAUSAL TRIGGER — the link that the two endpoint-level tests
    // (run_completed on the SSE side; sticky latch on LivePage side) don't cover.
    healthState.isSuccess = true;
    healthState.data = { active_run_id: "run-drain-test" };
    const { client } = renderPage();
    expect(screen.getByTestId("dashboard")).toBeInTheDocument();

    // The drain callback must have been registered during render.
    expect(capturedDrainCallback).not.toBeNull();

    const invalidateSpy = vi.spyOn(client, "invalidateQueries");

    // A non-terminal frame must NOT trigger invalidation.
    capturedDrainCallback!({ event: "phase_changed" });
    expect(invalidateSpy).not.toHaveBeenCalled();

    // A run_completed frame MUST trigger health invalidation.
    capturedDrainCallback!({ event: "run_completed" });
    await waitFor(() =>
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: roastKeys.health }),
    );
    expect(invalidateSpy).toHaveBeenCalledTimes(1);
  });
});
