import { describe, expect, it } from "vitest";

import type { CurvePoint } from "@/components/shared/LiveCurve/types";
import { CURVE_SMOOTHING_WINDOW_SECONDS, smoothCurveForDisplay } from "./rorSmoothing";

/**
 * Build a curve of evenly-spaced points. `rors` drives the RoR channel; `beans`
 * (optional) drives the bean channel — when omitted bean is a smooth linear ramp
 * so RoR-focused tests can ignore it.
 */
function curve(
  rors: (number | null)[],
  stepSeconds = 1,
  beans?: (number | null)[],
): CurvePoint[] {
  return rors.map((ror, i) => ({
    t: i * stepSeconds,
    bean: beans ? beans[i] : 100 + i,
    env: 120 + i,
    ror,
    heat: 80,
    fan: 40,
  }));
}

const rorOf = (points: CurvePoint[]): (number | null)[] => points.map((p) => p.ror);
const beanOf = (points: CurvePoint[]): (number | null)[] => points.map((p) => p.bean);

describe("smoothCurveForDisplay — Savitzky-Golay kernel (#344)", () => {
  it("matches the closed-form centered quadratic SG coefficients on a uniform window", () => {
    // For a 7-point window (half-width 3) at unit spacing the quadratic SG centre
    // coefficients are c_i = (3(3m²+3m-1) − 15 i²) / ((2m+3)(2m+1)(2m-1)), m=3:
    //   norm = 9·7·5 = 315; centre 0.333…, ±1: 0.2857…, ±2: 0.142857…, ±3: −0.095238…
    // Convolve those with a known RoR vector and compare to the fitted centre.
    const ror = [3, 1, 4, 1, 5, 9, 2, 6, 5, 3, 5];
    // window 6 s → ±3 s → ±3 samples at 1 Hz (a 7-point window).
    const smoothed = rorOf(smoothCurveForDisplay(curve(ror), 6)) as number[];

    const c = [-0.0952380952, 0.1428571429, 0.2857142857, 0.3333333333, 0.2857142857, 0.1428571429, -0.0952380952];
    const i = 5; // a fully-bracketed interior index
    const expected = c.reduce((acc, w, k) => acc + w * ror[i - 3 + k], 0);
    expect(smoothed[i]).toBeCloseTo(expected, 9);
  });

  it("reproduces a quadratic exactly — SG fits parabolas with no bias (unlike an MA)", () => {
    // y = 2 + 0.5x − 0.03x²: a perfect parabola. A quadratic SG returns the curve
    // unchanged at every bracketed interior point (a moving average would NOT — it
    // pulls a curved peak toward the chord, flattening it).
    const xs = Array.from({ length: 25 }, (_, i) => i);
    const para = xs.map((x) => 2 + 0.5 * x - 0.03 * x * x);
    const smoothed = rorOf(smoothCurveForDisplay(curve(para), 11)) as number[];
    // interior (bracketed) points reproduce the parabola to numerical precision.
    for (let i = 6; i < smoothed.length - 6; i += 1) {
      expect(smoothed[i]).toBeCloseTo(para[i], 6);
    }
  });

  it("dissolves the quantised staircase into distinct ascending values (the #205/#344 bug)", () => {
    const staircase = curve([10, 10, 10, 12, 12, 12, 14, 14, 14, 16, 16, 16]);
    const smoothed = rorOf(smoothCurveForDisplay(staircase, 7)) as number[];
    const distinct = new Set(
      // ignore the raw-fallback tail; the interior must be step-free
      smoothed.slice(3, -3).map((v) => v.toFixed(4)),
    );
    expect(distinct.size).toBeGreaterThan(new Set([10, 12, 14, 16]).size - 1);
  });

  it("does not lag a peak — a centered SG keeps the apex at the same x", () => {
    // Symmetric triangle peaking at index 5; SG keeps the max at index 5 (a trailing
    // filter / EMA would shift it right).
    const tri = curve([0, 1, 2, 3, 4, 5, 4, 3, 2, 1, 0]);
    const smoothed = rorOf(smoothCurveForDisplay(tri, 5)) as number[];
    let maxIdx = 0;
    for (let i = 1; i < smoothed.length; i += 1) {
      if (smoothed[i] > smoothed[maxIdx]) maxIdx = i;
    }
    expect(maxIdx).toBe(5);
  });

  it("preserves the crash trough far better than a same-width box-car would", () => {
    // A roast-3-style post-charge RoR crash: steady ~8, a V-shaped plunge bottoming
    // at −29, recover (a real crash is a wide V, not a one-sample spike — the replay
    // crash spanned ~25 s, so it must be comparable to the window). The SG trough must
    // survive (the drop logic / operator read needs the depth) and stay centered. SG
    // keeps far more depth than the box-car mean of the SAME window — the whole point.
    const ror = [8, 4, -2, -10, -19, -29, -19, -10, -2, 4, 8];
    const window = 11;
    const smoothed = rorOf(smoothCurveForDisplay(curve(ror), window)) as number[];
    const min = Math.min(...smoothed);
    const minIdx = smoothed.indexOf(min);
    // box-car (window mean over the whole 11-pt V) ≈ the average of the V ≈ −6.1.
    const boxcar = ror.reduce((a, b) => a + b, 0) / ror.length;
    // SG retains ~63% of the −29 depth here; the box-car of the same window retains
    // ~21%. Assert SG is materially deeper than the MA (the whole point of #344) and
    // still reads as a deep crash, not flattened away.
    expect(min).toBeLessThan(boxcar - 10); // SG dip is far deeper than the box-car mean
    expect(min).toBeLessThan(-15); // and it stays a genuine deep crash
    expect(minIdx).toBe(5); // centered — trough stays at the raw dip's x
  });

  it("preserves the pre-FC flick — a fast upward bump survives, peak not lagged", () => {
    // The pre-FC "flick" the operator times the drop on: a steady RoR with a brief
    // upward bump. SG must keep the bump's height (a box-car would round it) and its
    // x (centered, no lag) — the named #344 criterion alongside the crash depth.
    // a wider flick (≈ the window) so the peak survives clearly, like the ~25 s crash.
    const ror = [10, 10, 11, 13, 16, 20, 16, 13, 11, 10, 10];
    const smoothed = rorOf(smoothCurveForDisplay(curve(ror), 11)) as number[];
    const max = Math.max(...smoothed);
    const maxIdx = smoothed.indexOf(max);
    const boxcar = ror.reduce((a, b) => a + b, 0) / ror.length; // ≈ 12.7
    expect(max).toBeGreaterThan(boxcar + 2); // SG keeps the bump above the MA mean
    expect(max).toBeGreaterThan(15); // and it reads as a real flick, not flattened
    // centered — the smoothed peak sits over the raw apex (index 5), within ±1 of it
    // (the smoothed top is a near-flat plateau, so allow the FP tie to land at 4/5/6),
    // never displaced toward the flat shoulders the way a trailing filter would push it.
    expect(maxIdx).toBeGreaterThanOrEqual(4);
    expect(maxIdx).toBeLessThanOrEqual(6);
  });

  it("smooths the BEAN channel too (the #344 addition — bean is no longer raw)", () => {
    // A bean staircase: flat-then-jump climb. SG must turn the interior into a
    // strictly-increasing, step-free line (today only RoR was smoothed).
    const beans = [70, 70, 70, 71, 71, 71, 72, 72, 72, 73, 73, 73];
    const points = curve(new Array(beans.length).fill(5), 1, beans);
    const smoothed = beanOf(smoothCurveForDisplay(points, 7)) as number[];
    const interior = smoothed.slice(3, -3);
    for (let i = 1; i < interior.length; i += 1) {
      expect(interior[i]).toBeGreaterThan(interior[i - 1]); // strictly increasing
    }
    // and it does not run away from the raw range
    expect(Math.min(...interior)).toBeGreaterThanOrEqual(70);
    expect(Math.max(...interior)).toBeLessThanOrEqual(73);
  });

  it("falls back to the RAW point at the live tail (no one-sided wobble)", () => {
    // Only the LAST point (index 10) has zero right-side neighbours and is unbracketed
    // → renders RAW. Indices 8 and 9 each have at least one right neighbour within
    // ±3.5 s (window 7 → half 3.5 s) and are bracketed/smoothed normally. Build a
    // curve whose last raw value is deliberately spiky so a fit would visibly differ.
    const ror = [10, 10, 10, 10, 10, 10, 10, 10, 30, 5, 25];
    const points = curve(ror);
    const smoothed = rorOf(smoothCurveForDisplay(points, 7)) as number[];
    // index 10: zero right neighbours → not bracketed → raw 25.
    expect(smoothed[smoothed.length - 1]).toBe(25);
    // an early interior point IS bracketed and therefore smoothed (≈10, not spiky).
    expect(smoothed[4]).toBeCloseTo(10, 6);
  });

  it("a single point and a two-point curve stay raw (cannot bracket / under-determined)", () => {
    expect(rorOf(smoothCurveForDisplay(curve([7]), 21))).toEqual([7]);
    // two points: each lacks a neighbour on one side → both raw.
    expect(rorOf(smoothCurveForDisplay(curve([7, 9]), 21))).toEqual([7, 9]);
  });

  it("keeps a null sample as a gap (the gap point itself stays null), per channel", () => {
    const points = curve([10, 10, null, 12, 12], 1, [70, 70, null, 72, 72]);
    const smoothed = smoothCurveForDisplay(points, 9);
    expect(smoothed[2].ror).toBeNull();
    expect(smoothed[2].bean).toBeNull();
  });

  it("does NOT fit across a gap — a gap bounds the window on each side", () => {
    // [10, null, 30] with a wide window: index 0 has no real right neighbour within
    // its contiguous run (the gap bounds it) → not bracketed → raw 10. Likewise 30.
    const acrossGap = curve([10, null, 30]);
    const smoothed = rorOf(smoothCurveForDisplay(acrossGap, 15));
    expect(smoothed[0]).toBe(10);
    expect(smoothed[1]).toBeNull();
    expect(smoothed[2]).toBe(30);
  });

  it("fits only within the contiguous run on each side of a gap", () => {
    // [10×5, null, 12×5]: the gap splits the curve. Each side's interior is bracketed
    // and flat (all equal) → stays its level; nothing mixes the 10s with the 12s.
    const ror = [10, 10, 10, 10, 10, null, 12, 12, 12, 12, 12];
    const smoothed = rorOf(smoothCurveForDisplay(curve(ror), 21)) as (number | null)[];
    expect(smoothed[2]).toBeCloseTo(10, 9); // bracketed interior of the left run
    expect(smoothed[5]).toBeNull();
    expect(smoothed[8]).toBeCloseTo(12, 9); // bracketed interior of the right run
  });

  it("leaves env / heat / fan untouched (smoothing is bean + RoR only)", () => {
    const points = curve([10, 14, 12, 16, 13, 17], 1, [70, 71, 72, 73, 74, 75]);
    const smoothed = smoothCurveForDisplay(points, 7);
    smoothed.forEach((p, i) => {
      expect(p.t).toBe(points[i].t);
      expect(p.env).toBe(points[i].env);
      expect(p.heat).toBe(points[i].heat);
      expect(p.fan).toBe(points[i].fan);
    });
  });

  it("does not mutate the input array or its points", () => {
    const points = curve([10, 12, 14, 16, 18], 1, [70, 71, 72, 73, 74]);
    const snapshot = JSON.parse(JSON.stringify(points));
    smoothCurveForDisplay(points, 7);
    expect(points).toEqual(snapshot);
  });

  it("returns the input unchanged for an empty curve or a non-positive window", () => {
    expect(smoothCurveForDisplay([], 21)).toEqual([]);
    const points = curve([10, 12, 14]);
    expect(smoothCurveForDisplay(points, 0)).toBe(points);
    expect(smoothCurveForDisplay(points, -5)).toBe(points);
  });

  it("respects the window in SECONDS, not point count (sparse cadence)", () => {
    // 30 s spacing, 21 s window → ±10.5 s reaches no neighbour, so no point can be
    // bracketed → every point stays raw. Proves the band is time-based.
    const sparse = curve([10, 14, 12, 16], 30);
    expect(rorOf(smoothCurveForDisplay(sparse))).toEqual([10, 14, 12, 16]);
  });

  it("pins the window to 21 s (mid-band, validated against a roast-3-style replay)", () => {
    expect(CURVE_SMOOTHING_WINDOW_SECONDS).toBe(21);
    expect(CURVE_SMOOTHING_WINDOW_SECONDS).toBeGreaterThanOrEqual(15);
    expect(CURVE_SMOOTHING_WINDOW_SECONDS).toBeLessThanOrEqual(25);
  });

  it("clamps an overshooting fit to the window's raw [min, max] (no spurious spike)", () => {
    // A sharp step edge: a quadratic fit at the corner OVERSHOOTS past the raw step
    // (Gibbs-like ringing) — the fitted centre would land below the local min / above
    // the local max. The clamp pins every smoothed value inside the window's raw range
    // so no spurious spike appears, while the genuine step extremes survive.
    const ror = [0, 0, 0, 0, 0, 20, 20, 20, 20, 20];
    const points = curve(ror);
    const smoothed = rorOf(smoothCurveForDisplay(points, 11)) as number[];
    for (let i = 0; i < smoothed.length; i += 1) {
      // each point's window is a sub-range of [0, 20], so every smoothed value must
      // stay within the global raw [min, max] (a tighter per-window bound is implied).
      expect(smoothed[i]).toBeGreaterThanOrEqual(0);
      expect(smoothed[i]).toBeLessThanOrEqual(20);
    }
    // and the step is still resolved (not flattened to a constant): low end near 0,
    // high end near 20.
    expect(Math.min(...smoothed)).toBeLessThan(5);
    expect(Math.max(...smoothed)).toBeGreaterThan(15);
  });
});
