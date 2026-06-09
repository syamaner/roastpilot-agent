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

describe("useDashboardEvents (hook)", () => {
  it("folds the latest event into the view-model", () => {
    const { result } = renderHook(
      ({ ev, run }: { ev: SseEvent | null; run: string | null }) => useDashboardEvents(ev, run),
      { initialProps: { ev: advisory as SseEvent | null, run: "run-1" as string | null } },
    );
    expect(result.current.latestAdvisory?.decision?.target_heat).toBe(60);
  });

  it("resets the view-model when the run id changes (no cross-run carryover)", () => {
    const { result, rerender } = renderHook(
      ({ ev, run }: { ev: SseEvent | null; run: string | null }) => useDashboardEvents(ev, run),
      { initialProps: { ev: advisory as SseEvent | null, run: "run-1" as string | null } },
    );
    expect(result.current.latestAdvisory).not.toBeNull();

    // A new run starts while the page stays mounted; with no new event yet, the
    // accumulated view-model must clear so the previous run isn't painted forward.
    rerender({ ev: null, run: "run-2" });
    expect(result.current.latestAdvisory).toBeNull();
    expect(result.current.points).toHaveLength(0);
    expect(result.current.advisorySeq).toBe(0);
  });
});
