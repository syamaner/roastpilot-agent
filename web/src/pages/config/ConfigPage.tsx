/**
 * /config page — agent configuration view (#419, D78).
 *
 * Two-pane layout (268px category rail + content pane) matching the design
 * handoff spec. Renders from GET /api/config (AppConfigSnapshot) — all values
 * come from the server; the page never derives config values locally.
 *
 * Architecture invariants:
 *  - Phase is never inferred client-side; this page reads only config, not run state.
 *  - Safety fields are always read_only=true in M1 (D78 decision 2); the server
 *    enforces this; the page mirrors the read_only flag per field.
 *  - PUT /api/config excludes safety; the edit object is built from dirty
 *    non-safety fields only.
 *  - Temperatures displayed are Celsius (the schema carries no Fahrenheit values).
 *
 * Split into PR slices:
 *  PR2 (this file): layout + category rail + field rows + save model.
 *  PR3a (3b): env-override badge in ConfigFieldRow.
 *  PR3b (3c, this PR): Hardware/Audio/FC-Detection categories + mic-test placeholder.
 */

import { useCallback, useEffect, useMemo, useReducer, useRef } from "react";

import { AppFrame } from "@/components/shared";
import { useConfig, useSaveConfig } from "@/hooks/queries";
import { cn } from "@/lib/cn";
import type { AppConfigSnapshot } from "@/lib/types";

import { CONFIG_CATEGORIES, CONFIG_FIELD_MAP } from "./configSchema";
import type { FieldCategory } from "./configSchema";
import { ConfigFieldRow } from "./ConfigFieldRow";
import {
  buildEditFromDirty,
  buildValuesFromSnapshot,
  valuesEqual,
  type ConfigValues,
} from "./configState";

// ---------------------------------------------------------------------------
// Save-model state
// ---------------------------------------------------------------------------

interface State {
  /** Active category in the rail. */
  activeCategory: FieldCategory;
  /** Current (possibly-dirty) form values keyed by field key. */
  values: ConfigValues;
  /** Baseline values as of last successful save (or initial load). */
  saved: ConfigValues;
}

type Action =
  | { type: "INIT"; snapshot: AppConfigSnapshot }
  | { type: "SET_CATEGORY"; category: FieldCategory }
  | { type: "SET_VALUE"; key: string; value: unknown }
  | { type: "RESET_FIELD"; key: string; defaultValue: unknown }
  | { type: "DISCARD" };

function reducer(state: State, action: Action): State {
  switch (action.type) {
    case "INIT": {
      const values = buildValuesFromSnapshot(action.snapshot);
      return { ...state, values, saved: values };
    }
    case "SET_CATEGORY":
      return { ...state, activeCategory: action.category };
    case "SET_VALUE":
      return { ...state, values: { ...state.values, [action.key]: action.value } };
    case "RESET_FIELD":
      return {
        ...state,
        values: { ...state.values, [action.key]: action.defaultValue },
      };
    case "DISCARD":
      return { ...state, values: { ...state.saved } };
  }
}

// ---------------------------------------------------------------------------
// Page header
// ---------------------------------------------------------------------------

function ConfigHeader(): React.JSX.Element {
  return (
    <header className="mb-8" data-testid="config-page-header">
      <p className="text-[11px] font-semibold uppercase tracking-widest text-muted-foreground">
        Configuration
      </p>
      <h1 className="mt-1 text-2xl font-semibold tracking-tight text-foreground">
        Settings
      </h1>
      <p className="mt-1 max-w-[62ch] text-sm text-muted-foreground [text-wrap:pretty]">
        Applies to the next roast — not the live loop. Heat, fan and other
        in-roast controls stay on the roast page.
      </p>
    </header>
  );
}

// ---------------------------------------------------------------------------
// Category rail
// ---------------------------------------------------------------------------

interface RailItemProps {
  index: number;
  id: FieldCategory;
  label: string;
  description: string;
  isActive: boolean;
  isDirty: boolean;
  onClick: () => void;
}

function RailItem({
  index,
  id,
  label,
  description,
  isActive,
  isDirty,
  onClick,
}: RailItemProps): React.JSX.Element {
  const isSafety = id === "Safety";
  return (
    <button
      type="button"
      onClick={onClick}
      data-testid={`rail-item-${id}`}
      aria-current={isActive ? "true" : undefined}
      className={cn(
        "flex w-full items-start gap-2 rounded-[9px] px-3 py-[11px] text-left transition-colors",
        isActive
          ? "border border-border bg-secondary text-foreground"
          : "border border-transparent text-muted-foreground hover:bg-white/[.04] hover:text-foreground",
      )}
    >
      {/* Monospace index */}
      <span className="mt-px shrink-0 font-mono text-[11px] text-muted-foreground/60">
        {String(index + 1).padStart(2, "0")}
      </span>

      {/* Name + blurb */}
      <span className="min-w-0 flex-1">
        <span className="flex items-center gap-1">
          <span className={cn("text-sm", isActive ? "font-semibold" : "font-medium")}>
            {label}
          </span>
          {isSafety && (
            <svg
              className="h-3 w-3 shrink-0 text-muted-foreground"
              aria-label="Safety limits — read-only"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <rect x="3" y="11" width="18" height="11" rx="2" ry="2" />
              <path d="M7 11V7a5 5 0 0 1 10 0v4" />
            </svg>
          )}
        </span>
        <span className="block truncate text-xs text-muted-foreground/70">
          {description}
        </span>
      </span>

      {/* Dirty indicator */}
      {isDirty && (
        <span
          className="mt-1 h-1.5 w-1.5 shrink-0 rounded-full bg-roast-caution"
          aria-label="Unsaved changes"
          data-testid={`rail-dirty-${id}`}
        />
      )}
    </button>
  );
}

// ---------------------------------------------------------------------------
// Save bar
// ---------------------------------------------------------------------------

interface SaveBarProps {
  dirtyCount: number;
  saving: boolean;
  saveError: string | null;
  onSave: () => void;
  onDiscard: () => void;
}

function SaveBar({
  dirtyCount,
  saving,
  saveError,
  onSave,
  onDiscard,
}: SaveBarProps): React.JSX.Element | null {
  if (dirtyCount === 0) return null;
  return (
    <div
      className="fixed inset-x-0 bottom-0 z-50 border-t border-border bg-[rgba(26,26,31,.92)] py-3 backdrop-blur-sm"
      data-testid="config-save-bar"
      role="region"
      aria-label="Unsaved changes"
    >
      <div className="mx-auto flex max-w-[1180px] items-center justify-between px-6">
        <span className="flex items-center gap-2 text-sm text-muted-foreground">
          <span
            className="h-1.5 w-1.5 rounded-full bg-roast-caution"
            aria-hidden="true"
          />
          <span>
            Unsaved changes — {dirtyCount} setting{dirtyCount !== 1 ? "s" : ""} modified
          </span>
          {saveError && (
            <span className="ml-2 text-roast-fault" data-testid="config-save-error">
              {saveError}
            </span>
          )}
        </span>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={onDiscard}
            disabled={saving}
            className="h-10 rounded-[9px] border border-border px-4 text-sm font-medium text-muted-foreground transition-colors hover:border-foreground/40 hover:text-foreground disabled:opacity-50"
            data-testid="config-discard-btn"
          >
            Discard
          </button>
          <button
            type="button"
            onClick={onSave}
            disabled={saving}
            className="h-10 rounded-[9px] bg-foreground px-4 text-sm font-medium text-background transition-colors hover:bg-foreground/90 disabled:opacity-50"
            data-testid="config-save-btn"
          >
            {saving ? "Saving…" : "Save changes"}
          </button>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Inner (loaded) view
// ---------------------------------------------------------------------------

interface ConfigInnerProps {
  snapshot: AppConfigSnapshot;
}

function ConfigInner({ snapshot }: ConfigInnerProps): React.JSX.Element {
  const [state, dispatch] = useReducer(reducer, {
    activeCategory: CONFIG_CATEGORIES[0]!.id,
    values: buildValuesFromSnapshot(snapshot),
    saved: buildValuesFromSnapshot(snapshot),
  });

  // Re-initialise when the snapshot reference changes — but ONLY when there are
  // no unsaved edits. Background refetches and reconnect snapshots must not
  // silently clobber dirty edits the operator hasn't saved yet.
  //
  // The save flow is safe: handleSave awaits mutateAsync, which on success calls
  // setQueryData (new snapshot reference) → this component re-renders with the
  // server-confirmed snapshot. By the time that snapshot arrives, the save has
  // completed and state.values === state.saved (both rebuilt by the last INIT
  // from the previous snapshot), so the dirty guard is false and re-init fires.
  const snapshotRef = useRef(snapshot);
  const stateRef = useRef(state);
  stateRef.current = state;
  useEffect(() => {
    if (snapshotRef.current !== snapshot) {
      snapshotRef.current = snapshot;
      const { values, saved } = stateRef.current;
      const isDirty = Object.keys(values).some((k) => values[k] !== saved[k]);
      if (!isDirty) {
        dispatch({ type: "INIT", snapshot });
      }
      // When dirty: skip re-init; the operator's edits are preserved.
      // The next explicit Save or Discard will re-sync with the snapshot.
    }
  }, [snapshot]);

  const saveConfig = useSaveConfig();

  // Dirty tracking per category + global count.
  // Uses valuesEqual so array fields (recording_devices) don't produce false
  // positives when a DeviceMultiSelect toggle creates a new array with the
  // same contents.
  const dirtyKeys = useMemo(
    () =>
      Object.keys(state.values).filter(
        (k) => !valuesEqual(state.values[k], state.saved[k]),
      ),
    [state.values, state.saved],
  );
  const isDirtyByCategory = useMemo(() => {
    const map: Partial<Record<FieldCategory, boolean>> = {};
    for (const key of dirtyKeys) {
      const def = CONFIG_FIELD_MAP[key];
      if (def) map[def.category] = true;
    }
    return map;
  }, [dirtyKeys]);

  // On success: the mutation's onSuccess calls setQueryData with the server's
  // confirmed snapshot → the parent ConfigInner receives a new snapshot prop →
  // the useEffect above dispatches INIT, re-syncing values and saved baseline.
  const handleSave = useCallback(async () => {
    const edit = buildEditFromDirty(state.values, state.saved, snapshot);
    await saveConfig.mutateAsync(edit);
  }, [state.values, state.saved, snapshot, saveConfig]);

  const handleDiscard = useCallback(() => dispatch({ type: "DISCARD" }), []);

  const activeCategory = CONFIG_CATEGORIES.find(
    (c) => c.id === state.activeCategory,
  )!;

  return (
    <>
      {/* Two-pane grid: 268px rail | content */}
      <div
        className="grid gap-11"
        style={{ gridTemplateColumns: "268px minmax(0,1fr)" }}
        data-testid="config-layout"
      >
        {/* Category rail */}
        <nav
          className="sticky top-4 flex flex-col gap-[3px]"
          aria-label="Configuration categories"
          data-testid="config-rail"
        >
          {CONFIG_CATEGORIES.map((cat, i) => (
            <RailItem
              key={cat.id}
              index={i}
              id={cat.id}
              label={cat.label}
              description={cat.description}
              isActive={cat.id === state.activeCategory}
              isDirty={isDirtyByCategory[cat.id] ?? false}
              onClick={() => dispatch({ type: "SET_CATEGORY", category: cat.id })}
            />
          ))}
        </nav>

        {/* Content pane */}
        <div data-testid={`config-pane-${activeCategory.id}`}>
          <header className="mb-6">
            <h2 className="text-[19px] font-semibold text-foreground">
              {activeCategory.label}
            </h2>
            <p className="mt-1 text-sm text-muted-foreground">
              {activeCategory.description}
            </p>
          </header>

          <div className="flex flex-col">
            {activeCategory.fields
              // Respect revealWhen — hide dependent fields whose controlling
              // field doesn't match the required value.
              .filter((fieldDef) =>
                fieldDef.revealWhen === undefined ||
                state.values[fieldDef.revealWhen.key] === fieldDef.revealWhen.equals,
              )
              .map((fieldDef, idx, visible) => {
              const meta =
                fieldDef.key
                  .split(".")
                  .reduce(
                    (obj: Record<string, unknown>, seg) =>
                      (obj[seg] as Record<string, unknown>) ?? {},
                    snapshot as unknown as Record<string, unknown>,
                  );

              // Cross-field bounds: server rejects values that violate sibling
              // constraints. Wire dynamic min/max so the UI can't offer an
              // unsaveable value.
              //
              // heat_target_percent ≥ trim_heat_percent (effective from snapshot)
              //   → trim_heat_percent is the floor for heat.
              // fan_target_percent ≤ fan_ceiling_percent
              //   → fan_ceiling_percent is NOT in the snapshot; use the server
              //     default of 30 (PreFirstCrackLevers.fan_ceiling_percent).
              let dynMin: number | undefined;
              let dynMax: number | undefined;
              if (fieldDef.key === "controller.pre_fc_heat_target_percent") {
                const trimHeatMeta = snapshot.controller.late_maillard_trim_heat_percent;
                const trimHeat = typeof trimHeatMeta.effective_value === "number"
                  ? trimHeatMeta.effective_value
                  : 10;  // ge=10 floor from LateMaillardTrimEdit
                dynMin = trimHeat;
              } else if (fieldDef.key === "controller.pre_fc_fan_target_percent") {
                // fan_ceiling_percent not exposed in GET /api/config snapshot;
                // cap at the server default (30). PR3/S4 can expose and
                // dynamically wire once the snapshot includes it.
                dynMax = 30;
              }

              return (
                <ConfigFieldRow
                  key={fieldDef.key}
                  fieldDef={fieldDef}
                  meta={meta as unknown as import("@/lib/types").ConfigFieldMeta}
                  value={state.values[fieldDef.key] ?? null}
                  isLast={idx === visible.length - 1}
                  onChange={(v) =>
                    dispatch({ type: "SET_VALUE", key: fieldDef.key, value: v })
                  }
                  onReset={(defaultValue) =>
                    dispatch({ type: "RESET_FIELD", key: fieldDef.key, defaultValue })
                  }
                  dynMin={dynMin}
                  dynMax={dynMax}
                />
              );
            })}
          </div>
        </div>
      </div>

      {/* Sticky save bar */}
      <SaveBar
        dirtyCount={dirtyKeys.length}
        saving={saveConfig.isPending}
        saveError={
          saveConfig.isError
            ? (saveConfig.error as Error).message
            : null
        }
        onSave={() => void handleSave()}
        onDiscard={handleDiscard}
      />
    </>
  );
}

// ---------------------------------------------------------------------------
// Page root
// ---------------------------------------------------------------------------

export function ConfigPage(): React.JSX.Element {
  const configQuery = useConfig();

  return (
    <AppFrame
      headerRight={
        <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          Configuration
        </span>
      }
    >
      <div className="mx-auto max-w-[1180px]" data-testid="config-page">
        <ConfigHeader />

        {configQuery.isPending && (
          <div
            className="flex h-32 items-center justify-center text-sm text-muted-foreground"
            data-testid="config-loading"
          >
            Loading configuration…
          </div>
        )}

        {configQuery.isError && (
          <div
            className="rounded-lg border border-roast-fault/40 bg-roast-fault/10 p-4 text-sm text-roast-fault"
            data-testid="config-error"
          >
            Failed to load configuration.{" "}
            {(configQuery.error as Error).message}
          </div>
        )}

        {configQuery.data && <ConfigInner snapshot={configQuery.data} />}
      </div>
    </AppFrame>
  );
}
