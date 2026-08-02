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

/** Whether a cooling row can close an interval without crossing a restart. */
function sameProcessCoolingBoundary(
  currentElapsed: number | null,
  nextElapsed: number | null,
  currentCharge: number,
  nextCharge: number,
): boolean {
  if (!finite(currentElapsed) || !finite(nextElapsed)) return false;
  const serveDelta = nextElapsed - currentElapsed;
  const chargeDelta = nextCharge - currentCharge;
  // Charge time may advance less because it freezes at drop. It cannot advance
  // more than the process-local serve clock unless the charge clock was restored
  // across a restart.
  return serveDelta >= 0 && chargeDelta >= 0 && chargeDelta <= serveDelta + 0.1;
}

/** Summarize only fields persisted by schema v16; no state is inferred from RoR.
 *  Durations include only intervals closed by a later server snapshot. */
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
      (sameProcessInterval(
          point.elapsed_seconds,
          nextPoint.elapsed_seconds,
          point.charge_elapsed_seconds,
          nextChargeElapsed,
        ) ||
        (nextPoint.agent_phase === "cooling" &&
          sameProcessCoolingBoundary(
            point.elapsed_seconds,
            nextPoint.elapsed_seconds,
            point.charge_elapsed_seconds,
            nextChargeElapsed,
          )))
    ) {
      // The charge clock freezes at drop, so it may advance less than the serve
      // clock at a COOLING boundary. Otherwise both clocks must advance together.
      // Both forms exclude a restart, where restored charge time can jump ahead
      // of the new process-local serve clock.
      const duration = nextChargeElapsed - point.charge_elapsed_seconds;
      // A non-DEVELOPMENT row may retain the output accepted earlier in its
      // transition tick as historical evidence. It does not own live authority
      // after the phase exit, so it must never accrue the following interval.
      if (point.agent_phase === "development") {
        if (state === "recovering") recoveringDurationSeconds += duration;
        if (state === "gliding") glidingDurationSeconds += duration;
      }
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
