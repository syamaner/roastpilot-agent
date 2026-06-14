import { describe, expect, it } from "vitest";

import { chargeCueState } from "./chargeWindow";

const BAND = { minC: 170, maxC: 200 };

describe("chargeCueState", () => {
  it("is in_window when preheating AND bean within the band", () => {
    expect(chargeCueState("preheating", 185, BAND)).toBe("in_window");
  });

  it("is hidden below the band (not yet at charge temperature)", () => {
    expect(chargeCueState("preheating", 120, BAND)).toBe("hidden");
    expect(chargeCueState("preheating", 169.9, BAND)).toBe("hidden");
  });

  it("is over_window above the band while still preheating (no silent over-preheat, #211)", () => {
    expect(chargeCueState("preheating", 201, BAND)).toBe("over_window");
    // A tight band (170–180) keeps escalating up to the pre-T0 safety bound.
    expect(chargeCueState("preheating", 195, { minC: 170, maxC: 180 })).toBe("over_window");
  });

  it("is hidden in any non-preheating phase even when the bean is in/over range", () => {
    expect(chargeCueState("roasting_pre_first_crack", 185, BAND)).toBe("hidden");
    expect(chargeCueState("development", 185, BAND)).toBe("hidden");
    expect(chargeCueState("cooling", 185, BAND)).toBe("hidden");
    expect(chargeCueState("roasting_pre_first_crack", 210, BAND)).toBe("hidden");
    expect(chargeCueState(null, 185, BAND)).toBe("hidden");
  });

  it("is hidden without a hydrated band or a finite bean reading", () => {
    expect(chargeCueState("preheating", 185, null)).toBe("hidden");
    expect(chargeCueState("preheating", null, BAND)).toBe("hidden");
    expect(chargeCueState("preheating", Number.NaN, BAND)).toBe("hidden");
  });

  it("treats the band edges as in_window (inclusive) and escalates just above", () => {
    expect(chargeCueState("preheating", 170, BAND)).toBe("in_window");
    expect(chargeCueState("preheating", 200, BAND)).toBe("in_window");
    expect(chargeCueState("preheating", 200.1, BAND)).toBe("over_window");
  });
});
