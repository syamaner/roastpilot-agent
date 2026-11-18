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

it("applies only complete same-run HUMAN projections before frame-ID dedup", () => {
  const current = {
    run_id: "run-1",
    human_endpoint: "issued",
    human_command_disposition: "settled",
    physical_intervention_required: false,
    emergency_fallback_disclosure: "Emergency fallback may stop the drum.",
  };
  const normal = applyEvent(initialRoastStreamState, {
    event: "phase_changed", id: 10, data: { phase: "cooling" },
  });
  const next = applyEvent(normal, { event: "heartbeat", id: 10, data: current }, "run-1");
  expect(next).toMatchObject({
    phase: "cooling",
    lastEventId: 10,
    humanProtection: { data: { human_endpoint: "issued" } },
  });
  const duplicate = applyEvent(next, {
    event: "phase_changed", id: 10, data: { phase: "faulted" },
  });
  expect(duplicate).toBe(next);
  for (const data of [
    { ...current, run_id: "previous-run" },
    { ...current, human_endpoint: "cooling_confirmed" },
    { ...current, human_command_disposition: true },
    { ...current, emergency_fallback_disclosure: 0 },
  ]) {
    expect(applyEvent(next, { event: "heartbeat", data }, "run-1")).toBe(next);
  }
  expect(hydrate(next, snapshot({ id: "other" }), "run-1").humanProtection)
    .toBe(next.humanProtection);
});

describe("run history durability", () => {
  const allNull = {
    human_endpoint: null,
    human_command_disposition: null,
    physical_intervention_required: null,
    emergency_fallback_disclosure: null,
  };
  const runId = "run-1";

  it.each(["complete", "delayed", "incomplete", "unknown", "unavailable"] as const)(
    "hydrates a durability-only %s snapshot without deriving phase or actions",
    (durability) => {
      const state = hydrate(initialRoastStreamState, snapshot({
        ...allNull,
        agent_phase: "operator_recovery_required",
        enabled_actions: ["emergency_stop"],
        human_history_durability: durability,
      }));
      expect(state).toMatchObject({
        phase: "operator_recovery_required",
        enabledActions: ["emergency_stop"],
        lastEventId: null,
        humanProtection: {
          runId, data: allNull, historyDurability: durability, historyIdentifiesHuman: true,
        },
      });
    },
  );

  it("accepts changed legacy evidence with bad history and keeps verified owner until an explicit clear", () => {
    const start = hydrate(initialRoastStreamState, snapshot({
      ...allNull, human_history_durability: "complete",
    }));
    const heartbeat = (data: Record<string, unknown>, state = start) =>
      applyEvent(state, { event: "heartbeat", id: 99, data: { run_id: runId, ...data } }, runId);

    const incompleteLegacy = { ...allNull, physical_intervention_required: undefined };
    expect(heartbeat({ ...incompleteLegacy, human_history_durability: "delayed" })).toBe(start);
    expect(heartbeat({ ...incompleteLegacy, human_history_durability: null })).toBe(start);
    expect(heartbeat({ ...allNull, run_id: "other", human_history_durability: "unavailable" }))
      .toBe(start);

    const legacyChange = heartbeat({
      ...allNull, human_endpoint: "withheld", human_history_durability: ["delayed"],
    });
    expect(legacyChange.humanProtection).toEqual({
      runId,
      data: { ...allNull, human_endpoint: "withheld" },
      historyDurability: null,
      historyIdentifiesHuman: true,
    });
    expect(legacyChange.lastEventId).toBeNull();

    const legacyOnly = heartbeat(allNull, legacyChange);
    expect(legacyOnly.humanProtection).toMatchObject({
      data: allNull, historyDurability: null, historyIdentifiesHuman: true,
    });
    const clear = heartbeat({ ...allNull, human_history_durability: null }, legacyOnly);
    expect(clear.humanProtection).toMatchObject({
      data: allNull, historyDurability: null, historyIdentifiesHuman: false,
    });

    const replacement = hydrate(start, snapshot({ ...allNull, id: "new-run" }), "new-run");
    expect(replacement.humanProtection).toMatchObject({
      runId: "new-run", data: allNull, historyDurability: null, historyIdentifiesHuman: false,
    });
  });
});
