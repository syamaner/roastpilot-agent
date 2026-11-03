/** Runtime admission for the public D212/D213 fault-controls health projection. */

import type { FaultControls, RestartClearance } from "./types";

const TERMINAL_FAULT_ACTIONS = new Set<string>([
  "emergency_stop",
  "start_cooling",
  "stop_cooling",
  "drop_beans",
  "stop_cooling_and_acknowledge",
]);

/**
 * Validate untrusted health JSON before it decides a Start or terminal-control
 * view. This validates the projection only; it never establishes hardware state.
 */
export function isCoherentFaultControls(value: unknown): value is FaultControls {
  if (value === null || typeof value !== "object") return false;
  const candidate = value as Record<string, unknown>;
  const status = candidate.status;
  const generation = candidate.generation;
  const actions = candidate.enabled_actions;
  if (status !== "closed" && status !== "open" && status !== "unknown") return false;
  if (
    generation !== null &&
    (typeof generation !== "number" || !Number.isSafeInteger(generation) || generation < 0)
  ) {
    return false;
  }
  if (!Array.isArray(actions) || !actions.every((action) => typeof action === "string")) {
    return false;
  }
  if (!actions.every((action) => TERMINAL_FAULT_ACTIONS.has(action))) return false;
  if (new Set(actions).size !== actions.length) return false;
  // A closed lease normally advertises no terminal actions. The server can
  // retain its independently resolved software e-stop, however: it is the
  // sole CLOSED-state action which does not reopen routine controls.
  if (
    status === "closed" &&
    actions.length !== 0 &&
    !(actions.length === 1 && actions[0] === "emergency_stop")
  ) {
    return false;
  }
  if (status === "open" && generation === null) return false;
  return !(status === "unknown" && actions.some((action) => action !== "emergency_stop"));
}

/**
 * Validate the server restart-clearance projection before it controls a
 * terminal-control affordance. A cleared process gate is coherent even when a
 * separate server-side Start barrier leaves it ineligible.
 */
export function isCoherentRestartClearance(value: unknown): value is RestartClearance {
  if (value === null || typeof value !== "object") return false;
  const candidate = value as Record<string, unknown>;
  if (
    typeof candidate.eligible !== "boolean" ||
    typeof candidate.closure_only_available !== "boolean" ||
    !Array.isArray(candidate.enabled_actions) ||
    candidate.enabled_actions.some((action) => action !== "end_recovered_run") ||
    new Set(candidate.enabled_actions).size !== candidate.enabled_actions.length
  ) {
    return false;
  }
  switch (candidate.state) {
    case "required":
      return candidate.eligible === false && candidate.enabled_actions.length === 0;
    case "closure_only":
      return candidate.eligible === false && candidate.closure_only_available === false;
    case "unknown":
      return (
        candidate.eligible === false &&
        candidate.closure_only_available === false &&
        candidate.enabled_actions.length === 0
      );
    case "cleared":
      return candidate.closure_only_available === false && candidate.enabled_actions.length === 0;
    default:
      return false;
  }
}
