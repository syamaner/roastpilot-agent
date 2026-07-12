/**
 * #513 real-integration regression coverage for the `/live` start-roast flow.
 *
 * `LivePage.test.tsx` stubs `useHealth` and `StartRoastForm` directly, which is
 * exactly the layer that hid #513: those tests could (and did) pass while the
 * real handoff — a real `useHealth()` backed by a real `QueryClient`, driving
 * the real `StartRoastForm` through a real submit — was broken. This file
 * drives the actual `fetch` boundary instead, so the assertions exercise the
 * genuine wiring an operator's browser would run.
 *
 * Covers: a clean start reaching the dashboard; a transient post-restart
 * `/health` failure recovering via retry; a 409 (double-submit / already-active)
 * leaving the form visible and usable, never silently reset; the active-run
 * banner on `/start` (the legacy, still-URL-reachable alias) so a bare form is
 * never the only thing on screen once a run is active; and the Codex follow-up
 * (#514/#515 review) — a REAL `QueryClient` cache primed with a within-staleTime
 * "idle" health snapshot must not let the bare form render before the forced
 * fresh refetch resolves, even when that fresh read reveals a run that started
 * elsewhere in the last 30s.
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
          mcp_child: "running",
          active_run_id: opts.activeRunId(),
        }),
        { status: 200 },
      );
    }
    if (u === "/api/roasts" && init?.method === "POST") {
      return opts.onPost();
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

function fillMinimum() {
  fireEvent.change(screen.getByTestId("start-roast-name"), { target: { value: "Morning batch" } });
  fireEvent.change(screen.getByTestId("start-roast-bean_origin"), {
    target: { value: "Ethiopia Guji" },
  });
  fireEvent.change(screen.getByTestId("start-roast-bean_weight_grams"), { target: { value: "250" } });
}

describe("#513 real-integration — /live start flow", () => {
  it("a clean 201 reaches the live dashboard with no operator action", async () => {
    let activeRunId: string | null = null;
    global.fetch = fakeFetch({
      activeRunId: () => activeRunId,
      onPost: () => {
        activeRunId = "run-new";
        return new Response(JSON.stringify({ id: "run-new" }), { status: 201 });
      },
    });

    renderLive();
    await waitFor(() => expect(screen.getByTestId("live-start-view")).toBeInTheDocument());
    fillMinimum();
    fireEvent.submit(screen.getByTestId("start-roast-form"));

    await waitFor(() => expect(screen.getByTestId("dashboard-stub")).toBeInTheDocument());
    expect(screen.queryByTestId("live-start-view")).toBeNull();
  });

  it("recovers from a transient post-restart /health failure without operator action (#513 root cause)", async () => {
    let activeRunId: string | null = null;
    let healthCalls = 0;
    global.fetch = fakeFetch({
      activeRunId: () => activeRunId,
      healthStatus: () => {
        healthCalls += 1;
        // The first health GET (idle load) succeeds; the confirm attempt right
        // after the POST fails once (server mid-restart/respawn), then recovers.
        return healthCalls === 2 ? 503 : 200;
      },
      onPost: () => {
        activeRunId = "run-new";
        return new Response(JSON.stringify({ id: "run-new" }), { status: 201 });
      },
    });

    renderLive();
    await waitFor(() => expect(screen.getByTestId("live-start-view")).toBeInTheDocument());
    fillMinimum();
    fireEvent.submit(screen.getByTestId("start-roast-form"));

    // The form must vanish immediately — it never looks untouched.
    await waitFor(() => expect(screen.getByTestId("live-start-confirming")).toBeInTheDocument());
    expect(screen.queryByTestId("live-start-view")).toBeNull();

    // And it must still reach the dashboard once the retry succeeds.
    await waitFor(() => expect(screen.getByTestId("dashboard-stub")).toBeInTheDocument(), {
      timeout: 5000,
    });
  }, 8000);

  it("a 409 (double-submit / already active) leaves the form visible, usable, and un-reset", async () => {
    global.fetch = fakeFetch({
      activeRunId: () => null,
      onPost: () =>
        new Response(JSON.stringify({ detail: "a roast is already active" }), { status: 409 }),
    });

    renderLive();
    await waitFor(() => expect(screen.getByTestId("live-start-view")).toBeInTheDocument());
    fillMinimum();
    fireEvent.submit(screen.getByTestId("start-roast-form"));

    await waitFor(() => expect(screen.getByTestId("start-roast-error")).toBeInTheDocument());
    expect(screen.getByTestId("start-roast-error")).toHaveTextContent(/already active/i);
    // Never the silent-looking "roast started" transitional state, and never a
    // reset that erases what the operator typed.
    expect(screen.queryByTestId("live-start-confirming")).toBeNull();
    expect(screen.getByTestId("live-start-view")).toBeInTheDocument();
    expect(screen.getByTestId("start-roast-name")).toHaveValue("Morning batch");
    expect(screen.getByTestId("start-roast-submit")).toBeEnabled();
  });

  it("#513 Codex follow-up: a within-staleTime cached idle snapshot never lets the bare form render before a genuinely fresh read", async () => {
    // Mirrors the app's real QueryClient config (staleTime: 30_000 — see
    // web/src/lib/queryClient.ts) so this test exercises the actual hazard:
    // a cache primed by an EARLIER health read (e.g. the home page, or a
    // prior /live visit) within the last 30s must not let a fresh mount of
    // /live render the bare start form from that stale "idle" snapshot,
    // even though `isSuccess` would be true instantly with NO network call
    // at all under plain `useHealth` semantics.
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, staleTime: 30_000 } },
    });
    // Prime the cache exactly as an earlier real fetch would have.
    client.setQueryData(roastKeys.health, {
      status: "ok",
      version: "test",
      mcp_child: "running",
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
        // Reveals a run that started elsewhere in the stale window — the
        // scenario Codex flagged, not just a plausible one.
        return new Response(
          JSON.stringify({
            status: "ok",
            version: "test",
            mcp_child: "running",
            active_run_id: "run-from-another-tab",
          }),
          { status: 200 },
        );
      }
      throw new Error(`unexpected fetch ${u}`);
    }) as unknown as typeof fetch;

    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/live"]}>
          <Routes>
            <Route path="/live" element={<LivePage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    // The stale cache alone must never render the bare form or the dashboard.
    await waitFor(() => expect(healthCalls).toBe(1));
    expect(screen.getByTestId("live-page-loading")).toBeInTheDocument();
    expect(screen.queryByTestId("live-start-view")).toBeNull();
    expect(screen.queryByTestId("dashboard-stub")).toBeNull();

    // Release the fresh read — it reveals the run that started elsewhere.
    resolveHealth();
    await waitFor(() => expect(screen.getByTestId("dashboard-stub")).toBeInTheDocument());
    expect(screen.queryByTestId("live-page-loading")).toBeNull();
    expect(screen.queryByTestId("live-start-view")).toBeNull();
  });

  it("#513: unmounting mid-confirm never lets the orphaned loop write the cache after unmount", async () => {
    // qa's probe scenario: the component unmounts WHILE the confirm loop is
    // between attempts (e.g. the operator navigates away, or the "Open live
    // dashboard" fallback forces a remount). Without the mounted guard, the
    // orphaned closure's next `api.health()` resolution would still call
    // `queryClient.setQueryData` on the shared (app-wide) query cache — and a
    // remount that starts its OWN confirm loop would then race the orphaned
    // one, both writing `roastKeys.health`.
    let activeRunId: string | null = null;
    let healthCalls = 0;
    let resolveSecondHealth: (() => void) | null = null;
    global.fetch = fakeFetch({
      activeRunId: () => activeRunId,
      onPost: () => {
        activeRunId = "run-new";
        return new Response(JSON.stringify({ id: "run-new" }), { status: 201 });
      },
    }) as unknown as typeof fetch;
    // Wrap the fake fetch so the SECOND /api/health call (the confirm loop's
    // first post-start attempt) stalls until we explicitly release it —
    // giving the test a deterministic window to unmount mid-attempt.
    const baseFetch = global.fetch;
    global.fetch = vi.fn(async (url: string, init?: RequestInit) => {
      const u = String(url);
      if (u === "/api/health") {
        healthCalls += 1;
        if (healthCalls === 2) {
          await new Promise<void>((resolve) => {
            resolveSecondHealth = resolve;
          });
        }
      }
      return baseFetch(url, init);
    }) as unknown as typeof fetch;

    const { client, unmount } = renderLive();
    const setQueryDataSpy = vi.spyOn(client, "setQueryData");

    await waitFor(() => expect(screen.getByTestId("live-start-view")).toBeInTheDocument());
    fillMinimum();
    fireEvent.submit(screen.getByTestId("start-roast-form"));

    // The confirm loop's second api.health() call (its first post-start
    // attempt) is now stalled mid-flight.
    await waitFor(() => expect(healthCalls).toBe(2));
    setQueryDataSpy.mockClear(); // clear the idle-load setQueryData noise, if any

    // Unmount WHILE that attempt is still pending.
    unmount();

    // Release the stalled health call — this is exactly the orphaned-closure
    // moment: the promise resolves, but the component is already gone.
    const release = resolveSecondHealth as unknown as (() => void) | null;
    expect(release).not.toBeNull();
    release?.();

    // Give the orphaned closure every chance to (wrongly) write the cache.
    await new Promise((r) => setTimeout(r, 50));

    expect(setQueryDataSpy).not.toHaveBeenCalled();
  });
});

describe("#513 real-integration — /start (legacy alias) active-run guard", () => {
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
    // Same scenario as the /live version above, on the /start legacy route.
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, staleTime: 30_000 } },
    });
    client.setQueryData(roastKeys.health, {
      status: "ok",
      version: "test",
      mcp_child: "running",
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
            active_run_id: "run-from-another-tab",
          }),
          { status: 200 },
        );
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
