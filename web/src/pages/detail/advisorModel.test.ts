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
  it("preserves the monotonic one-evaluation-per-tick fixture projection", () => {
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
    // Bean-temp (#325) is joined from the same telemetry tick (92 + 8*8.2 = 157.6 °C).
    expect(clamp.beanTempC).toBeCloseTo(157.6, 5);
  });

  it("joins a decision to its non-last same-tick evaluation FK", () => {
    const rows = toAdvisorRows(duplicateTickTimeline(101), EMPTY_SERIES);

    expect(rows[0]).toMatchObject({
      verdict: "clamp",
      verdictReason: "first evaluation at tick",
    });
  });

  it("uses insertion-ordered projection identities for unlinked same-tick advisors", () => {
    const timeline = duplicateTickTimeline(null);
    timeline.advisor_decisions.push({
      ...timeline.advisor_decisions[0],
      recorded_at_utc: "2026-06-07T09:10:01Z",
    });

    const rows = toAdvisorRows(timeline, EMPTY_SERIES);
    expect(rows.map((row) => row.rowId)).toEqual([
      "advisor-unlinked-projection-0",
      "advisor-unlinked-projection-1",
    ]);
    expect(rows.map((row) => row.tick)).toEqual([7, 7]);
  });

  it.each([
    ["null", null],
    ["dangling", 999],
  ])("does not guess by tick for a %s advisor FK", (_label, safetyEvaluationId) => {
    const timeline = duplicateTickTimeline(safetyEvaluationId);

    expect(toAdvisorRows(timeline, EMPTY_SERIES)[0]).toMatchObject({
      verdict: null,
      verdictReason: null,
    });
    expect(advisorSummary(timeline)).toMatchObject({ clamped: 0, rejected: 0 });
  });

  it("leaves beanTempC null when the tick has no telemetry join (#325)", () => {
    // A consult whose tick isn't in the telemetry series (or no series) → null
    // bean-temp, never a fabricated value (same rule as elapsedSeconds).
    const rows = toAdvisorRows(FIXTURE_TIMELINE, EMPTY_SERIES);
    expect(rows.every((r) => r.beanTempC === null)).toBe(true);
  });

  it("latest-wins INCLUDING null on a duplicate tick — a later null clears the temp (#325)", () => {
    // Augment medium: skipping null readings would latch the older non-null temp,
    // rendering a STALE temp instead of the null placeholder. The latest point for a
    // tick must win even when its reading is null. Two points at tick 4: first 178,
    // then null → the tick-4 row reads null (placeholder), not 178.
    const base = FIXTURE_TELEMETRY.points[0];
    const series: TelemetrySeries = {
      run_id: "dup",
      downsample: 1,
      point_count: 2,
      points: [
        { ...base, tick: 4, elapsed_seconds: 120, bean_temp_c: 178 },
        { ...base, tick: 4, elapsed_seconds: 120, bean_temp_c: null },
      ],
    };
    const rows = toAdvisorRows(FIXTURE_TIMELINE, series);
    const tick4 = rows.find((r) => r.tick === 4)!;
    expect(tick4.beanTempC).toBeNull();
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

  it("counts verdicts through exact FKs when duplicate ticks disagree", () => {
    expect(advisorSummary(duplicateTickTimeline(101))).toEqual({
      consults: 1,
      ok: 1,
      failed: 0,
      clamped: 1,
      rejected: 0,
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

function duplicateTickTimeline(safetyEvaluationId: number | null): RoastTimeline {
  return {
    run_id: "duplicate-tick",
    events: [],
    safety_evaluations: [
      {
        id: 101,
        tick: 7,
        rule: "bounds",
        verdict: "clamp",
        input_heat: 81,
        input_fan: 40,
        adjusted_heat: 80,
        adjusted_fan: 40,
        reason: "first evaluation at tick",
        recorded_at_utc: "2026-06-07T09:14:00Z",
      },
      {
        id: 102,
        tick: 7,
        rule: "drop_guard",
        verdict: "reject",
        input_heat: 0,
        input_fan: 100,
        adjusted_heat: null,
        adjusted_fan: null,
        reason: "last evaluation at tick",
        recorded_at_utc: "2026-06-07T09:14:01Z",
      },
    ],
    advisor_decisions: [
      {
        tick: 7,
        provider: "fake",
        model: "fixture",
        prompt_version: "v1",
        latency_ms: 5,
        status: "ok",
        decision: { target_heat: 81, target_fan: 40, should_drop: false },
        safety_evaluation_id: safetyEvaluationId,
        recorded_at_utc: "2026-06-07T09:14:00Z",
      },
    ],
    commands: [],
  } as RoastTimeline;
}
