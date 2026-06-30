/**
 * ConfigFieldRow — env-override badge tests (#419, slice 3b).
 *
 * Asserts real behaviour:
 *  1. Badge absent when env_overridden=false.
 *  2. Badge present when env_overridden=true; shows the envVar name and note.
 *  3. Control is disabled when env_overridden=true (saved value won't apply).
 *  4. Control is NOT disabled by the badge alone when env_overridden=false.
 *  5. Badge absent when env_overridden=true but fieldDef.envVar is null
 *     (masked/api-key fields that are never env-injected).
 *  6. Guarded chip and env badge can coexist (safety field with env override).
 *  7. Reset button absent when env_overridden=true (field is effectively read-only).
 */

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ConfigFieldMeta } from "@/lib/types";
import type { ConfigFieldDef } from "./configSchema";
import { ConfigFieldRow } from "./ConfigFieldRow";

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

function makeFieldMeta(overrides?: Partial<ConfigFieldMeta>): ConfigFieldMeta {
  return {
    saved_value: 100,
    effective_value: 100,
    default: 100,
    env_overridden: false,
    read_only: false,
    description: "Test field description",
    ...overrides,
  };
}

const NUMBER_FIELD: ConfigFieldDef = {
  key: "controller.pre_fc_heat_target_percent",
  label: "Pre-FC heat",
  hint: "Heat level held from charge to first crack.",
  type: "number",
  unit: "%",
  min: 0,
  max: 100,
  step: 1,
  envVar: "ROASTPILOT_CONTROLLER__PRE_FIRST_CRACK_LEVERS__HEAT_TARGET_PERCENT",
  editKey: "pre_first_crack_levers.heat_target_percent",
  category: "Pre-FC Control",
  readOnlyStatic: false,
};

const MASKED_FIELD: ConfigFieldDef = {
  key: "advisor.api_key_env",
  label: "API key env-var",
  hint: "The env var that holds the advisor API key.",
  type: "masked",
  envVar: null,
  editKey: null,
  category: "Advisor",
  readOnlyStatic: true,
};

const SAFETY_FIELD: ConfigFieldDef = {
  key: "safety.max_bean_temp_c",
  label: "Bean temp ceiling",
  hint: "Hard bean-temperature ceiling (°C).",
  type: "number",
  unit: "°C",
  envVar: "ROASTPILOT_SAFETY__MAX_BEAN_TEMP_C",
  editKey: null,
  category: "Safety",
  readOnlyStatic: true,
};

// ---------------------------------------------------------------------------
// Helper
// ---------------------------------------------------------------------------

function renderRow(fieldDef: ConfigFieldDef, meta: ConfigFieldMeta, value: unknown = 100) {
  render(
    <ConfigFieldRow
      fieldDef={fieldDef}
      meta={meta}
      value={value}
      isLast={false}
      onChange={vi.fn()}
      onReset={vi.fn()}
    />,
  );
}

afterEach(() => {
  cleanup();
});

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("ConfigFieldRow — env-override badge", () => {
  it("badge is absent when env_overridden=false", () => {
    renderRow(NUMBER_FIELD, makeFieldMeta({ env_overridden: false }));
    expect(screen.queryByTestId("env-override-badge")).toBeNull();
  });

  it("badge is present when env_overridden=true and fieldDef.envVar is set", () => {
    renderRow(NUMBER_FIELD, makeFieldMeta({ env_overridden: true }));
    expect(screen.getByTestId("env-override-badge")).toBeInTheDocument();
  });

  it("badge shows the envVar name from the static schema", () => {
    renderRow(NUMBER_FIELD, makeFieldMeta({ env_overridden: true }));
    expect(screen.getByTestId("env-override-badge"))
      .toHaveTextContent(NUMBER_FIELD.envVar!);
  });

  it("badge shows the 'Overridden by env' label and the note about saved value", () => {
    renderRow(NUMBER_FIELD, makeFieldMeta({ env_overridden: true }));
    const badge = screen.getByTestId("env-override-badge");
    expect(badge).toHaveTextContent("Overridden by env");
    expect(badge).toHaveTextContent("Saved value won't take effect while this env var is set.");
  });

  it("control is disabled when env_overridden=true", () => {
    renderRow(NUMBER_FIELD, makeFieldMeta({ env_overridden: true }), 80);
    const input = screen.getByLabelText(NUMBER_FIELD.label);
    expect(input).toBeDisabled();
  });

  it("control is NOT disabled by the badge alone when env_overridden=false", () => {
    renderRow(NUMBER_FIELD, makeFieldMeta({ env_overridden: false }));
    const input = screen.getByLabelText(NUMBER_FIELD.label);
    expect(input).not.toBeDisabled();
  });

  it("badge is absent when env_overridden=true but fieldDef.envVar is null", () => {
    // api_key_env has envVar=null — never env-injected; badge must not render.
    renderRow(MASKED_FIELD, makeFieldMeta({ env_overridden: true, read_only: true }), "OPENROUTER_API_KEY");
    expect(screen.queryByTestId("env-override-badge")).toBeNull();
  });

  it("Guarded chip and env badge both render for a safety field with env_overridden=true", () => {
    renderRow(SAFETY_FIELD, makeFieldMeta({ env_overridden: true, read_only: true }), 230);
    expect(screen.getByText("Guarded")).toBeInTheDocument();
    expect(screen.getByTestId("env-override-badge")).toBeInTheDocument();
  });

  it("reset button is absent when env_overridden=true (field effectively read-only)", () => {
    // Value differs from default — reset would normally show, but not when overridden.
    renderRow(NUMBER_FIELD, makeFieldMeta({ env_overridden: true, default: 100 }), 80);
    expect(screen.queryByTestId(`reset-${NUMBER_FIELD.key}`)).toBeNull();
  });
});
