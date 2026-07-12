/**
 * Start-roast route (#324, updated #403, #513): renders the reused Start
 * form, and a successful start POSTs then navigates to `/live` (the stable
 * reload-safe live-roast route, #403) UNCONDITIONALLY on the proven 201 —
 * never gated on a health refetch, which can itself fail silently (#513;
 * `LivePage`'s idle state owns confirming the new run with retries). No local
 * run state is fabricated — the active run is discovered from the server.
 *
 * Also covers the defensive states that replace the bare form: pending
 * health / a stale-cache read still revalidating (loading hold, mirroring
 * `LivePage`'s — post-#514/#515 review, `useFreshHealthGate`), an
 * already-active run (banner + link to `/live`), and a persistent health
 * error (status-unknown state) — active-run status genuinely unknown, or not
 * yet known FRESH, must never be treated as "no run".
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
// run banner + status-unknown + loading-hold tests need a controllable
// `useFreshHealthGate()` without a real fetch. `isFresh` models the Codex
// follow-up: false while pending OR while `isSuccess` is true only from a
// stale cache entry with the genuinely fresh refetch still in flight.
const healthState: {
  data: { active_run_id: string | null } | undefined;
  isSuccess: boolean;
  isError: boolean;
  isFresh: boolean;
} = { data: undefined, isSuccess: false, isError: false, isFresh: false };

// Mutable history stub (#523): the stale-session check's data source.
const historyState: {
  data: { runs: { id: string; outcome: string | null }[] } | undefined;
  isPending: boolean;
} = { data: { runs: [] }, isPending: false };

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
    useFreshHealthGate: () => healthState,
    useHistory: () => historyState,
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
  healthState.isFresh = true;
  historyState.data = { runs: [] };
  historyState.isPending = false;
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

describe("StartRoastView — loading hold (#513 follow-up)", () => {
  it("renders the loading placeholder until health resolves — NEVER the bare form (mirrors LivePage)", () => {
    // Pending: neither isSuccess nor isError yet (the initial fetch in
    // flight). Previously fell through both existing guards straight to the
    // bare form — a reload of this still-URL-reachable route mid-roast would
    // show an untouched-looking form for one /health round-trip.
    healthState.isSuccess = false;
    healthState.isError = false;
    healthState.isFresh = false;
    healthState.data = undefined;
    renderView();
    expect(screen.getByTestId("start-roast-loading")).toBeInTheDocument();
    expect(screen.queryByTestId("start-roast-form")).toBeNull();
    expect(screen.queryByTestId("start-roast-active-run-banner")).toBeNull();
    expect(screen.queryByTestId("start-roast-status-unknown")).toBeNull();
  });

  it("#513 Codex follow-up: holds through a stale-cache remount even though isSuccess is already true", () => {
    // The exact scenario Codex found: useHealth's shared 30s staleTime lets a
    // remount render a CACHED idle snapshot with isSuccess:true while the
    // genuinely fresh forced refetch (useFreshHealthGate) is still in
    // flight. A naive `!isSuccess` check would render the bare form here —
    // isFresh:false is the only signal that catches it.
    healthState.isSuccess = true;
    healthState.isError = false;
    healthState.isFresh = false;
    healthState.data = { active_run_id: null }; // stale cached "idle" value
    renderView();
    expect(screen.getByTestId("start-roast-loading")).toBeInTheDocument();
    expect(screen.queryByTestId("start-roast-form")).toBeNull();
    expect(screen.queryByTestId("start-roast-active-run-banner")).toBeNull();
  });
});

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
    healthState.isFresh = true; // isError implies isFresh (see useFreshHealthGate)
    healthState.data = undefined;
    renderView();
    expect(screen.getByTestId("start-roast-status-unknown")).toBeInTheDocument();
    expect(screen.queryByTestId("start-roast-form")).toBeNull();
    expect(screen.queryByTestId("start-roast-active-run-banner")).toBeNull();
  });
});

describe("StartRoastView — stale-session detection (#523)", () => {
  it("holds while history is still pending, even once health is fresh and idle", () => {
    healthState.data = { active_run_id: null };
    healthState.isSuccess = true;
    healthState.isFresh = true;
    historyState.data = undefined;
    historyState.isPending = true;
    renderView();
    expect(screen.getByTestId("start-roast-loading")).toBeInTheDocument();
    expect(screen.queryByTestId("start-roast-view")).toBeNull();
    expect(screen.queryByTestId("start-roast-stale-session")).toBeNull();
  });

  it("shows the stale-session state when a history run is open (outcome: null) and health doesn't recognise it as active — the 12 Jul incident signature", () => {
    healthState.data = { active_run_id: null };
    healthState.isSuccess = true;
    healthState.isFresh = true;
    historyState.data = { runs: [{ id: "run-stranded", outcome: null }] };
    historyState.isPending = false;
    renderView();
    expect(screen.getByTestId("start-roast-stale-session")).toBeInTheDocument();
    expect(screen.queryByTestId("start-roast-form")).toBeNull();
    expect(screen.getByTestId("start-roast-stale-session-link")).toHaveAttribute("href", "/live");
  });

  it("does NOT flag a stale session when the open run IS the one health recognises as active (handled by the active-run banner instead)", () => {
    // A genuinely active run (including operator_recovery_required, whose
    // health snapshot still carries a non-null active_run_id) has an open
    // history row too — but health.active_run_id matches it, so this is the
    // correct, safer active-run-banner path, not the stale-session dead end.
    healthState.data = { active_run_id: "run-active" };
    healthState.isSuccess = true;
    healthState.isFresh = true;
    historyState.data = { runs: [{ id: "run-active", outcome: null }] };
    historyState.isPending = false;
    renderView();
    expect(screen.getByTestId("start-roast-active-run-banner")).toBeInTheDocument();
    expect(screen.queryByTestId("start-roast-stale-session")).toBeNull();
  });

  it("does NOT flag a stale session for a just-started roast — a fresh run has outcome: null too, but health already reports its id as active", () => {
    // Distinct from the recovery case above: this is the EVERYDAY path every
    // roast takes on start (StartRoastForm POSTs, LivePage's health cache
    // picks up the new active_run_id — see LiveStartFlow.integration.test.tsx
    // for that real handoff). A run this fresh has NOT been finalised yet
    // (outcome: null is correct and expected, not a sign of anything wrong),
    // so the id-match check must clear it exactly like the recovery case —
    // guarded separately because "just started" and "restarted mid-roast"
    // are different real-world triggers worth distinct regression coverage.
    healthState.data = { active_run_id: "run-just-started" };
    healthState.isSuccess = true;
    healthState.isFresh = true;
    historyState.data = { runs: [{ id: "run-just-started", outcome: null }] };
    historyState.isPending = false;
    renderView();
    expect(screen.getByTestId("start-roast-active-run-banner")).toBeInTheDocument();
    expect(screen.queryByTestId("start-roast-stale-session")).toBeNull();
    expect(screen.queryByTestId("start-roast-form")).toBeNull();
  });

  it("does NOT flag a stale session when every history run has a real outcome", () => {
    healthState.data = { active_run_id: null };
    healthState.isSuccess = true;
    healthState.isFresh = true;
    historyState.data = {
      runs: [
        { id: "run-1", outcome: "completed" },
        { id: "run-2", outcome: "faulted" },
      ],
    };
    historyState.isPending = false;
    renderView();
    expect(screen.getByTestId("start-roast-view")).toBeInTheDocument();
    expect(screen.queryByTestId("start-roast-stale-session")).toBeNull();
  });

  it("navigating the stale-session link reaches the live view", () => {
    healthState.data = { active_run_id: null };
    healthState.isSuccess = true;
    healthState.isFresh = true;
    historyState.data = { runs: [{ id: "run-stranded", outcome: null }] };
    historyState.isPending = false;
    renderView();
    fireEvent.click(screen.getByTestId("start-roast-stale-session-link"));
    expect(screen.getByTestId("live-page")).toBeInTheDocument();
  });
});
