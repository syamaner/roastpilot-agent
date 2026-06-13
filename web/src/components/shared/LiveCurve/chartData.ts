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
