/**
 * Fault banner + safety event trail (ui-prompts Prompt B §2, kickoff §2).
 *
 * Full-width banner shown when the roast has FAULTED. It states what the safety
 * layer did (the fault `reason`), the current forced-safe state (heat forced to
 * 0 %, fan held at a safe level — the fail-closed posture), and the accumulated
 * safety event trail (what the safety layer did and when). Serious but not
 * alarmist — an operator console.
 *
 * INFORMATIONAL + PERSISTENT — the banner must not let the operator hide an ACTIVE
 * fault: it stays on screen until the operator explicitly acknowledges it. There is
 * no client-side dismiss.
 *
 * Post-#206 a fault no longer auto-finalises the run: the faulted run stays operable
 * (loop alive, heat forced to 0) so the operator can still engage/stop cooling on a
 * physically-running machine — that genuine cooling/e-stop need in `faulted` is
 * served by the OperatorActionBar (START/STOP COOLING + the always-enabled, correctly
 * labelled EMERGENCY STOP, all surfaced from the server's `enabled_actions`).
 *
 * The banner's only affordance is an optional acknowledge action — the real next
 * step once the operator is done. It dispatches the genuine `acknowledge_fault`
 * control action (#117/#206), which finalises the run (outcome `faulted`) server-side
 * and clears `active_run_id`; the page then drops its sticky-faulted pin and re-fetches
 * health → the idle Start form (#124). `acknowledge_fault` issues NO roaster command
 * (heat is already off in `faulted`), so the button label is honest. The page renders
 * the affordance only when the server's `enabled_actions` mirror enables
 * `acknowledge_fault` (the `faulted` phase) — render-from-server, no client-side
 * command matrix (#117, D25); when omitted the banner shows no affordance.
 *
 * Driven by the real `fault` + `safety_alert` SafetyEvaluation payloads (the page
 * accumulates the trail). All temperatures Celsius.
 */

import type { ReactNode } from "react";

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
  /** Optional acknowledge affordance. Dispatches the `acknowledge_fault` control
   *  action (#117/#206) — finalising the operable-faulted run server-side — then
   *  clears the sticky-faulted pin + re-fetches health → idle form (#124). Issues
   *  no roaster command (heat is already off in faulted). The page passes it only
   *  when the server's `enabled_actions` mirror enables `acknowledge_fault`;
   *  omit/undefined renders no affordance. */
  startNewRoast?: ReactNode;
  className?: string;
}

export function FaultBanner({
  fault,
  trail,
  startNewRoast,
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
        {startNewRoast && <div className="shrink-0">{startNewRoast}</div>}
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
