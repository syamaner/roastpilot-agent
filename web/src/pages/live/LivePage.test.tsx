/**
 * LivePage (#403): stable /live route — reload-safe, server-driven states.
 *
 * 1. Holds (loading div) until health resolves — start form must not flash first.
 * 2. Active run  → the dashboard (DashboardPage).
 * 3. No run (idle, error, or post-completion) → the live start-roast view.
 *    — After a roast ends /live shows the start form directly (not the home hub).
 *    — On health error /live falls back to the start form (unknown = treat as idle).
 * 4. Start-roast: on success, POSTs, refetches health, navigates to /live.
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
    useCreateBeanProfile: noopMutation,
    useUpdateBeanProfile: noopMutation,
    useDeleteBeanProfile: noopMutation,
  };
});

// Stub the destination bodies so the test asserts the gate branch, not their internals.
vi.mock("@/pages/dashboard/DashboardPage", () => ({
  DashboardPage: () => <div data-testid="dashboard-stub" />,
}));

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
  const wrapper = (children: ReactNode) => (
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[initialPath]}>
        <Routes>
          <Route path="/live" element={children} />
          <Route path="/" element={<div data-testid="home-landing" />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
  render(wrapper(<LivePage />));
  return { client, refetchSpy };
}

describe("LivePage (#403) — loading hold", () => {
  it("renders the loading placeholder until health resolves", () => {
    healthState.isSuccess = false;
    healthState.data = undefined;
    renderPage();
    expect(screen.getByTestId("live-page-loading")).toBeInTheDocument();
    expect(screen.queryByTestId("dashboard-stub")).toBeNull();
    expect(screen.queryByTestId("live-start-view")).toBeNull();
  });
});

describe("LivePage (#403) — active run", () => {
  it("shows the dashboard when the server reports an active run", () => {
    healthState.isSuccess = true;
    healthState.data = { active_run_id: "run-42" };
    renderPage();
    expect(screen.getByTestId("dashboard-stub")).toBeInTheDocument();
    expect(screen.queryByTestId("live-start-view")).toBeNull();
    expect(screen.queryByTestId("live-page-loading")).toBeNull();
  });

  it("is reload-safe: a fresh render with an active run shows the dashboard (no flash)", () => {
    // The reload-safe guarantee: loading /live with an active run in server state
    // shows the dashboard after health resolves — no intermediate idle flash.
    healthState.isSuccess = true;
    healthState.data = { active_run_id: "run-99" };
    renderPage();
    // The loading placeholder is gone once health has resolved.
    expect(screen.queryByTestId("live-page-loading")).toBeNull();
    // The start form must not appear even for a frame.
    expect(screen.queryByTestId("live-start-view")).toBeNull();
    expect(screen.getByTestId("dashboard-stub")).toBeInTheDocument();
  });
});

describe("LivePage (#403) — no active run (idle / post-completion)", () => {
  it("shows the start-roast view when the server reports no active run", () => {
    // Idle (no run has ever been started) or after a run ends.
    healthState.isSuccess = true;
    healthState.data = { active_run_id: null };
    renderPage();
    expect(screen.getByTestId("live-start-view")).toBeInTheDocument();
    expect(screen.queryByTestId("dashboard-stub")).toBeNull();
    expect(screen.queryByTestId("live-page-loading")).toBeNull();
  });

  it("stays on /live with the start form after a roast ends (not redirected to home hub)", () => {
    // Call #3: after a roast completes (active_run_id → null), /live shows the
    // start form directly — NOT the home hub's two-tile landing at /. The operator
    // can immediately begin the next roast without navigating away from /live.
    healthState.isSuccess = true;
    healthState.data = { active_run_id: null };
    const { client } = renderPage();
    expect(screen.getByTestId("live-start-view")).toBeInTheDocument();

    // Simulate a completed run's state change — /live must NOT redirect anywhere.
    client.setQueryData(roastKeys.health, {
      status: "ok",
      version: "t",
      mcp_child: "running",
      active_run_id: null,
    });
    // Still on /live showing the start form, not the home hub.
    expect(screen.getByTestId("live-start-view")).toBeInTheDocument();
    expect(screen.queryByTestId("home-landing")).toBeNull();
  });

  it("falls back to the start-roast view when the health fetch errors", () => {
    // Error state: active run unknown — treat as idle (same as HomeGate's fallback).
    healthState.isSuccess = false;
    healthState.isError = true;
    healthState.data = undefined;
    renderPage();
    expect(screen.getByTestId("live-start-view")).toBeInTheDocument();
    expect(screen.queryByTestId("live-page-loading")).toBeNull();
    expect(screen.queryByTestId("dashboard-stub")).toBeNull();
  });
});

describe("LivePage (#403) — start-roast flow", () => {
  it("POSTs, refetches health, and stays on /live after starting a roast", async () => {
    healthState.isSuccess = true;
    healthState.data = { active_run_id: null };
    const { refetchSpy } = renderPage();
    expect(screen.getByTestId("live-start-view")).toBeInTheDocument();

    // Simulate form submit (the stubbed form calls onStart on submit).
    fireEvent.submit(screen.getByTestId("start-roast-form-stub"));
    await waitFor(() => expect(startRoastMock).toHaveBeenCalledTimes(1));

    // Render-from-server: start AWAITS a health refetch before the page re-evaluates.
    await waitFor(() =>
      expect(refetchSpy).toHaveBeenCalledWith({ queryKey: roastKeys.health }),
    );

    // We stay on /live (the stable live-roast URL) — NOT navigating away to /.
    // (The home-landing would only appear if navigate("/") were called.)
    expect(screen.queryByTestId("home-landing")).toBeNull();
  });
});
