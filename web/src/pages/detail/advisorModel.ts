/**
 * Advisor-decision timeline projection for the roast detail page (#170).
 *
 * The existing `traceModel.toTraceRows` is SAFETY-spined: one row per safety
 * evaluation, enriched with the advisor recommendation joined through its exact
 * safety-evaluation FK. That model cannot surface a consult the advisor FAILED on,
 * because a failed decision has no recommendation for a safety-spined row; a roast
 * where every consult failed would render a blank decision-trace panel (#170
 * forbids exactly that), even though failures persist linked safety evaluations.
 *
 * This module is the ADVISOR-spined complement: one row per persisted
 * `advisor_decisions` entry — including preheat consults and failures — joined to
 * the safety verdict it produced by `safety_evaluation_id`. Failures show the
 * failure, never a blank.
 *
 * Everything is read from the REST `RoastTimeline` contract; nothing is inferred.
 * All temperatures Celsius. The advisor `decision` payload is opaque `dict` on the
 * wire, so its `RoastDecision` keys are read defensively (present → render, absent
 * → null) — we never fabricate a recommendation the contract did not carry.
 */

import type {
  AdvisorTraceStatus,
  RoastTimeline,
  SafetyVerdict,
  TelemetrySeries,
  TimelineAdvisorDecision,
} from "@/lib/types";

/**
 * One advisor consult, projected for the timeline. A row exists for EVERY
 * persisted consult, regardless of status — a failed consult still has a tick,
 * provider/model, latency, and (no) verdict, and must render its failure.
 */
export interface AdvisorRow {
  tick: number;
  /** Seconds since T0 for this tick (from telemetry), or `null` if unknown. */
  elapsedSeconds: number | null;
  /** Bean temperature (°C) at this tick, joined from telemetry by tick (#325) —
   *  the roast-moment temp the advisor reasoned at; `null` when unknown. */
  beanTempC: number | null;
  recordedAtUtc: string;
  provider: string;
  model: string;
  promptVersion: string;
  status: AdvisorTraceStatus;
  latencyMs: number | null;
  /** Advisor recommendation (only meaningful when `status === "ok"`). */
  recommendedHeat: number | null;
  recommendedFan: number | null;
  shouldDrop: boolean | null;
  confidence: number | null;
  rationale: string | null;
  /** The safety verdict this consult produced, joined by tick (`null` if none). */
  verdict: SafetyVerdict | null;
  /** Safety reason — falls back as the row's explanation when no rationale. */
  verdictReason: string | null;
}

/** Per-roast advisor summary for the history list + detail header. */
export interface AdvisorSummary {
  /** Total persisted consults (incl. preheat + failures). */
  consults: number;
  /** Consults that returned a usable decision (`status === "ok"`). */
  ok: number;
  /** Consults that did NOT return a usable decision (timeout/malformed/error). */
  failed: number;
  /** Of the produced verdicts: how many were clamped. */
  clamped: number;
  /** Of the produced verdicts: how many were rejected. */
  rejected: number;
}

/**
 * Project the timeline's advisor decisions into timeline rows, joined to the
 * safety verdict each produced (by exact FK) and placed on the curve's seconds axis.
 * Insertion order is preserved (the store reads `ORDER BY id ASC`).
 */
export function toAdvisorRows(
  timeline: RoastTimeline | undefined,
  series: TelemetrySeries | undefined,
): AdvisorRow[] {
  if (!timeline) return [];
  const elapsed = tickElapsedIndex(series);
  const beanByTick = tickBeanTempIndex(series);
  const evaluationById = new Map(
    timeline.safety_evaluations.map((evaluation) => [evaluation.id, evaluation]),
  );

  return timeline.advisor_decisions.map((advisor) => {
    const evaluation =
      advisor.safety_evaluation_id === null
        ? undefined
        : evaluationById.get(advisor.safety_evaluation_id);
    const decision = readDecision(advisor);
    return {
      tick: advisor.tick,
      elapsedSeconds: elapsed.get(advisor.tick) ?? null,
      beanTempC: beanByTick.get(advisor.tick) ?? null,
      recordedAtUtc: advisor.recorded_at_utc,
      provider: advisor.provider,
      model: advisor.model,
      promptVersion: advisor.prompt_version,
      status: advisor.status,
      latencyMs: advisor.latency_ms,
      recommendedHeat: decision.heat,
      recommendedFan: decision.fan,
      shouldDrop: decision.shouldDrop,
      confidence: decision.confidence,
      rationale: decision.rationale,
      verdict: evaluation?.verdict ?? null,
      verdictReason: evaluation?.reason ?? null,
    };
  });
}

/**
 * Summarise a roast's advisor activity from its timeline — consult count, ok vs
 * failed, and how many produced verdicts were clamped / rejected. All counts come
 * straight from the persisted rows; nothing is inferred.
 */
export function advisorSummary(timeline: RoastTimeline | undefined): AdvisorSummary {
  const decisions = timeline?.advisor_decisions ?? [];
  const evaluationById = new Map(
    (timeline?.safety_evaluations ?? []).map((evaluation) => [evaluation.id, evaluation]),
  );

  let ok = 0;
  let failed = 0;
  let clamped = 0;
  let rejected = 0;
  for (const decision of decisions) {
    if (decision.status === "ok") {
      ok += 1;
    } else {
      failed += 1;
    }
    const verdict =
      decision.safety_evaluation_id === null
        ? undefined
        : evaluationById.get(decision.safety_evaluation_id)?.verdict;
    if (verdict === "clamp") clamped += 1;
    if (verdict === "reject") rejected += 1;
  }
  return { consults: decisions.length, ok, failed, clamped, rejected };
}

/** Whether a status represents a failed consult (no usable decision). */
export function isFailureStatus(status: AdvisorTraceStatus): boolean {
  return status !== "ok";
}

/** Human label for an advisor trace status. */
export function advisorStatusLabel(status: AdvisorTraceStatus): string {
  switch (status) {
    case "ok":
      return "OK";
    case "timeout":
      return "TIMEOUT";
    case "malformed":
      return "MALFORMED";
    case "provider_error":
      return "PROVIDER ERROR";
  }
}

// --- internals ----------------------------------------------------------------

/** Build a tick → `elapsed_seconds` lookup; latest value wins on duplicate ticks. */
function tickElapsedIndex(series: TelemetrySeries | undefined): Map<number, number> {
  const index = new Map<number, number>();
  if (!series) return index;
  for (const p of series.points) {
    if (p.elapsed_seconds !== null) index.set(p.tick, p.elapsed_seconds);
  }
  return index;
}

/**
 * Build a tick → `bean_temp_c` lookup (#325); LATEST value wins on duplicate
 * ticks, INCLUDING a later null reading.
 *
 * Every point sets its tick (we do NOT skip nulls), so a later duplicate point
 * with a null bean reading OVERWRITES an earlier non-null one — the row then
 * renders the null placeholder, not a stale temp. (Skipping nulls would leave the
 * older value latched, contradicting latest-wins.) The map value is therefore
 * `number | null`; the caller `?? null`-coalesces a missing tick (`undefined`) and
 * an explicit null alike, so both read as "no temp".
 */
function tickBeanTempIndex(series: TelemetrySeries | undefined): Map<number, number | null> {
  const index = new Map<number, number | null>();
  if (!series) return index;
  for (const p of series.points) {
    index.set(p.tick, p.bean_temp_c);
  }
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
