/**
 * Display formatters for the detail page. Numeric readouts use tabular figures
 * (the `.numeric` class); these just normalize null/units. All temps Celsius.
 */

const EM_DASH = "—";

/** `mm:ss` from seconds (e.g. 510 → "08:30"). `null` → em dash. */
export function formatClock(seconds: number | null): string {
  if (seconds === null || Number.isNaN(seconds)) return EM_DASH;
  const total = Math.max(0, Math.round(seconds));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

/** `123.4 °C`. `null` → em dash. */
export function formatTemp(value: number | null): string {
  if (value === null || Number.isNaN(value)) return EM_DASH;
  return `${value.toFixed(1)} °C`;
}

/** `65 %` (rounded). `null` → em dash. */
export function formatPercent(value: number | null): string {
  if (value === null || Number.isNaN(value)) return EM_DASH;
  return `${Math.round(value)} %`;
}

/** `21.0 %` (one decimal, for development %). `null` → em dash. */
export function formatPercent1(value: number | null): string {
  if (value === null || Number.isNaN(value)) return EM_DASH;
  return `${value.toFixed(1)} %`;
}

/** `0.82` confidence, two decimals. `null` → em dash. */
export function formatConfidence(value: number | null): string {
  if (value === null || Number.isNaN(value)) return EM_DASH;
  return value.toFixed(2);
}

/** A UTC ISO timestamp → a local date string (title block). `null`/bad → em dash. */
export function formatDate(iso: string | null): string {
  if (!iso) return EM_DASH;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return EM_DASH;
  return date.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}
