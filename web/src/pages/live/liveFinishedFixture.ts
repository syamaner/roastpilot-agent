/**
 * Deterministic fixtures for the LiveFinishedView snapshot harness (#423).
 *
 * Shared between `LiveFinishedHarnessPage` (the `/__live-finished-harness` route)
 * and `LivePage.test.tsx` (the outcome-content assertions) so both stay in
 * lock-step with the real rendered values. All temperatures Celsius.
 *
 * The fixture represents a short completed roast:
 *   phase  0–9  roasting_pre_first_crack
 *   phase 10–12 development (FC at tick 10)
 *   tick   13   cooling (drop)
 * Telemetry is downsampled to 5 points (matching the LiveFinishedView fetch).
 */

import type { RoastDetail, TelemetryPoint, TelemetrySeries } from "@/lib/types";

export const FIXTURE_FINISHED_RUN_ID = "live-finished-fixture-001";

/**
 * Five downsampled telemetry points covering the full arc:
 * preheat → pre-FC → development → cooling.
 * Elapsed and bean_temp_c are set so headlineStats produces deterministic values.
 */
const TELEMETRY_POINTS: TelemetryPoint[] = [
  {
    tick: 0,
    elapsed_seconds: 0,
    charge_elapsed_seconds: 0,
    agent_phase: "roasting_pre_first_crack",
    bean_temp_c: 155.0,
    env_temp_c: 185.0,
    bean_ror_c_per_min: 14.0,
    env_ror_c_per_min: 12.0,
    heat_level_percent: 80,
    fan_level_percent: 40,
    cooling_on: false,
    development_percent: null,
  },
  {
    tick: 3,
    elapsed_seconds: 90,
    charge_elapsed_seconds: 90,
    agent_phase: "roasting_pre_first_crack",
    bean_temp_c: 172.0,
    env_temp_c: 198.0,
    bean_ror_c_per_min: 12.5,
    env_ror_c_per_min: 10.0,
    heat_level_percent: 75,
    fan_level_percent: 50,
    cooling_on: false,
    development_percent: null,
  },
  {
    tick: 6,
    elapsed_seconds: 180,
    charge_elapsed_seconds: 180,
    agent_phase: "roasting_pre_first_crack",
    bean_temp_c: 185.0,
    env_temp_c: 210.0,
    bean_ror_c_per_min: 10.0,
    env_ror_c_per_min: 8.5,
    heat_level_percent: 70,
    fan_level_percent: 60,
    cooling_on: false,
    development_percent: null,
  },
  {
    // First point in "development" phase → headlineStats uses this for FC seconds + temp.
    tick: 10,
    elapsed_seconds: 300,
    charge_elapsed_seconds: 300,
    agent_phase: "development",
    bean_temp_c: 193.0,
    env_temp_c: 218.0,
    bean_ror_c_per_min: 6.5,
    env_ror_c_per_min: 5.0,
    heat_level_percent: 60,
    fan_level_percent: 70,
    cooling_on: false,
    development_percent: 3.5,
  },
  {
    // First point in "cooling" phase → headlineStats uses this for drop seconds + temp.
    tick: 13,
    elapsed_seconds: 390,
    charge_elapsed_seconds: 390,
    agent_phase: "cooling",
    bean_temp_c: 191.0,
    env_temp_c: 220.0,
    bean_ror_c_per_min: 4.0,
    env_ror_c_per_min: 3.5,
    heat_level_percent: 0,
    fan_level_percent: 100,
    cooling_on: true,
    development_percent: 18.7,
  },
];

export const FIXTURE_FINISHED_TELEMETRY: TelemetrySeries = {
  run_id: FIXTURE_FINISHED_RUN_ID,
  downsample: 5,
  point_count: TELEMETRY_POINTS.length,
  points: TELEMETRY_POINTS,
};

/**
 * Expected headlineStats output for the fixture telemetry — used by tests to
 * assert the rendered tile text without re-deriving the projection.
 *
 * dropTempC: 191.0 → "191 °C"
 * developmentPercent: 18.7 → "18.7 %"   (last point that carries one)
 * totalSeconds: 390 → "6:30"
 * firstCrackSeconds: 300 → first development point
 */
export const FIXTURE_FINISHED_STATS = {
  dropTempDisplay: "191 °C",
  devPercentDisplay: "18.7 %",
  totalTimeDisplay: "6:30",
} as const;

export const FIXTURE_FINISHED_DETAIL: RoastDetail = {
  id: FIXTURE_FINISHED_RUN_ID,
  agent_phase: "complete",
  profile: {
    name: "Colombia Huila — Medium",
    bean_origin: "Colombia, Huila",
    bean_varietal: "Castillo",
    country: "Colombia",
    farm: "El Paraíso",
    description: "Washed; caramel, dark chocolate, citrus brightness.",
    bean_species: "arabica",
    is_blend: false,
    bean_weight_grams: 250,
    charge_guidance_min_c: 170,
    charge_guidance_max_c: 200,
    initial_heat_percent: 80,
    initial_fan_percent: 40,
    target_drop_temp_c: 195,
    target_development_percent: 18,
  },
  outcome: "completed",
  started_at_utc: "2026-07-01T10:00:00Z",
  completed_at_utc: "2026-07-01T10:06:30Z",
  fault_reason: null,
  rating: null,
  notes: null,
  roasted_weight_grams: 213,
  weight_loss_percent: 14.8,
  export_manifest: null,
  enabled_actions: [],
};
