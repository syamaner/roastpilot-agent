import { describe, expect, it } from "vitest";

import type { CurvePoint } from "@/components/shared/LiveCurve/types";
import { ROR_SMOOTHING_WINDOW_SECONDS, smoothRorForDisplay } from "./rorSmoothing";

/** Build a curve of evenly-spaced points carrying the given RoR samples. */
function curve(rors: (number | null)[], stepSeconds = 1): CurvePoint[] {
  return rors.map((ror, i) => ({
    t: i * stepSeconds,
    bean: 100 + i,
    env: 120 + i,
    ror,
    heat: 80,
    fan: 40,
  }));
}

const rorOf = (points: CurvePoint[]): (number | null)[] => points.map((p) => p.ror);

describe("smoothRorForDisplay", () => {
  it("turns a staircase into a monotone-ish smooth ramp (the #205 bug)", () => {
    // A classic quantised RoR staircase: holds a level, then jumps a whole step.
    const staircase = curve([10, 10, 10, 12, 12, 12, 14, 14, 14, 16, 16, 16]);
    const smoothed = rorOf(smoothRorForDisplay(staircase, 7));

    // The flat-then-jump pattern is gone: the run of equal values becomes a set of
    // distinct, ascending values (the slope no longer "steps").
    const distinct = new Set(smoothed.map((v) => (v as number).toFixed(4)));
    expect(distinct.size).toBeGreaterThan(
      new Set(staircase.map((p) => p.ror)).size,
    );

    // Strictly non-decreasing across the interior (a monotone-rising input stays
    // monotone after a centered MA — no overshoot/ringing artefacts).
    for (let i = 1; i < smoothed.length; i += 1) {
      expect(smoothed[i] as number).toBeGreaterThanOrEqual(smoothed[i - 1] as number);
    }
  });

  it("preserves the overall range — averaging never exceeds the raw extremes", () => {
    const staircase = curve([10, 10, 12, 12, 14, 14, 16, 16]);
    const smoothed = rorOf(smoothRorForDisplay(staircase, 7)) as number[];
    expect(Math.min(...smoothed)).toBeGreaterThanOrEqual(10);
    expect(Math.max(...smoothed)).toBeLessThanOrEqual(16);
  });

  it("computes a centered window average for an interior point", () => {
    // 1 s spacing, window 5 s → ±2.5 s → indices i-2..i+2. ror = index, so for
    // index 5 the five samples are indices 3..7, carrying RoR 3,4,5,6,7.
    const points = curve([0, 1, 2, 3, 4, 5, 6, 7, 8, 9]);
    const smoothed = rorOf(smoothRorForDisplay(points, 5)) as number[];
    expect(smoothed[5]).toBeCloseTo((3 + 4 + 5 + 6 + 7) / 5, 10);
  });

  it("does not lag a peak — a centered window keeps the apex at the same x", () => {
    // Symmetric triangle peaking at index 5; a centered MA keeps the max index 5
    // (a trailing filter/EMA would shift it right).
    const tri = curve([0, 1, 2, 3, 4, 5, 4, 3, 2, 1, 0]);
    const smoothed = rorOf(smoothRorForDisplay(tri, 5)) as number[];
    let maxIdx = 0;
    for (let i = 1; i < smoothed.length; i += 1) {
      if (smoothed[i] > smoothed[maxIdx]) maxIdx = i;
    }
    expect(maxIdx).toBe(5);
  });

  it("preserves the crash/flick shape — a sharp dip stays a dip, not flattened away", () => {
    // A post-charge style crash: steady, plunge, recover. With a modest window the
    // trough must remain clearly the minimum (the drop logic / operator read needs it).
    const crash = curve([12, 12, 12, -40, -40, 12, 12, 12]);
    const smoothed = rorOf(smoothRorForDisplay(crash, 5)) as number[];
    const min = Math.min(...smoothed);
    const minIdx = smoothed.indexOf(min);
    expect(min).toBeLessThan(0); // the crash survives smoothing (not flattened away)
    // The trough sits over the raw dip (indices 3-4 ± the ±2-sample window reach),
    // never displaced to the flat steady-state shoulders.
    expect(minIdx).toBeGreaterThanOrEqual(2);
    expect(minIdx).toBeLessThanOrEqual(5);
  });

  it("smooths less at the live tail (fewer future neighbours = less lag where it matters)", () => {
    // The final point has no right-side neighbours, so its window is one-sided and
    // narrower than a deep-interior point's — the freshest reading is the least
    // altered. Compare the last point's deviation from raw vs an interior point's.
    const ramp = curve([0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20]);
    const smoothed = rorOf(smoothRorForDisplay(ramp, 7)) as number[];
    // Last point: neighbours only to the left, on a linear ramp the centered mean
    // of a one-sided trailing window pulls it DOWN from 20 but by less than a full
    // symmetric window would for an interior point of the same slope.
    const lastDev = Math.abs(smoothed[smoothed.length - 1] - 20);
    const interiorDev = Math.abs(smoothed[5] - 10);
    // Interior is centered on a symmetric ramp → ~0 deviation; the tail is biased.
    expect(interiorDev).toBeLessThan(lastDev);
  });

  it("keeps null RoR samples as gaps and never fabricates a value across them", () => {
    const withGap = curve([10, 10, null, 12, 12]);
    const smoothed = rorOf(smoothRorForDisplay(withGap, 9));
    expect(smoothed[2]).toBeNull(); // the gap stays a gap
    // Neighbours of the gap are still averaged from the real samples around them.
    expect(smoothed[0]).not.toBeNull();
    expect(smoothed[3]).not.toBeNull();
  });

  it("leaves every non-RoR channel untouched (display smoothing is RoR-only)", () => {
    const points = curve([10, 14, 12, 16]);
    const smoothed = smoothRorForDisplay(points, 7);
    smoothed.forEach((p, i) => {
      expect(p.t).toBe(points[i].t);
      expect(p.bean).toBe(points[i].bean);
      expect(p.env).toBe(points[i].env);
      expect(p.heat).toBe(points[i].heat);
      expect(p.fan).toBe(points[i].fan);
    });
  });

  it("does not mutate the input array or its points", () => {
    const points = curve([10, 12, 14, 16]);
    const snapshot = JSON.parse(JSON.stringify(points));
    smoothRorForDisplay(points, 7);
    expect(points).toEqual(snapshot);
  });

  it("returns the input unchanged for an empty curve or a non-positive window", () => {
    expect(smoothRorForDisplay([], 15)).toEqual([]);
    const points = curve([10, 12, 14]);
    expect(smoothRorForDisplay(points, 0)).toBe(points);
    expect(smoothRorForDisplay(points, -5)).toBe(points);
  });

  it("respects the window in SECONDS, not point count (sparse cadence)", () => {
    // 30 s spacing, default 15 s window → ±7.5 s reaches no neighbour, so each
    // point is its own average (unchanged). Proves the band is time-based.
    const sparse = curve([10, 14, 12, 16], 30);
    const smoothed = rorOf(smoothRorForDisplay(sparse));
    expect(smoothed).toEqual([10, 14, 12, 16]);
  });

  it("exports a sensible default window constant", () => {
    expect(ROR_SMOOTHING_WINDOW_SECONDS).toBe(15);
  });
});
