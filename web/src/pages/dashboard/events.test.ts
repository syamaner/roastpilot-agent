import { describe, expect, it } from "vitest";

import type { RoastTimeline, TimelineEvent } from "@/lib/types";
import { firstCrackFromTimeline } from "./events";

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
