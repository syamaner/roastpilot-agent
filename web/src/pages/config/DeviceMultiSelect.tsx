/**
 * DeviceMultiSelect — backend-enumerated multi-select dropdown for recording_devices
 * (#419, slice 3a-2). Value is `string[]`; toggling a row adds or removes its
 * machine-id from the array; the popover stays open after each toggle.
 *
 * Builds on the shared primitive exported by DeviceSelect.tsx (DevicePopover with
 * the triggerRef outside-click fix, RescanFooter, useDevices). Provides its own
 * MultiOptionRow (checkbox visual + role="checkbox") and MultiDeviceListBody
 * (the loaded/empty/error/query-error states wired for a string[] selection).
 *
 * Invariants: renders from server state only; never calls MCP; never free-text.
 * Note: recording_devices values are audio device identifiers passed through from
 * the backend enumeration — the index-vs-name translation for MCP is handled at
 * the mounting layer (slice 3c), not here.
 */

import { useId, useRef, useState } from "react";

import { cn } from "@/lib/cn";
import type { DeviceOption } from "@/lib/types";
import { useDevices } from "@/hooks/queries";
import { DevicePopover, RescanFooter } from "./DeviceSelect";

// Inline glyphs — no icon dependency in this build (matches MicStatusIcon.tsx).
const SVG_BASE = { fill: "none" as const, stroke: "currentColor" as const, strokeWidth: 2, strokeLinecap: "round" as const, strokeLinejoin: "round" as const, "aria-hidden": true as const };

function CheckGlyph(): React.JSX.Element {
  return <svg width="12" height="12" viewBox="0 0 16 16" {...SVG_BASE}><polyline points="3 8 6.5 11.5 13 5" /></svg>;
}
function ChevronGlyph({ open }: { open: boolean }): React.JSX.Element {
  return <svg width="16" height="16" viewBox="0 0 16 16" {...SVG_BASE} className={cn("transition-transform text-muted-foreground/60", open && "rotate-180")}><polyline points="4 6 8 10 12 6" /></svg>;
}
function SpinnerGlyph(): React.JSX.Element {
  return <svg width="14" height="14" viewBox="0 0 16 16" {...SVG_BASE} strokeLinejoin={undefined} className="animate-spin"><circle cx="8" cy="8" r="6" strokeOpacity="0.25" /><path d="M14 8A6 6 0 0 0 8 2" /></svg>;
}

// --- MultiOptionRow — checkable row with checkbox visual ---

interface MultiOptionRowProps {
  option: DeviceOption;
  isChecked: boolean;
  isUnavailable: boolean;
  onToggle: () => void;
}

function MultiOptionRow({ option, isChecked, isUnavailable, onToggle }: MultiOptionRowProps): React.JSX.Element {
  return (
    <div
      role="option"
      aria-selected={isChecked}
      tabIndex={0}
      onClick={onToggle}
      onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onToggle(); } }}
      data-testid={`device-multi-option-${option.value}`}
      className={cn(
        "flex cursor-pointer items-center gap-3 px-3 py-2.5 text-sm transition-colors",
        "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
        isChecked
          ? "bg-secondary/60 text-foreground"
          : "text-muted-foreground hover:bg-white/[.04] hover:text-foreground",
        isUnavailable && "opacity-60",
      )}
    >
      {/* Decorative checkbox box — semantics are on the parent role="option" + aria-selected. */}
      <span
        aria-hidden="true"
        className={cn(
          "flex h-4 w-4 shrink-0 items-center justify-center rounded-sm border transition-colors",
          isChecked
            ? "border-roast-nominal bg-roast-nominal/20 text-roast-nominal"
            : "border-border bg-transparent",
        )}
      >
        {isChecked && <CheckGlyph />}
      </span>
      <span className="min-w-0 flex-1">
        <span className="block truncate font-medium text-foreground">{option.label}</span>
        <span className="block truncate font-mono text-xs text-muted-foreground/70">{option.value}</span>
        {option.note && (
          <span className="block truncate text-xs text-muted-foreground/50">{option.note}</span>
        )}
        {isUnavailable && (
          <span className="block text-xs text-roast-caution/80">
            Not found on rescan — previously configured
          </span>
        )}
      </span>
    </div>
  );
}

// --- MultiDeviceListBody — loaded / loading / empty / error / query-error states ---

interface MultiDeviceListBodyProps {
  devices: DeviceOption[];
  error: string | null;
  /** Non-null when GET /api/config/devices itself failed (network / 5xx). */
  queryError: string | null;
  isLoading: boolean;
  selectedValues: string[];
  onToggle: (value: string) => void;
  isRescanning: boolean;
  onRescan: () => void;
}

function MultiDeviceListBody({
  devices,
  error,
  queryError,
  isLoading,
  selectedValues,
  onToggle,
  isRescanning,
  onRescan,
}: MultiDeviceListBodyProps): React.JSX.Element {
  if (isLoading) {
    return (
      <div className="flex items-center gap-2 px-3 py-4 text-sm text-muted-foreground" data-testid="device-list-loading">
        <SpinnerGlyph />
        Scanning for devices…
      </div>
    );
  }

  if (queryError) {
    return (
      <div data-testid="device-list-query-error">
        <div className="px-3 py-4">
          <p className="text-sm font-medium text-roast-fault">Couldn't load devices</p>
          <p className="mt-0.5 font-mono text-xs text-muted-foreground/70">{queryError}</p>
        </div>
        <RescanFooter isRescanning={isRescanning} onRescan={onRescan} />
      </div>
    );
  }

  if (error) {
    return (
      <div data-testid="device-list-error">
        <div className="px-3 py-4">
          <p className="text-sm font-medium text-roast-fault">Device enumeration failed</p>
          <p className="mt-0.5 font-mono text-xs text-muted-foreground/70">{error}</p>
        </div>
        <RescanFooter isRescanning={isRescanning} onRescan={onRescan} />
      </div>
    );
  }

  // Ghost rows: configured values absent from enumerated list.
  const enumValues = new Set(devices.map((d) => d.value));
  const ghostOptions: DeviceOption[] = selectedValues
    .filter((v) => !enumValues.has(v))
    .map((v) => ({ value: v, label: v, note: "" }));
  const allOptions = [...devices, ...ghostOptions];

  if (allOptions.length === 0) {
    return (
      <div data-testid="device-list-empty">
        <div className="px-3 py-4">
          <p className="text-sm font-medium text-foreground">No devices found</p>
          <p className="mt-0.5 text-xs text-muted-foreground/70">
            Check the USB connection to the roaster, then rescan.
          </p>
        </div>
        <RescanFooter isRescanning={isRescanning} onRescan={onRescan} />
      </div>
    );
  }

  const ghostSet = new Set(ghostOptions.map((g) => g.value));

  return (
    <>
      <div role="listbox" aria-multiselectable="true" className="max-h-60 overflow-y-auto py-1">
        {allOptions.map((opt) => (
          <MultiOptionRow
            key={opt.value}
            option={opt}
            isChecked={selectedValues.includes(opt.value)}
            isUnavailable={ghostSet.has(opt.value)}
            onToggle={() => onToggle(opt.value)}
          />
        ))}
      </div>
      <RescanFooter isRescanning={isRescanning} onRescan={onRescan} />
    </>
  );
}

// --- DeviceMultiSelect ---

export interface DeviceMultiSelectProps {
  /** Field label shown above the trigger. */
  label: string;
  /** Currently-selected machine ids. */
  values: string[];
  /** Called with the complete updated array on each toggle. */
  onChange: (values: string[]) => void;
  /** When true the control is read-only and cannot be opened. */
  disabled?: boolean;
  /** Optional data-testid prefix (defaults to "device-multi-select"). */
  testId?: string;
}

/**
 * Multi-select device dropdown for recording_devices, backed by
 * GET /api/config/devices (audio_input list).
 *
 * The popover stays open after each toggle so the operator can select several
 * devices in one session. Ghost rows appear for configured values no longer in
 * the enumerated list. A chip strip below the trigger shows selected values.
 */
export function DeviceMultiSelect({
  label,
  values,
  onChange,
  disabled = false,
  testId = "device-multi-select",
}: DeviceMultiSelectProps): React.JSX.Element {
  const [open, setOpen] = useState(false);
  const id = useId();
  const triggerRef = useRef<HTMLButtonElement>(null);

  const { data, isPending, isRefetching, isError, error: queryErr, refetch } = useDevices();

  const devices = data?.audio_input ?? [];
  const error = data?.audio_input_error ?? null;
  const queryError = isError ? (queryErr instanceof Error ? queryErr.message : "Request failed") : null;
  // isPending: initial load → spinner + loading body.
  // isRefetching: re-scan with data → spinner + list with footer rescanning.
  const triggerBusy = isPending || isRefetching;

  const triggerLabel =
    triggerBusy
      ? "Scanning for devices…"
      : values.length === 0
        ? "No devices selected"
        : values.length === 1
          ? (devices.find((d) => d.value === values[0])?.label ?? values[0]!)
          : `${values.length.toString()} devices selected`;

  function handleRescan(): void { void refetch(); }

  function handleToggle(v: string): void {
    onChange(values.includes(v) ? values.filter((x) => x !== v) : [...values, v]);
    // Multi-select intentionally stays open after toggle.
  }

  return (
    <div className="flex flex-col gap-1.5" data-testid={testId}>
      <label
        htmlFor={id}
        className="text-[12px] font-semibold uppercase tracking-wide text-foreground"
      >
        {label}
      </label>

      <div className="relative">
        <button
          ref={triggerRef}
          id={id}
          type="button"
          aria-haspopup="listbox"
          aria-multiselectable="true"
          aria-expanded={open}
          aria-label={label}
          disabled={disabled}
          onClick={() => !disabled && setOpen((o) => !o)}
          data-testid={`${testId}-trigger`}
          className={cn(
            "flex h-11 w-full items-center justify-between gap-2 rounded-[9px] border border-input bg-input px-3 text-sm transition-colors",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2",
            open && "ring-2 ring-ring ring-offset-2",
            disabled && "cursor-not-allowed opacity-50",
          )}
        >
          <span className="flex min-w-0 items-center gap-2">
            {triggerBusy && <SpinnerGlyph />}
            <span className="truncate">{triggerLabel}</span>
          </span>
          <ChevronGlyph open={open} />
        </button>

        <DevicePopover open={open} onClose={() => setOpen(false)} triggerRef={triggerRef}>
          <MultiDeviceListBody
            devices={devices}
            error={error}
            queryError={queryError}
            isLoading={isPending}
            selectedValues={values}
            onToggle={handleToggle}
            isRescanning={isRefetching}
            onRescan={handleRescan}
          />
        </DevicePopover>
      </div>

      {/* Chip strip of selected values below trigger */}
      {!triggerBusy && values.length > 0 && (
        <div className="flex flex-wrap gap-1" data-testid={`${testId}-chips`}>
          {values.map((v) => (
            <span
              key={v}
              className="inline-flex items-center rounded-sm border border-border bg-secondary/60 px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground/70"
            >
              {v}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
