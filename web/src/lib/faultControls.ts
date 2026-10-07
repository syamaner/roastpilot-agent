/** Runtime admission for the public D212/D213 fault-controls health projection. */

import type { FaultControls } from "./types";

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
  if (status === "closed" && actions.length !== 0) return false;
  if (status === "open" && generation === null) return false;
  return !(status === "unknown" && actions.some((action) => action !== "emergency_stop"));
}
