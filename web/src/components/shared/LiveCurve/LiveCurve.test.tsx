import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { LiveCurve } from "./LiveCurve";
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
