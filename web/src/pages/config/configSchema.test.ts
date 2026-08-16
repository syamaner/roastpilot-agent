import { describe, expect, it } from "vitest";

import { CONFIG_FIELD_MAP } from "./configSchema";

describe("late-Maillard trim schema", () => {
  it("explains the qualified post-FC heat-cap coupling", () => {
    const field = CONFIG_FIELD_MAP["controller.late_maillard_trim_heat_percent"];

    expect(field).toBeDefined();
    expect(field?.hint).toContain("When the window is open at first crack");
    expect(field?.hint).toContain("resolved level is also the post-FC base heat cap");
  });
});
