/**
 * Charge-cue derivation (#211).
 *
 * Whether — and HOW — the persistent charge cue should show, derived from server
 * state: the server phase is `preheating` and the live bean temperature relative
 * to the profile's charge band. This is a PRESENTATION derivation — phase still
 * comes solely from the server; we never infer phase from telemetry (invariant).
 *
 * The cue is tri-state so it never goes SILENT once the bean reaches the charge
 * temperature (the #211 failure mode). A profile can set a tight band (e.g.
 * 170–180 °C) while the server stays in `preheating` up to the ~200 °C pre-T0
 * safety bound; if the cue vanished above `maxC` the operator would be back to an
 * unannounced over-preheat. So:
 *   - below `minC` (or off-phase / unhydrated)  → `hidden`
 *   - `minC <= bean <= maxC`                     → `in_window`   (charge now)
 *   - `bean > maxC` (still preheating)           → `over_window` (escalated warning)
 *
 * Kept in its own module (not in `ChargeBanner.tsx`) so the component file only
 * exports a component (react-refresh) and the state is independently unit-testable
 * + reusable by `DashboardPage` (the dwell timer keys off it). All temps Celsius.
 */

import { CHARGE_BAND_PHASE, type RoastPhase } from "@/lib/types";

/** The profile's charge band (Celsius), from the REST snapshot. */
export interface ChargeBand {
  minC: number;
  maxC: number;
}

/**
 * The charge cue's display state:
 *  - `hidden`: not preheating, band not hydrated, no finite bean reading, or the
 *    bean is still below `minC` (not yet at charge temperature).
 *  - `in_window`: preheating and `minC <= bean <= maxC` — the "charge now" cue.
 *  - `over_window`: preheating and `bean > maxC` — escalated over-temperature
 *    warning (the cue must NOT disappear above the band, #211).
 */
export type ChargeCueState = "hidden" | "in_window" | "over_window";

/**
 * Derive the charge cue's display state from the server phase, the live bean
 * temperature, and the profile's charge band. `hidden` when off-phase, when the
 * band hasn't hydrated, when there's no finite bean reading, or below `minC`.
 */
export function chargeCueState(
  phase: RoastPhase | null,
  beanTempC: number | null,
  chargeBand: ChargeBand | null,
): ChargeCueState {
  if (phase !== CHARGE_BAND_PHASE) return "hidden";
  if (chargeBand === null) return "hidden";
  if (beanTempC == null || !Number.isFinite(beanTempC)) return "hidden";
  if (beanTempC < chargeBand.minC) return "hidden";
  return beanTempC <= chargeBand.maxC ? "in_window" : "over_window";
}
