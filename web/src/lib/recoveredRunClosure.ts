/** D216 recovered-run closure affordance projection helpers. */

import type { OperatorAction, RestartClearance } from "./types";

/**
 * Accept End only when both current server projections agree.
 *
 * This does not derive eligibility: it merely intersects the coherent health
 * projection with the current run's effective server action mirror. A stale
 * health read cannot retain the affordance.
 */
export function isRecoveredRunEndProjected(
  restartClearance: RestartClearance | null,
  effectiveEnabledActions: readonly OperatorAction[] | null,
  healthStale: boolean,
): boolean {
  return (
    !healthStale &&
    restartClearance?.enabled_actions.includes("end_recovered_run") === true &&
    effectiveEnabledActions?.includes("end_recovered_run") === true
  );
}
