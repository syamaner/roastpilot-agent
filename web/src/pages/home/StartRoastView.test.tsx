/**
 * Start-roast route (#324, updated #403, #513): renders the reused Start
 * form, and a successful start POSTs then navigates to `/live` (the stable
 * reload-safe live-roast route, #403) UNCONDITIONALLY on the proven 201 —
 * never gated on a health refetch, which can itself fail silently (#513;
 * `LivePage`'s idle state owns confirming the new run with retries). No local
 * run state is fabricated — the active run is discovered from the server.
 *
 * Also covers the two defensive states that replace the bare form: an
 * already-active run (banner + link to `/live`) and a persistent health
 * error (status-unknown state) — active-run status genuinely unknown must
 * never be treated as "no run".
 */

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { StartRoastView } from "./StartRoastView";

const startRoastMock = vi.hoisted(() => vi.fn(async () => ({ id: "run-new" })));
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, api: { ...actual.api, startRoast: startRoastMock } };
});

// Mutable health stub — mirrors LivePage.test.tsx's pattern (#513): the active-
// run banner + status-unknown tests need a controllable `useHealth()` without
// a real fetch.
const healthState: {
  data: { active_run_id: string | null } | undefined;
  isSuccess: boolean;
  isError: boolean;
} = { data: undefined, isSuccess: false, isError: false };

// Bean-profile library hooks stubbed so the form's dropdown wires real fixture data
// without firing a (failing) jsdom fetch — mirrors the dashboard idle spec (#303).
vi.mock("@/hooks/queries", async () => {
  const actual = await vi.importActual<typeof import("@/hooks/queries")>("@/hooks/queries");
  const { FIXTURE_BEAN_PROFILES } = await vi.importActual<
    typeof import("@/pages/dashboard/beanProfileFixture")
  >("@/pages/dashboard/beanProfileFixture");
  const noopMutation = () => ({ mutateAsync: vi.fn(async () => undefined) });
  return {
    ...actual,
    useHealth: () => healthState,
    useBeanProfiles: () => ({ data: { profiles: FIXTURE_BEAN_PROFILES }, isLoading: false }),
    useCreateBeanProfile: noopMutation,
    useUpdateBeanProfile: noopMutation,
    useDeleteBeanProfile: noopMutation,
  };
});

function renderView() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = (children: ReactNode) => (
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/start"]}>
        <Routes>
          <Route path="/start" element={children} />
          {/* #403: StartRoastView now routes to /live, not /. */}
          <Route path="/live" element={<div data-testid="live-page" />} />
          <Route path="/" element={<div data-testid="home-landing" />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
  render(wrapper(<StartRoastView />));
}

afterEach(cleanup);
beforeEach(() => {
  startRoastMock.mockClear();
  healthState.data = { active_run_id: null };
  healthState.isSuccess = true;
  healthState.isError = false;
});

/** Fill the required bean fields + weight; the draft defaults cover the rest. */
function fillMinimum() {
  fireEvent.change(screen.getByTestId("start-roast-name"), {
    target: { value: "Morning batch" },
  });
  fireEvent.change(screen.getByTestId("start-roast-bean_origin"), {
    target: { value: "Ethiopia Guji" },
  });
  fireEvent.change(screen.getByTestId("start-roast-bean_weight_grams"), {
    target: { value: "250" },
  });
}

describe("StartRoastView (#324)", () => {
  it("renders the reused Start-Roast form with the bean-profile library", () => {
    renderView();
    expect(screen.getByTestId("start-roast-view")).toBeInTheDocument();
    expect(screen.getByTestId("start-roast-form")).toBeInTheDocument();
    expect(screen.getByTestId("bean-profile-picker")).toBeInTheDocument();
  });

  it("POSTs, then navigates to `/live` unconditionally on the proven 201 (#403/#513)", async () => {
    renderView();
    fillMinimum();
    fireEvent.submit(screen.getByTestId("start-roast-form"));
    await waitFor(() => expect(startRoastMock).toHaveBeenCalledTimes(1));
    // #513: navigation must NOT be gated on a health refetch (which can itself
    // fail silently) — it fires as soon as the POST is proven (201). #403: we
    // route to `/live` (the stable reload-safe live-roast URL), not `/`.
    // LivePage then owns confirming the new run against `/health`.
    await waitFor(() =>
      expect(screen.getByTestId("live-page")).toBeInTheDocument(),
    );
    // We must NOT land on the home hub.
    expect(screen.queryByTestId("home-landing")).toBeNull();
  });

  it("#513: shows the active-run banner instead of a bare form when a run is already active", () => {
    healthState.data = { active_run_id: "run-42" };
    healthState.isSuccess = true;
    renderView();
    expect(screen.getByTestId("start-roast-active-run-banner")).toBeInTheDocument();
    expect(screen.queryByTestId("start-roast-form")).toBeNull();
    expect(screen.getByTestId("start-roast-active-run-link")).toHaveAttribute("href", "/live");
  });

  it("#513: navigating the active-run banner link reaches the live dashboard", () => {
    healthState.data = { active_run_id: "run-42" };
    healthState.isSuccess = true;
    renderView();
    fireEvent.click(screen.getByTestId("start-roast-active-run-link"));
    expect(screen.getByTestId("live-page")).toBeInTheDocument();
  });

  it("#513 medium: shows a neutral status-unknown state (NEVER the bare form) when health persistently errors", () => {
    // This route (still URL-reachable) is almost certainly what the incident
    // screenshot was taken on — active-run status unknown must never fall
    // through to the bare form, the same hazard as the active-run-banner case.
    healthState.isSuccess = false;
    healthState.isError = true;
    healthState.data = undefined;
    renderView();
    expect(screen.getByTestId("start-roast-status-unknown")).toBeInTheDocument();
    expect(screen.queryByTestId("start-roast-form")).toBeNull();
    expect(screen.queryByTestId("start-roast-active-run-banner")).toBeNull();
  });
});
