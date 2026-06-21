/**
 * Start-roast route (#324): renders the reused Start form, and a successful start
 * POSTs then navigates to `/` (where HomeGate shows the live dashboard). No local
 * run state is fabricated — the start path refetches health (render from server).
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
  // start must refresh the active-run snapshot before routing to `/`.
  const refetchSpy = vi.spyOn(client, "refetchQueries");
  const wrapper = (children: ReactNode) => (
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/start"]}>
        <Routes>
          <Route path="/start" element={children} />
          <Route path="/" element={<div data-testid="home-or-dashboard" />} />
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

  it("POSTs, refetches health, then navigates to `/` on success", async () => {
    const { refetchSpy } = renderView();
    fillMinimum();
    fireEvent.submit(screen.getByTestId("start-roast-form"));
    await waitFor(() => expect(startRoastMock).toHaveBeenCalledTimes(1));
    // Render-from-server: start AWAITS a health refetch (the active-run snapshot)
    // before routing, so `/` resolves to the dashboard with no idle-hub flash.
    await waitFor(() =>
      expect(refetchSpy).toHaveBeenCalledWith({ queryKey: roastKeys.health }),
    );
    // On success we route to `/` (HomeGate → live dashboard once health refetches).
    await waitFor(() =>
      expect(screen.getByTestId("home-or-dashboard")).toBeInTheDocument(),
    );
  });
});
