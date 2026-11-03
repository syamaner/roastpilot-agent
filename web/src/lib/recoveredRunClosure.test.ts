import { describe, expect, it } from "vitest";

import { isRecoveredRunEndProjected } from "./recoveredRunClosure";

const CLOSURE_ONLY = {
  state: "closure_only" as const,
  eligible: false,
  enabled_actions: ["end_recovered_run" as const],
  closure_only_available: false,
};

describe("isRecoveredRunEndProjected", () => {
  it("requires matching fresh health and effective current-run End projections", () => {
    expect(isRecoveredRunEndProjected(CLOSURE_ONLY, ["end_recovered_run"], false)).toBe(true);
    expect(isRecoveredRunEndProjected(CLOSURE_ONLY, [], false)).toBe(false);
    expect(
      isRecoveredRunEndProjected(
        { ...CLOSURE_ONLY, enabled_actions: [] },
        ["end_recovered_run"],
        false,
      ),
    ).toBe(false);
    expect(isRecoveredRunEndProjected(CLOSURE_ONLY, ["end_recovered_run"], true)).toBe(false);
  });
});
