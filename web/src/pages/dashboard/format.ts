/**
 * Display formatters + the phase→token map for the live dashboard.
 *
 * All numeric displays use tabular figures (the `.numeric` utility / `tabular-nums`)
 * so digits don't jitter as values tick — readable from 1 m at the roaster
 * (kickoff §1). All temperatures are Celsius.
 */

import type { RoastPhase } from "@/lib/types";

/** `mm:ss` from a (possibly null) seconds value; `--:--` when unknown. */
export function formatClock(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return "--:--";
  const whole = Math.floor(seconds);
  const mm = Math.floor(whole / 60);
  const ss = whole % 60;
  return `${String(mm).padStart(2, "0")}:${String(ss).padStart(2, "0")}`;
}

/** `123.4 °C` from a (possibly null) Celsius value; `— °C` when unknown. */
export function formatTempC(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "— °C";
  return `${value.toFixed(1)} °C`;
}

/** `65 %` from a (possibly null) percent; `— %` when unknown. Whole-number %. */
export function formatPercent(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "— %";
  return `${Math.round(value)} %`;
}

/** `18.5 %` from a (possibly null) percent; `— %` when unknown. One decimal —
 *  the precision DTR needs (15.x% vs 20% is a real roasting difference), matching
 *  the detail page's `Development` stat (#220). */
export function formatPercent1(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "— %";
  return `${value.toFixed(1)} %`;
}

/** `8.2 °C/min` from a (possibly null) rate; `— °C/min` when unknown. */
export function formatRoR(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "— °C/min";
  return `${value.toFixed(1)} °C/min`;
}

/** `0.82` confidence from a 0–1 value; `—` when unknown. */
export function formatConfidence(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return value.toFixed(2);
}

/** `29.7 °C` from a (possibly null) ambient Celsius reading; `— °C` when
 *  unknown (#464 — the live "Conditions" readout, mirroring `formatTempC`'s
 *  precision). Kept distinct from the bean-probe formatter so it stays
 *  self-contained (this is corpus/context, never the bean-probe signal). */
export function formatAmbientTempC(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "— °C";
  return `${value.toFixed(1)} °C`;
}

/** `41 % RH` from a (possibly null) relative-humidity percent; `— % RH` when
 *  unknown (#464). Whole-number %, "RH" suffix disambiguates from DTR/heat%. */
export function formatAmbientHumidity(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "— % RH";
  return `${Math.round(value)} % RH`;
}

/** `1008 hPa` from a (possibly null) barometric-pressure reading; `— hPa` when
 *  unknown (#464). Whole-number hectopascals. */
export function formatAmbientPressure(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "— hPa";
  return `${Math.round(value)} hPa`;
}

/** Human label for each phase — the header badge text (operator-facing truth). */
export const PHASE_LABEL: Record<RoastPhase, string> = {
  idle: "IDLE",
  starting: "STARTING",
  preheating: "PREHEATING",
  roasting_pre_first_crack: "ROASTING",
  development: "DEVELOPMENT",
  cooling: "COOLING",
  complete: "COMPLETE",
  faulted: "FAULT",
  operator_recovery_required: "RECOVERY REQUIRED",
};

/**
 * The roast token CSS var the phase badge tints with. The phase accent shifts as
 * the roast progresses (ui-prompts shared block); fault/recovery use the fault
 * token, terminal/idle stay neutral. Returns a `var(--roast-*)` string or `null`
 * for the neutral (muted) styling.
 */
export function phaseAccentVar(phase: RoastPhase | null): string | null {
  switch (phase) {
    case "preheating":
      return "var(--roast-phase-preheat)";
    case "roasting_pre_first_crack":
      return "var(--roast-phase-roasting)";
    case "development":
      return "var(--roast-phase-development)";
    case "cooling":
      return "var(--roast-phase-cooling)";
    case "faulted":
    case "operator_recovery_required":
      return "var(--roast-fault)";
    case "idle":
    case "starting":
    case "complete":
    case null:
      return null;
  }
}

/**
 * Server roast phases BEFORE first crack, where the controller drives heat/fan
 * deterministically off the bean profile (D59) and re-asserts them every tick, so
 * the dashboard renders heat/fan as READ-OUTS, not dials (#318). Membership is a
 * pure projection of the SERVER-provided phase — never inferred from telemetry.
 */
const PRE_FIRST_CRACK_PHASES: ReadonlySet<RoastPhase> = new Set<RoastPhase>([
  "preheating",
  "roasting_pre_first_crack",
]);

/** True iff the server phase is a pre-first-crack phase (read-out presentation). */
export function isPreFirstCrackPhase(phase: RoastPhase | null): boolean {
  return phase !== null && PRE_FIRST_CRACK_PHASES.has(phase);
}
