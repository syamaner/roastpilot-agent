/**
 * #513 / #523 real-integration regression coverage for the start-roast flow
 * and the `/live` handoff.
 *
 * Component-level specs (`StartRoastView.test.tsx`, `LivePage.test.tsx`) stub
 * `useHealth`/`useHistory` directly, which is exactly the layer that hid #513:
 * those tests could (and did) pass while the real handoff — a real
 * `useHealth()` backed by a real `QueryClient`, driving the real
 * `StartRoastForm` through a real submit — was broken. This file drives the
 * actual `fetch` boundary instead, so the assertions exercise the genuine
 * wiring an operator's browser would run.
 *
 * #523 moved the ONLY start-form surface to `/start` (`/live` never renders
 * one) — the start-flow tests below drive `/start`, then verify the real
 * handoff to `/live` reaching the live dashboard. Also covers: a transient
 * post-restart `/health` failure recovering via retry; a 409 (double-submit /
 * already-active) leaving the form visible and usable, never silently reset;
 * the active-run banner on `/start`; the Codex follow-up (#514/#515 review) —
 * a REAL `QueryClient` cache primed with a within-staleTime "idle" health
 * snapshot must not let the bare form render before the forced fresh refetch
 * resolves, even when that fresh read reveals a run that started elsewhere in
 * the last 30s; and (#523) `/live`'s idle state doing a REAL `GET /api/roasts`
 * to find the persistent last-completed summary, surviving what a reload does.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { roastKeys } from "@/hooks/queries";
import { LivePage } from "@/pages/live/LivePage";
import { StartRoastView } from "@/pages/home/StartRoastView";

vi.mock("@/pages/dashboard/DashboardPage", () => ({
  DashboardPage: () => <div data-testid="dashboard-stub" />,
}));

afterEach(cleanup);

/** A minimal fake `/api/health` + `/api/roasts` + `/api/bean-profiles` backend. */
function fakeFetch(opts: {
  activeRunId: () => string | null;
  onPost: () => Response | Promise<Response>;
  healthStatus?: () => number;
  history?: () => { runs: unknown[] };
  // #516: the instance_id THIS fake process's /api/health reports — defaults
  // to a fixed id so every pre-#516 test in this file keeps its POST's
  // (implicit, real-server-shaped) instance_id matching health's, unless a
  // test deliberately overrides it to simulate an impostor process.
  instanceId?: () => string;
}): typeof fetch {
  return vi.fn(async (url: string, init?: RequestInit) => {
    const u = String(url);
    if (u === "/api/health") {
      const status = opts.healthStatus?.() ?? 200;
      if (status !== 200) {
        return new Response(JSON.stringify({ detail: "unavailable" }), { status });
      }
      return new Response(
        JSON.stringify({
          status: "ok",
          version: "test",
          instance_id: opts.instanceId?.() ?? "server-a",
          mcp_child: "running",
          mcp_hardware_clear_required: false,
          mcp_teardown_incident_id: null,
          active_run_id: opts.activeRunId(),
        }),
        { status: 200 },
      );
    }
    if (u === "/api/roasts" && init?.method === "POST") {
      return opts.onPost();
    }
    if (u === "/api/roasts") {
      return new Response(JSON.stringify(opts.history?.() ?? { runs: [] }), { status: 200 });
    }
    if (u === "/api/bean-profiles") {
      return new Response(JSON.stringify({ profiles: [] }), { status: 200 });
    }
    throw new Error(`unexpected fetch ${u} ${init?.method ?? "GET"}`);
  }) as unknown as typeof fetch;
}

function renderLive() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const result = render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/live"]}>
        <Routes>
          <Route path="/live" element={<LivePage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { client, ...result };
}

function renderStart() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const result = render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/start"]}>
        <Routes>
          <Route path="/start" element={<StartRoastView />} />
          <Route path="/live" element={<LivePage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { client, ...result };
}

function fillMinimum() {
  fireEvent.change(screen.getByTestId("start-roast-name"), { target: { value: "Morning batch" } });
  fireEvent.change(screen.getByTestId("start-roast-bean_origin"), {
    target: { value: "Ethiopia Guji" },
  });
  fireEvent.change(screen.getByTestId("start-roast-bean_weight_grams"), { target: { value: "250" } });
}

describe("#523 real-integration — /start start flow, real handoff to /live", () => {
  it("a clean 201 navigates to /live, which reaches the real live dashboard", async () => {
    let activeRunId: string | null = null;
    global.fetch = fakeFetch({
      activeRunId: () => activeRunId,
      onPost: () => {
        activeRunId = "run-new";
        return new Response(JSON.stringify({ id: "run-new" }), { status: 201 });
      },
    });

    renderStart();
    await waitFor(() => expect(screen.getByTestId("start-roast-view")).toBeInTheDocument());
    fillMinimum();
    fireEvent.submit(screen.getByTestId("start-roast-form"));

    // #403/#513: navigate("/live") fires unconditionally on the proven 201 —
    // /live's own fresh health read (not a cache write from this page) then
    // reaches the real dashboard.
    await waitFor(() => expect(screen.getByTestId("dashboard-stub")).toBeInTheDocument());
    expect(screen.queryByTestId("start-roast-view")).toBeNull();
  });

  it("#516: a 201 followed by a DIFFERENT process answering /api/health surfaces the distinct instance-mismatch state, real fetch boundary end-to-end", async () => {
    // Simulates the #513 port-impostor incident through the real /start → POST
    // → navigate("/live") → real useFreshHealthGate() chain, not a stubbed
    // hook: the 201 response reports "server-a" (this fake process's own
    // instance_id), but by the time /live's forced-fresh health read lands, a
    // DIFFERENT process ("server-b") answers instead — the exact SO_REUSEADDR
    // scenario docs/recent-fixes.md describes.
    let activeRunId: string | null = null;
    global.fetch = fakeFetch({
      activeRunId: () => activeRunId,
      instanceId: () => "server-b",
      onPost: () => {
        activeRunId = "run-new";
        return new Response(
          JSON.stringify({ id: "run-new", instance_id: "server-a" }),
          { status: 201 },
        );
      },
    });

    renderStart();
    await waitFor(() => expect(screen.getByTestId("start-roast-view")).toBeInTheDocument());
    fillMinimum();
    fireEvent.submit(screen.getByTestId("start-roast-form"));

    await waitFor(() => expect(screen.getByTestId("live-status-unknown")).toBeInTheDocument());
    expect(screen.getByTestId("live-status-unknown")).toHaveAttribute(
      "data-variant",
      "instance-mismatch",
    );
    // Never the real dashboard — a wrong process's active_run_id (even
    // though this fake DOES report one) must not be trusted.
    expect(screen.queryByTestId("dashboard-stub")).toBeNull();
  });

  it("a 409 (double-submit / already active) leaves the form visible, usable, and un-reset", async () => {
    global.fetch = fakeFetch({
      activeRunId: () => null,
      onPost: () =>
        new Response(JSON.stringify({ detail: "a roast is already active" }), { status: 409 }),
    });

    renderStart();
    await waitFor(() => expect(screen.getByTestId("start-roast-view")).toBeInTheDocument());
    fillMinimum();
    fireEvent.submit(screen.getByTestId("start-roast-form"));

    await waitFor(() => expect(screen.getByTestId("start-roast-error")).toBeInTheDocument());
    expect(screen.getByTestId("start-roast-error")).toHaveTextContent(/already active/i);
    // Never navigates away, and never a reset that erases what the operator typed.
    expect(screen.getByTestId("start-roast-view")).toBeInTheDocument();
    expect(screen.getByTestId("start-roast-name")).toHaveValue("Morning batch");
    expect(screen.getByTestId("start-roast-submit")).toBeEnabled();
  });

  it("#513 Codex follow-up: a within-staleTime cached idle snapshot never lets the bare form render before a genuinely fresh read", async () => {
    // Mirrors the app's real QueryClient config (staleTime: 30_000 — see
    // web/src/lib/queryClient.ts) so this test exercises the actual hazard: a
    // cache primed by an EARLIER health read (e.g. the home page) within the
    // last 30s must not let a fresh mount of /start render the bare form from
    // that stale "idle" snapshot, even though `isSuccess` would be true
    // instantly with NO network call at all under plain `useHealth` semantics.
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, staleTime: 30_000 } },
    });
    client.setQueryData(roastKeys.health, {
      status: "ok",
      version: "test",
      mcp_child: "running",
      mcp_hardware_clear_required: false,
      mcp_teardown_incident_id: null,
      active_run_id: null,
    });

    let healthCalls = 0;
    let resolveHealth: () => void = () => {};
    global.fetch = vi.fn(async (url: string) => {
      const u = String(url);
      if (u === "/api/health") {
        healthCalls += 1;
        // The forced refetch (useFreshHealthGate's refetchOnMount: "always")
        // stalls until released — giving the test a deterministic window to
        // assert the hold fires from stale data before the fresh read lands.
        await new Promise<void>((resolve) => {
          resolveHealth = resolve;
        });
        // The fresh read genuinely confirms idle — the scenario Codex flagged
        // was the STALE cache rendering the form before this resolves at all,
        // not any particular fresh outcome.
        return new Response(
          JSON.stringify({
            status: "ok",
            version: "test",
            mcp_child: "running",
            mcp_hardware_clear_required: false,
            mcp_teardown_incident_id: null,
            active_run_id: null,
          }),
          { status: 200 },
        );
      }
      if (u === "/api/roasts") {
        // #535: StartRoastView now also gates on useFreshHistoryGate (the
        // stale-session source) — this test's focus is the HEALTH hold, so
        // history resolves cleanly and immediately with no open runs, never
        // itself becoming the blocking factor being asserted on here.
        return new Response(JSON.stringify({ runs: [] }), { status: 200 });
      }
      if (u === "/api/bean-profiles") {
        return new Response(JSON.stringify({ profiles: [] }), { status: 200 });
      }
      throw new Error(`unexpected fetch ${u}`);
    }) as unknown as typeof fetch;

    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/start"]}>
          <Routes>
            <Route path="/start" element={<StartRoastView />} />
            <Route path="/live" element={<LivePage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    await waitFor(() => expect(healthCalls).toBe(1));
    expect(screen.getByTestId("start-roast-loading")).toBeInTheDocument();
    expect(screen.queryByTestId("start-roast-view")).toBeNull();
    expect(screen.queryByTestId("start-roast-active-run-banner")).toBeNull();

    resolveHealth();
    await waitFor(() => expect(screen.getByTestId("start-roast-view")).toBeInTheDocument());
    expect(screen.queryByTestId("start-roast-loading")).toBeNull();
  });
});

describe("#513 real-integration — /start active-run guard", () => {
  it("never renders a bare form when the server already reports an active run", async () => {
    global.fetch = fakeFetch({
      activeRunId: () => "run-already-active",
      onPost: () => new Response(JSON.stringify({ detail: "a roast is already active" }), { status: 409 }),
    });

    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/start"]}>
          <Routes>
            <Route path="/start" element={<StartRoastView />} />
            <Route path="/live" element={<LivePage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    await waitFor(() =>
      expect(screen.getByTestId("start-roast-active-run-banner")).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("start-roast-form")).toBeNull();

    fireEvent.click(screen.getByTestId("start-roast-active-run-link"));
    await waitFor(() => expect(screen.getByTestId("dashboard-stub")).toBeInTheDocument());
  });

  it("#513 Codex follow-up: a within-staleTime cached idle snapshot never lets the bare form render before a genuinely fresh read", async () => {
    // Same scenario as the loading-hold test above, asserted via the banner
    // outcome instead (an active run resolves, not an idle one).
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, staleTime: 30_000 } },
    });
    client.setQueryData(roastKeys.health, {
      status: "ok",
      version: "test",
      mcp_child: "running",
      mcp_hardware_clear_required: false,
      mcp_teardown_incident_id: null,
      active_run_id: null,
    });

    let healthCalls = 0;
    let resolveHealth: () => void = () => {};
    global.fetch = vi.fn(async (url: string) => {
      const u = String(url);
      if (u === "/api/health") {
        healthCalls += 1;
        await new Promise<void>((resolve) => {
          resolveHealth = resolve;
        });
        return new Response(
          JSON.stringify({
            status: "ok",
            version: "test",
            mcp_child: "running",
            mcp_hardware_clear_required: false,
            mcp_teardown_incident_id: null,
            active_run_id: "run-from-another-tab",
          }),
          { status: 200 },
        );
      }
      if (u === "/api/roasts") {
        // #535: StartRoastView now also gates on useFreshHistoryGate (the
        // stale-session source) — this test's focus is the HEALTH hold, so
        // history resolves cleanly and immediately with no open runs, never
        // itself becoming the blocking factor being asserted on here.
        return new Response(JSON.stringify({ runs: [] }), { status: 200 });
      }
      if (u === "/api/bean-profiles") {
        return new Response(JSON.stringify({ profiles: [] }), { status: 200 });
      }
      throw new Error(`unexpected fetch ${u}`);
    }) as unknown as typeof fetch;

    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/start"]}>
          <Routes>
            <Route path="/start" element={<StartRoastView />} />
            <Route path="/live" element={<LivePage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    await waitFor(() => expect(healthCalls).toBe(1));
    expect(screen.getByTestId("start-roast-loading")).toBeInTheDocument();
    expect(screen.queryByTestId("start-roast-form")).toBeNull();
    expect(screen.queryByTestId("start-roast-active-run-banner")).toBeNull();

    resolveHealth();
    await waitFor(() =>
      expect(screen.getByTestId("start-roast-active-run-banner")).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("start-roast-loading")).toBeNull();
    expect(screen.queryByTestId("start-roast-form")).toBeNull();
  });
});

describe("#523 real-integration — /live idle state reads REAL history for the persistent summary", () => {
  it("with no active run and a completed run in history, shows the last-completed summary from a real GET /api/roasts", async () => {
    global.fetch = fakeFetch({
      activeRunId: () => null,
      onPost: () => new Response(JSON.stringify({ detail: "unused" }), { status: 500 }),
      history: () => ({
        runs: [
          {
            id: "run-old-completed",
            started_at_utc: "2026-07-01T10:00:00Z",
            completed_at_utc: "2026-07-01T10:06:30Z",
            first_crack_at_utc: null,
            agent_phase: "complete",
            outcome: "completed",
            bean_origin: "Ethiopia Guji",
            bean_varietal: null,
            rating: null,
            development_percent: 18.7,
            advisor_consults: 0,
            advisor_clamped: 0,
            advisor_rejected: 0,
            advisor_failed: 0,
          },
        ],
      }),
    });

    renderLive();

    // No session-sticky state exists on this fresh mount — the summary must
    // come from the REAL history fetch, proving the persistence guarantee
    // (#523) a reload relies on.
    await waitFor(() => expect(screen.getByTestId("live-finished-view")).toBeInTheDocument());
    expect(screen.getByTestId("live-finished-view-detail")).toHaveAttribute(
      "href",
      "/roasts/run-old-completed",
    );
    expect(screen.queryByTestId("live-no-roasts-view")).toBeNull();
  });

  it("with no active run and no completed run in history, shows the neutral no-roasts state, never a form", async () => {
    global.fetch = fakeFetch({
      activeRunId: () => null,
      onPost: () => new Response(JSON.stringify({ detail: "unused" }), { status: 500 }),
      history: () => ({ runs: [] }),
    });

    renderLive();

    await waitFor(() => expect(screen.getByTestId("live-no-roasts-view")).toBeInTheDocument());
    expect(screen.getByTestId("live-no-roasts-start-link")).toHaveAttribute("href", "/start");
    expect(screen.queryByTestId("live-finished-view")).toBeNull();
    expect(screen.queryByTestId("start-roast-form")).toBeNull();
  });

  it("#532 round 2: does NOT render a STALE cached detail for the history-derived run — fetches it fresh before trusting it", async () => {
    // The exact hazard round 2 flagged: `roastKeys.detail(id)` might already
    // hold a MID-ROAST snapshot in the query cache for the same run id — left
    // over from an earlier dashboard/detail view in this same browser
    // session, cached with the app's default staleTime (30s), well within
    // which this render can land. Without the fresh-detail fetch,
    // `LiveFinishedView`'s `useRoast(runId)` would resolve synchronously from
    // that STALE cache entry (outcome: null, no drop temp yet) instead of the
    // server's genuine terminal snapshot.
    //
    // Scope note (mutation-tested, not just written and trusted): this DOES
    // catch removing the fresh-fetch effect entirely (verified — the mutant
    // renders the stale bean_origin and empty stat tiles). It does NOT catch
    // removing only the RENDER-TIME hold that waits for that fetch to
    // resolve (the effect alone still populates the cache before this
    // harness's synchronous `act()`-flushed render settles — the same
    // RTL passive-effect-timing limitation documented on the transition-
    // flash test above). The render-time hold matters for the real,
    // post-paint `useEffect` timing a browser has; this test proves the
    // fetch-before-trust DATA correctness, not that specific frame timing.
    const HISTORY_RUN_ID = "run-with-stale-cache";
    let detailFetchCount = 0;
    global.fetch = vi.fn(async (url: string, init?: RequestInit) => {
      const u = String(url);
      if (u === "/api/health") {
        return new Response(
          JSON.stringify({
            status: "ok",
            version: "test",
            mcp_child: "running",
            mcp_hardware_clear_required: false,
            mcp_teardown_incident_id: null,
            active_run_id: null,
          }),
          { status: 200 },
        );
      }
      if (u === "/api/roasts") {
        return new Response(
          JSON.stringify({
            runs: [
              {
                id: HISTORY_RUN_ID,
                started_at_utc: "2026-07-01T10:00:00Z",
                completed_at_utc: "2026-07-01T10:06:30Z",
                first_crack_at_utc: null,
                agent_phase: "complete",
                outcome: "completed",
                bean_origin: "Ethiopia Guji",
                bean_varietal: null,
                rating: null,
                development_percent: 18.7,
                advisor_consults: 0,
                advisor_clamped: 0,
                advisor_rejected: 0,
                advisor_failed: 0,
              },
            ],
          }),
          { status: 200 },
        );
      }
      if (u === `/api/roasts/${HISTORY_RUN_ID}`) {
        detailFetchCount += 1;
        // The REAL server response: a genuine terminal snapshot.
        return new Response(
          JSON.stringify({
            id: HISTORY_RUN_ID,
            agent_phase: "complete",
            profile: {
              name: "Test",
              bean_origin: "Ethiopia Guji — Fresh From Server",
              bean_varietal: null,
              bean_weight_grams: 250,
              charge_guidance_min_c: 170,
              charge_guidance_max_c: 200,
              initial_heat_percent: 80,
              initial_fan_percent: 30,
              target_drop_temp_c: 195,
              target_development_percent: 20,
            },
            outcome: "completed",
            started_at_utc: "2026-07-01T10:00:00Z",
            completed_at_utc: "2026-07-01T10:06:30Z",
            fault_reason: null,
            rating: null,
            notes: null,
            export_manifest: null,
          }),
          { status: 200 },
        );
      }
      throw new Error(`unexpected fetch ${u} ${init?.method ?? "GET"}`);
    }) as unknown as typeof fetch;

    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    // Seed a STALE, mid-roast cache entry for this exact run id — the
    // scenario an earlier same-session dashboard/detail view would leave
    // behind. Its bean_origin is deliberately different from the server's
    // "fresh" response so the test can tell which one actually rendered.
    client.setQueryData(roastKeys.detail(HISTORY_RUN_ID), {
      id: HISTORY_RUN_ID,
      agent_phase: "development",
      profile: {
        name: "Test",
        bean_origin: "STALE MID-ROAST SNAPSHOT",
        bean_varietal: null,
        bean_weight_grams: 250,
        charge_guidance_min_c: 170,
        charge_guidance_max_c: 200,
        initial_heat_percent: 80,
        initial_fan_percent: 30,
        target_drop_temp_c: 195,
        target_development_percent: 20,
      },
      outcome: null,
      started_at_utc: "2026-07-01T10:00:00Z",
      completed_at_utc: null,
      fault_reason: null,
      rating: null,
      notes: null,
      export_manifest: null,
    });

    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/live"]}>
          <Routes>
            <Route path="/live" element={<LivePage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    await waitFor(() => expect(screen.getByTestId("live-finished-view")).toBeInTheDocument());
    // The fresh server response's bean_origin renders — NOT the stale cache's.
    expect(screen.getByText("Ethiopia Guji — Fresh From Server")).toBeInTheDocument();
    expect(screen.queryByText("STALE MID-ROAST SNAPSHOT")).toBeNull();
    // A real fetch to the detail endpoint genuinely happened (proving this
    // isn't passing because the stale cache was never even present).
    expect(detailFetchCount).toBeGreaterThan(0);
  });

  it("#532 round 3: invalidates STALE cached telemetry for the history-derived run alongside the detail fetch", async () => {
    // Round 2 fixed the DETAIL cache; round 3's finding is that the summary's
    // headline stats + mini curve come from useTelemetry (downsample=1 for
    // stats, downsample=5 for the curve), which share the app's default 30s
    // staleTime — a same-session stale telemetry cache entry for this exact
    // run id could still render outdated numbers alongside a freshly-
    // verified detail. Seeds BOTH downsample variants stale, asserts the
    // fresh server values render for both.
    const HISTORY_RUN_ID = "run-with-stale-telemetry";
    let telemetryFetchCount = 0;
    const freshTelemetryPoints = [
      {
        tick: 13,
        elapsed_seconds: 390,
        charge_elapsed_seconds: 390,
        agent_phase: "cooling" as const,
        bean_temp_c: 199.0, // the FRESH drop temp — distinct from the stale value below
        env_temp_c: 220.0,
        bean_ror_c_per_min: 4.0,
        env_ror_c_per_min: 3.5,
        heat_level_percent: 0,
        fan_level_percent: 100,
        cooling_on: true,
        development_percent: 22.5,
      },
    ];
    const staleTelemetryPoints = [
      {
        tick: 6,
        elapsed_seconds: 180,
        charge_elapsed_seconds: 180,
        agent_phase: "roasting_pre_first_crack" as const,
        bean_temp_c: 185.0, // a STALE, pre-drop mid-roast reading
        env_temp_c: 210.0,
        bean_ror_c_per_min: 10.0,
        env_ror_c_per_min: 8.5,
        heat_level_percent: 70,
        fan_level_percent: 60,
        cooling_on: false,
        development_percent: null,
      },
    ];

    global.fetch = vi.fn(async (url: string, init?: RequestInit) => {
      const u = String(url);
      if (u === "/api/health") {
        return new Response(
          JSON.stringify({
            status: "ok",
            version: "test",
            mcp_child: "running",
            mcp_hardware_clear_required: false,
            mcp_teardown_incident_id: null,
            active_run_id: null,
          }),
          { status: 200 },
        );
      }
      if (u === "/api/roasts") {
        return new Response(
          JSON.stringify({
            runs: [
              {
                id: HISTORY_RUN_ID,
                started_at_utc: "2026-07-01T10:00:00Z",
                completed_at_utc: "2026-07-01T10:06:30Z",
                first_crack_at_utc: null,
                agent_phase: "complete",
                outcome: "completed",
                bean_origin: "Ethiopia Guji",
                bean_varietal: null,
                rating: null,
                development_percent: 22.5,
                advisor_consults: 0,
                advisor_clamped: 0,
                advisor_rejected: 0,
                advisor_failed: 0,
              },
            ],
          }),
          { status: 200 },
        );
      }
      if (u === `/api/roasts/${HISTORY_RUN_ID}`) {
        return new Response(
          JSON.stringify({
            id: HISTORY_RUN_ID,
            agent_phase: "complete",
            profile: {
              name: "Test",
              bean_origin: "Ethiopia Guji",
              bean_varietal: null,
              bean_weight_grams: 250,
              charge_guidance_min_c: 170,
              charge_guidance_max_c: 200,
              initial_heat_percent: 80,
              initial_fan_percent: 30,
              target_drop_temp_c: 195,
              target_development_percent: 20,
            },
            outcome: "completed",
            started_at_utc: "2026-07-01T10:00:00Z",
            completed_at_utc: "2026-07-01T10:06:30Z",
            fault_reason: null,
            rating: null,
            notes: null,
            export_manifest: null,
          }),
          { status: 200 },
        );
      }
      if (u.startsWith(`/api/roasts/${HISTORY_RUN_ID}/telemetry`)) {
        telemetryFetchCount += 1;
        return new Response(
          JSON.stringify({
            run_id: HISTORY_RUN_ID,
            downsample: u.includes("downsample=5") ? 5 : 1,
            point_count: freshTelemetryPoints.length,
            points: freshTelemetryPoints,
          }),
          { status: 200 },
        );
      }
      throw new Error(`unexpected fetch ${u} ${init?.method ?? "GET"}`);
    }) as unknown as typeof fetch;

    // Mirrors the app's REAL QueryClient config (staleTime: 30_000 — see
    // web/src/lib/queryClient.ts), NOT this file's usual bare
    // `{ retry: false }` client. `useTelemetry` has no per-call staleTime
    // override (unlike `fetchHistoryRunDetail`'s explicit `staleTime: 0`),
    // so it inherits whatever the CLIENT's default is — a bare client
    // defaults to TanStack's own library staleTime (0, "always stale"),
    // which would make the seeded cache below look stale regardless of
    // whether the fix's invalidation call exists, silently defeating this
    // test's whole premise. Confirmed empirically: this test passed even
    // with the invalidation call removed until this staleTime was added.
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, staleTime: 30_000 } },
    });
    // Seed BOTH downsample variants with a STALE mid-roast series.
    client.setQueryData(roastKeys.telemetry(HISTORY_RUN_ID, 1), {
      run_id: HISTORY_RUN_ID,
      downsample: 1,
      point_count: staleTelemetryPoints.length,
      points: staleTelemetryPoints,
    });
    client.setQueryData(roastKeys.telemetry(HISTORY_RUN_ID, 5), {
      run_id: HISTORY_RUN_ID,
      downsample: 5,
      point_count: staleTelemetryPoints.length,
      points: staleTelemetryPoints,
    });

    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/live"]}>
          <Routes>
            <Route path="/live" element={<LivePage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    await waitFor(() => expect(screen.getByTestId("live-finished-view")).toBeInTheDocument());
    // The FRESH drop temp (199 °C) renders — not the stale mid-roast reading.
    await waitFor(() => expect(screen.getByTestId("stat-drop-temp")).toHaveTextContent("199 °C"));
    // A real telemetry fetch genuinely happened for this run (proving the
    // stale seeded cache was actually invalidated, not just never read).
    expect(telemetryFetchCount).toBeGreaterThan(0);
  });

  it("#532 round 3: routes to the history-error state (never renders stale data under a 'verified' flag) when a CACHED detail exists but the forced refresh FAILS", async () => {
    // The fail-open/fail-closed split round 3 asked for: a cached detail
    // entry already exists for this run id (the stale-mid-roast scenario),
    // and the forced-fresh refetch FAILS (network/server error) rather than
    // succeeding. Failing open here would render the stale cached data under
    // a "freshly verified" flag — worse than the neutral error state, since
    // it looks trustworthy. Must show LiveHistoryUnknownView instead.
    const HISTORY_RUN_ID = "run-detail-refresh-fails";
    global.fetch = vi.fn(async (url: string, init?: RequestInit) => {
      const u = String(url);
      if (u === "/api/health") {
        return new Response(
          JSON.stringify({
            status: "ok",
            version: "test",
            mcp_child: "running",
            mcp_hardware_clear_required: false,
            mcp_teardown_incident_id: null,
            active_run_id: null,
          }),
          { status: 200 },
        );
      }
      if (u === "/api/roasts") {
        return new Response(
          JSON.stringify({
            runs: [
              {
                id: HISTORY_RUN_ID,
                started_at_utc: "2026-07-01T10:00:00Z",
                completed_at_utc: "2026-07-01T10:06:30Z",
                first_crack_at_utc: null,
                agent_phase: "complete",
                outcome: "completed",
                bean_origin: "Ethiopia Guji",
                bean_varietal: null,
                rating: null,
                development_percent: 18.7,
                advisor_consults: 0,
                advisor_clamped: 0,
                advisor_rejected: 0,
                advisor_failed: 0,
              },
            ],
          }),
          { status: 200 },
        );
      }
      // The forced-fresh detail refetch FAILS — genuinely, at the HTTP layer.
      if (u === `/api/roasts/${HISTORY_RUN_ID}`) {
        return new Response(JSON.stringify({ detail: "server error" }), { status: 500 });
      }
      if (u.startsWith(`/api/roasts/${HISTORY_RUN_ID}/telemetry`)) {
        return new Response(
          JSON.stringify({ run_id: HISTORY_RUN_ID, downsample: 1, point_count: 0, points: [] }),
          { status: 200 },
        );
      }
      throw new Error(`unexpected fetch ${u} ${init?.method ?? "GET"}`);
    }) as unknown as typeof fetch;

    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    // A cached detail entry ALREADY exists for this run — the exact
    // precondition for the fail-CLOSED branch (as opposed to fail-open,
    // which only applies when NO cache entry exists).
    client.setQueryData(roastKeys.detail(HISTORY_RUN_ID), {
      id: HISTORY_RUN_ID,
      agent_phase: "development",
      profile: {
        name: "Test",
        bean_origin: "STALE — MUST NOT RENDER",
        bean_varietal: null,
        bean_weight_grams: 250,
        charge_guidance_min_c: 170,
        charge_guidance_max_c: 200,
        initial_heat_percent: 80,
        initial_fan_percent: 30,
        target_drop_temp_c: 195,
        target_development_percent: 20,
      },
      outcome: null,
      started_at_utc: "2026-07-01T10:00:00Z",
      completed_at_utc: null,
      fault_reason: null,
      rating: null,
      notes: null,
      export_manifest: null,
    });

    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/live"]}>
          <Routes>
            <Route path="/live" element={<LivePage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    await waitFor(() => expect(screen.getByTestId("live-history-unknown")).toBeInTheDocument());
    // The stale cached data never renders, under any flag.
    expect(screen.queryByText("STALE — MUST NOT RENDER")).toBeNull();
    expect(screen.queryByTestId("live-finished-view")).toBeNull();
  });

  it("#532 round 3: fails OPEN (not closed) when NO cached detail exists and the forced refresh fails — LiveFinishedView's own useRoast gets an independent shot", async () => {
    // The other half of the fail-open/fail-closed split: no cache entry
    // existed for this id at all, so there is nothing stale to mislead with.
    // The verification fetch still fails, but the page must NOT show the
    // history-error state — it falls through, and LiveFinishedView's own
    // useRoast() gets its own independent attempt (which also fails here,
    // rendering the view with "—" placeholders rather than blocking /live
    // entirely on one transient error).
    const HISTORY_RUN_ID = "run-no-cache-refresh-fails";
    global.fetch = vi.fn(async (url: string, init?: RequestInit) => {
      const u = String(url);
      if (u === "/api/health") {
        return new Response(
          JSON.stringify({
            status: "ok",
            version: "test",
            mcp_child: "running",
            mcp_hardware_clear_required: false,
            mcp_teardown_incident_id: null,
            active_run_id: null,
          }),
          { status: 200 },
        );
      }
      if (u === "/api/roasts") {
        return new Response(
          JSON.stringify({
            runs: [
              {
                id: HISTORY_RUN_ID,
                started_at_utc: "2026-07-01T10:00:00Z",
                completed_at_utc: "2026-07-01T10:06:30Z",
                first_crack_at_utc: null,
                agent_phase: "complete",
                outcome: "completed",
                bean_origin: "Ethiopia Guji",
                bean_varietal: null,
                rating: null,
                development_percent: 18.7,
                advisor_consults: 0,
                advisor_clamped: 0,
                advisor_rejected: 0,
                advisor_failed: 0,
              },
            ],
          }),
          { status: 200 },
        );
      }
      // Every detail fetch fails — no cached entry ever existed either.
      if (u === `/api/roasts/${HISTORY_RUN_ID}`) {
        return new Response(JSON.stringify({ detail: "server error" }), { status: 500 });
      }
      if (u.startsWith(`/api/roasts/${HISTORY_RUN_ID}/telemetry`)) {
        return new Response(
          JSON.stringify({ run_id: HISTORY_RUN_ID, downsample: 1, point_count: 0, points: [] }),
          { status: 200 },
        );
      }
      throw new Error(`unexpected fetch ${u} ${init?.method ?? "GET"}`);
    }) as unknown as typeof fetch;

    // NO pre-seeded cache entry this time.
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/live"]}>
          <Routes>
            <Route path="/live" element={<LivePage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    // Falls through to LiveFinishedView (never the history-error state) —
    // its own useRoast() gets an independent attempt at the same failing
    // endpoint, so the view renders with placeholders, not a blocked page.
    await waitFor(() => expect(screen.getByTestId("live-finished-view")).toBeInTheDocument());
    expect(screen.queryByTestId("live-history-unknown")).toBeNull();
  });
});
