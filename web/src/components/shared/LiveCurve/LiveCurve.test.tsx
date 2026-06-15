import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import type uPlot from "uplot";
import { afterEach, describe, expect, it } from "vitest";

import { LiveCurve } from "./LiveCurve";
import { FIXED_SCALE_RANGES, makeAutoRange } from "./scales";
import type { CurveMarker, CurvePoint } from "./types";

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
    // The hook carries the live uPlot x/°C scale ranges so an e2e test can assert
    // the scale COVERS the data (catching the collapsed/unranged-scale bug a blank
    // snapshot can't). Under jsdom the canvas is stubbed, so we assert the SHAPE is
    // present (the real range values are asserted in the Playwright suite).
    render(<LiveCurve points={POINTS} phase="development" />);
    const scales = window.__chart?.scales;
    expect(scales).toBeDefined();
    // Both scale entries exist with min/max keys (values are null under the stubbed
    // jsdom canvas; the real covering ranges are asserted in the Playwright suite).
    expect(Object.keys(scales?.x ?? {})).toEqual(["min", "max"]);
    expect(Object.keys(scales?.c ?? {})).toEqual(["min", "max"]);
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

  it("exposes event marker labels on the data hook (T0/FIRST CRACK, assert without pixels)", () => {
    render(<LiveCurve points={POINTS} markers={MARKERS} />);
    const labels = window.__chart?.markers.map((m) => m.label);
    expect(labels).toContain("T0");
    expect(labels).toContain("FIRST CRACK");
  });
});

// The fixed value-axis ranges (#217) are the load-bearing contract: both Y-axes are
// pinned so the curve never auto-zooms to the current sensor reading. The range
// callback is pure (no canvas), so it is asserted directly — deterministic and
// independent of the jsdom canvas stub (which leaves the live plot's scale min/max
// null, as the scale-shape test above documents).
describe("LiveCurve fixed value-axis ranges (#217)", () => {
  // A minimal uPlot stand-in carrying only `data` — all the range callback reads.
  function fakeSelf(data: (number | null)[][]): uPlot {
    return { data } as unknown as uPlot;
  }

  // The fixed ranges encode the operator-confirmed bounds; pin them so an
  // accidental edit to the constants is caught here, not only in the pixel gate.
  it("pins the operator-confirmed bounds: temp 0–210 °C, RoR −20..+30 °C/min", () => {
    expect(FIXED_SCALE_RANGES.c).toEqual([0, 210]);
    expect(FIXED_SCALE_RANGES.ror).toEqual([-20, 30]);
  });

  it("returns the FIXED temperature range regardless of the data extent", () => {
    const range = makeAutoRange(() => ({ visible: false, minC: 170, maxC: 200 }));
    // Bean/env data that would otherwise auto-fit to ~38–186 °C…
    const self = fakeSelf([[0, 30], [38, 186], [40, 190], [10, -90]]);
    // …still yields the pinned 0–210, so the axis never zooms to the live reading.
    expect(range(self, 38, 186, "c")).toEqual([0, 210]);
  });

  it("returns the FIXED RoR range regardless of the data extent (charge crash clipped)", () => {
    const range = makeAutoRange(() => ({ visible: false, minC: 170, maxC: 200 }));
    // RoR column dives to −90 on the charge crash and peaks at +25…
    const self = fakeSelf([[0, 30], [38, 186], [40, 190], [-90, 25]]);
    // …but the axis stays pinned −20..+30 (the trough dives off-screen by design).
    expect(range(self, -90, 25, "ror")).toEqual([-20, 30]);
  });

  it("keeps the 170–200 charge band inside the fixed temperature range", () => {
    // The whole point of 0–210: the band overlay is always in frame without the
    // band having to stretch the domain.
    const [lo, hi] = FIXED_SCALE_RANGES.c;
    expect(lo).toBeLessThanOrEqual(170);
    expect(hi).toBeGreaterThanOrEqual(200);
  });

  it("leaves the x (time) axis data-driven and ranged tight (no soft padding)", () => {
    const range = makeAutoRange(() => ({ visible: false, minC: 170, maxC: 200 }));
    const self = fakeSelf([[0, 30, 60, 1031]]);
    // x covers the loaded elapsed-time range exactly — it must NOT be pinned.
    expect(range(self, 0, 0, "x")).toEqual([0, 1031]);
  });

  it("falls back to uPlot's passed bounds for x on an empty mount (no finite data)", () => {
    const range = makeAutoRange(() => ({ visible: false, minC: 170, maxC: 200 }));
    const self = fakeSelf([[]]);
    expect(range(self, 0, 100, "x")).toEqual([0, 100]);
  });

  it("exposes the fixed c/ror ranges on the test-hook scale shape", () => {
    render(<LiveCurve points={POINTS} phase="development" />);
    const scales = window.__chart?.scales;
    // The hook now carries ror too (so the e2e suite can assert the fixed range
    // against the real rendered plot). Shape under jsdom; values asserted in e2e.
    expect(Object.keys(scales?.ror ?? {})).toEqual(["min", "max"]);
  });
});
