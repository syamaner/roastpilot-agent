import { describe, expect, it } from "vitest";

import type { RoastTimeline, SafetyVerdict, TelemetrySeries } from "@/lib/types";
import { FIXTURE_TELEMETRY, FIXTURE_TIMELINE, FIXTURE_TIMELINE_TURNING_POINT } from "./fixture";
import {
  headlineStats,
  tickToSeconds,
  toCurveMarkers,
  toCurvePoints,
  toTraceRows,
} from "./traceModel";

describe("toCurvePoints", () => {
  it("projects telemetry into the LiveCurve point form (x = elapsed seconds)", () => {
    const points = toCurvePoints(FIXTURE_TELEMETRY);
    expect(points).toHaveLength(FIXTURE_TELEMETRY.points.length);
    expect(points[0]).toMatchObject({ t: 0 });
    // Celsius temps + heat/fan carried through, RoR mapped from bean RoR.
    expect(points[1].bean).toBeCloseTo(FIXTURE_TELEMETRY.points[1].bean_temp_c!);
    expect(points[1].heat).toBe(FIXTURE_TELEMETRY.points[1].heat_level_percent);
    expect(points[1].ror).toBe(FIXTURE_TELEMETRY.points[1].bean_ror_c_per_min);
  });

  it("drops points without an elapsed time (cannot be placed on the axis)", () => {
    const series: TelemetrySeries = {
      run_id: "r",
      downsample: 1,
      point_count: 2,
      points: [
        { ...FIXTURE_TELEMETRY.points[0], elapsed_seconds: null },
        { ...FIXTURE_TELEMETRY.points[1], elapsed_seconds: 30 },
      ],
    };
    expect(toCurvePoints(series)).toHaveLength(1);
  });

  it("sorts only the chart projection after a process-local clock reset", () => {
    const elapsed = [100, 105, 0, 5];
    const series: TelemetrySeries = {
      run_id: "recovered-run",
      downsample: 1,
      point_count: elapsed.length,
      points: elapsed.map((elapsedSeconds, index) => ({
        ...FIXTURE_TELEMETRY.points[index],
        elapsed_seconds: elapsedSeconds,
        bean_temp_c: 150 + index,
      })),
    };

    expect(toCurvePoints(series).map(({ t, bean }) => [t, bean])).toEqual([
      [0, 152],
      [5, 153],
      [100, 150],
      [105, 151],
    ]);
    expect(series.points.map((point) => point.elapsed_seconds)).toEqual(elapsed);
  });

  it("returns [] for undefined telemetry", () => {
    expect(toCurvePoints(undefined)).toEqual([]);
  });
});

describe("toCurveMarkers", () => {
  it("anchors T0 at x=0 and places FC/drop from telemetry phase transitions", () => {
    const markers = toCurveMarkers(FIXTURE_TIMELINE, FIXTURE_TELEMETRY);
    const byKind = Object.fromEntries(markers.map((m) => [m.kind, m]));
    expect(byKind.t0.t).toBe(0);
    // FC = first `development` point (tick 11 → 330 s), drop = first `cooling`
    // point (tick 15 → 450 s). Derived from server phase, not a client guess.
    expect(byKind.first_crack.t).toBe(330);
    expect(byKind.drop.t).toBe(450);
  });

  it("omits T0 when no t0_detected event is present", () => {
    const timeline: RoastTimeline = { ...FIXTURE_TIMELINE, events: [] };
    const markers = toCurveMarkers(timeline, FIXTURE_TELEMETRY);
    expect(markers.find((m) => m.kind === "t0")).toBeUndefined();
  });

  it("omits FC/drop when telemetry never enters those phases", () => {
    const series: TelemetrySeries = {
      ...FIXTURE_TELEMETRY,
      points: FIXTURE_TELEMETRY.points.map((p) => ({
        ...p,
        agent_phase: "roasting_pre_first_crack",
      })),
    };
    const markers = toCurveMarkers(FIXTURE_TIMELINE, series);
    expect(markers.map((m) => m.kind)).toEqual(["t0"]);
  });

  it("places turning-point (#409) at the first telemetry point reaching the event's charge-elapsed", () => {
    // The persisted turning_point timeline event carries elapsed_since_charge_seconds
    // (the charge-referenced clock at the RoR-zero cross) but no tick. Its x is the
    // first telemetry point whose charge_elapsed_seconds >= that value.
    // FIXTURE_TELEMETRY has charge_elapsed_seconds === elapsed_seconds = i*30.
    // elapsed_since_charge_seconds: 90 → first point with charge_elapsed >= 90 is
    // tick 3 at elapsed_seconds 90. monotonic_seconds 888 is distinct — confirms we
    // are NOT returning the event's own monotonic (the scan-for-cross is the logic).
    const markers = toCurveMarkers(FIXTURE_TIMELINE_TURNING_POINT, FIXTURE_TELEMETRY);
    expect(markers.find((m) => m.kind === "turning_point")).toEqual({
      kind: "turning_point",
      t: 90, // charge_elapsed 90 → elapsed_seconds 90 (NOT monotonic 888)
      label: "TURN",
    });
  });

  it("omits turning-point when no turning_point event is on the timeline", () => {
    // FIXTURE_TIMELINE has no turning_point event → no marker (event presence is the gate).
    const markers = toCurveMarkers(FIXTURE_TIMELINE, FIXTURE_TELEMETRY);
    expect(markers.some((m) => m.kind === "turning_point")).toBe(false);
  });

  it("omits turning-point when the event carries no numeric elapsed_since_charge_seconds", () => {
    const timeline: RoastTimeline = {
      ...FIXTURE_TIMELINE,
      events: [
        ...FIXTURE_TIMELINE.events,
        {
          kind: "turning_point",
          source: "controller",
          monotonic_seconds: 90,
          recorded_at_utc: "2026-06-07T09:13:30Z",
          payload: { bean_temp_c: 116.6 }, // elapsed_since_charge_seconds absent
        },
      ],
    };
    const markers = toCurveMarkers(timeline, FIXTURE_TELEMETRY);
    expect(markers.some((m) => m.kind === "turning_point")).toBe(false);
  });

  it("places dry-end (#351) at the first telemetry point reaching the event's server threshold", () => {
    // The persisted drying_end timeline event carries the server's threshold but no
    // tick — so its x is the first telemetry point whose bean_temp_c reaches that
    // threshold (the server's own rising cross, replayed against persisted readings).
    // Fixture bean_temp_c = 92 + i*8.2 → tick 7 = 149.4 (below), tick 8 = 157.6 (≥150),
    // elapsed 240 s.
    //
    // monotonic_seconds is a DISTINCT, arbitrary server wall-clock (999) — NOT 240 —
    // so this oracle falsifies the shortcut of returning event.monotonic_seconds
    // directly: only a real telemetry scan for the threshold cross yields t === 240.
    const timeline: RoastTimeline = {
      ...FIXTURE_TIMELINE,
      events: [
        ...FIXTURE_TIMELINE.events,
        {
          kind: "drying_end",
          source: "controller",
          monotonic_seconds: 999,
          recorded_at_utc: "2026-06-07T09:16:00Z",
          payload: { bean_temp_c: 157.6, threshold_c: 150 },
        },
      ],
    };
    const markers = toCurveMarkers(timeline, FIXTURE_TELEMETRY);
    expect(markers.find((m) => m.kind === "dry_end")).toEqual({
      kind: "dry_end",
      t: 240, // the threshold-cross elapsed_seconds, NOT the event's monotonic 999
      label: "DRY END",
    });
  });

  it("omits dry-end when no drying_end event is on the timeline", () => {
    // FIXTURE_TIMELINE has no drying_end event → no marker, even though the bean
    // curve crosses 150 °C (the event's presence is the gate, never a client threshold).
    const markers = toCurveMarkers(FIXTURE_TIMELINE, FIXTURE_TELEMETRY);
    expect(markers.some((m) => m.kind === "dry_end")).toBe(false);
  });

  it("omits dry-end when the drying_end event carries no numeric threshold", () => {
    const timeline: RoastTimeline = {
      ...FIXTURE_TIMELINE,
      events: [
        ...FIXTURE_TIMELINE.events,
        {
          kind: "drying_end",
          source: "controller",
          monotonic_seconds: 240,
          recorded_at_utc: "2026-06-07T09:16:00Z",
          payload: { bean_temp_c: 157.6 },
        },
      ],
    };
    const markers = toCurveMarkers(timeline, FIXTURE_TELEMETRY);
    expect(markers.some((m) => m.kind === "dry_end")).toBe(false);
  });

  it("omits dry-end when no telemetry point reaches the event's threshold (gap between windows)", () => {
    // Realistic when the server fires drying_end between downsampled telemetry
    // windows: the event is present with a numeric threshold, but no persisted point
    // reaches it (here threshold 999 °C, well above the fixture's ~215 °C peak), so
    // there is no cross to anchor on → null → no marker (the scan exhausts cleanly).
    const timeline: RoastTimeline = {
      ...FIXTURE_TIMELINE,
      events: [
        ...FIXTURE_TIMELINE.events,
        {
          kind: "drying_end",
          source: "controller",
          monotonic_seconds: 240,
          recorded_at_utc: "2026-06-07T09:16:00Z",
          payload: { bean_temp_c: 157.6, threshold_c: 999 },
        },
      ],
    };
    const markers = toCurveMarkers(timeline, FIXTURE_TELEMETRY);
    expect(markers.some((m) => m.kind === "dry_end")).toBe(false);
  });
});

describe("toTraceRows", () => {
  it("joins safety evals to advisor decisions + commands by tick", () => {
    const rows = toTraceRows(FIXTURE_TIMELINE, FIXTURE_TELEMETRY);
    expect(rows).toHaveLength(FIXTURE_TIMELINE.safety_evaluations.length);

    const clamp = rows.find((r) => r.verdict === "clamp")!;
    // The recommendation it judged (from advisor_decisions.decision) ...
    expect(clamp.recommendedHeat).toBe(105);
    expect(clamp.confidence).toBeCloseTo(0.78);
    expect(clamp.rationale).toContain("momentum");
    // ... and what safety actually let through (adjusted = the clamp delta).
    expect(clamp.executedHeat).toBe(100);
    expect(clamp.executedFan).toBe(40);
    // The command logged for that tick.
    expect(clamp.commandTool).toBe("set_heat");
    // Placed on the curve axis via telemetry's elapsed seconds (tick 8 → 240 s).
    expect(clamp.elapsedSeconds).toBe(240);
  });

  it("falls back executed→input when nothing was adjusted", () => {
    const rows = toTraceRows(FIXTURE_TIMELINE, FIXTURE_TELEMETRY);
    const allow = rows.find((r) => r.verdict === "allow")!;
    expect(allow.executedHeat).toBe(allow.recommendedHeat);
  });

  it("passes ALL SIX verdicts through unchanged (it renders history)", () => {
    const verdicts: SafetyVerdict[] = [
      "allow",
      "clamp",
      "reject",
      "recovery",
      "fault",
      "emergency_stop",
    ];
    const timeline: RoastTimeline = {
      ...FIXTURE_TIMELINE,
      advisor_decisions: [],
      commands: [],
      safety_evaluations: verdicts.map((verdict, i) => ({
        tick: i,
        rule: "r",
        verdict,
        input_heat: null,
        input_fan: null,
        adjusted_heat: null,
        adjusted_fan: null,
        reason: "x",
        recorded_at_utc: `2026-06-07T09:0${i}:00Z`,
      })),
    };
    expect(toTraceRows(timeline, FIXTURE_TELEMETRY).map((r) => r.verdict)).toEqual(verdicts);
  });

  it("reads advisor fields defensively when the decision dict is absent", () => {
    const timeline: RoastTimeline = {
      ...FIXTURE_TIMELINE,
      advisor_decisions: [],
      commands: [],
      safety_evaluations: [FIXTURE_TIMELINE.safety_evaluations[0]],
    };
    const row = toTraceRows(timeline, FIXTURE_TELEMETRY)[0];
    expect(row.recommendedHeat).toBeNull();
    expect(row.rationale).toBeNull();
    expect(row.commandTool).toBeNull();
  });

  it("returns [] for an undefined timeline", () => {
    expect(toTraceRows(undefined, FIXTURE_TELEMETRY)).toEqual([]);
  });
});

describe("tickToSeconds", () => {
  it("maps a tick to its telemetry elapsed seconds", () => {
    expect(tickToSeconds(FIXTURE_TELEMETRY, 8)).toBe(240);
  });
  it("returns null for an unknown tick or undefined telemetry", () => {
    expect(tickToSeconds(FIXTURE_TELEMETRY, 999)).toBeNull();
    expect(tickToSeconds(undefined, 0)).toBeNull();
  });
});

describe("headlineStats", () => {
  it("derives total/FC/drop/development from telemetry phase transitions", () => {
    const stats = headlineStats(FIXTURE_TIMELINE, FIXTURE_TELEMETRY);
    expect(stats.totalSeconds).toBe(450); // last elapsed (tick 15).
    expect(stats.firstCrackSeconds).toBe(330); // first development point.
    expect(stats.firstCrackTempC).toBeCloseTo(92 + 11 * 8.2);
    expect(stats.dropSeconds).toBe(450); // first cooling point.
    expect(stats.developmentPercent).not.toBeNull();
  });

  it("returns all-null stats for empty telemetry", () => {
    const stats = headlineStats(undefined, undefined);
    expect(stats).toEqual({
      totalSeconds: null,
      firstCrackSeconds: null,
      firstCrackTempC: null,
      dropSeconds: null,
      dropTempC: null,
      developmentPercent: null,
    });
  });
});
