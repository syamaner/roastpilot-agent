import { renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { SseEvent } from "@/lib/types";
import { useDashboardEvents } from "./useDashboardEvents";

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
        useDashboardEvents(frames, count, run),
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
        useDashboardEvents(frames, count, run),
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
        useDashboardEvents(frames, count, run),
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
        useDashboardEvents(frames, count, run),
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
});
