import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";
import { useHistory, useRoast, useTelemetry, useTimeline } from "./queries";

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
