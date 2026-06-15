/**
 * LiveCurve axis-scaling (#217). The two VALUE axes are pinned to fixed ranges so
 * the operator reads the roast against an UNCHANGING frame and the curve never
 * auto-zooms to "wherever the sensors are now" (the live-roast misread: the axis
 * shifted under the data, and a high env made the charge readiness look alarming).
 * Only the x (time) axis stays data-driven. Kept in its own module so the chart
 * component file exports only components (react-refresh) and the pure range logic
 * stays unit-testable without a canvas.
 */

import type uPlot from "uplot";

// Column indices per scale (x, bean, env, ror, heat, fan). Only the `x` entry is
// consulted at runtime — `makeAutoRange` returns the FIXED range for `c`/`ror`
// BEFORE reading this map (#217), so the `c`/`ror` entries are documentation-only
// (they record which data columns feed each scale; not used to range them).
export const SCALE_COLUMNS: Record<string, number[]> = {
  x: [0],
  c: [1, 2], // doc-only: bean + env feed °C (fixed range — not data-driven)
  ror: [3], // doc-only: RoR column feeds the RoR axis (fixed range — not data-driven)
};

/**
 * FIXED value-axis ranges (#217), operator-confirmed 14 Jun (see issue #217):
 *
 *   - Temperature (°C, scale "c"): 0–210. Shows the whole roast — preheat climb →
 *     drop — at usable resolution, and always keeps the 170–200 charge band in
 *     frame without the band having to stretch the domain. Replaces both the old
 *     static 60–220 (which buried the preheat climb) and the charge-band-driven
 *     auto-fit (which keyed the range off the current temperature).
 *   - RoR (°C/min, scale "ror"): −20 to +30. Reads the development RoR (the decline
 *     into FC and the post-FC range); the charge-crash trough (~−90) dives off the
 *     bottom — visible as a plunge, exact trough intentionally clipped.
 *
 * A fixed scale is also inherently snapshot-stable, which suits the D26 Playwright
 * pixel gate.
 */
export const FIXED_SCALE_RANGES: Record<string, uPlot.Range.MinMax> = {
  c: [0, 210],
  ror: [-20, 30],
};

/** The charge-band extent (retained for the overlay; no longer ranges the axis). */
export interface ChargeBandRange {
  visible: boolean;
  minC: number;
  maxC: number;
}

/**
 * Build a uPlot `scales.<key>.range` callback.
 *
 * The two VALUE axes are pinned to `FIXED_SCALE_RANGES` (#217) so the curve never
 * auto-zooms to the current sensor reading; the 0–210 °C range always contains the
 * 170–200 charge band, so the band overlay stays in frame without stretching the
 * domain. Only the x (time) axis remains data-driven.
 *
 * The x scale must still re-range to the data on every `setData`: the plot is built
 * ONCE (on [height, meta]) while the live series is still EMPTY — the dashboard
 * mounts LiveCurve before any SSE frame arrives — so uPlot leaves x unset
 * (`{min:null,max:null}`), which collapses the series onto a single point at index 0
 * (invisible). This callback recomputes x's extent from `self.data` so it always
 * covers the loaded elapsed-time range; it is ranged tight (no padding — it is time).
 *
 * `getChargeBand` is retained for API symmetry but the fixed °C range already keeps
 * the band visible, so it no longer influences the domain.
 */
export function makeAutoRange(
  _getChargeBand: () => ChargeBandRange,
): (self: uPlot, min: number, max: number, scaleKey: string) => uPlot.Range.MinMax {
  return (self: uPlot, _min: number, _max: number, scaleKey: string): uPlot.Range.MinMax => {
    // Both value axes are FIXED (#217) — see FIXED_SCALE_RANGES for the rationale.
    const fixed = FIXED_SCALE_RANGES[scaleKey];
    if (fixed) return fixed;
    // The x (time) axis stays data-driven so it tracks the live roast duration.
    const cols = SCALE_COLUMNS[scaleKey] ?? [];
    let lo = Infinity;
    let hi = -Infinity;
    for (const ci of cols) {
      const series = self.data[ci];
      if (!series) continue;
      for (const v of series) {
        if (v == null || !Number.isFinite(v)) continue;
        if (v < lo) lo = v;
        if (v > hi) hi = v;
      }
    }
    // No finite data (empty mount): let uPlot keep whatever it passed.
    if (lo === Infinity || hi === -Infinity) return [_min, _max];
    // The x (time) axis is ranged tight (no soft padding).
    return [lo, hi];
  };
}
