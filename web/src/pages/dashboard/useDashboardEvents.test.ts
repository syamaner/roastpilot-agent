import { renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { SseEvent } from "@/lib/types";
import {
  ADVISORY_HISTORY_LIMIT,
  dashboardReducer,
  initialDashboardViewModel,
  snapshotFault,
  useDashboardEvents,
} from "./useDashboardEvents";

function ev<T>(event: SseEvent["event"], data: T, id?: number): { kind: "event"; event: SseEvent } {
  return { kind: "event", event: { event, data: data as Record<string, unknown>, id } };
}

const ADVISORY_DECISION = {
  trigger: "tick",
  decision: { target_heat: 60, target_fan: 75, should_drop: false, confidence: 0.82, rationale: "hold" },
  evaluation: {
    rule: "rate_limit",
    verdict: "clamp",
    input_heat: 80,
    input_fan: 40,
    adjusted_heat: 65,
    adjusted_fan: 40,
    reason: "heat clamped 80→65",
  },
};

describe("dashboardReducer", () => {
  it("appends a curve point per telemetry frame (x = SERVE-elapsed seconds, #326)", () => {
    let s = initialDashboardViewModel;
    // The curve buffer keys `t` on serve elapsed_seconds (#326), NOT
    // charge_elapsed_seconds — so preheat plots live; the charge re-origin is a
    // downstream display transform. The differing charge value is ignored for the x.
    s = dashboardReducer(s, ev("telemetry", { elapsed_seconds: 510, charge_elapsed_seconds: 10, bean_temp_c: 120, env_temp_c: 140, bean_ror_c_per_min: 16, heat_percent: 70, fan_percent: 40 }));
    s = dashboardReducer(s, ev("telemetry", { elapsed_seconds: 511, charge_elapsed_seconds: 11, bean_temp_c: 121, env_temp_c: 141, bean_ror_c_per_min: 15, heat_percent: 70, fan_percent: 40 }));
    expect(s.points).toHaveLength(2);
    expect(s.points[0]).toMatchObject({ t: 510, bean: 120, heat: 70 });
  });

  it("plots PRE-charge telemetry so the preheat curve is visible (#326 regression guard for #316)", () => {
    // Pre-charge the server sends charge_elapsed_seconds: null but serve
    // elapsed_seconds is set; the buffer keys on serve elapsed so the preheat frame
    // PLOTS (the #316 fix dropped these and left the chart blank during preheat).
    const s = dashboardReducer(
      initialDashboardViewModel,
      ev("telemetry", { elapsed_seconds: 90, charge_elapsed_seconds: null, bean_temp_c: 120, env_temp_c: 140 }),
    );
    expect(s.points).toHaveLength(1);
    expect(s.points[0]).toMatchObject({ t: 90, bean: 120 });
  });

  it("drops a telemetry frame only when the serve clock is null (no x to place, #326)", () => {
    const s = dashboardReducer(
      initialDashboardViewModel,
      ev("telemetry", { elapsed_seconds: null, charge_elapsed_seconds: null, bean_temp_c: 120, env_temp_c: 140 }),
    );
    expect(s.points).toHaveLength(0);
  });

  // --- #153 backfill: seed + dedupe ---

  function tp(t: number, bean = 100 + t) {
    return {
      tick: t,
      elapsed_seconds: t,
      agent_phase: "development" as const,
      bean_temp_c: bean,
      env_temp_c: 120 + t,
      bean_ror_c_per_min: 12,
      env_ror_c_per_min: 14,
      heat_level_percent: 70,
      fan_level_percent: 40,
      cooling_on: false,
      development_percent: null,
    };
  }

  it("seeds points from a /telemetry snapshot series (TelemetryPoint → CurvePoint)", () => {
    const s = dashboardReducer(initialDashboardViewModel, {
      kind: "seed",
      points: [tp(0), tp(30), tp(60)].map((p) => ({
        t: p.elapsed_seconds,
        bean: p.bean_temp_c,
        env: p.env_temp_c,
        ror: p.bean_ror_c_per_min,
        heat: p.heat_level_percent,
        fan: p.fan_level_percent,
      })),
    });
    expect(s.points.map((p) => p.t)).toEqual([0, 30, 60]);
    expect(s.points[0]).toMatchObject({ t: 0, bean: 100, heat: 70, fan: 40 });
  });

  it("re-seed does not duplicate already-present ticks (reconnect catch-up)", () => {
    const seed = [
      { t: 0, bean: 100, env: 120, ror: 12, heat: 70, fan: 40 },
      { t: 30, bean: 130, env: 150, ror: 12, heat: 70, fan: 40 },
    ];
    let s = dashboardReducer(initialDashboardViewModel, { kind: "seed", points: seed });
    // Reconnect re-seeds the SAME window plus one new tick.
    s = dashboardReducer(s, {
      kind: "seed",
      points: [...seed, { t: 60, bean: 160, env: 180, ror: 12, heat: 70, fan: 40 }],
    });
    expect(s.points.map((p) => p.t)).toEqual([0, 30, 60]); // no dupes
  });

  it("a re-seed never clobbers a fresher live frame for the same tick", () => {
    // Seed t=0..30, then a LIVE frame updates t=30, then a reconnect re-seeds t=30.
    let s = dashboardReducer(initialDashboardViewModel, {
      kind: "seed",
      points: [
        { t: 0, bean: 100, env: 120, ror: 12, heat: 70, fan: 40 },
        { t: 30, bean: 130, env: 150, ror: 12, heat: 70, fan: 40 },
      ],
    });
    // Live frames key on serve elapsed_seconds (#326); the seam tick is serve t=30.
    s = dashboardReducer(
      s,
      ev("telemetry", { elapsed_seconds: 30, charge_elapsed_seconds: 0, bean_temp_c: 999, env_temp_c: 150, bean_ror_c_per_min: 12, heat_percent: 70, fan_percent: 40 }),
    );
    s = dashboardReducer(s, {
      kind: "seed",
      points: [{ t: 30, bean: 130, env: 150, ror: 12, heat: 70, fan: 40 }],
    });
    // The live value (999) survives the re-seed (existing points win on collision).
    expect(s.points.find((p) => p.t === 30)?.bean).toBe(999);
  });

  it("a live frame replaces, not duplicates, a seeded tick at the seam", () => {
    let s = dashboardReducer(initialDashboardViewModel, {
      kind: "seed",
      points: [
        { t: 0, bean: 100, env: 120, ror: 12, heat: 70, fan: 40 },
        { t: 30, bean: 130, env: 150, ror: 12, heat: 70, fan: 40 },
      ],
    });
    // Live frame at the last seeded tick (serve t=30) with a fresher value, then a
    // new tick. Live frames key on serve elapsed_seconds (#326).
    s = dashboardReducer(
      s,
      ev("telemetry", { elapsed_seconds: 30, charge_elapsed_seconds: 0, bean_temp_c: 200, env_temp_c: 150, bean_ror_c_per_min: 12, heat_percent: 70, fan_percent: 40 }),
    );
    s = dashboardReducer(
      s,
      ev("telemetry", { elapsed_seconds: 60, charge_elapsed_seconds: 30, bean_temp_c: 260, env_temp_c: 180, bean_ror_c_per_min: 12, heat_percent: 70, fan_percent: 40 }),
    );
    expect(s.points.map((p) => p.t)).toEqual([0, 30, 60]); // t=30 not duplicated
    expect(s.points.find((p) => p.t === 30)?.bean).toBe(200); // live value won
  });

  it("keeps points ascending and deduped when a frame arrives out of order", () => {
    let s = dashboardReducer(initialDashboardViewModel, { kind: "seed", points: [
      { t: 0, bean: 100, env: 120, ror: 12, heat: 70, fan: 40 },
      { t: 60, bean: 160, env: 180, ror: 12, heat: 70, fan: 40 },
    ] });
    // A late frame for serve-t=30 (out of order) inserts between 0 and 60.
    s = dashboardReducer(
      s,
      ev("telemetry", { elapsed_seconds: 30, charge_elapsed_seconds: 0, bean_temp_c: 130, env_temp_c: 150, bean_ror_c_per_min: 12, heat_percent: 70, fan_percent: 40 }),
    );
    expect(s.points.map((p) => p.t)).toEqual([0, 30, 60]);
  });

  it("interleaves a re-seed that FILLS gaps between live points, staying ascending (#155 merge)", () => {
    // The mergeSeed rewrite (#155) is a two-pointer merge of two ascending sequences;
    // this guards the interleave the old per-point insertion handled — a re-seed whose
    // ticks fall BETWEEN existing live points, supplied out of order and with an
    // internal duplicate, must still produce one ascending, deduped, existing-wins curve.
    let s = dashboardReducer(initialDashboardViewModel, {
      kind: "seed",
      points: [
        { t: 0, bean: 100, env: 120, ror: 12, heat: 70, fan: 40 },
        { t: 60, bean: 999, env: 180, ror: 12, heat: 70, fan: 40 }, // a "live" value at t=60
      ],
    });
    // A backfill that fills t=30 and t=90 (gaps), arrives unsorted, repeats t=60 (a dupe
    // vs the existing point) and repeats t=30 within itself.
    s = dashboardReducer(s, {
      kind: "seed",
      points: [
        { t: 90, bean: 190, env: 210, ror: 12, heat: 70, fan: 40 },
        { t: 30, bean: 130, env: 150, ror: 12, heat: 70, fan: 40 },
        { t: 60, bean: 160, env: 180, ror: 12, heat: 70, fan: 40 }, // dupe of existing → skipped
        { t: 30, bean: 131, env: 151, ror: 12, heat: 70, fan: 40 }, // dupe within seed → first wins
      ],
    });
    // One ascending, deduped sequence covering both windows.
    expect(s.points.map((p) => p.t)).toEqual([0, 30, 60, 90]);
    // Existing point at t=60 won over the seed's duplicate (999, not 160).
    expect(s.points.find((p) => p.t === 60)?.bean).toBe(999);
    // The first seed entry for the within-seed duplicate t=30 won (130, not 131).
    expect(s.points.find((p) => p.t === 30)?.bean).toBe(130);
  });

  it("sets the latest advisory + verdict from an advisory frame with a decision", () => {
    const s = dashboardReducer(initialDashboardViewModel, ev("advisory", ADVISORY_DECISION));
    expect(s.latestAdvisory?.decision?.target_heat).toBe(60);
    expect(s.latestAdvisory?.evaluation?.verdict).toBe("clamp");
    expect(s.advisoryHistory).toHaveLength(1);
  });

  it("stamps the advisory with the latest telemetry's serve-time + bean-temp (#325)", () => {
    // The advisory SSE frame carries no clock/telemetry, so the record is stamped
    // from the latest plotted telemetry point the reducer holds — server-derived.
    let s = dashboardReducer(initialDashboardViewModel, ev("telemetry", { elapsed_seconds: 1010, charge_elapsed_seconds: 480, bean_temp_c: 203, env_temp_c: 215 }));
    s = dashboardReducer(s, ev("advisory", ADVISORY_DECISION));
    expect(s.latestAdvisory?.atServeSeconds).toBe(1010);
    expect(s.latestAdvisory?.beanTempC).toBe(203);
  });

  it("each advisory captures the serve-time/bean-temp at ITS tick (distinguishable rows, #325)", () => {
    // The #325 motivation: successive advisories at different roast-moments must
    // carry different context. Stamp at each fold from the then-latest point.
    let s = dashboardReducer(initialDashboardViewModel, ev("telemetry", { elapsed_seconds: 950, charge_elapsed_seconds: 420, bean_temp_c: 195, env_temp_c: 210 }));
    s = dashboardReducer(s, ev("advisory", ADVISORY_DECISION));
    s = dashboardReducer(s, ev("telemetry", { elapsed_seconds: 1010, charge_elapsed_seconds: 480, bean_temp_c: 203, env_temp_c: 215 }));
    s = dashboardReducer(s, ev("advisory", ADVISORY_DECISION));
    // History is newest-first: the later advisory (1010/203) leads, the earlier
    // (950/195) follows — distinct stamps, so the rows are no longer identical.
    expect(s.advisoryHistory.map((r) => [r.atServeSeconds, r.beanTempC])).toEqual([
      [1010, 203],
      [950, 195],
    ]);
  });

  it("stamps null time/temp for an advisory folded before any telemetry (#325)", () => {
    // No plotted point yet → null stamps (never a fabricated 0). The panel renders
    // the formatter placeholders for these.
    const s = dashboardReducer(initialDashboardViewModel, ev("advisory", ADVISORY_DECISION));
    expect(s.latestAdvisory?.atServeSeconds).toBeNull();
    expect(s.latestAdvisory?.beanTempC).toBeNull();
  });

  it("marks the synthesized replay CLAMP key frame", () => {
    const s = dashboardReducer(
      initialDashboardViewModel,
      ev("advisory", { ...ADVISORY_DECISION, synthesized: true, source: "replay_overlay" }),
    );
    expect(s.latestAdvisory?.synthesized).toBe(true);
  });

  it("caps the advisory history at the display limit", () => {
    let s = initialDashboardViewModel;
    for (let i = 0; i < ADVISORY_HISTORY_LIMIT + 3; i++) {
      s = dashboardReducer(s, ev("advisory", ADVISORY_DECISION));
    }
    expect(s.advisoryHistory).toHaveLength(ADVISORY_HISTORY_LIMIT);
  });

  it("assigns each advisory a unique, monotonic seq (stable list key)", () => {
    let s = initialDashboardViewModel;
    s = dashboardReducer(s, ev("advisory", ADVISORY_DECISION));
    s = dashboardReducer(s, ev("advisory", ADVISORY_DECISION));
    s = dashboardReducer(s, ev("advisory", ADVISORY_DECISION));
    const seqs = s.advisoryHistory.map((r) => r.seq);
    expect(new Set(seqs).size).toBe(seqs.length); // all unique
    expect(s.advisorySeq).toBe(3);
  });

  it("folds the pause/resume toggle into advisoryPaused without entering the feed", () => {
    let s = dashboardReducer(initialDashboardViewModel, ev("advisory", { advisory_paused: true }));
    expect(s.advisoryPaused).toBe(true);
    expect(s.advisoryHistory).toHaveLength(0);
    s = dashboardReducer(s, ev("advisory", { advisory_paused: false }));
    expect(s.advisoryPaused).toBe(false);
  });

  it("ignores a skipped advisory record (no decision, no evaluation)", () => {
    const s = dashboardReducer(
      initialDashboardViewModel,
      ev("advisory", { trigger: "manual", skipped: "no_telemetry" }),
    );
    expect(s.latestAdvisory).toBeNull();
    expect(s.advisoryHistory).toHaveLength(0);
  });

  it("does not fold charge_guidance into the view-model (#211/#215: cue is derived via ChargeBanner)", () => {
    // The live add-beans cue is now derived (ChargeBanner from phase + telemetry +
    // band), so the reducer no longer surfaces the charge_guidance frame; it's a
    // no-op here (the raw frame stays in the event buffer for a future trace panel).
    const s = dashboardReducer(
      initialDashboardViewModel,
      ev("charge_guidance", { bean_temp_c: 180, env_temp_c: 190, guidance_min_c: 170, guidance_max_c: 200 }),
    );
    expect(s).toBe(initialDashboardViewModel);
  });

  it("captures a recovery handshake and adds it to the safety trail", () => {
    const evalData = { rule: "pre_t0_overrun", verdict: "recovery", input_heat: null, input_fan: null, adjusted_heat: 0, adjusted_fan: 100, reason: "pre-T0 overrun" };
    const s = dashboardReducer(initialDashboardViewModel, ev("recovery_required", evalData));
    expect(s.recovery?.verdict).toBe("recovery");
    expect(s.safetyTrail).toHaveLength(1);
    expect(s.safetyTrail[0].kind).toBe("recovery_required");
  });

  it("clears the recovery trigger on recovery_acknowledged", () => {
    let s = dashboardReducer(initialDashboardViewModel, ev("recovery_required", { rule: "r", verdict: "recovery", input_heat: null, input_fan: null, adjusted_heat: 0, adjusted_fan: 100, reason: "x" }));
    s = dashboardReducer(s, ev("recovery_acknowledged", {}));
    expect(s.recovery).toBeNull();
  });

  it("captures a fault handshake + accumulates safety_alert + fault on the trail", () => {
    let s = dashboardReducer(initialDashboardViewModel, ev("safety_alert", { rule: "env_ceiling", verdict: "emergency_stop", input_heat: null, input_fan: null, adjusted_heat: 0, adjusted_fan: 100, reason: "env temp exceeded ceiling" }));
    s = dashboardReducer(s, ev("fault", { rule: "env_ceiling", verdict: "fault", input_heat: null, input_fan: null, adjusted_heat: 0, adjusted_fan: 100, reason: "faulted" }));
    expect(s.fault?.verdict).toBe("fault");
    expect(s.safetyTrail.map((e) => e.kind)).toEqual(["safety_alert", "fault"]);
  });

  it("adds T0 and first-crack markers at the serve-elapsed of detection (once each, #326)", () => {
    // Markers key on serve elapsed (#326): plot a preheat frame, then the charge
    // tick (serve 510) at which T0 fires, then a post-charge tick before FC.
    let s = dashboardReducer(initialDashboardViewModel, ev("telemetry", { elapsed_seconds: 480, charge_elapsed_seconds: null, bean_temp_c: 90, env_temp_c: 180 }));
    s = dashboardReducer(s, ev("telemetry", { elapsed_seconds: 510, charge_elapsed_seconds: 0, bean_temp_c: 160, env_temp_c: 200 }));
    s = dashboardReducer(s, ev("t0_detected", { bean_temp_c: 160 }));
    s = dashboardReducer(s, ev("telemetry", { elapsed_seconds: 1010, charge_elapsed_seconds: 500, bean_temp_c: 200, env_temp_c: 210 }));
    s = dashboardReducer(s, ev("first_crack", { source: "mcp", bean_temp_c: 201 }));
    // Re-deliver first_crack — must not duplicate the marker.
    s = dashboardReducer(s, ev("first_crack", { source: "mcp", bean_temp_c: 201 }));
    expect(s.t0).not.toBeNull();
    expect(s.firstCrack?.source).toBe("mcp");
    expect(s.markers.filter((m) => m.kind === "first_crack")).toHaveLength(1);
    // FC marker sits at the latest point's serve-elapsed (1010), not the charge clock.
    expect(s.markers.find((m) => m.kind === "first_crack")?.t).toBe(1010);
  });

  it("sets t0ElapsedSeconds + the T0 marker to the serve-elapsed at charge (#326)", () => {
    // t0ElapsedSeconds is set by the first post-charge telemetry frame via
    // withRecoveredOrigin (elapsed − charge_elapsed). The T0 marker anchors at the
    // recovered charge serve-elapsed so the axis reads 0:00 = charge. t0_detected
    // updates the detection record only; it does not move the marker or the origin.
    let s = dashboardReducer(initialDashboardViewModel, ev("telemetry", { elapsed_seconds: 480, charge_elapsed_seconds: null, bean_temp_c: 90, env_temp_c: 180 }));
    expect(s.t0ElapsedSeconds).toBeNull();
    s = dashboardReducer(s, ev("telemetry", { elapsed_seconds: 540, charge_elapsed_seconds: 0, bean_temp_c: 160, env_temp_c: 200 }));
    s = dashboardReducer(s, ev("t0_detected", { bean_temp_c: 160 }));
    expect(s.t0ElapsedSeconds).toBe(540);
    expect(s.markers.find((m) => m.kind === "t0")).toEqual({ kind: "t0", t: 540, label: "T0" });
  });

  it("anchors the T0 marker at the charge tick even when t0_detected fires before the first post-charge telemetry (#404)", () => {
    // Regression guard for the roast-7 / roast-8 bug: the t0_detected event fires
    // after a debounce (~11 s post-charge). In the production SSE delivery order,
    // t0_detected arrives in the same tick-batch as — or just before — the first
    // telemetry frame carrying charge_elapsed_seconds. If t0_detected is dispatched
    // first (t0ElapsedSeconds still null), using the latest preheat point's t would
    // place the marker at the thermal dip (~153 °C) instead of the charge peak
    // (~170-181 °C).
    //
    // The fix: t0_detected only records the detection. withRecoveredOrigin on the
    // first post-charge telemetry frame sets the authoritative charge serve-elapsed
    // via elapsed − charge_elapsed (= 551 − 11 = 540 here), and that is where the
    // T0 marker is placed.
    let s = initialDashboardViewModel;
    // Preheat frames — charge has not happened yet.
    s = dashboardReducer(s, ev("telemetry", { elapsed_seconds: 530, charge_elapsed_seconds: null, bean_temp_c: 175, env_temp_c: 220 }));
    s = dashboardReducer(s, ev("telemetry", { elapsed_seconds: 540, charge_elapsed_seconds: null, bean_temp_c: 181, env_temp_c: 222 }));
    // t0_detected fires before the server has emitted the first post-charge telemetry.
    s = dashboardReducer(s, ev("t0_detected", { bean_temp_c: 153, debounce_ticks: 11 }));
    // t0 detection is recorded; origin and marker are deferred to the telemetry path.
    expect(s.t0).not.toBeNull();
    expect(s.t0ElapsedSeconds).toBeNull();
    expect(s.markers.some((m) => m.kind === "t0")).toBe(false);
    // Post-charge frames arrive — first one has charge_elapsed_seconds non-null.
    // elapsed − charge_elapsed = 551 − 11 = 540 = the actual charge serve-elapsed.
    s = dashboardReducer(s, ev("telemetry", { elapsed_seconds: 551, charge_elapsed_seconds: 11, bean_temp_c: 153, env_temp_c: 218 }));
    // T0 marker must be at the CHARGE serve-elapsed (540), not the detection tick (551).
    expect(s.t0ElapsedSeconds).toBe(540);
    expect(s.markers.find((m) => m.kind === "t0")).toEqual({ kind: "t0", t: 540, label: "T0" });
    // Later telemetry must not move the established origin/marker.
    s = dashboardReducer(s, ev("telemetry", { elapsed_seconds: 561, charge_elapsed_seconds: 21, bean_temp_c: 150, env_temp_c: 215 }));
    expect(s.t0ElapsedSeconds).toBe(540);
    expect(s.markers.filter((m) => m.kind === "t0")).toHaveLength(1);
  });

  it("recovers the T0 origin from a live post-charge telemetry frame on reload (no t0_detected, #326)", () => {
    // SSE doesn't replay t0_detected: a reload mid-roast folds telemetry without ever
    // seeing the live T0 event. The first post-charge frame (charge_elapsed_seconds
    // non-null) recovers the origin from the server's own clocks —
    // elapsed − charge_elapsed — and places the T0 marker, so the axis reads roast
    // time. A pre-charge frame (null charge clock) leaves the origin unknown.
    let s = dashboardReducer(
      initialDashboardViewModel,
      ev("telemetry", { elapsed_seconds: 400, charge_elapsed_seconds: null, bean_temp_c: 90, env_temp_c: 180 }),
    );
    expect(s.t0ElapsedSeconds).toBeNull();
    // First post-charge frame: serve 600, charge 60 → origin = 540.
    s = dashboardReducer(
      s,
      ev("telemetry", { elapsed_seconds: 600, charge_elapsed_seconds: 60, bean_temp_c: 165, env_temp_c: 205 }),
    );
    expect(s.t0ElapsedSeconds).toBe(540);
    expect(s.markers.find((m) => m.kind === "t0")).toEqual({ kind: "t0", t: 540, label: "T0" });
    // A later post-charge frame must NOT move the established origin/marker.
    s = dashboardReducer(
      s,
      ev("telemetry", { elapsed_seconds: 630, charge_elapsed_seconds: 90, bean_temp_c: 170, env_temp_c: 206 }),
    );
    expect(s.t0ElapsedSeconds).toBe(540);
    expect(s.markers.filter((m) => m.kind === "t0")).toHaveLength(1);
  });

  it("recovers the T0 origin from a seeded /telemetry snapshot on cold reload (#326)", () => {
    // The cold-reload/late-join path: the backfill seed carries post-charge snapshot
    // points, and the seed action passes the origin recovered from their server clocks
    // (first post-charge point: serve 510 − charge 0 = 510). No t0_detected fires.
    const s = dashboardReducer(initialDashboardViewModel, {
      kind: "seed",
      origin: 510,
      points: [
        { t: 510, bean: 160, env: 200, ror: 18, heat: 70, fan: 40 },
        { t: 540, bean: 165, env: 205, ror: 16, heat: 70, fan: 40 },
      ],
    });
    expect(s.t0ElapsedSeconds).toBe(510);
    expect(s.markers.find((m) => m.kind === "t0")).toEqual({ kind: "t0", t: 510, label: "T0" });
    expect(s.points.map((p) => p.t)).toEqual([510, 540]);
  });

  it("a seed with a null origin (pre-charge-only snapshot) leaves t0ElapsedSeconds null (#326)", () => {
    const s = dashboardReducer(initialDashboardViewModel, {
      kind: "seed",
      origin: null,
      points: [{ t: 90, bean: 60, env: 180, ror: 30, heat: 100, fan: 30 }],
    });
    expect(s.t0ElapsedSeconds).toBeNull();
    expect(s.markers.some((m) => m.kind === "t0")).toBe(false);
  });

  it("a telemetry-derived origin is not overwritten by a later seed/frame (#326)", () => {
    // withRecoveredOrigin sets the origin on the first post-charge telemetry frame
    // (charge_elapsed_seconds: 0 → elapsed − 0 = 540). A subsequent reconnect
    // re-seed with a different recovered origin must NOT move the established value
    // (existing origin wins — withRecoveredOrigin is a no-op once t0ElapsedSeconds
    // is non-null). t0_detected does not touch t0ElapsedSeconds (#404).
    let s = dashboardReducer(initialDashboardViewModel, ev("telemetry", { elapsed_seconds: 540, charge_elapsed_seconds: 0, bean_temp_c: 160, env_temp_c: 200 }));
    expect(s.t0ElapsedSeconds).toBe(540); // set by withRecoveredOrigin
    s = dashboardReducer(s, ev("t0_detected", { bean_temp_c: 160 }));
    expect(s.t0ElapsedSeconds).toBe(540); // t0_detected is a no-op for t0ElapsedSeconds
    s = dashboardReducer(s, { kind: "seed", origin: 999, points: [{ t: 600, bean: 170, env: 205, ror: 16, heat: 70, fan: 40 }] });
    expect(s.t0ElapsedSeconds).toBe(540); // unchanged — first-wins on the established origin
  });

  it("t0_detected does NOT overwrite an already-derived t0ElapsedSeconds (#326/#404)", () => {
    // The canonical origin is derived by withRecoveredOrigin from the first post-charge
    // telemetry frame (elapsed − charge_elapsed). t0_detected only records the
    // detection; it does not touch t0ElapsedSeconds or the markers, so the established
    // origin (520) is unchanged regardless of what the latest plotted point's t is (600).
    let s = dashboardReducer(
      initialDashboardViewModel,
      ev("telemetry", { elapsed_seconds: 580, charge_elapsed_seconds: 60, bean_temp_c: 165, env_temp_c: 205 }),
    );
    expect(s.t0ElapsedSeconds).toBe(520); // derived: 580 − 60
    s = dashboardReducer(s, ev("telemetry", { elapsed_seconds: 600, charge_elapsed_seconds: 80, bean_temp_c: 170, env_temp_c: 206 }));
    s = dashboardReducer(s, ev("t0_detected", { bean_temp_c: 165 }));
    expect(s.t0ElapsedSeconds).toBe(520); // unchanged — telemetry origin wins
    expect(s.t0).not.toBeNull();
  });

  it("t0_detected with an EMPTY buffer leaves t0ElapsedSeconds null (no point to anchor, #326)", () => {
    // With no plotted point there's no serve-elapsed for charge; defaulting to 0
    // would mislabel preheat as large positive roast-time. Leave the origin null
    // (and place no marker) — the telemetry-derive path sets it once a post-charge
    // frame lands. The detection itself is still recorded.
    const s = dashboardReducer(initialDashboardViewModel, ev("t0_detected", { bean_temp_c: 175 }));
    expect(s.t0).not.toBeNull();
    expect(s.t0ElapsedSeconds).toBeNull();
    expect(s.markers.some((m) => m.kind === "t0")).toBe(false);
  });

  it("plots the curve in SERVE-elapsed time, preheat included (#326)", () => {
    // The buffer keys on serve elapsed: a preheat frame (null charge clock) AND the
    // post-charge frames all plot, continuous through preheat → charge → roast. RoR
    // is real probe data and stays plotted (the operator still steers by it).
    let s = dashboardReducer(
      initialDashboardViewModel,
      ev("telemetry", { elapsed_seconds: 300, charge_elapsed_seconds: null, bean_temp_c: 60, env_temp_c: 190, bean_ror_c_per_min: 30 }),
    );
    s = dashboardReducer(
      s,
      ev("telemetry", { elapsed_seconds: 510, charge_elapsed_seconds: 0, bean_temp_c: 148, env_temp_c: 200, bean_ror_c_per_min: 22 }),
    );
    s = dashboardReducer(
      s,
      ev("telemetry", { elapsed_seconds: 540, charge_elapsed_seconds: 30, bean_temp_c: 165, env_temp_c: 205, bean_ror_c_per_min: 18 }),
    );
    expect(s.points.map((p) => p.t)).toEqual([300, 510, 540]);
    expect(s.points.every((p) => p.ror !== null)).toBe(true);
    expect(s.points[1]).toMatchObject({ t: 510, ror: 22 });
  });

  it("adds a drop marker at the latest point's serve-elapsed on drop_beans (#326)", () => {
    let s = dashboardReducer(initialDashboardViewModel, ev("telemetry", { elapsed_seconds: 1100, charge_elapsed_seconds: 600, bean_temp_c: 215, env_temp_c: 220 }));
    s = dashboardReducer(s, ev("command_executed", { command: "drop_beans", source: "operator" }));
    expect(s.markers.find((m) => m.kind === "drop")?.t).toBe(1100);
  });

  it("ignores a non-drop command_executed (no marker)", () => {
    const s = dashboardReducer(
      initialDashboardViewModel,
      ev("command_executed", { heat_percent: 65, fan_percent: 40 }),
    );
    expect(s.markers).toHaveLength(0);
  });

  it("adds a cooling marker at the latest point's serve-elapsed on phase_changed→cooling (#309)", () => {
    // Cooling is sourced from the server phase value, NOT inferred: the reducer
    // reads phase_changed.phase === "cooling" and places the marker at the latest
    // plotted point's serve-elapsed (the T0/FC/drop axis, #326). It never sets phase.
    let s = dashboardReducer(initialDashboardViewModel, ev("telemetry", { elapsed_seconds: 1130, charge_elapsed_seconds: 630, bean_temp_c: 205, env_temp_c: 215 }));
    s = dashboardReducer(s, ev("phase_changed", { phase: "cooling", enabled_actions: ["stop_cooling"] }));
    expect(s.markers.find((m) => m.kind === "cooling")).toEqual({ kind: "cooling", t: 1130, label: "COOLING" });
  });

  it("places the cooling marker once — a re-fired cooling phase does not duplicate it (#309)", () => {
    let s = dashboardReducer(initialDashboardViewModel, ev("telemetry", { elapsed_seconds: 1130, charge_elapsed_seconds: 630, bean_temp_c: 205, env_temp_c: 215 }));
    s = dashboardReducer(s, ev("phase_changed", { phase: "cooling" }));
    // A later telemetry frame then a re-delivered cooling phase_changed must not
    // move/duplicate the marker (withMarker dedupe; FIRST-wins on t).
    s = dashboardReducer(s, ev("telemetry", { elapsed_seconds: 1160, charge_elapsed_seconds: 660, bean_temp_c: 200, env_temp_c: 90 }));
    s = dashboardReducer(s, ev("phase_changed", { phase: "cooling" }));
    expect(s.markers.filter((m) => m.kind === "cooling")).toHaveLength(1);
    expect(s.markers.find((m) => m.kind === "cooling")?.t).toBe(1130);
  });

  it("does NOT place a cooling marker for a non-cooling phase_changed (no client phase inference, #309)", () => {
    // Every other phase transition (e.g. development) is the shared reducer's
    // concern — the dashboard reducer never sets phase and places no cooling marker.
    // (A pre-charge telemetry frame is used so no T0 origin/marker is auto-recovered
    // from the server clocks; we are isolating the phase_changed handler here.)
    let s = dashboardReducer(initialDashboardViewModel, ev("telemetry", { elapsed_seconds: 300, charge_elapsed_seconds: null, bean_temp_c: 80, env_temp_c: 190 }));
    s = dashboardReducer(s, ev("phase_changed", { phase: "development" }));
    s = dashboardReducer(s, ev("phase_changed", { phase: "roasting_pre_first_crack" }));
    expect(s.markers.some((m) => m.kind === "cooling")).toBe(false);
    expect(s.markers).toHaveLength(0);
  });

  it("adds the dry-end marker at the latest point's serve-elapsed on drying_end, once (#351)", () => {
    // The pre-FC drying-end landmark (#351) is server-sourced (the controller's
    // bean-temp threshold cross → the drying_end SSE event). Like FC its payload
    // carries no clock, so the marker rides the latest plotted point's serve-elapsed
    // (the same #326 axis as T0/FC/drop/cooling) and fires exactly once.
    let s = dashboardReducer(initialDashboardViewModel, ev("telemetry", { elapsed_seconds: 480, charge_elapsed_seconds: null, bean_temp_c: 90, env_temp_c: 180 }));
    s = dashboardReducer(s, ev("telemetry", { elapsed_seconds: 870, charge_elapsed_seconds: 360, bean_temp_c: 150, env_temp_c: 200 }));
    s = dashboardReducer(s, ev("drying_end", { bean_temp_c: 151, threshold_c: 150 }));
    // Re-deliver drying_end — must not duplicate the marker.
    s = dashboardReducer(s, ev("drying_end", { bean_temp_c: 151, threshold_c: 150 }));
    expect(s.markers.filter((m) => m.kind === "dry_end")).toHaveLength(1);
    expect(s.markers.find((m) => m.kind === "dry_end")).toEqual({ kind: "dry_end", t: 870, label: "DRY END" });
  });

  it("renders the full server-sourced marker set: charge/T0, dry-end, FC, drop, cooling (#309/#351)", () => {
    // Every marker from its own server signal, all riding the same serve-elapsed
    // axis (#326). Dry-end (#351) now has a server signal (the drying_end event),
    // so it joins the four #309 markers in roast order.
    let s = dashboardReducer(initialDashboardViewModel, ev("telemetry", { elapsed_seconds: 510, charge_elapsed_seconds: 0, bean_temp_c: 160, env_temp_c: 200 }));
    s = dashboardReducer(s, ev("t0_detected", { bean_temp_c: 160 }));
    s = dashboardReducer(s, ev("telemetry", { elapsed_seconds: 870, charge_elapsed_seconds: 360, bean_temp_c: 151, env_temp_c: 205 }));
    s = dashboardReducer(s, ev("drying_end", { bean_temp_c: 151, threshold_c: 150 }));
    s = dashboardReducer(s, ev("telemetry", { elapsed_seconds: 1010, charge_elapsed_seconds: 500, bean_temp_c: 201, env_temp_c: 215 }));
    s = dashboardReducer(s, ev("first_crack", { source: "mcp", bean_temp_c: 201 }));
    s = dashboardReducer(s, ev("telemetry", { elapsed_seconds: 1100, charge_elapsed_seconds: 590, bean_temp_c: 213, env_temp_c: 218 }));
    s = dashboardReducer(s, ev("command_executed", { command: "drop_beans", source: "operator" }));
    s = dashboardReducer(s, ev("phase_changed", { phase: "cooling" }));
    const byKind = Object.fromEntries(s.markers.map((m) => [m.kind, m.t]));
    expect(byKind).toEqual({ t0: 510, dry_end: 870, first_crack: 1010, drop: 1100, cooling: 1100 });
  });

  it("resets to the initial view-model", () => {
    let s = dashboardReducer(initialDashboardViewModel, ev("advisory", ADVISORY_DECISION));
    s = dashboardReducer(s, { kind: "reset" });
    expect(s).toEqual(initialDashboardViewModel);
  });
});

// These pin the EXACT field names the page handlers read off raw `lastEvent.data`
// — the payloads are NOT in shared types.ts (the handlers cast), so this is the
// guard against the contract-drift class that bit `phase_changed` (server sent
// `phase`, the reducer read `agent_phase`). Each payload below is keyed exactly as
// the server emit site keys it (controller.py / replay.py / safety.py model_dump);
// if a server field is renamed, the matching assertion fails loud here.
describe("dashboardReducer — payload field-name contract", () => {
  it("advisory: reads decision.{target_heat,target_fan,should_drop,confidence,rationale} + evaluation.{verdict,reason}", () => {
    // advisor.RoastDecision.model_dump + safety.SafetyEvaluation.model_dump, as
    // emitted at controller.py:814 / replay.py:645.
    const s = dashboardReducer(
      initialDashboardViewModel,
      ev("advisory", {
        trigger: "tick",
        decision: { target_heat: 60, target_fan: 75, should_drop: true, confidence: 0.82, rationale: "stretch development" },
        evaluation: { rule: "rate_limit", verdict: "clamp", input_heat: 80, input_fan: 40, adjusted_heat: 65, adjusted_fan: 40, reason: "heat clamped 80→65" },
      }),
    );
    const a = s.latestAdvisory;
    expect(a?.decision).toEqual({ target_heat: 60, target_fan: 75, should_drop: true, confidence: 0.82, rationale: "stretch development" });
    expect(a?.evaluation?.verdict).toBe("clamp");
    expect(a?.evaluation?.reason).toBe("heat clamped 80→65");
  });

  it("advisory toggle: reads `advisory_paused` (controller.py:1129/1135)", () => {
    expect(
      dashboardReducer(initialDashboardViewModel, ev("advisory", { advisory_paused: true })).advisoryPaused,
    ).toBe(true);
  });

  it("charge_guidance: not folded into the view-model (#211/#215 — cue derived via ChargeBanner)", () => {
    // The reducer no longer reads the charge_guidance payload; the live cue derives
    // from phase + telemetry + the profile band. The wire shape is documented by
    // `ChargeGuidanceData` in events.ts and guarded by the contract-fixture test.
    const next = dashboardReducer(
      initialDashboardViewModel,
      ev("charge_guidance", { bean_temp_c: 185, env_temp_c: 195, guidance_min_c: 170, guidance_max_c: 200 }),
    );
    expect(next).toBe(initialDashboardViewModel);
  });

  it("recovery_required / fault: reads SafetyEvaluation {rule,verdict,input_heat,input_fan,adjusted_heat,adjusted_fan,reason}", () => {
    const evalData = { rule: "pre_t0_overrun", verdict: "recovery" as const, input_heat: null, input_fan: null, adjusted_heat: 0, adjusted_fan: 100, reason: "pre-T0 overrun" };
    const r = dashboardReducer(initialDashboardViewModel, ev("recovery_required", evalData)).recovery;
    expect(r).toEqual(evalData);
    const faultData = { ...evalData, verdict: "fault" as const, reason: "faulted" };
    const f = dashboardReducer(initialDashboardViewModel, ev("fault", faultData)).fault;
    expect(f).toEqual(faultData);
  });

  it("first_crack: reads `source` + `bean_temp_c` (controller.py:620)", () => {
    const fc = dashboardReducer(
      initialDashboardViewModel,
      ev("first_crack", { source: "mcp", bean_temp_c: 201.2 }),
    ).firstCrack;
    expect(fc).toEqual({ source: "mcp", bean_temp_c: 201.2 });
  });

  it("command_executed: reads `command` (the drop marker key, controller.py:849/1030)", () => {
    const s = dashboardReducer(
      dashboardReducer(initialDashboardViewModel, ev("telemetry", { elapsed_seconds: 1100, charge_elapsed_seconds: 600, bean_temp_c: 215, env_temp_c: 220 })),
      ev("command_executed", { command: "drop_beans", source: "advisor" }),
    );
    expect(s.markers.some((m) => m.kind === "drop")).toBe(true);
  });

  it("phase_changed: reads `phase` (the cooling marker key — `phase`, NOT agent_phase; api.py _phase_changed_with_actions)", () => {
    // The wire field is `phase` (the controller's emit) enriched with
    // enabled_actions — guard the exact name the cooling-marker handler reads, the
    // same contract-drift class that previously bit phase_changed.
    const s = dashboardReducer(
      dashboardReducer(initialDashboardViewModel, ev("telemetry", { elapsed_seconds: 1130, charge_elapsed_seconds: 630, bean_temp_c: 205, env_temp_c: 215 })),
      ev("phase_changed", { phase: "cooling", enabled_actions: ["stop_cooling"] }),
    );
    expect(s.markers.some((m) => m.kind === "cooling")).toBe(true);
  });
});

describe("snapshotFault (#329 — restore/reload fault from the hydrated snapshot)", () => {
  it("synthesizes a fault evaluation when the server phase is faulted", () => {
    const f = snapshotFault("faulted", "env ceiling exceeded");
    expect(f).not.toBeNull();
    expect(f?.verdict).toBe("fault");
    expect(f?.reason).toBe("env ceiling exceeded");
    // Mirrors the server-guaranteed fail-closed posture in faulted (heat off, fan held).
    expect(f?.adjusted_heat).toBe(0);
    expect(f?.adjusted_fan).toBeNull();
  });

  it("falls back to a generic reason when the snapshot carries no fault_reason", () => {
    // The snapshot persists the phase but may not carry a reason — still render the
    // banner (the operator needs the ACKNOWLEDGE button), with an honest placeholder.
    expect(snapshotFault("faulted", null)?.reason).toBe("Run faulted — heat forced off.");
    expect(snapshotFault("faulted", undefined)?.reason).toBe("Run faulted — heat forced off.");
  });

  it("returns null for any non-faulted phase (faulted-only; no false banner)", () => {
    // The fallback is strictly faulted-gated — a normal hydrate must NOT synthesize a
    // fault. Phase is the server's truth; we only branch on it, never infer it.
    for (const phase of ["preheating", "roasting_pre_first_crack", "development", "cooling", "complete", "idle", null] as const) {
      expect(snapshotFault(phase, "anything")).toBeNull();
    }
  });
});

describe("useDashboardEvents drain dedup (#339 resume re-delivery)", () => {
  const FAULT = {
    rule: "env_ceiling",
    verdict: "fault",
    input_heat: null,
    input_fan: null,
    adjusted_heat: 0,
    adjusted_fan: 100,
    reason: "faulted",
  };
  const frame = (id: number): SseEvent => ({ event: "fault", data: { ...FAULT }, id });

  it("does not double-append a re-delivered (resume-replayed) frame to the safety trail", () => {
    // The raw `frames` drain channel is deliberately NOT id-deduped (#122), so a
    // resume/reconnect that re-delivers a fault frame would otherwise append it to
    // safetyTrail twice. The hook guards on event.id; the trail stays length 1.
    const buffer: SseEvent[] = [frame(1)];
    const { result, rerender } = renderHook(
      ({ count }: { count: number }) =>
        useDashboardEvents(buffer, count, "run-1", "connecting"),
      { initialProps: { count: 1 } },
    );
    expect(result.current.safetyTrail).toHaveLength(1);

    // Resume re-delivers the SAME id (1) as a new buffer entry — frameCount advances
    // (the raw channel appends), but the hook must skip it.
    buffer.push(frame(1));
    rerender({ count: 2 });
    expect(result.current.safetyTrail).toHaveLength(1);

    // A genuinely-new id (2) still folds — the guard only skips already-seen ids.
    buffer.push(frame(2));
    rerender({ count: 3 });
    expect(result.current.safetyTrail).toHaveLength(2);
  });

  it("re-applies frames after a run change resets the dedup cursor", () => {
    // The per-run reset clears lastDispatchedId so a new run's id 1 is not mistaken
    // for the previous run's already-seen id 1.
    const buffer: SseEvent[] = [frame(1)];
    const { result, rerender } = renderHook(
      ({ count, runId }: { count: number; runId: string }) =>
        useDashboardEvents(buffer, count, runId, "connecting"),
      { initialProps: { count: 1, runId: "run-1" } },
    );
    expect(result.current.safetyTrail).toHaveLength(1);

    // New run: the hook clears the stream buffer and frameCount drops to 0 (mirrors
    // useRoastStream's per-run reset), then the new run's first frame bumps it to 1.
    buffer.length = 0;
    rerender({ count: 0, runId: "run-2" });
    buffer.push(frame(1));
    rerender({ count: 1, runId: "run-2" });
    expect(result.current.safetyTrail).toHaveLength(1);
  });
});
