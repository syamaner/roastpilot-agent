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
