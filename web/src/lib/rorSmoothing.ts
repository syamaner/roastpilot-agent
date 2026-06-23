/**
 * Display-only curve smoothing for the bean-temperature and RoR (rate-of-rise)
 * series (#205, #344).
 *
 * The `bean_ror_c_per_min` channel is derived server-side from a short-window
 * temperature delta at the 1 Hz controller tick, and the Hottop thermocouple
 * resolution is coarse, so the slope quantises into a visible **staircase** (it
 * climbs in discrete integer-ish steps rather than a smooth slope). The bean
 * temperature itself climbs the same way — small flat-then-jump segments at the
 * coarse probe resolution. This module smooths BOTH channels for RENDERING ONLY,
 * leaving every other channel and the raw telemetry untouched.
 *
 * ## Scope: display-only — never alters the raw signal
 *
 * This runs in the SPA over the already-plotted points. It does NOT change what
 * the controller feeds the advisor or what the safety policy evaluates — those
 * consume the raw `bean_temp_c` / `bean_ror_c_per_min` server-side, well before
 * any of this. The caveat on #205 is explicit: over-smoothing adds lag that would
 * dull the pre-FC anticipatory heat-cut (which already fights the ~12-21 s
 * audio-detector lag). So this layer is cosmetic/legibility only; the raw series
 * remains the source of truth for every control decision.
 *
 * ## Why a CENTERED Savitzky-Golay fit (not a moving average) — #344
 *
 * #205 dissolved the staircase with a centered **moving average** on the RoR
 * channel only. The operator's read after roast 3 is that it is not enough: the
 * bean line is still raw and staircases, and a wider box-car MA starts to round
 * off the post-charge RoR crash and the pre-FC flick — the very shapes the
 * operator times the heat-cut and the drop on.
 *
 * A **quadratic Savitzky-Golay** filter fits a least-squares parabola over each
 * centered window and takes the fitted centre. At a given window width it removes
 * the high-frequency quantisation steps while **preserving peak height and slope**
 * far better than a moving average, because the local parabola tracks a curving
 * trend (a crash trough, a flick) instead of averaging it flat. It stays a fixed
 * linear convolution on uniform-cadence data ⇒ deterministic ⇒ compatible with the
 * D26 pixel-snapshot gate (unlike an adaptive one-Euro / EMA filter, which would
 * also add structural trailing lag — the failure mode #205 rejected).
 *
 * Centered (symmetric) ⇒ ~zero net lag on the persisted/detail curve: the smoothed
 * peak/trough sits at the same x as the raw one. On the LIVE dashboard the newest
 * point has no future neighbours, so a one-sided parabola there can over/undershoot
 * (wobble); the **live tail (a point lacking enough right-side neighbours to
 * bracket the centre) falls back to the RAW point** rather than showing that
 * wobble — meaning the freshest reading (where lag/wobble would hurt the operator's
 * FC read most) is shown raw, then fills in symmetrically as later ticks arrive.
 *
 * The window is expressed in SECONDS (not a fixed point count) so it behaves the
 * same whether points arrive at the 1 Hz live cadence or a sparser persisted
 * snapshot cadence, and a gap in the signal bounds the window so we never
 * least-squares across a missing-signal stretch.
 *
 * All temperatures Celsius; RoR is °C/min.
 */

import type { CurvePoint } from "@/components/shared/LiveCurve/types";

/**
 * Smoothing window, in seconds (centered: ±{@link CURVE_SMOOTHING_WINDOW_SECONDS}/2).
 *
 * Pinned to **21 s** (mid-band of the operator-approved 15-25 s range, #344). The
 * width was validated against a roast-3-style RoR replay (1 Hz, post-charge crash
 * to ~-29 °C/min, a pre-FC flick) the same way #205's 15 s was justified:
 *
 *   - At 21 s the quadratic SG retains the −29 °C/min crash trough at ~−28 °C/min
 *     (≈97 %) and the flick peak almost intact, while the centered **moving average**
 *     of #205 — even at its narrower 15 s — rounds the same crash to ~−24 °C/min and
 *     keeps blunting it as the window widens (≈−21 at 21 s, ≈−18 at 25 s). So SG at
 *     21 s smooths over a WIDER window than the old 15 s MA yet preserves the
 *     crash/flick BETTER than that MA did — the whole point of moving to SG.
 *   - The bean staircase dissolves into a strictly-increasing line at 21 s, with the
 *     smoothed interior tracking the true underlying ramp to within ~0.05 °C.
 *   - 25 s started to visibly soften the flick; 15 s left a touch more residual
 *     step on the bean line. 21 s is the knee: maximum staircase removal that still
 *     reads the crash depth and the flick clearly.
 *
 * Centered, so it adds no net lag on the persisted curve and the live tail falls
 * back to the raw point rather than lagging the freshest reading.
 */
export const CURVE_SMOOTHING_WINDOW_SECONDS = 21;

/**
 * Back-compat alias for the #205 RoR window constant. The single pinned width now
 * covers both channels (#344); kept so older imports/tests resolve.
 *
 * @deprecated Use {@link CURVE_SMOOTHING_WINDOW_SECONDS}.
 */
export const ROR_SMOOTHING_WINDOW_SECONDS = CURVE_SMOOTHING_WINDOW_SECONDS;

/** The display channels SG-smoothed for rendering (#344): the bean line and RoR. */
type SmoothChannel = "bean" | "ror";
const SMOOTH_CHANNELS: readonly SmoothChannel[] = ["bean", "ror"];

/**
 * Centered quadratic Savitzky-Golay smoothed value for the point at `i`, over a
 * ±`half`-second symmetric window, or `null` if the point itself is a gap.
 *
 * The fit:
 *   - Walks outward from `i` collecting neighbours within the time band on each
 *     side. A `null` neighbour BOUNDS the window (a gap stops the walk on that
 *     side); we never least-squares across a missing-signal stretch.
 *   - Requires the window to BRACKET the centre — at least one real neighbour on
 *     BOTH sides — before fitting. A point without right-side neighbours is the
 *     LIVE TAIL: a one-sided parabola there wobbles, so the caller falls back to
 *     the raw value (this function signals that by returning the raw value when it
 *     cannot bracket, see {@link bracketed}).
 *   - Least-squares-fits y = a + b·x + c·x² (x measured from the centre's t) and
 *     returns `a`, the fitted value AT the centre. On uniform 1 Hz spacing this is
 *     exactly the closed-form centered SG convolution; the local fit generalises it
 *     to sparse/irregular cadence and degenerate windows.
 *   - Falls back to the raw value if fewer than 3 points (a parabola is
 *     under-determined) — preserving the #205 "a lone sample is unchanged" contract.
 *
 * Pure helper over the channel's numeric values; gap handling is the caller's.
 */
function savitzkyGolayCentre(
  ts: number[],
  ys: (number | null)[],
  i: number,
  half: number,
): { value: number; bracketed: boolean } {
  const yc = ys[i] as number; // caller guarantees non-null
  const tc = ts[i];

  // Collect the symmetric, gap-bounded, time-bounded window around i.
  const xs: number[] = [0];
  const vs: number[] = [yc];
  let hasLeft = false;
  let hasRight = false;

  for (let j = i - 1; j >= 0; j -= 1) {
    if (tc - ts[j] > half) break;
    const v = ys[j];
    if (v === null) break; // a gap bounds the window on this side
    xs.push(ts[j] - tc);
    vs.push(v);
    hasLeft = true;
  }
  for (let j = i + 1; j < ts.length; j += 1) {
    if (ts[j] - tc > half) break;
    const v = ys[j];
    if (v === null) break;
    xs.push(ts[j] - tc);
    vs.push(v);
    hasRight = true;
  }

  // The window must BRACKET the centre for a no-net-lag centered fit. A point with
  // no right-side neighbour is the live tail (or a gap edge): report not-bracketed
  // so the caller renders the raw point instead of a wobbly one-sided fit.
  const bracketed = hasLeft && hasRight;
  if (xs.length < 3) return { value: yc, bracketed: false };

  // Solve the 3×3 normal equations for the quadratic least-squares fit
  // y = a + b·x + c·x²; we only need `a` (the fitted value at x = 0, the centre).
  // Moments of x (S0..S4) and cross-moments with y (T0..T2).
  let s0 = 0;
  let s1 = 0;
  let s2 = 0;
  let s3 = 0;
  let s4 = 0;
  let t0 = 0;
  let t1 = 0;
  let t2 = 0;
  for (let k = 0; k < xs.length; k += 1) {
    const x = xs[k];
    const x2 = x * x;
    const y = vs[k];
    s0 += 1;
    s1 += x;
    s2 += x2;
    s3 += x2 * x;
    s4 += x2 * x2;
    t0 += y;
    t1 += x * y;
    t2 += x2 * y;
  }

  // Cramer's rule on the symmetric normal matrix
  //   [ s0 s1 s2 ] [a]   [t0]
  //   [ s1 s2 s3 ] [b] = [t1]
  //   [ s2 s3 s4 ] [c]   [t2]
  const m00 = s2 * s4 - s3 * s3;
  const m01 = s1 * s4 - s2 * s3;
  const m02 = s1 * s3 - s2 * s2;
  const det = s0 * m00 - s1 * m01 + s2 * m02;

  // Singular (e.g. all x identical — should not happen for ascending distinct t,
  // but guard anyway): fall back to the raw value rather than divide by ~0.
  if (!Number.isFinite(det) || Math.abs(det) < 1e-12) {
    return { value: yc, bracketed };
  }

  // a = det(matrix with column 0 replaced by [t0,t1,t2]) / det.
  const a = (t0 * m00 - s1 * (t1 * s4 - t2 * s3) + s2 * (t1 * s3 - t2 * s2)) / det;
  return { value: a, bracketed };
}

/**
 * Return a copy of `points` with the bean and RoR channels replaced by their
 * centered quadratic Savitzky-Golay fit over a ±half-window time band (#344); every
 * other field is passed through unchanged. A new array of new objects is returned
 * (object identity is not preserved), suitable as a memoised render input.
 *
 * Behaviour (per channel, independently):
 *   - A point whose own value is `null` stays `null` (a gap never becomes a
 *     fabricated value).
 *   - A `null` neighbour BOUNDS the window: the scan stops at the gap on that side,
 *     so values from the far side of a missing-signal gap never enter the fit.
 *   - The **live tail** — a point the window cannot bracket on both sides (the
 *     newest points, with no future neighbours yet) — falls back to its RAW value,
 *     because a one-sided parabola there wobbles. As later ticks arrive the point
 *     becomes bracketed and the fitted value fills in symmetrically.
 *   - Points are assumed ascending in `t` (the curve invariant).
 *
 * @param points  The curve points (ascending `t`, seconds).
 * @param windowSeconds  Total centered window width in seconds; defaults to
 *   {@link CURVE_SMOOTHING_WINDOW_SECONDS}. Values <= 0 return the points unchanged.
 */
export function smoothCurveForDisplay(
  points: CurvePoint[],
  windowSeconds: number = CURVE_SMOOTHING_WINDOW_SECONDS,
): CurvePoint[] {
  if (points.length === 0 || windowSeconds <= 0) return points;

  const half = windowSeconds / 2;
  const ts = points.map((p) => p.t);

  // Precompute each channel's smoothed value array so the per-point map can apply
  // both without re-walking the window per channel inside the object spread.
  const smoothedByChannel: Record<SmoothChannel, (number | null)[]> = {
    bean: [],
    ror: [],
  };
  for (const channel of SMOOTH_CHANNELS) {
    const ys = points.map((p) => p[channel]);
    smoothedByChannel[channel] = points.map((point, i) => {
      if (point[channel] === null) return null; // gap stays a gap
      const { value, bracketed } = savitzkyGolayCentre(ts, ys, i, half);
      // Live tail / unbracketed → show the raw point, no one-sided wobble.
      if (!bracketed) return point[channel];
      return value;
    });
  }

  return points.map((point, i) => {
    const bean = smoothedByChannel.bean[i];
    const ror = smoothedByChannel.ror[i];
    // Cheap identity guard: if neither channel moved, reuse the original object.
    if (bean === point.bean && ror === point.ror) return point;
    return { ...point, bean, ror };
  });
}

/**
 * Back-compat wrapper for the #205 call sites: smooth the curve and return it.
 *
 * #344 promoted smoothing from RoR-only to bean+RoR; this delegates to
 * {@link smoothCurveForDisplay} so existing `smoothRorForDisplay(...)` imports keep
 * working while the new behaviour applies. Prefer importing
 * {@link smoothCurveForDisplay} directly in new code.
 *
 * @deprecated Use {@link smoothCurveForDisplay}.
 */
export function smoothRorForDisplay(
  points: CurvePoint[],
  windowSeconds: number = CURVE_SMOOTHING_WINDOW_SECONDS,
): CurvePoint[] {
  return smoothCurveForDisplay(points, windowSeconds);
}
