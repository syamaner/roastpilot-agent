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
