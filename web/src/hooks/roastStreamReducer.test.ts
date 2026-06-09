import { describe, expect, it } from "vitest";

import type { OperatorAction, RoastDetail, SseEvent } from "@/lib/types";
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

  it("sets phase from a phase_changed event (wire field is `phase`)", () => {
    // The server emits phase_changed as `{phase}` (NOT `agent_phase` — that's the
    // RoastDetail snapshot field). The reducer must read `phase`.
    const event: SseEvent = {
      event: "phase_changed",
      data: { phase: "cooling" },
      id: 5,
    };
    const state = applyEvent({ ...initialRoastStreamState, phase: "development" }, event);
    expect(state.phase).toBe("cooling");
  });

  it("a wrong-shaped phase_changed (agent_phase, no phase) does NOT set phase", () => {
    // Regression guard for the drift this fix corrected: a frame carrying only
    // `agent_phase` is malformed for this event, so `phase` must not silently
    // become that value — it ends up undefined, never the stale-but-wrong field.
    const event: SseEvent = {
      event: "phase_changed",
      data: { agent_phase: "cooling" } as unknown as Record<string, unknown>,
      id: 5,
    };
    const state = applyEvent({ ...initialRoastStreamState, phase: "development" }, event);
    expect(state.phase).not.toBe("cooling");
  });

  it("updates enabledActions from a phase_changed event that carries them", () => {
    // The live action-bar update mechanism (S3): the server re-sends
    // enabled_actions on phase_changed, so the bar mirrors the new phase.
    const prior: OperatorAction[] = ["mark_beans_added", "emergency_stop"];
    const next: OperatorAction[] = ["start_cooling", "stop_cooling", "emergency_stop"];
    const start = { ...initialRoastStreamState, phase: "preheating" as const, enabledActions: prior };
    const event: SseEvent = {
      event: "phase_changed",
      data: { phase: "cooling", enabled_actions: next },
      id: 6,
    };
    const state = applyEvent(start, event);
    expect(state.phase).toBe("cooling");
    expect(state.enabledActions).toEqual(next);
  });

  it("preserves enabledActions when a phase_changed event omits them", () => {
    // Forward-compat: before the E7 enabled_actions contract lands (or any frame
    // that omits the field), a phase change must not wipe a known action set.
    const prior: OperatorAction[] = ["mark_beans_added", "emergency_stop"];
    const start = { ...initialRoastStreamState, phase: "preheating" as const, enabledActions: prior };
    const event: SseEvent = {
      event: "phase_changed",
      data: { phase: "roasting_pre_first_crack" },
      id: 7,
    };
    const state = applyEvent(start, event);
    expect(state.phase).toBe("roasting_pre_first_crack");
    expect(state.enabledActions).toEqual(prior);
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
    const stale: SseEvent = { event: "phase_changed", data: { phase: "cooling" }, id: 7 };
    const next = applyEvent(start, stale);
    expect(next).toBe(start);
    expect(next.phase).toBe("preheating");
  });

  it("advances lastEventId on applied frames", () => {
    const event: SseEvent = { event: "phase_changed", data: { phase: "development" }, id: 12 };
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
