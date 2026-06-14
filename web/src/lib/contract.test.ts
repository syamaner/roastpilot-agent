/// <reference types="node" />
/**
 * Contract-fixture drift guard — the SPA half (E10-S6 PR2, #98).
 *
 * The TS SSE/REST types in `@/lib/types` and `@/pages/dashboard/events` are a
 * HAND mirror of the Python contract. A hand mirror drifts silently: #115 was a
 * `phase_changed` `{phase}` → reducer-read `agent_phase` mismatch that every S2
 * test enshrined (green-but-wrong, because the tests re-asserted the wrong shape
 * against itself). This test closes that whole class by loading the REAL server
 * frames — dumped from the real `api.py`/replay emit path by
 * `tests/test_contract_fixtures.py` — and running them through the SPA's REAL
 * parsers (`applyEvent`, `dashboardReducer`, `hydrate`), never a re-declared
 * shape. A Python-side field rename/reshape the TS doesn't mirror makes one of
 * these assertions fail.
 *
 * It deliberately does NOT re-assert hand-authored payloads (the #115 trap): the
 * frames come off disk from the real model `model_dump`. The proof that it
 * actually catches drift (rename a server field, watch it go red) is recorded in
 * the PR.
 */

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import {
  applyEvent,
  hydrate,
  initialRoastStreamState,
} from "@/hooks/roastStreamReducer";
import {
  dashboardReducer,
  initialDashboardViewModel,
} from "@/pages/dashboard/useDashboardEvents";
import type {
  FcStatus,
  MicHealth,
  MicStatus,
  RoastDetail,
  RoastPhase,
  RoastSummary,
  SseEvent,
  SseEventType,
  TelemetryEventData,
} from "@/lib/types";

// --- Fixture loading -------------------------------------------------------
//
// The committed fixtures live at repo-root `tests/fixtures/contract/` (outside
// `web/`, per plan §8). Read them with node `fs` rather than a vite import so we
// never have to widen vite's `server.fs.allow` for a parent dir — `fs` is raw
// node, unaffected by vite's fs sandbox. Path is resolved from this file's URL so
// it's CWD-independent.

const HERE = dirname(fileURLToPath(import.meta.url));
const CONTRACT_DIR = resolve(HERE, "../../../tests/fixtures/contract");

interface SseFixture {
  frames: SseEvent[];
  advisory_variants: SseEvent[];
  command_variants: SseEvent[];
}
interface RestFixture {
  roast_detail: RoastDetail;
  roast_summary: RoastSummary;
}

const sse = JSON.parse(
  readFileSync(resolve(CONTRACT_DIR, "sse_frames.json"), "utf-8"),
) as SseFixture;
const rest = JSON.parse(
  readFileSync(resolve(CONTRACT_DIR, "rest_snapshots.json"), "utf-8"),
) as RestFixture;

/** Look up the single representative frame for an event type. */
function frame(type: SseEventType): SseEvent {
  const found = sse.frames.find((f) => f.event === type);
  if (found === undefined) {
    throw new Error(`fixture has no frame for event type: ${type}`);
  }
  return found;
}

/** Assert a frame's `data` carries every listed key (catches a server DROP). */
function expectKeys(data: Record<string, unknown>, keys: string[]): void {
  for (const key of keys) {
    expect(data, `missing field "${key}"`).toHaveProperty(key);
  }
}

// The phases the SPA's RoastPhase union accepts — used to prove `phase_changed`
// resolved to a REAL phase (not `undefined`, the #115 failure mode).
const PHASES: readonly RoastPhase[] = [
  "idle",
  "starting",
  "preheating",
  "roasting_pre_first_crack",
  "development",
  "cooling",
  "complete",
  "faulted",
  "operator_recovery_required",
];

// The mic-icon vocab the SPA renders (#197) — pinned so a server `MicHealth` /
// `FirstCrackStatusLiteral` value the TS union doesn't mirror is caught here.
const MIC_HEALTHS: readonly MicHealth[] = ["ok", "error", "idle"];
const FC_STATUSES: readonly FcStatus[] = [
  "disabled",
  "manual",
  "pending",
  "detected",
  "faulted",
  "unavailable",
];

describe("SSE contract — every event type has a real frame", () => {
  // The Python dump pins the full SseEventType set; mirror that here so a server
  // that adds a type (and regenerates the fixture) forces a conscious TS edit.
  const EXPECTED_EVENT_TYPES: SseEventType[] = [
    "run_started",
    "phase_changed",
    "charge_guidance",
    "t0_detected",
    "first_crack",
    "advisory",
    "command_executed",
    "command_failed",
    "safety_alert",
    "fault",
    "recovery_required",
    "recovery_acknowledged",
    "logs_exported",
    "run_completed",
    "telemetry",
    "heartbeat",
  ];

  it("the fixture covers exactly the SseEventType set the SPA declares", () => {
    const inFixture = new Set(sse.frames.map((f) => f.event));
    expect([...inFixture].sort()).toEqual([...EXPECTED_EVENT_TYPES].sort());
  });
});

describe("phase_changed — the #115 drift site", () => {
  it("the shared reducer resolves phase from the real frame (no agent_phase)", () => {
    // The literal #115 guard: a server rename `{phase}`→`{agent_phase}` (or any
    // reshape) makes `state.phase` `undefined` here — RED.
    const next = applyEvent(initialRoastStreamState, frame("phase_changed"));
    expect(next.phase).not.toBeNull();
    expect(PHASES).toContain(next.phase);
  });

  it("the real frame carries enabled_actions the action bar mirrors", () => {
    const data = frame("phase_changed").data as Record<string, unknown>;
    expectKeys(data, ["phase", "enabled_actions"]);
    const next = applyEvent(initialRoastStreamState, frame("phase_changed"));
    expect(Array.isArray(next.enabledActions)).toBe(true);
  });
});

describe("telemetry — every field the SPA renders is present", () => {
  it("the shared reducer folds the real telemetry frame", () => {
    const next = applyEvent(initialRoastStreamState, frame("telemetry"));
    expect(next.telemetry).not.toBeNull();
    const t = next.telemetry as TelemetryEventData;
    // Every TelemetryEventData field the SPA reads — a dropped/renamed server
    // field surfaces as a missing key here.
    expectKeys(t as unknown as Record<string, unknown>, [
      "agent_phase",
      "bean_temp_c",
      "env_temp_c",
      "bean_ror_c_per_min",
      "env_ror_c_per_min",
      "heat_percent",
      "fan_percent",
      "cooling_on",
      "elapsed_seconds",
      "t0_detected",
      "first_crack_detected",
      // #197: the capture-alive mic status rides the telemetry frame.
      "mic_status",
    ]);
    expect(typeof t.bean_temp_c).toBe("number");
    expect(PHASES).toContain(t.agent_phase);
  });

  it("the telemetry frame carries the full MicStatus shape (#197)", () => {
    // The replay path synthesizes a capture-alive mic_status, so the fixture pins
    // the real MicStatus shape (not null). A server-side rename/reshape of any
    // MicStatus field the TS mirror doesn't track makes a key assertion below RED.
    const next = applyEvent(initialRoastStreamState, frame("telemetry"));
    const mic = (next.telemetry as TelemetryEventData).mic_status;
    expect(mic).not.toBeNull();
    expectKeys(mic as unknown as Record<string, unknown>, [
      "mic_health",
      "audio_running",
      "fc_status",
      "queued_window_count",
      "emitted_window_count",
      "dropped_window_count",
      "processed_window_count",
      "reason",
    ]);
    // mic_health is one of the three the icon tints by (the union the SPA renders).
    expect(MIC_HEALTHS).toContain((mic as MicStatus).mic_health);
    // fc_status is one of the FC runtime-status literals.
    expect(FC_STATUSES).toContain((mic as MicStatus).fc_status);
    expect(typeof (mic as MicStatus).audio_running).toBe("boolean");
    expect(typeof (mic as MicStatus).queued_window_count).toBe("number");
  });
});

describe("dashboard page parser — folds every event it consumes", () => {
  it("advisory (CLAMP overlay): decision + evaluation fields fold into the panel", () => {
    const next = dashboardReducer(initialDashboardViewModel, {
      kind: "event",
      event: frame("advisory"),
    });
    expect(next.latestAdvisory).not.toBeNull();
    const decision = next.latestAdvisory?.decision;
    expect(decision).toBeDefined();
    expectKeys(decision as unknown as Record<string, unknown>, [
      "target_heat",
      "target_fan",
      "should_drop",
      "confidence",
      "rationale",
    ]);
    const evaluation = next.latestAdvisory?.evaluation;
    expect(evaluation).toBeDefined();
    expectKeys(evaluation as unknown as Record<string, unknown>, [
      "rule",
      "verdict",
      "input_heat",
      "input_fan",
      "adjusted_heat",
      "adjusted_fan",
      "reason",
    ]);
    // Pin the verdict VALUE on the clamp path (the replay overlay is a CLAMP),
    // matching the recovery test — not just key-presence.
    expect((evaluation as unknown as Record<string, unknown>).verdict).toBe("clamp");
    expect(next.latestAdvisory?.synthesized).toBe(true);
  });

  it("advisory pause toggle folds advisoryPaused", () => {
    const paused = sse.advisory_variants.find(
      (f) => "advisory_paused" in (f.data as Record<string, unknown>),
    );
    expect(paused).toBeDefined();
    const next = dashboardReducer(initialDashboardViewModel, {
      kind: "event",
      event: paused as SseEvent,
    });
    expect(next.advisoryPaused).toBe(true);
  });

  it("advisory skip variant is trace-only (no panel record)", () => {
    const skipped = sse.advisory_variants.find(
      (f) => "skipped" in (f.data as Record<string, unknown>),
    );
    expect(skipped).toBeDefined();
    const next = dashboardReducer(initialDashboardViewModel, {
      kind: "event",
      event: skipped as SseEvent,
    });
    expect(next.latestAdvisory).toBeNull();
  });

  it("charge_guidance frame carries the documented wire payload (ChargeGuidanceData)", () => {
    // Since #211/#215 the dashboard reducer no longer FOLDS this frame (the live
    // add-beans cue derives from phase + telemetry + band via ChargeBanner). The
    // wire-contract drift guard now asserts the raw frame payload off disk directly,
    // so a server-side field rename/drop still fails here. Folding it through the
    // reducer is verified to be a no-op below.
    const f = frame("charge_guidance");
    expectKeys(f.data as unknown as Record<string, unknown>, [
      "bean_temp_c",
      "env_temp_c",
      "guidance_min_c",
      "guidance_max_c",
    ]);
    const next = dashboardReducer(initialDashboardViewModel, { kind: "event", event: f });
    expect(next).toBe(initialDashboardViewModel);
  });

  it("fault folds the SafetyEvaluation handshake (verdict + reason)", () => {
    const next = dashboardReducer(initialDashboardViewModel, {
      kind: "event",
      event: frame("fault"),
    });
    expect(next.fault).not.toBeNull();
    expectKeys(next.fault as unknown as Record<string, unknown>, [
      "rule",
      "verdict",
      "reason",
      "input_heat",
      "input_fan",
      "adjusted_heat",
      "adjusted_fan",
    ]);
    expect(next.safetyTrail.some((e) => e.kind === "fault")).toBe(true);
  });

  it("recovery_required folds the recovery handshake + trail", () => {
    const next = dashboardReducer(initialDashboardViewModel, {
      kind: "event",
      event: frame("recovery_required"),
    });
    expect(next.recovery).not.toBeNull();
    expect((next.recovery as unknown as Record<string, unknown>).verdict).toBe(
      "recovery",
    );
    expect(next.safetyTrail.some((e) => e.kind === "recovery_required")).toBe(true);
  });

  it("safety_alert folds into the safety trail", () => {
    const next = dashboardReducer(initialDashboardViewModel, {
      kind: "event",
      event: frame("safety_alert"),
    });
    expect(next.safetyTrail.some((e) => e.kind === "safety_alert")).toBe(true);
    expectKeys(
      next.safetyTrail[0].evaluation as unknown as Record<string, unknown>,
      ["rule", "verdict", "reason"],
    );
  });

  it("t0_detected sets the T0 marker", () => {
    const next = dashboardReducer(initialDashboardViewModel, {
      kind: "event",
      event: frame("t0_detected"),
    });
    expect(next.t0).not.toBeNull();
    expect(next.markers.some((m) => m.kind === "t0")).toBe(true);
  });

  it("first_crack carries source and sets the FC marker", () => {
    const next = dashboardReducer(initialDashboardViewModel, {
      kind: "event",
      event: frame("first_crack"),
    });
    expect(next.firstCrack).not.toBeNull();
    expectKeys(next.firstCrack as unknown as Record<string, unknown>, ["source"]);
    expect(next.markers.some((m) => m.kind === "first_crack")).toBe(true);
  });

  it("command_executed drop variant sets the DROP marker", () => {
    const drop = sse.command_variants.find(
      (f) => (f.data as Record<string, unknown>).command === "drop_beans",
    );
    expect(drop).toBeDefined();
    const next = dashboardReducer(initialDashboardViewModel, {
      kind: "event",
      event: drop as SseEvent,
    });
    expect(next.markers.some((m) => m.kind === "drop")).toBe(true);
  });
});

describe("REST snapshot contract", () => {
  it("RoastDetail hydrates phase + enabled_actions through the shared reducer", () => {
    const next = hydrate(initialRoastStreamState, rest.roast_detail);
    expect(PHASES).toContain(next.phase);
    expect(Array.isArray(next.enabledActions)).toBe(true);
  });

  it("RoastDetail carries every field the SPA reads (incl. enabled_actions)", () => {
    expectKeys(rest.roast_detail as unknown as Record<string, unknown>, [
      "id",
      "agent_phase",
      "profile",
      "outcome",
      "started_at_utc",
      "completed_at_utc",
      "fault_reason",
      "rating",
      "notes",
      "export_manifest",
      "enabled_actions",
    ]);
    // #197: the active-run snapshot carries the capture-alive mic status the
    // header paints the icon from before the first telemetry frame.
    expect(rest.roast_detail.mic_status).not.toBeNull();
    expectKeys(rest.roast_detail.mic_status as unknown as Record<string, unknown>, [
      "mic_health",
      "audio_running",
      "fc_status",
      "queued_window_count",
      "emitted_window_count",
      "dropped_window_count",
      "processed_window_count",
      "reason",
    ]);
    expect(MIC_HEALTHS).toContain((rest.roast_detail.mic_status as MicStatus).mic_health);
    // The nested profile the detail/header render from.
    expectKeys(rest.roast_detail.profile as unknown as Record<string, unknown>, [
      "name",
      "bean_origin",
      "bean_varietal",
      "bean_weight_grams",
      "charge_guidance_min_c",
      "charge_guidance_max_c",
      "initial_heat_percent",
      "initial_fan_percent",
      "target_drop_temp_c",
      "target_development_percent",
    ]);
  });

  it("RoastSummary carries every field the history list reads", () => {
    expectKeys(rest.roast_summary as unknown as Record<string, unknown>, [
      "id",
      "started_at_utc",
      "completed_at_utc",
      "agent_phase",
      "outcome",
      "bean_origin",
      "bean_varietal",
      "rating",
      "development_percent",
    ]);
  });
});
