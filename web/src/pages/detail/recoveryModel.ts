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

/** Whether two snapshots share the same process-local serve-clock origin. */
function sameProcessInterval(
  currentElapsed: number | null,
  nextElapsed: number | null,
  currentCharge: number,
  nextCharge: number,
): boolean {
  if (!finite(currentElapsed) || !finite(nextElapsed)) return false;
  const serveDelta = nextElapsed - currentElapsed;
  const chargeDelta = nextCharge - currentCharge;
  return (
    serveDelta >= 0 &&
    chargeDelta >= 0 &&
    Math.abs(serveDelta - chargeDelta) <= 0.1
  );
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

    const nextPoint = points[index + 1];
    const nextChargeElapsed = nextPoint?.charge_elapsed_seconds;
    if (
      finite(point.charge_elapsed_seconds) &&
      finite(nextChargeElapsed) &&
      nextChargeElapsed >= point.charge_elapsed_seconds &&
      (nextPoint.agent_phase === "cooling" ||
        sameProcessInterval(
          point.elapsed_seconds,
          nextPoint.elapsed_seconds,
          point.charge_elapsed_seconds,
          nextChargeElapsed,
        ))
    ) {
      // The charge clock freezes at drop, so the final DEVELOPMENT interval
      // may end at a COOLING row. Otherwise both clocks must advance together:
      // that closes an in-process fault/recovery boundary while excluding an
      // agent restart, whose serve clock resets as its charge clock is restored.
      const duration = nextChargeElapsed - point.charge_elapsed_seconds;
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
