/**
 * History-page formatting + filtering helpers (E10-S4).
 *
 * Pure functions over the shared `RoastSummary` type — no I/O, no React — so the
 * component tests can assert filtering/formatting behavior directly. Renders only
 * what `GET /api/roasts` carries (invariant: REST-only, never invent fields).
 */

import type { RoastOutcome, RoastSummary } from "@/lib/types";

/** The "all" sentinel for the optional select filters. */
export const ANY = "all";

/** Filter state: search text (bean), an outcome, and a minimum star rating. */
export interface HistoryFilters {
  /** Case-insensitive substring match against bean origin + varietal + country (#164). */
  search: string;
  /** A `RoastOutcome` value, or `ANY`. */
  outcome: RoastOutcome | typeof ANY;
  /** Minimum star rating as a string ("1".."5"), or `ANY`. */
  minRating: string;
}

export const EMPTY_FILTERS: HistoryFilters = {
  search: "",
  outcome: ANY,
  minRating: ANY,
};

/**
 * The bean display used for the search match + the row aria-label: origin,
 * optionally qualified by varietal and country (#164), so searching by country
 * works. The visual cell renders these on separate lines.
 */
export function beanLabel(run: RoastSummary): string {
  // species excluded intentionally: of the #164 identity fields only country is
  // searchable (species is a 4-value tag, not a useful free-text search term).
  return [run.bean_origin, run.bean_varietal, run.country]
    .filter((part): part is string => typeof part === "string" && part.length > 0)
    .join(" ");
}

/**
 * Format an ISO-8601 UTC timestamp as `YYYY-MM-DD HH:MM` for the Date column.
 *
 * Deliberately UTC + a fixed numeric layout (not locale-formatted): the snapshot
 * suite must be byte-stable across runners, and the operator reads UTC roast
 * logs. Returns the raw string unchanged if it cannot be parsed.
 */
export function formatStartedAt(isoUtc: string): string {
  const date = new Date(isoUtc);
  if (Number.isNaN(date.getTime())) return isoUtc;
  const pad = (n: number): string => String(n).padStart(2, "0");
  return (
    `${date.getUTCFullYear()}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())}` +
    ` ${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}`
  );
}

/** Development percent as a whole-number `NN%`, or an em dash when absent. */
export function formatDevPercent(value: number | null): string {
  return value === null ? "—" : `${Math.round(value)}%`;
}

/**
 * Format the roast weight-loss % (#388) for the history column — one decimal, or
 * an em dash for an un-weighed roast (`weight_loss_percent: null`). Distinct from
 * `formatDevPercent`'s whole-number rounding: weight loss is a tighter, more
 * granular signal (an ~11.6% reads differently from ~12%).
 */
export function formatWeightLoss(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : `${value.toFixed(1)}%`;
}

/**
 * Format the charge-time ambient reading (#342/#464) for the compact history
 * column — `"22.4°C · 41%"` when both fields are captured, temp-only or
 * humidity-only when just one is (a partial-null real state, #463), or an em
 * dash when neither was captured (pre-#342 run, or an ambient-disabled/
 * unavailable MCP config). Pressure is omitted here — it's the least
 * decision-relevant of the triad for a compact column; the full triad is on the
 * detail page's "Roast conditions" widget.
 */
export function formatAmbientCell(
  ambientTempC: number | null | undefined,
  ambientHumidityPct: number | null | undefined,
): string {
  const parts: string[] = [];
  if (ambientTempC !== null && ambientTempC !== undefined) parts.push(`${ambientTempC.toFixed(1)}°C`);
  if (ambientHumidityPct !== null && ambientHumidityPct !== undefined) {
    parts.push(`${Math.round(ambientHumidityPct)}%`);
  }
  return parts.length === 0 ? "—" : parts.join(" · ");
}

/**
 * Format the first-crack timestamp (#111) as a UTC time-of-day `HH:MM` for the
 * FC-time column, or an em dash when no first crack was recorded for the run.
 *
 * Time-of-day rather than the full date (which the Date column already carries):
 * the operator scans the FC column alongside the same-day Date, so the clock
 * time is the useful, non-redundant signal. UTC + fixed numeric layout matches
 * `formatStartedAt` so the snapshot suite stays byte-stable across runners.
 * Returns the raw string unchanged if it cannot be parsed.
 */
export function formatFcTime(isoUtc: string | null): string {
  if (isoUtc === null) return "—";
  const date = new Date(isoUtc);
  if (Number.isNaN(date.getTime())) return isoUtc;
  const pad = (n: number): string => String(n).padStart(2, "0");
  return `${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}`;
}

/**
 * Apply the active filters to the run list (pure, client-side over the small REST
 * list — no server round-trip). Order is preserved (the server returns newest
 * first). A run with no rating is excluded once a minimum rating is set.
 */
export function filterRuns(
  runs: readonly RoastSummary[],
  filters: HistoryFilters,
): RoastSummary[] {
  const search = filters.search.trim().toLowerCase();
  const minRating = filters.minRating === ANY ? null : Number(filters.minRating);
  return runs.filter((run) => {
    if (search && !beanLabel(run).toLowerCase().includes(search)) return false;
    if (filters.outcome !== ANY && run.outcome !== filters.outcome) return false;
    if (minRating !== null && (run.rating ?? 0) < minRating) return false;
    return true;
  });
}
