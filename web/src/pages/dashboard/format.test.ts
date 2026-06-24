import { describe, expect, it } from "vitest";

import type { RoastPhase } from "@/lib/types";

import {
  formatClock,
  formatConfidence,
  formatPercent,
  formatRoR,
  formatTempC,
  isPreFirstCrackPhase,
  PHASE_LABEL,
  phaseAccentVar,
} from "./format";

describe("format helpers", () => {
  it("formats seconds as mm:ss with padding", () => {
    expect(formatClock(0)).toBe("00:00");
    expect(formatClock(72)).toBe("01:12");
    expect(formatClock(582)).toBe("09:42");
  });

  it("returns a placeholder clock for null/negative/non-finite", () => {
    expect(formatClock(null)).toBe("--:--");
    expect(formatClock(-5)).toBe("--:--");
    expect(formatClock(Number.NaN)).toBe("--:--");
  });

  it("formats Celsius to one decimal with a unit", () => {
    expect(formatTempC(201.23)).toBe("201.2 °C");
    expect(formatTempC(null)).toBe("— °C");
  });

  it("formats percent as a whole number", () => {
    expect(formatPercent(64.6)).toBe("65 %");
    expect(formatPercent(null)).toBe("— %");
  });

  it("formats RoR with a unit", () => {
    expect(formatRoR(8.21)).toBe("8.2 °C/min");
    expect(formatRoR(null)).toBe("— °C/min");
  });

  it("formats confidence to two decimals", () => {
    expect(formatConfidence(0.823)).toBe("0.82");
    expect(formatConfidence(null)).toBe("—");
  });

  it("labels every phase (operator-facing truth)", () => {
    expect(PHASE_LABEL.development).toBe("DEVELOPMENT");
    expect(PHASE_LABEL.roasting_pre_first_crack).toBe("ROASTING");
    expect(PHASE_LABEL.operator_recovery_required).toBe("RECOVERY REQUIRED");
  });

  it("maps phases to roast accent tokens, neutral for idle/terminal", () => {
    expect(phaseAccentVar("preheating")).toContain("--roast-phase-preheat");
    expect(phaseAccentVar("faulted")).toContain("--roast-fault");
    expect(phaseAccentVar("complete")).toBeNull();
    expect(phaseAccentVar(null)).toBeNull();
  });

  it("flags pre-first-crack phases for the heat/fan read-out gate (#318)", () => {
    // The read-out-vs-interactive gate keys on the SERVER phase only.
    expect(isPreFirstCrackPhase("preheating")).toBe(true);
    expect(isPreFirstCrackPhase("roasting_pre_first_crack")).toBe(true);
    const notPreFc: (RoastPhase | null)[] = [
      "development",
      "cooling",
      "complete",
      "faulted",
      "operator_recovery_required",
      "idle",
      "starting",
      null,
    ];
    for (const phase of notPreFc) {
      expect(isPreFirstCrackPhase(phase)).toBe(false);
    }
  });
});
