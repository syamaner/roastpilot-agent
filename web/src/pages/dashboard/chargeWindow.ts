/**
 * Charge-window derivation (#211).
 *
 * Whether the persistent charge cue should show, derived from server state: the
 * server phase is `preheating` AND the live bean temperature is within the
 * profile's charge band. This is a PRESENTATION derivation — phase still comes
 * solely from the server; we never infer phase from telemetry (invariant).
 *
 * Kept in its own module (not in `ChargeBanner.tsx`) so the component file only
 * exports a component (react-refresh) and the boolean is independently unit-
 * testable + reusable by `DashboardPage` (the dwell timer keys off it). All
 * temperatures Celsius.
 */

import { CHARGE_BAND_PHASE, type RoastPhase } from "@/lib/types";

/** The profile's charge band (Celsius), from the REST snapshot. */
export interface ChargeBand {
  minC: number;
  maxC: number;
}

/**
 * True only when the server says `preheating` AND the live bean temperature is
 * within (inclusive of) the profile's charge band. False when off-phase, when
 * the band hasn't hydrated, or when there's no finite bean reading.
 */
export function isInChargeWindow(
  phase: RoastPhase | null,
  beanTempC: number | null,
  chargeBand: ChargeBand | null,
): boolean {
  if (phase !== CHARGE_BAND_PHASE) return false;
  if (chargeBand === null) return false;
  if (beanTempC == null || !Number.isFinite(beanTempC)) return false;
  return beanTempC >= chargeBand.minC && beanTempC <= chargeBand.maxC;
}
