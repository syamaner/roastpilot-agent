/**
 * ConfigFieldRow — env-override badge tests (#419, slice 3b).
 *
 * Asserts real behaviour:
 *  1. Badge absent when env_overridden=false.
 *  2. Badge present when env_overridden=true; shows the envVar name and note.
 *  3. Control is NOT disabled by env_overridden — env_overridden and read_only are
 *     separate server flags; PUT /api/config accepts env-overridden non-safety fields.
 *  4. Badge absent when env_overridden=true but fieldDef.envVar is null
 *     (masked/api-key fields that are never env-injected).
 *  5. Guarded chip and env badge can coexist (safety field with env override).
 *  6. Reset button still available when env_overridden=true and value≠default.
 *  7. Field row carries responsive grid classes so it collapses to single-column
 *     at <900px (control stacks below description).
 */

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ConfigFieldMeta } from "@/lib/types";
import type { ConfigFieldDef } from "./configSchema";
import { ConfigFieldRow } from "./ConfigFieldRow";

// ---------------------------------------------------------------------------
// Mock useDevices — needed only by the deviceSelect allowClear test (#439).
// ---------------------------------------------------------------------------

vi.mock("@/hooks/queries", () => ({
  useDevices: vi.fn(() => ({
    data: {
      serial: [{ value: "/dev/cu.usbserial-ABC", label: "/dev/cu.usbserial-ABC", note: "Hottop · FT232R" }],
      serial_error: null,
      audio_input: [],
      audio_input_error: null,
    },
    isPending: false,
    isRefetching: false,
    isError: false,
    error: null,
    refetch: vi.fn(),
  })),
  useConfig: vi.fn(),
  useSaveConfig: vi.fn(),
}));

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

// mcp_device boolean field — tri-state control (#439).
const NULLABLE_BOOL_FIELD: ConfigFieldDef = {
  key: "mcp_device.recording_enabled",
  label: "Recording enabled",
  hint: "Whether the MCP audio recorder is active.",
  type: "boolean",
  envVar: "ROASTPILOT_MCP_DEVICE__RECORDING_ENABLED",
  editKey: "recording_enabled",
  category: "Audio",
  readOnlyStatic: false,
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

  it("control is NOT disabled by env_overridden alone — field stays editable", () => {
    // env_overridden and read_only are separate server flags. The backend accepts
    // PUT /api/config for env-overridden non-safety fields (read_only=false). The
    // badge is informational; the operator can still save a value that takes effect
    // once the env var is removed.
    renderRow(NUMBER_FIELD, makeFieldMeta({ env_overridden: true }), 80);
    const input = screen.getByLabelText(NUMBER_FIELD.label);
    expect(input).not.toBeDisabled();
  });

  it("control is NOT disabled when env_overridden=false either (sanity)", () => {
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

  it("reset button is still available when env_overridden=true and value differs from default", () => {
    // The field is editable while env-overridden; the operator can still reset to
    // default (the saved value takes effect once the env var is removed).
    renderRow(NUMBER_FIELD, makeFieldMeta({ env_overridden: true, default: 100 }), 80);
    expect(screen.getByTestId(`reset-${NUMBER_FIELD.key}`)).toBeInTheDocument();
  });
});

describe("ConfigFieldRow — NullableBooleanControl (mcp_device boolean, #439)", () => {
  it("renders a tri-state radio group with Inherit / On / Off segments", () => {
    renderRow(NULLABLE_BOOL_FIELD, makeFieldMeta({ effective_value: null, default: null }), null);
    // The three segments must be present as radio buttons.
    expect(screen.getByRole("radio", { name: "Inherit" })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "On" })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Off" })).toBeInTheDocument();
  });

  it("Inherit segment is checked when value is null", () => {
    renderRow(NULLABLE_BOOL_FIELD, makeFieldMeta({ effective_value: null, default: null }), null);
    const inheritBtn = screen.getByRole("radio", { name: "Inherit" });
    expect(inheritBtn).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("radio", { name: "On" })).toHaveAttribute("aria-checked", "false");
    expect(screen.getByRole("radio", { name: "Off" })).toHaveAttribute("aria-checked", "false");
  });

  it("On segment is checked when value is true", () => {
    renderRow(NULLABLE_BOOL_FIELD, makeFieldMeta({ effective_value: true, default: null }), true);
    expect(screen.getByRole("radio", { name: "On" })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("radio", { name: "Inherit" })).toHaveAttribute("aria-checked", "false");
  });

  it("Off segment is checked when value is false", () => {
    renderRow(NULLABLE_BOOL_FIELD, makeFieldMeta({ effective_value: false, default: null }), false);
    expect(screen.getByRole("radio", { name: "Off" })).toHaveAttribute("aria-checked", "true");
  });

  it("clicking Inherit calls onChange with null", () => {
    const onChange = vi.fn();
    render(
      <ConfigFieldRow
        fieldDef={NULLABLE_BOOL_FIELD}
        meta={makeFieldMeta({ effective_value: true, default: null })}
        value={true}
        isLast={false}
        onChange={onChange}
        onReset={vi.fn()}
      />,
    );
    screen.getByRole("radio", { name: "Inherit" }).click();
    expect(onChange).toHaveBeenCalledWith(null);
  });

  it("clicking On calls onChange with true", () => {
    const onChange = vi.fn();
    render(
      <ConfigFieldRow
        fieldDef={NULLABLE_BOOL_FIELD}
        meta={makeFieldMeta({ effective_value: null, default: null })}
        value={null}
        isLast={false}
        onChange={onChange}
        onReset={vi.fn()}
      />,
    );
    screen.getByRole("radio", { name: "On" }).click();
    expect(onChange).toHaveBeenCalledWith(true);
  });

  it("clicking Off calls onChange with false", () => {
    const onChange = vi.fn();
    render(
      <ConfigFieldRow
        fieldDef={NULLABLE_BOOL_FIELD}
        meta={makeFieldMeta({ effective_value: null, default: null })}
        value={null}
        isLast={false}
        onChange={onChange}
        onReset={vi.fn()}
      />,
    );
    screen.getByRole("radio", { name: "Off" }).click();
    expect(onChange).toHaveBeenCalledWith(false);
  });

  it("non-mcp_device boolean field still uses the two-state toggle (not tri-state)", () => {
    const TRIM_BOOL_FIELD: ConfigFieldDef = {
      key: "controller.late_maillard_trim_enabled",
      label: "Trim enabled",
      hint: "Enable the anticipatory heat trim.",
      type: "boolean",
      envVar: "ROASTPILOT_CONTROLLER__PRE_FIRST_CRACK_LEVERS__LATE_MAILLARD_TRIM__ENABLED",
      editKey: "pre_first_crack_levers.late_maillard_trim.enabled",
      category: "Late-Maillard Trim",
      readOnlyStatic: false,
    };
    renderRow(TRIM_BOOL_FIELD, makeFieldMeta({ effective_value: true, default: true }), true);
    // Two-state toggle: role="switch", no radio group.
    expect(screen.getByRole("switch")).toBeInTheDocument();
    expect(screen.queryByRole("radiogroup")).toBeNull();
  });
});

describe("ConfigFieldRow — responsive layout", () => {
  it("field row carries the two-column grid class and the <900px single-column override", () => {
    // Asserts the inline-style→Tailwind migration: the row must use Tailwind classes
    // so the max-[900px]:grid-cols-1 variant can collapse the layout at narrow widths
    // (control stacks below description, per design handoff S4 responsive spec).
    renderRow(NUMBER_FIELD, makeFieldMeta());
    const row = screen.getByTestId(`config-field-${NUMBER_FIELD.key}`);
    expect(row.className).toContain("grid-cols-[minmax(0,1fr)_384px]");
    expect(row.className).toContain("max-[900px]:grid-cols-1");
  });
});

// ---------------------------------------------------------------------------
// deviceSelect allowClear adapter (#439 — bug fix #2)
// ---------------------------------------------------------------------------

// mcp_device serial device field — the ConfigFieldRow adapter passes allowClear
// to DeviceSelect and converts "" → null so clearing calls onChange(null).
const SERIAL_PORT_FIELD: ConfigFieldDef = {
  key: "mcp_device.serial_port",
  label: "Serial port",
  hint: "USB serial port the Hottop is connected to.",
  type: "deviceSelect",
  deviceSource: "serial",
  envVar: "ROASTPILOT_MCP_DEVICE__SERIAL_PORT",
  editKey: "serial_port",
  category: "Hardware",
  readOnlyStatic: false,
};

describe("ConfigFieldRow — deviceSelect allowClear → onChange(null) (#439)", () => {
  it("selecting 'Inherit from yaml' calls parent onChange with null, not empty string", async () => {
    // ConfigFieldRow passes allowClear to DeviceSelect, which prepends an
    // "Inherit from yaml" option (value = ""). The adapter in ConfigFieldRow
    // converts "" → null so the parent receives null (clear to inherit), not "".
    const onChange = vi.fn();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <ConfigFieldRow
          fieldDef={SERIAL_PORT_FIELD}
          meta={makeFieldMeta({ effective_value: "/dev/cu.usbserial-ABC", default: null })}
          value="/dev/cu.usbserial-ABC"
          isLast={false}
          onChange={onChange}
          onReset={vi.fn()}
        />
      </QueryClientProvider>,
    );

    // Open the popover.
    fireEvent.click(screen.getByTestId("device-select-trigger"));

    // "Inherit from yaml" option must be present (allowClear=true).
    const inheritOption = await waitFor(() =>
      screen.getByTestId("device-option-"),
    );
    expect(inheritOption).toBeInTheDocument();

    // Click it — the adapter must call onChange(null), not onChange("").
    fireEvent.click(inheritOption);
    expect(onChange).toHaveBeenCalledWith(null);
    expect(onChange).not.toHaveBeenCalledWith("");
  });
});
