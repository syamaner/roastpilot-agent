/**
 * Pure data-shaping for the roast detail page (E10-S5).
 *
 * Kept out of the React components so the timeline → view-model projection is
 * unit-testable without a canvas or a render (D24). Every value is read from the
 * REST contract (`RoastTimeline` / `TelemetrySeries` / `RoastDetail`); nothing
 * is inferred. All temperatures Celsius.
 *
 * Projections:
 *   - `toCurvePoints`  : telemetry snapshots → the shared `LiveCurve` point form
 *                        (x = seconds since T0).
 *   - `toCurveMarkers` : T0 (from the `t0_detected` event) + first-crack / drop
 *                        from the telemetry's `development` / `cooling` phase
 *                        transitions (the controller's FC/drop events carry no
 *                        tick or time — server-derived phase is the reliable axis);
 *                        plus turning-point (#409) from the persisted `turning_point`
 *                        event's charge-clock (see `turningPointSeconds`), and
 *                        dry-end (#351) from the persisted `drying_end` event's
 *                        server threshold (see `dryEndSeconds`).
 *   - `toTraceRows`    : safety evaluations joined by `tick` to their advisor
 *                        decision + executed command → the decision-trace table.
 *   - `headlineStats`  : title-block stats derived from the same telemetry phases.
 */

import type { CurveMarker, CurvePoint } from "@/components/shared/LiveCurve";
import type {
  RoastTimeline,
  SafetyVerdict,
  TelemetryPoint,
  TelemetrySeries,
  TimelineAdvisorDecision,
} from "@/lib/types";

/**
 * One row of the decision-trace table — a safety verdict, optionally enriched
 * with the advisor recommendation it judged and the command it produced. The
 * verdict column renders ALL SIX verdicts (it's history, not advisory state).
 */
export interface TraceRow {
  /** Controller tick — the join key and the curve-highlight anchor. */
  tick: number;
  /** Seconds since T0 for this tick (from telemetry), or `null` if unknown. */
  elapsedSeconds: number | null;
  recordedAtUtc: string;
  verdict: SafetyVerdict;
  rule: string;
  reason: string;
  /** Advisor-recommended target (from `advisor_decisions.decision`), if any. */
  recommendedHeat: number | null;
  recommendedFan: number | null;
  shouldDrop: boolean | null;
  confidence: number | null;
  rationale: string | null;
  /** What the safety layer actually allowed through (the clamp delta). */
  executedHeat: number | null;
  executedFan: number | null;
  /** The MCP command this tick produced, if one was logged. */
  commandTool: string | null;
  commandStatus: "ok" | "failed" | null;
}

/** Title-block headline stats — all derived from telemetry + timeline markers. */
export interface HeadlineStats {
  /** Total roast duration in seconds (last telemetry `elapsed_seconds`). */
  totalSeconds: number | null;
  firstCrackSeconds: number | null;
  firstCrackTempC: number | null;
  dropSeconds: number | null;
  dropTempC: number | null;
  /** Final development % (last telemetry point that carries one). */
  developmentPercent: number | null;
}

// --- Curve projection ---------------------------------------------------------

/**
 * Project telemetry snapshots into the shared `LiveCurve` point form.
 *
 * x is seconds since T0 (`elapsed_seconds`); points without an elapsed value are
 * dropped (they cannot be placed on the time axis). Heat/fan are the *executed*
 * control levels; a missing channel is a gap (`null`), not zero.
 */
export function toCurvePoints(series: TelemetrySeries | undefined): CurvePoint[] {
  if (!series) return [];
  const points: CurvePoint[] = [];
  for (const p of series.points) {
    if (p.elapsed_seconds === null) continue;
    points.push({
      t: p.elapsed_seconds,
      bean: p.bean_temp_c,
      env: p.env_temp_c,
      ror: p.bean_ror_c_per_min,
      heat: p.heat_level_percent,
      fan: p.fan_level_percent,
    });
  }
  return points;
}

/**
 * Build a tick → `elapsed_seconds` lookup so a trace row (keyed by tick) can be
 * placed on the curve's seconds axis. Latest value wins on duplicate ticks.
 */
function tickElapsedIndex(series: TelemetrySeries | undefined): Map<number, number> {
  const index = new Map<number, number>();
  if (!series) return index;
  for (const p of series.points) {
    if (p.elapsed_seconds !== null) index.set(p.tick, p.elapsed_seconds);
  }
  return index;
}

/** Map a trace-row tick to its curve x-position (seconds since T0). */
export function tickToSeconds(
  series: TelemetrySeries | undefined,
  tick: number,
): number | null {
  return tickElapsedIndex(series).get(tick) ?? null;
}

/**
 * Vertical event markers for the curve, all positioned on the curve's
 * seconds-since-T0 axis. T0 anchors x=0 (it defines the origin). First-crack and
 * drop are NOT carried with a tick or time on the timeline event payload (the
 * controller emits `{source, bean_temp_c}` / `{phase}`), so we read their moment
 * from the persisted telemetry's `agent_phase` transitions instead — the first
 * point in `development` is first crack, the first in `cooling` is the drop. Both
 * signals are in the REST contract; nothing is inferred.
 */
export function toCurveMarkers(
  timeline: RoastTimeline | undefined,
  series: TelemetrySeries | undefined,
): CurveMarker[] {
  const markers: CurveMarker[] = [];
  const hasT0 = (timeline?.events ?? []).some((e) => e.kind === "t0_detected");
  if (hasT0) markers.push({ kind: "t0", t: 0, label: "T0" });

  const tp = turningPointSeconds(timeline, series);
  if (tp !== null) markers.push({ kind: "turning_point", t: tp, label: "TURN" });

  const dryEnd = dryEndSeconds(timeline, series);
  if (dryEnd !== null) markers.push({ kind: "dry_end", t: dryEnd, label: "DRY END" });

  const fc = firstPhaseSeconds(series, "development");
  if (fc !== null) markers.push({ kind: "first_crack", t: fc, label: "FIRST CRACK" });

  const drop = firstPhaseSeconds(series, "cooling");
  if (drop !== null) markers.push({ kind: "drop", t: drop, label: "DROP" });

  return markers;
}

/**
 * `elapsed_seconds` of the turning-point landmark for the persisted detail curve
 * (#409), or null when the roast never recorded one.
 *
 * The pre-FC `turning_point` timeline event carries `{bean_temp_c,
 * elapsed_since_charge_seconds}` — the charge-referenced clock at the tick RoR first
 * crossed zero. Its x is placed by scanning the persisted telemetry for the first
 * point whose `charge_elapsed_seconds` reaches the event's
 * `elapsed_since_charge_seconds`, then returning that point's serve-referenced
 * `elapsed_seconds` (the detail curve's x-axis). The event's presence is the gate;
 * nothing is inferred client-side. Mirrors the `dryEndSeconds` pattern (#351).
 */
function turningPointSeconds(
  timeline: RoastTimeline | undefined,
  series: TelemetrySeries | undefined,
): number | null {
  const event = (timeline?.events ?? []).find((e) => e.kind === "turning_point");
  if (!event || !series) return null;
  const chargeElapsed = event.payload?.elapsed_since_charge_seconds;
  if (typeof chargeElapsed !== "number") return null;
  for (const p of series.points) {
    if (
      p.charge_elapsed_seconds !== null &&
      p.charge_elapsed_seconds >= chargeElapsed &&
      p.elapsed_seconds !== null
    ) {
      return p.elapsed_seconds;
    }
  }
  return null;
}

/**
 * `elapsed_seconds` of the dry-end landmark for the persisted detail curve (#351),
 * or null when the roast never recorded one.
 *
 * The pre-FC `drying_end` timeline event carries the server's `bean_temp_c` +
 * `threshold_c` but NO tick or time (it is not a tick-keyed trace record), so —
 * unlike FC/drop, which read a server PHASE transition — its x is placed from the
 * SERVER'S OWN threshold against the persisted telemetry: the first telemetry point
 * whose `bean_temp_c` reaches the event's `threshold_c`. That is the exact rising
 * cross the controller latched on (`_maybe_emit_drying_end`), replayed against the
 * server's persisted readings — the threshold is the server's, the temps are the
 * server's, and the event's presence is the gate; nothing is inferred client-side.
 * Gated on the event existing so a roast with no drying_end never paints a marker.
 */
function dryEndSeconds(
  timeline: RoastTimeline | undefined,
  series: TelemetrySeries | undefined,
): number | null {
  const event = (timeline?.events ?? []).find((e) => e.kind === "drying_end");
  if (!event || !series) return null;
  const threshold = event.payload?.threshold_c;
  if (typeof threshold !== "number") return null;
  for (const p of series.points) {
    if (p.bean_temp_c !== null && p.bean_temp_c >= threshold && p.elapsed_seconds !== null) {
      return p.elapsed_seconds;
    }
  }
  return null;
}

/** `elapsed_seconds` of the first telemetry point that entered `phase`, or null. */
function firstPhaseSeconds(
  series: TelemetrySeries | undefined,
  phase: TelemetryPoint["agent_phase"],
): number | null {
  if (!series) return null;
  for (const p of series.points) {
    if (p.agent_phase === phase && p.elapsed_seconds !== null) return p.elapsed_seconds;
  }
  return null;
}

/** Bean temp at the first telemetry point that entered `phase`, or null. */
function firstPhaseBeanTemp(
  series: TelemetrySeries | undefined,
  phase: TelemetryPoint["agent_phase"],
): number | null {
  if (!series) return null;
  for (const p of series.points) {
    if (p.agent_phase === phase) return p.bean_temp_c;
  }
  return null;
}

// --- Trace-table projection ---------------------------------------------------

/**
 * Join the timeline's three trace streams by `tick` into table rows — one row per
 * safety evaluation (the verdict is the spine), enriched with the advisor
 * recommendation it judged and the command it produced.
 *
 * The advisor `decision` payload is typed `dict[str, Any]` on the wire (opaque by
 * design), so we read its known `RoastDecision` keys defensively: present →
 * render, absent → `null`. We never fabricate a recommendation the contract
 * didn't carry.
 */
export function toTraceRows(
  timeline: RoastTimeline | undefined,
  series: TelemetrySeries | undefined,
): TraceRow[] {
  if (!timeline) return [];
  const elapsed = tickElapsedIndex(series);
  const advisorByTick = lastByTick(timeline.advisor_decisions);
  const commandByTick = lastByTick(timeline.commands);

  return timeline.safety_evaluations.map((evaluation) => {
    const advisor = advisorByTick.get(evaluation.tick);
    const command = commandByTick.get(evaluation.tick);
    const decision = readDecision(advisor);
    return {
      tick: evaluation.tick,
      elapsedSeconds: elapsed.get(evaluation.tick) ?? null,
      recordedAtUtc: evaluation.recorded_at_utc,
      verdict: evaluation.verdict,
      rule: evaluation.rule,
      reason: evaluation.reason,
      recommendedHeat: decision.heat,
      recommendedFan: decision.fan,
      shouldDrop: decision.shouldDrop,
      confidence: decision.confidence,
      rationale: decision.rationale,
      // Executed = what safety let through. `adjusted_*` is the clamped value;
      // when nothing was clamped it equals the input, so fall back to input.
      executedHeat: evaluation.adjusted_heat ?? evaluation.input_heat,
      executedFan: evaluation.adjusted_fan ?? evaluation.input_fan,
      commandTool: command?.tool ?? null,
      commandStatus: command?.status ?? null,
    };
  });
}

// --- Headline stats -----------------------------------------------------------

/**
 * Derive the title-block stats from the persisted telemetry. First crack and drop
 * are the telemetry's first `development` / `cooling` points (the same
 * phase-transition signal the curve markers use) — server-derived phase, never
 * inferred client-side. `_timeline` is accepted for symmetry with the other
 * projections (and forward use) but the stats come from telemetry.
 */
export function headlineStats(
  _timeline: RoastTimeline | undefined,
  series: TelemetrySeries | undefined,
): HeadlineStats {
  const points = series?.points ?? [];
  return {
    totalSeconds: lastElapsed(points),
    firstCrackSeconds: firstPhaseSeconds(series, "development"),
    firstCrackTempC: firstPhaseBeanTemp(series, "development"),
    dropSeconds: firstPhaseSeconds(series, "cooling"),
    dropTempC: firstPhaseBeanTemp(series, "cooling"),
    developmentPercent: lastDevelopmentPercent(points),
  };
}

// --- internals ----------------------------------------------------------------

/** Index records by tick, keeping the last record for a tick (insertion order). */
function lastByTick<T extends { tick: number }>(records: readonly T[]): Map<number, T> {
  const index = new Map<number, T>();
  for (const record of records) index.set(record.tick, record);
  return index;
}

interface DecisionFields {
  heat: number | null;
  fan: number | null;
  shouldDrop: boolean | null;
  confidence: number | null;
  rationale: string | null;
}

/** Read the known `RoastDecision` keys off the opaque advisor `decision` dict. */
function readDecision(advisor: TimelineAdvisorDecision | undefined): DecisionFields {
  const d = advisor?.decision ?? null;
  if (d === null) {
    return { heat: null, fan: null, shouldDrop: null, confidence: null, rationale: null };
  }
  return {
    heat: numberOrNull(d.target_heat),
    fan: numberOrNull(d.target_fan),
    shouldDrop: typeof d.should_drop === "boolean" ? d.should_drop : null,
    confidence: numberOrNull(d.confidence),
    rationale: typeof d.rationale === "string" ? d.rationale : null,
  };
}

function numberOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function lastElapsed(points: readonly TelemetryPoint[]): number | null {
  for (let i = points.length - 1; i >= 0; i -= 1) {
    if (points[i].elapsed_seconds !== null) return points[i].elapsed_seconds;
  }
  return null;
}

function lastDevelopmentPercent(points: readonly TelemetryPoint[]): number | null {
  for (let i = points.length - 1; i >= 0; i -= 1) {
    if (points[i].development_percent !== null) return points[i].development_percent;
  }
  return null;
}
