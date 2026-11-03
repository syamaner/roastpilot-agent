/** D216 recovered-run closure affordance projection helpers. */

import type { OperatorAction, RestartClearance } from "./types";

/**
 * Accept End only when both current server projections agree.
 *
 * This does not derive eligibility: it merely intersects the coherent health
 * projection with the current successfully-fetched run detail. D216's End
 * action is intentionally not carried by the phase-change SSE mirror, unlike
 * ordinary dashboard controls. A stale health read cannot retain it.
 */
export function isRecoveredRunEndProjected(
  restartClearance: RestartClearance | null,
  currentRunDetailActions: readonly OperatorAction[] | null,
  healthStale: boolean,
): boolean {
  return (
    !healthStale &&
    restartClearance?.enabled_actions.includes("end_recovered_run") === true &&
    currentRunDetailActions?.includes("end_recovered_run") === true
  );
}
