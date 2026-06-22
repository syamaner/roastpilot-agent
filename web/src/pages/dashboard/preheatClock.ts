/**
 * Preheat-clock freeze (#330).
 *
 * The pre-charge "Preheat" read-out (#316) renders the server's run-referenced
 * `elapsed_seconds`. The controller keeps emitting telemetry frames after the
 * run latches into a terminal phase (fault / emergency-stop → faulted, recovery,
 * cooling, complete), and the run clock (`roast_elapsed_seconds`) keeps advancing
 * on those frames — so a fault/e-stop DURING preheat leaves the Preheat clock
 * climbing, misrepresenting a stopped run (roast 3, 21 Jun).
 *
 * This is the client-side analogue of the drop-freeze the server already applies
 * to the charge clock + development time/DTR (#239/#261): once the SERVER reports
 * a terminal phase, the read-out HOLDS the last live elapsed value rather than
 * tracking the still-advancing server value. The freeze is driven by the server
 * phase (read, never inferred) and by server-provided elapsed values — the SPA
 * still renders from server state. Phase is NOT inferred from telemetry here; the
 * caller passes the server-authoritative `phase`.
 */

import { useEffect, useRef } from "react";

import type { RoastPhase } from "@/lib/types";

/**
 * Phases in which the run clock must FREEZE — the terminal / faulted / e-stopped
 * / post-drop holds. A fault or emergency stop lands the run in `faulted` (or
 * `operator_recovery_required` on a restart-with-active-run); `cooling`/`complete`
 * are post-drop, where the server already freezes the charge clock. Holding the
 * Preheat read-out across all of them keeps it consistent with the server's drop
 * freeze and never lets a stopped run keep counting up.
 *
 * Driven by the server phase only — this is a presentation test, NOT phase
 * inference (the value comes from the SSE `phase_changed` / hydrate snapshot).
 */
const FROZEN_PHASES: ReadonlySet<RoastPhase> = new Set<RoastPhase>([
  "faulted",
  "operator_recovery_required",
  "cooling",
  "complete",
]);

/** Whether the run clock should be frozen for this server phase. */
export function isClockFrozen(phase: RoastPhase | null): boolean {
  return phase !== null && FROZEN_PHASES.has(phase);
}

/**
 * The elapsed value to DISPLAY, frozen at the last live value once the server
 * phase is terminal.
 *
 * While the phase is live (not frozen) it tracks `elapsedSeconds` directly and
 * remembers it. Once the phase becomes terminal it returns the last remembered
 * live value, ignoring any further server advance — so a fault/e-stop during
 * preheat holds the Preheat read-out at the last live tick rather than climbing.
 *
 * @param elapsedSeconds Server run-referenced elapsed (`telemetry.elapsed_seconds`).
 * @param phase Server-authoritative phase (never inferred client-side).
 * @returns The elapsed value to render (held while frozen).
 */
export function useFrozenElapsed(
  elapsedSeconds: number | null,
  phase: RoastPhase | null,
): number | null {
  // The last elapsed value seen while the phase was LIVE — the value we hold once
  // the server reports a terminal phase. A ref (not state) because updating it on a
  // live frame must not itself trigger a render; the parent already re-renders on
  // every telemetry frame.
  const lastLiveRef = useRef<number | null>(null);
  const frozen = isClockFrozen(phase);

  useEffect(() => {
    // Capture only the last NON-NULL live value. Once frozen we stop updating, so
    // the held value is the last live tick. The `!== null` guard matters because
    // `telemetry.elapsed_seconds` is nullable: a transient null frame (around
    // hydrate / reconnect) must NOT overwrite the last good reading, else the
    // freeze would land on null and display "--:--" instead of the real elapsed.
    // A new run re-mounts the dashboard live view (runId-keyed reset upstream), so
    // the ref starts fresh — no stale carry-over across runs.
    if (!frozen && elapsedSeconds !== null) {
      lastLiveRef.current = elapsedSeconds;
    }
  }, [frozen, elapsedSeconds]);

  if (frozen) {
    // Hold the last live value. If the phase was already terminal on first paint
    // (e.g. a device joining a faulted run mid-fault) we have no captured live
    // value, so fall back to the server value — the best available, and it is no
    // longer advancing in practice (the run is stopped).
    return lastLiveRef.current ?? elapsedSeconds;
  }
  return elapsedSeconds;
}
