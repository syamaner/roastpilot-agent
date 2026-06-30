/**
 * DeviceSelect — backend-enumerated single-select device dropdown (#419, slice 3a-1).
 *
 * Backed by GET /api/config/devices via `useDevices()`. Also exports the shared
 * primitive components (DevicePopover, DeviceListBody, OptionRow, RescanFooter)
 * that slice 3a-2 (DeviceMultiSelect) builds on.
 *
 * States: loaded (option rows + rescan footer), loading (spinner), empty ("No devices
 * found"), error (per-source error string). A configured value absent from the
 * enumerated list is shown as a ghost row so the operator sees what is configured.
 *
 * Invariants: renders from server state only; never calls MCP; never free-text.
 */

import { useEffect, useId, useRef, useState } from "react";

import { cn } from "@/lib/cn";
import type { DeviceOption } from "@/lib/types";
import { useDevices } from "@/hooks/queries";

// Inline glyphs — no icon dependency in this build (matches MicStatusIcon.tsx).
const SVG_BASE = { fill: "none" as const, stroke: "currentColor" as const, strokeWidth: 2, strokeLinecap: "round" as const, strokeLinejoin: "round" as const, "aria-hidden": true as const };

function CheckGlyph(): React.JSX.Element {
  return <svg width="16" height="16" viewBox="0 0 16 16" {...SVG_BASE}><polyline points="3 8 6.5 11.5 13 5" /></svg>;
}
function ChevronGlyph({ open }: { open: boolean }): React.JSX.Element {
  return <svg width="16" height="16" viewBox="0 0 16 16" {...SVG_BASE} className={cn("transition-transform text-muted-foreground/60", open && "rotate-180")}><polyline points="4 6 8 10 12 6" /></svg>;
}
function RefreshGlyph(): React.JSX.Element {
  return <svg width="14" height="14" viewBox="0 0 16 16" {...SVG_BASE}><path d="M13.5 2.5A6.5 6.5 0 0 0 2 8.5" /><path d="M2.5 13.5A6.5 6.5 0 0 0 14 7.5" /><polyline points="13.5 2.5 13.5 6 10 6" /><polyline points="2.5 13.5 2.5 10 6 10" /></svg>;
}
function SpinnerGlyph(): React.JSX.Element {
  return <svg width="14" height="14" viewBox="0 0 16 16" {...SVG_BASE} strokeLinejoin={undefined} className="animate-spin"><circle cx="8" cy="8" r="6" strokeOpacity="0.25" /><path d="M14 8A6 6 0 0 0 8 2" /></svg>;
}

// --- Shared: RescanFooter ---

interface RescanFooterProps {
  isRescanning: boolean;
  onRescan: () => void;
}

export function RescanFooter({ isRescanning, onRescan }: RescanFooterProps): React.JSX.Element {
  return (
    <div className="border-t border-[#2e2e34] px-3 py-2">
      <button
        type="button"
        onClick={onRescan}
        disabled={isRescanning}
        data-testid="device-rescan-btn"
        className={cn(
          "flex items-center gap-1.5 text-xs text-muted-foreground/70 transition-colors hover:text-foreground",
          isRescanning && "cursor-not-allowed opacity-50",
        )}
      >
        {isRescanning ? <SpinnerGlyph /> : <RefreshGlyph />}
        {isRescanning ? "Rescanning…" : "↻ Rescan devices"}
      </button>
    </div>
  );
}

// --- Shared: OptionRow (single-select; 3a-2 provides its own multi-select row) ---

interface OptionRowProps {
  option: DeviceOption;
  isSelected: boolean;
  isUnavailable: boolean;
  onSelect: () => void;
}

export function OptionRow({ option, isSelected, isUnavailable, onSelect }: OptionRowProps): React.JSX.Element {
  return (
    <div
      role="option"
      aria-selected={isSelected}
      tabIndex={0}
      onClick={onSelect}
      onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onSelect(); } }}
      data-testid={`device-option-${option.value}`}
      className={cn(
        "flex cursor-pointer items-center gap-3 px-3 py-2.5 text-sm transition-colors",
        "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
        isSelected
          ? "bg-secondary text-foreground"
          : "text-muted-foreground hover:bg-white/[.04] hover:text-foreground",
        isUnavailable && "opacity-60",
      )}
    >
      <span className="w-4 shrink-0 text-foreground">{isSelected && <CheckGlyph />}</span>
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

// --- Shared: DeviceListBody (loaded / loading / empty / error states) ---

interface DeviceListBodyProps {
  devices: DeviceOption[];
  error: string | null;
  isLoading: boolean;
  selectedValue: string;
  onSelect: (value: string) => void;
  isRescanning: boolean;
  onRescan: () => void;
}

export function DeviceListBody({
  devices,
  error,
  isLoading,
  selectedValue,
  onSelect,
  isRescanning,
  onRescan,
}: DeviceListBodyProps): React.JSX.Element {
  if (isLoading) {
    return (
      <div className="flex items-center gap-2 px-3 py-4 text-sm text-muted-foreground" data-testid="device-list-loading">
        <SpinnerGlyph />
        Scanning for devices…
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

  // Build option list: enumerated first, then configured-but-absent ghost rows.
  const enumValues = new Set(devices.map((d) => d.value));
  const ghostOption: DeviceOption | null =
    selectedValue && !enumValues.has(selectedValue)
      ? { value: selectedValue, label: selectedValue, note: "" }
      : null;
  const allOptions = ghostOption ? [...devices, ghostOption] : devices;

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

  return (
    <>
      <div role="listbox" className="max-h-60 overflow-y-auto py-1">
        {allOptions.map((opt) => (
          <OptionRow
            key={opt.value}
            option={opt}
            isSelected={opt.value === selectedValue}
            isUnavailable={opt.value === ghostOption?.value}
            onSelect={() => onSelect(opt.value)}
          />
        ))}
      </div>
      <RescanFooter isRescanning={isRescanning} onRescan={onRescan} />
    </>
  );
}

// --- Shared: Popover container (lightweight controlled div) ---

interface PopoverProps {
  open: boolean;
  onClose: () => void;
  children: React.ReactNode;
}

export function DevicePopover({ open, onClose, children }: PopoverProps): React.JSX.Element | null {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function onMouseDown(e: MouseEvent): void {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose();
    }
    function onKeyDown(e: KeyboardEvent): void {
      if (e.key === "Escape") onClose();
    }
    document.addEventListener("mousedown", onMouseDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onMouseDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div
      ref={ref}
      className="absolute left-0 right-0 top-[calc(100%+4px)] z-50 overflow-hidden rounded-[10px] border border-border bg-[#1a1a1f] shadow-lg"
      role="presentation"
    >
      {children}
    </div>
  );
}

// --- DeviceSelect — single-select ---

export interface DeviceSelectProps {
  /** Field label shown above the trigger. */
  label: string;
  /** The currently-saved value (machine id). May be "" when unconfigured. */
  value: string;
  /**
   * Which device list to draw from.
   * "serial" → DevicesSnapshot.serial + serial_error.
   * "audio_input" → DevicesSnapshot.audio_input + audio_input_error.
   */
  deviceKind: "serial" | "audio_input";
  /** Called with the new machine-id value when the operator picks a device. */
  onChange: (value: string) => void;
  /** When true the control is read-only and cannot be opened. */
  disabled?: boolean;
  /** Optional data-testid prefix (defaults to "device-select"). */
  testId?: string;
}

/**
 * Single-select device dropdown backed by GET /api/config/devices.
 *
 * Rescan calls `refetch()` on the `useDevices` query. Keyboard nav follows
 * ARIA listbox pattern (`aria-haspopup="listbox"`, `role="listbox"`,
 * `role="option"`, `aria-selected`).
 */
export function DeviceSelect({
  label,
  value,
  deviceKind,
  onChange,
  disabled = false,
  testId = "device-select",
}: DeviceSelectProps): React.JSX.Element {
  const [open, setOpen] = useState(false);
  const id = useId();
  const triggerRef = useRef<HTMLButtonElement>(null);

  const { data, isPending, isRefetching, refetch } = useDevices();

  const devices = data?.[deviceKind] ?? [];
  const error = data?.[`${deviceKind}_error`] ?? null;
  // isPending: no data yet (initial load) → trigger shows spinner; body shows loading state.
  // isRefetching: re-scan with existing data → trigger shows spinner; body shows list + footer spinner.
  const triggerBusy = isPending || isRefetching;

  const selectedDevice = devices.find((d) => d.value === value);
  const triggerLabel =
    triggerBusy
      ? "Scanning for devices…"
      : selectedDevice
        ? selectedDevice.label
        : value
          ? value          // ghost: show raw configured value
          : "Select a device…";

  function handleRescan(): void { void refetch(); }

  function handleSelect(v: string): void {
    onChange(v);
    setOpen(false);
    triggerRef.current?.focus();
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
            <span className={cn(
              "truncate",
              !selectedDevice && !triggerBusy && value && "font-mono text-muted-foreground",
            )}>
              {triggerLabel}
            </span>
          </span>
          <ChevronGlyph open={open} />
        </button>

        <DevicePopover open={open} onClose={() => setOpen(false)}>
          <DeviceListBody
            devices={devices}
            error={error}
            isLoading={isPending}
            selectedValue={value}
            onSelect={handleSelect}
            isRescanning={isRefetching}
            onRescan={handleRescan}
          />
        </DevicePopover>
      </div>

      {/* Monospace value line below trigger when a device is configured */}
      {!triggerBusy && value && (
        <span className="font-mono text-xs text-muted-foreground/50" data-testid={`${testId}-value`}>
          {value}
        </span>
      )}
    </div>
  );
}
