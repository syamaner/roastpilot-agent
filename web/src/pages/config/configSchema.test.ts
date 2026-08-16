import { describe, expect, it } from "vitest";

import { CONFIG_FIELD_MAP } from "./configSchema";

describe("late-Maillard trim schema", () => {
  it("explains the qualified post-FC heat-cap coupling", () => {
    const field = CONFIG_FIELD_MAP["controller.late_maillard_trim_heat_percent"];

    expect(field).toBeDefined();
    expect(field?.hint).toContain("post-FC RoR loop is enabled");
    expect(field?.hint).toContain("actual actuated heat at FC sets the D88/base cap");
    expect(field?.hint).toContain("recovery cap only when recovery is enabled");
    expect(field?.hint).not.toContain("actual actuated heat at FC sets base/recovery caps");
    expect(field?.hint).toContain("lower bean pre_fc_heat may bind");
    expect(field?.hint).toContain(
      "confirm the current saved/effective loop and recovery settings on this Config page",
    );
    expect(field?.hint).not.toContain("agent startup readout labelled POST-FC RoR LOOP");
  });
});
