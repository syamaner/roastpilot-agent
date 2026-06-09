/**
 * Fault banner + safety event trail (ui-prompts Prompt B §2, kickoff §2).
 *
 * Full-width banner shown when the roast has FAULTED. It states what the safety
 * layer did (the fault `reason`), the current forced-safe state (heat forced to
 * 0 %, fan held at a safe level — the fail-closed posture), the accumulated
 * safety event trail (what the safety layer did and when), and the single
 * ACKNOWLEDGE FAULT action. Serious but not alarmist — an operator console.
 *
 * Driven by the real `fault` + `safety_alert` SafetyEvaluation payloads (the page
 * accumulates the trail). Acknowledge POSTs through the action bar's handler;
 * `acknowledge_recovery` is the recovery flow — a faulted run is acknowledged via
 * the emergency-stop / explicit operator path the server exposes, so the
 * Acknowledge button dispatches the action the caller wires (kept a prop so this
 * component never hardcodes which server action clears a fault).
 */

import { cn } from "@/lib/cn";
import { verdictLabel } from "@/lib/verdict";
import { formatPercent } from "./format";
import type { SafetyEvaluationData } from "./events";
import type { SafetyTrailEntry } from "./useDashboardEvents";

export interface FaultBannerProps {
  /** The fault handshake (drives the banner). When null, renders nothing. */
  fault: SafetyEvaluationData | null;
  /** The accumulated safety event trail (newest entries appended). */
  trail: SafetyTrailEntry[];
  /** Acknowledge the fault (the page wires this to the server action). */
  onAcknowledge: () => void;
  /** Whether acknowledge is currently permitted (mirrors server enablement). */
  canAcknowledge: boolean;
  className?: string;
}

export function FaultBanner({
  fault,
  trail,
  onAcknowledge,
  canAcknowledge,
  className,
}: FaultBannerProps): React.JSX.Element | null {
  if (fault === null) return null;
  return (
    <section
      data-testid="fault-banner"
      role="alert"
      className={cn(
        "flex flex-col gap-4 rounded-lg border-2 border-roast-fault bg-roast-fault/10 p-5",
        className,
      )}
    >
      <div className="flex items-start justify-between gap-4">
        <div className="flex flex-col gap-1">
          <h2 className="text-lg font-bold uppercase tracking-wide text-roast-fault">
            Fault
          </h2>
          <p data-testid="fault-reason" className="text-sm">
            {fault.reason}
          </p>
        </div>
        <button
          type="button"
          data-testid="acknowledge-fault"
          disabled={!canAcknowledge}
          aria-disabled={!canAcknowledge}
          onClick={onAcknowledge}
          className={cn(
            "shrink-0 rounded-md border px-4 py-2 text-sm font-semibold uppercase tracking-wide transition-colors",
            canAcknowledge
              ? "border-roast-fault/60 bg-roast-fault/15 text-roast-fault hover:bg-roast-fault/25"
              : "cursor-not-allowed border-border/40 text-muted-foreground/50",
          )}
        >
          Acknowledge Fault
        </button>
      </div>

      {/* Current forced-safe state — the fail-closed posture (heat 0, fan safe). */}
      <dl className="grid grid-cols-2 gap-x-6 gap-y-1 text-sm sm:grid-cols-3">
        <SafeStat label="Heat" value={`${formatPercent(fault.adjusted_heat ?? 0)} (forced)`} />
        <SafeStat
          label="Fan"
          value={fault.adjusted_fan != null ? `${formatPercent(fault.adjusted_fan)} (safe)` : "held"}
        />
        <SafeStat label="Rule" value={fault.rule} />
      </dl>

      {trail.length > 0 && (
        <div className="flex flex-col gap-2">
          <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Safety System Event Trail
          </h3>
          <ul data-testid="safety-trail" className="flex flex-col gap-1">
            {trail.map((entry, i) => (
              <li
                key={i}
                data-testid="safety-trail-row"
                className="flex items-center justify-between gap-3 rounded border border-roast-fault/30 px-3 py-1.5 text-sm"
              >
                <span className="truncate">{entry.evaluation.reason}</span>
                <span className="numeric shrink-0 text-xs uppercase tracking-wide text-roast-fault">
                  {verdictLabel(entry.evaluation.verdict)}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

function SafeStat({ label, value }: { label: string; value: string }): React.JSX.Element {
  return (
    <div className="flex flex-col">
      <dt className="text-xs uppercase tracking-wide text-muted-foreground">{label}</dt>
      <dd className="numeric font-semibold">{value}</dd>
    </div>
  );
}
