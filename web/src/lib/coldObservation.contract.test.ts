/// <reference types="node" />
/**
 * Cold observation contract — the SPA half of the #954 U3 bridge (C1).
 *
 * Loads the committed frames dumped by `tests/test_cold_observation_contract_fixture.py`
 * from the REAL U2 projection and renderer (never hand-authored) and asserts the
 * real adapter admits every one, including the labelled `public_model_only` case.
 * In-memory single-field mutations of real frames must be refused.
 */

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { admitHeartbeat, admitObservation } from "./coldObservation";

interface FixtureFrame {
  name: string;
  source: "retained_tick" | "public_model_only" | "heartbeat";
  frame_text: string;
  id: string | null;
  event: "observation" | "heartbeat";
  data: string;
}

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURE_PATH = resolve(HERE, "../../../tests/fixtures/contract/cold_observation_frames.json");
const fixture = JSON.parse(readFileSync(FIXTURE_PATH, "utf8")) as {
  epoch: string;
  frames: FixtureFrame[];
};

const observations = fixture.frames.filter((frame) => frame.event === "observation");
const heartbeats = fixture.frames.filter((frame) => frame.event === "heartbeat");

describe("cold contract fixture", () => {
  it("carries retained ticks, one public-model-only case and the heartbeat", () => {
    expect(observations.map((frame) => frame.name)).toEqual([
      "canonical_isoformat_micros",
      "utc_without_micros",
      "utc_zulu",
      "device_absent",
      "device_temperatures_null",
      "heat_max_exact_int",
      "heat_min_exact_int",
      "heat_beyond_exact_int_projects_null",
      "outcome_observed",
      "outcome_not_eligible",
      "outcome_unsupported",
      "outcome_unreadable",
      "outcome_malformed",
      "observed_level_0",
      "observed_level_100",
      "cooling_on_true",
      "cooling_on_false",
      "observed_null_level",
    ]);
    expect(heartbeats).toHaveLength(1);
    expect(
      fixture.frames.filter((frame) => frame.source === "public_model_only").map((f) => f.name),
    ).toEqual(["observed_null_level"]);
  });

  it.each(observations.map((frame) => [frame.name, frame] as const))(
    "admits the real %s frame with exactly its wire fields",
    (_name, frame) => {
      const admitted = admitObservation(frame.data, frame.id);
      expect(admitted).not.toBeNull();
      expect(admitted).toEqual(JSON.parse(frame.data));
      expect(frame.frame_text).toBe(
        `id: ${frame.id}\nevent: observation\ndata: ${frame.data}\n\n`,
      );
    },
  );

  it("admits the real heartbeat bytes", () => {
    const [heartbeat] = heartbeats;
    expect(heartbeat.frame_text).toBe("event: heartbeat\ndata: {}\n\n");
    expect(heartbeat.id).toBeNull();
    expect(admitHeartbeat(heartbeat.data)).toBe(true);
  });

  const mutations: [string, (record: Record<string, unknown>) => void][] = [
    ["extra key", (r) => void (r.extra = null)],
    ["missing key", (r) => void delete r.cold_phase],
    ["schema_version 2", (r) => void (r.schema_version = 2)],
    ["unknown phase", (r) => void (r.cold_phase = "cooling")],
    ["non-string UTC", (r) => void (r.recorded_at_utc = 0)],
    ["string device_reported", (r) => void (r.device_reported = "true")],
    ["string bean", (r) => void (r.bean_temp_c = "21.5")],
    ["string env", (r) => void (r.env_temp_c = "22.25")],
    ["fractional heat", (r) => void (r.heat_percent = 0.5)],
    ["main fan value", (r) => void (r.fan_percent = 50)],
    ["unknown outcome", (r) => void (r.roast_fan_outcome = "measured")],
    ["roast fan 101", (r) => void ((r.roast_fan_outcome = "observed"), (r.roast_fan_percent = 101))],
    ["string cooling", (r) => void (r.cooling_on = "false")],
  ];

  it.each(
    observations.flatMap((frame) =>
      mutations.map(([label, mutate]) => [frame.name, label, frame, mutate] as const),
    ),
  )("refuses %s mutated by %s", (_name, _label, frame, mutate) => {
    const record = JSON.parse(frame.data) as Record<string, unknown>;
    mutate(record);
    expect(admitObservation(JSON.stringify(record), frame.id)).toBeNull();
  });

  it.each(observations.map((frame) => [frame.name, frame] as const))(
    "refuses the real %s frame under a malformed ID",
    (_name, frame) => {
      expect(admitObservation(frame.data, `${frame.id}0`.replace(/-.*/, "-0"))).toBeNull();
      expect(admitObservation(frame.data, null)).toBeNull();
    },
  );
});
