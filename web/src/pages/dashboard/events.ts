/**
 * Page-local typed shapes for the SSE event payloads the SHARED reducer
 * deliberately does NOT fold (it owns phase/telemetry/enabledActions and surfaces
 * everything else via `lastEvent` — the designed seam). The dashboard folds these
 * itself in `useDashboardEvents`.
 *
 * Each shape mirrors the server emit site byte-for-byte (verified against
 * controller.py / replay.py / safety.py); all temperatures Celsius. We keep these
 * here rather than in shared `lib/types.ts` because they are the dashboard's
 * consumption concern — other pages read these events differently (the detail
 * page reads the persisted timeline, not the live frames).
 */

import type { RoastTimeline, SafetyVerdict } from "@/lib/types";

/** A safety handshake (`safety.SafetyEvaluation.model_dump`). Carried by
 *  `recovery_required`, `fault`, `safety_alert`, and inside an `advisory`'s
 *  `evaluation`. `input_*` is the requested command (unbounded); `adjusted_*` is
 *  what may execute (bounded, nullable on most non-ALLOW/CLAMP verdicts). */
export interface SafetyEvaluationData {
  rule: string;
  verdict: SafetyVerdict;
  input_heat: number | null;
  input_fan: number | null;
  adjusted_heat: number | null;
  adjusted_fan: number | null;
  reason: string;
}

/** The advisor's recommendation (`advisor.RoastDecision.model_dump`). */
export interface AdvisorDecisionData {
  target_heat: number;
  target_fan: number;
  should_drop: boolean;
  confidence: number;
  rationale: string;
}

/**
 * The `advisory` SSE payload. Several shapes share the event:
 *  - a full recommendation: `decision` + `evaluation`
 *  - a phase-gated / advisor-failure record: `evaluation` only (no decision)
 *  - a skipped record: `skipped`
 *  - the pause/resume toggles: `advisory_paused`
 * The replay CLAMP key frame additionally carries `source: "replay_overlay"` /
 * `synthesized: true` so a reader never mistakes it for live-evaluated output.
 */
export interface AdvisoryEventData {
  trigger?: string;
  decision?: AdvisorDecisionData;
  evaluation?: SafetyEvaluationData;
  advisory_paused?: boolean;
  skipped?: string;
  source?: string;
  synthesized?: boolean;
}

/** Wire shape of the `charge_guidance` frame; consumed via the raw event buffer /
 *  future trace panel. The LIVE add-beans cue is now the persistent `ChargeBanner`
 *  derived from phase + telemetry + the profile band (#211/#215), so the dashboard
 *  reducer no longer folds this frame into a view-model field — but the type stays
 *  here to document the wire contract (the controller still emits the frame). */
export interface ChargeGuidanceData {
  bean_temp_c: number;
  env_temp_c: number;
  guidance_min_c: number;
  guidance_max_c: number;
}

/** The `t0_detected` payload. */
export interface T0DetectedData {
  debounce_ticks?: number;
  bean_temp_c?: number;
}

/** The `first_crack` payload — `source` is `mcp` (auto/audio) or `operator`. */
export interface FirstCrackData {
  source: string;
  bean_temp_c?: number;
}

/**
 * Recover the first-crack event from the persisted server timeline.
 *
 * The live dashboard normally receives this payload over SSE. SSE does not
 * replay one-shot events after a reload, so the same server-persisted event is
 * the reload-safe fallback; this never infers FC from phase or curve points.
 */
export function firstCrackFromTimeline(
  timeline: RoastTimeline | undefined,
): FirstCrackData | null {
  const event = timeline?.events.find((candidate) => candidate.kind === "first_crack");
  if (event === undefined) return null;

  const payload = event.payload;
  const source =
    payload !== null && typeof payload.source === "string"
      ? payload.source
      : event.source;
  const beanTempC =
    payload !== null &&
    typeof payload.bean_temp_c === "number" &&
    Number.isFinite(payload.bean_temp_c)
      ? payload.bean_temp_c
      : undefined;

  return beanTempC === undefined
    ? { source }
    : { source, bean_temp_c: beanTempC };
}

/** The `turning_point` payload (#409) — the post-charge bean-temp minimum. Carries
 *  the bean temp at the RoR-zero cross + the charge-referenced elapsed clock at that
 *  tick; the clock IS the authoritative marker x (unlike `drying_end`, which has no
 *  clock and rides the latest-point heuristic). `elapsed_since_charge_seconds` is the
 *  server's charge clock — the same field `turningPointSeconds()` uses on the detail
 *  page. The serve-elapsed position for the live marker is
 *  `t0ElapsedSeconds + elapsed_since_charge_seconds`. */
export interface TurningPointData {
  bean_temp_c?: number;
  elapsed_since_charge_seconds: number;
}

/** The `drying_end` payload (#351) — the pre-FC drying→browning landmark. Carries
 *  the bean temp at the cross + the server threshold that fired it; like
 *  `first_crack` it carries NO clock, so the marker's x is derived from the latest
 *  plotted telemetry point (the same serve-elapsed axis, #326). */
export interface DryingEndData {
  bean_temp_c?: number;
  threshold_c?: number;
}
