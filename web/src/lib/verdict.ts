/**
 * D15 verdict rendering (E10 kickoff §3).
 *
 * Six safety verdicts, three advisory badges. ALLOW / CLAMP / REJECT are the
 * outcomes of advisor recommendations and render as advisory-panel badges. The
 * other three are NOT badges:
 *   - RECOVERY        → the `recovery_required` event → RecoveryModal
 *   - FAULT / E-STOP  → FaultBanner + a phase change
 * so this helper returns `null` for them. UI copy follows the enum: `ALLOW`,
 * never the prototype's `ACCEPT`.
 *
 * The decision-trace table (detail page) shows all six in its verdict column —
 * it renders history, not advisory state — and uses `verdictLabel` directly.
 */

import type { SafetyVerdict } from "./types";

/** Visual intent for a badge — mapped to roast tokens by the consuming component. */
export type VerdictTone = "nominal" | "caution" | "fault";

export interface VerdictBadgeSpec {
  /** Uppercase enum label, e.g. "ALLOW" (never "ACCEPT"). */
  label: string;
  tone: VerdictTone;
}

/** Uppercased label for any verdict (used by the decision-trace column). */
export function verdictLabel(verdict: SafetyVerdict): string {
  return verdict === "emergency_stop"
    ? "EMERGENCY STOP"
    : verdict.toUpperCase();
}

/**
 * The advisory badge spec for a verdict, or `null` when the verdict is not an
 * advisory badge (RECOVERY / FAULT / EMERGENCY_STOP — see module docstring).
 */
export function verdictBadge(verdict: SafetyVerdict): VerdictBadgeSpec | null {
  switch (verdict) {
    case "allow":
      return { label: "ALLOW", tone: "nominal" };
    case "clamp":
      return { label: "CLAMP", tone: "caution" };
    case "reject":
      return { label: "REJECT", tone: "fault" };
    case "recovery":
    case "fault":
    case "emergency_stop":
      return null;
  }
}
