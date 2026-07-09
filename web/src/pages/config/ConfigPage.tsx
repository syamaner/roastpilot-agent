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
import type { ConfigGroup, FieldCategory } from "./configSchema";
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
        // Wide: full-width column item
        "flex w-full items-start gap-2 rounded-[9px] px-3 py-[11px] text-left transition-colors",
        // Narrow: compact horizontal chip (shrinks to fit label)
        "max-[900px]:w-auto max-[900px]:shrink-0 max-[900px]:items-center max-[900px]:py-2",
        isActive
          ? "border border-border bg-secondary text-foreground"
          : "border border-transparent text-muted-foreground hover:bg-white/[.04] hover:text-foreground",
      )}
    >
      {/* Monospace index — hidden on narrow */}
      <span className="mt-px shrink-0 font-mono text-[11px] text-muted-foreground/60 max-[900px]:hidden">
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
        {/* Blurb hidden on narrow — chip is just the label */}
        <span className="block truncate text-xs text-muted-foreground/70 max-[900px]:hidden">
          {description}
        </span>
      </span>

      {/* Dirty indicator */}
      {isDirty && (
        <span
          className="mt-1 h-1.5 w-1.5 shrink-0 rounded-full bg-roast-caution max-[900px]:mt-0"
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
  // This guard is for snapshots that arrive OUTSIDE the save flow (background
  // refetch, SSE-triggered invalidation, reconnect). The save flow itself
  // rebaselines synchronously in handleSave below — by the time the mutation's
  // setQueryData causes this effect to see a new snapshot, values === saved
  // already, so isDirty is false and this is a no-op re-confirmation, not the
  // mechanism that clears dirty state.
  const snapshotRef = useRef(snapshot);
  const stateRef = useRef(state);
  stateRef.current = state;
  useEffect(() => {
    if (snapshotRef.current !== snapshot) {
      snapshotRef.current = snapshot;
      const { values, saved } = stateRef.current;
      // Use valuesEqual so array fields (recording_devices) don't produce a
      // spurious "dirty" when a DeviceMultiSelect toggle creates a new array
      // with the same content — which would incorrectly block re-init.
      const isDirty = Object.keys(values).some((k) => !valuesEqual(values[k], saved[k]));
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

  // On success: rebaseline directly from the PUT response snapshot (the
  // server's authoritative post-save effective values), rather than relying
  // on the prop-change effect above. The mutation's onSuccess also calls
  // setQueryData so the snapshot prop updates in the background, but that
  // update alone never rebaselines — the effect's dirty-guard sees the
  // still-dirty `values` vs. the old `saved` baseline and correctly refuses
  // to clobber it, which used to leave the save bar and dirty dots stuck on
  // after a successful save (#483). Dispatching INIT with the response
  // snapshot here sets values === saved from the just-saved values, clearing
  // the dirty state and establishing the new baseline in one step.
  //
  // A rejected PUT throws out of mutateAsync before this dispatch runs, so a
  // failed save leaves the operator's edits and dirty state untouched. The
  // mutation object already tracks the failure (saveConfig.isError/.error,
  // rendered as saveError below) — swallow the rejection here so it doesn't
  // surface as an unhandled promise rejection; SaveBar reads the error state.
  const handleSave = useCallback(async () => {
    const edit = buildEditFromDirty(state.values, state.saved, snapshot);
    try {
      const saved = await saveConfig.mutateAsync(edit);
      dispatch({ type: "INIT", snapshot: saved });
    } catch {
      // Already recorded on saveConfig.error; nothing further to do here.
    }
  }, [state.values, state.saved, snapshot, saveConfig]);

  const handleDiscard = useCallback(() => dispatch({ type: "DISCARD" }), []);

  const activeCategory = CONFIG_CATEGORIES.find(
    (c) => c.id === state.activeCategory,
  )!;

  // Render the fields within one group, applying the revealWhen filter and
  // cross-field dynamic bounds. Returns null when the group has no visible fields
  // after filtering (possible if all fields are hidden by revealWhen).
  function renderGroup(group: ConfigGroup): React.JSX.Element | null {
    const visibleFields = group.fields.filter((fieldDef) =>
      fieldDef.revealWhen === undefined ||
      state.values[fieldDef.revealWhen.key] === fieldDef.revealWhen.equals,
    );
    if (visibleFields.length === 0) return null;

    return (
      <div key={group.title} className="mb-2">
        {/* Group subheading: h3 uppercase + hairline per design handoff */}
        <h3 className="border-b border-[#2e2e34] pb-[10px] text-[11.5px] font-semibold uppercase tracking-[.06em] text-muted-foreground/60">
          {group.title}
        </h3>
        <div className="flex flex-col">
          {visibleFields.map((fieldDef, idx) => {
            const meta =
              fieldDef.key
                .split(".")
                .reduce(
                  (obj: Record<string, unknown>, seg) =>
                    (obj[seg] as Record<string, unknown>) ?? {},
                  snapshot as unknown as Record<string, unknown>,
                );

            // Cross-field bounds: wire dynamic min/max so the UI can't offer
            // a value that the server would reject.
            //   heat_target_percent ≥ trim_heat_percent (floor from snapshot)
            //   fan_target_percent ≤ 30 (fan_ceiling_percent server default)
            let dynMin: number | undefined;
            let dynMax: number | undefined;
            if (fieldDef.key === "controller.pre_fc_heat_target_percent") {
              const trimHeatMeta = snapshot.controller.late_maillard_trim_heat_percent;
              const trimHeat = typeof trimHeatMeta.effective_value === "number"
                ? trimHeatMeta.effective_value
                : 10;
              dynMin = trimHeat;
            } else if (fieldDef.key === "controller.pre_fc_fan_target_percent") {
              dynMax = 30;
            }

            return (
              <ConfigFieldRow
                key={fieldDef.key}
                fieldDef={fieldDef}
                meta={meta as unknown as import("@/lib/types").ConfigFieldMeta}
                value={state.values[fieldDef.key] ?? null}
                isLast={idx === visibleFields.length - 1}
                onChange={(v) =>
                  dispatch({ type: "SET_VALUE", key: fieldDef.key, value: v })
                }
                onReset={(defaultValue) =>
                  dispatch({ type: "RESET_FIELD", key: fieldDef.key, defaultValue })
                }
                dynMin={dynMin}
                dynMax={dynMax}
                saving={saveConfig.isPending}
              />
            );
          })}
        </div>
      </div>
    );
  }

  // Render content pane fields: grouped when the category declares groups,
  // flat (with a single implicit group) otherwise.
  function renderFields(): React.JSX.Element {
    if (activeCategory.groups && activeCategory.groups.length > 0) {
      return (
        <div className="flex flex-col gap-6">
          {activeCategory.groups.map((g) => renderGroup(g))}
        </div>
      );
    }
    // Flat: treat all fields as one group with no subheading.
    const visibleFields = activeCategory.fields.filter((fieldDef) =>
      fieldDef.revealWhen === undefined ||
      state.values[fieldDef.revealWhen.key] === fieldDef.revealWhen.equals,
    );
    return (
      <div className="flex flex-col">
        {visibleFields.map((fieldDef, idx) => {
          const meta =
            fieldDef.key
              .split(".")
              .reduce(
                (obj: Record<string, unknown>, seg) =>
                  (obj[seg] as Record<string, unknown>) ?? {},
                snapshot as unknown as Record<string, unknown>,
              );
          let dynMin: number | undefined;
          let dynMax: number | undefined;
          if (fieldDef.key === "controller.pre_fc_heat_target_percent") {
            const trimHeatMeta = snapshot.controller.late_maillard_trim_heat_percent;
            dynMin = typeof trimHeatMeta.effective_value === "number" ? trimHeatMeta.effective_value : 10;
          } else if (fieldDef.key === "controller.pre_fc_fan_target_percent") {
            dynMax = 30;
          }
          return (
            <ConfigFieldRow
              key={fieldDef.key}
              fieldDef={fieldDef}
              meta={meta as unknown as import("@/lib/types").ConfigFieldMeta}
              value={state.values[fieldDef.key] ?? null}
              isLast={idx === visibleFields.length - 1}
              onChange={(v) =>
                dispatch({ type: "SET_VALUE", key: fieldDef.key, value: v })
              }
              onReset={(defaultValue) =>
                dispatch({ type: "RESET_FIELD", key: fieldDef.key, defaultValue })
              }
              dynMin={dynMin}
              dynMax={dynMax}
              saving={saveConfig.isPending}
            />
          );
        })}
      </div>
    );
  }

  return (
    <>
      {/*
        Two-pane layout: 268px fixed rail + content.
        Responsive: at <900px switches to single-column; rail becomes a
        horizontal chip scroller (blurbs hidden, sticky disabled).
      */}
      <div
        className={cn(
          // Wide: two-column grid; narrow: single column stack
          "grid gap-11",
          "max-[900px]:flex max-[900px]:flex-col max-[900px]:gap-4",
        )}
        style={{ gridTemplateColumns: "268px minmax(0,1fr)" }}
        data-testid="config-layout"
      >
        {/* Category rail — sticky on wide, horizontal chip scroller on narrow */}
        <nav
          className={cn(
            "sticky top-4 flex flex-col gap-[3px]",
            // Narrow: horizontal scrolling chip strip
            "max-[900px]:static max-[900px]:flex-row max-[900px]:overflow-x-auto max-[900px]:gap-1.5 max-[900px]:pb-1",
          )}
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

          {renderFields()}
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
