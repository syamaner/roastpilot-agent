import { describe, expect, it } from "vitest";

import type { RoastSummary } from "@/lib/types";

import {
  ANY,
  beanLabel,
  EMPTY_FILTERS,
  filterRuns,
  formatAmbientCell,
  formatDevPercent,
  formatFcTime,
  formatStartedAt,
  formatWeightLoss,
} from "./format";

function run(overrides: Partial<RoastSummary> = {}): RoastSummary {
  return {
    id: "r1",
    started_at_utc: "2026-06-07T14:00:00Z",
    completed_at_utc: "2026-06-07T14:12:00Z",
    first_crack_at_utc: "2026-06-07T14:09:00Z",
    agent_phase: "complete",
    outcome: "completed",
    bean_origin: "Ethiopian Yirgacheffe",
    bean_varietal: "Medium",
    rating: 4,
    development_percent: 19.4,
    advisor_consults: 0,
    advisor_clamped: 0,
    advisor_rejected: 0,
    advisor_failed: 0,
    ...overrides,
  };
}

describe("beanLabel", () => {
  it("joins origin and varietal", () => {
    expect(beanLabel(run())).toBe("Ethiopian Yirgacheffe Medium");
  });

  it("omits a null varietal", () => {
    expect(beanLabel(run({ bean_varietal: null }))).toBe("Ethiopian Yirgacheffe");
  });

  it("includes the country when present so search matches it (#164)", () => {
    expect(beanLabel(run({ bean_varietal: null, country: "Ethiopia" }))).toBe(
      "Ethiopian Yirgacheffe Ethiopia",
    );
  });

  it("omits unset #164 identity fields (back-compat)", () => {
    expect(beanLabel(run({ bean_varietal: null, country: null }))).toBe("Ethiopian Yirgacheffe");
  });
});

describe("formatStartedAt", () => {
  it("formats an ISO UTC timestamp as YYYY-MM-DD HH:MM in UTC", () => {
    expect(formatStartedAt("2026-06-07T14:05:00Z")).toBe("2026-06-07 14:05");
  });

  it("returns the raw string when unparseable", () => {
    expect(formatStartedAt("not-a-date")).toBe("not-a-date");
  });
});

describe("formatDevPercent", () => {
  it("rounds to a whole percent", () => {
    expect(formatDevPercent(19.4)).toBe("19%");
    expect(formatDevPercent(19.6)).toBe("20%");
  });

  it("renders an em dash for null", () => {
    expect(formatDevPercent(null)).toBe("—");
  });
});

describe("formatWeightLoss", () => {
  it("formats to one decimal", () => {
    expect(formatWeightLoss(11.6)).toBe("11.6%");
    expect(formatWeightLoss(15)).toBe("15.0%");
  });

  it("renders an em dash for null/undefined (un-weighed)", () => {
    expect(formatWeightLoss(null)).toBe("—");
    expect(formatWeightLoss(undefined)).toBe("—");
  });
});

describe("formatAmbientCell (#464 — charge-time ambient, history column)", () => {
  it("joins temp and humidity with units when both are present", () => {
    expect(formatAmbientCell(22.4, 41)).toBe("22.4°C · 41%");
  });

  it("renders temp-only when humidity is null (a partial-null real state, #463)", () => {
    expect(formatAmbientCell(22.4, null)).toBe("22.4°C");
  });

  it("renders humidity-only when temp is null", () => {
    expect(formatAmbientCell(null, 41)).toBe("41%");
  });

  it("renders an em dash when neither field was captured", () => {
    expect(formatAmbientCell(null, null)).toBe("—");
    expect(formatAmbientCell(undefined, undefined)).toBe("—");
  });
});

describe("formatFcTime", () => {
  it("formats an ISO UTC timestamp as HH:MM in UTC", () => {
    expect(formatFcTime("2026-06-07T14:09:30Z")).toBe("14:09");
  });

  it("renders an em dash when no first crack was recorded (null)", () => {
    expect(formatFcTime(null)).toBe("—");
  });

  it("returns the raw string when unparseable", () => {
    expect(formatFcTime("not-a-date")).toBe("not-a-date");
  });
});

describe("filterRuns", () => {
  const runs: RoastSummary[] = [
    run({ id: "a", bean_origin: "Ethiopian", bean_varietal: "Light", outcome: "completed", rating: 5 }),
    run({ id: "b", bean_origin: "Colombian", bean_varietal: "Dark", outcome: "aborted", rating: 2 }),
    run({ id: "c", bean_origin: "Kenyan", bean_varietal: null, outcome: "faulted", rating: null }),
  ];

  it("returns all runs with empty filters", () => {
    expect(filterRuns(runs, EMPTY_FILTERS).map((r) => r.id)).toEqual(["a", "b", "c"]);
  });

  it("matches the search against origin + varietal, case-insensitively", () => {
    expect(filterRuns(runs, { ...EMPTY_FILTERS, search: "color" }).map((r) => r.id)).toEqual([]);
    expect(filterRuns(runs, { ...EMPTY_FILTERS, search: "colomb" }).map((r) => r.id)).toEqual(["b"]);
    expect(filterRuns(runs, { ...EMPTY_FILTERS, search: "DARK" }).map((r) => r.id)).toEqual(["b"]);
  });

  it("filters by outcome", () => {
    expect(filterRuns(runs, { ...EMPTY_FILTERS, outcome: "faulted" }).map((r) => r.id)).toEqual(["c"]);
  });

  it("filters by minimum rating, excluding unrated runs", () => {
    expect(filterRuns(runs, { ...EMPTY_FILTERS, minRating: "3" }).map((r) => r.id)).toEqual(["a"]);
    // The unrated run (rating: null) is treated as below any minimum.
    expect(filterRuns(runs, { ...EMPTY_FILTERS, minRating: "1" }).map((r) => r.id)).toEqual(["a", "b"]);
  });

  it("combines filters (AND)", () => {
    const out = filterRuns(runs, { search: "ethi", outcome: "completed", minRating: "5" });
    expect(out.map((r) => r.id)).toEqual(["a"]);
  });

  it("treats ANY as no constraint", () => {
    expect(filterRuns(runs, { search: "", outcome: ANY, minRating: ANY })).toHaveLength(3);
  });
});
