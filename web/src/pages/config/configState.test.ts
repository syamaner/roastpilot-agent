/**
 * configState — save-model helpers (#419, slice 3c).
 *
 * Tests assert real behaviour:
 *  1. buildValuesFromSnapshot extracts effective_value for all sections
 *     including mcp_device.
 *  2. buildEditFromDirty includes mcp_device section when mcp_device fields
 *     are dirty.
 *  3. buildEditFromDirty omits mcp_device section when no mcp_device fields
 *     are dirty.
 *  4. buildEditFromDirty omits micTestButton pseudo-field (editKey: null).
 *  5. buildEditFromDirty omits fields where the server's read_only=true.
 *  6. buildEditFromDirty handles recording_devices as an array value.
 */

import { describe, expect, it } from "vitest";

import type { AppConfigSnapshot, ConfigFieldMeta } from "@/lib/types";
import { buildEditFromDirty, buildValuesFromSnapshot, valuesEqual } from "./configState";

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

function makeFieldMeta(overrides?: Partial<ConfigFieldMeta>): ConfigFieldMeta {
  return {
    saved_value: null,
    effective_value: null,
    default: null,
    env_overridden: false,
    read_only: false,
    description: "",
    ...overrides,
  };
}

/** Minimal AppConfigSnapshot with all required sections. */
function makeSnapshot(overrides?: {
  serial_port?: string | null;
  audio_input_device?: string | null;
  recording_devices?: string[] | null;
  fc_mode?: string | null;
  fc_confidence_threshold?: number | null;
  roaster_driver?: string | null;
}): AppConfigSnapshot {
  const o = {
    serial_port: null,
    audio_input_device: null,
    recording_devices: null,
    fc_mode: null,
    fc_confidence_threshold: null,
    roaster_driver: null,
    ...overrides,
  };
  return {
    controller: {
      tick_interval_seconds: makeFieldMeta({ effective_value: 1.0, default: 1.0, read_only: true }),
      pre_fc_heat_target_percent: makeFieldMeta({ effective_value: 100, default: 100 }),
      pre_fc_fan_target_percent: makeFieldMeta({ effective_value: 30, default: 30 }),
      late_maillard_trim_enabled: makeFieldMeta({ effective_value: true, default: true }),
      late_maillard_trim_heat_percent: makeFieldMeta({ effective_value: 65, default: 65 }),
      late_maillard_trim_window_fc_eta_seconds: makeFieldMeta({ effective_value: 60.0, default: 60.0 }),
      late_maillard_trim_min_bean_temp_c: makeFieldMeta({ effective_value: 155.0, default: 155.0 }),
      late_maillard_trim_adaptive_depth_enabled: makeFieldMeta({ effective_value: false, default: false }),
      late_maillard_trim_base_trim: makeFieldMeta({ effective_value: 65, default: 65 }),
      late_maillard_trim_k_ror: makeFieldMeta({ effective_value: 1.5, default: 1.5 }),
      late_maillard_trim_k_eta: makeFieldMeta({ effective_value: 0.2, default: 0.2 }),
      late_maillard_trim_ror_ref: makeFieldMeta({ effective_value: 8.0, default: 8.0 }),
      late_maillard_trim_eta_ref: makeFieldMeta({ effective_value: 60.0, default: 60.0 }),
      late_maillard_trim_min_trim: makeFieldMeta({ effective_value: 45, default: 45 }),
      late_maillard_trim_max_trim: makeFieldMeta({ effective_value: 75, default: 75 }),
    },
    advisor: {
      model_slug: makeFieldMeta({ effective_value: "openai/gpt-4o", default: "openai/gpt-4o" }),
      prompt_version: makeFieldMeta({ effective_value: "c3", default: "c3" }),
      provider: makeFieldMeta({ effective_value: "openai_compatible", default: "openai_compatible", read_only: true }),
      provider_base_url: makeFieldMeta({ effective_value: "https://openrouter.ai/api/v1", default: "https://openrouter.ai/api/v1", read_only: true }),
      api_key_env: makeFieldMeta({ effective_value: "OPENROUTER_API_KEY", read_only: true }),
      timeout_seconds: makeFieldMeta({ effective_value: 10.0, default: 10.0 }),
      temperature: makeFieldMeta({ effective_value: 0.0, default: 0.0 }),
    },
    safety: {
      max_bean_temp_c: makeFieldMeta({ effective_value: 230, default: 230, read_only: true }),
      max_env_temp_c: makeFieldMeta({ effective_value: 240, default: 240, read_only: true }),
      pre_t0_max_bean_temp_c: makeFieldMeta({ effective_value: 200, default: 200, read_only: true }),
      overrun_safe_fan_percent: makeFieldMeta({ effective_value: 100, default: 100, read_only: true }),
      pre_t0_overrun_severity: makeFieldMeta({ effective_value: "recovery", default: "recovery", read_only: true }),
      min_seconds_between_commands: makeFieldMeta({ effective_value: 2.0, default: 2.0, read_only: true }),
      max_consecutive_mcp_failures: makeFieldMeta({ effective_value: 3, default: 3, read_only: true }),
      max_consecutive_advisor_failures: makeFieldMeta({ effective_value: 3, default: 3, read_only: true }),
      bitter_ceiling_temp_c: makeFieldMeta({ effective_value: 196, default: 196, read_only: true }),
      emergency_drop_temp_c: makeFieldMeta({ effective_value: 198, default: 198, read_only: true }),
    },
    mcp_device: {
      serial_port: makeFieldMeta({ effective_value: o.serial_port, default: null }),
      roaster_driver: makeFieldMeta({ effective_value: o.roaster_driver, default: null }),
      audio_input_device: makeFieldMeta({ effective_value: o.audio_input_device, default: null }),
      recording_enabled: makeFieldMeta({ effective_value: null, default: null }),
      recording_autocapture: makeFieldMeta({ effective_value: null, default: null }),
      recording_devices: makeFieldMeta({ effective_value: o.recording_devices, default: null }),
      fc_mode: makeFieldMeta({ effective_value: o.fc_mode, default: null }),
      fc_confidence_threshold: makeFieldMeta({ effective_value: o.fc_confidence_threshold, default: null }),
      auto_t0_detection_enabled: makeFieldMeta({ effective_value: null, default: null }),
      auto_t0_drop_threshold_c: makeFieldMeta({ effective_value: null, default: null }),
    },
  };
}

// ---------------------------------------------------------------------------
// Tests — buildValuesFromSnapshot
// ---------------------------------------------------------------------------

describe("buildValuesFromSnapshot", () => {
  it("extracts effective_value for mcp_device fields", () => {
    const snapshot = makeSnapshot({ serial_port: "/dev/ttyUSB0", fc_mode: "audio" });
    const values = buildValuesFromSnapshot(snapshot);
    expect(values["mcp_device.serial_port"]).toBe("/dev/ttyUSB0");
    expect(values["mcp_device.fc_mode"]).toBe("audio");
  });

  it("stores null for unset mcp_device fields", () => {
    const values = buildValuesFromSnapshot(makeSnapshot());
    expect(values["mcp_device.serial_port"]).toBeNull();
    expect(values["mcp_device.audio_input_device"]).toBeNull();
  });

  it("stores null for the micTestButton pseudo-field (no server entry)", () => {
    // The _mic_test key resolves to null via resolveFieldMeta; effective_value
    // falls back to null.
    const values = buildValuesFromSnapshot(makeSnapshot());
    expect(values["mcp_device._mic_test"]).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Tests — buildEditFromDirty (mcp_device section)
// ---------------------------------------------------------------------------

describe("buildEditFromDirty — mcp_device section", () => {
  it("includes mcp_device section when a device field is dirty", () => {
    const snapshot = makeSnapshot();
    const saved = buildValuesFromSnapshot(snapshot);
    const values = { ...saved, "mcp_device.serial_port": "/dev/ttyUSB0" };

    const edit = buildEditFromDirty(values, saved, snapshot);
    expect(edit.mcp_device).toEqual({ serial_port: "/dev/ttyUSB0" });
  });

  it("includes fc_mode when changed", () => {
    const snapshot = makeSnapshot({ fc_mode: "disabled" });
    const saved = buildValuesFromSnapshot(snapshot);
    const values = { ...saved, "mcp_device.fc_mode": "audio" };

    const edit = buildEditFromDirty(values, saved, snapshot);
    expect((edit.mcp_device as Record<string, unknown>)?.fc_mode).toBe("audio");
  });

  it("includes recording_devices as an array when changed", () => {
    const snapshot = makeSnapshot();
    const saved = buildValuesFromSnapshot(snapshot);
    const values = { ...saved, "mcp_device.recording_devices": ["USB PnP", "Built-in Mic"] };

    const edit = buildEditFromDirty(values, saved, snapshot);
    expect((edit.mcp_device as Record<string, unknown>)?.recording_devices)
      .toEqual(["USB PnP", "Built-in Mic"]);
  });

  it("omits mcp_device section when no device fields are dirty", () => {
    const snapshot = makeSnapshot({ serial_port: "/dev/ttyUSB0" });
    const saved = buildValuesFromSnapshot(snapshot);
    const values = { ...saved };  // identical — not dirty

    const edit = buildEditFromDirty(values, saved, snapshot);
    expect(edit.mcp_device).toBeUndefined();
  });

  it("omits the micTestButton pseudo-field even when its value changes", () => {
    // editKey is null for _mic_test — must never appear in the PUT body.
    const snapshot = makeSnapshot();
    const saved = buildValuesFromSnapshot(snapshot);
    const values = { ...saved, "mcp_device._mic_test": "anything" };

    const edit = buildEditFromDirty(values, saved, snapshot);
    // mcp_device section omitted entirely (only dirty field is the button)
    expect(edit.mcp_device).toBeUndefined();
  });

  it("includes both advisor and mcp_device when both are dirty", () => {
    const snapshot = makeSnapshot();
    const saved = buildValuesFromSnapshot(snapshot);
    const values = {
      ...saved,
      "advisor.model_slug": "anthropic/claude-opus-4-8",
      "mcp_device.roaster_driver": "mock",
    };

    const edit = buildEditFromDirty(values, saved, snapshot);
    expect((edit.advisor as Record<string, unknown>)?.model_slug)
      .toBe("anthropic/claude-opus-4-8");
    expect((edit.mcp_device as Record<string, unknown>)?.roaster_driver)
      .toBe("mock");
  });

  it("respects server read_only=true — skips even if dirty", () => {
    // recording_enabled is read_only=true in this snapshot.
    const snapshot = makeSnapshot();
    // Override one field to be read_only on the server side.
    (snapshot.mcp_device.recording_enabled as ConfigFieldMeta) =
      makeFieldMeta({ effective_value: true, default: null, read_only: true });
    const saved = buildValuesFromSnapshot(snapshot);
    const values = { ...saved, "mcp_device.recording_enabled": false };

    const edit = buildEditFromDirty(values, saved, snapshot);
    // Should not include recording_enabled because the server says read_only.
    expect(edit.mcp_device).toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// Tests — valuesEqual
// ---------------------------------------------------------------------------

describe("valuesEqual", () => {
  it("returns true for identical scalars (===)", () => {
    expect(valuesEqual(42, 42)).toBe(true);
    expect(valuesEqual("USB PnP", "USB PnP")).toBe(true);
    expect(valuesEqual(null, null)).toBe(true);
    expect(valuesEqual(true, true)).toBe(true);
  });

  it("returns false for different scalars", () => {
    expect(valuesEqual(42, 43)).toBe(false);
    expect(valuesEqual("USB PnP", "Built-in Mic")).toBe(false);
    expect(valuesEqual(null, "USB PnP")).toBe(false);
    expect(valuesEqual(true, false)).toBe(false);
  });

  it("returns true for two arrays with identical element-wise content", () => {
    expect(valuesEqual(["USB PnP"], ["USB PnP"])).toBe(true);
    expect(valuesEqual(["USB PnP", "ATR2100"], ["USB PnP", "ATR2100"])).toBe(true);
    expect(valuesEqual([], [])).toBe(true);
  });

  it("returns false for arrays with different content", () => {
    expect(valuesEqual(["USB PnP"], ["ATR2100"])).toBe(false);
    expect(valuesEqual(["USB PnP", "ATR2100"], ["USB PnP"])).toBe(false);
    expect(valuesEqual([], ["USB PnP"])).toBe(false);
  });

  it("returns false for arrays with the same elements in different order", () => {
    // Element-wise means order matters.
    expect(valuesEqual(["A", "B"], ["B", "A"])).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Tests — recording_devices array reference-equality in buildEditFromDirty
// ---------------------------------------------------------------------------

describe("buildEditFromDirty — recording_devices array equality", () => {
  it("does NOT include recording_devices when saved and current have same content (new array reference)", () => {
    // This guards the reference-equality false-positive: React state toggles
    // produce a new array reference even when content is unchanged.
    const snapshot = makeSnapshot({ recording_devices: ["USB PnP"] });
    const saved = buildValuesFromSnapshot(snapshot);
    // Simulate React state creating a new array with the same content.
    const values = { ...saved, "mcp_device.recording_devices": ["USB PnP"] };

    const edit = buildEditFromDirty(values, saved, snapshot);
    // Not dirty (same content) → mcp_device section must be absent.
    expect(edit.mcp_device).toBeUndefined();
  });

  it("DOES include recording_devices when content actually differs", () => {
    const snapshot = makeSnapshot({ recording_devices: ["USB PnP"] });
    const saved = buildValuesFromSnapshot(snapshot);
    const values = {
      ...saved,
      "mcp_device.recording_devices": ["USB PnP", "ATR2100"],
    };

    const edit = buildEditFromDirty(values, saved, snapshot);
    expect((edit.mcp_device as Record<string, unknown>)?.recording_devices)
      .toEqual(["USB PnP", "ATR2100"]);
  });

  it("includes recording_devices when toggling from null to an array", () => {
    const snapshot = makeSnapshot({ recording_devices: null });
    const saved = buildValuesFromSnapshot(snapshot);
    const values = { ...saved, "mcp_device.recording_devices": ["USB PnP"] };

    const edit = buildEditFromDirty(values, saved, snapshot);
    expect((edit.mcp_device as Record<string, unknown>)?.recording_devices)
      .toEqual(["USB PnP"]);
  });
});
