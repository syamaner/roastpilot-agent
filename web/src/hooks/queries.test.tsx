import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";
import { FIXTURE_DETAIL } from "@/pages/detail/fixture";
import {
  roastKeys,
  useAddTasting,
  useFreshHealthGate,
  useHistory,
  useRoast,
  useTastings,
  useTelemetry,
  useTimeline,
} from "./queries";

function wrapper(client?: QueryClient) {
  const c = client ?? new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={c}>{children}</QueryClientProvider>
  );
}

// #533 Codex follow-up (PR #543): mirrors the APP's real QueryClient defaults
// (web/src/lib/queryClient.ts — refetchOnWindowFocus: false, staleTime:
// 30_000), not the bare `{ retry: false }` client `wrapper()` above uses for
// every other test in this file. A bare client is the "staleness vacuity
// trap" this repo's recent-fixes has burned on before: it would let a
// refetchInterval assertion pass by coincidence (TanStack's default
// staleTime is 0, so a bare client refetches far more eagerly than the real
// app ever would) without proving anything about production behaviour.
function appDefaultsWrapper() {
  const client = new QueryClient({
    defaultOptions: {
      queries: { refetchOnWindowFocus: false, retry: false, staleTime: 30_000 },
    },
  });
  return {
    Wrapper: ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    ),
    client,
  };
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("useRoast (skipToken when runId is null)", () => {
  it("does not fetch when runId is null", () => {
    const spy = vi.spyOn(api, "roast");
    const { result } = renderHook(() => useRoast(null), { wrapper: wrapper() });
    // skipToken → the query is disabled, never fetches, stays non-loading.
    expect(spy).not.toHaveBeenCalled();
    expect(result.current.fetchStatus).toBe("idle");
  });

  it("fetches with the real id when runId is provided", async () => {
    const spy = vi
      .spyOn(api, "roast")
      .mockResolvedValue({ id: "r1" } as Awaited<ReturnType<typeof api.roast>>);
    renderHook(() => useRoast("r1"), { wrapper: wrapper() });
    await waitFor(() => expect(spy).toHaveBeenCalledWith("r1"));
  });
});

describe("useRoast polling while the run is LIVE (#533 Codex follow-up, PR #543)", () => {
  // Without this, DetailView's completed-run-only widget gate
  // (completed_at_utc !== null) never refreshes on its own — no interval on
  // this hook, refetchOnWindowFocus off, no SSE invalidation of roast-detail
  // queries — so an operator watching a live run's detail page would see it
  // finish and stay locked out of RoastRating/RoastedWeight/ChargeWeight/
  // RoastTastings until an unrelated remount.
  it("polls every 5s while completed_at_utc is null (the run is still live)", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const liveDetail = { ...FIXTURE_DETAIL, completed_at_utc: null };
    const spy = vi.spyOn(api, "roast").mockResolvedValue(liveDetail);
    const { Wrapper } = appDefaultsWrapper();

    renderHook(() => useRoast(liveDetail.id), { wrapper: Wrapper });
    await vi.waitFor(() => expect(spy).toHaveBeenCalledTimes(1));

    await vi.advanceTimersByTimeAsync(5000);
    await vi.waitFor(() => expect(spy).toHaveBeenCalledTimes(2));

    await vi.advanceTimersByTimeAsync(5000);
    await vi.waitFor(() => expect(spy).toHaveBeenCalledTimes(3));
  });

  it("does NOT poll when completed_at_utc is already set (the completed-run common case)", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const spy = vi.spyOn(api, "roast").mockResolvedValue(FIXTURE_DETAIL); // completed_at_utc set
    const { Wrapper } = appDefaultsWrapper();

    renderHook(() => useRoast(FIXTURE_DETAIL.id), { wrapper: Wrapper });
    await vi.waitFor(() => expect(spy).toHaveBeenCalledTimes(1));

    // Advance well past several would-be interval ticks — no further fetch.
    await vi.advanceTimersByTimeAsync(20_000);
    expect(spy).toHaveBeenCalledTimes(1);
  });

  it("stops polling once a refetch reports the run has completed — the gate-flip case", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const liveDetail = { ...FIXTURE_DETAIL, completed_at_utc: null };
    const completedDetail = { ...FIXTURE_DETAIL, completed_at_utc: "2026-07-13T23:00:00Z" };
    const spy = vi
      .spyOn(api, "roast")
      .mockResolvedValueOnce(liveDetail)
      .mockResolvedValueOnce(completedDetail);
    const { Wrapper } = appDefaultsWrapper();

    const { result } = renderHook(() => useRoast(liveDetail.id), { wrapper: Wrapper });
    await vi.waitFor(() => expect(result.current.data).toBeDefined());
    expect(result.current.data?.completed_at_utc).toBeNull();

    // The 5s poll fires and the SECOND mocked response reports completion —
    // this IS the gate-flip #543's fix restores: DetailView's completed-run-
    // only widgets become visible within one interval tick of the run ending.
    await vi.advanceTimersByTimeAsync(5000);
    await vi.waitFor(() => expect(spy).toHaveBeenCalledTimes(2));
    await vi.waitFor(() => expect(result.current.data?.completed_at_utc).not.toBeNull());

    // No further polling once completed — advancing well past another
    // interval must not trigger a third fetch.
    await vi.advanceTimersByTimeAsync(20_000);
    expect(spy).toHaveBeenCalledTimes(2);
  });
});

describe("useTimeline (skipToken when runId is null)", () => {
  it("does not fetch when runId is null", () => {
    const spy = vi.spyOn(api, "timeline");
    const { result } = renderHook(() => useTimeline(null), { wrapper: wrapper() });
    expect(spy).not.toHaveBeenCalled();
    expect(result.current.fetchStatus).toBe("idle");
  });

  it("fetches the timeline for a real id", async () => {
    const spy = vi
      .spyOn(api, "timeline")
      .mockResolvedValue({ run_id: "r1" } as Awaited<ReturnType<typeof api.timeline>>);
    renderHook(() => useTimeline("r1"), { wrapper: wrapper() });
    await waitFor(() => expect(spy).toHaveBeenCalledWith("r1"));
  });
});

describe("useTelemetry (skipToken when runId is null)", () => {
  it("does not fetch when runId is null", () => {
    const spy = vi.spyOn(api, "telemetry");
    const { result } = renderHook(() => useTelemetry(null), { wrapper: wrapper() });
    expect(spy).not.toHaveBeenCalled();
    expect(result.current.fetchStatus).toBe("idle");
  });

  it("fetches telemetry with the run id and downsample", async () => {
    const spy = vi
      .spyOn(api, "telemetry")
      .mockResolvedValue({ run_id: "r1" } as Awaited<ReturnType<typeof api.telemetry>>);
    renderHook(() => useTelemetry("r1", 5), { wrapper: wrapper() });
    await waitFor(() => expect(spy).toHaveBeenCalledWith("r1", 5));
  });
});

describe("useHistory", () => {
  it("fetches the roast history list", async () => {
    const spy = vi
      .spyOn(api, "history")
      .mockResolvedValue({ runs: [] } as Awaited<ReturnType<typeof api.history>>);
    renderHook(() => useHistory(), { wrapper: wrapper() });
    await waitFor(() => expect(spy).toHaveBeenCalled());
  });
});

describe("useTastings (skipToken when runId is null) (#522)", () => {
  it("does not fetch when runId is null", () => {
    const spy = vi.spyOn(api, "tastings");
    const { result } = renderHook(() => useTastings(null), { wrapper: wrapper() });
    expect(spy).not.toHaveBeenCalled();
    expect(result.current.fetchStatus).toBe("idle");
  });

  it("fetches the tasting list for a real id", async () => {
    const spy = vi
      .spyOn(api, "tastings")
      .mockResolvedValue({ run_id: "r1", tastings: [] } as Awaited<ReturnType<typeof api.tastings>>);
    renderHook(() => useTastings("r1"), { wrapper: wrapper() });
    await waitFor(() => expect(spy).toHaveBeenCalledWith("r1"));
  });
});

describe("useAddTasting (#522)", () => {
  it("posts the entry and writes the returned list into the tasting query cache", async () => {
    const updated = { run_id: "r1", tastings: [{ id: 1, stars: 5 }] } as Awaited<
      ReturnType<typeof api.addTasting>
    >;
    vi.spyOn(api, "addTasting").mockResolvedValue(updated);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { result } = renderHook(() => useAddTasting("r1"), { wrapper: wrapper(client) });

    result.current.mutate({ stars: 5 });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(client.getQueryData(roastKeys.tastings("r1"))).toEqual(updated);
  });

  it("cancels an in-flight initial GET before writing, so a stale GET resolving AFTER the save cannot overwrite the just-saved list (#522 Codex P2, mirrors useSaveConfig's #483 fix)", async () => {
    type Tastings = Awaited<ReturnType<typeof api.tastings>>;
    const preSave: Tastings = { run_id: "r1", tastings: [] };
    const postSave = {
      run_id: "r1",
      tastings: [{ id: 1, stars: 5 }],
    } as Awaited<ReturnType<typeof api.addTasting>>;

    let resolveGet: ((v: Tastings) => void) | null = null;
    vi.spyOn(api, "tastings").mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveGet = resolve;
        }),
    );
    vi.spyOn(api, "addTasting").mockResolvedValue(postSave);

    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    // The initial GET (useTastings' own mount fetch) is now in flight and
    // never resolved yet — the realistic race window: the operator saves
    // before the page's own initial read has settled.
    renderHook(() => useTastings("r1"), { wrapper: wrapper(client) });
    await waitFor(() => expect(api.tastings).toHaveBeenCalled());

    const { result } = renderHook(() => useAddTasting("r1"), { wrapper: wrapper(client) });
    result.current.mutate({ stars: 5 });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(client.getQueryData(roastKeys.tastings("r1"))).toEqual(postSave);

    // NOW the slow initial GET resolves with the PRE-save (stale) snapshot.
    // Without the cancelQueries fix this overwrites the cache back to
    // `preSave`, silently hiding the just-saved entry.
    resolveGet!(preSave);
    await waitFor(() => expect(api.tastings).toHaveResolvedWith(preSave));
    expect(client.getQueryData(roastKeys.tastings("r1"))).toEqual(postSave);
  });
});

/**
 * `useFreshHealthGate` (#513 Codex follow-up on #514/#515 review): the two
 * start-form gating views (`LiveStartView`/`LivePage`, `StartRoastView`) must
 * hold until a GENUINELY FRESH health read, not just a resolved one — plain
 * `useHealth`'s shared 30s `staleTime` would otherwise let a remount within
 * that window render a cached "idle" snapshot with `isSuccess: true` and NO
 * network request at all.
 */
describe("useFreshHealthGate", () => {
  it("no cache entry: isFresh is false until the first fetch settles, then true", async () => {
    const healthSpy = vi
      .spyOn(api, "health")
      .mockResolvedValue({
        status: "ok",
        version: "t",
        mcp_child: "running",
        active_run_id: null,
      } as Awaited<ReturnType<typeof api.health>>);
    const { result } = renderHook(() => useFreshHealthGate(), { wrapper: wrapper() });
    expect(result.current.isFresh).toBe(false);
    await waitFor(() => expect(result.current.isFresh).toBe(true));
    expect(healthSpy).toHaveBeenCalledTimes(1);
  });

  it("a within-staleTime cached entry: isFresh stays false through the forced refetch even though isSuccess is already true from cache", async () => {
    let call = 0;
    let resolveSecond: ((v: Awaited<ReturnType<typeof api.health>>) => void) | null = null;
    vi.spyOn(api, "health").mockImplementation(async () => {
      call += 1;
      if (call === 1) {
        return {
          status: "ok",
          version: "t",
          mcp_child: "running",
          active_run_id: null,
        } as Awaited<ReturnType<typeof api.health>>;
      }
      return new Promise((resolve) => {
        resolveSecond = resolve;
      });
    });

    const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 30_000 } } });
    // Prime the cache with a fresh-by-staleTime-accounting entry, then unmount
    // (simulating an earlier real page's health read).
    const first = renderHook(() => useFreshHealthGate(), { wrapper: wrapper(client) });
    await waitFor(() => expect(first.result.current.isFresh).toBe(true));
    first.unmount();

    // Remount within staleTime: isSuccess is instantly true from cache, but
    // isFresh must stay false until THIS mount's forced refetch resolves.
    const second = renderHook(() => useFreshHealthGate(), { wrapper: wrapper(client) });
    await waitFor(() => expect(call).toBe(2)); // the forced refetch is in flight
    expect(second.result.current.isFresh).toBe(false);
    expect(second.result.current.isSuccess).toBe(true); // proves a naive isSuccess check is unsafe
    expect(second.result.current.data?.active_run_id).toBe(null); // still the STALE value

    // Resolve with a DIFFERENT value (a run that started in another tab).
    resolveSecond!({
      status: "ok",
      version: "t",
      mcp_child: "running",
      active_run_id: "run-from-another-tab",
    } as Awaited<ReturnType<typeof api.health>>);
    await waitFor(() => expect(second.result.current.isFresh).toBe(true));
    expect(second.result.current.data?.active_run_id).toBe("run-from-another-tab");
  });

  it("a persistent error settles isFresh to true (never stuck pending)", async () => {
    vi.spyOn(api, "health").mockRejectedValue(new Error("still down"));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { result } = renderHook(() => useFreshHealthGate(), { wrapper: wrapper(client) });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.isFresh).toBe(true);
  });

  it("writes to the same query key useHealth reads (roastKeys.health)", async () => {
    vi.spyOn(api, "health").mockResolvedValue({
      status: "ok",
      version: "t",
      mcp_child: "running",
      active_run_id: null,
    } as Awaited<ReturnType<typeof api.health>>);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    renderHook(() => useFreshHealthGate(), { wrapper: wrapper(client) });
    await waitFor(() =>
      expect(client.getQueryData(roastKeys.health)).toEqual(
        expect.objectContaining({ active_run_id: null }),
      ),
    );
  });
});
