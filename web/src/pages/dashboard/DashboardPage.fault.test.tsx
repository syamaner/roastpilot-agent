/**
 * DashboardPage fault-path behaviour (#124 / #206 / #329 / #423 / #513).
 *
 * Restored from `DashboardPage.idle.test.tsx` (#517/#523) — that file's name
 * described only ONE of its five describe blocks (the idle/active-wiring
 * suite, which really was dead and unreachable via the router). Deleting the
 * whole file alongside that dead branch also deleted four LIVE-behaviour
 * suites whose code paths still exist and still run in production:
 *   - the #124 faulted-run sticky banner (a health refetch must not drop the
 *     fault banner before the operator acknowledges it),
 *   - the #206/#117/#513 acknowledge_fault flow, including its confirm-retry
 *     loop (transient failure, exhausted retries, no-double-submit, and the
 *     unmount-mid-confirm cache-write guard),
 *   - the #329 restored/reloaded-fault snapshot fallback (booting or
 *     reloading onto an already-faulted run with no live SSE `fault` frame),
 *   - the #423 P2-1 `run_completed` → health-invalidation drain callback.
 * `stickyFaultedRunId`, `handleAcknowledgeFault`, the hydrated-snapshot fault
 * fallback (`snapshotFault`), and the drain callback all still live in
 * `DashboardPage.tsx` — only the idle/active-wiring describe (the genuinely
 * dead ~158-line branch, plus its own bean-profile/start-roast mocks) was
 * correctly removed.
 *
 * ADAPTED from the original, not blind-restored: the pre-#523 assertions
 * expected acknowledging a fault to "return to idle" — i.e. this component
 * rendering its OWN start form (`start-roast-form`). That form no longer
 * exists on this page (#523): `DashboardPage.tsx` has no idle branch of its
 * own any more and unconditionally renders the dashboard shell; `LivePage`
 * (the sole mount point) is the one that swaps this component out once
 * `active_run_id` clears. So the post-acknowledge assertions here check what
 * THIS component is actually responsible for — the acknowledge action firing,
 * the confirm loop resolving, and the sticky-faulted pin clearing (so a LATER
 * `active_run_id`→null resolves `runId` to `null` rather than staying pinned
 * to the acknowledged run) — not a form this page hasn't shown since #513.
 * The bean-profile CRUD hooks the idle branch needed are also gone from the
 * mocks below; nothing here exercises them any more.
 */

import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { CurvePoint } from "@/components/shared/LiveCurve";
import type { ConnectionStatus } from "@/hooks/useRoastStream";
import { roastKeys } from "@/hooks/queries";
import type { RoastTimeline } from "@/lib/types";
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
const healthApiMock = vi.hoisted(() =>
  vi.fn(async () => ({
    status: "ok" as const,
    version: "test",
    mcp_child: "connected" as const,
    active_run_id: "run-new" as string | null,
  })),
);
const timelineApiMock = vi.hoisted(() =>
  vi.fn<(runId: string) => Promise<RoastTimeline>>(),
);
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: {
      ...actual.api,
      operatorAction: operatorActionMock,
      health: healthApiMock,
      timeline: timelineApiMock,
    },
  };
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
const timelineRefetchMock = vi.hoisted(() => vi.fn(async () => undefined));
const timelineState: {
  data: RoastTimeline | undefined;
  refetch: typeof timelineRefetchMock;
} = {
  data: undefined,
  refetch: timelineRefetchMock,
};
const queryHookState = vi.hoisted(() => ({ useRealTimeline: false }));

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

vi.mock("@/hooks/queries", async () => {
  const actual = await vi.importActual<typeof import("@/hooks/queries")>("@/hooks/queries");
  return {
    ...actual,
    useHealth: () => healthState,
    useRoast: () => roastState,
    useTimeline: (runId: string | null) =>
      queryHookState.useRealTimeline ? actual.useTimeline(runId) : timelineState,
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

// `useFrameDrain` is called once by DashboardPage now (the S2 deletion removed the
// idle branch's own bean-profile wiring, not this drain hook) — the P2-1
// run_completed → health invalidation drain (#423). We stub it so:
//   a. The common wiring tests get a no-op (frameCount is 0; the callback never fires).
//   b. The drain-callback test can capture the registered callback and invoke it.
type DrainCb = (frame: { event: string }) => void;
let capturedDrainCallback: DrainCb | null = null;
vi.mock("@/hooks/useRoastStream", () => ({
  useRoastStream: () => streamState,
  useFrameDrain: (_frames: unknown, _frameCount: unknown, cb: DrainCb) => {
    capturedDrainCallback = cb;
  },
}));

// The view-model folds frames; for this wiring test an empty view is enough.
// `fault` is mutable so the #124 sticky-faulted-pin behavior can be exercised.
// `snapshotFault` is the REAL helper (the page uses it for the #329 restore/reload
// banner) — only `useDashboardEvents` is stubbed; the pure synthesizer is genuine.
const viewState: { fault: unknown; points: CurvePoint[] } = {
  fault: null,
  points: [],
};
vi.mock("./useDashboardEvents", async () => {
  const actual =
    await vi.importActual<typeof import("./useDashboardEvents")>("./useDashboardEvents");
  return {
    ...actual,
    useDashboardEvents: () => ({
      points: viewState.points,
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
  viewState.points = [];
  streamState.enabledActions = null;
  streamState.phase = null;
  streamState.status = "connecting";
  streamState.telemetry = null;
  capturedDrainCallback = null;
  queryHookState.useRealTimeline = false;
  timelineState.data = {
    run_id: "run-live",
    events: [],
    safety_evaluations: [],
    advisor_decisions: [],
    commands: [],
  };
  timelineRefetchMock.mockClear();
  timelineRefetchMock.mockImplementation(async () => {
    timelineState.data = {
      run_id: "run-live",
      events: [
        {
          kind: "first_crack",
          source: "mcp",
          monotonic_seconds: 1034,
          recorded_at_utc: "2026-07-26T18:02:45Z",
          payload: { source: "mcp", bean_temp_c: 196 },
        },
      ],
      safety_evaluations: [],
      advisor_decisions: [],
      commands: [],
    };
  });
  timelineApiMock.mockReset();
  timelineApiMock.mockResolvedValue({
    run_id: "run-live",
    events: [],
    safety_evaluations: [],
    advisor_decisions: [],
    commands: [],
  });
  operatorActionMock.mockClear();
  healthApiMock.mockClear();
  healthApiMock.mockImplementation(async () => ({
    status: "ok" as const,
    version: "test",
    mcp_child: "connected" as const,
    active_run_id: "run-new",
  }));
});

describe("DashboardPage FC timeline subscription barrier (#592)", () => {
  it("refreshes once on first telemetry and re-arms after reconnect", async () => {
    healthState.isSuccess = true;
    healthState.data = { active_run_id: "run-live", mcp_child: "running" };
    const rendered = renderPage();

    expect(timelineRefetchMock).not.toHaveBeenCalled();
    streamState.status = "live";
    rendered.rerender(
      <QueryClientProvider client={rendered.client}>
        <MemoryRouter>
          <DashboardPage />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    capturedDrainCallback?.({ event: "telemetry" });
    capturedDrainCallback?.({ event: "telemetry" });
    await waitFor(() => expect(timelineRefetchMock).toHaveBeenCalledTimes(1));
    rendered.rerender(
      <QueryClientProvider client={rendered.client}>
        <MemoryRouter>
          <DashboardPage />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(screen.getByTestId("bean-fc-reference")).toHaveTextContent("▲ FC 196.0 °C");

    streamState.status = "reconnecting";
    rendered.rerender(
      <QueryClientProvider client={rendered.client}>
        <MemoryRouter>
          <DashboardPage />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    streamState.status = "live";
    rendered.rerender(
      <QueryClientProvider client={rendered.client}>
        <MemoryRouter>
          <DashboardPage />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    capturedDrainCallback?.({ event: "telemetry" });
    await waitFor(() => expect(timelineRefetchMock).toHaveBeenCalledTimes(2));
  });

  it("cancels an in-flight empty read so the post-barrier FC response wins", async () => {
    const emptyTimeline: RoastTimeline = {
      run_id: "run-live",
      events: [],
      safety_evaluations: [],
      advisor_decisions: [],
      commands: [],
    };
    const persistedFirstCrack: RoastTimeline = {
      ...emptyTimeline,
      events: [
        {
          kind: "first_crack",
          source: "mcp",
          monotonic_seconds: 1034,
          recorded_at_utc: "2026-07-26T18:02:45Z",
          payload: { source: "mcp", bean_temp_c: 196 },
        },
      ],
    };
    let resolveInitialRead: (timeline: RoastTimeline) => void = () => undefined;
    const initialRead = new Promise<RoastTimeline>((resolve) => {
      resolveInitialRead = resolve;
    });
    timelineApiMock.mockReset();
    timelineApiMock
      .mockImplementationOnce(() => initialRead)
      .mockResolvedValueOnce(persistedFirstCrack);
    queryHookState.useRealTimeline = true;
    healthState.isSuccess = true;
    healthState.data = { active_run_id: "run-live", mcp_child: "running" };

    const rendered = renderPage();
    await waitFor(() => expect(timelineApiMock).toHaveBeenCalledTimes(1));

    streamState.status = "live";
    rendered.rerender(
      <QueryClientProvider client={rendered.client}>
        <MemoryRouter>
          <DashboardPage />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    capturedDrainCallback?.({ event: "telemetry" });

    await waitFor(() => expect(timelineApiMock).toHaveBeenCalledTimes(2));
    await waitFor(() =>
      expect(screen.getByTestId("bean-fc-reference")).toHaveTextContent(
        "▲ FC 196.0 °C",
      ),
    );

    // The cancelled pre-barrier request may still settle at the transport
    // layer, but TanStack must not let its stale empty response overwrite FC.
    await act(async () => {
      resolveInitialRead(emptyTimeline);
      await initialRead;
    });
    expect(screen.getByTestId("bean-fc-reference")).toHaveTextContent(
      "▲ FC 196.0 °C",
    );
    expect(timelineApiMock).toHaveBeenCalledTimes(2);
  });
});

describe("DashboardPage live readout authority (#592)", () => {
  it("uses persisted RoR only before live telemetry, then honors an explicit null", () => {
    healthState.isSuccess = true;
    healthState.data = { active_run_id: "run-live", mcp_child: "running" };
    viewState.points = [
      { t: 60, bean: 170, env: 190, ror: 8.5, heat: 65, fan: 30 },
    ];
    const rendered = renderPage();
    expect(screen.getByTestId("ror-readout")).toHaveTextContent("8.5 °C/min");

    streamState.telemetry = {
      agent_phase: "roasting_pre_first_crack",
      bean_temp_c: 171,
      env_temp_c: 191,
      bean_ror_c_per_min: null,
      env_ror_c_per_min: null,
      heat_percent: 65,
      fan_percent: 30,
      cooling_on: false,
      elapsed_seconds: 121,
      charge_elapsed_seconds: 61,
      development_elapsed_seconds: null,
      development_percent: null,
      t0_detected: true,
      first_crack_detected: false,
    };
    rendered.rerender(
      <QueryClientProvider client={rendered.client}>
        <MemoryRouter>
          <DashboardPage />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(screen.getByTestId("ror-readout")).toHaveTextContent("— °C/min");
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
    expect(screen.getByTestId("fault-banner")).toBeInTheDocument();

    // The fault finalizes the run server-side and a health refetch (reconnect)
    // reports no active run. The dashboard must NOT lose the fault banner —
    // it stays until the operator acknowledges it (#124) via the sticky pin.
    healthState.data = { active_run_id: null };
    rerender(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter>
          <DashboardPage />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(screen.getByTestId("dashboard")).toBeInTheDocument();
    expect(screen.getByTestId("fault-banner")).toBeInTheDocument();
  });

  it("#523: acknowledges the fault by POSTing acknowledge_fault, then confirms the run cleared — LivePage (not this component) owns the swap away", async () => {
    // Faulted, with a live active run (post-#206 a fault stays operable until ack).
    healthState.isSuccess = true;
    healthState.data = { active_run_id: "run-fault", mcp_child: "stopped" };
    viewState.fault = { reason: "env ceiling exceeded" };
    // #117: the affordance is driven by the server's enabled_actions mirror — the
    // faulted-phase SSE frame carries acknowledge_fault.
    streamState.enabledActions = ["acknowledge_fault", "emergency_stop"];
    // #513: the confirm loop polls api.health() directly — resolve it with
    // active_run_id: null so the loop terminates on its first attempt instead
    // of spinning through its full retry budget in the background (the default
    // beforeEach mock resolves active_run_id: "run-new", which never satisfies
    // the confirm condition and would leak a live retry loop past this test,
        // polluting a LATER test's mockRejectedValueOnce/mockResolvedValueOnce
    // queue on the same shared mock).
    healthApiMock.mockResolvedValue({
      status: "ok",
      version: "test",
      mcp_child: "connected",
      active_run_id: null,
    });
    renderPage();
    fireEvent.click(screen.getByTestId("fault-acknowledge"));
    // #206: the affordance dispatches the genuine acknowledge_fault control action.
    await waitFor(() =>
      expect(operatorActionMock).toHaveBeenCalledWith("run-fault", {
        action: "acknowledge_fault",
      }),
    );
    // #523: DashboardPage has no idle branch of its own any more — it never
    // shows a start form. What THIS component owns is: the action dispatches,
    // and the confirm loop resolves against the real api.health(). The swap
    // away from the dashboard (to LivePage's own idle/summary state) happens
    // one level up, once LivePage's own `active_run_id` read goes null — out
    // of scope for a DashboardPage-only render test.
    await waitFor(() => expect(healthApiMock).toHaveBeenCalled());
    expect(screen.getByTestId("dashboard")).toBeInTheDocument();
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

  it("#513: disables the acknowledge button while confirming (no double-submit)", async () => {
    healthState.isSuccess = true;
    healthState.data = { active_run_id: "run-fault", mcp_child: "stopped" };
    viewState.fault = { reason: "env ceiling exceeded" };
    streamState.enabledActions = ["acknowledge_fault", "emergency_stop"];
    // api.health() stalls only long enough to assert the pending state, then
    // resolves — a truly-eternal pending promise would leak the component's
    // background retry loop past this test's cleanup and pollute the NEXT
    // test's mock queue (its retry keeps firing and consuming that test's
    // mockRejectedValueOnce/mockResolvedValueOnce slots before it even runs).
    let resolveHealth: (v: {
      status: "ok";
      version: string;
      mcp_child: "connected";
      active_run_id: string | null;
    }) => void = () => {};
    healthApiMock.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveHealth = resolve;
        }),
    );
    renderPage();

    fireEvent.click(screen.getByTestId("fault-acknowledge"));
    await waitFor(() => expect(screen.getByTestId("fault-acknowledge")).toBeDisabled());
    expect(screen.getByTestId("fault-acknowledge")).toHaveTextContent(/acknowledging/i);

    // Let the pending confirm resolve so the component's retry loop terminates
    // cleanly before the next test runs.
    resolveHealth({
      status: "ok",
      version: "test",
      mcp_child: "connected",
      active_run_id: null,
    });
    await waitFor(() => expect(screen.getByTestId("fault-acknowledge")).toBeEnabled());
  });

  it("#513: retries a transient post-acknowledge api.health() failure, then confirms", async () => {
    healthState.isSuccess = true;
    healthState.data = { active_run_id: "run-fault", mcp_child: "stopped" };
    viewState.fault = { reason: "env ceiling exceeded" };
    streamState.enabledActions = ["acknowledge_fault", "emergency_stop"];
    healthApiMock
      .mockRejectedValueOnce(new Error("503 starting up"))
      .mockResolvedValueOnce({
        status: "ok",
        version: "test",
        mcp_child: "connected",
        active_run_id: null,
      });
    renderPage();

    fireEvent.click(screen.getByTestId("fault-acknowledge"));
    await waitFor(() =>
      expect(operatorActionMock).toHaveBeenCalledWith("run-fault", { action: "acknowledge_fault" }),
    );
    await waitFor(() => expect(healthApiMock).toHaveBeenCalledTimes(2), { timeout: 5000 });
    expect(screen.queryByTestId("fault-acknowledge-confirm-failed")).toBeNull();
    // The button must re-enable, not stay stuck on "Acknowledging…".
    await waitFor(() => expect(screen.getByTestId("fault-acknowledge")).toBeEnabled());
  }, 8000);

  it("#513: shows a visible failure note (never a silently-stale banner) when every confirm attempt fails, and stays retryable", async () => {
    healthState.isSuccess = true;
    healthState.data = { active_run_id: "run-fault", mcp_child: "stopped" };
    viewState.fault = { reason: "env ceiling exceeded" };
    streamState.enabledActions = ["acknowledge_fault", "emergency_stop"];
    healthApiMock.mockRejectedValue(new Error("still down"));
    renderPage();

    fireEvent.click(screen.getByTestId("fault-acknowledge"));
    await waitFor(() => expect(screen.getByTestId("fault-acknowledge")).toBeDisabled());
    await waitFor(
      () => expect(screen.getByTestId("fault-acknowledge-confirm-failed")).toBeInTheDocument(),
      { timeout: 10000 },
    );
    // The FaultBanner is still visibly present (never a stranded blank state) and
    // the button re-enables for a manual retry — never permanently stuck.
    expect(screen.getByTestId("fault-banner")).toBeInTheDocument();
    expect(screen.getByTestId("fault-acknowledge")).toBeEnabled();
  }, 12000);

  it("#513: unmounting mid-confirm never lets the orphaned acknowledge loop write the cache", async () => {
    healthState.isSuccess = true;
    healthState.data = { active_run_id: "run-fault", mcp_child: "stopped" };
    viewState.fault = { reason: "env ceiling exceeded" };
    streamState.enabledActions = ["acknowledge_fault", "emergency_stop"];
    let resolveHealth: (v: {
      status: "ok";
      version: string;
      mcp_child: "connected";
      active_run_id: string | null;
    }) => void = () => {};
    healthApiMock.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveHealth = resolve;
        }),
    );
    const { client, unmount } = renderPage();
    const setQueryDataSpy = vi.spyOn(client, "setQueryData");

    fireEvent.click(screen.getByTestId("fault-acknowledge"));
    await waitFor(() =>
      expect(operatorActionMock).toHaveBeenCalledWith("run-fault", { action: "acknowledge_fault" }),
    );
    await waitFor(() => expect(healthApiMock).toHaveBeenCalledTimes(1));
    setQueryDataSpy.mockClear();

    // Unmount WHILE the confirm attempt is still in flight.
    unmount();
    resolveHealth({
      status: "ok",
      version: "test",
      mcp_child: "connected",
      active_run_id: null,
    });
    await new Promise((r) => setTimeout(r, 50));

    expect(setQueryDataSpy).not.toHaveBeenCalled();
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

  it("#523: acknowledges a snapshot-restored fault (no live frame) via the real acknowledge_fault action", async () => {
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
    // #513: resolve the confirm loop so it terminates on its first attempt
    // rather than leaking a background retry loop past this test — see the
    // matching comment on the earlier acknowledge test.
    healthApiMock.mockResolvedValue({
      status: "ok",
      version: "test",
      mcp_child: "connected",
      active_run_id: null,
    });
    renderPage();
    fireEvent.click(screen.getByTestId("fault-acknowledge"));
    await waitFor(() =>
      expect(operatorActionMock).toHaveBeenCalledWith("run-fault", {
        action: "acknowledge_fault",
      }),
    );
    // #523: as above — this component's responsibility ends at dispatching the
    // real action and confirming the health transition; it never renders its
    // own idle form (LivePage owns that swap).
    await waitFor(() => expect(healthApiMock).toHaveBeenCalled());
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
  it("invoking the drain callback with run_completed invalidates roastKeys.health and roastKeys.history", async () => {
    // DashboardPage registers a useFrameDrain callback for the P2-1 health-invalidation.
    // The stub above captures it; here we invoke it directly to assert the behaviour,
    // bypassing the SSE buffer so the test is deterministic regardless of frame-buffer
    // timing. This tests the CAUSAL TRIGGER — the link that the two endpoint-level tests
    // (run_completed on the SSE side; sticky latch on LivePage side) don't cover.
    //
    // #523: DashboardPage.tsx now ALSO invalidates roastKeys.history alongside
    // roastKeys.health on this same trigger (LivePage's persistent last-completed
    // fallback reads useHistory(), whose default 30s staleTime would otherwise
    // leave the just-finished run out of it) — asserted alongside the pre-existing
    // health invalidation, not a separate restoration.
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

    // A run_completed frame MUST trigger BOTH health and history invalidation.
    capturedDrainCallback!({ event: "run_completed" });
    await waitFor(() =>
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: roastKeys.health }),
    );
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: roastKeys.history });
    expect(invalidateSpy).toHaveBeenCalledTimes(2);
  });
});
