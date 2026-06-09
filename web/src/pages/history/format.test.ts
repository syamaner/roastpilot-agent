import { describe, expect, it } from "vitest";

import type { RoastSummary } from "@/lib/types";

import {
  ANY,
  beanLabel,
  EMPTY_FILTERS,
  filterRuns,
  formatDevPercent,
  formatStartedAt,
} from "./format";

function run(overrides: Partial<RoastSummary> = {}): RoastSummary {
  return {
    id: "r1",
    started_at_utc: "2026-06-07T14:00:00Z",
    completed_at_utc: "2026-06-07T14:12:00Z",
    agent_phase: "complete",
    outcome: "completed",
    bean_origin: "Ethiopian Yirgacheffe",
    bean_varietal: "Medium",
    rating: 4,
    development_percent: 19.4,
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
