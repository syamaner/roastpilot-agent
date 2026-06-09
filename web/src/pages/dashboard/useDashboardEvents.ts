/**
 * Page-local accumulator for the live-dashboard view-model.
 *
 * The shared `useRoastStream` reducer folds ONLY phase / telemetry /
 * enabledActions and surfaces every other frame via `lastEvent` (the designed
 * seam). The dashboard folds those remaining frames here: the advisory feed
 * (latest + recent history), the charge-guidance toast, the recovery handshake,
 * the fault handshake + safety event trail, the event markers, and the live curve
 * point buffer.
 *
 * The reducer is pure and exported so its folding is unit-testable. It NEVER sets
 * phase — phase is the server's truth, owned by the shared reducer (invariant).
 * All temperatures Celsius.
 */

import { useEffect, useReducer } from "react";

import type { CurveMarker, CurvePoint } from "@/components/shared/LiveCurve/types";
import type { SseEvent, TelemetryEventData } from "@/lib/types";
import type {
  AdvisoryEventData,
  ChargeGuidanceData,
  FirstCrackData,
  SafetyEvaluationData,
  T0DetectedData,
} from "./events";

/** One advisory recommendation rendered in the panel / decision history. */
export interface AdvisoryRecord {
  /** The advisor's recommended targets + rationale (absent on gate/skip frames). */
  decision: AdvisoryEventData["decision"];
  /** The safety handshake on this advice (absent on skip/pause frames). */
  evaluation: SafetyEvaluationData | undefined;
  /** True for the synthesized replay CLAMP key frame (never live-evaluated). */
  synthesized: boolean;
}

/** One row in the safety event trail (FaultBanner). */
export interface SafetyTrailEntry {
  kind: "safety_alert" | "fault" | "recovery_required";
  evaluation: SafetyEvaluationData;
}

export interface DashboardViewModel {
  /** The most recent advisory carrying a decision or verdict (drives the panel). */
  latestAdvisory: AdvisoryRecord | null;
  /** Recent advisories, newest first, capped (the decision-history list). */
  advisoryHistory: AdvisoryRecord[];
  /** Whether the advisor is paused (from the pause/resume toggle frames). */
  advisoryPaused: boolean;
  /** The latest charge-guidance payload (the add-beans toast); null until fired. */
  chargeGuidance: ChargeGuidanceData | null;
  /** The recovery handshake (drives the RecoveryModal); null unless in recovery. */
  recovery: SafetyEvaluationData | null;
  /** The fault handshake (drives the FaultBanner); null unless faulted. */
  fault: SafetyEvaluationData | null;
  /** Accumulated safety events for the fault trail, in arrival order. */
  safetyTrail: SafetyTrailEntry[];
  /** First crack detection (header FC status); null until detected. */
  firstCrack: FirstCrackData | null;
  /** T0 detection; null until detected. */
  t0: T0DetectedData | null;
  /** Event markers for the curve (T0 / first crack / drop), x in seconds. */
  markers: CurveMarker[];
  /** Appended live curve points (one per telemetry frame), x = elapsed seconds. */
  points: CurvePoint[];
}

export const initialDashboardViewModel: DashboardViewModel = {
  latestAdvisory: null,
  advisoryHistory: [],
  advisoryPaused: false,
  chargeGuidance: null,
  recovery: null,
  fault: null,
  safetyTrail: [],
  firstCrack: null,
  t0: null,
  markers: [],
  points: [],
};

/** Decision-history depth shown in the advisory panel (ui-prompts Prompt A: "last 4"). */
export const ADVISORY_HISTORY_LIMIT = 4;

type Action =
  | { kind: "event"; event: SseEvent }
  | { kind: "reset" };

/** Pull the elapsed-seconds x for a telemetry frame; null frames don't plot. */
function pointFromTelemetry(t: TelemetryEventData): CurvePoint | null {
  if (t.elapsed_seconds == null) return null;
  return {
    t: t.elapsed_seconds,
    bean: t.bean_temp_c,
    env: t.env_temp_c,
    ror: t.bean_ror_c_per_min,
    heat: t.heat_percent,
    fan: t.fan_percent,
  };
}

/** Append a marker iff one of that kind isn't already present (markers fire once). */
function withMarker(markers: CurveMarker[], marker: CurveMarker): CurveMarker[] {
  if (markers.some((m) => m.kind === marker.kind)) return markers;
  return [...markers, marker];
}

export function dashboardReducer(
  state: DashboardViewModel,
  action: Action,
): DashboardViewModel {
  if (action.kind === "reset") return initialDashboardViewModel;

  const event = action.event;
  switch (event.event) {
    case "telemetry": {
      const point = pointFromTelemetry(event.data as unknown as TelemetryEventData);
      if (point === null) return state;
      return { ...state, points: [...state.points, point] };
    }
    case "advisory": {
      const data = event.data as unknown as AdvisoryEventData;
      // Pause/resume toggles carry only `advisory_paused` — fold the flag, but
      // they are NOT advisory recommendations, so they never enter the feed.
      if (typeof data.advisory_paused === "boolean") {
        return { ...state, advisoryPaused: data.advisory_paused };
      }
      // A skipped record (no decision, no evaluation) is trace-only — ignore for
      // the panel; the connection/phase already convey the live state.
      if (data.decision === undefined && data.evaluation === undefined) {
        return state;
      }
      const record: AdvisoryRecord = {
        decision: data.decision,
        evaluation: data.evaluation,
        synthesized: data.synthesized === true,
      };
      return {
        ...state,
        latestAdvisory: record,
        advisoryHistory: [record, ...state.advisoryHistory].slice(0, ADVISORY_HISTORY_LIMIT),
      };
    }
    case "charge_guidance":
      return { ...state, chargeGuidance: event.data as unknown as ChargeGuidanceData };
    case "recovery_required": {
      const evaluation = event.data as unknown as SafetyEvaluationData;
      return {
        ...state,
        recovery: evaluation,
        safetyTrail: [...state.safetyTrail, { kind: "recovery_required", evaluation }],
      };
    }
    case "recovery_acknowledged":
      // The operator acknowledged; clear the modal trigger (phase moves via the
      // server's phase_changed, which the shared reducer owns).
      return { ...state, recovery: null };
    case "fault": {
      const evaluation = event.data as unknown as SafetyEvaluationData;
      return {
        ...state,
        fault: evaluation,
        safetyTrail: [...state.safetyTrail, { kind: "fault", evaluation }],
      };
    }
    case "safety_alert": {
      const evaluation = event.data as unknown as SafetyEvaluationData;
      return {
        ...state,
        safetyTrail: [...state.safetyTrail, { kind: "safety_alert", evaluation }],
      };
    }
    case "t0_detected": {
      const data = event.data as unknown as T0DetectedData;
      return {
        ...state,
        t0: data,
        markers: withMarker(state.markers, { kind: "t0", t: 0, label: "T0" }),
      };
    }
    case "first_crack": {
      const data = event.data as unknown as FirstCrackData;
      // The FC marker's x is the elapsed time at detection — the latest plotted
      // point's t (the curve x-axis is elapsed seconds since T0).
      const at = state.points.length > 0 ? state.points[state.points.length - 1].t : 0;
      return {
        ...state,
        firstCrack: data,
        markers: withMarker(state.markers, { kind: "first_crack", t: at, label: "FIRST CRACK" }),
      };
    }
    case "command_executed": {
      // A drop command moves the roast to cooling; mark the drop point so the
      // curve shows it. Other executed commands (set_targets) need no marker.
      const data = event.data as { command?: string } | undefined;
      if (data?.command !== "drop_beans") return state;
      const at = state.points.length > 0 ? state.points[state.points.length - 1].t : 0;
      return {
        ...state,
        markers: withMarker(state.markers, { kind: "drop", t: at, label: "DROP" }),
      };
    }
    default:
      // run_started / command_failed / logs_exported / run_completed / heartbeat
      // / phase_changed — not folded here (phase is the shared reducer's; the
      // rest aren't dashboard view-model state).
      return state;
  }
}

/**
 * Fold the shared hook's `lastEvent` stream into the dashboard view-model.
 *
 * Driven by the raw frame the shared hook surfaces — we re-dispatch each new
 * `lastEvent` exactly once (keyed by frame identity, since the hook replaces the
 * object on every applied frame). On a run change (runId), the caller resets.
 */
export function useDashboardEvents(lastEvent: SseEvent | null): DashboardViewModel {
  const [state, dispatch] = useReducer(dashboardReducer, initialDashboardViewModel);

  useEffect(() => {
    if (lastEvent === null) return;
    dispatch({ kind: "event", event: lastEvent });
  }, [lastEvent]);

  return state;
}
