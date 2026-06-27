import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { RoastTimeline, TimelineEvent } from "@/lib/types";

import { EventTimeline } from "./EventTimeline";

afterEach(cleanup);

/** Build a timeline from milestone events; the other channels are unused here. */
function timelineOf(events: TimelineEvent[]): RoastTimeline {
  return { run_id: "r", events, safety_evaluations: [], advisor_decisions: [], commands: [] };
}

function event(
  kind: TimelineEvent["kind"],
  monotonic_seconds: number | null,
  payload: Record<string, unknown> | null = {},
): TimelineEvent {
  return { kind, source: "controller", monotonic_seconds, recorded_at_utc: "2026-06-21T19:57:09Z", payload };
}

/** The numeric clock cell (first `.numeric` span) of a milestone row. */
function clockOf(container: HTMLElement, kind: string): string | undefined {
  return container.querySelector(`[data-kind="${kind}"] .numeric`)?.textContent ?? undefined;
}

describe("EventTimeline milestone clocks (#379)", () => {
  it("places post-T0 milestones by rebasing monotonic_seconds to the T0 event (no payload.tick needed)", () => {
    // Real controller milestone events carry monotonic_seconds but NO payload.tick.
    const { container } = render(
      <EventTimeline
        tickToSeconds={() => null} // tick path unavailable — proves monotonic is used
        timeline={timelineOf([
          event("run_started", 970, { profile: {} }),
          event("t0_detected", 1000, { bean_temp_c: 168 }),
          event("first_crack", 1520, { source: "audio_model", bean_temp_c: 200 }),
          event("run_completed", 1702, null),
        ])}
      />,
    );

    expect(clockOf(container, "t0_detected")).toBe("00:00");
    expect(clockOf(container, "first_crack")).toBe("08:40"); // 1520 - 1000 = 520 s
    expect(clockOf(container, "run_completed")).toBe("11:42"); // 1702 - 1000 = 702 s
    // Pre-T0 events stay em-dash (negative since-T0 must NOT clamp to 00:00).
    expect(clockOf(container, "run_started")).toBe("—");
  });

  it("falls back to the tick→seconds path when an event carries a tick but no monotonic", () => {
    const { container } = render(
      <EventTimeline
        tickToSeconds={(tick) => (tick === 5 ? 300 : null)}
        timeline={timelineOf([
          event("t0_detected", 1000, { bean_temp_c: 168 }),
          event("first_crack", null, { tick: 5 }),
        ])}
      />,
    );

    expect(clockOf(container, "first_crack")).toBe("05:00"); // tickToSeconds(5) = 300 s
  });
});
