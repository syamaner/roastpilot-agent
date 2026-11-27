import { describe, expect, it } from "vitest";

import type { RoastTimeline, TimelineEvent } from "@/lib/types";
import { firstCrackFromTimeline, safetyNoticesFromTimeline } from "./events";

function timelineWith(event: TimelineEvent): RoastTimeline {
  return {
    run_id: "run-1",
    events: [event],
    safety_evaluations: [],
    advisor_decisions: [],
    commands: [],
  };
}

describe("firstCrackFromTimeline (#592)", () => {
  it("recovers the original server event payload for reload", () => {
    const result = firstCrackFromTimeline(
      timelineWith({
        kind: "first_crack",
        source: "mcp",
        monotonic_seconds: 1034,
        recorded_at_utc: "2026-07-26T18:02:45Z",
        payload: { source: "mcp", bean_temp_c: 178 },
      }),
    );
    expect(result).toEqual({ source: "mcp", bean_temp_c: 178 });
  });

  it("uses the typed event source when an older payload omits source", () => {
    const result = firstCrackFromTimeline(
      timelineWith({
        kind: "first_crack",
        source: "operator",
        monotonic_seconds: 1034,
        recorded_at_utc: "2026-07-26T18:02:45Z",
        payload: { bean_temp_c: 196 },
      }),
    );
    expect(result).toEqual({ source: "operator", bean_temp_c: 196 });
  });

  it("fails soft on absent or non-finite payload temperature", () => {
    expect(firstCrackFromTimeline(undefined)).toBeNull();
    expect(
      firstCrackFromTimeline(
        timelineWith({
          kind: "first_crack",
          source: "mcp",
          monotonic_seconds: null,
          recorded_at_utc: "2026-07-26T18:02:45Z",
          payload: { bean_temp_c: Number.NaN },
        }),
      ),
    ).toEqual({ source: "mcp" });
  });
});

describe("safetyNoticesFromTimeline (#1131)", () => {
  const notice = (payload: Record<string, unknown> | null, kind = "safety_alert"): TimelineEvent =>
    ({
      kind,
      source: "controller",
      monotonic_seconds: 1,
      recorded_at_utc: "2026-11-28T09:00:00Z",
      payload,
    }) as TimelineEvent;

  it("returns verdict-less alert names for this run, oldest first", () => {
    const timeline: RoastTimeline = {
      ...timelineWith(notice({ alert: "finish_disabled_target_at_guard" })),
      events: [
        notice({ alert: "finish_disabled_target_at_guard" }),
        notice({ rule: "r", verdict: "fault", reason: "x", alert: "not_a_notice" }),
        notice(null),
        notice({ alert: 3 }),
        notice({ alert: "first_crack_lookalike" }, "first_crack"),
        notice({ alert: "low_development" }),
      ],
    };
    expect(safetyNoticesFromTimeline(timeline, "run-1")).toEqual([
      "finish_disabled_target_at_guard",
      "low_development",
    ]);
  });

  it("yields nothing for another run, no run or no timeline", () => {
    const timeline = timelineWith(notice({ alert: "finish_overrun" }));
    expect(safetyNoticesFromTimeline(timeline, "run-2")).toEqual([]);
    expect(safetyNoticesFromTimeline(timeline, null)).toEqual([]);
    expect(safetyNoticesFromTimeline(undefined, "run-1")).toEqual([]);
  });
});
