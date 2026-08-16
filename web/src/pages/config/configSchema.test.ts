import { describe, expect, it } from "vitest";

import { CONFIG_FIELD_MAP } from "./configSchema";

describe("late-Maillard trim schema", () => {
  it("explains the qualified post-FC heat-cap coupling", () => {
    const field = CONFIG_FIELD_MAP["controller.late_maillard_trim_heat_percent"];

    expect(field).toBeDefined();
    expect(field?.hint).toContain("post-FC RoR loop is enabled");
    expect(field?.hint).toContain("actual heat at FC sets base/recovery caps");
    expect(field?.hint).toContain("lower bean pre_fc_heat may bind");
    expect(field?.hint).toContain(
      "Confirm effective loop state in the agent startup readout labelled POST-FC RoR LOOP",
    );
  });
});
