/**
 * Page-local accumulator for the live-dashboard view-model.
 *
 * The shared `useRoastStream` reducer folds ONLY phase / telemetry /
 * enabledActions and surfaces every other frame via `lastEvent` (the designed
 * seam). The dashboard folds those remaining frames here: the advisory feed
 * (latest + recent history), the charge-guidance frame (trace-only since #211 —
 * the live add-beans cue is the derived `ChargeBanner`), the recovery handshake,
 * the fault handshake + safety event trail, the event markers, and the live curve
 * point buffer.
 *
 * The reducer is pure and exported so its folding is unit-testable. It NEVER sets
 * phase — phase is the server's truth, owned by the shared reducer (invariant).
 * All temperatures Celsius.
 */

import { useCallback, useEffect, useReducer, useRef } from "react";

import type { CurveMarker, CurvePoint } from "@/components/shared/LiveCurve/types";
import { useFrameDrain, type ConnectionStatus } from "@/hooks/useRoastStream";
import { api } from "@/lib/api";
import type { SseEvent, TelemetryEventData, TelemetryPoint } from "@/lib/types";
import type {
  AdvisoryEventData,
  ChargeGuidanceData,
  FirstCrackData,
  SafetyEvaluationData,
  T0DetectedData,
} from "./events";

/** One advisory recommendation rendered in the panel / decision history. */
export interface AdvisoryRecord {
  /** Monotonic per-run sequence — a stable React key for the history list (the
   *  list prepends, so an array index would re-key every row each advisory). */
  seq: number;
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
  /** The latest charge-guidance payload (kept for the trace; the live add-beans cue
   *  is the derived `ChargeBanner`, #211); null until fired. */
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
  /** Curve points, x = elapsed seconds, ascending and deduped on `t`. Seeded from
   *  the `/telemetry` snapshot on (re)connect (#153), then appended/merged per live
   *  telemetry frame. */
  points: CurvePoint[];
  /** Monotonic counter assigning each advisory record a stable key (per run). */
  advisorySeq: number;
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
  advisorySeq: 0,
};

/** Decision-history depth shown in the advisory panel (ui-prompts Prompt A: "last 4"). */
export const ADVISORY_HISTORY_LIMIT = 4;

type Action =
  | { kind: "event"; event: SseEvent }
  | { kind: "seed"; points: CurvePoint[] }
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

/** Project a persisted `/telemetry` snapshot point into the curve form (x = elapsed
 *  seconds). Null-elapsed points can't be placed on the x-axis, so they're dropped —
 *  same rule as the live frame path (`pointFromTelemetry`). */
function pointFromSnapshot(p: TelemetryPoint): CurvePoint | null {
  if (p.elapsed_seconds == null) return null;
  return {
    t: p.elapsed_seconds,
    bean: p.bean_temp_c,
    env: p.env_temp_c,
    ror: p.bean_ror_c_per_min,
    heat: p.heat_level_percent,
    fan: p.fan_level_percent,
  };
}

/**
 * Insert/replace a single point keyed by its elapsed-seconds `t`, keeping the
 * buffer sorted ascending and DEDUPED on `t`.
 *
 * Dedupe key is `t` (the tick's elapsed seconds): a backfilled point and the live
 * frame for the same tick must not double-plot (the #153 seam). The INCOMING point
 * wins on a `t` collision — for the live append that means a fresher frame replaces
 * its backfilled placeholder; for a re-seed it means the server snapshot refreshes
 * what's there. Points usually arrive in order, so the common path is a cheap push.
 */
function upsertPoint(points: CurvePoint[], point: CurvePoint): CurvePoint[] {
  if (points.length === 0 || point.t > points[points.length - 1].t) {
    return [...points, point];
  }
  const next = points.slice();
  // Find the insertion/replacement slot (small buffers; linear scan from the end
  // since out-of-order/duplicate arrivals are near the tail in practice).
  let i = next.length - 1;
  while (i >= 0 && next[i].t > point.t) i -= 1;
  if (i >= 0 && next[i].t === point.t) {
    next[i] = point; // replace the duplicate tick
  } else {
    next.splice(i + 1, 0, point); // insert keeping ascending order
  }
  return next;
}

/** Merge a backfilled snapshot series into the live buffer, deduping on `t`. Existing
 *  points (already-seen live frames) win over a re-seed's duplicates, so a reconnect
 *  re-hydrate only FILLS the window the device missed and never double-plots or
 *  clobbers fresher live data. */
function mergeSeed(points: CurvePoint[], seed: CurvePoint[]): CurvePoint[] {
  const present = new Set(points.map((p) => p.t));
  const additions = seed.filter((p) => !present.has(p.t));
  if (additions.length === 0) return points;
  let merged = points;
  for (const p of additions) merged = upsertPoint(merged, p);
  return merged;
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

  if (action.kind === "seed") {
    const merged = mergeSeed(state.points, action.points);
    if (merged === state.points) return state;
    return { ...state, points: merged };
  }

  const event = action.event;
  switch (event.event) {
    case "telemetry": {
      const point = pointFromTelemetry(event.data as unknown as TelemetryEventData);
      if (point === null) return state;
      return { ...state, points: upsertPoint(state.points, point) };
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
        seq: state.advisorySeq,
        decision: data.decision,
        evaluation: data.evaluation,
        synthesized: data.synthesized === true,
      };
      return {
        ...state,
        advisorySeq: state.advisorySeq + 1,
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
      const data = event.data as { command?: string };
      if (data.command !== "drop_beans") return state;
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

/** Optional test seams for `useDashboardEvents`. */
export interface UseDashboardEventsOptions {
  /** Backfill fetch (defaults to the REST `api.telemetry`). Test seam. */
  fetchTelemetry?: typeof api.telemetry;
}

/**
 * Fold the shared hook's NON-LOSSY frame stream into the dashboard view-model.
 *
 * Drains every frame from `useRoastStream`'s append-only buffer via `useFrameDrain`
 * (cursored on `frameCount`), so a burst that coalesces into one render still
 * dispatches EVERY frame — the fault / recovery / advisory / marker frames are
 * never dropped (#122). (The previous `[lastEvent]` single-slot input silently lost
 * intermediate frames when a replay `advance-to` flushed a whole run at once.)
 *
 * BACKFILL (#153): the live curve is built only from `telemetry` SSE frames, so a
 * device that opens the dashboard mid/post-roast — or RECONNECTS after backgrounding
 * — would otherwise show a blank/partial curve (the hydrate snapshot carries no
 * series). On every (re)connect — each transition of `status` INTO `live` — we fetch
 * `GET /api/roasts/{id}/telemetry` and SEED the reducer's points, deduped on elapsed
 * seconds against the live frames already folded. A reconnect re-seeds so a
 * backgrounded device re-hydrates the window it missed (the #135 "reconnect catches
 * up" criterion), and the full series gives LiveCurve a whole-roast x-range (no
 * moving window). Phase is NOT touched here — it stays the server's truth (invariant).
 *
 * On a run change (`runId`) the view-model resets, so a fresh run never paints the
 * previous run's points/markers/fault onto its curve (the page may stay mounted
 * across runs).
 */
export function useDashboardEvents(
  frames: readonly SseEvent[],
  frameCount: number,
  runId: string | null,
  status: ConnectionStatus,
  options: UseDashboardEventsOptions = {},
): DashboardViewModel {
  const [state, dispatch] = useReducer(dashboardReducer, initialDashboardViewModel);

  // Reset when the run changes (or clears) — the accumulated view-model is
  // per-run. Runs BEFORE the drain effect so a reset can't drop a frame: changing
  // runId re-subscribes the stream (clearing its buffer), whose frames arrive later.
  useEffect(() => {
    dispatch({ kind: "reset" });
  }, [runId]);

  const onFrame = useCallback((frame: SseEvent) => {
    dispatch({ kind: "event", event: frame });
  }, []);
  useFrameDrain(frames, frameCount, onFrame);

  // Held in a ref so an inline test seam doesn't re-trigger the backfill effect.
  const fetchTelemetryRef = useRef<typeof api.telemetry>(api.telemetry);
  fetchTelemetryRef.current = options.fetchTelemetry ?? api.telemetry;

  // Backfill on (re)connect: fire once per transition INTO `live`. `wasLive`
  // tracks the previous connection state so a reconnect (reconnecting/stale → live)
  // re-seeds, while staying-live renders don't refetch. A run change resets it so
  // the next run's first `live` re-seeds from scratch.
  const wasLiveRef = useRef(false);
  useEffect(() => {
    wasLiveRef.current = false;
  }, [runId]);
  useEffect(() => {
    if (runId === null) return;
    if (status !== "live") {
      // Dropped/connecting: arm the next live transition to re-seed.
      if (status === "reconnecting" || status === "stale") wasLiveRef.current = false;
      return;
    }
    if (wasLiveRef.current) return; // already seeded for this connected session
    wasLiveRef.current = true;

    let cancelled = false;
    void fetchTelemetryRef
      .current(runId)
      .then((series) => {
        if (cancelled) return;
        const seeded: CurvePoint[] = [];
        for (const p of series.points) {
          const point = pointFromSnapshot(p);
          if (point !== null) seeded.push(point);
        }
        dispatch({ kind: "seed", points: seeded });
      })
      .catch(() => {
        // A failed backfill leaves the live frames as-is and re-arms so a later
        // (re)connect can try again — the curve degrades to live-only, never errors.
        if (!cancelled) wasLiveRef.current = false;
      });
    return () => {
      cancelled = true;
    };
  }, [status, runId]);

  return state;
}
