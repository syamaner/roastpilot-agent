import { renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { ConnectionStatus } from "@/hooks/useRoastStream";
import type { SseEvent, TelemetrySeries } from "@/lib/types";
import { useDashboardEvents } from "./useDashboardEvents";

/** A backfill stub that resolves no points — keeps the seed path inert for the
 *  existing fold/burst/reset tests so they assert only frame folding. */
const noTelemetry = (): Promise<TelemetrySeries> =>
  Promise.resolve({ run_id: "run-1", downsample: 1, point_count: 0, points: [] });

const advisory: SseEvent = {
  event: "advisory",
  data: {
    decision: { target_heat: 60, target_fan: 75, should_drop: false, confidence: 0.8, rationale: "hold" },
    evaluation: { rule: "r", verdict: "allow", input_heat: 60, input_fan: 75, adjusted_heat: 60, adjusted_fan: 75, reason: "ok" },
  } as Record<string, unknown>,
  id: 1,
};

const faultFrame: SseEvent = {
  event: "fault",
  data: {
    rule: "max_env_temp",
    verdict: "emergency_stop",
    input_heat: 100,
    input_fan: 0,
    adjusted_heat: 0,
    adjusted_fan: 0,
    reason: "env 242C over 240C ceiling",
  } as Record<string, unknown>,
  id: 209,
};

describe("useDashboardEvents (hook)", () => {
  it("folds frames from the non-lossy buffer into the view-model", () => {
    const { result } = renderHook(
      ({ frames, count, run }: { frames: readonly SseEvent[]; count: number; run: string | null }) =>
        useDashboardEvents(frames, count, run, "connecting", { fetchTelemetry: noTelemetry }),
      { initialProps: { frames: [advisory] as readonly SseEvent[], count: 1, run: "run-1" as string | null } },
    );
    expect(result.current.latestAdvisory?.decision?.target_heat).toBe(60);
  });

  it("drains EVERY frame of a burst — the fault frame is never coalesced away (#122)", () => {
    // Simulate the replay `advance-to fault` burst: a whole run's frames land in the
    // buffer in one go (count jumps from 0 → N in a single render). The drain must
    // dispatch all of them, including the trailing `fault` frame — the exact loss the
    // single `lastEvent` slot caused.
    const burst: SseEvent[] = [];
    for (let i = 0; i < 208; i += 1) {
      burst.push({ event: "telemetry", data: { elapsed_seconds: i } as Record<string, unknown>, id: i + 1 });
    }
    burst.push(faultFrame);

    const { result } = renderHook(
      ({ frames, count, run }: { frames: readonly SseEvent[]; count: number; run: string | null }) =>
        useDashboardEvents(frames, count, run, "connecting", { fetchTelemetry: noTelemetry }),
      { initialProps: { frames: burst as readonly SseEvent[], count: burst.length, run: "run-1" as string | null } },
    );

    // The fault handshake was folded — proving the trailing frame survived the burst.
    expect(result.current.fault).not.toBeNull();
    expect(result.current.fault?.verdict).toBe("emergency_stop");
    expect(result.current.safetyTrail.some((e) => e.kind === "fault")).toBe(true);
  });

  it("drains only NEW frames as the buffer grows (no re-fold of seen frames)", () => {
    const { result, rerender } = renderHook(
      ({ frames, count, run }: { frames: readonly SseEvent[]; count: number; run: string | null }) =>
        useDashboardEvents(frames, count, run, "connecting", { fetchTelemetry: noTelemetry }),
      { initialProps: { frames: [advisory] as readonly SseEvent[], count: 1, run: "run-1" as string | null } },
    );
    expect(result.current.advisorySeq).toBe(1);

    // Append a second advisory; only the new frame should fold (advisorySeq → 2, not 3).
    const buffer = [advisory, { ...advisory, id: 2 }];
    rerender({ frames: buffer as readonly SseEvent[], count: 2, run: "run-1" });
    expect(result.current.advisorySeq).toBe(2);
  });

  it("resets the view-model when the run id changes (no cross-run carryover)", () => {
    const { result, rerender } = renderHook(
      ({ frames, count, run }: { frames: readonly SseEvent[]; count: number; run: string | null }) =>
        useDashboardEvents(frames, count, run, "connecting", { fetchTelemetry: noTelemetry }),
      { initialProps: { frames: [advisory] as readonly SseEvent[], count: 1, run: "run-1" as string | null } },
    );
    expect(result.current.latestAdvisory).not.toBeNull();

    // A new run starts while the page stays mounted; the buffer resets to empty
    // (count → 0) and the accumulated view-model must clear so the previous run is
    // not painted forward.
    rerender({ frames: [] as readonly SseEvent[], count: 0, run: "run-2" });
    expect(result.current.latestAdvisory).toBeNull();
    expect(result.current.points).toHaveLength(0);
    expect(result.current.advisorySeq).toBe(0);
  });

  // --- #153: curve backfill from /telemetry on (re)connect ---

  /** A persisted telemetry series for the backfill stub. `t` is the SERVE-elapsed
   *  clock (#326) the curve buffer keys on; the charge clock is offset (t − 510) so
   *  the two are never confused — these ticks are post-charge. */
  function series(ticks: number[]): TelemetrySeries {
    return {
      run_id: "run-1",
      downsample: 1,
      point_count: ticks.length,
      points: ticks.map((t, i) => ({
        tick: i,
        elapsed_seconds: t,
        charge_elapsed_seconds: t - 510,
        agent_phase: "development",
        bean_temp_c: 100 + t,
        env_temp_c: 120 + t,
        bean_ror_c_per_min: 12,
        env_ror_c_per_min: 14,
        heat_level_percent: 70,
        fan_level_percent: 40,
        cooling_on: false,
        development_percent: null,
      })),
    };
  }

  /** A live telemetry SSE frame at SERVE-elapsed `t` (#326; the buffer key). */
  function telemetry(t: number): SseEvent {
    return {
      event: "telemetry",
      data: {
        elapsed_seconds: t,
        charge_elapsed_seconds: t - 510,
        bean_temp_c: 200 + t,
        env_temp_c: 220 + t,
        bean_ror_c_per_min: 10,
        heat_percent: 60,
        fan_percent: 50,
      } as Record<string, unknown>,
      id: 1000 + t,
    };
  }

  it("seeds the curve from /telemetry when the stream goes live (late-join backfill)", async () => {
    const fetchTelemetry = vi.fn(() => Promise.resolve(series([0, 30, 60, 90])));
    const { result, rerender } = renderHook(
      ({ status }: { status: ConnectionStatus }) =>
        useDashboardEvents([], 0, "run-1", status, { fetchTelemetry }),
      { initialProps: { status: "connecting" as ConnectionStatus } },
    );
    // No backfill while merely connecting.
    expect(fetchTelemetry).not.toHaveBeenCalled();
    expect(result.current.points).toHaveLength(0);

    rerender({ status: "live" });
    await waitFor(() => expect(result.current.points).toHaveLength(4));
    expect(fetchTelemetry).toHaveBeenCalledTimes(1);
    expect(result.current.points.map((p) => p.t)).toEqual([0, 30, 60, 90]);
    expect(result.current.points[0]).toMatchObject({ t: 0, bean: 100, env: 120, heat: 70, fan: 40 });
  });

  it("re-seeds on reconnect WITHOUT duplicating points (reconnect catches up, #135)", async () => {
    const fetchTelemetry = vi.fn(() => Promise.resolve(series([0, 30, 60])));
    const { result, rerender } = renderHook(
      ({ status }: { status: ConnectionStatus }) =>
        useDashboardEvents([], 0, "run-1", status, { fetchTelemetry }),
      { initialProps: { status: "live" as ConnectionStatus } },
    );
    await waitFor(() => expect(result.current.points).toHaveLength(3));
    expect(fetchTelemetry).toHaveBeenCalledTimes(1);

    // Drop, then reconnect: the stream goes reconnecting → live again. The hook must
    // re-fetch and re-seed, but the overlapping ticks must NOT double-plot.
    rerender({ status: "reconnecting" });
    rerender({ status: "live" });
    await waitFor(() => expect(fetchTelemetry).toHaveBeenCalledTimes(2));
    expect(result.current.points.map((p) => p.t)).toEqual([0, 30, 60]); // deduped on t
  });

  it("appends live frames after the seed with no duplicate at the seam", async () => {
    const fetchTelemetry = vi.fn(() => Promise.resolve(series([0, 30, 60])));
    const { result, rerender } = renderHook(
      ({ frames, count, status }: { frames: readonly SseEvent[]; count: number; status: ConnectionStatus }) =>
        useDashboardEvents(frames, count, "run-1", status, { fetchTelemetry }),
      { initialProps: { frames: [] as readonly SseEvent[], count: 0, status: "live" as ConnectionStatus } },
    );
    await waitFor(() => expect(result.current.points).toHaveLength(3));

    // A live frame at t=60 (the last seeded tick) must REPLACE, not duplicate; a
    // frame at t=90 extends the curve.
    const frames = [telemetry(60), telemetry(90)];
    rerender({ frames: frames as readonly SseEvent[], count: 2, status: "live" });
    await waitFor(() => expect(result.current.points.map((p) => p.t)).toEqual([0, 30, 60, 90]));
    // t=60 was replaced by the live frame (bean 200+60=260), not the seed (160).
    expect(result.current.points.find((p) => p.t === 60)?.bean).toBe(260);
  });

  it("a failed /telemetry backfill degrades to live-only — no crash, live frames still render (#155)", async () => {
    // The backfill catch path (#155): a rejected /telemetry must NOT throw out of the
    // effect or wipe the curve — it degrades to the live stream. Assert the live frame
    // already in the buffer is preserved and the failed fetch adds NO backfilled points.
    const fetchTelemetry = vi.fn(() => Promise.reject(new Error("telemetry 503")));
    const { result, rerender } = renderHook(
      ({ frames, count, status }: { frames: readonly SseEvent[]; count: number; status: ConnectionStatus }) =>
        useDashboardEvents(frames, count, "run-1", status, { fetchTelemetry }),
      {
        // A live frame is already folded in before the backfill resolves/rejects.
        initialProps: {
          frames: [telemetry(60)] as readonly SseEvent[],
          count: 1,
          status: "live" as ConnectionStatus,
        },
      },
    );

    // The live frame renders immediately (it does not depend on the backfill).
    expect(result.current.points.map((p) => p.t)).toEqual([60]);

    // The backfill was attempted and rejected; the hook swallows it (no throw).
    await waitFor(() => expect(fetchTelemetry).toHaveBeenCalledTimes(1));
    // The curve is unchanged by the failure: only the live frame, no seed points,
    // bean from the LIVE frame (200+60=260) — not a stale/empty wipe.
    expect(result.current.points.map((p) => p.t)).toEqual([60]);
    expect(result.current.points.find((p) => p.t === 60)?.bean).toBe(260);

    // And a later live frame still appends — the stream is unbroken after the failure.
    rerender({ frames: [telemetry(60), telemetry(90)] as readonly SseEvent[], count: 2, status: "live" });
    await waitFor(() => expect(result.current.points.map((p) => p.t)).toEqual([60, 90]));
  });

  it("re-arms after a failed backfill so a later reconnect RETRIES the fetch (#155)", async () => {
    // The catch branch resets `wasLive` so the next live transition re-seeds. First
    // attempt rejects (degrade to live-only); on reconnect the fetch succeeds and the
    // missed window is finally backfilled — the failure is recoverable, not terminal.
    const fetchTelemetry = vi
      .fn<typeof import("@/lib/api").api.telemetry>()
      .mockRejectedValueOnce(new Error("telemetry 503"))
      .mockResolvedValueOnce(series([0, 30, 60]));
    const { result, rerender } = renderHook(
      ({ status }: { status: ConnectionStatus }) =>
        useDashboardEvents([], 0, "run-1", status, { fetchTelemetry }),
      { initialProps: { status: "live" as ConnectionStatus } },
    );

    // First live transition: fetch rejected, curve stays empty (no points seeded).
    await waitFor(() => expect(fetchTelemetry).toHaveBeenCalledTimes(1));
    expect(result.current.points).toHaveLength(0);

    // Reconnect (reconnecting → live) re-arms and re-fetches; this time it resolves.
    rerender({ status: "reconnecting" });
    rerender({ status: "live" });
    await waitFor(() => expect(fetchTelemetry).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(result.current.points.map((p) => p.t)).toEqual([0, 30, 60]));
  });
});
