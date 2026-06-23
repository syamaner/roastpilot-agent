/**
 * Deterministic REST-shaped fixtures for the detail page (E10-S5).
 *
 * Used by BOTH the snapshot harness route (`/__detail-harness`) and the component
 * tests, so the two stay in lock-step and the Playwright baselines are stable.
 * The shapes mirror the real `RoastDetail` / `TelemetrySeries` / `RoastTimeline`
 * contract exactly; the values are hand-authored (a short COMPLETED roast that
 * carries one CLAMP decision — the talk's key frame on the detail trace table).
 *
 * Not production data — a fixed stand-in for the real `/telemetry`+`/timeline`
 * the page renders against a live run id. All temperatures Celsius.
 */

import type {
  RoastDetail,
  RoastTimeline,
  TelemetryPoint,
  TelemetrySeries,
} from "@/lib/types";

export const FIXTURE_RUN_ID = "detail-fixture-001";

/**
 * A short roast arc, by server-derived phase (the same signal the page reads):
 *   ticks 0–10  roasting_pre_first_crack   (climb to first crack)
 *   ticks 11–14 development                 (FC at tick 11)
 *   tick  15    cooling                      (drop into cooling)
 * The page derives FC/drop from these phase transitions — never inferred.
 */
const TELEMETRY_POINTS: TelemetryPoint[] = Array.from({ length: 16 }, (_, i) => {
  const elapsed = i * 30; // 30 s per sampled tick (downsampled view).
  const phase = i < 11 ? "roasting_pre_first_crack" : i < 15 ? "development" : "cooling";
  return {
    tick: i,
    elapsed_seconds: elapsed,
    // #308: charge-referenced clock. This fixture's roast is already past charge
    // (it opens in roasting_pre_first_crack), so mirror the elapsed value — the
    // detail page plots on its own x and does not read this yet.
    charge_elapsed_seconds: elapsed,
    agent_phase: phase,
    bean_temp_c: 92 + i * 8.2,
    env_temp_c: 120 + i * 6.5,
    bean_ror_c_per_min: Math.max(3, 16 - i * 0.7),
    env_ror_c_per_min: Math.max(2, 14 - i * 0.6),
    heat_level_percent: i < 5 ? 80 : i < 10 ? 70 : i < 15 ? 60 : 0,
    fan_level_percent: i < 5 ? 40 : i < 10 ? 55 : i < 15 ? 70 : 100,
    cooling_on: i >= 15,
    development_percent: i >= 11 ? (i - 11) * 5 + 4 : null,
  };
});

export const FIXTURE_DETAIL: RoastDetail = {
  id: FIXTURE_RUN_ID,
  agent_phase: "complete",
  profile: {
    name: "Ethiopian Yirgacheffe — Medium",
    bean_origin: "Ethiopia, Yirgacheffe",
    bean_varietal: "Heirloom",
    country: "Ethiopia",
    farm: "Gedeb — Worka Sakaro",
    description: "Washed; jasmine, bergamot, stone fruit.",
    bean_species: "arabica",
    is_blend: false,
    bean_weight_grams: 220,
    charge_guidance_min_c: 170,
    charge_guidance_max_c: 200,
    initial_heat_percent: 80,
    initial_fan_percent: 40,
    target_drop_temp_c: 218,
    target_development_percent: 21,
  },
  outcome: "completed",
  started_at_utc: "2026-06-07T09:12:00Z",
  completed_at_utc: "2026-06-07T09:24:54Z",
  fault_reason: null,
  rating: 4,
  notes: "Good body, slightly bright.",
  export_manifest: {
    log_dir: "/var/roastpilot/logs/detail-fixture-001",
    jsonl_path: "/var/roastpilot/logs/detail-fixture-001/roast.jsonl",
    csv_path: "/var/roastpilot/logs/detail-fixture-001/roast.csv",
    summary_path: "/var/roastpilot/logs/detail-fixture-001/summary.json",
    ready: true,
    note: null,
  },
  enabled_actions: [],
};

export const FIXTURE_TELEMETRY: TelemetrySeries = {
  run_id: FIXTURE_RUN_ID,
  downsample: 1,
  point_count: TELEMETRY_POINTS.length,
  points: TELEMETRY_POINTS,
};

/**
 * The decision trace. One ALLOW, one CLAMP (heat 105 → 100, the synthesized demo
 * key frame), one REJECT — so the table shows the three advisory verdicts plus the
 * verdict-column label coverage the page must render. The CLAMP row at tick 8 is
 * the `roast-detail-selected` snapshot target.
 */
export const FIXTURE_TIMELINE: RoastTimeline = {
  run_id: FIXTURE_RUN_ID,
  events: [
    { kind: "run_started", source: "controller", monotonic_seconds: 0, recorded_at_utc: "2026-06-07T09:12:00Z", payload: { tick: 0 } },
    { kind: "t0_detected", source: "mcp", monotonic_seconds: 0, recorded_at_utc: "2026-06-07T09:12:30Z", payload: { tick: 1 } },
    { kind: "first_crack", source: "mcp", monotonic_seconds: 330, recorded_at_utc: "2026-06-07T09:18:00Z", payload: { source: "audio_model", bean_temp_c: 201.2, confidence: 0.907 } },
    { kind: "phase_changed", source: "controller", monotonic_seconds: 450, recorded_at_utc: "2026-06-07T09:20:00Z", payload: { phase: "cooling" } },
    { kind: "logs_exported", source: "controller", monotonic_seconds: 470, recorded_at_utc: "2026-06-07T09:24:54Z", payload: null },
  ],
  safety_evaluations: [
    {
      tick: 4,
      rule: "rate_limit",
      verdict: "allow",
      input_heat: 80,
      input_fan: 40,
      adjusted_heat: 80,
      adjusted_fan: 40,
      reason: "within limits",
      recorded_at_utc: "2026-06-07T09:14:00Z",
    },
    {
      tick: 8,
      rule: "bounds",
      verdict: "clamp",
      input_heat: 105,
      input_fan: 40,
      adjusted_heat: 100,
      adjusted_fan: 40,
      reason: "requested heat 105 % / fan 40 % outside the control box (heat 0–100 %, fan 0–100 %): clamped to heat 100 % / fan 40 %",
      recorded_at_utc: "2026-06-07T09:16:00Z",
    },
    {
      tick: 12,
      rule: "drop_guard",
      verdict: "reject",
      input_heat: 0,
      input_fan: 100,
      adjusted_heat: null,
      adjusted_fan: null,
      reason: "drop rejected: development below target; advisor confidence 0.40 under threshold",
      recorded_at_utc: "2026-06-07T09:18:30Z",
    },
  ],
  advisor_decisions: [
    {
      tick: 4,
      provider: "openrouter",
      model: "anthropic/claude-opus-4.8",
      prompt_version: "v1",
      latency_ms: 820,
      status: "ok",
      decision: { target_heat: 80, target_fan: 40, should_drop: false, confidence: 0.86, rationale: "Holding heat; RoR climbing as expected after charge." },
      recorded_at_utc: "2026-06-07T09:14:00Z",
    },
    {
      tick: 8,
      provider: "openrouter",
      model: "anthropic/claude-opus-4.8",
      prompt_version: "v1",
      latency_ms: 910,
      status: "ok",
      decision: { target_heat: 105, target_fan: 40, should_drop: false, confidence: 0.78, rationale: "RoR stalling near first crack; push heat to keep momentum into development." },
      recorded_at_utc: "2026-06-07T09:16:00Z",
    },
    {
      tick: 12,
      provider: "openrouter",
      model: "anthropic/claude-opus-4.8",
      prompt_version: "v1",
      latency_ms: 760,
      status: "ok",
      decision: { target_heat: 0, target_fan: 100, should_drop: true, confidence: 0.4, rationale: "Considering an early drop, but development ratio is still low." },
      recorded_at_utc: "2026-06-07T09:18:30Z",
    },
  ],
  commands: [
    { tick: 4, tool: "set_heat", source: "advisor", status: "ok", args: { percent: 80 }, result: { ok: true }, recorded_at_utc: "2026-06-07T09:14:00Z" },
    { tick: 8, tool: "set_heat", source: "safety", status: "ok", args: { percent: 100 }, result: { ok: true }, recorded_at_utc: "2026-06-07T09:16:00Z" },
  ],
};

// --- Dry-end fixture (#351) ---------------------------------------------------
//
// FIXTURE_TIMELINE plus a persisted `drying_end` timeline event (the pre-FC
// drying→browning landmark), so the detail-page reload path can be exercised
// END-TO-END (a positive D24 assertion that `dry_end` reaches the chart's
// `window.__chart` data). Kept as a SEPARATE export — the base FIXTURE_TIMELINE
// (and its committed `roast-detail` snapshot) stay untouched. The event carries
// the server's threshold (150 °C); the marker's x is derived from the first shared
// TELEMETRY_POINTS bean reading reaching it (92 + i*8.2 → tick 8 = 157.6 °C at
// elapsed 240 s). monotonic_seconds is an arbitrary server wall-clock, NOT the
// curve x (it is not the placement source — the threshold cross is).
export const FIXTURE_TIMELINE_DRY_END: RoastTimeline = {
  ...FIXTURE_TIMELINE,
  events: [
    ...FIXTURE_TIMELINE.events,
    {
      kind: "drying_end",
      source: "controller",
      monotonic_seconds: 999,
      recorded_at_utc: "2026-06-07T09:16:00Z",
      payload: { bean_temp_c: 157.6, threshold_c: 150 },
    },
  ],
};

// --- Advisor-failure fixture (#170) ------------------------------------------
//
// A roast where the advisor NEVER returned a usable decision — every consult is a
// `provider_error` (the #134 expired-key failure mode), so NO safety evaluation
// was ever produced from advice. The detail page's advisor timeline must render
// these failures (incl. the preheat consult), NOT a blank panel. Reuses the same
// telemetry/detail so the curve still renders; only the timeline differs.

export const FIXTURE_FAILED_RUN_ID = "detail-fixture-failed-001";

export const FIXTURE_DETAIL_FAILED: RoastDetail = {
  ...FIXTURE_DETAIL,
  id: FIXTURE_FAILED_RUN_ID,
  notes: "Advisor key expired mid-roast; controller held policy levels.",
};

export const FIXTURE_TELEMETRY_FAILED: TelemetrySeries = {
  ...FIXTURE_TELEMETRY,
  run_id: FIXTURE_FAILED_RUN_ID,
};

/**
 * Advisor-failure timeline: three consults, all `provider_error`, INCLUDING a
 * preheat consult (tick 1) — no decision payload, no safety evaluation. The
 * advisor timeline must still render three rows with their failure status.
 */
export const FIXTURE_TIMELINE_FAILED: RoastTimeline = {
  run_id: FIXTURE_FAILED_RUN_ID,
  events: [
    { kind: "run_started", source: "controller", monotonic_seconds: 0, recorded_at_utc: "2026-06-07T09:12:00Z", payload: { tick: 0 } },
    { kind: "t0_detected", source: "mcp", monotonic_seconds: 0, recorded_at_utc: "2026-06-07T09:12:30Z", payload: { tick: 1 } },
  ],
  safety_evaluations: [],
  advisor_decisions: [
    {
      tick: 1,
      provider: "openrouter",
      model: "anthropic/claude-opus-4.8",
      prompt_version: "v1",
      latency_ms: 180,
      status: "provider_error",
      decision: null,
      recorded_at_utc: "2026-06-07T09:12:30Z",
    },
    {
      tick: 4,
      provider: "openrouter",
      model: "anthropic/claude-opus-4.8",
      prompt_version: "v1",
      latency_ms: 180,
      status: "provider_error",
      decision: null,
      recorded_at_utc: "2026-06-07T09:14:00Z",
    },
    {
      tick: 8,
      provider: "openrouter",
      model: "anthropic/claude-opus-4.8",
      prompt_version: "v1",
      latency_ms: 180,
      status: "provider_error",
      decision: null,
      recorded_at_utc: "2026-06-07T09:16:00Z",
    },
  ],
  commands: [],
};

// --- Long-roast fixture (#271) -----------------------------------------------
//
// A long roast whose advisor-decisions list and decision-trace table both far
// exceed the inline cap of 5 — the state the cap + "View all" modal exists for.
// Reuses the same telemetry/detail (the curve is incidental here) and synthesizes
// many ALLOW ticks with one CLAMP near the end, so the CLAMP row is among the last
// 5 and stays inline (the `roast-detail-selected` highlight guard, #126, keeps
// working). The full set lives in the modal.

export const FIXTURE_LONG_RUN_ID = "detail-fixture-long-001";

const LONG_TRACE_TICK_COUNT = 24; // > 5 so the cap engages.
// The CLAMP sits at the second-to-last tick → inside the last-5 inline window.
const LONG_CLAMP_TICK = LONG_TRACE_TICK_COUNT - 2;

export const FIXTURE_DETAIL_LONG: RoastDetail = {
  ...FIXTURE_DETAIL,
  id: FIXTURE_LONG_RUN_ID,
  notes: "Long roast — decision lists exceed the inline cap.",
};

// Telemetry long enough that every synthesized trace/advisor tick maps to an
// `elapsed_seconds` (so a selected row — inline or modal — has a curve x-position).
const LONG_TELEMETRY_POINTS: TelemetryPoint[] = Array.from(
  { length: LONG_TRACE_TICK_COUNT + 2 },
  (_, i) => {
    const elapsed = i * 30;
    const phase =
      i < LONG_TRACE_TICK_COUNT - 4
        ? "roasting_pre_first_crack"
        : i < LONG_TRACE_TICK_COUNT
          ? "development"
          : "cooling";
    return {
      tick: i,
      elapsed_seconds: elapsed,
      // #308: charge-referenced clock; mirror elapsed (this fixture is post-charge).
      charge_elapsed_seconds: elapsed,
      agent_phase: phase,
      bean_temp_c: 92 + i * 5.2,
      env_temp_c: 120 + i * 4.0,
      bean_ror_c_per_min: Math.max(3, 16 - i * 0.4),
      env_ror_c_per_min: Math.max(2, 14 - i * 0.35),
      heat_level_percent: 70,
      fan_level_percent: 45,
      cooling_on: i >= LONG_TRACE_TICK_COUNT,
      development_percent: i >= LONG_TRACE_TICK_COUNT - 4 ? (i - (LONG_TRACE_TICK_COUNT - 4)) * 4 : null,
    };
  },
);

export const FIXTURE_TELEMETRY_LONG: TelemetrySeries = {
  run_id: FIXTURE_LONG_RUN_ID,
  downsample: 1,
  point_count: LONG_TELEMETRY_POINTS.length,
  points: LONG_TELEMETRY_POINTS,
};

export const FIXTURE_TIMELINE_LONG: RoastTimeline = {
  run_id: FIXTURE_LONG_RUN_ID,
  events: FIXTURE_TIMELINE.events,
  safety_evaluations: Array.from({ length: LONG_TRACE_TICK_COUNT }, (_, i) => {
    const isClamp = i === LONG_CLAMP_TICK;
    const recordedAt = `2026-06-07T09:${String(12 + i).padStart(2, "0")}:00Z`;
    return isClamp
      ? {
          tick: i,
          rule: "bounds",
          verdict: "clamp" as const,
          input_heat: 105,
          input_fan: 40,
          adjusted_heat: 100,
          adjusted_fan: 40,
          reason:
            "requested heat 105 % / fan 40 % outside the control box (heat 0–100 %, fan 0–100 %): clamped to heat 100 % / fan 40 %",
          recorded_at_utc: recordedAt,
        }
      : {
          tick: i,
          rule: "rate_limit",
          verdict: "allow" as const,
          input_heat: 70,
          input_fan: 45,
          adjusted_heat: 70,
          adjusted_fan: 45,
          reason: "within limits",
          recorded_at_utc: recordedAt,
        };
  }),
  advisor_decisions: Array.from({ length: LONG_TRACE_TICK_COUNT }, (_, i) => {
    const isClamp = i === LONG_CLAMP_TICK;
    const recordedAt = `2026-06-07T09:${String(12 + i).padStart(2, "0")}:00Z`;
    return {
      tick: i,
      provider: "openrouter",
      model: "anthropic/claude-opus-4.8",
      prompt_version: "v1",
      latency_ms: 800 + i,
      status: "ok" as const,
      decision: {
        target_heat: isClamp ? 105 : 70,
        target_fan: isClamp ? 40 : 45,
        should_drop: false,
        confidence: 0.8,
        rationale: isClamp
          ? "Pushing heat near first crack to hold momentum into development."
          : `Holding heat; RoR tracking the profile (tick ${i}).`,
      },
      recorded_at_utc: recordedAt,
    };
  }),
  commands: [],
};
