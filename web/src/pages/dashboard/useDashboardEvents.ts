/**
 * Page-local accumulator for the live-dashboard view-model.
 *
 * The shared `useRoastStream` reducer folds ONLY phase / telemetry /
 * enabledActions and surfaces every other frame via `lastEvent` (the designed
 * seam). The dashboard folds those remaining frames here: the advisory feed
 * (latest + recent history), the recovery handshake, the fault handshake + safety
 * event trail, the event markers, and the live curve point buffer. The
 * `charge_guidance` frame is NOT folded — since #211 the live add-beans cue is the
 * derived `ChargeBanner` (phase + telemetry + the profile band), so the frame is
 * left in the raw event buffer (future trace panel) rather than a view-model field.
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
  // NOTE (#211/#215): the live add-beans cue is now DERIVED (`ChargeBanner` from
  // phase + telemetry + the profile band), so the dashboard no longer folds the
  // `charge_guidance` frame into a view-model field — nothing read it. The raw
  // frame remains in the event buffer for any future trace panel; its wire shape is
  // documented by `ChargeGuidanceData` in `events.ts`.
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
  /** SERVE-elapsed seconds at the T0/charge moment (#326), null until T0 fires.
   *  Passed to LiveCurve as the charge origin so the x-axis + cursor read CHARGE-
   *  referenced ROAST TIME (0:00 = charge, negative in preheat) while the point
   *  buffer stays serve-keyed (so preheat plots live). Null before T0 → the chart
   *  shows serve-elapsed. */
  t0ElapsedSeconds: number | null;
  /** Event markers for the curve (T0 / first crack / drop / cooling), x in
   *  SERVE-elapsed seconds (#326) — the same axis the points are keyed on. The T0
   *  marker sits at the serve-elapsed of the charge moment, so it lands at the
   *  charge tick on the serve-referenced curve (and reads 0:00 once the origin
   *  transform applies). Cooling is placed from the server `phase_changed`→cooling
   *  frame (#309) — the server's phase value, never inferred locally. */
  markers: CurveMarker[];
  /** Curve points, x = SERVE-elapsed seconds (#326), ascending and deduped on `t`.
   *  Seeded from the `/telemetry` snapshot on (re)connect (#153), then appended/
   *  merged per live telemetry frame. PRE-charge frames ARE plotted (only a null
   *  serve clock is dropped), so the curve is continuous through preheat → charge →
   *  roast → drop; the charge origin is applied as a display transform downstream. */
  points: CurvePoint[];
  /** Monotonic counter assigning each advisory record a stable key (per run). */
  advisorySeq: number;
}

export const initialDashboardViewModel: DashboardViewModel = {
  latestAdvisory: null,
  advisoryHistory: [],
  advisoryPaused: false,
  recovery: null,
  fault: null,
  safetyTrail: [],
  firstCrack: null,
  t0: null,
  t0ElapsedSeconds: null,
  markers: [],
  points: [],
  advisorySeq: 0,
};

/** Decision-history depth shown in the advisory panel (ui-prompts Prompt A: "last 4"). */
export const ADVISORY_HISTORY_LIMIT = 4;

type Action =
  | { kind: "event"; event: SseEvent }
  | {
      kind: "seed";
      points: CurvePoint[];
      /** Recovered T0 origin (serve-elapsed at charge) from the snapshot's server
       *  clocks, for the cold-reload/late-join case (#326) — applied only if the
       *  origin isn't already known. Null/omitted when the snapshot has no post-charge
       *  point. */
      origin?: number | null;
    }
  | { kind: "reset" };

/**
 * Pull the SERVE-elapsed x for a telemetry frame (#326); null frames don't plot.
 *
 * The point buffer keys `t` on serve `elapsed_seconds`, NOT `charge_elapsed_seconds`
 * — so PRE-charge (preheat) frames plot LIVE, before T0/charge is known, and the
 * curve stays continuous through preheat → charge → roast → drop (the #316 fix that
 * dropped preheat frames left the chart blank during preheat). Only a null serve
 * clock is dropped (no x to place). The charge-referenced ROAST TIME display
 * (0:00 = charge, negative in preheat) is a downstream label transform in LiveCurve
 * keyed on `t0ElapsedSeconds`, not a change to the buffered x.
 */
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

/** Project a persisted `/telemetry` snapshot point into the curve form, x =
 *  SERVE-elapsed seconds (#326). Only a null serve clock is dropped — pre-charge
 *  snapshots plot, same rule as the live frame path (`pointFromTelemetry`). */
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
 * Recover the SERVE-elapsed at the T0/charge moment from a frame that carries BOTH
 * server clocks (#326 reload/late-join fix).
 *
 * SSE does not replay the one-shot `t0_detected` event, so an operator who reloads
 * (or reconnects) mid-roast would fold telemetry/snapshot frames but never the live
 * T0 event — leaving the chart axis stuck on serve-elapsed while the ROAST TIME
 * header reads charge time (seen repeatedly in roast 3). When the server reports a
 * non-null `charge_elapsed_seconds` (post-charge), the serve-elapsed at charge is
 * `elapsed_seconds − charge_elapsed_seconds`. This is NOT client-side clock
 * derivation: both operands are SERVER fields (the controller's own clocks), and we
 * round each before subtracting so the recovered origin matches the live path's
 * integer serve-elapsed. Returns null when either clock is missing (pre-charge, or a
 * partial frame) — the origin stays unknown until a post-charge frame arrives.
 */
function originFromClocks(
  elapsedSeconds: number | null | undefined,
  chargeElapsedSeconds: number | null | undefined,
): number | null {
  if (elapsedSeconds == null || chargeElapsedSeconds == null) return null;
  return Math.round(elapsedSeconds) - Math.round(chargeElapsedSeconds);
}

/** Fold a recovered T0 origin into the view-model iff it isn't already known —
 *  setting `t0ElapsedSeconds` and placing the T0 marker at that serve-elapsed (the
 *  reload path; the live `t0_detected` handler owns the streamed case). A no-op once
 *  the origin is set, so a later frame never moves an established origin/marker. */
function withRecoveredOrigin(state: DashboardViewModel, origin: number | null): DashboardViewModel {
  if (origin === null || state.t0ElapsedSeconds !== null) return state;
  return {
    ...state,
    t0ElapsedSeconds: origin,
    markers: withMarker(state.markers, { kind: "t0", t: origin, label: "T0" }),
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
 *  clobbers fresher live data.
 *
 *  Linear two-pointer merge of two ascending sequences (#155): `points` is kept
 *  sorted by `upsertPoint`, and the additions are sorted here, so the merge is
 *  O(M + N) rather than the old per-addition `upsertPoint` scan (O(M×N) when many
 *  additions land mid-buffer). On a `t` collision the existing live point wins (the
 *  addition is skipped). */
function mergeSeed(points: CurvePoint[], seed: CurvePoint[]): CurvePoint[] {
  const present = new Set(points.map((p) => p.t));
  // Dedupe vs the live buffer, then dedupe within the seed itself and sort ascending
  // so the two-pointer merge below sees a clean ascending sequence (a downsampled
  // snapshot is chronological in practice, but don't depend on it).
  const seenInSeed = new Set<number>();
  const additions: CurvePoint[] = [];
  for (const p of seed) {
    // Intra-seed duplicate `t` is FIRST-wins here (we keep the first, skip the rest) —
    // a deliberate change from the old per-point `upsertPoint` loop, which replaced on
    // `t` (LAST-wins). Moot for the real path: the /telemetry backfill series carries
    // unique timestamps, so no within-seed `t` collision occurs. The load-bearing
    // invariant is unaffected — an EXISTING live point still wins over ANY seed point
    // (the `present.has(p.t)` guard), so a re-seed never clobbers fresher live data.
    if (present.has(p.t) || seenInSeed.has(p.t)) continue;
    seenInSeed.add(p.t);
    additions.push(p);
  }
  if (additions.length === 0) return points;
  additions.sort((a, b) => a.t - b.t);

  // Merge the two ascending, mutually-disjoint sequences. No `t` collisions remain
  // between them (additions were filtered against `present`), so this is a plain
  // ascending interleave.
  const merged: CurvePoint[] = new Array<CurvePoint>(points.length + additions.length);
  let i = 0;
  let j = 0;
  let k = 0;
  while (i < points.length && j < additions.length) {
    merged[k++] = points[i].t <= additions[j].t ? points[i++] : additions[j++];
  }
  while (i < points.length) merged[k++] = points[i++];
  while (j < additions.length) merged[k++] = additions[j++];
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
    // Recover the T0 origin from the snapshot's server clocks on a cold reload/
    // late-join (#326), then merge the points. Origin recovery runs even when the
    // points themselves dedupe to a no-op (a reconnect re-seed of an already-present
    // window), so a reload still hydrates the axis origin.
    const withOrigin = withRecoveredOrigin(state, action.origin ?? null);
    const merged = mergeSeed(withOrigin.points, action.points);
    if (merged === withOrigin.points) return withOrigin;
    return { ...withOrigin, points: merged };
  }

  const event = action.event;
  switch (event.event) {
    case "telemetry": {
      const data = event.data as unknown as TelemetryEventData;
      const point = pointFromTelemetry(data);
      if (point === null) return state;
      // Recover the T0 origin from the live frame's server clocks if it isn't yet
      // known (#326): SSE doesn't replay `t0_detected`, so a reload/reconnect mid-
      // roast recovers the charge origin from the first post-charge telemetry frame
      // (charge_elapsed_seconds non-null) rather than waiting on an event that
      // already fired. A no-op once the origin is set (the live `t0_detected` path).
      const recovered = withRecoveredOrigin(state, originFromClocks(data.elapsed_seconds, data.charge_elapsed_seconds));
      return { ...recovered, points: upsertPoint(recovered.points, point) };
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
      // The charge moment's SERVE-elapsed is the latest plotted point's t (#326) —
      // the same value the FC/drop markers use, and the origin the LiveCurve display
      // subtracts to read roast time (0:00 here, preheat negative). The T0 marker
      // sits at this serve-elapsed t so it lands on the charge tick of the serve-
      // referenced curve.
      //
      // Guard an EMPTY buffer: with no plotted point we have no serve-elapsed for
      // charge, so leave `t0ElapsedSeconds` null (and place no marker) — the
      // telemetry-derive path sets it correctly once the first post-charge frame
      // lands. Defaulting to 0 here would mislabel every preheat tick as a large
      // positive roast-time. Always record the T0 detection itself.
      if (state.points.length === 0) {
        return { ...state, t0: data };
      }
      const at = state.points[state.points.length - 1].t;
      return {
        ...state,
        t0: data,
        // FIRST-WINS, consistent with the telemetry/seed recovery path: if a
        // reconnect/late-join already DERIVED the origin from the server's own clocks
        // (elapsed − charge_elapsed, the canonical value), keep it — a re-fired
        // t0_detected must not clobber it with the latest-point heuristic. They agree
        // in practice (both reference the T0-detection tick), so this only set it when
        // still null. The marker dedupes via withMarker.
        t0ElapsedSeconds: state.t0ElapsedSeconds ?? at,
        markers: withMarker(state.markers, { kind: "t0", t: at, label: "T0" }),
      };
    }
    case "first_crack": {
      const data = event.data as unknown as FirstCrackData;
      // The FC marker's x is the serve-elapsed at detection — the latest plotted
      // point's t (the buffer x-axis is serve-elapsed seconds, #326; the roast-time
      // re-label is a display transform in LiveCurve).
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
    case "phase_changed": {
      // The COOLING marker (#309). Phase is the SERVER's truth — owned by the
      // shared reducer; we do NOT set it here and never infer it. We only READ the
      // server-emitted phase value to place a display marker at the cooling
      // transition (every drop lands in COOLING — controller.py — and explicit
      // recovery cooling also transitions to COOLING, so phase_changed→cooling is
      // the one signal that covers both paths). The marker sits at the latest
      // plotted point's serve-elapsed (the same axis as T0/FC/drop, #326; the
      // roast-time re-label is a display transform in LiveCurve), and dedupes via
      // withMarker. Any other phase transition places no marker.
      const data = event.data as { phase?: string };
      if (data.phase !== "cooling") return state;
      const at = state.points.length > 0 ? state.points[state.points.length - 1].t : 0;
      return {
        ...state,
        markers: withMarker(state.markers, { kind: "cooling", t: at, label: "COOLING" }),
      };
    }
    default:
      // run_started / command_failed / logs_exported / run_completed / heartbeat
      // / charge_guidance — not folded here (charge_guidance is now consumed via
      // the derived ChargeBanner, #211; the rest aren't dashboard view-model
      // state). NOTE phase_changed is now matched above for the COOLING marker
      // ONLY — phase itself remains the shared reducer's truth (we never set it).
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
        // Recover the T0 origin from the FIRST post-charge snapshot point's server
        // clocks (#326 cold-reload): SSE didn't replay `t0_detected`, so the snapshot
        // is the only place the origin can be recovered on a fresh page load. Earliest
        // post-charge point keeps the recovery deterministic regardless of order.
        let origin: number | null = null;
        for (const p of series.points) {
          const point = pointFromSnapshot(p);
          if (point !== null) seeded.push(point);
          if (origin === null) {
            const o = originFromClocks(p.elapsed_seconds, p.charge_elapsed_seconds);
            if (o !== null) origin = o;
          }
        }
        dispatch({ kind: "seed", points: seeded, origin });
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
