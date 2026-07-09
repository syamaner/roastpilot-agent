/**
 * Per-field row in the config view (#419, PR3).
 *
 * 2-column grid: left = label + description; right = control + meta line.
 * Control types rendered here: text, number, boolean (toggle), select, masked,
 * deviceSelect (single-select dropdown), deviceMultiSelect (checkable list),
 * micTestButton (placeholder — not available in M1).
 *
 * Per-field "Reset to default" appears only when the value differs from the
 * field's schema default and the field is editable. Masked fields show
 * "Managed via env-var" instead of a default line.
 *
 * Safety fields receive a `Guarded` chip and a disabled control in M1 —
 * the edit-gate dialog is deferred to a later slice (D78 decision 2 = all
 * safety read-only in M1, no dialog needed).
 *
 * deviceSelect / deviceMultiSelect fields receive the pre-fetched
 * `devicesSnapshot` from ConfigPage (fetched once, passed to each row).
 */

import type { ConfigFieldMeta } from "@/lib/types";
import { cn } from "@/lib/cn";
import type { ConfigFieldDef } from "./configSchema";
import { DeviceSelect } from "./DeviceSelect";
import { DeviceMultiSelect } from "./DeviceMultiSelect";

// ---------------------------------------------------------------------------
// Field-level controls
// ---------------------------------------------------------------------------

interface ControlProps {
  fieldDef: ConfigFieldDef;
  value: unknown;
  disabled: boolean;
  onChange: (v: unknown) => void;
  /** Dynamic lower bound for number fields — overrides the static schema `min`.
   *  Used for cross-field constraints: heat ≥ effective trim_heat_percent. */
  dynMin?: number;
  /** Dynamic upper bound for number fields — overrides the static schema `max`.
   *  Used for cross-field constraints: fan ≤ effective fan_ceiling_percent. */
  dynMax?: number;
  /**
   * The value currently in the hand-authored MCP yaml for this field (#482),
   * or `null` when not an `mcp_device` field / no yaml value is resolvable.
   * Used to render the true inherit state ("Inherit from yaml (audio)")
   * instead of a bogus concrete default when the field is unconfigured
   * (`value === null`).
   */
  yamlValue?: unknown;
}

function TextControl({ fieldDef, value, disabled, onChange, yamlValue }: ControlProps): React.JSX.Element {
  // mcp_device text fields are tri-state (#439/#482): null already renders as
  // a blank input (no bogus concrete value), but the placeholder shows what
  // the hand-authored yaml actually has so a blank field doesn't read as
  // "nothing configured anywhere" when it's really "inheriting a real value".
  const isInheritField = fieldDef.key.startsWith("mcp_device.");
  const placeholder =
    isInheritField && value === null && typeof yamlValue === "string" && yamlValue
      ? `${yamlValue} (from yaml)`
      : undefined;
  return (
    <input
      type="text"
      id={fieldDef.key}
      value={typeof value === "string" ? value : String(value ?? "")}
      placeholder={placeholder}
      disabled={disabled}
      onChange={(e) => onChange(e.target.value)}
      className={cn(
        "h-11 w-full rounded-[9px] border border-input bg-input px-3 text-sm text-foreground transition-colors",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2",
        disabled && "cursor-not-allowed opacity-50",
      )}
      aria-label={fieldDef.label}
    />
  );
}

function MaskedControl({ fieldDef, value, disabled }: Omit<ControlProps, "onChange">): React.JSX.Element {
  // api_key_env: show the env-var name (the key itself is never in config).
  const display = typeof value === "string" && value ? value : "—";
  return (
    <div
      className="flex h-11 w-full items-center rounded-[9px] border border-input bg-input px-3 text-sm text-muted-foreground"
      aria-label={fieldDef.label}
      aria-disabled={disabled}
      data-testid={`masked-${fieldDef.key}`}
    >
      <span className="font-mono">{display}</span>
    </div>
  );
}

function NumberControl({
  fieldDef,
  value,
  disabled,
  onChange,
  dynMin,
  dynMax,
  yamlValue,
}: ControlProps): React.JSX.Element {
  // mcp_device number fields are tri-state (#439/#482): null means "inherit
  // from the hand-authored yaml", not "0". Coercing null to Number(null ?? 0)
  // rendered a real-looking "0" in the input — the #482 scare for
  // ambient_poll_interval_seconds / fc_confidence_threshold. When inherited,
  // render a BLANK input (not a fabricated 0) with a placeholder showing the
  // real yaml value when known.
  const isInheritField = fieldDef.key.startsWith("mcp_device.");
  const isInherited = isInheritField && value === null;
  const numVal = typeof value === "number" ? value : isInherited ? "" : Number(value ?? 0);
  const placeholder =
    isInherited && typeof yamlValue === "number" ? `${yamlValue} (from yaml)` : undefined;
  const minVal = dynMin !== undefined ? dynMin : fieldDef.min;
  const maxVal = dynMax !== undefined ? dynMax : fieldDef.max;
  return (
    <input
      type="number"
      id={fieldDef.key}
      value={numVal}
      placeholder={placeholder}
      min={minVal}
      max={maxVal}
      step={fieldDef.step ?? 1}
      disabled={disabled}
      onChange={(e) => onChange(e.target.value === "" ? null : Number(e.target.value))}
      className={cn(
        "h-11 w-full rounded-[9px] border border-input bg-input px-3 font-mono text-sm text-foreground tabular-nums transition-colors",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2",
        disabled && "cursor-not-allowed opacity-50",
      )}
      aria-label={fieldDef.label}
    />
  );
}

function BooleanControl({ fieldDef, value, disabled, onChange }: ControlProps): React.JSX.Element {
  const checked = Boolean(value);
  return (
    <button
      type="button"
      role="switch"
      id={fieldDef.key}
      aria-checked={checked}
      aria-label={fieldDef.label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      data-testid={`toggle-${fieldDef.key}`}
      className={cn(
        "relative inline-flex h-6 w-10 items-center rounded-full transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2",
        checked ? "bg-roast-nominal" : "bg-input",
        disabled && "cursor-not-allowed opacity-50",
      )}
    >
      <span
        className={cn(
          "inline-block h-4 w-4 rounded-full bg-foreground shadow-sm transition-transform",
          checked ? "translate-x-5" : "translate-x-1",
        )}
      />
    </button>
  );
}

/**
 * Tri-state boolean control for nullable mcp_device fields (#439).
 *
 * Three states: `null` = Inherit (from hand-authored yaml), `true` = On,
 * `false` = Off. Used for `mcp_device.*` boolean fields where `null` means
 * "keep whatever the operator's hand-authored coffee-roaster-mcp.yaml says".
 * Rendered as a three-segment radio group so all three states are visible and
 * the current state is always unambiguous (unlike a two-state toggle where
 * `null` and `false` would look identical).
 */
function NullableBooleanControl({ fieldDef, value, disabled, onChange }: ControlProps): React.JSX.Element {
  const segments: Array<{ label: string; val: boolean | null }> = [
    { label: "Inherit", val: null },
    { label: "On",      val: true },
    { label: "Off",     val: false },
  ];
  const current = value === null || value === undefined ? null : Boolean(value);

  return (
    <div
      role="radiogroup"
      aria-label={fieldDef.label}
      data-testid={`nullable-bool-${fieldDef.key}`}
      className="flex h-11 overflow-hidden rounded-[9px] border border-input"
    >
      {segments.map(({ label, val }) => {
        const isSelected = current === val;
        return (
          <button
            key={String(val)}
            type="button"
            role="radio"
            aria-checked={isSelected}
            disabled={disabled}
            onClick={() => onChange(val)}
            data-testid={`nullable-bool-${fieldDef.key}-${String(val)}`}
            className={cn(
              "flex flex-1 items-center justify-center text-xs font-medium transition-colors",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset",
              "border-r border-input last:border-r-0",
              isSelected
                ? val === null
                  ? "bg-secondary text-foreground"
                  : val
                    ? "bg-roast-nominal text-foreground"
                    : "bg-input text-muted-foreground"
                : "bg-transparent text-muted-foreground/70 hover:bg-white/[.04] hover:text-foreground",
              disabled && "cursor-not-allowed opacity-50",
            )}
          >
            {label}
          </button>
        );
      })}
    </div>
  );
}

function SelectControl({
  fieldDef,
  value,
  disabled,
  onChange,
  yamlValue,
}: ControlProps): React.JSX.Element {
  // mcp_device select fields are tri-state (#439/#482): null means "inherit
  // from the hand-authored yaml", not "the first enum option". Rendering an
  // inherited fc_mode as the native <select>'s first option (previously
  // "disabled") falsely told the operator FC detection was off when the yaml
  // actually said "audio" — the #482 scare. When null, inject an explicit
  // inherit option carrying the real yaml value (when known) so the operator
  // sees what will actually govern, never a fabricated concrete choice.
  const isInheritField = fieldDef.key.startsWith("mcp_device.");
  const isInherited = isInheritField && value === null;
  const inheritLabel =
    typeof yamlValue === "string" || typeof yamlValue === "number"
      ? `Inherit from yaml (${yamlValue})`
      : "Inherit from yaml";

  return (
    <select
      id={fieldDef.key}
      value={isInherited ? "" : typeof value === "string" ? value : String(value ?? "")}
      disabled={disabled}
      onChange={(e) => onChange(e.target.value === "" ? null : e.target.value)}
      className={cn(
        "h-11 w-full rounded-[9px] border border-input bg-input px-3 text-sm text-foreground transition-colors",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2",
        disabled && "cursor-not-allowed opacity-50",
      )}
      aria-label={fieldDef.label}
    >
      {isInheritField && <option value="">{inheritLabel}</option>}
      {(fieldDef.options ?? []).map((opt) => (
        <option key={opt.value} value={opt.value}>
          {opt.label}
        </option>
      ))}
    </select>
  );
}

// ---------------------------------------------------------------------------
// Mic test button (placeholder — not available in M1)
// ---------------------------------------------------------------------------

/** Rendered for fieldDef.type === "micTestButton". */
function MicTestButtonControl(): React.JSX.Element {
  return (
    <div className="flex items-center gap-3">
      <button
        type="button"
        disabled
        className="flex h-11 items-center gap-2 rounded-[9px] border border-input bg-input px-4 text-sm font-medium text-muted-foreground/50 cursor-not-allowed"
        aria-label="Test microphone — not available in M1"
        data-testid="mic-test-button"
      >
        Test input
      </button>
      <span className="text-xs text-muted-foreground/50">
        Not available in M1 — the backend sample endpoint is deferred.
      </span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Guarded chip (safety read-only in M1)
// ---------------------------------------------------------------------------

function GuardedChip(): React.JSX.Element {
  return (
    <span className="inline-flex items-center gap-1 rounded-sm border border-roast-caution/40 px-1.5 py-0.5 text-[11px] font-medium text-roast-caution">
      Guarded
    </span>
  );
}

// ---------------------------------------------------------------------------
// Env-override badge (D78-1, #419 slice 3b)
// ---------------------------------------------------------------------------

interface EnvOverrideBadgeProps {
  /** The env-var name from the static schema (ConfigFieldDef.envVar). */
  envVar: string;
}

/**
 * Rendered below the control when ConfigFieldMeta.env_overridden is true.
 * Communicates that the host environment is overriding the saved value so
 * the operator knows why their saved value has no effect.
 */
function EnvOverrideBadge({ envVar }: EnvOverrideBadgeProps): React.JSX.Element {
  return (
    <div
      className="flex flex-col gap-0.5 rounded-[6px] border border-roast-nominal/30 bg-roast-nominal/[.06] px-2.5 py-1.5"
      data-testid="env-override-badge"
    >
      <span className="text-[11px] font-semibold uppercase tracking-wide text-roast-nominal">
        Overridden by env
      </span>
      <span className="font-mono text-[11px] text-muted-foreground/70">{envVar}</span>
      <span className="text-[11px] text-muted-foreground/50">
        Saved value won't take effect while this env var is set.
      </span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Row
// ---------------------------------------------------------------------------

export interface ConfigFieldRowProps {
  fieldDef: ConfigFieldDef;
  /** The ConfigFieldMeta from the server snapshot for this field. */
  meta: ConfigFieldMeta;
  /** Current (possibly dirty) value for the field. */
  value: unknown;
  /** True when this is the last field in the group (omit bottom border). */
  isLast: boolean;
  onChange: (v: unknown) => void;
  /** Called with the field's schema default value so the parent can reset to it. */
  onReset: (defaultValue: unknown) => void;
  /** Dynamic lower bound — overrides the static schema `min` for cross-field
   *  constraints (e.g. heat_target_percent ≥ effective trim_heat_percent). */
  dynMin?: number;
  /** Dynamic upper bound — overrides the static schema `max` for cross-field
   *  constraints (e.g. fan_target_percent ≤ effective fan_ceiling_percent). */
  dynMax?: number;
  /**
   * True while a save PUT is in flight (#483 fix round). Disables the control
   * regardless of read-only status so a mid-save edit can't race the
   * unconditional post-save rebaseline (ConfigPage's handleSave INITs from
   * the response snapshot, which would silently clobber an in-flight edit).
   */
  saving?: boolean;
}

export function ConfigFieldRow({
  fieldDef,
  meta,
  value,
  isLast,
  onChange,
  onReset,
  dynMin,
  dynMax,
  saving = false,
}: ConfigFieldRowProps): React.JSX.Element {
  const isEnvOverridden = meta.env_overridden && fieldDef.envVar !== null;
  // env_overridden and read_only are SEPARATE server flags: an env-overridden
  // non-safety field has read_only=false and PUT /api/config accepts it. The badge
  // is purely informational — the operator can save a value that becomes effective
  // once the env var is removed. Only readOnlyStatic and server read_only gate edits.
  const isReadOnly = fieldDef.readOnlyStatic || meta.read_only || saving;
  const isSafetyField = fieldDef.category === "Safety";
  const isDirtyFromDefault = value !== meta.default;
  // mcp_device fields are tri-state (#439): schema default is always null
  // (inherit); the operator-relevant baseline is the hand-authored yaml's
  // current value (#482), so the "Default" line and reset-button copy differ
  // for this field class. Keyed off the key prefix (not category) so a
  // category rename never silently drops the distinction (mirrors the
  // #439 boolean-control convention above).
  const isMcpDeviceField = fieldDef.key.startsWith("mcp_device.");

  // Effective bounds: dynamic override > static schema value
  const effectiveMin = dynMin !== undefined ? dynMin : fieldDef.min;
  const effectiveMax = dynMax !== undefined ? dynMax : fieldDef.max;

  const controlProps: ControlProps = {
    fieldDef,
    value,
    disabled: isReadOnly,
    onChange,
    dynMin,
    dynMax,
    yamlValue: meta.yaml_value,
  };

  let control: React.JSX.Element;
  switch (fieldDef.type) {
    case "masked":
      control = <MaskedControl {...controlProps} />;
      break;
    case "boolean":
      // mcp_device boolean fields use the tri-state control (#439): null = inherit
      // from the hand-authored yaml, true = on, false = off. Other boolean fields
      // (controller, advisor) only have two states (true/false), so they keep the
      // simple toggle — their defaults are non-null and they never need "inherit".
      // Keyed off fieldDef.key prefix, not category, so renaming a category never
      // silently reintroduces the null→off collapse (#439 review fix).
      control = fieldDef.key.startsWith("mcp_device.")
        ? <NullableBooleanControl {...controlProps} />
        : <BooleanControl {...controlProps} />;
      break;
    case "number":
      control = <NumberControl {...controlProps} />;
      break;
    case "select":
      control = <SelectControl {...controlProps} />;
      break;
    case "deviceSelect": {
      // DeviceSelect fetches via useDevices() internally (shared query cache).
      // `deviceKind` picks the right list from DevicesSnapshot.
      // allowClear enables the "Inherit from yaml" option (#439): when selected
      // it calls onChange(null), clearing the override so the hand-authored
      // MCP yaml governs the device field on the next spawn.
      const deviceKind = fieldDef.deviceSource ?? "serial";
      control = (
        <DeviceSelect
          label={fieldDef.label}
          deviceKind={deviceKind}
          value={typeof value === "string" ? value : ""}
          disabled={isReadOnly}
          onChange={(v) => onChange(v !== "" ? v : null)}
          allowClear
        />
      );
      break;
    }
    case "deviceMultiSelect": {
      // DeviceMultiSelect fetches via useDevices() internally (shared query cache).
      // The saved value is stored as a tuple of strings (server) or a string[]
      // (local edit). Normalise to string[].
      const multiValue = Array.isArray(value)
        ? (value as unknown[]).filter((v): v is string => typeof v === "string")
        : [];
      control = (
        <DeviceMultiSelect
          label={fieldDef.label}
          values={multiValue}
          disabled={isReadOnly}
          onChange={(vs) => onChange(vs)}
        />
      );
      break;
    }
    case "micTestButton":
      control = <MicTestButtonControl />;
      break;
    default:
      control = <TextControl {...controlProps} />;
  }

  return (
    <div
      className={cn(
        "grid gap-8 py-[22px]",
        "grid-cols-[minmax(0,1fr)_384px] max-[900px]:grid-cols-1",
        !isLast && "border-b border-[#2e2e34]",
      )}
      data-testid={`config-field-${fieldDef.key}`}
    >
      {/* Left: label + description */}
      <div className="flex flex-col gap-1">
        <label
          htmlFor={fieldDef.type !== "boolean" ? fieldDef.key : undefined}
          className="flex items-center gap-2 text-[12px] font-semibold uppercase tracking-wide text-foreground"
        >
          {fieldDef.label}
          {isSafetyField && <GuardedChip />}
        </label>
        <p className="max-w-[44ch] text-[13.5px] leading-relaxed text-muted-foreground [text-wrap:pretty]">
          {fieldDef.hint}
        </p>
      </div>

      {/* Right: control + meta */}
      <div className="flex flex-col gap-1.5">
        {control}

        {/* Env-override badge — shown when the host env var overrides the saved value */}
        {isEnvOverridden && <EnvOverrideBadge envVar={fieldDef.envVar!} />}

        {/* Range hint for numeric fields — uses effective (dynamic) bounds */}
        {fieldDef.type === "number" && (effectiveMin !== undefined || effectiveMax !== undefined) && (
          <p className="text-xs text-muted-foreground/70">
            {fieldDef.unit ? `${fieldDef.unit} · ` : ""}
            {effectiveMin !== undefined && effectiveMax !== undefined
              ? `Range ${effectiveMin}–${effectiveMax}`
              : effectiveMin !== undefined
              ? `Min ${effectiveMin}`
              : `Max ${effectiveMax}`}
          </p>
        )}

        {/* Default + reset-to-default (suppressed for action-only field types) */}
        <div className="flex items-center justify-between">
          {fieldDef.type === "masked" ? (
            <span className="text-xs text-muted-foreground/50">Managed via env-var</span>
          ) : fieldDef.type === "micTestButton" ? (
            // No default line for the mic-test placeholder.
            <span />
          ) : isMcpDeviceField && meta.yaml_value !== null && meta.yaml_value !== undefined ? (
            // mcp_device fields have no meaningful schema default (it's always
            // null, per D78-4) — the operator-relevant baseline is the
            // hand-authored yaml's current value (#482), not "Default —".
            <span className="font-mono text-xs tabular-nums text-muted-foreground/50">
              From yaml: {String(meta.yaml_value)}
              {fieldDef.unit ? ` ${fieldDef.unit}` : ""}
            </span>
          ) : (
            <span className="font-mono text-xs tabular-nums text-muted-foreground/50">
              Default {String(meta.default ?? "—")}
              {fieldDef.unit ? ` ${fieldDef.unit}` : ""}
            </span>
          )}

          {!isReadOnly && isDirtyFromDefault && (
            <button
              type="button"
              onClick={() => onReset(meta.default)}
              className="text-xs text-muted-foreground/70 transition-colors hover:text-foreground"
              data-testid={`reset-${fieldDef.key}`}
            >
              {isMcpDeviceField ? "↺ Restore to yaml" : "↺ Reset to default"}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
