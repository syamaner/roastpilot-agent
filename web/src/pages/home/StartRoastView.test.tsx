/**
 * Start-roast route (#324, updated #403): renders the reused Start form, and a
 * successful start POSTs then navigates to `/live` (the stable reload-safe
 * live-roast route, #403). No local run state is fabricated — the start path
 * refetches health (render from server).
 */

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { roastKeys } from "@/hooks/queries";
import { StartRoastView } from "./StartRoastView";

const startRoastMock = vi.hoisted(() => vi.fn(async () => ({ id: "run-new" })));
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, api: { ...actual.api, startRoast: startRoastMock } };
});

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
    useBeanProfiles: () => ({ data: { profiles: FIXTURE_BEAN_PROFILES }, isLoading: false }),
    useCreateBeanProfile: noopMutation,
    useUpdateBeanProfile: noopMutation,
    useDeleteBeanProfile: noopMutation,
  };
});

function renderView() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  // Spy on the awaited health refetch — the render-from-server enforcement point:
  // start must refresh the active-run snapshot before routing to `/live`.
  const refetchSpy = vi.spyOn(client, "refetchQueries");
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
  return { refetchSpy };
}

afterEach(cleanup);
beforeEach(() => {
  startRoastMock.mockClear();
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

  it("POSTs, refetches health, then navigates to `/live` on success (#403)", async () => {
    const { refetchSpy } = renderView();
    fillMinimum();
    fireEvent.submit(screen.getByTestId("start-roast-form"));
    await waitFor(() => expect(startRoastMock).toHaveBeenCalledTimes(1));
    // Render-from-server: start AWAITS a health refetch (the active-run snapshot)
    // before routing, so `/live` resolves to the dashboard with no start-form flash.
    await waitFor(() =>
      expect(refetchSpy).toHaveBeenCalledWith({ queryKey: roastKeys.health }),
    );
    // #403: On success we route to `/live` (the stable reload-safe live-roast URL),
    // not `/`. LivePage then shows the dashboard once health reports the new run.
    await waitFor(() =>
      expect(screen.getByTestId("live-page")).toBeInTheDocument(),
    );
    // We must NOT land on the home hub.
    expect(screen.queryByTestId("home-landing")).toBeNull();
  });
});
