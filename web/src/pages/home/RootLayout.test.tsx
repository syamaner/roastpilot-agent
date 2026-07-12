/**
 * The persistent nav renders on EVERY route and state (#523) — including a
 * loading hold and an error view, not just the "happy path" form/dashboard
 * states each page's own spec already covers.
 *
 * `RootLayout` mounts `NavBar` above the routed `Outlet` unconditionally (see
 * RootLayout.tsx) — this is a structural guarantee, not per-page plumbing, so
 * one test at the layout level proves it for every nested route rather than
 * duplicating the assertion in each page's own spec. `StartRoastView` is used
 * as the driven page because it already has three distinct server-state
 * branches (loading hold, status-unknown error, and the form) reachable via a
 * controllable health mock — the exact three states #523 calls out by name.
 */

import { cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { RootLayout } from "./RootLayout";
import { StartRoastView } from "./StartRoastView";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, api: { ...actual.api, startRoast: vi.fn() } };
});

// One mutable health stub feeds BOTH NavBar's `useHealth()` and
// StartRoastView's `useFreshHealthGate()` — the two hooks this route tree
// actually calls — so a single state change drives the page branch and the
// nav's active-run slot together, exactly like the real app.
const healthState: {
  data: { active_run_id: string | null } | undefined;
  isSuccess: boolean;
  isError: boolean;
  isFresh: boolean;
} = { data: undefined, isSuccess: false, isError: false, isFresh: false };

vi.mock("@/hooks/queries", async () => {
  const actual = await vi.importActual<typeof import("@/hooks/queries")>("@/hooks/queries");
  const { FIXTURE_BEAN_PROFILES } = await vi.importActual<
    typeof import("@/pages/dashboard/beanProfileFixture")
  >("@/pages/dashboard/beanProfileFixture");
  const noopMutation = () => ({ mutateAsync: vi.fn(async () => undefined) });
  return {
    ...actual,
    useHealth: () => healthState,
    useFreshHealthGate: () => healthState,
    // #523: StartRoastView's stale-session check reads history too — an
    // empty, settled list here (no stale run) so this file's assertions
    // stay focused on the nav-everywhere invariant, not the stale-session
    // branch (covered by StartRoastView.test.tsx).
    useHistory: () => ({ data: { runs: [] }, isPending: false }),
    useBeanProfiles: () => ({ data: { profiles: FIXTURE_BEAN_PROFILES }, isLoading: false }),
    useCreateBeanProfile: noopMutation,
    useUpdateBeanProfile: noopMutation,
    useDeleteBeanProfile: noopMutation,
  };
});

function renderLayout() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/start"]}>
        <Routes>
          <Route element={<RootLayout />}>
            <Route path="/start" element={<StartRoastView />} />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(cleanup);
beforeEach(() => {
  healthState.data = undefined;
  healthState.isSuccess = false;
  healthState.isError = false;
  healthState.isFresh = false;
});

describe("RootLayout — nav renders on every route and state (#523)", () => {
  it("shows the nav during a loading hold (health pending)", () => {
    healthState.isSuccess = false;
    healthState.isError = false;
    healthState.isFresh = false;
    healthState.data = undefined;
    renderLayout();
    expect(screen.getByTestId("app-nav")).toBeInTheDocument();
    expect(screen.getByTestId("start-roast-loading")).toBeInTheDocument();
  });

  it("shows the nav on a persistent health error (status-unknown view)", () => {
    healthState.isSuccess = false;
    healthState.isError = true;
    healthState.isFresh = true;
    healthState.data = undefined;
    renderLayout();
    expect(screen.getByTestId("app-nav")).toBeInTheDocument();
    expect(screen.getByTestId("start-roast-status-unknown")).toBeInTheDocument();
  });

  it("shows the nav on the normal form state, and reflects active-run presence", () => {
    healthState.isSuccess = true;
    healthState.isError = false;
    healthState.isFresh = true;
    healthState.data = { active_run_id: null };
    renderLayout();
    expect(screen.getByTestId("app-nav")).toBeInTheDocument();
    expect(screen.getByTestId("start-roast-view")).toBeInTheDocument();
    // Idle: the nav's first slot is Home, not Live roast.
    expect(screen.getByTestId("nav-home")).toBeInTheDocument();
    expect(screen.queryByTestId("nav-live-roast")).toBeNull();
  });

  it("swaps the nav's first slot to Live roast when the same health state reports an active run", () => {
    healthState.isSuccess = true;
    healthState.isError = false;
    healthState.isFresh = true;
    healthState.data = { active_run_id: "run-42" };
    renderLayout();
    expect(screen.getByTestId("app-nav")).toBeInTheDocument();
    // The active-run banner replaces the form; the nav still renders above it.
    expect(screen.getByTestId("start-roast-active-run-banner")).toBeInTheDocument();
    expect(screen.getByTestId("nav-live-roast")).toBeInTheDocument();
    expect(screen.queryByTestId("nav-home")).toBeNull();
  });
});
