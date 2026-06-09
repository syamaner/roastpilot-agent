/**
 * The pure SSE → view-model reducer (E10 kickoff §6).
 *
 * Kept pure and separate from the React hook so the invariants are unit-testable
 * and structurally enforced:
 *
 *   PHASE COMES FROM THE SERVER ONLY. `phase` is set solely by the hydrate
 *   snapshot and `phase_changed` events. Telemetry frames update temperatures
 *   and heat/fan but DELIBERATELY never touch `phase` — there is no code path
 *   here that infers phase from a temperature or any client-side rule.
 *
 * The hook (`useRoastStream`) owns transport (EventSource, reconnect, liveness);
 * this module owns state transitions. All temperatures Celsius.
 */

import type {
  OperatorAction,
  PhaseChangedEventData,
  RoastDetail,
  RoastPhase,
  SseEvent,
  TelemetryEventData,
} from "@/lib/types";

/** The live view-model the dashboard renders from. */
export interface RoastStreamState {
  /** Server-authoritative phase. `null` until the first snapshot/phase event. */
  phase: RoastPhase | null;
  /** The latest per-tick telemetry, or `null` before the first frame. */
  telemetry: TelemetryEventData | null;
  /** Server-provided enabled operator actions (option (a)); `null` until known. */
  enabledActions: OperatorAction[] | null;
  /** Monotonic id of the last applied frame (for ordering/dedup). */
  lastEventId: number | null;
}

export const initialRoastStreamState: RoastStreamState = {
  phase: null,
  telemetry: null,
  enabledActions: null,
  lastEventId: null,
};

/**
 * Apply the hydrate snapshot from `GET /api/roasts/{id}`. Called on every
 * (re)connect BEFORE replaying live events, so a reconnect re-bases phase on
 * the server's truth rather than trusting stale client state.
 */
export function hydrate(
  state: RoastStreamState,
  snapshot: RoastDetail,
): RoastStreamState {
  return {
    ...state,
    phase: snapshot.agent_phase,
    enabledActions: snapshot.enabled_actions ?? state.enabledActions,
  };
}

/**
 * Apply one typed SSE frame. Returns the SAME state object when the frame is a
 * heartbeat/no-op or an out-of-order duplicate, so React can skip re-renders.
 */
export function applyEvent(
  state: RoastStreamState,
  event: SseEvent,
): RoastStreamState {
  // Drop frames we've already applied (the broadcaster stamps a monotonic id);
  // a snapshot hydrate after reconnect may re-deliver buffered frames.
  if (
    typeof event.id === "number" &&
    state.lastEventId !== null &&
    event.id <= state.lastEventId
  ) {
    return state;
  }

  const next = applyByType(state, event);
  if (next === state) return state;
  return typeof event.id === "number" ? { ...next, lastEventId: event.id } : next;
}

function applyByType(
  state: RoastStreamState,
  event: SseEvent,
): RoastStreamState {
  switch (event.event) {
    case "telemetry": {
      // Telemetry carries `agent_phase`, but we read it ONLY into the telemetry
      // record for display — it does NOT drive `state.phase`. Phase transitions
      // arrive exclusively via `phase_changed`. (Belt-and-braces: the server
      // sends a `phase_changed` whenever the phase moves.)
      const telemetry = event.data as unknown as TelemetryEventData;
      return { ...state, telemetry };
    }
    case "phase_changed": {
      const data = event.data as unknown as PhaseChangedEventData;
      return {
        ...state,
        phase: data.agent_phase,
        enabledActions: data.enabled_actions ?? state.enabledActions,
      };
    }
    case "heartbeat":
      // Liveness only — no state change. The hook tracks freshness separately.
      return state;
    default:
      // Other events (advisory, fault, recovery_required, command_*, markers…)
      // are consumed by page-level handlers off the raw event stream, not folded
      // into this shared view-model. The foundation surfaces them; pages route them.
      return state;
  }
}
