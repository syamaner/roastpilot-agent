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
  /** SERVE-elapsed seconds (the server clock since run start) — the chart x-axis
   *  (#326). NOT charge-referenced: the ROAST TIME display (0:00 = charge, negative
   *  in preheat) is a label transform in LiveCurve, keyed on `originSeconds`. */
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
  /** x position in SERVE-elapsed seconds (same axis as CurvePoint.t, #326) — the
   *  ROAST TIME re-label is applied at display time, not here. */
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
  /**
   * Serve-elapsed seconds at the T0/charge moment, for the CHARGE-referenced ROAST
   * TIME display (#326). The point buffer is keyed on serve elapsed (so preheat
   * plots live); passing the charge origin re-labels the x-axis ticks + cursor
   * readout to roast time (0:00 = charge, negative before T0) WITHOUT moving any
   * point. `null` (default, or before T0 lands) → the axis shows serve-elapsed.
   * The dashboard passes `vm.t0ElapsedSeconds`; the detail page omits it.
   */
  originSeconds?: number | null;
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
  /**
   * The RENDERED uPlot scale ranges (min/max) for the x (elapsed), c (°C), ror
   * (°C/min) and pct (heat/fan %) scales, read off the live plot after each draw.
   *
   *   - x: asserted to COVER the data — a collapsed/unranged scale (the bug where the
   *     curve drew off-screen / onto one point) leaves a scale that does NOT span the
   *     data, which a blank-but-byte-deterministic snapshot can't catch (D26 / #131).
   *   - c (°C): CONTROLLED-DYNAMIC auto-range with hysteresis (#307) — a test asserts
   *     it COVERS the bean+env data (with padding) and does NOT collapse to a
   *     zero-width range; it is no longer the fixed 0–210 of #217.
   *   - ror (°C/min): FIXED −20..+30 (a band comparable across roasts) — asserted to
   *     hold its pinned bounds regardless of the data.
   *   - pct (%): FIXED 0–100 (the dedicated control-line axis, #307).
   */
  scales: {
    x: { min: number | null; max: number | null };
    c: { min: number | null; max: number | null };
    ror: { min: number | null; max: number | null };
    pct: { min: number | null; max: number | null };
  };
}

declare global {
  interface Window {
    /** Set by the most-recently-mounted LiveCurve (test-only, D24). */
    __chart?: ChartTestHook;
  }
}
