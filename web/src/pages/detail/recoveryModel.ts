/** Pure summary of the persisted, server-owned D96 recovery trace (#699). */

import type { PostFcHeatAuthorityState, TelemetrySeries } from "@/lib/types";

export interface PostFcRecoverySummaryData {
  observedRecovery: boolean;
  recoveryEnabled: boolean | null;
  cycleCount: number;
  firstRecoveryChargeSeconds: number | null;
  maxEffectiveHeatCeilingPercent: number | null;
  recoveringDurationSeconds: number;
  glidingDurationSeconds: number;
  glideToRecoveryRetriggerCount: number;
}

function finite(value: number | null | undefined): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

/** Summarize only fields persisted by schema v16; no state is inferred from RoR. */
export function postFcRecoverySummary(
  telemetry: TelemetrySeries | undefined,
): PostFcRecoverySummaryData {
  const points = telemetry?.points ?? [];
  let recoveryEnabled: boolean | null = null;
  let previousState: PostFcHeatAuthorityState | null = null;
  let cycleCount = 0;
  let firstRecoveryChargeSeconds: number | null = null;
  let maxEffectiveHeatCeilingPercent: number | null = null;
  let recoveringDurationSeconds = 0;
  let glidingDurationSeconds = 0;
  let glideToRecoveryRetriggerCount = 0;

  for (let index = 0; index < points.length; index += 1) {
    const point = points[index];
    if (typeof point.post_fc_recovery_enabled === "boolean") {
      recoveryEnabled = point.post_fc_recovery_enabled;
    }
    const ceiling = point.post_fc_effective_heat_ceiling_percent;
    if (finite(ceiling)) {
      maxEffectiveHeatCeilingPercent =
        maxEffectiveHeatCeilingPercent === null
          ? ceiling
          : Math.max(maxEffectiveHeatCeilingPercent, ceiling);
    }

    const state = point.post_fc_heat_authority_state ?? null;
    if (state === "recovering" && previousState !== "recovering") {
      cycleCount += 1;
      if (firstRecoveryChargeSeconds === null && finite(point.charge_elapsed_seconds)) {
        firstRecoveryChargeSeconds = point.charge_elapsed_seconds;
      }
      if (previousState === "gliding") glideToRecoveryRetriggerCount += 1;
    }

    const nextElapsed = points[index + 1]?.elapsed_seconds;
    if (finite(point.elapsed_seconds) && finite(nextElapsed) && nextElapsed >= point.elapsed_seconds) {
      const duration = nextElapsed - point.elapsed_seconds;
      if (state === "recovering") recoveringDurationSeconds += duration;
      if (state === "gliding") glidingDurationSeconds += duration;
    }
    previousState = state;
  }

  return {
    observedRecovery: cycleCount > 0,
    recoveryEnabled,
    cycleCount,
    firstRecoveryChargeSeconds,
    maxEffectiveHeatCeilingPercent,
    recoveringDurationSeconds,
    glidingDurationSeconds,
    glideToRecoveryRetriggerCount,
  };
}
