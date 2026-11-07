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
  it("TC1: admits only the dedicated required restart shape", () => {
    expect(isCoherentRestartClearance(restart)).toBe(true);
    for (const candidate of [
      { ...restart, eligible: true },
      { ...restart, closure_only_available: true },
      { ...restart, enabled_actions: ["retire_stranded_fault", "initialize_safety_record"] },
      { ...restart, enabled_actions: ["retire_stranded_fault", "retire_stranded_fault"] },
      { ...restart, enabled_actions: [" retire_stranded_fault"] },
      { ...restart, enabled_actions: ["RETIRE_STRANDED_FAULT"] },
      { ...restart, enabled_actions: ["unknown"] },
      { ...restart, enabled_actions: [1] },
      { ...restart, state: "closure_only" },
      { ...restart, state: "unknown" },
      { ...restart, state: "cleared" },
    ]) expect(isCoherentRestartClearance(candidate)).toBe(false);
  });

  it("TC2: validates every provenance and physical-stop type without masking invalid provenance", () => {
    for (const provenance of ["exact_session_safe_zero", "operator_attested_retirement", "baseline"]) {
      for (const physicalStopOperative of [true, false, undefined]) {
        expect(isCoherentFaultControls({ ...controls, status: "closed", enabled_actions: ["emergency_stop"], closure_provenance: provenance, physical_stop_operative: physicalStopOperative })).toBe(true);
      }
    }
    for (const physicalStopOperative of [true, false, undefined]) {
      expect(isCoherentFaultControls({ ...controls, closure_provenance: null, physical_stop_operative: physicalStopOperative })).toBe(true);
      expect(isCoherentFaultControls({ ...controls, physical_stop_operative: physicalStopOperative })).toBe(true);
    }
    const closed = { ...controls, status: "closed" as const, enabled_actions: ["emergency_stop"] };
    for (const provenance of ["safe_zero", "Baseline", 1]) {
      expect(isCoherentFaultControls({ ...closed, closure_provenance: provenance })).toBe(false);
    }
    for (const physicalStopOperative of ["true", 1, null]) {
      expect(isCoherentFaultControls({ ...controls, physical_stop_operative: physicalStopOperative })).toBe(false);
    }
    for (const status of ["open", "unknown"] as const) {
      expect(isCoherentFaultControls({ ...controls, status, closure_provenance: "baseline" })).toBe(false);
    }
  });

  it("TC3/TC3-O5: derives a key only for the advertised, physically operative open lease", () => {
    expect(strandedFaultRetirementState(restart, controls, "p1")).toEqual({ kind: "available", resetKey: "p1#3" });
    expect(strandedFaultRetirementState({ ...restart, enabled_actions: [] }, controls, undefined)).toEqual({ kind: "absent" });
    for (const invalidControls of [
      { ...controls, status: "closed" as const, enabled_actions: ["emergency_stop"] },
      { ...controls, status: "unknown" as const },
      { ...controls, generation: 0 },
      { ...controls, physical_stop_operative: false },
      { ...controls, physical_stop_operative: undefined },
    ]) expect(strandedFaultRetirementState(restart, invalidControls as FaultControls, "p1")).toEqual({ kind: "incoherent" });
    for (const id of [undefined, "", 42, null]) {
      expect(strandedFaultRetirementState(restart, controls, id)).toEqual({ kind: "incoherent" });
      expect(strandedFaultRetirementState({ ...restart, enabled_actions: [] }, controls, id)).toEqual({ kind: "absent" });
    }
  });
});

describe("stranded-fault retirement result parser", () => {
  it("TC4a: accepts independent nested generations and permitted outcome generations", () => {
    expect(parseStrandedFaultRetirementResult({ ...result, fault_controls: { ...controls, generation: 4 } })).toMatchObject({ outcome: "confirmed" });
    expect(parseStrandedFaultRetirementResult({ ...result, outcome: "unconfirmed" })).toMatchObject({ outcome: "unconfirmed" });
    expect(parseStrandedFaultRetirementResult({ ...result, outcome: "rejected", attempted_generation: null })).toMatchObject({ outcome: "rejected" });
    expect(parseStrandedFaultRetirementResult({ ...result, outcome: "rejected", attempted_generation: 2 })).toMatchObject({ outcome: "rejected" });
  });

  it("TC4b-d: rejects every malformed generation, key set, object and nested projection with one fixed error", () => {
    const error = "Stranded-fault retirement result could not be confirmed.";
    const missingKeys = ["outcome", "attempted_generation", "restart_clearance", "fault_controls"] as const;
    const missing = missingKeys.map((key) => {
      const copy = { ...result } as Record<string, unknown>;
      delete copy[key];
      return copy;
    });
    const invalidGenerations = [null, 0, -1, 1.5, "1"];
    const generationCases = [
      ...invalidGenerations.map((attempted_generation) => ({ ...result, attempted_generation })),
      { ...result, outcome: "unconfirmed", attempted_generation: null },
      ...[0, -1, 1.5, "1"].map((attempted_generation) => ({
        ...result,
        outcome: "unconfirmed",
        attempted_generation,
      })),
      ...[0, -1, 1.5, "1"].map((attempted_generation) => ({
        ...result,
        outcome: "rejected",
        attempted_generation,
      })),
    ];
    for (const value of [
      null, "x", [], [1, 2, 3, 4],
      ...generationCases,
      ...missing,
      { ...result, reason: "run-id" }, { ...result, run_id: "run-id" }, { ...result, instance_id: "process-id" },
      { ...result, outcome: "unknown" },
      { ...result, restart_clearance: [] },
      { ...result, fault_controls: [] },
      { ...result, fault_controls: { ...controls, physical_stop_operative: "true" } },
    ]) expect(() => parseStrandedFaultRetirementResult(value)).toThrow(error);
  });
});
