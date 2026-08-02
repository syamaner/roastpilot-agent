import { describe, expect, it } from "vitest";

import type { TelemetryPoint, TelemetrySeries } from "@/lib/types";
import { postFcRecoverySummary } from "./recoveryModel";

function series(points: Partial<TelemetryPoint>[]): TelemetrySeries {
  return {
    run_id: "run-1",
    downsample: 1,
    point_count: points.length,
    points: points.map((point, tick) => ({
      tick,
      elapsed_seconds: tick * 5,
      charge_elapsed_seconds: tick * 5,
      agent_phase: "development",
      bean_temp_c: 185,
      env_temp_c: 205,
      bean_ror_c_per_min: 5,
      env_ror_c_per_min: 4,
      heat_level_percent: 60,
      fan_level_percent: 50,
      cooling_on: false,
      development_percent: 10,
      ...point,
    })),
  };
}

describe("postFcRecoverySummary", () => {
  it("reports no observed recovery for empty, pre-v16, and holding-only traces", () => {
    expect(postFcRecoverySummary(undefined).observedRecovery).toBe(false);
    expect(postFcRecoverySummary(series([{}])).recoveryEnabled).toBeNull();
    expect(
      postFcRecoverySummary(
        series([
          {
            post_fc_recovery_enabled: true,
            post_fc_heat_authority_state: "holding",
          },
        ]),
      ).observedRecovery,
    ).toBe(false);
  });

  it("counts cycles, durations, first entry, max ceiling, and glide retriggers", () => {
    const summary = postFcRecoverySummary(
      series([
        { post_fc_recovery_enabled: true, post_fc_heat_authority_state: "holding", post_fc_effective_heat_ceiling_percent: 60 },
        { post_fc_recovery_enabled: true, post_fc_heat_authority_state: "recovering", post_fc_effective_heat_ceiling_percent: 75 },
        { post_fc_recovery_enabled: true, post_fc_heat_authority_state: "recovering", post_fc_effective_heat_ceiling_percent: 75 },
        { post_fc_recovery_enabled: true, post_fc_heat_authority_state: "gliding", post_fc_effective_heat_ceiling_percent: 70 },
        { post_fc_recovery_enabled: true, post_fc_heat_authority_state: "recovering", post_fc_effective_heat_ceiling_percent: 75 },
        { post_fc_recovery_enabled: true, post_fc_heat_authority_state: "holding", post_fc_effective_heat_ceiling_percent: 60 },
      ]),
    );
    expect(summary).toEqual({
      observedRecovery: true,
      recoveryEnabled: true,
      cycleCount: 2,
      firstRecoveryChargeSeconds: 5,
      maxEffectiveHeatCeilingPercent: 75,
      recoveringDurationSeconds: 15,
      glidingDurationSeconds: 5,
      glideToRecoveryRetriggerCount: 1,
    });
  });

  it("clips the final authority interval at drop instead of counting cooling time", () => {
    const summary = postFcRecoverySummary(
      series([
        {
          elapsed_seconds: 100,
          charge_elapsed_seconds: 50,
          post_fc_recovery_enabled: true,
          post_fc_heat_authority_state: "recovering",
        },
        {
          elapsed_seconds: 105,
          charge_elapsed_seconds: 55,
          post_fc_recovery_enabled: true,
          post_fc_heat_authority_state: "gliding",
        },
        {
          elapsed_seconds: 112,
          charge_elapsed_seconds: 58,
          agent_phase: "cooling",
          cooling_on: true,
          post_fc_recovery_enabled: true,
          post_fc_heat_authority_state: null,
        },
      ]),
    );

    expect(summary.recoveringDurationSeconds).toBe(5);
    expect(summary.glidingDurationSeconds).toBe(3);
  });

  it("does not attribute restart downtime to the last pre-restart authority state", () => {
    const summary = postFcRecoverySummary(
      series([
        {
          elapsed_seconds: 45,
          charge_elapsed_seconds: 45,
          post_fc_recovery_enabled: true,
          post_fc_heat_authority_state: "recovering",
        },
        {
          elapsed_seconds: 50,
          charge_elapsed_seconds: 50,
          post_fc_recovery_enabled: true,
          post_fc_heat_authority_state: "recovering",
        },
        {
          elapsed_seconds: 0,
          charge_elapsed_seconds: 180,
          agent_phase: "operator_recovery_required",
          post_fc_recovery_enabled: true,
          post_fc_heat_authority_state: null,
        },
      ]),
    );

    expect(summary.recoveringDurationSeconds).toBe(5);
  });

  it("closes an authority interval at a same-process recovery boundary", () => {
    const summary = postFcRecoverySummary(
      series([
        {
          elapsed_seconds: 100,
          charge_elapsed_seconds: 50,
          post_fc_recovery_enabled: true,
          post_fc_heat_authority_state: "recovering",
        },
        {
          elapsed_seconds: 105,
          charge_elapsed_seconds: 55,
          agent_phase: "operator_recovery_required",
          post_fc_recovery_enabled: true,
          post_fc_heat_authority_state: null,
        },
      ]),
    );

    expect(summary.recoveringDurationSeconds).toBe(5);
  });
});
