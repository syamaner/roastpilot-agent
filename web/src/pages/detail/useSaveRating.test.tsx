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
});
