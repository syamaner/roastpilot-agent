import { describe, expect, it } from "vitest";

import { isInChargeWindow } from "./chargeWindow";

const BAND = { minC: 170, maxC: 200 };

describe("isInChargeWindow", () => {
  it("is true only when preheating AND bean within the band", () => {
    expect(isInChargeWindow("preheating", 185, BAND)).toBe(true);
  });

  it("is false below the band (not yet at charge)", () => {
    expect(isInChargeWindow("preheating", 120, BAND)).toBe(false);
  });

  it("is false above the band (over-preheat)", () => {
    expect(isInChargeWindow("preheating", 210, BAND)).toBe(false);
  });

  it("is false in any non-preheating phase even when the bean is in range", () => {
    expect(isInChargeWindow("roasting_pre_first_crack", 185, BAND)).toBe(false);
    expect(isInChargeWindow("development", 185, BAND)).toBe(false);
    expect(isInChargeWindow("cooling", 185, BAND)).toBe(false);
    expect(isInChargeWindow(null, 185, BAND)).toBe(false);
  });

  it("is false without a hydrated band or a finite bean reading", () => {
    expect(isInChargeWindow("preheating", 185, null)).toBe(false);
    expect(isInChargeWindow("preheating", null, BAND)).toBe(false);
    expect(isInChargeWindow("preheating", Number.NaN, BAND)).toBe(false);
  });

  it("is inclusive of the band edges", () => {
    expect(isInChargeWindow("preheating", 170, BAND)).toBe(true);
    expect(isInChargeWindow("preheating", 200, BAND)).toBe(true);
  });
});
