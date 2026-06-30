/**
 * LivePage (#403 / #423 D81): single live-roast home at /live.
 *
 * Four server-state-driven branches tested here:
 * 1. Loading hold  — neither the start form nor the dashboard appears until health resolves.
 * 2. Active run    — DashboardPage; reload-safe guarantee.
 * 3. Sticky summary (#423) — when active_run_id transitions non-null → null in the
 *    SAME session, LiveFinishedView appears (not the start form).
 * 4. No run, no sticky — LiveStartView (idle / fresh session).
 * 5. Start-roast flow — POSTs, refetches health, stays on /live.
 *
 * Phase is never inferred: the only server-state read here is active_run_id.
 * DashboardPage + StartRoastForm bodies are stubbed to isolate the gate logic.
 */

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { roastKeys } from "@/hooks/queries";
import { LivePage } from "./LivePage";

// --- Mutable health stub. ---
const healthState: {
  data: { active_run_id: string | null } | undefined;
  isSuccess: boolean;
  isError: boolean;
} = { data: undefined, isSuccess: false, isError: false };

vi.mock("@/hooks/queries", async () => {
  const actual = await vi.importActual<typeof import("@/hooks/queries")>("@/hooks/queries");
  const noopMutation = () => ({ mutateAsync: vi.fn(async () => undefined) });
  return {
    ...actual,
    useHealth: () => healthState,
    useBeanProfiles: () => ({ data: { profiles: [] }, isLoading: false }),
    useRoast: () => ({ data: null }),
    useTelemetry: () => ({ data: undefined }),
    useCreateBeanProfile: noopMutation,
    useUpdateBeanProfile: noopMutation,
    useDeleteBeanProfile: noopMutation,
  };
});

// Stub the destination bodies so the test asserts the gate branch, not their internals.
vi.mock("@/pages/dashboard/DashboardPage", () => ({
  DashboardPage: () => <div data-testid="dashboard-stub" />,
}));
vi.mock("@/pages/detail/traceModel", () => ({
  headlineStats: () => ({
    dropTempC: null,
    developmentPercent: null,
    totalSeconds: null,
    firstCrackSeconds: null,
    firstCrackTempC: null,
    dropSeconds: null,
  }),
  toCurvePoints: () => [],
  toCurveMarkers: () => [],
}));
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
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
  const result = render(
    <Wrapper>
      <LivePage />
    </Wrapper>,
  );
  /** Force a re-render of LivePage (e.g. after mutating `healthState`). */
  function rerender() {
    result.rerender(
      <Wrapper>
        <LivePage />
      </Wrapper>,
    );
  }
  return { client, refetchSpy, rerender };
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
    // The sticky summary should NOT appear on a fresh /live with no prior run.
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
    // Start with an active run so LivePage records the run id in prevRunIdRef.
    healthState.isSuccess = true;
    healthState.data = { active_run_id: "run-42" };
    const { rerender } = renderPage();
    expect(screen.getByTestId("dashboard-stub")).toBeInTheDocument();

    // Simulate the run ending: update healthState then force a re-render so the
    // mocked useHealth returns the new value and the useEffect fires.
    healthState.data = { active_run_id: null };
    rerender();

    // LiveFinishedView should appear instead of the start form.
    await waitFor(() =>
      expect(screen.getByTestId("live-finished-view")).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("live-start-view")).toBeNull();
    expect(screen.queryByTestId("dashboard-stub")).toBeNull();
  });

  it("'Start next roast' clears the sticky and returns to the start form", async () => {
    healthState.isSuccess = true;
    healthState.data = { active_run_id: "run-55" };
    const { rerender } = renderPage();
    expect(screen.getByTestId("dashboard-stub")).toBeInTheDocument();

    // Run ends.
    healthState.data = { active_run_id: null };
    rerender();

    await waitFor(() =>
      expect(screen.getByTestId("live-finished-view")).toBeInTheDocument(),
    );

    // Operator clicks "Start next roast".
    fireEvent.click(screen.getByTestId("live-finished-start-next"));

    // Should now show the start form, not the summary.
    await waitFor(() =>
      expect(screen.getByTestId("live-start-view")).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("live-finished-view")).toBeNull();
  });

  it("a reload (fresh render with null active_run_id) shows the start form, not the summary", () => {
    // Reload: healthState already null — no prior run in this render session.
    healthState.isSuccess = true;
    healthState.data = { active_run_id: null };
    renderPage();
    // Fresh session: no sticky, so we show the start form.
    expect(screen.getByTestId("live-start-view")).toBeInTheDocument();
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

    // We stay on /live — NOT navigating away to /.
    expect(screen.queryByTestId("home-landing")).toBeNull();
  });
});
