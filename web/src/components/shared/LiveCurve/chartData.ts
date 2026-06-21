/**
 * Pure data-shaping for LiveCurve — kept out of the uPlot component so the
 * series mapping is unit-testable without a canvas (D24).
 */

import type { ChartColumns, CurvePoint, SeriesKey } from "./types";
import { SERIES_KEYS } from "./types";

/** Project curve points into uPlot's columnar form: [x, bean, env, ror, heat, fan]. */
export function toColumns(points: CurvePoint[]): ChartColumns {
  const x: number[] = [];
  const cols: Record<SeriesKey, (number | null)[]> = {
    bean: [],
    env: [],
    ror: [],
    heat: [],
    fan: [],
  };
  for (const p of points) {
    x.push(p.t);
    for (const key of SERIES_KEYS) {
      cols[key].push(p[key]);
    }
  }
  return [x, cols.bean, cols.env, cols.ror, cols.heat, cols.fan];
}

/**
 * Format roast-elapsed seconds as `M:SS` for the time axis + cursor readout
 * (#153) — e.g. 720 → `12:00`, 66 → `1:06`, 5 → `0:05`.
 *
 * The DATA stays in seconds (the curve x-axis, markers, and dedupe key are all
 * seconds); this is DISPLAY-ONLY. Negative or non-finite values render as `—` so a
 * stray tick can't print a malformed label. Hours roll into the minutes field
 * (a roast never runs an hour, but 75:00 is still well-formed).
 */
export function formatElapsed(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds) || seconds < 0) return "—";
  const total = Math.round(seconds);
  const mins = Math.floor(total / 60);
  const secs = total % 60;
  return `${mins}:${secs.toString().padStart(2, "0")}`;
}

/**
 * Format a point's serve-elapsed `seconds` as CHARGE-referenced ROAST TIME for
 * the time axis + cursor readout (#326), given the serve-elapsed `origin` at the
 * T0/charge moment.
 *
 * The point buffer is keyed on SERVE elapsed (so preheat plots live, before T0 is
 * known); this is a pure DISPLAY transform that re-labels those positions to roast
 * time (0:00 = charge) without moving any point:
 *   - `origin == null` (T0 not detected yet — live preheat): fall back to the
 *     serve-elapsed display (`formatElapsed`), so the axis still reads a sensible
 *     clock before charge lands.
 *   - `origin != null`: `d = round(seconds) − round(origin)` →
 *     `0` → `0:00` (charge), positive → `M:SS`, negative (preheat) → `-M:SS`.
 *   - `null`/non-finite `seconds` → `—` (the existing stray-tick guard).
 *
 * Rounding both operands before subtracting keeps 0:00 exact at the charge tick
 * (the marker sits there) and avoids a sub-second `-0:00`.
 */
export function formatRoastTime(seconds: number | null, origin: number | null): string {
  if (seconds === null || !Number.isFinite(seconds)) return "—";
  if (origin === null || !Number.isFinite(origin)) return formatElapsed(seconds);
  const d = Math.round(seconds) - Math.round(origin);
  const mins = Math.floor(Math.abs(d) / 60);
  const secs = Math.abs(d) % 60;
  const body = `${mins}:${secs.toString().padStart(2, "0")}`;
  return d < 0 ? `-${body}` : body;
}

/** Format a series value for the legend readout. `null` → an em dash. */
export function formatSeriesValue(key: SeriesKey, value: number | null): string {
  if (value === null || Number.isNaN(value)) return "—";
  switch (key) {
    case "bean":
    case "env":
      return `${value.toFixed(1)} °C`;
    case "ror":
      return `${value.toFixed(1)} °C/min`;
    case "heat":
    case "fan":
      return `${Math.round(value)} %`;
  }
}
