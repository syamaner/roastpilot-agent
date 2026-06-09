import { describe, expect, it } from "vitest";

import type { SseEvent } from "@/lib/types";
import {
  ADVISORY_HISTORY_LIMIT,
  dashboardReducer,
  initialDashboardViewModel,
} from "./useDashboardEvents";

function ev<T>(event: SseEvent["event"], data: T, id?: number): { kind: "event"; event: SseEvent } {
  return { kind: "event", event: { event, data: data as Record<string, unknown>, id } };
}

const ADVISORY_DECISION = {
  trigger: "tick",
  decision: { target_heat: 60, target_fan: 75, should_drop: false, confidence: 0.82, rationale: "hold" },
  evaluation: {
    rule: "rate_limit",
    verdict: "clamp",
    input_heat: 80,
    input_fan: 40,
    adjusted_heat: 65,
    adjusted_fan: 40,
    reason: "heat clamped 80→65",
  },
};

describe("dashboardReducer", () => {
  it("appends a curve point per telemetry frame (x = elapsed seconds)", () => {
    let s = initialDashboardViewModel;
    s = dashboardReducer(s, ev("telemetry", { elapsed_seconds: 10, bean_temp_c: 120, env_temp_c: 140, bean_ror_c_per_min: 16, heat_percent: 70, fan_percent: 40 }));
    s = dashboardReducer(s, ev("telemetry", { elapsed_seconds: 11, bean_temp_c: 121, env_temp_c: 141, bean_ror_c_per_min: 15, heat_percent: 70, fan_percent: 40 }));
    expect(s.points).toHaveLength(2);
    expect(s.points[0]).toMatchObject({ t: 10, bean: 120, heat: 70 });
  });

  it("skips telemetry with no elapsed_seconds (can't place it on the x-axis)", () => {
    const s = dashboardReducer(
      initialDashboardViewModel,
      ev("telemetry", { elapsed_seconds: null, bean_temp_c: 120, env_temp_c: 140 }),
    );
    expect(s.points).toHaveLength(0);
  });

  it("sets the latest advisory + verdict from an advisory frame with a decision", () => {
    const s = dashboardReducer(initialDashboardViewModel, ev("advisory", ADVISORY_DECISION));
    expect(s.latestAdvisory?.decision?.target_heat).toBe(60);
    expect(s.latestAdvisory?.evaluation?.verdict).toBe("clamp");
    expect(s.advisoryHistory).toHaveLength(1);
  });

  it("marks the synthesized replay CLAMP key frame", () => {
    const s = dashboardReducer(
      initialDashboardViewModel,
      ev("advisory", { ...ADVISORY_DECISION, synthesized: true, source: "replay_overlay" }),
    );
    expect(s.latestAdvisory?.synthesized).toBe(true);
  });

  it("caps the advisory history at the display limit", () => {
    let s = initialDashboardViewModel;
    for (let i = 0; i < ADVISORY_HISTORY_LIMIT + 3; i++) {
      s = dashboardReducer(s, ev("advisory", ADVISORY_DECISION));
    }
    expect(s.advisoryHistory).toHaveLength(ADVISORY_HISTORY_LIMIT);
  });

  it("assigns each advisory a unique, monotonic seq (stable list key)", () => {
    let s = initialDashboardViewModel;
    s = dashboardReducer(s, ev("advisory", ADVISORY_DECISION));
    s = dashboardReducer(s, ev("advisory", ADVISORY_DECISION));
    s = dashboardReducer(s, ev("advisory", ADVISORY_DECISION));
    const seqs = s.advisoryHistory.map((r) => r.seq);
    expect(new Set(seqs).size).toBe(seqs.length); // all unique
    expect(s.advisorySeq).toBe(3);
  });

  it("folds the pause/resume toggle into advisoryPaused without entering the feed", () => {
    let s = dashboardReducer(initialDashboardViewModel, ev("advisory", { advisory_paused: true }));
    expect(s.advisoryPaused).toBe(true);
    expect(s.advisoryHistory).toHaveLength(0);
    s = dashboardReducer(s, ev("advisory", { advisory_paused: false }));
    expect(s.advisoryPaused).toBe(false);
  });

  it("ignores a skipped advisory record (no decision, no evaluation)", () => {
    const s = dashboardReducer(
      initialDashboardViewModel,
      ev("advisory", { trigger: "manual", skipped: "no_telemetry" }),
    );
    expect(s.latestAdvisory).toBeNull();
    expect(s.advisoryHistory).toHaveLength(0);
  });

  it("captures charge guidance for the toast", () => {
    const s = dashboardReducer(
      initialDashboardViewModel,
      ev("charge_guidance", { bean_temp_c: 180, env_temp_c: 190, guidance_min_c: 170, guidance_max_c: 200 }),
    );
    expect(s.chargeGuidance?.guidance_min_c).toBe(170);
  });

  it("captures a recovery handshake and adds it to the safety trail", () => {
    const evalData = { rule: "pre_t0_overrun", verdict: "recovery", input_heat: null, input_fan: null, adjusted_heat: 0, adjusted_fan: 100, reason: "pre-T0 overrun" };
    const s = dashboardReducer(initialDashboardViewModel, ev("recovery_required", evalData));
    expect(s.recovery?.verdict).toBe("recovery");
    expect(s.safetyTrail).toHaveLength(1);
    expect(s.safetyTrail[0].kind).toBe("recovery_required");
  });

  it("clears the recovery trigger on recovery_acknowledged", () => {
    let s = dashboardReducer(initialDashboardViewModel, ev("recovery_required", { rule: "r", verdict: "recovery", input_heat: null, input_fan: null, adjusted_heat: 0, adjusted_fan: 100, reason: "x" }));
    s = dashboardReducer(s, ev("recovery_acknowledged", {}));
    expect(s.recovery).toBeNull();
  });

  it("captures a fault handshake + accumulates safety_alert + fault on the trail", () => {
    let s = dashboardReducer(initialDashboardViewModel, ev("safety_alert", { rule: "env_ceiling", verdict: "emergency_stop", input_heat: null, input_fan: null, adjusted_heat: 0, adjusted_fan: 100, reason: "env temp exceeded ceiling" }));
    s = dashboardReducer(s, ev("fault", { rule: "env_ceiling", verdict: "fault", input_heat: null, input_fan: null, adjusted_heat: 0, adjusted_fan: 100, reason: "faulted" }));
    expect(s.fault?.verdict).toBe("fault");
    expect(s.safetyTrail.map((e) => e.kind)).toEqual(["safety_alert", "fault"]);
  });

  it("adds T0 and first-crack markers (once each)", () => {
    let s = dashboardReducer(initialDashboardViewModel, ev("t0_detected", { bean_temp_c: 175 }));
    s = dashboardReducer(s, ev("telemetry", { elapsed_seconds: 500, bean_temp_c: 200, env_temp_c: 210 }));
    s = dashboardReducer(s, ev("first_crack", { source: "mcp", bean_temp_c: 201 }));
    // Re-deliver first_crack — must not duplicate the marker.
    s = dashboardReducer(s, ev("first_crack", { source: "mcp", bean_temp_c: 201 }));
    expect(s.t0).not.toBeNull();
    expect(s.firstCrack?.source).toBe("mcp");
    expect(s.markers.filter((m) => m.kind === "first_crack")).toHaveLength(1);
    expect(s.markers.find((m) => m.kind === "first_crack")?.t).toBe(500);
  });

  it("adds a drop marker on a drop_beans command_executed", () => {
    let s = dashboardReducer(initialDashboardViewModel, ev("telemetry", { elapsed_seconds: 600, bean_temp_c: 215, env_temp_c: 220 }));
    s = dashboardReducer(s, ev("command_executed", { command: "drop_beans", source: "operator" }));
    expect(s.markers.find((m) => m.kind === "drop")?.t).toBe(600);
  });

  it("ignores a non-drop command_executed (no marker)", () => {
    const s = dashboardReducer(
      initialDashboardViewModel,
      ev("command_executed", { heat_percent: 65, fan_percent: 40 }),
    );
    expect(s.markers).toHaveLength(0);
  });

  it("resets to the initial view-model", () => {
    let s = dashboardReducer(initialDashboardViewModel, ev("advisory", ADVISORY_DECISION));
    s = dashboardReducer(s, { kind: "reset" });
    expect(s).toEqual(initialDashboardViewModel);
  });
});

// These pin the EXACT field names the page handlers read off raw `lastEvent.data`
// — the payloads are NOT in shared types.ts (the handlers cast), so this is the
// guard against the contract-drift class that bit `phase_changed` (server sent
// `phase`, the reducer read `agent_phase`). Each payload below is keyed exactly as
// the server emit site keys it (controller.py / replay.py / safety.py model_dump);
// if a server field is renamed, the matching assertion fails loud here.
describe("dashboardReducer — payload field-name contract", () => {
  it("advisory: reads decision.{target_heat,target_fan,should_drop,confidence,rationale} + evaluation.{verdict,reason}", () => {
    // advisor.RoastDecision.model_dump + safety.SafetyEvaluation.model_dump, as
    // emitted at controller.py:814 / replay.py:645.
    const s = dashboardReducer(
      initialDashboardViewModel,
      ev("advisory", {
        trigger: "tick",
        decision: { target_heat: 60, target_fan: 75, should_drop: true, confidence: 0.82, rationale: "stretch development" },
        evaluation: { rule: "rate_limit", verdict: "clamp", input_heat: 80, input_fan: 40, adjusted_heat: 65, adjusted_fan: 40, reason: "heat clamped 80→65" },
      }),
    );
    const a = s.latestAdvisory;
    expect(a?.decision).toEqual({ target_heat: 60, target_fan: 75, should_drop: true, confidence: 0.82, rationale: "stretch development" });
    expect(a?.evaluation?.verdict).toBe("clamp");
    expect(a?.evaluation?.reason).toBe("heat clamped 80→65");
  });

  it("advisory toggle: reads `advisory_paused` (controller.py:1129/1135)", () => {
    expect(
      dashboardReducer(initialDashboardViewModel, ev("advisory", { advisory_paused: true })).advisoryPaused,
    ).toBe(true);
  });

  it("charge_guidance: reads {bean_temp_c,env_temp_c,guidance_min_c,guidance_max_c} (controller.py:632)", () => {
    const g = dashboardReducer(
      initialDashboardViewModel,
      ev("charge_guidance", { bean_temp_c: 185, env_temp_c: 195, guidance_min_c: 170, guidance_max_c: 200 }),
    ).chargeGuidance;
    expect(g).toEqual({ bean_temp_c: 185, env_temp_c: 195, guidance_min_c: 170, guidance_max_c: 200 });
  });

  it("recovery_required / fault: reads SafetyEvaluation {rule,verdict,input_heat,input_fan,adjusted_heat,adjusted_fan,reason}", () => {
    const evalData = { rule: "pre_t0_overrun", verdict: "recovery" as const, input_heat: null, input_fan: null, adjusted_heat: 0, adjusted_fan: 100, reason: "pre-T0 overrun" };
    const r = dashboardReducer(initialDashboardViewModel, ev("recovery_required", evalData)).recovery;
    expect(r).toEqual(evalData);
    const faultData = { ...evalData, verdict: "fault" as const, reason: "faulted" };
    const f = dashboardReducer(initialDashboardViewModel, ev("fault", faultData)).fault;
    expect(f).toEqual(faultData);
  });

  it("first_crack: reads `source` + `bean_temp_c` (controller.py:620)", () => {
    const fc = dashboardReducer(
      initialDashboardViewModel,
      ev("first_crack", { source: "mcp", bean_temp_c: 201.2 }),
    ).firstCrack;
    expect(fc).toEqual({ source: "mcp", bean_temp_c: 201.2 });
  });

  it("command_executed: reads `command` (the drop marker key, controller.py:849/1030)", () => {
    const s = dashboardReducer(
      dashboardReducer(initialDashboardViewModel, ev("telemetry", { elapsed_seconds: 600, bean_temp_c: 215, env_temp_c: 220 })),
      ev("command_executed", { command: "drop_beans", source: "advisor" }),
    );
    expect(s.markers.some((m) => m.kind === "drop")).toBe(true);
  });
});
