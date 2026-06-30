/**
 * Per-field row in the config view (#419, PR2).
 *
 * 2-column grid: left = label + description; right = control + meta line.
 * Control types rendered here: text, number, boolean (toggle), select, masked.
 * The `deviceSelect` type (PR3) is rendered as a disabled text placeholder.
 *
 * Per-field "Reset to default" appears only when the value differs from the
 * field's schema default and the field is editable. Masked fields show
 * "Managed via env-var" instead of a default line.
 *
 * Safety fields receive a `Guarded` chip and a disabled control in M1 —
 * the edit-gate dialog is deferred to a later slice (D78 decision 2 = all
 * safety read-only in M1, no dialog needed).
 */

import type { ConfigFieldMeta } from "@/lib/types";
import { cn } from "@/lib/cn";
import type { ConfigFieldDef } from "./configSchema";

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
}

function TextControl({ fieldDef, value, disabled, onChange }: ControlProps): React.JSX.Element {
  return (
    <input
      type="text"
      id={fieldDef.key}
      value={typeof value === "string" ? value : String(value ?? "")}
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

function NumberControl({ fieldDef, value, disabled, onChange, dynMin, dynMax }: ControlProps): React.JSX.Element {
  const numVal = typeof value === "number" ? value : Number(value ?? 0);
  const minVal = dynMin !== undefined ? dynMin : fieldDef.min;
  const maxVal = dynMax !== undefined ? dynMax : fieldDef.max;
  return (
    <input
      type="number"
      id={fieldDef.key}
      value={numVal}
      min={minVal}
      max={maxVal}
      step={fieldDef.step ?? 1}
      disabled={disabled}
      onChange={(e) => onChange(Number(e.target.value))}
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

function SelectControl({ fieldDef, value, disabled, onChange }: ControlProps): React.JSX.Element {
  return (
    <select
      id={fieldDef.key}
      value={typeof value === "string" ? value : String(value ?? "")}
      disabled={disabled}
      onChange={(e) => onChange(e.target.value)}
      className={cn(
        "h-11 w-full rounded-[9px] border border-input bg-input px-3 text-sm text-foreground transition-colors",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2",
        disabled && "cursor-not-allowed opacity-50",
      )}
      aria-label={fieldDef.label}
    >
      {(fieldDef.options ?? []).map((opt) => (
        <option key={opt.value} value={opt.value}>
          {opt.label}
        </option>
      ))}
    </select>
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
}: ConfigFieldRowProps): React.JSX.Element {
  const isEnvOverridden = meta.env_overridden && fieldDef.envVar !== null;
  // env_overridden and read_only are SEPARATE server flags: an env-overridden
  // non-safety field has read_only=false and PUT /api/config accepts it. The badge
  // is purely informational — the operator can save a value that becomes effective
  // once the env var is removed. Only readOnlyStatic and server read_only gate edits.
  const isReadOnly = fieldDef.readOnlyStatic || meta.read_only;
  const isSafetyField = fieldDef.category === "Safety";
  const isDirtyFromDefault = value !== meta.default;

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
  };

  let control: React.JSX.Element;
  switch (fieldDef.type) {
    case "masked":
      control = <MaskedControl {...controlProps} />;
      break;
    case "boolean":
      control = <BooleanControl {...controlProps} />;
      break;
    case "number":
      control = <NumberControl {...controlProps} />;
      break;
    case "select":
      control = <SelectControl {...controlProps} />;
      break;
    default:
      control = <TextControl {...controlProps} />;
  }

  return (
    <div
      className={cn(
        "grid gap-8 py-[22px]",
        !isLast && "border-b border-[#2e2e34]",
      )}
      style={{ gridTemplateColumns: "minmax(0,1fr) 384px" }}
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

        {/* Default + reset-to-default */}
        <div className="flex items-center justify-between">
          {fieldDef.type === "masked" ? (
            <span className="text-xs text-muted-foreground/50">Managed via env-var</span>
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
              ↺ Reset to default
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
