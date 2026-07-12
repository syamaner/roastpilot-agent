import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, api } from "./api";

function mockFetch(status: number, body: unknown): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () =>
      new Response(typeof body === "string" ? body : JSON.stringify(body), {
        status,
        headers: { "content-type": "application/json" },
      }),
    ),
  );
}

beforeEach(() => vi.restoreAllMocks());
afterEach(() => vi.unstubAllGlobals());

describe("api client", () => {
  it("GET /api/roasts/{id} returns the typed detail", async () => {
    mockFetch(200, { id: "r1", agent_phase: "preheating" });
    const detail = await api.roast("r1");
    expect(detail.id).toBe("r1");
    expect(fetch).toHaveBeenCalledWith("/api/roasts/r1", expect.any(Object));
  });

  it("POST /operator-actions sends the action body", async () => {
    mockFetch(200, { action: "drop_beans", result: "accepted", reason: "", queued: true });
    const res = await api.operatorAction("r1", { action: "drop_beans" });
    expect(res.result).toBe("accepted");
    const [, init] = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(init).toMatchObject({ method: "POST" });
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({ action: "drop_beans" });
  });

  it("throws ApiError carrying the server detail on a non-ok response", async () => {
    mockFetch(409, { detail: "a roast is already active" });
    await expect(api.startRoast({} as never)).rejects.toMatchObject({
      name: "ApiError",
      status: 409,
      detail: "a roast is already active",
    });
  });

  it("ApiError exposes status + detail", () => {
    const err = new ApiError(404, "nope");
    expect(err.status).toBe(404);
    expect(err.detail).toBe("nope");
    expect(err.message).toContain("404");
  });

  it("builds telemetry + events + log-artifact URLs", () => {
    expect(api.eventsUrl("r1")).toBe("/api/roasts/r1/events");
    expect(api.logArtifactUrl("r1", "csv")).toBe("/api/roasts/r1/log/csv");
  });

  it("telemetry passes the downsample query param", async () => {
    mockFetch(200, { run_id: "r1", downsample: 5, point_count: 0, points: [] });
    await api.telemetry("r1", 5);
    expect(fetch).toHaveBeenCalledWith("/api/roasts/r1/telemetry?downsample=5", expect.any(Object));
  });

  it("GET /tastings returns the typed list (#522)", async () => {
    mockFetch(200, { run_id: "r1", tastings: [] });
    const list = await api.tastings("r1");
    expect(list.run_id).toBe("r1");
    expect(fetch).toHaveBeenCalledWith("/api/roasts/r1/tastings", expect.any(Object));
  });

  it("POST /tastings sends the entry body and returns the updated list (#522)", async () => {
    mockFetch(201, { run_id: "r1", tastings: [{ id: 1, stars: 4 }] });
    const list = await api.addTasting("r1", { stars: 4 });
    expect(list.tastings).toHaveLength(1);
    const [, init] = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(init).toMatchObject({ method: "POST" });
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({ stars: 4 });
  });
});
