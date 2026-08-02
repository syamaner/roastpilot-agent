import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, api } from "./api";

function mockFetch(status: number, body: unknown, headers?: Record<string, string>): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () =>
      new Response(typeof body === "string" ? body : JSON.stringify(body), {
        status,
        headers: { "content-type": "application/json", ...headers },
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

  it("POST /api/mcp/acknowledge-hardware-clear sends the incident-bound decision", async () => {
    mockFetch(200, {
      result: "accepted",
      hardware_clear: true,
      teardown_incident_id: "a".repeat(32),
      fresh_spawn_permitted: true,
    });
    await api.acknowledgeHardwareClear({
      hardware_clear: true,
      teardown_incident_id: "a".repeat(32),
      reason: "roaster cold and child resources released",
    });
    expect(fetch).toHaveBeenCalledWith(
      "/api/mcp/acknowledge-hardware-clear",
      expect.objectContaining({ method: "POST" }),
    );
    const [, init] = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({
      hardware_clear: true,
      teardown_incident_id: "a".repeat(32),
      reason: "roaster cold and child resources released",
    });
  });

  it("draft-correlated profile create preserves JSON and attempt headers", async () => {
    mockFetch(201, { id: "bean-1", name: "Kenya" });
    const input = { name: "Kenya" } as never;
    await api.createBeanProfile(input, "0123456789abcdef0123456789abcdef");
    const [, init] = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    const headers = new Headers((init as RequestInit).headers);
    expect(headers.get("Content-Type")).toBe("application/json");
    expect(headers.get("X-RoastPilot-Draft-Attempt-Id")).toBe(
      "0123456789abcdef0123456789abcdef",
    );
    expect(JSON.parse((init as RequestInit).body as string)).toEqual(input);
  });

  it("POST /api/beans/recommend-from-catalogue sends the URL and validates its response", async () => {
    mockFetch(200, {
      recommendations: [
        {
          candidate_id: "candidate-01",
          product_url: "https://vendor.example/products/kiambu",
          name: "Kiambu \u202eLot",
          country: "Ken\u2066ya",
          processing: "washed",
          score: 3,
          reason_codes: ["missing_country"],
          reasons: ["Adds Ken\u202eya to the active roster."],
        },
      ],
      discovered_count: 4,
      extracted_count: 2,
    });

    const result = await api.recommendBeansFromCatalogue(
      "https://vendor.example/collections/green",
    );

    expect(result.recommendations[0]).toMatchObject({
      name: "Kiambu Lot",
      country: "Kenya",
      reasons: ["Adds Kenya to the active roster."],
    });
    expect(fetch).toHaveBeenCalledWith(
      "/api/beans/recommend-from-catalogue",
      expect.objectContaining({ method: "POST" }),
    );
    const [, init] = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({
      url: "https://vendor.example/collections/green",
    });
  });

  it("counts catalogue text with the server's Unicode code-point semantics", async () => {
    const name = "😀".repeat(300);
    const reason = "🌱".repeat(600);
    mockFetch(200, {
      recommendations: [
        {
          candidate_id: "candidate-01",
          product_url: "https://vendor.example/products/international-lot",
          name,
          country: "日本",
          processing: "washed",
          score: 3,
          reason_codes: ["rated_pair_affinity"],
          reasons: [reason],
        },
      ],
      discovered_count: 1,
      extracted_count: 1,
    });

    const result = await api.recommendBeansFromCatalogue(
      "https://vendor.example/collections/green",
    );

    expect(result.recommendations[0]?.name).toBe(name);
    expect(result.recommendations[0]?.reasons).toEqual([reason]);
  });

  it.each([
    ["more than three recommendations", { recommendations: Array(4).fill({}), discovered_count: 4, extracted_count: 4 }],
    [
      "an unknown reason code",
      {
        recommendations: [{
          candidate_id: "candidate-01",
          product_url: "https://vendor.example/products/a",
          name: "A",
          country: null,
          processing: null,
          score: 0,
          reason_codes: ["invented"],
          reasons: [],
        }],
        discovered_count: 1,
        extracted_count: 1,
      },
    ],
    [
      "a name over 500 Unicode code points",
      {
        recommendations: [
          {
            candidate_id: "candidate-01",
            product_url: "https://vendor.example/products/a",
            name: "A".repeat(501),
            country: null,
            processing: null,
            score: 0,
            reason_codes: [],
            reasons: [],
          },
        ],
        discovered_count: 1,
        extracted_count: 1,
      },
    ],
    [
      "a country over 500 Unicode code points",
      {
        recommendations: [
          {
            candidate_id: "candidate-01",
            product_url: "https://vendor.example/products/a",
            name: "A",
            country: "C".repeat(501),
            processing: null,
            score: 0,
            reason_codes: [],
            reasons: [],
          },
        ],
        discovered_count: 1,
        extracted_count: 1,
      },
    ],
    [
      "a reason over 600 Unicode code points",
      {
        recommendations: [
          {
            candidate_id: "candidate-01",
            product_url: "https://vendor.example/products/a",
            name: "A",
            country: null,
            processing: null,
            score: 0,
            reason_codes: [],
            reasons: ["R".repeat(601)],
          },
        ],
        discovered_count: 1,
        extracted_count: 1,
      },
    ],
    [
      "a browser-ambiguous product URL",
      {
        recommendations: [{
          candidate_id: "candidate-01",
          product_url: "https://vendor.example/\\evil.example/products/a",
          name: "A",
          country: null,
          processing: null,
          score: 0,
          reason_codes: [],
          reasons: [],
        }],
        discovered_count: 1,
        extracted_count: 1,
      },
    ],
    [
      "an oversized bidi-heavy text field",
      {
        recommendations: [
          {
            candidate_id: "candidate-01",
            product_url: "https://vendor.example/products/a",
            name: `A${"\u202e".repeat(565)}`,
            country: null,
            processing: null,
            score: 0,
            reason_codes: [],
            reasons: [],
          },
        ],
        discovered_count: 1,
        extracted_count: 1,
      },
    ],
    ["inconsistent counts", { recommendations: [], discovered_count: 1, extracted_count: 2 }],
  ])("rejects catalogue responses with %s", async (_case, body) => {
    mockFetch(200, body);
    await expect(
      api.recommendBeansFromCatalogue("https://vendor.example/catalogue"),
    ).rejects.toThrow("Invalid catalogue recommendation response");
  });

  it("throws ApiError carrying the server detail on a non-ok response", async () => {
    mockFetch(409, { detail: "a roast is already active" });
    await expect(api.startRoast({} as never)).rejects.toMatchObject({
      name: "ApiError",
      status: 409,
      detail: "a roast is already active",
    });
  });

  it("carries a typed conflict code from the response header", async () => {
    mockFetch(
      409,
      { detail: "already saved" },
      { "X-RoastPilot-Conflict-Code": "draft_attempt_already_claimed" },
    );
    await expect(api.createBeanProfile({} as never, "0".repeat(32))).rejects.toMatchObject({
      status: 409,
      detail: "already saved",
      code: "draft_attempt_already_claimed",
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

  it("POST /charge-weight sends the correction body and returns the detail (#520)", async () => {
    mockFetch(200, { id: "r1", corrected_charge_grams: 255 });
    const detail = await api.setChargeWeight("r1", { corrected_charge_grams: 255 });
    expect(detail.corrected_charge_grams).toBe(255);
    const [, init] = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(init).toMatchObject({ method: "POST" });
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({
      corrected_charge_grams: 255,
    });
  });

  it("POST /discard returns the flagged detail (#582)", async () => {
    mockFetch(200, { id: "r1", excluded: true });
    const detail = await api.discardRoast("r1");
    expect(detail.excluded).toBe(true);
    expect(fetch).toHaveBeenCalledWith("/api/roasts/r1/discard", expect.any(Object));
    const [, init] = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(init).toMatchObject({ method: "POST" });
  });

  it("POST /restore returns the un-flagged detail (#582)", async () => {
    mockFetch(200, { id: "r1", excluded: false });
    const detail = await api.restoreRoast("r1");
    expect(detail.excluded).toBe(false);
    expect(fetch).toHaveBeenCalledWith("/api/roasts/r1/restore", expect.any(Object));
    const [, init] = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(init).toMatchObject({ method: "POST" });
  });

  it("POST /discard surfaces the server's 409 detail on an in-progress run (#582)", async () => {
    mockFetch(409, { detail: "run r1 is still in progress; discard it after completion" });
    await expect(api.discardRoast("r1")).rejects.toMatchObject({
      name: "ApiError",
      status: 409,
    });
  });

  it("POST /clear-stale-session sends the reason body and returns the typed result (#525)", async () => {
    mockFetch(200, { run_id: "r1", outcome: "aborted", completed_at_utc: "2026-07-14T00:00:00Z" });
    const result = await api.clearStaleSession("r1", { reason: "orphaned after a crash" });
    expect(result.outcome).toBe("aborted");
    expect(fetch).toHaveBeenCalledWith(
      "/api/roasts/r1/clear-stale-session",
      expect.any(Object),
    );
    const [, init] = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(init).toMatchObject({ method: "POST" });
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({
      reason: "orphaned after a crash",
    });
  });

  it("POST /clear-stale-session surfaces the server's 409 detail on a rejected guard (#525)", async () => {
    mockFetch(409, { detail: "run r1 appears to be actively driven" });
    await expect(
      api.clearStaleSession("r1", { reason: "thought this was orphaned" }),
    ).rejects.toMatchObject({
      name: "ApiError",
      status: 409,
      detail: "run r1 appears to be actively driven",
    });
  });
});
