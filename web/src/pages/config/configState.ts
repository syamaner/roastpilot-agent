/**
 * Config save-model helpers (#419, PR2).
 *
 * Pure functions — no React, no side effects. Consumed by ConfigPage's
 * useReducer and by the PUT body builder.
 *
 * Safety fields are never included in the edit object sent to PUT /api/config
 * (D78 decision 2 + the server enforces this via AppConfigEdit which has no
 * safety key). The `buildEditFromDirty` function enforces this on the FE side
 * too: it skips any field with `readOnlyStatic: true`.
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
 * Build the PUT /api/config body from dirty fields.
 *
 * Only includes non-read-only fields whose value has changed from the saved
 * baseline. Organises fields back into the server's nested structure
 * (controller / advisor), matching AppConfigEdit's shape.
 *
 * Safety fields (readOnlyStatic: true) are never included — the server also
 * enforces this; this is defense-in-depth on the FE side.
 */
export function buildEditFromDirty(
  values: ConfigValues,
  saved: ConfigValues,
  snapshot: AppConfigSnapshot,
): Record<string, unknown> {
  const controller: Record<string, unknown> = {};
  const advisor: Record<string, unknown> = {};

  for (const [key, current] of Object.entries(values)) {
    if (current === saved[key]) continue;          // not dirty
    const def = CONFIG_FIELD_MAP[key];
    if (!def || def.readOnlyStatic) continue;      // read-only — never send
    const meta = resolveFieldMeta(snapshot, key);
    if (meta?.read_only) continue;                 // server also says read-only

    const parts = key.split(".");
    const [section, ...rest] = parts;
    const fieldName = rest.join("_"); // controller.pre_fc_heat_target_percent → pre_fc_heat_target_percent
    if (section === "controller") controller[fieldName] = current;
    if (section === "advisor") advisor[fieldName] = current;
    // "safety" is intentionally omitted
  }

  const edit: Record<string, unknown> = {};
  if (Object.keys(controller).length > 0) edit.controller = controller;
  if (Object.keys(advisor).length > 0) edit.advisor = advisor;
  return edit;
}
