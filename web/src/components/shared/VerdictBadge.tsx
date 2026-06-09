/**
 * Advisory verdict badge (D15 / E10 kickoff §3).
 *
 * Renders ALLOW / CLAMP / REJECT only. For RECOVERY / FAULT / EMERGENCY_STOP
 * the verdict helper returns `null` and this component renders nothing — those
 * states are a modal / banner / phase change, not an advisory badge.
 */

import { cn } from "@/lib/cn";
import type { SafetyVerdict } from "@/lib/types";
import { verdictBadge, type VerdictTone } from "@/lib/verdict";

const TONE_CLASS: Record<VerdictTone, string> = {
  nominal: "border-roast-nominal/40 bg-roast-nominal/15 text-roast-nominal",
  caution: "border-roast-caution/40 bg-roast-caution/15 text-roast-caution",
  fault: "border-roast-fault/40 bg-roast-fault/15 text-roast-fault",
};

export interface VerdictBadgeProps {
  verdict: SafetyVerdict;
  className?: string;
}

export function VerdictBadge({ verdict, className }: VerdictBadgeProps): React.JSX.Element | null {
  const spec = verdictBadge(verdict);
  if (spec === null) return null;
  return (
    <span
      data-testid="verdict-badge"
      data-verdict={verdict}
      data-tone={spec.tone}
      className={cn(
        "inline-flex items-center rounded-md border px-2 py-0.5 text-xs font-semibold uppercase tracking-wide",
        TONE_CLASS[spec.tone],
        className,
      )}
    >
      {spec.label}
    </span>
  );
}
