import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import type uPlot from "uplot";
import { afterEach, describe, expect, it } from "vitest";

import { LiveCurve } from "./LiveCurve";
import {
  type AutoRangeState,
  computeTempRange,
  FIXED_SCALE_RANGES,
  makeAutoRange,
  TEMP_RANGE,
} from "./scales";
import type { ChartColumns, CurveMarker, CurvePoint } from "./types";

// Canvas/matchMedia/ResizeObserver are stubbed in vitest.setup.ts so uPlot
// mounts under jsdom. We assert the DATA test hook (window.__chart) and the
// legend interaction — never the canvas pixels (D24).
afterEach(() => {
  cleanup();
  delete window.__chart;
});

const POINTS: CurvePoint[] = [
  { t: 0, bean: 90, env: 110, ror: 18, heat: 80, fan: 40 },
  { t: 30, bean: 120, env: 140, ror: 16, heat: 75, fan: 45 },
  { t: 60, bean: 150, env: 170, ror: 14, heat: 65, fan: 55 },
];

const MARKERS: CurveMarker[] = [
  { kind: "t0", t: 0, label: "T0" },
  { kind: "first_crack", t: 60, label: "FIRST CRACK" },
];

describe("LiveCurve", () => {
  it("exposes the series data on the window.__chart test hook (D24)", () => {
    render(<LiveCurve points={POINTS} markers={MARKERS} phase="preheating" />);
    const hook = window.__chart;
    expect(hook).toBeDefined();
    expect(hook?.columns[0]).toEqual([0, 30, 60]); // x
    expect(hook?.columns[1]).toEqual([90, 120, 150]); // bean
    expect(hook?.markers).toHaveLength(2);
  });

  it("exposes the rendered scale ranges on the test hook (#131 scale-covers-data guard)", () => {
    // The hook carries the live scale ranges so an e2e test can assert the scale COVERS
    // the data (catching the collapsed/unranged-scale bug a blank snapshot can't).
    render(<LiveCurve points={POINTS} phase="development" />);
    const scales = window.__chart?.scales;
    expect(scales).toBeDefined();
    // ALL FOUR scale entries exist with min/max keys (#133/#307). Values are null under
    // jsdom: the canvas is stubbed, so uPlot never invokes the `range` callback (no
    // layout), leaving both the stubbed `plot.scales` AND the auto-range
    // `tempRange` source the hook now reads (#341) unset. The real covering values are
    // asserted in the Playwright suite (dashboard-developed: c.max >= beanMax).
    expect(Object.keys(scales?.x ?? {})).toEqual(["min", "max"]);
    expect(Object.keys(scales?.c ?? {})).toEqual(["min", "max"]);
    expect(Object.keys(scales?.ror ?? {})).toEqual(["min", "max"]);
    expect(Object.keys(scales?.pct ?? {})).toEqual(["min", "max"]);
  });

  it("re-publishes the hook when the data columns change (live append, not a stale read)", () => {
    // The hook must track the LIVE plot, not a one-time mount snapshot: the dashboard
    // appends a telemetry frame every tick, and a test reading window.__chart after a
    // rerender must see the new data (#133 — assert hook BEHAVIOUR, not just shape).
    const { rerender } = render(<LiveCurve points={POINTS.slice(0, 1)} />);
    expect(window.__chart?.columns[0]).toEqual([0]);
    expect(window.__chart?.columns[1]).toEqual([90]);

    rerender(<LiveCurve points={POINTS} />);
    expect(window.__chart?.columns[0]).toEqual([0, 30, 60]);
    expect(window.__chart?.columns[1]).toEqual([90, 120, 150]);
  });

  it("re-publishes the hook when markers change (e.g. T0 → first-crack arrives)", () => {
    const { rerender } = render(<LiveCurve points={POINTS} markers={[MARKERS[0]]} />);
    expect(window.__chart?.markers.map((m) => m.label)).toEqual(["T0"]);

    rerender(<LiveCurve points={POINTS} markers={MARKERS} />);
    expect(window.__chart?.markers.map((m) => m.label)).toEqual(["T0", "FIRST CRACK"]);
  });

  it("destroys the uPlot instance on unmount (no leaked plot across remounts)", () => {
    // The build effect's cleanup must call plot.destroy() so a remount (route change /
    // strict-mode double-invoke) doesn't leak canvases or stack draw hooks. Assert via
    // the public DOM contract: uPlot owns a `.uplot` element inside the host, gone after
    // unmount (#133 cleanup item — verify teardown, don't just trust afterEach).
    const { unmount } = render(<LiveCurve points={POINTS} />);
    const host = screen.getByTestId("live-curve");
    expect(host.querySelector(".uplot")).not.toBeNull();

    unmount();
    expect(host.querySelector(".uplot")).toBeNull();
  });

  it("shows the charge band in preheating only", () => {
    const { rerender } = render(<LiveCurve points={POINTS} phase="preheating" />);
    expect(window.__chart?.chargeBandVisible).toBe(true);
    expect(screen.getByTestId("live-curve").querySelector("[data-charge-band]")).toHaveAttribute(
      "data-charge-band",
      "true",
    );

    rerender(<LiveCurve points={POINTS} phase="development" />);
    expect(window.__chart?.chargeBandVisible).toBe(false);
  });

  it("toggles a series off and on via the legend (click-to-toggle)", () => {
    render(<LiveCurve points={POINTS} />);
    const heat = screen.getByTestId("legend-heat");
    expect(heat).toHaveAttribute("data-visible", "true");
    expect(window.__chart?.visible.heat).toBe(true);

    fireEvent.click(heat);
    expect(heat).toHaveAttribute("data-visible", "false");
    expect(window.__chart?.visible.heat).toBe(false);

    fireEvent.click(heat);
    expect(window.__chart?.visible.heat).toBe(true);
  });

  it("renders the legend value readout for the latest point", () => {
    render(<LiveCurve points={POINTS} />);
    // Latest bean = 150 → "150.0 °C"
    expect(screen.getByTestId("legend-bean")).toHaveTextContent("150.0 °C");
  });

  it("renders the legend time readout as M:SS for the latest point (#153)", () => {
    // Latest point t=60 → "1:00" (not raw seconds).
    render(<LiveCurve points={[...POINTS, { t: 66, bean: 160, env: 180, ror: 12, heat: 60, fan: 60 }]} />);
    expect(screen.getByTestId("legend-time")).toHaveTextContent("1:06");
  });

  it("shows an em-dash time readout when there are no points", () => {
    render(<LiveCurve points={[]} />);
    expect(screen.getByTestId("legend-time")).toHaveTextContent("—");
  });

  it("renders the legend time as CHARGE-referenced roast time when originSeconds is set (#326)", () => {
    // The core acceptance contract: with the charge origin threaded through
    // LiveCurve → Legend, the cursor/latest readout reads SIGNED roast time
    // (point − origin), NOT serve-elapsed. Latest point serve t=600 with origin 540
    // → "1:00" (60 s into the roast), never "10:00" (the un-transformed serve value).
    render(
      <LiveCurve
        points={[
          { t: 480, bean: 90, env: 180, ror: 30, heat: 100, fan: 30 },
          { t: 540, bean: 160, env: 200, ror: 22, heat: 100, fan: 30 },
          { t: 600, bean: 175, env: 205, ror: 18, heat: 80, fan: 40 },
        ]}
        originSeconds={540}
      />,
    );
    expect(screen.getByTestId("legend-time")).toHaveTextContent("1:00");
    expect(screen.getByTestId("legend-time")).not.toHaveTextContent("10:00");
  });

  it("renders a NEGATIVE legend time for a pre-charge latest point when origin is set (#326)", () => {
    // A latest point before the charge origin reads negative roast time (preheat).
    // Several pre-charge points so uPlot has a non-degenerate x-range; the latest
    // (t=340) drives the readout.
    render(
      <LiveCurve
        points={[
          { t: 300, bean: 70, env: 172, ror: 30, heat: 100, fan: 30 },
          { t: 320, bean: 75, env: 174, ror: 30, heat: 100, fan: 30 },
          { t: 340, bean: 80, env: 175, ror: 30, heat: 100, fan: 30 },
        ]}
        originSeconds={540}
      />,
    );
    // 340 − 540 = −200 s → "-3:20".
    expect(screen.getByTestId("legend-time")).toHaveTextContent("-3:20");
  });

  // --- #326 regression: a DEGENERATE (single-point / zero-width) x-range must not
  // throw. uPlot's split calc divides by the x span; a single plotted point (or
  // several all at the same elapsed second) gives a zero-width range → a non-finite
  // increment → `new Array(NaN)` → "RangeError: Invalid array length", which threw
  // out of the async commit and broke the dashboard React tree (the SSE consumer
  // stopped updating, stalling the phase readout). This surfaced once #326 made
  // pre-charge frames plot: the preheat curve now STARTS as one point. The
  // scales.makeAutoRange guard widens a zero-width x-range so the chart is robust on
  // short/sparse roasts. These render without an unhandled error (vitest fails the
  // file on an uncaught RangeError from the deferred uPlot commit).
  it.each([
    ["a single point at a non-zero elapsed second, null origin", [{ t: 540, bean: 90, env: 180, ror: 18, heat: 80, fan: 40 }], null as number | null],
    ["a single point with the charge origin set", [{ t: 540, bean: 90, env: 180, ror: 18, heat: 80, fan: 40 }], 540],
    ["several points all at the same elapsed second", [
      { t: 540, bean: 90, env: 180, ror: 18, heat: 80, fan: 40 },
      { t: 540, bean: 91, env: 181, ror: 18, heat: 80, fan: 40 },
    ], 540],
  ])("renders a degenerate x-range without throwing: %s (#326)", (_label, points, originSeconds) => {
    render(<LiveCurve points={points as CurvePoint[]} originSeconds={originSeconds} />);
    // The chart mounted; the data hook is populated (no throw tore the tree down).
    expect(screen.getByTestId("live-curve")).toBeInTheDocument();
    expect(window.__chart).toBeDefined();
  });

  it("reflects the controlled highlightTime on the data hook", () => {
    const { rerender } = render(<LiveCurve points={POINTS} highlightTime={null} />);
    expect(window.__chart?.highlightTime).toBeNull();

    rerender(<LiveCurve points={POINTS} highlightTime={30} />);
    expect(window.__chart?.highlightTime).toBe(30);
  });

  it("honors initialHidden (series start hidden before any interaction)", () => {
    // S5 (detail) may mount the curve with heat/fan hidden by default.
    render(<LiveCurve points={POINTS} initialHidden={["heat", "fan"]} />);
    expect(window.__chart?.visible.heat).toBe(false);
    expect(window.__chart?.visible.fan).toBe(false);
    // The other three remain visible.
    expect(window.__chart?.visible.bean).toBe(true);
    expect(window.__chart?.visible.env).toBe(true);
    expect(window.__chart?.visible.ror).toBe(true);
    // The legend reflects the hidden state too.
    expect(screen.getByTestId("legend-heat")).toHaveAttribute("data-visible", "false");
  });

  it("renders heat + fan as independent control series in the legend with their tokens (#307)", () => {
    // The control lines (heat/fan) live on the dedicated 0–100 % axis, behind the
    // measurements. Assert they are present as their OWN toggleable series with the
    // warm/cool tokens, and read as percentages — not as a temperature on the °C axis.
    render(<LiveCurve points={POINTS} />);
    const heat = screen.getByTestId("legend-heat");
    const fan = screen.getByTestId("legend-fan");
    expect(heat).toHaveTextContent("65 %"); // latest heat, % (not 65 °C)
    expect(fan).toHaveTextContent("55 %"); // latest fan, %
    // The swatch carries each control's token color (warm = heat, cool = fan).
    expect(heat.querySelector("span[aria-hidden]")).toHaveStyle({ background: "#fbbf24" });
    expect(fan.querySelector("span[aria-hidden]")).toHaveStyle({ background: "#22d3ee" });
  });

  it("toggles heat and fan INDEPENDENTLY on the shared control axis (#307)", () => {
    // The two control lines share the pct axis but are separate series: hiding one must
    // not affect the other, nor the measurement curves.
    render(<LiveCurve points={POINTS} />);
    fireEvent.click(screen.getByTestId("legend-heat"));
    expect(window.__chart?.visible.heat).toBe(false);
    expect(window.__chart?.visible.fan).toBe(true); // fan untouched
    expect(window.__chart?.visible.bean).toBe(true); // measurements untouched
    expect(window.__chart?.visible.ror).toBe(true);
  });

  it("exposes event marker labels on the data hook (T0/FIRST CRACK, assert without pixels)", () => {
    render(<LiveCurve points={POINTS} markers={MARKERS} />);
    const labels = window.__chart?.markers.map((m) => m.label);
    expect(labels).toContain("T0");
    expect(labels).toContain("FIRST CRACK");
  });

  it("surfaces the full server-sourced marker set incl. dry-end + cooling on the data hook (#309/#351)", () => {
    // All five markers (T0 / dry-end / FC / drop / cooling) flow through the chart's
    // data hook (D24 asserts data, not the per-kind canvas colour/subordinate style).
    // The clustered drop+cooling pair at the roast end both appear — neither is dropped.
    const fullMarkers: CurveMarker[] = [
      { kind: "t0", t: 0, label: "T0" },
      { kind: "dry_end", t: 30, label: "DRY END" },
      { kind: "first_crack", t: 60, label: "FIRST CRACK" },
      { kind: "drop", t: 60, label: "DROP" },
      { kind: "cooling", t: 60, label: "COOLING" },
    ];
    render(<LiveCurve points={POINTS} markers={fullMarkers} />);
    expect(window.__chart?.markers.map((m) => m.kind)).toEqual([
      "t0",
      "dry_end",
      "first_crack",
      "drop",
      "cooling",
    ]);
    expect(window.__chart?.markers.map((m) => m.label)).toContain("DRY END");
  });
});

// Axis-scaling policy (#307): the temperature axis is controlled-dynamic auto-range
// with hysteresis (covers bean+env, no clip, no collapse, no jitter); the RoR axis
// stays FIXED (comparable across roasts). The range/compute functions are pure (no
// canvas), so they are asserted directly — deterministic and independent of the
// jsdom canvas stub (which leaves the live plot's scale min/max null).
describe("LiveCurve axis scaling (#307)", () => {
  // The range callback no longer reads `self.data` (#341): it reads the LOGICAL
  // columns via the provider, so the permuted plot data can't mis-range the axes.
  // `self` is therefore an inert stand-in; the data lives in the columns provider.
  const inertSelf = {} as unknown as uPlot;
  // A fresh hysteresis state per range callback under test.
  function freshState(): AutoRangeState {
    return { tempRange: null };
  }
  // Build a range callback over a fixed set of LOGICAL columns ([x, bean, env, ror,
  // heat, fan]). This is the canonical pre-permutation layout the auto-range indexes.
  function mkRange(columns: (number | null)[][]) {
    return makeAutoRange(
      () => ({ visible: false, minC: 170, maxC: 200 }),
      freshState(),
      () => columns as unknown as ChartColumns,
    );
  }

  // --- RoR stays FIXED (comparable across roasts; charge crash clipped). ---

  it("pins the operator-confirmed RoR band −20..+30 °C/min (no auto-range)", () => {
    expect(FIXED_SCALE_RANGES.ror).toEqual([-20, 30]);
    // The temperature axis is no longer fixed (#307 replaced the #217 0–210 pin).
    expect(FIXED_SCALE_RANGES.c).toBeUndefined();
  });

  it("returns the FIXED RoR range regardless of the data extent (charge crash clipped)", () => {
    // RoR column dives to −90 on the charge crash and peaks at +25…
    const range = mkRange([[0, 30], [38, 186], [40, 190], [-90, 25]]);
    // …but the axis stays pinned −20..+30 (the trough dives off-screen by design).
    expect(range(inertSelf, -90, 25, "ror")).toEqual([-20, 30]);
  });

  // --- Temperature axis: controlled-dynamic auto-range with hysteresis (#307). ---

  it("covers the bean+env data with padding (never clips a hot Env)", () => {
    // bean tops out ~178, env runs hotter to ~205 — the #217 fixed 210 nearly clipped
    // env; the auto-range must keep it comfortably inside the frame.
    const range = mkRange([[0, 60], [40, 178], [60, 205], [-10, 20]]);
    const [lo, hi] = range(inertSelf, 0, 0, "c") as [number, number];
    expect(lo).toBeLessThanOrEqual(40 - 0); // covers the min with floor/padding
    expect(hi).toBeGreaterThanOrEqual(205 + TEMP_RANGE.PAD); // covers env + its padding
  });

  it("ranges the TEMP axis over bean/env, NOT heat/fan, regardless of draw order (#341)", () => {
    // The bug Augment caught: the temp range scanned `self.data` at logical indices
    // [1,2], but LiveCurve permutes the plot columns so heat/fan draw behind — so
    // [1,2] became heat/fan (0–100 %) and the temp axis ranged over the control lines.
    // The fix reads the LOGICAL columns: even though heat/fan (cols 4,5) span 0–100,
    // the temp range must cover bean (col 1, ~40–178) and env (col 2, ~60–205), and
    // its lower bound must NOT be dragged down to 0 by the control percentages.
    const range = mkRange([
      [0, 60, 120], // x
      [40, 90, 178], // bean
      [60, 140, 205], // env
      [18, 16, 12], // ror
      [0, 0, 0], // heat — 0 % would drag a buggy temp-min to 0
      [100, 100, 100], // fan — 100 % is well below env's 205
    ]);
    const [lo, hi] = range(inertSelf, 0, 0, "c") as [number, number];
    expect(hi).toBeGreaterThanOrEqual(205 + TEMP_RANGE.PAD); // covers env, not capped at 100
    expect(lo).toBeGreaterThan(0); // NOT dragged to 0 by the 0 % heat line
    expect(lo).toBeLessThanOrEqual(40); // still covers the bean min
  });

  it("does NOT collapse to a zero-width range on a single point (the #128 guard)", () => {
    const range = mkRange([[540], [90], [180], [18]]);
    const [lo, hi] = range(inertSelf, 0, 0, "c") as [number, number];
    expect(hi - lo).toBeGreaterThanOrEqual(TEMP_RANGE.MIN_SPAN); // a usable height, never 0
    expect(lo).toBeLessThanOrEqual(90);
    expect(hi).toBeGreaterThanOrEqual(180);
  });

  it("does NOT collapse on an empty mount (holds a sane default band)", () => {
    const range = mkRange([[]]);
    const [lo, hi] = range(inertSelf, 0, 0, "c") as [number, number];
    expect(hi - lo).toBeGreaterThanOrEqual(TEMP_RANGE.MIN_SPAN);
  });

  it("keeps the charge band on-screen in preheating even when the curve is cool (#307)", () => {
    // While preheating, the 170–200 °C charge band is the readiness target and MUST
    // stay in frame (E10-spa.md). Preheat data is cool (~30–55 °C), so a pure data-fit
    // would push the band off the top; the auto-range must fold the visible band into
    // its extent. Build a range with the band VISIBLE and only cool preheat data.
    const range = makeAutoRange(
      () => ({ visible: true, minC: 170, maxC: 200 }),
      freshState(),
      () => [[0, 60], [30, 55], [40, 52], [18, 17]] as unknown as ChartColumns,
    );
    const [lo, hi] = range(inertSelf, 0, 0, "c") as [number, number];
    expect(lo).toBeLessThanOrEqual(170); // band bottom in frame
    expect(hi).toBeGreaterThanOrEqual(200); // band top in frame
    expect(lo).toBeLessThanOrEqual(30); // AND the cool curve is still covered
  });

  it("covers a FULL developed-curve mount immediately, even after a narrow earlier range (#341)", () => {
    // The second #341 bug: a full-data (re)mount — the detail page, or the dashboard
    // re-hydrating the whole curve from REST in one setData — must jump straight to
    // covering the whole span, NEVER stay pinned at a narrow earlier range. The
    // regen's dashboard-developed replay left c.max ≈ 60 because coverage deferred to
    // incremental expansion. Drive the SAME state ref through: empty mount → a narrow
    // preheat frame (~30–52 °C, establishes a low range) → the full developed curve
    // (bean→178, env→205) in one call. The final range MUST cover 205.
    const state = freshState();
    let current = [[]] as unknown as ChartColumns; // empty mount
    const range = makeAutoRange(
      () => ({ visible: false, minC: 170, maxC: 200 }),
      state,
      () => current, // provider returns whatever `current` points at this call
    );
    range(inertSelf, 0, 0, "c");
    current = [[0, 15], [30, 40], [50, 52], [18, 17]] as unknown as ChartColumns; // narrow preheat
    range(inertSelf, 0, 0, "c");
    current = [[0, 1031], [30, 178], [40, 205], [18, 12]] as unknown as ChartColumns; // full dev curve
    const [, hi] = range(inertSelf, 0, 0, "c") as [number, number];
    expect(hi).toBeGreaterThanOrEqual(205); // covers the developed env, not stuck low
  });

  // --- computeTempRange hysteresis (the jitter guard), asserted directly. ---

  it("hysteresis: a small data wobble INSIDE the frame does NOT re-range (no jitter)", () => {
    // Establish a range over 40–180 °C.
    const first = computeTempRange(40, 180, null);
    // The data wobbles by a couple of degrees, still well inside the padded frame.
    const second = computeTempRange(42, 178, first);
    expect(second).toEqual(first); // axis is untouched — this is what kills the per-frame jitter
  });

  it("hysteresis: data rising ABOVE the frame expands the upper bound (no clip)", () => {
    const first = computeTempRange(40, 180, null);
    const [, hi0] = first as [number, number];
    // env climbs past the top of the frame → the upper bound must grow to keep it in view.
    const grown = computeTempRange(40, hi0 + 30, first);
    expect((grown as [number, number])[1]).toBeGreaterThan(hi0);
  });

  it("hysteresis: a transient peak that recedes eventually contracts (doesn't stay zoomed out)", () => {
    const base = computeTempRange(40, 150, null);
    const peaked = computeTempRange(40, 240, base); // a spike pushes the top way up
    const recovered = computeTempRange(40, 150, peaked); // data falls back well inside
    expect((recovered as [number, number])[1]).toBeLessThan((peaked as [number, number])[1]);
  });

  it("quantises bounds to a stable grid (equivalent frames give an identical range)", () => {
    // Two data extents that, after padding, fall inside the SAME quantum bucket land on
    // an identical quantised range — so the axis (and the pixel baseline) is stable
    // frame to frame. lo: 42−8=34, 43−8=35 → both floor to 30; hi: 178+8=186, 179+8=187
    // → both ceil to 190. Sub-quantum data wobble ⇒ identical range.
    const a = computeTempRange(42, 178, null);
    const b = computeTempRange(43, 179, null);
    expect(a).toEqual(b);
    const [lo, hi] = a as [number, number];
    expect(lo % TEMP_RANGE.QUANTUM).toBe(0);
    expect(hi % TEMP_RANGE.QUANTUM).toBe(0);
  });

  // --- x (time) axis: data-driven, ranged tight, with the degenerate-x guard. ---

  it("leaves the x (time) axis data-driven and ranged tight (no soft padding)", () => {
    const range = mkRange([[0, 30, 60, 1031]]);
    // x covers the loaded elapsed-time range exactly — it must NOT be pinned.
    expect(range(inertSelf, 0, 0, "x")).toEqual([0, 1031]);
  });

  it("widens a zero-width x-range so uPlot's split calc never divides by zero (#326/#334)", () => {
    const range = mkRange([[540, 540]]); // several points at the same elapsed second
    expect(range(inertSelf, 0, 0, "x")).toEqual([539, 541]);
  });

  it("falls back to uPlot's passed bounds for x on an empty mount (no finite data)", () => {
    const range = mkRange([[]]);
    expect(range(inertSelf, 0, 100, "x")).toEqual([0, 100]);
  });

  it("exposes the c/ror/pct ranges on the test-hook scale shape", () => {
    render(<LiveCurve points={POINTS} phase="development" />);
    const scales = window.__chart?.scales;
    // The hook carries ror + pct too (so the e2e suite can assert the fixed RoR band
    // and the dedicated 0–100 % control axis). Shape under jsdom; values in e2e.
    expect(Object.keys(scales?.ror ?? {})).toEqual(["min", "max"]);
    expect(Object.keys(scales?.pct ?? {})).toEqual(["min", "max"]);
  });
});
