import { describe, expect, it } from "vitest";

import { formatSeriesValue, toColumns } from "./chartData";
import type { CurvePoint } from "./types";

const POINTS: CurvePoint[] = [
  { t: 0, bean: 90, env: 110, ror: 18, heat: 80, fan: 40 },
  { t: 30, bean: 95.5, env: 114, ror: 17, heat: 80, fan: 40 },
  { t: 60, bean: null, env: 118, ror: null, heat: 65, fan: 55 },
];

describe("toColumns", () => {
  it("projects points into uPlot columnar form [x, bean, env, ror, heat, fan]", () => {
    const cols = toColumns(POINTS);
    expect(cols[0]).toEqual([0, 30, 60]); // x
    expect(cols[1]).toEqual([90, 95.5, null]); // bean
    expect(cols[2]).toEqual([110, 114, 118]); // env
    expect(cols[3]).toEqual([18, 17, null]); // ror
    expect(cols[4]).toEqual([80, 80, 65]); // heat
    expect(cols[5]).toEqual([40, 40, 55]); // fan
  });

  it("returns six empty columns for no points", () => {
    expect(toColumns([])).toEqual([[], [], [], [], [], []]);
  });
});

describe("formatSeriesValue", () => {
  it("formats temps in °C, RoR in °C/min, control lines in %", () => {
    expect(formatSeriesValue("bean", 198.42)).toBe("198.4 °C");
    expect(formatSeriesValue("env", 211)).toBe("211.0 °C");
    expect(formatSeriesValue("ror", 8.25)).toBe("8.3 °C/min");
    expect(formatSeriesValue("heat", 64.6)).toBe("65 %");
    expect(formatSeriesValue("fan", 70)).toBe("70 %");
  });

  it("renders an em dash for null/NaN", () => {
    expect(formatSeriesValue("bean", null)).toBe("—");
    expect(formatSeriesValue("ror", NaN)).toBe("—");
  });
});
