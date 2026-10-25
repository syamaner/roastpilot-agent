import { describe, expect, it } from "vitest";

import { verdictBadge, verdictLabel } from "./verdict";

describe("verdictBadge (D15: six verdicts → three badges)", () => {
  it("maps the three advisory verdicts to badges with the enum spelling", () => {
    expect(verdictBadge("allow")).toMatchInlineSnapshot(`
      {
        "label": "ALLOW",
        "tone": "nominal",
      }
    `);
    expect(verdictBadge("clamp")).toMatchInlineSnapshot(`
      {
        "label": "CLAMP",
        "tone": "caution",
      }
    `);
    expect(verdictBadge("reject")).toMatchInlineSnapshot(`
      {
        "label": "REJECT",
        "tone": "fault",
      }
    `);
  });

  it("returns null for the non-badge verdicts (recovery/fault/e-stop)", () => {
    expect(verdictBadge("recovery")).toBeNull();
    expect(verdictBadge("fault")).toBeNull();
    expect(verdictBadge("emergency_stop")).toBeNull();
  });
});

describe("verdictLabel (decision-trace column shows all six)", () => {
  it("uppercases each verdict", () => {
    expect(verdictLabel("allow")).toBe("ALLOW");
    expect(verdictLabel("clamp")).toBe("CLAMP");
    expect(verdictLabel("reject")).toBe("REJECT");
    expect(verdictLabel("recovery")).toBe("RECOVERY");
    expect(verdictLabel("fault")).toBe("FAULT");
    expect(verdictLabel("emergency_stop")).toBe("EMERGENCY STOP");
  });
});
