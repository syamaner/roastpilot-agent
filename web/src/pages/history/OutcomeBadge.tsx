/**
 * Roast-outcome badge for the history table (E10-S4).
 *
 * Maps the three `RoastOutcome` values to the safety/verdict tokens
 * (nominal/caution/fault) — the same visual language the shared `VerdictBadge`
 * uses — and renders a distinct "IN PROGRESS" state for a run with no outcome
 * yet (the contract allows `outcome: null`). Copy follows the enum, uppercased.
 */

import { cn } from "@/lib/cn";
import type { RoastOutcome } from "@/lib/types";

type Tone = "nominal" | "caution" | "fault" | "neutral";

const TONE_CLASS: Record<Tone, string> = {
  nominal: "border-roast-nominal/40 bg-roast-nominal/15 text-roast-nominal",
  caution: "border-roast-caution/40 bg-roast-caution/15 text-roast-caution",
  fault: "border-roast-fault/40 bg-roast-fault/15 text-roast-fault",
  neutral: "border-border bg-muted/40 text-muted-foreground",
};

const OUTCOME_SPEC: Record<RoastOutcome, { label: string; tone: Tone }> = {
  completed: { label: "COMPLETED", tone: "nominal" },
  aborted: { label: "ABORTED", tone: "caution" },
  faulted: { label: "FAULTED", tone: "fault" },
};

export interface OutcomeBadgeProps {
  /** `null` renders the in-progress (no terminal outcome) state. */
  outcome: RoastOutcome | null;
  className?: string;
}

export function OutcomeBadge({ outcome, className }: OutcomeBadgeProps): React.JSX.Element {
  const spec = outcome === null ? { label: "IN PROGRESS", tone: "neutral" as Tone } : OUTCOME_SPEC[outcome];
  return (
    <span
      data-testid="outcome-badge"
      data-outcome={outcome ?? "in_progress"}
      data-tone={spec.tone}
      className={cn(
        "inline-flex items-center rounded-md border px-2 py-0.5 font-mono text-xs uppercase tracking-wide",
        TONE_CLASS[spec.tone],
        className,
      )}
    >
      {spec.label}
    </span>
  );
}
