/**
 * LivePage (#403 / #423 D81, updated #523, #532 Codex follow-up): single
 * live-roast home at /live. NEVER a form (#523 IA) — the only start-form
 * surface is /start.
 *
 * Branches tested:
 * 1. Loading hold  — neither a summary nor the dashboard appears until health
 *    AND history (the idle path's persistent fallback) each produce a
 *    GENUINELY FRESH read, and until any in-flight terminal-outcome fetch for
 *    a just-finished run resolves.
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
 *    f. Transition-flash guard (#532 Codex follow-up): completing a run with
 *       an OLDER completed run already in history must never flash that
 *       older run's summary before the just-finished run's own gate resolves.
 * 4. No active run, no completed run ever — LiveNoRoastsView (neutral, not a
 *    form), linking to /start.
 * 5. History error (#532 Codex follow-up) — a persistent history read
 *    failure gets its OWN neutral state, never the false "no roasts ever"
 *    claim `LiveNoRoastsView` would otherwise render.
 * 6. History staleness (#532 Codex follow-up) — history is now gated on
 *    `useFreshHistoryGate`, the same isSuccess≠current-class treatment health
 *    already has, so a within-staleTime remount can't render a cached
 *    (possibly empty or stale) history list as authoritative.
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
  data: { active_run_id: string | null; instance_id?: string } | undefined;
  isSuccess: boolean;
  isError: boolean;
  isFresh: boolean;
} = { data: undefined, isSuccess: false, isError: false, isFresh: false };

// Mutable history stub (#523, updated #532 Codex follow-up): LivePage's
// persistent idle fallback. `isFresh` models `useFreshHistoryGate` — the same
// pattern as `healthState.isFresh` above: false while pending OR while a
// stale-cache read has a genuinely fresh forced refetch still in flight.
const historyState: {
  data: RoastHistory | undefined;
  isError: boolean;
  isFresh: boolean;
} = { data: { runs: [] }, isError: false, isFresh: true };

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
    useFreshHistoryGate: () => historyState,
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
// gate, P2-3/P2-4) AND, as of #532 round 2, by the history-detail-freshness
// effect for a history-derived id. Typed with the real `(runId: string) =>`
// signature (not a zero-arg default) so `mockImplementation` calls that
// discriminate by runId — needed once two independent call sites can invoke
// this mock — type-check against the same signature as the actual `api.roast`.
const roastApiMock = vi.hoisted(() =>
  vi.fn(async (_runId: string): Promise<RoastDetail> => ({
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
  historyState.isError = false;
  // Default to a SETTLED read, mirroring healthState.isFresh's default above;
  // the loading-hold-specific tests below override it to false.
  historyState.isFresh = true;
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

function renderPage(
  initialPath = "/live",
  // #516: the router navigation state a StartRoastView-originated navigation
  // carries (`LiveNavigationState`) — undefined models every OTHER way this
  // page is reached (direct link, reload, back/forward), where there is
  // simply no "prior start" to compare against.
  state?: unknown,
) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const Wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[{ pathname: initialPath, state }]}>
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

  it("holds while history is not fresh, even once health is fresh and idle (#523, updated #532)", () => {
    healthState.isSuccess = true;
    healthState.isError = false;
    healthState.isFresh = true;
    healthState.data = { active_run_id: null };
    historyState.data = undefined;
    historyState.isFresh = false;
    renderPage();
    expect(screen.getByTestId("live-page-loading")).toBeInTheDocument();
    expect(screen.queryByTestId("live-no-roasts-view")).toBeNull();
    expect(screen.queryByTestId("live-finished-view")).toBeNull();
  });

  it("#532 Codex follow-up: holds through a stale-cache history remount even though history.data is already present", () => {
    // Mirrors the health stale-cache test above: a within-staleTime remount
    // could render a CACHED (possibly empty) history list with data already
    // present while the genuinely fresh forced refetch (useFreshHistoryGate)
    // is still in flight. A naive "data is present" check would render
    // LiveNoRoastsView here from stale data — isFresh:false is the only
    // signal that catches it.
    healthState.isSuccess = true;
    healthState.isError = false;
    healthState.isFresh = true;
    healthState.data = { active_run_id: null };
    historyState.data = { runs: [] }; // stale cached "no history" value
    historyState.isFresh = false;
    renderPage();
    expect(screen.getByTestId("live-page-loading")).toBeInTheDocument();
    expect(screen.queryByTestId("live-no-roasts-view")).toBeNull();
    expect(screen.queryByTestId("live-finished-view")).toBeNull();
  });

  it("holds through health becoming fresh-and-idle WHILE history is still not fresh — proves the history gate is independently reached, not short-circuited by the health gate having just cleared", () => {
    // The prior two tests each exercise the loading placeholder from a STATIC
    // initial snapshot: one where health alone is not fresh (history left at
    // its default, never even evaluated in practice since the health check
    // returns first), and one where health is ALREADY fresh with history not
    // fresh. Neither proves the health→history HANDOFF actually happens
    // within a single component instance — a refactor that accidentally
    // collapsed the two `if`s into one combined condition (e.g. `if
    // (!health.isFresh && !history.isFresh)`, a realistic De Morgan's slip)
    // would still pass both of those in isolation, since each only exercises
    // one side of a buggy AND. This test starts with BOTH conditions holding
    // (mirroring the realistic startup case where health and history are both
    // in-flight together), then RESOLVES health via rerender while history
    // stays not-fresh — the loading placeholder must persist across that
    // transition, proving the history gate is reached and evaluated on its
    // own once health clears, not bypassed because it was "already handled"
    // by the first render.
    healthState.isSuccess = false;
    healthState.isError = false;
    healthState.isFresh = false;
    healthState.data = undefined;
    historyState.data = undefined;
    historyState.isFresh = false;
    const { rerender } = renderPage();
    expect(screen.getByTestId("live-page-loading")).toBeInTheDocument();

    // Health resolves fresh-and-idle; history is STILL not fresh.
    healthState.isSuccess = true;
    healthState.isFresh = true;
    healthState.data = { active_run_id: null };
    rerender();

    expect(screen.getByTestId("live-page-loading")).toBeInTheDocument();
    expect(screen.queryByTestId("dashboard-stub")).toBeNull();
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
    historyState.isFresh = true;
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

describe("LivePage — impostor-process defence (#516)", () => {
  it("shows the DISTINCT instance-mismatch message when the fresh health read's instance_id differs from the one StartRoastView observed on the 201", () => {
    healthState.isSuccess = true;
    healthState.data = { active_run_id: "run-42", instance_id: "server-b" };
    renderPage("/live", { expectedInstanceId: "server-a" });
    const view = screen.getByTestId("live-status-unknown");
    expect(view).toHaveAttribute("data-variant", "instance-mismatch");
    expect(screen.getByTestId("live-status-unknown-message")).toHaveTextContent(
      /different server process/i,
    );
    // Takes PRECEDENCE over the active-run branch — a wrong process's
    // active_run_id cannot be trusted either, so the dashboard must NOT render.
    expect(screen.queryByTestId("dashboard-stub")).toBeNull();
  });

  it("#537: the instance-mismatch view shows the field protocol (verify before reloading), not a bare reload affordance", () => {
    // Capped #536 finding: a plain reload silently drops the only evidence
    // this view exists to carry (expectedInstanceId lives in router state,
    // absent after a reload) — sessionStorage persistence was rejected, so
    // the fix is honest guidance: verify with curl/lsof BEFORE reloading.
    // Port-agnostic here (see the dedicated PR #544 P2 test below for the
    // non-default-port interpolation itself) — this test's own concern is
    // the protocol's PRESENCE and content shape, not a specific port.
    healthState.isSuccess = true;
    healthState.data = { active_run_id: "run-42", instance_id: "server-b" };
    renderPage("/live", { expectedInstanceId: "server-a" });

    const protocol = screen.getByTestId("live-status-unknown-field-protocol");
    expect(protocol).toHaveTextContent(/curl localhost:\d+\/api\/health/i);
    expect(protocol).toHaveTextContent(/lsof -nP -iTCP:\d+ -sTCP:LISTEN/i);
    expect(protocol).toHaveTextContent(/launcher/i);
    // The reload affordance stays (no dead end) but must be honestly labelled
    // as clearing the warning, never as resolving the underlying condition.
    expect(screen.getByTestId("live-status-unknown")).toHaveTextContent(
      /reloading clears this warning/i,
    );
    expect(screen.getByTestId("live-status-unknown-reload")).toBeInTheDocument();
  });

  it("#537 PR #544 round-2 P1 fold: the launcher-restart step describes what the script ACTUALLY does (force-kill), never claims it 'refuses to start over a live one'", () => {
    // scripts/roast-live.sh force-kills any matching agent/MCP process FIRST
    // (unconditionally, no heat-off) and only refuses to start if something
    // ELSE remains listening after that kill — it does not refuse over a
    // live process, it kills it. The old "refuses to start over a live one"
    // wording contradicted the round-1 safety block right above it.
    healthState.isSuccess = true;
    healthState.data = { active_run_id: "run-42", instance_id: "server-b" };
    renderPage("/live", { expectedInstanceId: "server-a" });

    const protocol = screen.getByTestId("live-status-unknown-field-protocol");
    expect(protocol).toHaveTextContent(/physically safe/i);
    expect(protocol).toHaveTextContent(/force-kills any running agent/i);
    expect(protocol).toHaveTextContent(/no heat-off/i);
    expect(protocol).not.toHaveTextContent(/refuses to start over a live one/i);
  });

  it("#537 PR #544 round-2 P2 fold: the field protocol locates the commands 'On the roaster host' — the launcher's dashboard is a LAN URL, so the reader may not be on the roaster host", () => {
    // scripts/roast-live.sh advertises http://<LAN IP>:PORT/, so the
    // operator may be reading this view on a phone/laptop that is NOT the
    // roaster host — curl localhost there probes the WRONG machine, and
    // lsof can only ever run ON the roaster host anyway.
    healthState.isSuccess = true;
    healthState.data = { active_run_id: "run-42", instance_id: "server-b" };
    renderPage("/live", { expectedInstanceId: "server-a" });

    const locator = screen.getByTestId("live-status-unknown-host-locator");
    expect(locator).toHaveTextContent(/on the roaster host/i);
    expect(locator).toHaveTextContent(/the machine running the agent/i);
    // The locator must precede the commands it's introducing.
    const protocol = screen.getByTestId("live-status-unknown-field-protocol");
    const html = protocol.innerHTML;
    expect(html.indexOf('data-testid="live-status-unknown-host-locator"')).toBeLessThan(
      html.indexOf("curl localhost"),
    );
  });

  it("#537 PR #544 round-2 P3 fold: the field protocol notes the proxied-SPA port caveat next to the interpolated command", () => {
    // window.location.port only equals the agent's port in the field
    // deployment (a built SPA served BY the agent) — behind a dev/preview
    // proxy the page's origin is the proxy's port, not the agent's, and
    // this component cannot reliably detect that case. Covered with a copy
    // clause rather than code plumbing.
    healthState.isSuccess = true;
    healthState.data = { active_run_id: "run-42", instance_id: "server-b" };
    renderPage("/live", { expectedInstanceId: "server-a" });

    const protocol = screen.getByTestId("live-status-unknown-field-protocol");
    expect(protocol).toHaveTextContent(/this page's origin/i);
    expect(protocol).toHaveTextContent(/proxy/i);
    expect(protocol).toHaveTextContent(/substitute the agent's real port/i);
  });

  it("#537 PR #544 P1 fold: the instance-mismatch view puts the physical safety step FIRST, ahead of the field protocol — a launcher restart force-kills without heat-off", () => {
    healthState.isSuccess = true;
    healthState.data = { active_run_id: "run-42", instance_id: "server-b" };
    renderPage("/live", { expectedInstanceId: "server-a" });

    const view = screen.getByTestId("live-status-unknown");
    const safetyStep = screen.getByTestId("live-status-unknown-safety-step");
    const protocol = screen.getByTestId("live-status-unknown-field-protocol");
    expect(safetyStep).toHaveTextContent(/physical emergency stop/i);
    expect(safetyStep).toHaveTextContent(/power switch/i);
    expect(safetyStep).toHaveTextContent(/force-kills the controller without a heat-off/i);
    // ORDER matters: the safety step must precede the field protocol in the
    // rendered DOM, so it reads first regardless of viewport/scroll state.
    const html = view.innerHTML;
    expect(html.indexOf('data-testid="live-status-unknown-safety-step"')).toBeLessThan(
      html.indexOf('data-testid="live-status-unknown-field-protocol"'),
    );
    expect(protocol).toHaveTextContent(/launcher/i);
  });

  it("#537 PR #544 P2 fold: the field-protocol commands interpolate the PAGE'S OWN origin port, not a hardcoded 8000", () => {
    // The CLI accepts --port and the launcher reads PORT — a hardcoded
    // localhost:8000 would send the operator to inspect the WRONG socket on
    // a non-default port, finding nothing, then reloading and destroying
    // the evidence. jsdom's default test location is localhost:3000; this
    // test forces a DIFFERENT, clearly-non-default port to prove the
    // commands genuinely derive from window.location, not a lucky match.
    const originalLocation = window.location;
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { ...originalLocation, port: "9090" },
    });
    try {
      healthState.isSuccess = true;
      healthState.data = { active_run_id: "run-42", instance_id: "server-b" };
      renderPage("/live", { expectedInstanceId: "server-a" });
      const protocol = screen.getByTestId("live-status-unknown-field-protocol");
      expect(protocol).toHaveTextContent(/curl localhost:9090\/api\/health/i);
      expect(protocol).toHaveTextContent(/lsof -nP -iTCP:9090 -sTCP:LISTEN/i);
      expect(protocol).not.toHaveTextContent(/:8000/);
    } finally {
      Object.defineProperty(window, "location", {
        configurable: true,
        value: originalLocation,
      });
    }
  });

  it("#537: the GENERIC health-error variant does NOT show the field protocol or the safety step — it has no instance-id evidence to lose on reload", () => {
    healthState.isSuccess = false;
    healthState.isError = true;
    healthState.data = undefined;
    renderPage("/live", { expectedInstanceId: "server-a" });
    expect(screen.queryByTestId("live-status-unknown-field-protocol")).toBeNull();
    expect(screen.queryByTestId("live-status-unknown-safety-step")).toBeNull();
  });

  it("shows the dashboard normally when the instance_id MATCHES", () => {
    healthState.isSuccess = true;
    healthState.data = { active_run_id: "run-42", instance_id: "server-a" };
    renderPage("/live", { expectedInstanceId: "server-a" });
    expect(screen.getByTestId("dashboard-stub")).toBeInTheDocument();
    expect(screen.queryByTestId("live-status-unknown")).toBeNull();
  });

  it("does not gate on instance_id at all when there is no navigation state (a reload, direct link, or back/forward)", () => {
    // #516 module doc: router state deliberately does NOT survive a reload —
    // there is no "prior start" to compare against, so the page must fall
    // back to its plain fresh-health gate with no mismatch to check, even
    // though `instance_id` itself is present on the health response.
    healthState.isSuccess = true;
    healthState.data = { active_run_id: "run-42", instance_id: "server-b" };
    renderPage("/live"); // no state
    expect(screen.getByTestId("dashboard-stub")).toBeInTheDocument();
    expect(screen.queryByTestId("live-status-unknown")).toBeNull();
  });

  it("does not gate on instance_id when the navigation state is present but malformed — an unrelated key (defensive narrowing, not a cast)", () => {
    healthState.isSuccess = true;
    healthState.data = { active_run_id: "run-42", instance_id: "server-b" };
    renderPage("/live", { someUnrelatedKey: "server-a" });
    expect(screen.getByTestId("dashboard-stub")).toBeInTheDocument();
    expect(screen.queryByTestId("live-status-unknown")).toBeNull();
  });

  it("does not gate on instance_id when expectedInstanceId is present but NOT a string (qa nit — the typeof check must reject this, not just an unrelated key)", () => {
    healthState.isSuccess = true;
    healthState.data = { active_run_id: "run-42", instance_id: "server-b" };
    // expectedInstanceId is typed as `string`, but router state is `unknown`
    // at runtime — a non-string value (e.g. a stray number) must be rejected
    // by the `typeof ... === "string"` narrowing, not coerced or compared.
    renderPage("/live", { expectedInstanceId: 12345 });
    expect(screen.getByTestId("dashboard-stub")).toBeInTheDocument();
    expect(screen.queryByTestId("live-status-unknown")).toBeNull();
  });

  it("shows the ORIGINAL generic message (not the mismatch one) for a genuine persistent health error, even with navigation state present", () => {
    healthState.isSuccess = false;
    healthState.isError = true;
    healthState.data = undefined;
    renderPage("/live", { expectedInstanceId: "server-a" });
    const view = screen.getByTestId("live-status-unknown");
    expect(view).toHaveAttribute("data-variant", "health-error");
    expect(screen.getByTestId("live-status-unknown-message")).toHaveTextContent(
      /could not reach the agent/i,
    );
  });

  it("holds the loading state (does not gate on instance_id) until health is genuinely fresh", () => {
    healthState.isSuccess = true;
    healthState.isFresh = false;
    healthState.data = { active_run_id: "run-42", instance_id: "server-b" };
    renderPage("/live", { expectedInstanceId: "server-a" });
    expect(screen.getByTestId("live-page-loading")).toBeInTheDocument();
    expect(screen.queryByTestId("live-status-unknown")).toBeNull();
  });

  it("round-2 Codex fold: FAILS CLOSED when the fresh health read is missing instance_id entirely (not just a different value) while armed", () => {
    // The process that genuinely accepted the start always includes
    // instance_id (this same commit guarantees it on every code path), so an
    // ABSENT field while armed is itself impostor evidence — an older-code
    // process that predates this feature, not a benign gap to shrug past.
    healthState.isSuccess = true;
    healthState.data = { active_run_id: "run-42" }; // no instance_id at all
    renderPage("/live", { expectedInstanceId: "server-a" });
    const view = screen.getByTestId("live-status-unknown");
    expect(view).toHaveAttribute("data-variant", "instance-mismatch");
    expect(screen.queryByTestId("dashboard-stub")).toBeNull();
  });

  it("round-2 Codex fold: ONE-SHOT DISARM — a match at one health read must not be re-armed by a DIFFERENT id at a LATER health read (a legitimate restart)", async () => {
    // Simulates the scenario Codex flagged: location.state persists for the
    // whole history entry's lifetime, so without a disarm latch a genuine
    // later restart (a new process id while an active/recovery run exists)
    // would false-alarm this check indefinitely, blocking the dashboard
    // exactly when the operator needs it. Restart handling belongs to the
    // fresh-health-gate/recovery flow, not this one-shot start-confirmation.
    healthState.isSuccess = true;
    healthState.data = { active_run_id: "run-42", instance_id: "server-a" };
    const { rerender } = renderPage("/live", { expectedInstanceId: "server-a" });

    // First read: matches — dashboard renders, and the check disarms.
    await waitFor(() => expect(screen.getByTestId("dashboard-stub")).toBeInTheDocument());
    expect(screen.queryByTestId("live-status-unknown")).toBeNull();

    // A LATER health read reports a DIFFERENT instance_id — e.g. a
    // legitimate agent restart mid-roast. Because the check already
    // disarmed on the first match, this must NOT be treated as a mismatch.
    healthState.data = { active_run_id: "run-42", instance_id: "server-c" };
    rerender();

    expect(screen.getByTestId("dashboard-stub")).toBeInTheDocument();
    expect(screen.queryByTestId("live-status-unknown")).toBeNull();
  });
});

describe("LivePage — persistent last-completed summary from history (#523)", () => {
  it("shows LiveFinishedView sourced from history when idle and a completed run exists, with NO session-sticky state", async () => {
    // #532 round 2: the history-derived id is now verified fresh
    // (fetchTerminalOutcome, staleTime:0) before LiveFinishedView mounts —
    // an async gate, so this assertion must wait rather than check synchronously.
    healthState.isSuccess = true;
    healthState.data = { active_run_id: null };
    historyState.data = { runs: [historyRunFixture(FIXTURE_FINISHED_RUN_ID, "completed")] };
    historyState.isFresh = true;
    renderPage();
    await waitFor(() =>
      expect(screen.getByTestId("live-finished-view")).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("live-no-roasts-view")).toBeNull();
  });

  it("uses the newest completed run when history has multiple outcomes (newest-first list)", async () => {
    healthState.isSuccess = true;
    healthState.data = { active_run_id: null };
    historyState.data = {
      runs: [
        historyRunFixture("run-most-recent-faulted", "faulted"),
        historyRunFixture(FIXTURE_FINISHED_RUN_ID, "completed"),
      ],
    };
    historyState.isFresh = true;
    renderPage();
    await waitFor(() =>
      expect(screen.getByTestId("live-finished-view")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("live-finished-view-detail")).toHaveAttribute(
      "href",
      `/roasts/${FIXTURE_FINISHED_RUN_ID}`,
    );
  });

  it("a reload (fresh mount, no session state) shows the SAME persistent summary — survives reload (#523)", async () => {
    // This is the behaviour #523 changes: pre-#523 a reload lost the
    // session-only sticky and fell back to a start form. Now it falls back to
    // the history-derived id instead (verified fresh, #532 round 2).
    healthState.isSuccess = true;
    healthState.data = { active_run_id: null };
    historyState.data = { runs: [historyRunFixture(FIXTURE_FINISHED_RUN_ID, "completed")] };
    historyState.isFresh = true;
    renderPage();
    await waitFor(() =>
      expect(screen.getByTestId("live-finished-view")).toBeInTheDocument(),
    );
  });

  it("'Start next roast' links to /start (no local state to clear, #523)", async () => {
    healthState.isSuccess = true;
    healthState.data = { active_run_id: null };
    historyState.data = { runs: [historyRunFixture(FIXTURE_FINISHED_RUN_ID, "completed")] };
    historyState.isFresh = true;
    renderPage();
    await waitFor(() =>
      expect(screen.getByTestId("live-finished-start-next")).toHaveAttribute("href", "/start"),
    );
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
    //
    // #532 round 2: TWO independent effects now call api.roast — the
    // session-sticky path (for "run-faulted-2") AND the history-detail
    // verification (for FIXTURE_FINISHED_RUN_ID, the older run). Their
    // firing order is not guaranteed, so the mock discriminates by the
    // run id ARGUMENT rather than by call order (a single
    // mockResolvedValueOnce would be consumed by whichever call happens
    // to fire first, which could be either one).
    roastApiMock.mockImplementation(async (runId: string) =>
      runId === "run-faulted-2"
        ? { ...(FIXTURE_FINISHED_DETAIL as RoastDetail), outcome: "faulted" }
        : { ...(FIXTURE_FINISHED_DETAIL as RoastDetail), outcome: "completed" },
    );
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

describe("LivePage — history error (#532 Codex follow-up)", () => {
  it("shows a neutral history-unknown state (NEVER the false 'no roasts ever' claim) when history persistently errors", () => {
    healthState.isSuccess = true;
    healthState.data = { active_run_id: null };
    historyState.isError = true;
    historyState.isFresh = true; // isError implies settled (mirrors health)
    historyState.data = undefined;
    renderPage();
    expect(screen.getByTestId("live-history-unknown")).toBeInTheDocument();
    expect(screen.queryByTestId("live-no-roasts-view")).toBeNull();
    expect(screen.queryByTestId("live-finished-view")).toBeNull();
  });

  it("still shows the session-sticky summary on a history error — it doesn't depend on history at all", async () => {
    // A run just finished THIS session (stickyCompletedRunId latches from
    // fetchTerminalOutcome, independent of the history query). History then
    // fails — the summary must still render from the session-sticky path.
    healthState.isSuccess = true;
    healthState.data = { active_run_id: "run-sticky" };
    const { rerender } = renderPage();
    expect(screen.getByTestId("dashboard-stub")).toBeInTheDocument();

    healthState.data = { active_run_id: null };
    historyState.isError = true;
    historyState.data = undefined;
    rerender();

    await waitFor(() =>
      expect(screen.getByTestId("live-finished-view")).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("live-history-unknown")).toBeNull();
  });

  it("navigating the history-unknown reload link reaches a fresh /live mount", () => {
    healthState.isSuccess = true;
    healthState.data = { active_run_id: null };
    historyState.isError = true;
    historyState.isFresh = true;
    historyState.data = undefined;
    renderPage();
    expect(screen.getByTestId("live-history-unknown-reload")).toHaveAttribute("href", "/live");
  });
});

describe("LivePage — transition-flash guard (#532 Codex follow-up)", () => {
  it("does NOT flash an OLDER completed run's summary while the just-finished run's terminal-outcome fetch is still in flight", async () => {
    // An older completed run already sits in history — lastCompletedRunId
    // would resolve to it immediately. A NEW run then finishes: the
    // transition fires fetchTerminalOutcome, which we stall deliberately so
    // the test can assert the render DURING that window, before stickyCompletedRunId
    // has a chance to latch.
    historyState.data = { runs: [historyRunFixture("run-older-completed", "completed")] };

    let resolveRoast: (detail: RoastDetail) => void = () => {};
    roastApiMock.mockImplementationOnce(
      () =>
        new Promise<RoastDetail>((resolve) => {
          resolveRoast = resolve;
        }),
    );

    healthState.isSuccess = true;
    healthState.data = { active_run_id: "run-just-finished" };
    const { rerender } = renderPage();
    expect(screen.getByTestId("dashboard-stub")).toBeInTheDocument();

    // The run finishes — active_run_id goes null, firing fetchTerminalOutcome
    // for run-just-finished (stalled above).
    healthState.data = { active_run_id: null };
    rerender();

    // While that fetch is in flight, the page must hold — NEVER render the
    // older run's summary as an intermediate flash.
    await waitFor(() => expect(screen.getByTestId("live-page-loading")).toBeInTheDocument());
    expect(screen.queryByTestId("live-finished-view")).toBeNull();

    // Release the stalled fetch with a genuine "completed" outcome.
    resolveRoast({ ...(FIXTURE_FINISHED_DETAIL as RoastDetail), outcome: "completed" });

    // Now the JUST-FINISHED run's summary shows (session-sticky latches the
    // run id that just transitioned, "run-just-finished" — NOT the older
    // history-derived fallback's id, "run-older-completed").
    await waitFor(() =>
      expect(screen.getByTestId("live-finished-view")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("live-finished-view-detail")).toHaveAttribute(
      "href",
      "/roasts/run-just-finished",
    );
  });

  it("falls through cleanly to the older run's summary once the terminal fetch resolves NON-completed — no flash of a WRONG (stale) intermediate either", async () => {
    // Same setup, but the just-finished run turns out to be faulted — the
    // page must fall through to the older completed run's summary only
    // AFTER the fetch resolves, never flashing it mid-fetch (transition-flash
    // guard applies regardless of the eventual outcome).
    historyState.data = { runs: [historyRunFixture("run-older-completed-2", "completed")] };

    let resolveRoast: (detail: RoastDetail) => void = () => {};
    roastApiMock.mockImplementationOnce(
      () =>
        new Promise<RoastDetail>((resolve) => {
          resolveRoast = resolve;
        }),
    );

    healthState.isSuccess = true;
    healthState.data = { active_run_id: "run-will-fault" };
    const { rerender } = renderPage();

    healthState.data = { active_run_id: null };
    rerender();

    await waitFor(() => expect(screen.getByTestId("live-page-loading")).toBeInTheDocument());
    expect(screen.queryByTestId("live-finished-view")).toBeNull();

    resolveRoast({ ...(FIXTURE_FINISHED_DETAIL as RoastDetail), outcome: "faulted" });

    // Falls through to the persistent (older) fallback only once resolved.
    await waitFor(() =>
      expect(screen.getByTestId("live-finished-view")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("live-finished-view-detail")).toHaveAttribute(
      "href",
      "/roasts/run-older-completed-2",
    );
  });

  it("#532 round 2: the older run's summary is not in the DOM the INSTANT rerender() returns — checked synchronously, plus an honest note on what this harness can and cannot prove", async () => {
    // The two tests above prove the FINAL state is correct via `waitFor`
    // (which only samples the DOM periodically) — that alone doesn't rule
    // out a single-frame flash between polls. This asserts the DOM state
    // DIRECTLY, synchronously, the instant `rerender()` returns.
    //
    // HONEST LIMITATION, recorded rather than hidden behind a green check:
    // React Testing Library's `act()` (which every `render`/`rerender` call
    // is wrapped in) flushes PASSIVE EFFECTS (`useEffect`) SYNCHRONOUSLY in
    // test mode, before returning control to the test — confirmed
    // empirically (a probe `console.log` placed immediately after
    // `rerender()`, with the round-1 `prevRunIdRef` guard removed, still
    // showed the correct/held state). In a REAL BROWSER, `useEffect` runs
    // AFTER paint — genuinely later than this test harness's synchronous
    // `act()` flush — so the fix's `prevRunIdRef.current` check (reading the
    // ref during RENDER, before that render's own effects have run) is
    // necessary for the real one-frame gap Codex described, but this
    // component-test harness cannot reproduce that specific timing to prove
    // the OLD code was broken in the way described. Tried: a
    // MutationObserver (microtask-queued, so it also doesn't see an
    // intermediate commit inside one `act()` flush) and a synchronous
    // console probe (same result) — both confirmed the harness always
    // settles past the whole effect chain within one `rerender()`. Verified
    // manually instead (recorded in the commit message): reverting the
    // `prevRunIdRef.current !== null ||` half of the guard to leave only
    // `terminalFetchPending` still passes every test in this file, including
    // this one — proof this specific assertion does NOT, on its own, gate
    // merge on the round-1 timing fix. The ref-read is still the objectively
    // correct fix (state set inside `useEffect` cannot possibly reflect a
    // render that hasn't committed yet, which is the real hazard in a
    // browser); kept it and this assertion for the real behaviour it DOES
    // cover (the composed hold from BOTH mechanisms holds correctly through
    // a real async transition) rather than delete coverage over a harness
    // gap, but the two-line accounting above is the honest scope of what a
    // green run here actually proves.
    //
    // Isolating finding #1's fix from finding #2's (the fresh-history-detail
    // hold) requires the older run's id to already be HISTORY-FRESHNESS-
    // VERIFIED before the transition under test fires — otherwise finding
    // #2's own hold (waiting on the history-detail fetch) independently
    // covers this exact scenario too. So: mount idle with the older run in
    // history first (letting that verification resolve), THEN start a run
    // and let it finish.
    historyState.data = { runs: [historyRunFixture("run-older-preverified", "completed")] };
    healthState.isSuccess = true;
    healthState.data = { active_run_id: null };
    const { rerender } = renderPage();
    await waitFor(() =>
      expect(screen.getByTestId("live-finished-view-detail")).toHaveAttribute(
        "href",
        "/roasts/run-older-preverified",
      ),
    );

    // Now a run starts and finishes — the terminal-outcome fetch for it is
    // stalled so the test can inspect the DOM mid-transition.
    let resolveRoast: (detail: RoastDetail) => void = () => {};
    roastApiMock.mockImplementationOnce(
      () =>
        new Promise<RoastDetail>((resolve) => {
          resolveRoast = resolve;
        }),
    );
    healthState.data = { active_run_id: "run-just-finished-verified" };
    rerender();
    expect(screen.getByTestId("dashboard-stub")).toBeInTheDocument();

    // Fire the transition — this is the exact commit round-2 fixed.
    healthState.data = { active_run_id: null };
    rerender();

    // SYNCHRONOUS check, no await: the older run's summary must already be
    // gone from the DOM the instant this line runs, not just eventually.
    expect(screen.queryByTestId("live-finished-view")).toBeNull();
    expect(screen.getByTestId("live-page-loading")).toBeInTheDocument();

    resolveRoast({ ...(FIXTURE_FINISHED_DETAIL as RoastDetail), outcome: "completed" });
    await waitFor(() =>
      expect(screen.getByTestId("live-finished-view")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("live-finished-view-detail")).toHaveAttribute(
      "href",
      "/roasts/run-just-finished-verified",
    );
  });
});
