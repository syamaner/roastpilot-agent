import { describe, expect, it } from "vitest";

import type { RoastDetail, SseEvent } from "@/lib/types";
import {
  applyEvent,
  hydrate,
  initialRoastStreamState,
} from "./roastStreamReducer";

function snapshot(overrides: Partial<RoastDetail> = {}): RoastDetail {
  return {
    id: "run-1",
    agent_phase: "preheating",
    profile: {
      name: "Test",
      bean_origin: "Ethiopia",
      bean_varietal: null,
      bean_weight_grams: 250,
      charge_guidance_min_c: 170,
      charge_guidance_max_c: 200,
      initial_heat_percent: 80,
      initial_fan_percent: 40,
      target_drop_temp_c: 215,
      target_development_percent: 20,
    },
    outcome: null,
    started_at_utc: "2026-06-07T13:00:00Z",
    completed_at_utc: null,
    fault_reason: null,
    rating: null,
    notes: null,
    export_manifest: null,
    ...overrides,
  };
}

describe("roastStreamReducer", () => {
  it("hydrates phase from the snapshot", () => {
    const state = hydrate(initialRoastStreamState, snapshot({ agent_phase: "development" }));
    expect(state.phase).toBe("development");
  });

  it("hydrates enabled_actions when the snapshot carries them", () => {
    const state = hydrate(
      initialRoastStreamState,
      snapshot({ enabled_actions: ["drop_beans", "emergency_stop"] }),
    );
    expect(state.enabledActions).toEqual(["drop_beans", "emergency_stop"]);
  });

  it("sets phase from a phase_changed event", () => {
    const event: SseEvent = {
      event: "phase_changed",
      data: { agent_phase: "cooling" },
      id: 5,
    };
    const state = applyEvent({ ...initialRoastStreamState, phase: "development" }, event);
    expect(state.phase).toBe("cooling");
  });

  it("NEVER infers phase from a telemetry frame (invariant)", () => {
    // A telemetry frame carries agent_phase, but it must not drive state.phase —
    // only phase_changed + the hydrate snapshot may. Here the telemetry claims a
    // different phase than the current one; phase must stay put.
    const start = { ...initialRoastStreamState, phase: "preheating" as const };
    const event: SseEvent = {
      event: "telemetry",
      data: {
        agent_phase: "development",
        bean_temp_c: 150,
        env_temp_c: 180,
        bean_ror_c_per_min: 10,
        env_ror_c_per_min: 8,
        heat_percent: 80,
        fan_percent: 40,
        cooling_on: false,
        elapsed_seconds: 30,
        t0_detected: true,
        first_crack_detected: false,
      },
      id: 2,
    };
    const state = applyEvent(start, event);
    expect(state.phase).toBe("preheating"); // unchanged
    expect(state.telemetry?.bean_temp_c).toBe(150); // telemetry still recorded
  });

  it("ignores heartbeat frames (no state change)", () => {
    const start = { ...initialRoastStreamState, phase: "cooling" as const };
    const next = applyEvent(start, { event: "heartbeat", data: {}, id: 9 });
    expect(next).toBe(start); // same reference → no re-render
  });

  it("drops out-of-order/duplicate frames by id", () => {
    const start = { ...initialRoastStreamState, phase: "preheating" as const, lastEventId: 10 };
    const stale: SseEvent = { event: "phase_changed", data: { agent_phase: "cooling" }, id: 7 };
    const next = applyEvent(start, stale);
    expect(next).toBe(start);
    expect(next.phase).toBe("preheating");
  });

  it("advances lastEventId on applied frames", () => {
    const event: SseEvent = { event: "phase_changed", data: { agent_phase: "development" }, id: 12 };
    const state = applyEvent(initialRoastStreamState, event);
    expect(state.lastEventId).toBe(12);
  });

  it("re-hydration after reconnect re-bases phase on the server snapshot", () => {
    // Simulate: live state drifted, then a reconnect hydrates fresh truth.
    const drifted = { ...initialRoastStreamState, phase: "development" as const };
    const rebased = hydrate(drifted, snapshot({ agent_phase: "operator_recovery_required" }));
    expect(rebased.phase).toBe("operator_recovery_required");
  });
});
