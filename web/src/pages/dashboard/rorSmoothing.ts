/**
 * Display-only smoothing for the RoR (rate-of-rise) curve series (#205).
 *
 * The `bean_ror_c_per_min` channel is derived server-side from a short-window
 * temperature delta at the 1 Hz controller tick, and the Hottop thermocouple
 * resolution is coarse, so the slope quantises into a visible **staircase** (it
 * climbs in discrete integer-ish steps rather than a smooth slope). This module
 * smooths the RoR channel for RENDERING ONLY, leaving every other channel and the
 * raw telemetry untouched.
 *
 * ## Scope: display-only — never alters the raw signal
 *
 * This runs in the SPA over the already-plotted points. It does NOT change what
 * the controller feeds the advisor or what the safety policy evaluates — those
 * consume the raw `bean_ror_c_per_min` server-side, well before any of this. The
 * caveat on #205 is explicit: over-smoothing adds lag that would dull the pre-FC
 * anticipatory heat-cut (which already fights the ~12-21 s audio-detector lag). So
 * this layer is cosmetic/legibility only; the raw series remains the source of
 * truth for every control decision.
 *
 * ## Why a CENTERED moving average
 *
 * A centered (symmetric) window averages each point against its neighbours on
 * BOTH sides, so over the persisted/detail curve it adds ~zero net lag — the
 * smoothed peak/trough sits at the same x as the raw one. (A trailing/EMA filter
 * would shift the whole curve right by ~half the window, blunting exactly the
 * crash/flick shape that matters for the drop.) On the LIVE dashboard the latest
 * point has no future neighbours yet, so the window naturally shrinks at the tail
 * — meaning the freshest reading (where lag would hurt the operator's FC read
 * most) is smoothed the least, then fills in symmetrically as later ticks arrive.
 *
 * The window is expressed in SECONDS (not a fixed point count) so it behaves the
 * same whether points arrive at the 1 Hz live cadence or a sparser persisted
 * snapshot cadence.
 *
 * All temperatures Celsius; RoR is °C/min.
 */

import type { CurvePoint } from "@/components/shared/LiveCurve/types";

/**
 * RoR smoothing window, in seconds (centered: ±{@link ROR_SMOOTHING_WINDOW_SECONDS}/2).
 *
 * 15 s sits at the low end of the range roasting tools use for RoR smoothing
 * (Artisan commonly 15-30 s). Rationale for the low end: it is enough to dissolve
 * the integer-step staircase at the coarse thermocouple resolution and the slow
 * (multi-second) Hottop probe response, while a wider window would start to round
 * off the post-charge RoR crash and the pre-FC flick — shape the operator (and,
 * separately/server-side, the drop logic) must still read. Centered, so it adds
 * no net lag on the persisted curve and only tail-edge lag live.
 */
export const ROR_SMOOTHING_WINDOW_SECONDS = 15;

/**
 * Return a copy of `points` with ONLY the `ror` channel replaced by a centered
 * moving average over a ±half-window time band; every other field is passed
 * through unchanged (object identity is not preserved — a new array of new objects
 * is returned, suitable for a memoised render input).
 *
 * Behaviour:
 *   - A point whose own RoR is `null` stays `null` (a gap never becomes a
 *     fabricated value).
 *   - A `null` neighbour BOUNDS the window: the scan stops at the gap on that
 *     side, so values from the far side of a missing-signal gap never enter the
 *     average. We smooth a contiguous run of real samples; we do not interpolate
 *     across gaps. (A point sitting next to a gap is averaged only with the real
 *     samples between it and the gap, never across it.)
 *   - Points are assumed ascending in `t` (the curve invariant); the window is a
 *     symmetric scan outward from each index, bounded by the time band AND by any
 *     gap.
 *
 * @param points  The curve points (ascending `t`, seconds since T0).
 * @param windowSeconds  Total centered window width in seconds; defaults to
 *   {@link ROR_SMOOTHING_WINDOW_SECONDS}. Values <= 0 return the points unchanged.
 */
export function smoothRorForDisplay(
  points: CurvePoint[],
  windowSeconds: number = ROR_SMOOTHING_WINDOW_SECONDS,
): CurvePoint[] {
  if (points.length === 0 || windowSeconds <= 0) return points;

  const half = windowSeconds / 2;

  return points.map((point, i) => {
    // A genuine gap stays a gap — we never invent a RoR where the signal had none.
    if (point.ror === null) return point;

    const tCenter = point.t;
    let sum = point.ror;
    let count = 1;

    // Walk left while within the half-window time band. A null neighbour is a
    // missing-signal gap: it STOPS the walk (it is not merely skipped), so a value
    // from the FAR side of a gap never enters the average — we smooth a contiguous
    // run of real samples, we do not interpolate across a gap (the documented
    // contract). The gap therefore bounds the window on this side.
    for (let j = i - 1; j >= 0; j -= 1) {
      if (tCenter - points[j].t > half) break;
      const v = points[j].ror;
      if (v === null) break;
      sum += v;
      count += 1;
    }
    // Walk right while within the half-window time band (same gap-stop rule).
    for (let j = i + 1; j < points.length; j += 1) {
      if (points[j].t - tCenter > half) break;
      const v = points[j].ror;
      if (v === null) break;
      sum += v;
      count += 1;
    }

    const smoothed = sum / count;
    // Cheap identity guard: a lone real sample (no neighbours) is unchanged.
    if (smoothed === point.ror) return point;
    return { ...point, ror: smoothed };
  });
}
