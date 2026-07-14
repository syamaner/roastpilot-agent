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
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { StartRoastView } from "./StartRoastView";

// #516: instance_id defaults present (matching a real 201 response) so the
// existing pre-#516 tests below stay representative of real server
// behaviour; the dedicated #516 describe block below overrides it per test.
const startRoastMock = vi.hoisted(() =>
  vi.fn(async () => ({ id: "run-new", instance_id: "server-a" })),
);
// #525: the clear-stale-session mutation, mirroring startRoastMock's pattern.
const clearStaleSessionMock = vi.hoisted(() =>
  vi.fn(async () => ({ run_id: "run-stranded", outcome: "aborted", completed_at_utc: "now" })),
);
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: { ...actual.api, startRoast: startRoastMock, clearStaleSession: clearStaleSessionMock },
  };
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

// Mutable history stub (#523; #535 Codex follow-up: now `useFreshHistoryGate`
// shaped, mirroring `healthState` above — `isFresh` models the same Codex
// pattern: false while pending OR while `isSuccess` is true only from a
// stale cache entry with the genuinely fresh refetch still in flight).
const historyState: {
  data: { runs: { id: string; outcome: string | null }[] } | undefined;
  isError: boolean;
  isFresh: boolean;
} = { data: { runs: [] }, isError: false, isFresh: true };

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
    useFreshHistoryGate: () => historyState,
    useBeanProfiles: () => ({ data: { profiles: FIXTURE_BEAN_PROFILES }, isLoading: false }),
    useCreateBeanProfile: noopMutation,
    useUpdateBeanProfile: noopMutation,
    useDeleteBeanProfile: noopMutation,
  };
});

/** #516: reads the arriving location.state so tests can assert what
 *  StartRoastView actually handed `navigate("/live", { state })` — a real
 *  `LivePage` reads this same `useLocation().state` to compare instance ids. */
function LiveLandingWithState(): React.JSX.Element {
  const location = useLocation();
  return (
    <div data-testid="live-page" data-state={JSON.stringify(location.state ?? null)} />
  );
}

function renderView() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = (children: ReactNode) => (
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/start"]}>
        <Routes>
          <Route path="/start" element={children} />
          {/* #403: StartRoastView now routes to /live, not /. */}
          <Route path="/live" element={<LiveLandingWithState />} />
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
  clearStaleSessionMock.mockClear();
  clearStaleSessionMock.mockResolvedValue({
    run_id: "run-stranded",
    outcome: "aborted",
    completed_at_utc: "now",
  } as never);
  healthState.data = { active_run_id: null };
  healthState.isSuccess = true;
  healthState.isError = false;
  healthState.isFresh = true;
  historyState.data = { runs: [] };
  historyState.isError = false;
  historyState.isFresh = true;
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

  it("#516: passes the 201 response's instance_id forward as router navigation state", async () => {
    renderView();
    fillMinimum();
    fireEvent.submit(screen.getByTestId("start-roast-form"));
    await waitFor(() => expect(screen.getByTestId("live-page")).toBeInTheDocument());
    expect(screen.getByTestId("live-page")).toHaveAttribute(
      "data-state",
      JSON.stringify({ expectedInstanceId: "server-a" }),
    );
  });

  it("#516: navigates with NO state when the 201 response carries no instance_id (a pre-#516 server, or the field genuinely absent)", async () => {
    startRoastMock.mockResolvedValueOnce({ id: "run-new" } as never);
    renderView();
    fillMinimum();
    fireEvent.submit(screen.getByTestId("start-roast-form"));
    await waitFor(() => expect(screen.getByTestId("live-page")).toBeInTheDocument());
    expect(screen.getByTestId("live-page")).toHaveAttribute("data-state", "null");
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
    historyState.isFresh = false;
    renderView();
    expect(screen.getByTestId("start-roast-loading")).toBeInTheDocument();
    expect(screen.queryByTestId("start-roast-view")).toBeNull();
    expect(screen.queryByTestId("start-roast-stale-session")).toBeNull();
  });

  it("#535 Codex follow-up: holds through a stale-cache history remount even though data is already present", () => {
    // Mirrors the health equivalent above (line ~131) and /live's own
    // history-freshness hold (#532): useHistory's shared 30s staleTime lets a
    // remount render CACHED (possibly stale) history data with data already
    // present while the genuinely fresh forced refetch (useFreshHistoryGate)
    // is still in flight. isFresh:false is the only signal that catches it —
    // a naive "data is defined" check would fall through to the stale-session
    // evaluation on a cached snapshot that might already be out of date.
    healthState.data = { active_run_id: null };
    healthState.isSuccess = true;
    healthState.isFresh = true;
    historyState.data = { runs: [] }; // cached, present — but NOT fresh yet
    historyState.isFresh = false;
    renderView();
    expect(screen.getByTestId("start-roast-loading")).toBeInTheDocument();
    expect(screen.queryByTestId("start-roast-view")).toBeNull();
    expect(screen.queryByTestId("start-roast-stale-session")).toBeNull();
  });

  it("#535 Codex follow-up: shows a neutral history-unknown state (NEVER the bare form) when history persistently errors", () => {
    // A persistent /api/roasts failure leaves history.data undefined, so
    // staleRun would resolve to null — that must NEVER be read as "no stale
    // session", since the source itself is unknown. Mirrors /live's own
    // history.isError -> LiveHistoryUnknownView handling (#532) and this
    // route's existing health.isError -> status-unknown state.
    healthState.data = { active_run_id: null };
    healthState.isSuccess = true;
    healthState.isFresh = true;
    historyState.data = undefined;
    historyState.isError = true;
    historyState.isFresh = true; // isError implies isFresh (see useFreshGate)
    renderView();
    expect(screen.getByTestId("start-roast-history-unknown")).toBeInTheDocument();
    expect(screen.queryByTestId("start-roast-form")).toBeNull();
    expect(screen.queryByTestId("start-roast-stale-session")).toBeNull();
    expect(screen.queryByTestId("start-roast-loading")).toBeNull();
  });

  it("#535 Codex follow-up: a history error takes priority over an otherwise-idle health snapshot, never falling through to the form", () => {
    // Explicit composition check: health is fresh/idle/success (would render
    // the form on its own), but history — the second, independent
    // stale-session source on this route — is the one that's unknown here.
    healthState.data = { active_run_id: null };
    healthState.isSuccess = true;
    healthState.isError = false;
    healthState.isFresh = true;
    historyState.data = undefined;
    historyState.isError = true;
    historyState.isFresh = true;
    renderView();
    expect(screen.getByTestId("start-roast-history-unknown")).toBeInTheDocument();
    expect(screen.queryByTestId("start-roast-form")).toBeNull();
  });

  it("shows the stale-session state when a history run is open (outcome: null) and health doesn't recognise it as active — the 12 Jul incident signature", () => {
    healthState.data = { active_run_id: null };
    healthState.isSuccess = true;
    healthState.isFresh = true;
    historyState.data = { runs: [{ id: "run-stranded", outcome: null }] };
    historyState.isFresh = true;
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
    historyState.isFresh = true;
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
    historyState.isFresh = true;
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
    historyState.isFresh = true;
    renderView();
    expect(screen.getByTestId("start-roast-view")).toBeInTheDocument();
    expect(screen.queryByTestId("start-roast-stale-session")).toBeNull();
  });

  it("navigating the stale-session link reaches the live view", () => {
    healthState.data = { active_run_id: null };
    healthState.isSuccess = true;
    healthState.isFresh = true;
    historyState.data = { runs: [{ id: "run-stranded", outcome: null }] };
    historyState.isFresh = true;
    renderView();
    fireEvent.click(screen.getByTestId("start-roast-stale-session-link"));
    expect(screen.getByTestId("live-page")).toBeInTheDocument();
  });
});

describe("StartRoastView — clear-stale-session action (#525)", () => {
  function renderStaleSession() {
    healthState.data = { active_run_id: null };
    healthState.isSuccess = true;
    healthState.isFresh = true;
    historyState.data = { runs: [{ id: "run-stranded", outcome: null }] };
    historyState.isFresh = true;
    renderView();
  }

  it("the stale-session card renders the clear affordance alongside the existing live-view link", () => {
    renderStaleSession();
    expect(screen.getByTestId("start-roast-stale-session-clear-open")).toBeInTheDocument();
    // The existing #523 link stays — the clear action is additive, not a replacement.
    expect(screen.getByTestId("start-roast-stale-session-link")).toHaveAttribute("href", "/live");
  });

  it("opens a confirm step requiring a reason before the clear can be submitted", () => {
    renderStaleSession();
    fireEvent.click(screen.getByTestId("start-roast-stale-session-clear-open"));
    expect(screen.getByTestId("start-roast-stale-session-confirm")).toBeInTheDocument();
    // Empty reason: the confirm button is disabled — no accidental no-reason clears.
    expect(screen.getByTestId("start-roast-stale-session-clear-confirm")).toBeDisabled();
  });

  it("blocks submission on a whitespace-only reason (client-side mirror of the server's required, non-empty reason)", () => {
    renderStaleSession();
    fireEvent.click(screen.getByTestId("start-roast-stale-session-clear-open"));
    fireEvent.change(screen.getByTestId("start-roast-stale-session-reason"), {
      target: { value: "   " },
    });
    expect(screen.getByTestId("start-roast-stale-session-clear-confirm")).toBeDisabled();
  });

  it("submits the typed reason via api.clearStaleSession and shows the cleared state on success", async () => {
    renderStaleSession();
    fireEvent.click(screen.getByTestId("start-roast-stale-session-clear-open"));
    fireEvent.change(screen.getByTestId("start-roast-stale-session-reason"), {
      target: { value: "confirmed orphaned via SQL inspection" },
    });
    expect(screen.getByTestId("start-roast-stale-session-clear-confirm")).not.toBeDisabled();
    fireEvent.click(screen.getByTestId("start-roast-stale-session-clear-confirm"));

    await waitFor(() => expect(clearStaleSessionMock).toHaveBeenCalledTimes(1));
    expect(clearStaleSessionMock).toHaveBeenCalledWith("run-stranded", {
      reason: "confirmed orphaned via SQL inspection",
    });
    await waitFor(() =>
      expect(screen.getByTestId("start-roast-stale-session-cleared")).toBeInTheDocument(),
    );
  });

  it("cancelling the confirm step discards the typed reason and returns to the plain affordance", () => {
    renderStaleSession();
    fireEvent.click(screen.getByTestId("start-roast-stale-session-clear-open"));
    fireEvent.change(screen.getByTestId("start-roast-stale-session-reason"), {
      target: { value: "changed my mind" },
    });
    fireEvent.click(screen.getByTestId("start-roast-stale-session-clear-cancel"));
    expect(screen.getByTestId("start-roast-stale-session-clear-open")).toBeInTheDocument();
    expect(screen.queryByTestId("start-roast-stale-session-confirm")).toBeNull();
    expect(clearStaleSessionMock).not.toHaveBeenCalled();

    // Reopening starts from a blank reason again (discarded, not just hidden).
    fireEvent.click(screen.getByTestId("start-roast-stale-session-clear-open"));
    expect(screen.getByTestId("start-roast-stale-session-reason")).toHaveValue("");
  });

  it("shows an error state and lets the operator retry when the clear request fails (e.g. the server's guard (a)/(c) 409)", async () => {
    clearStaleSessionMock.mockRejectedValueOnce(new Error("actively driven — do not clear"));
    renderStaleSession();
    fireEvent.click(screen.getByTestId("start-roast-stale-session-clear-open"));
    fireEvent.change(screen.getByTestId("start-roast-stale-session-reason"), {
      target: { value: "thought this was mine" },
    });
    fireEvent.click(screen.getByTestId("start-roast-stale-session-clear-confirm"));

    await waitFor(() =>
      expect(screen.getByTestId("start-roast-stale-session-clear-error")).toBeInTheDocument(),
    );
    // The confirm step stays open (not silently reset) so the operator can retry.
    expect(screen.getByTestId("start-roast-stale-session-clear-confirm")).toBeInTheDocument();
    expect(screen.queryByTestId("start-roast-stale-session-cleared")).toBeNull();
  });
});
