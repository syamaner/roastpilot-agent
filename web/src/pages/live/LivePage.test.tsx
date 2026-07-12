/**
 * LivePage (#403 / #423 D81, updated #523): single live-roast home at /live.
 * NEVER a form (#523 IA) — the only start-form surface is /start.
 *
 * Branches tested:
 * 1. Loading hold  — neither a summary nor the dashboard appears until health
 *    (and, for the idle path, history) resolve.
 * 2. Active run    — DashboardPage; reload-safe guarantee.
 * 3. No active run, a completed run exists — LiveFinishedView, PERSISTENT
 *    (sourced from history, survives reload):
 *    a. Gate: correct view appears after a COMPLETED transition (P2-4).
 *    b. Outcome content: stat tiles + detail link href; stats from full-res telemetry (P2-2).
 *    c. "Start next roast" links to /start (never clears local state — there
 *       is none to clear; a reload shows the same summary, #523).
 *    d. Reload (fresh mount, no session state) → the SAME summary, sourced
 *       from history (#523 — this is the behaviour change from the old
 *       session-only sticky, which fell back to a start form here).
 *    e. Faulted/aborted run: active_run_id→null does NOT show the finished
 *       summary from the SESSION-STICKY path (P2-4) — falls through to
 *       whatever the persistent history-derived state is (a prior completed
 *       run, or no-roasts).
 * 4. No active run, no completed run ever — LiveNoRoastsView (neutral, not a
 *    form), linking to /start.
 *
 * Phase is never inferred: the only server-state reads are active_run_id and
 * the roast history list (both from the server).
 */

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RoastDetail, RoastHistory, TelemetrySeries } from "@/lib/types";
import { LivePage } from "./LivePage";
import {
  FIXTURE_FINISHED_DETAIL,
  FIXTURE_FINISHED_RUN_ID,
  FIXTURE_FINISHED_STATS,
  FIXTURE_FINISHED_TELEMETRY,
} from "./liveFinishedFixture";

// --- Mutable health stub. `isFresh` models the #513 Codex follow-up
// (`useFreshHealthGate`): false while pending OR while `isSuccess` is true
// only from a stale cache entry with the genuinely fresh refetch still in
// flight — see StartRoastView.test.tsx's matching stub for the fuller doc. ---
const healthState: {
  data: { active_run_id: string | null } | undefined;
  isSuccess: boolean;
  isError: boolean;
  isFresh: boolean;
} = { data: undefined, isSuccess: false, isError: false, isFresh: false };

// Mutable history stub (#523): LivePage's persistent idle fallback.
// `isPending` models the loading-hold-for-history case.
const historyState: { data: RoastHistory | undefined; isPending: boolean } = {
  data: { runs: [] },
  isPending: false,
};

// Mutable stubs for useRoast / useTelemetry — defaulting to null/undefined so
// gate-only tests don't see real data; the content-assertion tests override them.
const roastState: { data: unknown } = { data: null };
// useTelemetry is now called twice (downsample=1 for stats, downsample=5 for curve).
// Separate stubs so tests can control each independently.
const telemetryFullState: { data: TelemetrySeries | undefined } = { data: undefined };
const telemetryCurveState: { data: TelemetrySeries | undefined } = { data: undefined };

vi.mock("@/hooks/queries", async () => {
  const actual = await vi.importActual<typeof import("@/hooks/queries")>("@/hooks/queries");
  return {
    ...actual,
    useFreshHealthGate: () => healthState,
    useHistory: () => historyState,
    useRoast: () => roastState,
    // Return full-res stub for downsample=1 (stats), curve stub for downsample=5.
    useTelemetry: (_runId: string | null, downsample = 1) =>
      downsample === 1 ? telemetryFullState : telemetryCurveState,
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

// `api.roast` is called by fetchTerminalOutcome (the session-sticky outcome
// gate, P2-3/P2-4).
const roastApiMock = vi.hoisted(() =>
  vi.fn(async (): Promise<RoastDetail> => ({
    ...(FIXTURE_FINISHED_DETAIL as RoastDetail),
    outcome: "completed",
  })),
);
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: { ...actual.api, roast: roastApiMock },
  };
});

afterEach(cleanup);
beforeEach(() => {
  healthState.data = undefined;
  healthState.isSuccess = false;
  healthState.isError = false;
  // Default to a SETTLED read (matches the overwhelming majority of this
  // file's tests, which set isSuccess/isError explicitly right after this
  // runs); the loading-hold-specific tests below override isFresh to false.
  healthState.isFresh = true;
  historyState.data = { runs: [] };
  historyState.isPending = false;
  roastApiMock.mockClear();
  // Reset stubs to defaults (null data, no-op traceModel).
  roastState.data = null;
  telemetryFullState.data = undefined;
  telemetryCurveState.data = undefined;
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
  const Wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[initialPath]}>
        <Routes>
          <Route path="/live" element={children} />
          <Route path="/start" element={<div data-testid="start-landing" />} />
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
  return { client, rerender, remount };
}

/** A minimal completed-run history row for the persistent-fallback tests. */
function historyRunFixture(id: string, outcome: "completed" | "faulted" | "aborted") {
  return {
    id,
    started_at_utc: "2026-07-01T09:00:00Z",
    completed_at_utc: "2026-07-01T09:06:00Z",
    first_crack_at_utc: null,
    agent_phase: "complete" as const,
    outcome,
    bean_origin: "Ethiopia Guji",
    bean_varietal: null,
    rating: null,
    development_percent: 15.0,
    advisor_consults: 0,
    advisor_clamped: 0,
    advisor_rejected: 0,
    advisor_failed: 0,
  };
}

describe("LivePage — loading hold", () => {
  it("renders the loading placeholder until health resolves", () => {
    healthState.isSuccess = false;
    healthState.isFresh = false;
    healthState.data = undefined;
    renderPage();
    expect(screen.getByTestId("live-page-loading")).toBeInTheDocument();
    expect(screen.queryByTestId("dashboard-stub")).toBeNull();
    expect(screen.queryByTestId("live-no-roasts-view")).toBeNull();
  });

  it("#513 Codex follow-up: holds through a stale-cache remount even though isSuccess is already true", () => {
    // The exact scenario Codex found: useHealth's shared 30s staleTime lets a
    // remount render a CACHED idle snapshot with isSuccess:true while the
    // genuinely fresh forced refetch (useFreshHealthGate) is still in
    // flight. A naive `!isSuccess` check would render the idle view (or even
    // the dashboard, if the cache happened to hold an active run) here —
    // isFresh:false is the only signal that catches it.
    healthState.isSuccess = true;
    healthState.isError = false;
    healthState.isFresh = false;
    healthState.data = { active_run_id: null }; // stale cached "idle" value
    renderPage();
    expect(screen.getByTestId("live-page-loading")).toBeInTheDocument();
    expect(screen.queryByTestId("dashboard-stub")).toBeNull();
    expect(screen.queryByTestId("live-no-roasts-view")).toBeNull();
  });

  it("holds while history is still pending, even once health is fresh and idle (#523)", () => {
    healthState.isSuccess = true;
    healthState.isError = false;
    healthState.isFresh = true;
    healthState.data = { active_run_id: null };
    historyState.data = undefined;
    historyState.isPending = true;
    renderPage();
    expect(screen.getByTestId("live-page-loading")).toBeInTheDocument();
    expect(screen.queryByTestId("live-no-roasts-view")).toBeNull();
    expect(screen.queryByTestId("live-finished-view")).toBeNull();
  });
});

describe("LivePage — active run", () => {
  it("shows the dashboard when the server reports an active run", () => {
    healthState.isSuccess = true;
    healthState.data = { active_run_id: "run-42" };
    renderPage();
    expect(screen.getByTestId("dashboard-stub")).toBeInTheDocument();
    expect(screen.queryByTestId("live-no-roasts-view")).toBeNull();
    expect(screen.queryByTestId("live-page-loading")).toBeNull();
  });

  it("is reload-safe: a fresh render with an active run shows the dashboard (no flash)", () => {
    healthState.isSuccess = true;
    healthState.data = { active_run_id: "run-99" };
    renderPage();
    expect(screen.queryByTestId("live-page-loading")).toBeNull();
    expect(screen.queryByTestId("live-no-roasts-view")).toBeNull();
    expect(screen.getByTestId("dashboard-stub")).toBeInTheDocument();
  });
});

describe("LivePage — no active run, no completed run ever (#523)", () => {
  it("shows the neutral no-roasts view, never a form, linking to /start", () => {
    healthState.isSuccess = true;
    healthState.data = { active_run_id: null };
    historyState.data = { runs: [] };
    historyState.isPending = false;
    renderPage();
    expect(screen.getByTestId("live-no-roasts-view")).toBeInTheDocument();
    expect(screen.getByTestId("live-no-roasts-start-link")).toHaveAttribute("href", "/start");
    expect(screen.queryByTestId("dashboard-stub")).toBeNull();
    expect(screen.queryByTestId("live-page-loading")).toBeNull();
    expect(screen.queryByTestId("live-finished-view")).toBeNull();
    expect(screen.queryByTestId("start-roast-form")).toBeNull();
  });

  it("#513 medium: shows a neutral status-unknown state (NEVER a state implying no run) when the health fetch errors", () => {
    // Active-run status is UNKNOWN on a persistent health error (useHealth's
    // own retry:1 already rode out a single blip) — a run could genuinely be
    // active and heating, so falling through to a state implying no run is
    // active would strand the operator without a path to the dashboard/e-stop.
    healthState.isSuccess = false;
    healthState.isError = true;
    healthState.data = undefined;
    renderPage();
    expect(screen.getByTestId("live-status-unknown")).toBeInTheDocument();
    expect(screen.queryByTestId("live-no-roasts-view")).toBeNull();
    expect(screen.queryByTestId("live-page-loading")).toBeNull();
    expect(screen.queryByTestId("dashboard-stub")).toBeNull();
  });
});

describe("LivePage — persistent last-completed summary from history (#523)", () => {
  it("shows LiveFinishedView sourced from history when idle and a completed run exists, with NO session-sticky state", () => {
    healthState.isSuccess = true;
    healthState.data = { active_run_id: null };
    historyState.data = { runs: [historyRunFixture(FIXTURE_FINISHED_RUN_ID, "completed")] };
    historyState.isPending = false;
    renderPage();
    expect(screen.getByTestId("live-finished-view")).toBeInTheDocument();
    expect(screen.queryByTestId("live-no-roasts-view")).toBeNull();
  });

  it("uses the newest completed run when history has multiple outcomes (newest-first list)", () => {
    healthState.isSuccess = true;
    healthState.data = { active_run_id: null };
    historyState.data = {
      runs: [
        historyRunFixture("run-most-recent-faulted", "faulted"),
        historyRunFixture(FIXTURE_FINISHED_RUN_ID, "completed"),
      ],
    };
    historyState.isPending = false;
    renderPage();
    expect(screen.getByTestId("live-finished-view")).toBeInTheDocument();
    expect(screen.getByTestId("live-finished-view-detail")).toHaveAttribute(
      "href",
      `/roasts/${FIXTURE_FINISHED_RUN_ID}`,
    );
  });

  it("a reload (fresh mount, no session state) shows the SAME persistent summary — survives reload (#523)", () => {
    // This is the behaviour #523 changes: pre-#523 a reload lost the
    // session-only sticky and fell back to a start form. Now it falls back to
    // the history-derived id instead.
    healthState.isSuccess = true;
    healthState.data = { active_run_id: null };
    historyState.data = { runs: [historyRunFixture(FIXTURE_FINISHED_RUN_ID, "completed")] };
    historyState.isPending = false;
    renderPage();
    expect(screen.getByTestId("live-finished-view")).toBeInTheDocument();
  });

  it("'Start next roast' links to /start (no local state to clear, #523)", () => {
    healthState.isSuccess = true;
    healthState.data = { active_run_id: null };
    historyState.data = { runs: [historyRunFixture(FIXTURE_FINISHED_RUN_ID, "completed")] };
    historyState.isPending = false;
    renderPage();
    expect(screen.getByTestId("live-finished-start-next")).toHaveAttribute("href", "/start");
  });

  it("shows LiveFinishedView when active_run_id transitions non-null → null and outcome is completed (P2-4 gate, session-sticky path)", async () => {
    // roastApiMock defaults to outcome: "completed" (set in beforeEach default).
    healthState.isSuccess = true;
    healthState.data = { active_run_id: "run-42" };
    const { rerender } = renderPage();
    expect(screen.getByTestId("dashboard-stub")).toBeInTheDocument();

    // Run ends — update stub then trigger re-render.
    healthState.data = { active_run_id: null };
    rerender();

    // The latch fires only after api.roast resolves (async fetchTerminalOutcome).
    await waitFor(() =>
      expect(screen.getByTestId("live-finished-view")).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("live-no-roasts-view")).toBeNull();
    expect(screen.queryByTestId("dashboard-stub")).toBeNull();
  });

  it("renders stat tiles using full-resolution telemetry (P2-2) and detail link (MEDIUM 1)", async () => {
    // Both telemetry stubs carry the fixture data; the test checks the rendered tiles.
    // headlineStats is called with the FULL-RES series (downsample=1 stub); the curve
    // uses the downsample=5 stub. Both are set to the same fixture here for simplicity.
    roastState.data = FIXTURE_FINISHED_DETAIL;
    telemetryFullState.data = FIXTURE_FINISHED_TELEMETRY;
    telemetryCurveState.data = FIXTURE_FINISHED_TELEMETRY;
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

    // Stat tiles render the fixture-derived values (from the full-res telemetry, P2-2).
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

  it("stats are sourced from downsample=1 telemetry even when the drop row is absent in downsample=5 (P2-2)", async () => {
    // Simulate a roast where the drop row (cooling phase) is NOT in the stride-5
    // sample but IS in the full-res series. headlineStats called with the full-res
    // stub must surface drop stats; called with the curve stub it would miss them.
    // Construct a minimal full-res series that includes the drop row:
    const fullResSeries: TelemetrySeries = {
      ...FIXTURE_FINISHED_TELEMETRY,
      downsample: 1,
      // Include all fixture points (one of which is the cooling/drop row at tick 13).
      points: FIXTURE_FINISHED_TELEMETRY.points,
    };
    // Curve series omits the drop row (simulating stride-5 miss):
    const curveSeriesNoDrop: TelemetrySeries = {
      ...FIXTURE_FINISHED_TELEMETRY,
      downsample: 5,
      points: FIXTURE_FINISHED_TELEMETRY.points.filter((p) => p.agent_phase !== "cooling"),
    };
    roastState.data = FIXTURE_FINISHED_DETAIL;
    telemetryFullState.data = fullResSeries;
    telemetryCurveState.data = curveSeriesNoDrop;
    const { headlineStats } = await vi.importActual<
      typeof import("@/pages/detail/traceModel")
    >("@/pages/detail/traceModel");
    traceModelMock.headlineStats.mockImplementation(headlineStats);

    healthState.isSuccess = true;
    healthState.data = { active_run_id: "run-p2-2" };
    const { rerender } = renderPage();
    healthState.data = { active_run_id: null };
    rerender();

    await waitFor(() =>
      expect(screen.getByTestId("live-finished-view")).toBeInTheDocument(),
    );
    // Drop temp and dev% must be present (derived from full-res series which has the cooling row).
    expect(screen.getByTestId("stat-drop-temp")).toHaveTextContent("191 °C");
    expect(screen.getByTestId("stat-dev-percent")).toHaveTextContent("18.7 %");
  });

  it("P2-3: fetches the terminal run snapshot (staleTime:0) to get the final outcome on transition", async () => {
    // The in-progress detail cache has outcome:null (stale snapshot while running).
    // fetchTerminalOutcome must call api.roast with staleTime:0 to get the FINAL state.
    // Assert that api.roast was called (not just the stale cache used).
    healthState.isSuccess = true;
    healthState.data = { active_run_id: "run-p2-3" };
    const { rerender } = renderPage();
    expect(screen.getByTestId("dashboard-stub")).toBeInTheDocument();

    healthState.data = { active_run_id: null };
    rerender();

    await waitFor(() =>
      expect(screen.getByTestId("live-finished-view")).toBeInTheDocument(),
    );
    // api.roast must have been called to determine the final outcome.
    expect(roastApiMock).toHaveBeenCalledWith("run-p2-3");
  });

  it("P2-4: a faulted run's active_run_id→null does NOT latch the session-sticky summary — falls through to no-roasts", async () => {
    // A fault finalises the run (active_run_id → null) but the outcome is "faulted",
    // not "completed". The session-sticky summary must NOT latch for it. With an
    // empty history (no prior completed run), the page falls through to the
    // persistent no-roasts view. DashboardPage owns the fault-ack flow via
    // stickyFaultedRunId (separate mechanism, not tested here).
    roastApiMock.mockResolvedValueOnce({ ...FIXTURE_FINISHED_DETAIL, outcome: "faulted" });

    healthState.isSuccess = true;
    healthState.data = { active_run_id: "run-faulted" };
    const { rerender } = renderPage();
    expect(screen.getByTestId("dashboard-stub")).toBeInTheDocument();

    healthState.data = { active_run_id: null };
    rerender();

    // Wait for api.roast to resolve (async gate) — the finished view must NOT appear.
    await waitFor(() => expect(roastApiMock).toHaveBeenCalledWith("run-faulted"));
    // After the gate resolves with a non-completed outcome and no history
    // fallback, the no-roasts view shows.
    await waitFor(() =>
      expect(screen.getByTestId("live-no-roasts-view")).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("live-finished-view")).toBeNull();
  });

  it("an aborted run's active_run_id→null does NOT latch the session-sticky summary — falls through to no-roasts", async () => {
    // Mirrors the faulted-gate test (P2-4) for the aborted outcome. Both "faulted"
    // and "aborted" are non-completed outcomes that must not latch the
    // session-sticky path (reserved for "completed").
    roastApiMock.mockResolvedValueOnce({ ...FIXTURE_FINISHED_DETAIL, outcome: "aborted" });

    healthState.isSuccess = true;
    healthState.data = { active_run_id: "run-aborted" };
    const { rerender } = renderPage();
    expect(screen.getByTestId("dashboard-stub")).toBeInTheDocument();

    healthState.data = { active_run_id: null };
    rerender();

    await waitFor(() => expect(roastApiMock).toHaveBeenCalledWith("run-aborted"));
    await waitFor(() =>
      expect(screen.getByTestId("live-no-roasts-view")).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("live-finished-view")).toBeNull();
  });

  it("a faulted run's active_run_id→null still shows the PRIOR completed run's summary if one exists in history", async () => {
    // The persistent fallback (#523) is independent of the session-sticky
    // gate: even though THIS run faulted, a genuinely completed run from
    // earlier history still surfaces as the last-completed summary.
    roastApiMock.mockResolvedValueOnce({ ...FIXTURE_FINISHED_DETAIL, outcome: "faulted" });
    historyState.data = { runs: [historyRunFixture(FIXTURE_FINISHED_RUN_ID, "completed")] };

    healthState.isSuccess = true;
    healthState.data = { active_run_id: "run-faulted-2" };
    const { rerender } = renderPage();

    healthState.data = { active_run_id: null };
    rerender();

    await waitFor(() => expect(roastApiMock).toHaveBeenCalledWith("run-faulted-2"));
    await waitFor(() =>
      expect(screen.getByTestId("live-finished-view")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("live-finished-view-detail")).toHaveAttribute(
      "href",
      `/roasts/${FIXTURE_FINISHED_RUN_ID}`,
    );
  });

  it("navigate away + remount (new LivePage instance) still shows the persistent summary from history (#523)", async () => {
    // First session: run starts and ends, session-sticky latch fires.
    healthState.isSuccess = true;
    healthState.data = { active_run_id: "run-77" };
    historyState.data = { runs: [historyRunFixture(FIXTURE_FINISHED_RUN_ID, "completed")] };
    const { rerender, remount } = renderPage();
    expect(screen.getByTestId("dashboard-stub")).toBeInTheDocument();

    healthState.data = { active_run_id: null };
    rerender();

    await waitFor(() =>
      expect(screen.getByTestId("live-finished-view")).toBeInTheDocument(),
    );

    // Simulate the operator navigating away then back (unmount + fresh mount).
    // The new LivePage instance has no session-sticky state — #523: it still
    // shows the summary, now from the persistent history fallback.
    remount();

    await waitFor(() =>
      expect(screen.getByTestId("live-finished-view")).toBeInTheDocument(),
    );
  });
});
