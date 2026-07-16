import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";
import { roastKeys } from "@/hooks/queries";
import type { RoastDetail } from "@/lib/types";
import { ratingMutationKey, useSaveRating } from "./useSaveRating";

function wrapper(client: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

function fakeDetail(overrides: Partial<RoastDetail> = {}): RoastDetail {
  return { id: "r1", rating: 4, notes: "bright", ...overrides } as RoastDetail;
}

afterEach(() => vi.restoreAllMocks());

describe("useSaveRating", () => {
  it("#568 (PRRT_kwDOSzMG_c6Rdlk6 / PRRT_kwDOSzMG_c6RdxDQ): seeds the detail cache directly from api.rate's response, not just an invalidation", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    // Seed a STALE cached detail so a mere invalidation (which only marks the
    // query for a refetch, but doesn't itself change the currently-read data)
    // would still read old until that refetch resolves.
    client.setQueryData(roastKeys.detail("r1"), fakeDetail({ rating: 2, notes: "old" }));

    const invalidateSpy = vi.spyOn(client, "invalidateQueries");
    const returned = fakeDetail({ rating: 5, notes: "fresh from the mutation response" });
    vi.spyOn(api, "rate").mockResolvedValue(returned);

    const { result } = renderHook(() => useSaveRating("r1"), { wrapper: wrapper(client) });
    result.current.mutate({ stars: 5, notes: "fresh from the mutation response" });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    // The cache reflects the MUTATION'S OWN response synchronously — no
    // dependency on invalidateQueries' refetch round-trip settling.
    expect(client.getQueryData(roastKeys.detail("r1"))).toEqual(returned);
    // Never invalidates (that was the pre-fix, round-trip-dependent path).
    expect(invalidateSpy).not.toHaveBeenCalled();
  });

  it("uses a mutationKey scoped by run id, shared across every call site (the #568 cross-widget serialization contract)", () => {
    expect(ratingMutationKey("r1")).toEqual(["roasts", "r1", "rating"]);
    expect(ratingMutationKey("r2")).toEqual(["roasts", "r2", "rating"]);
    expect(ratingMutationKey("r1")).not.toEqual(ratingMutationKey("r2"));
  });

  it("#568 round 2 (PRRT_kwDOSzMG_c6ReetW): cancels any in-flight GET for the same key before seeding, so a stale background fetch resolving AFTER the mutation can't overwrite the fresh rating", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    // Seed the cache the way a real detail view would ALREADY have it
    // (useRoast's own GET has already populated this key before any rating
    // mutation ever fires) — round 3's merge-only seed has nothing to merge
    // into on a truly empty cache, which isn't the real scenario this guards.
    client.setQueryData(roastKeys.detail("r1"), fakeDetail({ rating: 4, notes: "old" }));

    // A background GET (e.g. useRoast's own polling refetch) for the SAME key
    // is already in flight when the mutation resolves — and it resolves to
    // the PRE-SAVE snapshot it fetched before the mutation even ran.
    let resolveStaleGet: ((detail: RoastDetail) => void) | undefined;
    const staleGetPromise = new Promise<RoastDetail>((resolve) => {
      resolveStaleGet = resolve;
    });
    // The mutation's `cancelQueries` is expected to cancel this fetch — catch
    // the resulting rejection so it doesn't surface as an unhandled promise
    // rejection in the test run (it's the exact behavior under test, not a
    // bug).
    client
      .fetchQuery({
        queryKey: roastKeys.detail("r1"),
        queryFn: () => staleGetPromise,
      })
      .catch(() => undefined);

    const returned = fakeDetail({ rating: 5, notes: "fresh from the mutation response" });
    vi.spyOn(api, "rate").mockResolvedValue(returned);

    const { result } = renderHook(() => useSaveRating("r1"), { wrapper: wrapper(client) });
    result.current.mutate({ stars: 5, notes: "fresh from the mutation response" });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    // The stale GET settles AFTER the mutation. Without cancelQueries, its
    // resolver would silently overwrite the just-seeded fresh rating.
    resolveStaleGet?.(fakeDetail({ rating: 2, notes: "the pre-save snapshot" }));
    await waitFor(() => expect(client.getQueryData(roastKeys.detail("r1"))).toBeDefined());

    // Give any (incorrectly) still-live GET resolver a chance to land before
    // asserting — the fix should mean it never gets the chance at all
    // (cancelled), but this also catches a race that "usually" wins by timing.
    await new Promise((r) => setTimeout(r, 20));

    expect(client.getQueryData(roastKeys.detail("r1"))).toEqual(returned);
  });

  it("#568 round 3 (PRRT_kwDOSzMG_c6RfBAA): cancels ONLY the detail query (exact: true), never a sibling in-flight timeline/telemetry/tastings read that shares the key prefix", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    client.setQueryData(roastKeys.detail("r1"), fakeDetail());

    // A sibling query under the SAME `["roasts", "r1", ...]` prefix
    // (`roastKeys.detail("r1")` is `["roasts", "r1"]`, a PREFIX of this key)
    // is in flight when the mutation resolves — e.g. the timeline table's
    // own GET. TanStack's default query-key matching is INCLUSIVE, so a
    // bare (non-`exact`) cancelQueries on the detail key would ALSO cancel
    // this one.
    let resolveTimeline: (() => void) | undefined;
    const timelinePromise = new Promise<{ ok: true }>((resolve) => {
      resolveTimeline = () => resolve({ ok: true });
    });
    const timelineQuery = client.fetchQuery({
      queryKey: roastKeys.timeline("r1"),
      queryFn: () => timelinePromise,
    });

    vi.spyOn(api, "rate").mockResolvedValue(fakeDetail({ rating: 5, notes: "fresh" }));
    const { result } = renderHook(() => useSaveRating("r1"), { wrapper: wrapper(client) });
    result.current.mutate({ stars: 5, notes: "fresh" });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    // The sibling query must NOT have been cancelled — it can still resolve
    // normally and its data lands in the cache.
    resolveTimeline?.();
    await expect(timelineQuery).resolves.toEqual({ ok: true });
    expect(client.getQueryState(roastKeys.timeline("r1"))?.status).toBe("success");
  });

  it("#568 round 3 (PRRT_kwDOSzMG_c6RfBAJ): merges only rating/notes into the cached detail — a concurrent roasted-weight save's fresher field is never rolled back", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    // A concurrent mutation (RoastedWeight's own save) has already written
    // ITS fresher field straight into this same cache entry — the rating
    // response never saw this value (it reflects whatever the SERVER had
    // for roasted_weight_grams at the moment the rating POST was handled,
    // which may predate this concurrent write).
    client.setQueryData(
      roastKeys.detail("r1"),
      fakeDetail({ rating: 2, notes: "old", roasted_weight_grams: 42 }),
    );

    vi.spyOn(api, "rate").mockResolvedValue(
      fakeDetail({ rating: 5, notes: "fresh", roasted_weight_grams: null }),
    );
    const { result } = renderHook(() => useSaveRating("r1"), { wrapper: wrapper(client) });
    result.current.mutate({ stars: 5, notes: "fresh" });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const cached = client.getQueryData<RoastDetail>(roastKeys.detail("r1"));
    // rating/notes are fresh (the original round-1 fix's whole point)...
    expect(cached?.rating).toBe(5);
    expect(cached?.notes).toBe("fresh");
    // ...but the concurrent weight write survives — never rolled back to
    // whatever (irrelevant, possibly stale) value the rating response
    // happened to carry for a field this mutation doesn't own.
    expect(cached?.roasted_weight_grams).toBe(42);
  });
});
