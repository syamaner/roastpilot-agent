import { describe, expect, it } from "vitest";

import type { RoastTimeline, TelemetrySeries } from "@/lib/types";
import { advisorSummary, toAdvisorRows } from "./advisorModel";
import {
  FIXTURE_TELEMETRY,
  FIXTURE_TIMELINE,
  FIXTURE_TIMELINE_FAILED,
} from "./fixture";

const EMPTY_SERIES: TelemetrySeries = {
  run_id: "x",
  downsample: 1,
  point_count: 0,
  points: [],
};

describe("toAdvisorRows", () => {
  it("emits one row per persisted consult, joined to its verdict by tick", () => {
    const rows = toAdvisorRows(FIXTURE_TIMELINE, FIXTURE_TELEMETRY);
    // The fixture has three ok consults at ticks 4, 8, 12.
    expect(rows.map((r) => r.tick)).toEqual([4, 8, 12]);
    // Each ok consult joins to the safety verdict at the same tick.
    expect(rows.map((r) => r.verdict)).toEqual(["allow", "clamp", "reject"]);
    // The recommendation + rationale come off the opaque decision payload.
    const clamp = rows.find((r) => r.tick === 8)!;
    expect(clamp.recommendedHeat).toBe(105);
    expect(clamp.recommendedFan).toBe(40);
    expect(clamp.status).toBe("ok");
    expect(clamp.rationale).toContain("RoR stalling");
    // Time is placed on the curve's seconds axis from telemetry (tick 8 → 240 s).
    expect(clamp.elapsedSeconds).toBe(240);
  });

  it("emits failure rows (no verdict, no recommendation) for failed consults", () => {
    const rows = toAdvisorRows(FIXTURE_TIMELINE_FAILED, FIXTURE_TELEMETRY);
    expect(rows).toHaveLength(3);
    for (const r of rows) {
      expect(r.status).toBe("provider_error");
      expect(r.verdict).toBeNull();
      expect(r.recommendedHeat).toBeNull();
      expect(r.rationale).toBeNull();
    }
    // Includes the preheat consult (tick 1).
    expect(rows.some((r) => r.tick === 1)).toBe(true);
  });

  it("returns no rows when the timeline is undefined", () => {
    expect(toAdvisorRows(undefined, EMPTY_SERIES)).toEqual([]);
  });
});

describe("advisorSummary", () => {
  it("counts consults, ok, and clamped/rejected verdicts", () => {
    const summary = advisorSummary(FIXTURE_TIMELINE);
    expect(summary).toEqual({
      consults: 3,
      ok: 3,
      failed: 0,
      clamped: 1,
      rejected: 1,
    });
  });

  it("counts failures for an all-provider_error roast", () => {
    const summary = advisorSummary(FIXTURE_TIMELINE_FAILED);
    expect(summary).toEqual({
      consults: 3,
      ok: 0,
      failed: 3,
      clamped: 0,
      rejected: 0,
    });
  });

  it("is all-zero for a roast with no advisor activity", () => {
    const empty: RoastTimeline = {
      run_id: "x",
      events: [],
      safety_evaluations: [],
      advisor_decisions: [],
      commands: [],
    };
    expect(advisorSummary(empty)).toEqual({
      consults: 0,
      ok: 0,
      failed: 0,
      clamped: 0,
      rejected: 0,
    });
  });
});
