import { describe, expect, it } from "vitest";

import { CONFIG_FIELD_MAP } from "./configSchema";

describe("late-Maillard trim schema", () => {
  it("explains the qualified post-FC heat-cap coupling", () => {
    const field = CONFIG_FIELD_MAP["controller.late_maillard_trim_heat_percent"];

    expect(field).toBeDefined();
    expect(field?.hint).toContain("post-FC loop is enabled");
    expect(field?.hint).toContain("actual pre-FC heat at FC is the D88/D96 basis");
    expect(field?.hint).toContain("lower bean pre_fc_heat can bind");
  });
});
