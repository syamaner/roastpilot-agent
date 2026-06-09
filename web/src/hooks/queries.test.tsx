import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";
import { useRoast } from "./queries";

function wrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

afterEach(() => vi.restoreAllMocks());

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
