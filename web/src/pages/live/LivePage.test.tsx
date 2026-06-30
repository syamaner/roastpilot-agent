/**
 * LivePage (#403 / #423 D81): single live-roast home at /live.
 *
 * Branches tested:
 * 1. Loading hold  — neither the start form nor the dashboard appears until health resolves.
 * 2. Active run    — DashboardPage; reload-safe guarantee.
 * 3. Sticky summary (#423) — active_run_id non-null → null this session → LiveFinishedView.
 *    a. Gate: correct view appears after the transition.
 *    b. Outcome content: stat tiles, detail link href (MEDIUM 1).
 *    c. "Start next roast" clears sticky → start form.
 *    d. Reload (fresh null) → start form (sticky is session-only).
 *    e. Navigate-away / remount → sticky does NOT persist (LOW 4).
 * 4. No run, no sticky — LiveStartView (idle / fresh session).
 * 5. Start-roast flow — POSTs, refetches health, stays on /live.
 *
 * Phase is never inferred: the only server-state read is active_run_id.
 */

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { roastKeys } from "@/hooks/queries";
import { LivePage } from "./LivePage";
import {
  FIXTURE_FINISHED_DETAIL,
  FIXTURE_FINISHED_RUN_ID,
  FIXTURE_FINISHED_STATS,
  FIXTURE_FINISHED_TELEMETRY,
} from "./liveFinishedFixture";

// --- Mutable health stub. ---
const healthState: {
  data: { active_run_id: string | null } | undefined;
  isSuccess: boolean;
  isError: boolean;
} = { data: undefined, isSuccess: false, isError: false };

// Mutable stubs for useRoast / useTelemetry — defaulting to null/undefined so
// gate-only tests don't see real data; the content-assertion tests override them.
const roastState: { data: unknown } = { data: null };
const telemetryState: { data: unknown } = { data: undefined };

vi.mock("@/hooks/queries", async () => {
  const actual = await vi.importActual<typeof import("@/hooks/queries")>("@/hooks/queries");
  const noopMutation = () => ({ mutateAsync: vi.fn(async () => undefined) });
  return {
    ...actual,
    useHealth: () => healthState,
    useBeanProfiles: () => ({ data: { profiles: [] }, isLoading: false }),
    useRoast: () => roastState,
    useTelemetry: () => telemetryState,
    useCreateBeanProfile: noopMutation,
    useUpdateBeanProfile: noopMutation,
    useDeleteBeanProfile: noopMutation,
  };
});

// Stub the destination bodies so the test asserts the gate branch, not their internals.
vi.mock("@/pages/dashboard/DashboardPage", () => ({
  DashboardPage: () => <div data-testid="dashboard-stub" />,
}));

// traceModel is stubbed for gate-only tests; the content tests override with real values.
// vi.hoisted ensures the refs are available when the hoisted vi.mock factory runs.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyFn = (...args: any[]) => any;
const traceModelMock = vi.hoisted(() => ({
  headlineStats: vi.fn<AnyFn>(() => ({
    dropTempC: null,
    developmentPercent: null,
    totalSeconds: null,
    firstCrackSeconds: null,
    firstCrackTempC: null,
    dropSeconds: null,
  })),
  toCurvePoints: vi.fn<AnyFn>(() => []),
  toCurveMarkers: vi.fn<AnyFn>(() => []),
}));
vi.mock("@/pages/detail/traceModel", () => traceModelMock);

vi.mock("@/components/shared", async () => {
  const actual = await vi.importActual<typeof import("@/components/shared")>("@/components/shared");
  return {
    ...actual,
    LiveCurve: () => <div data-testid="live-curve-stub" />,
  };
});

// Stub StartRoastForm with a minimal form that fires onStart on submit.
const startRoastMock = vi.hoisted(() => vi.fn(async () => ({ id: "run-new" })));
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, api: { ...actual.api, startRoast: startRoastMock } };
});

vi.mock("@/pages/dashboard/StartRoastForm", () => ({
  StartRoastForm: ({ onStart }: { onStart: (p: unknown) => Promise<void> }) => (
    <form
      data-testid="start-roast-form-stub"
      onSubmit={(e) => {
        e.preventDefault();
        void onStart({ name: "test", bean_origin: "Ethiopia" });
      }}
    >
      <button type="submit">Start</button>
    </form>
  ),
}));

afterEach(cleanup);
beforeEach(() => {
  healthState.data = undefined;
  healthState.isSuccess = false;
  healthState.isError = false;
  startRoastMock.mockClear();
  // Reset stubs to defaults (null data, no-op traceModel).
  roastState.data = null;
  telemetryState.data = undefined;
  traceModelMock.headlineStats.mockReturnValue({
    dropTempC: null,
    developmentPercent: null,
    totalSeconds: null,
    firstCrackSeconds: null,
    firstCrackTempC: null,
    dropSeconds: null,
  });
  traceModelMock.toCurvePoints.mockReturnValue([]);
  traceModelMock.toCurveMarkers.mockReturnValue([]);
});

function renderPage(initialPath = "/live") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const refetchSpy = vi.spyOn(client, "refetchQueries");
  const Wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[initialPath]}>
        <Routes>
          <Route path="/live" element={children} />
          <Route path="/" element={<div data-testid="home-landing" />} />
          <Route path="/roasts" element={<div data-testid="history-landing" />} />
          <Route path="/roasts/:runId" element={<div data-testid="detail-landing" />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
  const result = render(
    <Wrapper>
      <LivePage />
    </Wrapper>,
  );
  /** Force a re-render of LivePage (e.g. after mutating healthState). */
  function rerender() {
    result.rerender(
      <Wrapper>
        <LivePage />
      </Wrapper>,
    );
  }
  /** Remount a FRESH LivePage instance (simulates navigate away + back). */
  function remount() {
    result.unmount();
    render(
      <Wrapper>
        <LivePage />
      </Wrapper>,
    );
  }
  return { client, refetchSpy, rerender, remount };
}

describe("LivePage — loading hold", () => {
  it("renders the loading placeholder until health resolves", () => {
    healthState.isSuccess = false;
    healthState.data = undefined;
    renderPage();
    expect(screen.getByTestId("live-page-loading")).toBeInTheDocument();
    expect(screen.queryByTestId("dashboard-stub")).toBeNull();
    expect(screen.queryByTestId("live-start-view")).toBeNull();
  });
});

describe("LivePage — active run", () => {
  it("shows the dashboard when the server reports an active run", () => {
    healthState.isSuccess = true;
    healthState.data = { active_run_id: "run-42" };
    renderPage();
    expect(screen.getByTestId("dashboard-stub")).toBeInTheDocument();
    expect(screen.queryByTestId("live-start-view")).toBeNull();
    expect(screen.queryByTestId("live-page-loading")).toBeNull();
  });

  it("is reload-safe: a fresh render with an active run shows the dashboard (no flash)", () => {
    healthState.isSuccess = true;
    healthState.data = { active_run_id: "run-99" };
    renderPage();
    expect(screen.queryByTestId("live-page-loading")).toBeNull();
    expect(screen.queryByTestId("live-start-view")).toBeNull();
    expect(screen.getByTestId("dashboard-stub")).toBeInTheDocument();
  });
});

describe("LivePage — no active run, no sticky (idle / fresh session)", () => {
  it("shows the start-roast view when the server reports no active run and no prior run this session", () => {
    healthState.isSuccess = true;
    healthState.data = { active_run_id: null };
    renderPage();
    expect(screen.getByTestId("live-start-view")).toBeInTheDocument();
    expect(screen.queryByTestId("dashboard-stub")).toBeNull();
    expect(screen.queryByTestId("live-page-loading")).toBeNull();
    expect(screen.queryByTestId("live-finished-view")).toBeNull();
  });

  it("falls back to the start-roast view when the health fetch errors", () => {
    healthState.isSuccess = false;
    healthState.isError = true;
    healthState.data = undefined;
    renderPage();
    expect(screen.getByTestId("live-start-view")).toBeInTheDocument();
    expect(screen.queryByTestId("live-page-loading")).toBeNull();
    expect(screen.queryByTestId("dashboard-stub")).toBeNull();
  });
});

describe("LivePage — sticky finished-run summary (#423)", () => {
  it("shows LiveFinishedView when active_run_id transitions from non-null to null in the same session", async () => {
    healthState.isSuccess = true;
    healthState.data = { active_run_id: "run-42" };
    const { rerender } = renderPage();
    expect(screen.getByTestId("dashboard-stub")).toBeInTheDocument();

    // Run ends — update stub then trigger re-render.
    healthState.data = { active_run_id: null };
    rerender();

    await waitFor(() =>
      expect(screen.getByTestId("live-finished-view")).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("live-start-view")).toBeNull();
    expect(screen.queryByTestId("dashboard-stub")).toBeNull();
  });

  it("renders stat tiles and detail link with real roast + headlineStats data (MEDIUM 1)", async () => {
    // Inject real-shaped data so the rendered tile text is verifiable.
    roastState.data = FIXTURE_FINISHED_DETAIL;
    telemetryState.data = FIXTURE_FINISHED_TELEMETRY;
    // Use real headlineStats derivation from the fixture telemetry.
    const { headlineStats, toCurvePoints, toCurveMarkers } = await vi.importActual<
      typeof import("@/pages/detail/traceModel")
    >("@/pages/detail/traceModel");
    traceModelMock.headlineStats.mockImplementation(headlineStats);
    traceModelMock.toCurvePoints.mockImplementation(toCurvePoints);
    traceModelMock.toCurveMarkers.mockImplementation(toCurveMarkers);

    healthState.isSuccess = true;
    healthState.data = { active_run_id: FIXTURE_FINISHED_RUN_ID };
    const { rerender } = renderPage();
    expect(screen.getByTestId("dashboard-stub")).toBeInTheDocument();

    // Run ends.
    healthState.data = { active_run_id: null };
    rerender();

    await waitFor(() =>
      expect(screen.getByTestId("live-finished-view")).toBeInTheDocument(),
    );

    // Stat tiles render the fixture-derived values.
    expect(screen.getByTestId("stat-drop-temp")).toHaveTextContent(
      FIXTURE_FINISHED_STATS.dropTempDisplay,
    );
    expect(screen.getByTestId("stat-dev-percent")).toHaveTextContent(
      FIXTURE_FINISHED_STATS.devPercentDisplay,
    );
    expect(screen.getByTestId("stat-total-time")).toHaveTextContent(
      FIXTURE_FINISHED_STATS.totalTimeDisplay,
    );
    // Weight-loss comes from roast.data, not headlineStats.
    expect(screen.getByTestId("stat-weight-loss")).toHaveTextContent("14.8 %");

    // "View full detail" href must be the fixture run's detail route.
    expect(screen.getByTestId("live-finished-view-detail")).toHaveAttribute(
      "href",
      `/roasts/${FIXTURE_FINISHED_RUN_ID}`,
    );
  });

  it("'Start next roast' clears the sticky and returns to the start form", async () => {
    healthState.isSuccess = true;
    healthState.data = { active_run_id: "run-55" };
    const { rerender } = renderPage();
    expect(screen.getByTestId("dashboard-stub")).toBeInTheDocument();

    healthState.data = { active_run_id: null };
    rerender();

    await waitFor(() =>
      expect(screen.getByTestId("live-finished-view")).toBeInTheDocument(),
    );

    fireEvent.click(screen.getByTestId("live-finished-start-next"));

    await waitFor(() =>
      expect(screen.getByTestId("live-start-view")).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("live-finished-view")).toBeNull();
  });

  it("a reload (fresh render with null active_run_id) shows the start form, not the summary", () => {
    healthState.isSuccess = true;
    healthState.data = { active_run_id: null };
    renderPage();
    expect(screen.getByTestId("live-start-view")).toBeInTheDocument();
    expect(screen.queryByTestId("live-finished-view")).toBeNull();
  });

  it("navigate away + remount (new LivePage instance) resets sticky — finished view does not persist (LOW 4)", async () => {
    // First session: run starts and ends, sticky latch fires.
    healthState.isSuccess = true;
    healthState.data = { active_run_id: "run-77" };
    const { rerender, remount } = renderPage();
    expect(screen.getByTestId("dashboard-stub")).toBeInTheDocument();

    healthState.data = { active_run_id: null };
    rerender();

    await waitFor(() =>
      expect(screen.getByTestId("live-finished-view")).toBeInTheDocument(),
    );

    // Simulate the operator navigating away then back (unmount + fresh mount).
    // The new LivePage instance has no prior session state — sticky is gone.
    remount();

    // Post-remount: health still reports no active run; fresh session → start form.
    await waitFor(() =>
      expect(screen.getByTestId("live-start-view")).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("live-finished-view")).toBeNull();
  });
});

describe("LivePage — start-roast flow", () => {
  it("POSTs, refetches health, and stays on /live after starting a roast", async () => {
    healthState.isSuccess = true;
    healthState.data = { active_run_id: null };
    const { refetchSpy } = renderPage();
    expect(screen.getByTestId("live-start-view")).toBeInTheDocument();

    fireEvent.submit(screen.getByTestId("start-roast-form-stub"));
    await waitFor(() => expect(startRoastMock).toHaveBeenCalledTimes(1));

    await waitFor(() =>
      expect(refetchSpy).toHaveBeenCalledWith({ queryKey: roastKeys.health }),
    );

    expect(screen.queryByTestId("home-landing")).toBeNull();
  });
});
