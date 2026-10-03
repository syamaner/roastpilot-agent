/**
 * Adapter tests for the closed cold observation contract (#954 U3: P1-P5).
 *
 * Expected values are literals authored from the contract, never computed by the
 * code under test.
 */

import { describe, expect, it } from "vitest";

import {
  admitHeartbeat,
  admitObservation,
  COLD_EVENTS_PATH,
  coldEventsUrl,
  isColdEventId,
} from "./coldObservation";

const ID = "0123456789abcdef-1";

const CANONICAL = {
  bean_temp_c: 21.5,
  cold_phase: "recording_on",
  cooling_on: false,
  device_reported: true,
  env_temp_c: 22.25,
  fan_percent: null,
  heat_percent: 0,
  recorded_at_utc: "2026-01-01T00:00:01.250000+00:00",
  roast_fan_outcome: "observed",
  roast_fan_percent: 40,
  schema_version: 1,
} as const;

function frame(overrides: Record<string, unknown> = {}): string {
  return JSON.stringify({ ...CANONICAL, ...overrides });
}

function without(key: string): string {
  const copy: Record<string, unknown> = { ...CANONICAL };
  delete copy[key];
  return JSON.stringify(copy);
}

describe("admitObservation — P1 canonical", () => {
  it("admits a canonical frame with exact fields", () => {
    expect(admitObservation(frame(), ID)).toEqual({
      schema_version: 1,
      cold_phase: "recording_on",
      recorded_at_utc: "2026-01-01T00:00:01.250000+00:00",
      device_reported: true,
      bean_temp_c: 21.5,
      env_temp_c: 22.25,
      heat_percent: 0,
      fan_percent: null,
      roast_fan_outcome: "observed",
      roast_fan_percent: 40,
      cooling_on: false,
    });
  });

  it("returns a fresh object, not the parsed input", () => {
    const first = admitObservation(frame(), ID);
    const second = admitObservation(frame(), ID);
    expect(first).not.toBe(second);
  });
});

describe("admitObservation — P2 refusals", () => {
  const refused: [string, unknown][] = [
    ["missing key", without("cooling_on")],
    ["extra key", frame({ extra: 1 })],
    [
      "own __proto__ key",
      `{"__proto__":1,${frame().slice(1, -1).replace(/"cooling_on":false,?/, "")}}`,
    ],
    ["array root", "[]"],
    ["null root", "null"],
    ["number root", "1"],
    ["string root", '"x"'],
    ["non-JSON", "{not json"],
    ["empty data", ""],
    ["non-string data", { ...CANONICAL }],
    ["oversize data", frame({ recorded_at_utc: `2026-01-01T00:00:01${"0".repeat(4100)}+00:00` })],
    ["schema_version 2", frame({ schema_version: 2 })],
    ["schema_version string", frame({ schema_version: "1" })],
    ["unknown phase", frame({ cold_phase: "roasting" })],
    ["inherited phase name", frame({ cold_phase: "toString" })],
    ["unknown outcome", frame({ roast_fan_outcome: "measured" })],
    ["inherited outcome name", frame({ roast_fan_outcome: "constructor" })],
    ["main fan value", frame({ fan_percent: 50 })],
    ["main fan zero", frame({ fan_percent: 0 })],
    ["percent with non-observed outcome", frame({ roast_fan_outcome: "unreadable" })],
    ["observed 101", frame({ roast_fan_percent: 101 })],
    ["observed -1", frame({ roast_fan_percent: -1 })],
    ["observed 1.5", frame({ roast_fan_percent: 1.5 })],
    ["observed string", frame({ roast_fan_percent: "40" })],
    ["unsafe heat integer", frame({ heat_percent: 9007199254740992 })],
    ["fractional heat", frame({ heat_percent: 0.5 })],
    ["string temperature", frame({ bean_temp_c: "21.5" })],
    ["boolean temperature", frame({ env_temp_c: true })],
    ["infinite temperature 1e999", frame().replace('"bean_temp_c":21.5', '"bean_temp_c":1e999')],
    ["string cooling", frame({ cooling_on: "true" })],
    ["numeric cooling", frame({ cooling_on: 0 })],
    ["string device_reported", frame({ device_reported: "true" })],
    [
      "device absent with bean",
      frame({ device_reported: false, env_temp_c: null, heat_percent: null, cooling_on: null }),
    ],
    [
      "device absent with env",
      frame({ device_reported: false, bean_temp_c: null, heat_percent: null, cooling_on: null }),
    ],
    [
      "device absent with heat",
      frame({ device_reported: false, bean_temp_c: null, env_temp_c: null, cooling_on: null }),
    ],
    [
      "device absent with cooling",
      frame({ device_reported: false, bean_temp_c: null, env_temp_c: null, heat_percent: null }),
    ],
    ["empty UTC", frame({ recorded_at_utc: "" })],
    ["non-string UTC", frame({ recorded_at_utc: 1 })],
    ["UTC over 2048 UTF-8 bytes", frame({ recorded_at_utc: "é".repeat(1025) })],
    ["UTC with NUL", frame({ recorded_at_utc: "2026-01-01T00:00:01\u0000+00:00" })],
    ["UTC with newline", frame({ recorded_at_utc: "2026-01-01T00:00:01\n+00:00" })],
    ["UTC with DEL", frame({ recorded_at_utc: "2026-01-01T00:00:01\u007f+00:00" })],
    ["UTC with C1", frame({ recorded_at_utc: "2026-01-01T00:00:01\u0085+00:00" })],
    ["UTC with RLO", frame({ recorded_at_utc: "2026-01-01T00:00:01\u202e+00:00" })],
    ["UTC with LRE", frame({ recorded_at_utc: "\u202a2026-01-01T00:00:01+00:00" })],
    ["UTC with FSI", frame({ recorded_at_utc: "2026-01-01T00:00:01\u2068+00:00" })],
    ["UTC with PDI", frame({ recorded_at_utc: "2026-01-01T00:00:01\u2069+00:00" })],
  ];

  it.each(refused)("refuses %s", (_label, data) => {
    expect(admitObservation(data, ID)).toBeNull();
  });

  it("the oversize case is refused by length, not by its content", () => {
    // 4096 characters of an otherwise valid frame (padding with JSON whitespace).
    const base = frame();
    const atLimit = base + " ".repeat(4096 - base.length);
    expect(atLimit.length).toBe(4096);
    expect(admitObservation(atLimit, ID)).not.toBeNull();
    expect(admitObservation(`${atLimit} `, ID)).toBeNull();
  });

  it("an own __proto__ key is an own key and is refused by the exact key set", () => {
    const withProto = `{"__proto__":1,${frame().slice(1)}`;
    expect(Object.keys(JSON.parse(withProto) as object)).toContain("__proto__");
    expect(admitObservation(withProto, ID)).toBeNull();
  });
});

describe("admitObservation — P2b admitted variants", () => {
  it.each([
    ["observed with null percent", { roast_fan_percent: null }],
    ["observed 0", { roast_fan_percent: 0 }],
    ["observed 100", { roast_fan_percent: 100 }],
    ["UTC with Z", { recorded_at_utc: "2026-01-01T00:00:03Z" }],
    ["UTC without micros", { recorded_at_utc: "2026-01-01T00:00:02+00:00" }],
    ["verbatim arbitrary UTC text", { recorded_at_utc: "not parsed by the view" }],
    ["UTC at exactly 2048 UTF-8 bytes", { recorded_at_utc: "é".repeat(1024) }],
    ["heat max safe", { heat_percent: 9007199254740991 }],
    ["heat min safe", { heat_percent: -9007199254740991 }],
    ["negative temperature", { bean_temp_c: -40.5 }],
    ["null temperatures", { bean_temp_c: null, env_temp_c: null }],
    ["cooling on", { cooling_on: true }],
    ["cooling null with device", { cooling_on: null }],
    [
      "device absent with canonical nulls",
      {
        device_reported: false,
        bean_temp_c: null,
        env_temp_c: null,
        heat_percent: null,
        cooling_on: null,
      },
    ],
    ["not eligible without percent", { roast_fan_outcome: "not_eligible", roast_fan_percent: null }],
    ["recording off", { cold_phase: "recording_off" }],
  ])("admits %s", (_label, overrides) => {
    const admitted = admitObservation(frame(overrides), ID);
    expect(admitted).toEqual({ ...CANONICAL, ...overrides });
  });

  it("keeps the UTC verbatim (no parse, no reformat)", () => {
    const admitted = admitObservation(frame({ recorded_at_utc: "2026-01-01T00:00:03Z" }), ID);
    expect(admitted?.recorded_at_utc).toBe("2026-01-01T00:00:03Z");
  });
});

describe("event IDs — P3", () => {
  it.each([
    ["absent", undefined],
    ["null", null],
    ["empty", ""],
    ["upper-case hex", "0123456789ABCDEF-1"],
    ["sequence 0", "0123456789abcdef-0"],
    ["leading zero", "0123456789abcdef-01"],
    ["17 digits (34 characters)", "0123456789abcdef-12345678901234567"],
    ["short epoch", "0123456789abcde-1"],
    ["no dash", "0123456789abcdef1"],
    ["trailing newline", "0123456789abcdef-1\n"],
    ["number", 1],
    ["object", { toString: () => ID }],
  ])("refuses %s", (_label, id) => {
    expect(isColdEventId(id)).toBe(false);
    expect(admitObservation(frame(), id)).toBeNull();
  });

  it("admits the longest valid ID (33 characters) and returns the frame", () => {
    const longest = "0123456789abcdef-9999999999999999";
    expect(longest).toHaveLength(33);
    expect(isColdEventId(longest)).toBe(true);
    expect(admitObservation(frame(), longest)).not.toBeNull();
  });
});

describe("coldEventsUrl — P4", () => {
  it("is the bare path for a fresh stream", () => {
    expect(COLD_EVENTS_PATH).toBe("/api/cold-characterisation/events");
    expect(coldEventsUrl(null)).toBe("/api/cold-characterisation/events");
  });

  it("carries the exact ID string, encoded", () => {
    expect(coldEventsUrl("0123456789abcdef-9999999999999999")).toBe(
      "/api/cold-characterisation/events?last_event_id=0123456789abcdef-9999999999999999",
    );
    expect(coldEventsUrl("a b&c=d")).toBe(
      "/api/cold-characterisation/events?last_event_id=a%20b%26c%3Dd",
    );
  });
});

describe("admitHeartbeat — P5", () => {
  it("admits exactly {}", () => {
    expect(admitHeartbeat("{}")).toBe(true);
  });

  it.each([["{ }"], ["{}\n"], ["[]"], [""], ["null"], ['{"a":1}']])("refuses %j", (data) => {
    expect(admitHeartbeat(data)).toBe(false);
  });

  it("refuses non-string data", () => {
    expect(admitHeartbeat({})).toBe(false);
    expect(admitHeartbeat(undefined)).toBe(false);
  });
});
