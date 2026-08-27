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

describe("prompt-version schema (#710 RP-C slice 3, T56)", () => {
  it("adds c12 as the twelfth option, byte-identical to the pre-existing eleven", () => {
    // Pre-existing eleven options (value + label), byte-unchanged by this slice
    // (AC8/C4 — the SPA hand-mirror of advisor.py's _CONTROL_TEACHING_PROMPTS).
    const PRE_EXISTING_ELEVEN: ReadonlyArray<{ value: string; label: string }> = [
      { value: "c1", label: "c1 — original (v1 baseline)" },
      { value: "c2", label: "c2 — post-FC development stretch" },
      { value: "c3", label: "c3 — default (stable, production)" },
      { value: "c4", label: "c4 — experiment" },
      { value: "c5", label: "c5 — experiment" },
      { value: "c6", label: "c6 — experiment (#396 A/B)" },
      { value: "c7", label: "c7 — experiment (#499 pt.2 DTR-pace A/B)" },
      { value: "c8", label: "c8 — experiment (#559 D96 pace/bottom-edge/fan A/B)" },
      { value: "c9", label: "c9 — experiment (#567 reference-roast A/B)" },
      { value: "c10", label: "c10 — experiment (#705 read-provided-DTR A/B)" },
      { value: "c11", label: "c11 — experiment (#709 ambient-aware fan A/B)" },
    ];

    const field = CONFIG_FIELD_MAP["advisor.prompt_version"];
    expect(field).toBeDefined();
    const options = field?.options ?? [];

    expect(options).toHaveLength(12);
    expect(options.slice(0, 11)).toEqual(PRE_EXISTING_ELEVEN);
    expect(options[11]).toEqual({
      value: "c12",
      label: "c12 — experiment (#710 RP-C joint-window A/B)",
    });
  });
});
