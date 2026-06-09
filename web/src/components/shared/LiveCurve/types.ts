/**
 * Shared types for the LiveCurve chart (component plan §7, ui-prompts.md
 * prompts A & C). All temperatures Celsius.
 */

import type { RoastPhase } from "@/lib/types";

/** The five plotted series, in uPlot column order (x is series 0). */
export type SeriesKey = "bean" | "env" | "ror" | "heat" | "fan";

/** Ordered list of the five series keys (uPlot data columns 1..5). */
export const SERIES_KEYS: readonly SeriesKey[] = ["bean", "env", "ror", "heat", "fan"];

/** One curve point. Temps in °C, RoR in °C/min, heat/fan in %. `null` = gap. */
export interface CurvePoint {
  /** Seconds since charge (T0) — the chart x-axis. */
  t: number;
  bean: number | null;
  env: number | null;
  ror: number | null;
  heat: number | null;
  fan: number | null;
}

/** A vertical event marker on the curve (T0 / first crack / drop). */
export type CurveMarkerKind = "t0" | "first_crack" | "drop";

export interface CurveMarker {
  kind: CurveMarkerKind;
  /** x position in seconds (same axis as CurvePoint.t). */
  t: number;
  label: string;
}

/** uPlot's columnar form: [x[], bean[], env[], ror[], heat[], fan[]]. */
export type ChartColumns = [
  number[],
  (number | null)[],
  (number | null)[],
  (number | null)[],
  (number | null)[],
  (number | null)[],
];

export interface LiveCurveProps {
  points: CurvePoint[];
  markers?: CurveMarker[];
  /** Current phase — the charge band shows in `preheating` only. */
  phase?: RoastPhase | null;
  /** Charge guidance band (°C). Shown only while `phase === "preheating"`. */
  chargeBand?: { minC: number; maxC: number };
  /**
   * Controlled highlight time (seconds). A trace-row click sets this to draw a
   * vertical marker on the curve; re-clicking the same row clears it (the
   * consumer toggles by passing `null`). Owned by the consumer, not the chart.
   */
  highlightTime?: number | null;
  /** Series hidden by default (e.g. detail view may start with heat/fan off). */
  initialHidden?: SeriesKey[];
  className?: string;
  height?: number;
}

/**
 * The shape exposed on `window.__chart` and the `data-chart-*` attributes for
 * deterministic tests — assert the chart's DATA, never its canvas pixels (D24).
 */
export interface ChartTestHook {
  columns: ChartColumns;
  visible: Record<SeriesKey, boolean>;
  markers: CurveMarker[];
  highlightTime: number | null;
  chargeBandVisible: boolean;
}

declare global {
  interface Window {
    /** Set by the most-recently-mounted LiveCurve (test-only, D24). */
    __chart?: ChartTestHook;
  }
}
