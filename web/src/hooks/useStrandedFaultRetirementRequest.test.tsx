import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode, type ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const retireMock = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, api: { ...actual.api, retireStrandedFault: retireMock } };
});

import { useStrandedFaultRetirementRequest } from "./useStrandedFaultRetirementRequest";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}
function wrapper(client: QueryClient) {
  return ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}
const confirmed = {
  outcome: "confirmed" as const,
  attempted_generation: 1,
  restart_clearance: { state: "required" as const, eligible: false, enabled_actions: [], closure_only_available: false },
  fault_controls: { status: "closed" as const, generation: 1, enabled_actions: ["emergency_stop"] },
};

describe("useStrandedFaultRetirementRequest", () => {
  beforeEach(() => retireMock.mockReset());
  it("TH1: allows one synchronous in-flight POST and records its settled outcome", async () => {
    const pending = deferred<typeof confirmed>();
    retireMock.mockReturnValueOnce(pending.promise);
    const client = new QueryClient();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const { result } = renderHook(useStrandedFaultRetirementRequest, { wrapper: wrapper(client) });
    act(() => { result.current.submit("p1#1"); result.current.submit("p1#1"); });
    expect(retireMock).toHaveBeenCalledTimes(1);
    expect(result.current.pending).toBe(true);
    await act(async () => { pending.resolve(confirmed); await pending.promise; });
    await waitFor(() => expect(result.current.outcome).toEqual({ kind: "confirmed", key: "p1#1" }));
    expect(result.current.pending).toBe(false);
    expect(invalidate).toHaveBeenCalledTimes(1);
  });

  it("TH3: invalidates after an unmounted request without issuing another POST", async () => {
    const pending = deferred<typeof confirmed>();
    retireMock.mockReturnValueOnce(pending.promise);
    const client = new QueryClient();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const { result, unmount } = renderHook(useStrandedFaultRetirementRequest, { wrapper: wrapper(client) });
    act(() => result.current.submit("p1#1"));
    unmount();
    await act(async () => { pending.resolve(confirmed); await pending.promise; });
    await waitFor(() => expect(invalidate).toHaveBeenCalledTimes(1));
    expect(retireMock).toHaveBeenCalledTimes(1);
  });

  it("TH2: maps parser and transport failures to the fixed local error outcome", async () => {
    retireMock.mockRejectedValueOnce(new Error("run-abc /var/db"));
    const client = new QueryClient();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const { result } = renderHook(useStrandedFaultRetirementRequest, { wrapper: wrapper(client) });
    act(() => result.current.submit("p1#1"));
    await waitFor(() => expect(result.current.outcome).toEqual({ kind: "error", key: "p1#1" }));
    expect(result.current.pending).toBe(false);
    expect(invalidate).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["confirmed", { ...confirmed, outcome: "confirmed" as const }, "confirmed"],
    ["unconfirmed", { ...confirmed, outcome: "unconfirmed" as const }, "unconfirmed"],
    ["rejected", { ...confirmed, outcome: "rejected" as const, attempted_generation: null }, "rejected"],
  ])("TH2: settles %s, invalidates once, and admits a later deliberate submit", async (_name, response, kind) => {
    retireMock.mockResolvedValueOnce(response).mockResolvedValueOnce(confirmed);
    const client = new QueryClient();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const { result } = renderHook(useStrandedFaultRetirementRequest, { wrapper: wrapper(client) });
    act(() => result.current.submit("p1#1"));
    await waitFor(() => expect(result.current.outcome).toEqual({ kind, key: "p1#1" }));
    expect(result.current.pending).toBe(false);
    expect(invalidate).toHaveBeenCalledTimes(1);
    act(() => result.current.submit("p1#1"));
    await waitFor(() => expect(retireMock).toHaveBeenCalledTimes(2));
    expect(invalidate).toHaveBeenCalledTimes(2);
  });

  it("TH2: maps a rejected transport call to error, clears pending and invalidates once", async () => {
    retireMock.mockRejectedValueOnce(new Error("rejected"));
    const client = new QueryClient();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const { result } = renderHook(useStrandedFaultRetirementRequest, { wrapper: wrapper(client) });
    act(() => result.current.submit("p1#1"));
    await waitFor(() => expect(result.current.outcome).toEqual({ kind: "error", key: "p1#1" }));
    expect(result.current.pending).toBe(false);
    expect(invalidate).toHaveBeenCalledTimes(1);
  });

  it("TH4: restores the mounted fence after Strict Mode effect replay", async () => {
    const pending = deferred<typeof confirmed>();
    retireMock.mockReturnValueOnce(pending.promise);
    const client = new QueryClient();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const strictWrapper = ({ children }: { children: ReactNode }) => (
      <StrictMode><QueryClientProvider client={client}>{children}</QueryClientProvider></StrictMode>
    );
    const { result } = renderHook(useStrandedFaultRetirementRequest, { wrapper: strictWrapper });
    act(() => result.current.submit("p1#1"));
    expect(result.current.pending).toBe(true);
    expect(retireMock).toHaveBeenCalledTimes(1);
    await act(async () => { pending.resolve(confirmed); await pending.promise; });
    await waitFor(() => expect(result.current.outcome).toEqual({ kind: "confirmed", key: "p1#1" }));
    expect(result.current.pending).toBe(false);
    expect(invalidate).toHaveBeenCalledTimes(1);
  });
});
