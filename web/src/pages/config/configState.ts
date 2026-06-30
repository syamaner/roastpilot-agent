/**
 * Config save-model helpers (#419, PR2).
 *
 * Pure functions — no React, no side effects. Consumed by ConfigPage's
 * useReducer and by the PUT body builder.
 *
 * Safety fields are never included in the edit object sent to PUT /api/config
 * (D78 decision 2 + the server enforces this via AppConfigEdit which has no
 * safety key). The `buildEditFromDirty` function enforces this on the FE side
 * too: it skips any field with `editKey: null` (read-only or safety).
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

// Segments that could mutate Object.prototype — never present in our hardcoded
// editKeys, but guard explicitly so static-analysis tools (CodeQL) don't flag
// the assignment pattern as a prototype-pollution sink.
const FORBIDDEN_KEY_SEGMENTS = new Set(["__proto__", "constructor", "prototype"]);

/**
 * Set a value at a dot-path inside a mutable nested object, creating
 * intermediate objects as needed.
 *
 * e.g. setPath(obj, "pre_first_crack_levers.late_maillard_trim.enabled", true)
 * produces { pre_first_crack_levers: { late_maillard_trim: { enabled: true } } }
 *
 * Throws if any segment is a forbidden prototype key — all editKeys are
 * compile-time constants so this is unreachable in practice.
 */
function setPath(
  target: Record<string, unknown>,
  path: string,
  value: unknown,
): void {
  const parts = path.split(".");
  let node: Record<string, unknown> = target;
  for (let i = 0; i < parts.length - 1; i++) {
    const key = parts[i]!;
    if (FORBIDDEN_KEY_SEGMENTS.has(key)) {
      throw new Error(`setPath: forbidden key segment "${key}"`);
    }
    if (typeof node[key] !== "object" || node[key] === null) {
      node[key] = {};
    }
    node = node[key] as Record<string, unknown>;
  }
  const lastKey = parts[parts.length - 1]!;
  if (FORBIDDEN_KEY_SEGMENTS.has(lastKey)) {
    throw new Error(`setPath: forbidden key segment "${lastKey}"`);
  }
  node[lastKey] = value;
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
 */
export function buildEditFromDirty(
  values: ConfigValues,
  saved: ConfigValues,
  snapshot: AppConfigSnapshot,
): Record<string, unknown> {
  const controller: Record<string, unknown> = {};
  const advisor: Record<string, unknown> = {};
  const mcp_device: Record<string, unknown> = {};

  for (const [key, current] of Object.entries(values)) {
    if (current === saved[key]) continue;          // not dirty
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
      setPath(mcp_device, def.editKey, current);
    }
    // "safety" is intentionally omitted (editKey is null for all safety fields)
    // "mcp_device._mic_test" is omitted (editKey is null; button is not a field)
  }

  const edit: Record<string, unknown> = {};
  if (Object.keys(controller).length > 0) edit.controller = controller;
  if (Object.keys(advisor).length > 0) edit.advisor = advisor;
  if (Object.keys(mcp_device).length > 0) edit.mcp_device = mcp_device;
  return edit;
}
