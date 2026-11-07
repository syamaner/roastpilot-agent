import { describe, expect, it } from "vitest";

import {
  isCoherentFaultControls,
  isCoherentRestartClearance,
  parseStrandedFaultRetirementResult,
  strandedFaultRetirementState,
} from "./faultControls";
import type { FaultControls, RestartClearance } from "./types";

const restart: RestartClearance = {
  state: "required" as const,
  eligible: false,
  enabled_actions: ["retire_stranded_fault"],
  closure_only_available: false,
};
const controls: FaultControls = {
  status: "open" as const,
  generation: 3,
  enabled_actions: ["emergency_stop"],
  physical_stop_operative: true,
};
const result = {
  outcome: "confirmed",
  attempted_generation: 3,
  restart_clearance: restart,
  fault_controls: controls,
};

describe("stranded-fault retirement admission", () => {
  it("admits only the dedicated required restart shape", () => {
    expect(isCoherentRestartClearance(restart)).toBe(true);
    expect(isCoherentRestartClearance({ ...restart, eligible: true })).toBe(false);
    expect(isCoherentRestartClearance({ ...restart, closure_only_available: true })).toBe(false);
    expect(isCoherentRestartClearance({ ...restart, enabled_actions: ["retire_stranded_fault", "initialize_safety_record"] })).toBe(false);
    expect(isCoherentRestartClearance({ ...restart, state: "cleared" })).toBe(false);
  });

  it("validates generic provenance and physical-stop types without requiring their presence", () => {
    expect(isCoherentFaultControls(controls)).toBe(true);
    expect(isCoherentFaultControls({ ...controls, closure_provenance: "safe_zero" })).toBe(false);
    expect(isCoherentFaultControls({ ...controls, physical_stop_operative: "true" })).toBe(false);
    expect(isCoherentFaultControls({ ...controls, physical_stop_operative: 1 })).toBe(false);
    expect(isCoherentFaultControls({ ...controls, physical_stop_operative: null })).toBe(false);
    expect(isCoherentFaultControls({ ...controls, closure_provenance: "baseline" })).toBe(false);
    expect(isCoherentFaultControls({ ...controls, physical_stop_operative: undefined })).toBe(true);
  });

  it("derives a local reset key only for the advertised, physically operative open lease", () => {
    expect(strandedFaultRetirementState(restart, controls, "p1")).toEqual({ kind: "available", resetKey: "p1#3" });
    expect(strandedFaultRetirementState({ ...restart, enabled_actions: [] }, controls, undefined)).toEqual({ kind: "absent" });
    expect(strandedFaultRetirementState(restart, { ...controls, generation: 0 }, "p1")).toEqual({ kind: "incoherent" });
    expect(strandedFaultRetirementState(restart, { ...controls, physical_stop_operative: false }, "p1")).toEqual({ kind: "incoherent" });
    for (const id of [undefined, "", 42, null]) expect(strandedFaultRetirementState(restart, controls, id)).toEqual({ kind: "incoherent" });
  });
});

describe("stranded-fault retirement result parser", () => {
  it("accepts independent nested generations and permitted outcome generations", () => {
    expect(parseStrandedFaultRetirementResult({ ...result, fault_controls: { ...controls, generation: 4 } })).toMatchObject({ outcome: "confirmed" });
    expect(parseStrandedFaultRetirementResult({ ...result, outcome: "unconfirmed" })).toMatchObject({ outcome: "unconfirmed" });
    expect(parseStrandedFaultRetirementResult({ ...result, outcome: "rejected", attempted_generation: null })).toMatchObject({ outcome: "rejected" });
  });

  it("rejects malformed values with one fixed error", () => {
    const error = "Stranded-fault retirement result could not be confirmed.";
    for (const value of [null, "x", [], { ...result, attempted_generation: null }, { ...result, attempted_generation: 0 }, { ...result, attempted_generation: 1.5 }, { ...result, outcome: "unknown" }, { ...result, extra: "run-id" }, { outcome: "confirmed", attempted_generation: 3, restart_clearance: restart }, { ...result, fault_controls: { ...controls, physical_stop_operative: "true" } }]) expect(() => parseStrandedFaultRetirementResult(value)).toThrow(error);
  });
});
