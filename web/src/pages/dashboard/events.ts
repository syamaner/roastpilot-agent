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

import type { SafetyVerdict } from "@/lib/types";

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
