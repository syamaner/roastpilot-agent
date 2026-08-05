/**
 * Config save-model helpers (#419, PR2 + PR3).
 *
 * Pure functions — no React, no side effects. Consumed by ConfigPage's
 * useReducer and by the PUT body builder.
 *
 * Safety fields are never included in the edit object sent to PUT /api/config
 * (D78 decision 2 + the server enforces this via AppConfigEdit which has no
 * safety key). The `buildEditFromDirty` function enforces this on the FE side
 * too: it skips any field with `editKey: null` (read-only or safety).
 *
 * Array values (recording_devices) require element-wise equality rather than
 * reference equality. `valuesEqual` centralises this so dirty detection is
 * consistent across buildEditFromDirty, the save-bar count, and dirty-dot logic.
 */

import type { AppConfigSnapshot, ConfigFieldMeta } from "@/lib/types";

import { CONFIG_FIELD_MAP } from "./configSchema";

/** Flat map of all field keys → their current effective value. */
export type ConfigValues = Record<string, unknown>;

/**
 * Extract the flat `ConfigValues` map from a server snapshot.
 *
 * Each field key (e.g. "controller.pre_fc_heat_target_percent") is resolved
 * via dot-path into the snapshot and its `effective_value` is used as the
 * starting point for the form.
 */
export function buildValuesFromSnapshot(snapshot: AppConfigSnapshot): ConfigValues {
  const values: ConfigValues = {};
  for (const key of Object.keys(CONFIG_FIELD_MAP)) {
    const meta = resolveFieldMeta(snapshot, key);
    values[key] = meta?.effective_value ?? null;
  }
  return values;
}

/**
 * Resolve the `ConfigFieldMeta` for a dot-path field key from the snapshot.
 *
 * The snapshot has three nested sections (controller/advisor/safety); the
 * key's first segment names the section, the rest is the field name within.
 */
export function resolveFieldMeta(
  snapshot: AppConfigSnapshot,
  key: string,
): ConfigFieldMeta | null {
  const parts = key.split(".");
  if (parts.length < 2) return null;
  const [section, ...rest] = parts;
  const fieldName = rest.join(".");
  type Section = Record<string, ConfigFieldMeta>;
  const sectionObj = (snapshot as unknown as Record<string, Section>)[section!];
  if (!sectionObj) return null;
  return sectionObj[fieldName] ?? null;
}

/**
 * Deep-equality for config values.
 *
 * String arrays (recording_devices) are equal when they have the same length
 * and identical element-wise content. All other values fall back to `===`.
 *
 * This is intentionally narrow — the config schema only produces scalars and
 * string arrays; we don't need a generic deep-equal.
 */
export function valuesEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (Array.isArray(a) && Array.isArray(b)) {
    if (a.length !== b.length) return false;
    for (let i = 0; i < a.length; i++) {
      if (a[i] !== b[i]) return false;
    }
    return true;
  }
  return false;
}

/**
 * Set a value at a dot-path inside a mutable nested object, creating
 * intermediate objects as needed.
 *
 * e.g. setPath(obj, "pre_first_crack_levers.late_maillard_trim.enabled", true)
 * produces { pre_first_crack_levers: { late_maillard_trim: { enabled: true } } }
 *
 * Every segment is guarded against the three property names that can walk the
 * prototype chain (`__proto__`, `constructor`, `prototype`); a forbidden
 * segment throws. All editKeys are compile-time constants from
 * `CONFIG_FIELD_MAP`, so this is unreachable in practice — the guard exists so
 * the dynamic-key assignment can never become a prototype-pollution sink.
 *
 * The guard is written as explicit strict-equality comparisons rather than a
 * `Set.has()` membership test. The two are equivalent at runtime, but CodeQL's
 * `js/prototype-pollution-utility` query recognises an `EqualityTest` barrier
 * and does not recognise the Set lookup, so the Set form left issue #683's
 * alert #10 open despite the guard being correct. Exported for direct
 * behaviour tests of the guard, which `buildEditFromDirty` cannot reach.
 */
export function setPath(
  target: Record<string, unknown>,
  path: string,
  value: unknown,
): void {
  const parts = path.split(".");
  let node: Record<string, unknown> = target;
  for (let i = 0; i < parts.length; i++) {
    const key = parts[i]!;
    // Explicit `===` guard (not Set.has) so CodeQL can prove these dynamic-key
    // assignments cannot reach Object.prototype (#683 alert #10).
    if (key === "__proto__" || key === "constructor" || key === "prototype") {
      throw new Error(`setPath: forbidden key segment "${key}"`);
    }
    if (i === parts.length - 1) {
      node[key] = value;
      return;
    }
    if (typeof node[key] !== "object" || node[key] === null) {
      node[key] = {};
    }
    node = node[key] as Record<string, unknown>;
  }
}

/**
 * Build the PUT /api/config body from dirty fields.
 *
 * Uses `ConfigFieldDef.editKey` to map each flat snapshot key to its correct
 * nested location in `AppConfigEdit`:
 *
 *   advisor.model_slug
 *     → advisor.model_slug (flat)
 *   controller.pre_fc_heat_target_percent
 *     → controller.pre_first_crack_levers.heat_target_percent
 *   controller.late_maillard_trim_enabled
 *     → controller.pre_first_crack_levers.late_maillard_trim.enabled
 *
 * Fields with `editKey: null` (safety, hardware-pinned) are skipped.
 * Fields with `meta.read_only: true` from the server are also skipped
 * (defense-in-depth; the server enforces this too).
 *
 * Only sections with dirty writable fields are included: if only advisor
 * fields changed, the controller key is omitted from the result.
 *
 * **Tri-state inherit/override (#439):** `mcp_device.*` fields support
 * `null` as a first-class value meaning "inherit from hand-authored yaml".
 * When a field is cleared from a value back to `null`, an explicit `null` is
 * included in the PUT body so the backend knows to delete the saved key and
 * stop overriding the hand-authored yaml. The backend uses
 * `model_fields_set` to distinguish explicitly-null (clear) from absent
 * (skip). Non-null values are written as overrides as before.
 */
export function buildEditFromDirty(
  values: ConfigValues,
  saved: ConfigValues,
  snapshot: AppConfigSnapshot,
): Record<string, unknown> {
  const controller: Record<string, unknown> = {};
  const advisor: Record<string, unknown> = {};
  // mcp_device uses explicit null for "clear to inherit" (#439): track which
  // keys were explicitly set (even when null) so the backend can distinguish
  // "operator cleared this field" from "field was not touched".
  const mcp_device: Record<string, unknown> = {};
  let mcpDeviceHasDirty = false;

  for (const [key, current] of Object.entries(values)) {
    if (valuesEqual(current, saved[key])) continue;  // not dirty (array-aware)
    const def = CONFIG_FIELD_MAP[key];
    if (!def || def.editKey === null) continue;    // read-only or safety — never send
    const meta = resolveFieldMeta(snapshot, key);
    if (meta?.read_only) continue;                 // server also says read-only

    const section = key.split(".")[0];
    if (section === "controller") {
      setPath(controller, def.editKey, current);
    } else if (section === "advisor") {
      setPath(advisor, def.editKey, current);
    } else if (section === "mcp_device") {
      // Include null explicitly: null means "clear back to inherit from yaml"
      // (#439). setPath on a null value with a simple (non-nested) editKey
      // writes the null into the mcp_device object so it appears in the
      // JSON PUT body and the backend can honour it via model_fields_set.
      setPath(mcp_device, def.editKey, current ?? null);
      mcpDeviceHasDirty = true;
    }
    // "safety" is intentionally omitted (editKey is null for all safety fields)
    // "mcp_device._mic_test" is omitted (editKey is null; button is not a field)
  }

  const edit: Record<string, unknown> = {};
  if (Object.keys(controller).length > 0) edit.controller = controller;
  if (Object.keys(advisor).length > 0) edit.advisor = advisor;
  if (mcpDeviceHasDirty) edit.mcp_device = mcp_device;
  return edit;
}
