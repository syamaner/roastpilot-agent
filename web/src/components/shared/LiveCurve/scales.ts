/**
 * LiveCurve axis-scaling (#217, #307).
 *
 * Three scales, three policies:
 *
 *   - Temperature (°C, scale "c"): CONTROLLED-DYNAMIC auto-range (#307). Fits to the
 *     bean + env data with padding so the curve never CLIPS (the #217 fixed 0–210
 *     pegged a hot Env at 210), but with HYSTERESIS so it neither (a) collapses to a
 *     single point / zero-width range (the #128 / livecurve-yscale failure mode) nor
 *     (b) jitters the axis every 1 s frame. The range only moves when the data pushes
 *     past the current bounds by a margin (or has shrunk well inside them), and then
 *     it snaps to padded, quantised bounds — so a small wobble inside the band leaves
 *     the axis untouched.
 *   - RoR (°C/min, scale "ror"): FIXED −20..+30. A fixed band keeps RoR readable and
 *     comparable ACROSS roasts; the charge/crash spike (e.g. −29 °C/min) clipping off
 *     the bottom is acceptable (operator, 21 Jun). NOT auto-ranged.
 *   - x (time, seconds): data-driven, ranged tight.
 *
 * Kept in its own module so the chart component file exports only components
 * (react-refresh) and the pure range logic stays unit-testable without a canvas.
 */

import type uPlot from "uplot";

import type { ChartColumns } from "./types";

// LOGICAL column indices per scale, in the canonical [x, bean, env, ror, heat, fan]
// layout (the order `toColumns` produces, BEFORE LiveCurve's draw-order permutation).
// The auto-range reads these against the LOGICAL columns — never `self.data`, whose
// columns LiveCurve reorders to draw heat/fan behind bean/env (#307). Indexing
// `self.data` here would scan the wrong series after the permutation (Augment, #341):
// post-permutation, plot columns 1,2 are heat/fan (0–100 %), not bean/env, which would
// range the temperature axis over the control percentages.
export const SCALE_COLUMNS: Record<string, number[]> = {
  x: [0],
  c: [1, 2], // bean + env feed the °C axis (auto-ranged, #307) — LOGICAL indices
  ror: [3], // doc-only: RoR column feeds the RoR axis (FIXED range — not data-driven)
};

/**
 * FIXED RoR range (°C/min, scale "ror"), operator-confirmed (#217, re-confirmed
 * 21 Jun for #307): −20 to +30. Reads the development RoR (the decline into FC and
 * the post-FC range); the charge-crash trough dives off the bottom — a fixed band
 * stays comparable across roasts, exact trough intentionally clipped.
 *
 * The temperature axis is NO LONGER fixed (#307) — it auto-ranges, see
 * {@link makeAutoRange}.
 */
export const FIXED_SCALE_RANGES: Record<string, uPlot.Range.MinMax> = {
  ror: [-20, 30],
};

/**
 * Temperature auto-range tuning (°C, scale "c", #307).
 *
 *   - `PAD`: padding added below the data min / above the data max, in °C, so the
 *     curve never touches the frame edge.
 *   - `QUANTUM`: bounds snap to this grid (°C). Quantising keeps the axis STABLE —
 *     small frame-to-frame data changes land in the same quantised bound, so the
 *     range (and the pixel baseline) does not jitter.
 *   - `MIN_SPAN`: the smallest allowed range width (°C), so a flat/sparse curve (the
 *     #128 zero-width failure) still gives the axis a usable, positive height.
 *   - `FLOOR`: the bean probe never reads below ~ambient; clamp the low bound here so
 *     the axis doesn't dip into physically-impossible negative °C on noise.
 */
export const TEMP_RANGE = {
  PAD: 8,
  QUANTUM: 10,
  MIN_SPAN: 40,
  FLOOR: 0,
} as const;

/** The charge-band extent (retained for the overlay; no longer ranges the axis). */
export interface ChargeBandRange {
  visible: boolean;
  minC: number;
  maxC: number;
}

/**
 * Mutable hysteresis state for the temperature auto-range (#307). One instance per
 * mounted plot — `makeAutoRange` reads + updates `tempRange` across range-callback
 * invocations so the axis only moves when the data leaves the current band. `null`
 * until the first finite reading establishes a range.
 */
export interface AutoRangeState {
  tempRange: uPlot.Range.MinMax | null;
}

/** Round `v` DOWN to the nearest `q` grid line. */
function floorTo(v: number, q: number): number {
  return Math.floor(v / q) * q;
}

/** Round `v` UP to the nearest `q` grid line. */
function ceilTo(v: number, q: number): number {
  return Math.ceil(v / q) * q;
}

/**
 * Compute the controlled-dynamic temperature range for the current data extent.
 *
 * Coverage is the invariant: the returned range ALWAYS covers the currently-loaded
 * bean+env data with padding, so a full-data (re)mount jumps straight to the whole
 * span (#341 — never stuck at a narrow earlier range). Hysteresis is applied ON TOP
 * only to resist jitter: it lets the frame stay put on a sub-quantum wobble and
 * resists contracting after a transient peak — it can never make the range
 * under-cover the data.
 *
 * Exported for direct unit testing (the range callback wires it to live plot data).
 *
 * @param dataLo - finite data minimum across bean + env, or `null` if no finite data.
 * @param dataHi - finite data maximum across bean + env, or `null` if no finite data.
 * @param prev - the previously-settled range, or `null` on first range.
 * @returns the range to use now (possibly unchanged from `prev`).
 */
export function computeTempRange(
  dataLo: number | null,
  dataHi: number | null,
  prev: uPlot.Range.MinMax | null,
): uPlot.Range.MinMax {
  const { PAD, QUANTUM, MIN_SPAN, FLOOR } = TEMP_RANGE;

  // No finite data yet (empty mount): hold the previous range if we have one, else a
  // sensible default band so the empty plot has a real height (and never a zero-width
  // range — the #128 / livecurve-yscale collapse).
  if (dataLo === null || dataHi === null) {
    return prev ?? [FLOOR, FLOOR + Math.max(MIN_SPAN, 2 * PAD + QUANTUM)];
  }

  // Target bounds: pad the data, clamp the low end to the physical floor, then snap
  // to the quantum grid so equivalent frames produce an identical range (stability).
  let targetLo = floorTo(Math.max(FLOOR, dataLo - PAD), QUANTUM);
  let targetHi = ceilTo(dataHi + PAD, QUANTUM);

  // Enforce a minimum span around the data midpoint so a flat/sparse curve still has
  // a usable height (and the range is never degenerate — the #128 guard).
  if (targetHi - targetLo < MIN_SPAN) {
    const mid = (dataLo + dataHi) / 2;
    targetLo = floorTo(Math.max(FLOOR, mid - MIN_SPAN / 2), QUANTUM);
    targetHi = ceilTo(targetLo + MIN_SPAN, QUANTUM);
  }

  // First range (or a partially-null previous range): adopt the target outright.
  if (prev === null || prev[0] === null || prev[1] === null) return [targetLo, targetHi];

  const prevLo: number = prev[0];
  const prevHi: number = prev[1];

  // COVERAGE FIRST, then hysteresis (the #131 / #341 lesson). The returned range MUST
  // always cover the currently-loaded data with padding — a full-data (re)mount (the
  // detail page, or the dashboard re-hydrating the whole curve from REST in one
  // setData) must jump straight to covering the whole ~0–205 °C span, never stay
  // pinned at a narrow earlier range (the bug where a developed curve left c.max ≈ 60).
  // Hysteresis only DAMPS jitter: it lets the frame stay PUT on a small wobble, and
  // resists CONTRACTING after a transient peak — it must never UNDER-cover the data.
  //
  // So each bound expands to the padded target whenever the data pokes outside the
  // current frame (unconditional coverage), and otherwise holds unless enough slack
  // has opened to justify contracting.
  const slack = QUANTUM;
  const paddedLo = dataLo - PAD;
  const paddedHi = dataHi + PAD;

  let nextLo = prevLo;
  let nextHi = prevHi;

  // Lower bound: expand DOWN to cover data below the frame; else contract up only
  // once a full quantum of slack has opened (damped), never tighter than the target.
  if (paddedLo < prevLo) nextLo = Math.min(prevLo, targetLo); // cover data below → expand down
  else if (paddedLo > prevLo + slack) nextLo = targetLo; // slack opened → contract up

  // Upper bound: expand UP to cover data above the frame (this is the coverage
  // guarantee that fixes the c.max-stuck-at-60 bug); else contract down only once a
  // full quantum of slack has opened (damped).
  if (paddedHi > prevHi) nextHi = Math.max(prevHi, targetHi); // cover data above → expand up
  else if (paddedHi < prevHi - slack) nextHi = targetHi; // slack opened → contract down

  // Final coverage clamp + degeneracy guard: whatever hysteresis decided, the range
  // must still cover the padded data and never be narrower than MIN_SPAN. This is the
  // belt-and-braces guarantee that a kept-prev bound can never leave the data clipped.
  nextLo = Math.min(nextLo, targetLo);
  nextHi = Math.max(nextHi, targetHi);
  if (nextHi - nextLo < MIN_SPAN) return [targetLo, targetHi];
  return [nextLo, nextHi];
}

/** Scan the finite extent of `columns` at the given LOGICAL indices. */
function dataExtent(columns: ChartColumns, indices: number[]): { lo: number; hi: number; hasData: boolean } {
  let lo = Infinity;
  let hi = -Infinity;
  for (const ci of indices) {
    const series = columns[ci];
    if (!series) continue;
    for (const v of series) {
      if (v == null || !Number.isFinite(v)) continue;
      if (v < lo) lo = v;
      if (v > hi) hi = v;
    }
  }
  return { lo, hi, hasData: lo !== Infinity && hi !== -Infinity };
}

/**
 * Build a uPlot `scales.<key>.range` callback.
 *
 *   - "c" (temperature): controlled-dynamic auto-range with hysteresis (#307), via
 *     {@link computeTempRange}, reading bean + env from the LOGICAL columns (#341 —
 *     NOT `self.data`, whose columns LiveCurve permutes to draw heat/fan behind, which
 *     would otherwise range the temp axis over the control percentages). The settled
 *     range is stashed on `state.tempRange` so the next call applies hysteresis.
 *   - "ror": FIXED −20..+30 (#217) — a fixed band, comparable across roasts.
 *   - "x" (time): data-driven, ranged tight, with a zero-width guard (the #326 /
 *     #334 degenerate-x hardening — a single point or several at the same second
 *     widened to a small symmetric window so uPlot's split calc never divides by a
 *     zero span and throws "Invalid array length").
 *
 * `getLogicalColumns` returns the canonical [x, bean, env, ror, heat, fan] columns
 * (pre-permutation) so the range is derived from each series' LOGICAL identity,
 * independent of draw order. `getChargeBand` is retained for API symmetry (the
 * overlay reads the band live); neither value axis is driven by it.
 */
export function makeAutoRange(
  getChargeBand: () => ChargeBandRange,
  state: AutoRangeState,
  getLogicalColumns: () => ChartColumns,
): (self: uPlot, min: number, max: number, scaleKey: string) => uPlot.Range.MinMax {
  return (_self: uPlot, _min: number, _max: number, scaleKey: string): uPlot.Range.MinMax => {
    // RoR is FIXED (#217) — a fixed band stays comparable across roasts.
    const fixed = FIXED_SCALE_RANGES[scaleKey];
    if (fixed) return fixed;

    // Read the data extent from the LOGICAL columns by this scale's logical indices —
    // NOT `self.data` (which LiveCurve has permuted into draw order, #341).
    const cols = SCALE_COLUMNS[scaleKey] ?? [];
    const { lo, hi, hasData } = dataExtent(getLogicalColumns(), cols);

    // Temperature axis (#307): controlled-dynamic with hysteresis.
    if (scaleKey === "c") {
      // While the charge band is shown (preheating), the band MUST stay on-screen — it
      // is the charge-readiness target (E10-spa.md). Preheat data is cool (~30–60 °C),
      // so a pure data-fit would push the 170–200 band off the top; fold the band into
      // the extent so the auto-range always covers BOTH the curve and the band. Once
      // the band is hidden (post-charge) the range fits the curve alone, free to climb.
      let lo2 = lo;
      let hi2 = hi;
      let has = hasData;
      const band = getChargeBand();
      if (band.visible) {
        lo2 = has ? Math.min(lo2, band.minC) : band.minC;
        hi2 = has ? Math.max(hi2, band.maxC) : band.maxC;
        has = true;
      }
      const next = computeTempRange(has ? lo2 : null, has ? hi2 : null, state.tempRange);
      state.tempRange = next;
      return next;
    }

    // The x (time) axis stays data-driven so it tracks the live roast duration.
    // No finite data (empty mount): let uPlot keep whatever it passed.
    if (!hasData) return [_min, _max];
    // Guard a DEGENERATE (zero-width) x-range: a single plotted point — or several
    // points all at the same elapsed second — gives lo === hi, and uPlot's split
    // calc (numAxisSplits) divides by the span, producing a non-finite increment →
    // `new Array(NaN)` → "RangeError: Invalid array length", which throws out of the
    // async commit and breaks the dashboard React tree (seen on short/sparse roasts:
    // the preheat curve starts as one point now that pre-charge frames plot, #326).
    // Widen to a small symmetric window so the axis always has a positive width; the
    // single point sits centered. A 1 s half-window matches the 1 s tick cadence.
    if (lo === hi) return [lo - 1, hi + 1];
    // The x (time) axis is ranged tight (no soft padding).
    return [lo, hi];
  };
}
