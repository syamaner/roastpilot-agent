/**
 * ConfigPage (#419) — view skeleton + save model.
 *
 * Tests assert interaction, not just render:
 *  1. Loading + error states while GET /api/config is in-flight or fails.
 *  2. Category rail switches the content pane.
 *  3. Editing an editable field marks the category as dirty (dirty dot visible).
 *  4. Save bar appears when dirty; disappears after discard.
 *  5. Save flow: mutation fired with correct edit body (excludes safety).
 *  6. Discard resets values to the saved baseline.
 *  7. Reset-to-default button restores a field's default value.
 *  8. Read-only fields (safety, hardware-pinned) render disabled controls.
 */

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AppConfigSnapshot, ConfigFieldMeta } from "@/lib/types";
import { ConfigPage } from "./ConfigPage";

// ---------------------------------------------------------------------------
// Snapshot factory
// ---------------------------------------------------------------------------

function makeFieldMeta(overrides?: Partial<ConfigFieldMeta>): ConfigFieldMeta {
  return {
    saved_value: null,
    effective_value: null,
    default_value: null,
    env_var: null,
    env_overridden: false,
    read_only: false,
    description: "",
    ...overrides,
  };
}

/** Minimal AppConfigSnapshot covering the fields exercised by the tests. */
function makeSnapshot(overrides?: {
  model_slug?: string;
  pre_fc_heat?: number;
  trim_enabled?: boolean;
  max_bean_temp?: number;
}): AppConfigSnapshot {
  const o = { model_slug: "openai/gpt-4o", pre_fc_heat: 100, trim_enabled: true, max_bean_temp: 230, ...overrides };
  return {
    controller: {
      tick_interval_seconds: makeFieldMeta({ effective_value: 1.0, default_value: 1.0, read_only: true }),
      pre_fc_heat_target_percent: makeFieldMeta({ effective_value: o.pre_fc_heat, default_value: 100 }),
      pre_fc_fan_target_percent: makeFieldMeta({ effective_value: 30, default_value: 30 }),
      late_maillard_trim_enabled: makeFieldMeta({ effective_value: o.trim_enabled, default_value: true }),
      late_maillard_trim_heat_percent: makeFieldMeta({ effective_value: 65, default_value: 65 }),
      late_maillard_trim_window_fc_eta_seconds: makeFieldMeta({ effective_value: 60.0, default_value: 60.0 }),
      late_maillard_trim_min_bean_temp_c: makeFieldMeta({ effective_value: 155.0, default_value: 155.0 }),
      late_maillard_trim_adaptive_depth_enabled: makeFieldMeta({ effective_value: false, default_value: false }),
      late_maillard_trim_base_trim: makeFieldMeta({ effective_value: 65, default_value: 65 }),
      late_maillard_trim_k_ror: makeFieldMeta({ effective_value: 1.5, default_value: 1.5 }),
      late_maillard_trim_k_eta: makeFieldMeta({ effective_value: 0.2, default_value: 0.2 }),
      late_maillard_trim_ror_ref: makeFieldMeta({ effective_value: 8.0, default_value: 8.0 }),
      late_maillard_trim_eta_ref: makeFieldMeta({ effective_value: 60.0, default_value: 60.0 }),
      late_maillard_trim_min_trim: makeFieldMeta({ effective_value: 45, default_value: 45 }),
      late_maillard_trim_max_trim: makeFieldMeta({ effective_value: 75, default_value: 75 }),
    },
    advisor: {
      model_slug: makeFieldMeta({ effective_value: o.model_slug, default_value: "openai/gpt-4o" }),
      prompt_version: makeFieldMeta({ effective_value: "c3", default_value: "c3" }),
      provider: makeFieldMeta({ effective_value: "openai_compatible", default_value: "openai_compatible" }),
      provider_base_url: makeFieldMeta({ effective_value: "https://openrouter.ai/api/v1", default_value: "https://openrouter.ai/api/v1" }),
      api_key_env: makeFieldMeta({ effective_value: "OPENROUTER_API_KEY", default_value: "OPENROUTER_API_KEY", read_only: true }),
      timeout_seconds: makeFieldMeta({ effective_value: 10.0, default_value: 10.0 }),
      temperature: makeFieldMeta({ effective_value: 0.0, default_value: 0.0 }),
    },
    safety: {
      max_bean_temp_c: makeFieldMeta({ effective_value: o.max_bean_temp, default_value: 230, read_only: true }),
      max_env_temp_c: makeFieldMeta({ effective_value: 240, default_value: 240, read_only: true }),
      pre_t0_max_bean_temp_c: makeFieldMeta({ effective_value: 200, default_value: 200, read_only: true }),
      overrun_safe_fan_percent: makeFieldMeta({ effective_value: 100, default_value: 100, read_only: true }),
      pre_t0_overrun_severity: makeFieldMeta({ effective_value: "recovery", default_value: "recovery", read_only: true }),
      min_seconds_between_commands: makeFieldMeta({ effective_value: 2.0, default_value: 2.0, read_only: true }),
      max_consecutive_mcp_failures: makeFieldMeta({ effective_value: 3, default_value: 3, read_only: true }),
      max_consecutive_advisor_failures: makeFieldMeta({ effective_value: 3, default_value: 3, read_only: true }),
      bitter_ceiling_temp_c: makeFieldMeta({ effective_value: 196, default_value: 196, read_only: true }),
      emergency_drop_temp_c: makeFieldMeta({ effective_value: 198, default_value: 198, read_only: true }),
    },
  };
}

// ---------------------------------------------------------------------------
// Mock api.config + api.saveConfig
// ---------------------------------------------------------------------------

const configMock = vi.hoisted(() => vi.fn<[], Promise<AppConfigSnapshot>>());
const saveConfigMock = vi.hoisted(() => vi.fn<[unknown], Promise<AppConfigSnapshot>>());

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: {
      ...actual.api,
      config: configMock,
      saveConfig: saveConfigMock,
    },
  };
});

// ---------------------------------------------------------------------------
// Render helper
// ---------------------------------------------------------------------------

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <ConfigPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { client };
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

beforeEach(() => {
  configMock.mockResolvedValue(makeSnapshot());
  saveConfigMock.mockResolvedValue(makeSnapshot());
});

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("ConfigPage — loading / error states", () => {
  it("shows the loading placeholder while the config query is pending", () => {
    // Never resolves
    configMock.mockReturnValue(new Promise(() => undefined));
    renderPage();
    expect(screen.getByTestId("config-loading")).toBeInTheDocument();
    expect(screen.queryByTestId("config-layout")).toBeNull();
  });

  it("shows an error banner when GET /api/config fails", async () => {
    configMock.mockRejectedValue(new Error("Network error"));
    renderPage();
    await waitFor(() =>
      expect(screen.getByTestId("config-error")).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("config-layout")).toBeNull();
  });

  it("renders the two-pane layout once config loads", async () => {
    renderPage();
    await waitFor(() =>
      expect(screen.getByTestId("config-layout")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("config-rail")).toBeInTheDocument();
  });
});

describe("ConfigPage — category rail", () => {
  it("renders a rail item for every category", async () => {
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    // The four M1 categories
    expect(screen.getByTestId("rail-item-Advisor")).toBeInTheDocument();
    expect(screen.getByTestId("rail-item-Pre-FC Control")).toBeInTheDocument();
    expect(screen.getByTestId("rail-item-Late-Maillard Trim")).toBeInTheDocument();
    expect(screen.getByTestId("rail-item-Safety")).toBeInTheDocument();
  });

  it("switches the content pane when a rail item is clicked", async () => {
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    // Default: Advisor pane is visible
    expect(screen.getByTestId("config-pane-Advisor")).toBeInTheDocument();
    // Click Pre-FC Control
    fireEvent.click(screen.getByTestId("rail-item-Pre-FC Control"));
    expect(screen.getByTestId("config-pane-Pre-FC Control")).toBeInTheDocument();
    expect(screen.queryByTestId("config-pane-Advisor")).toBeNull();
  });

  it("shows a dirty dot on a category when one of its fields is changed", async () => {
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    // No dirty dot on Advisor initially
    expect(screen.queryByTestId("rail-dirty-Advisor")).toBeNull();
    // Change the model slug field (text input in Advisor)
    const modelInput = screen.getByTestId("config-field-advisor.model_slug").querySelector("input");
    expect(modelInput).not.toBeNull();
    fireEvent.change(modelInput!, { target: { value: "anthropic/claude-3-haiku" } });
    // Dirty dot appears
    expect(screen.getByTestId("rail-dirty-Advisor")).toBeInTheDocument();
  });
});

describe("ConfigPage — save model", () => {
  it("shows the save bar when a field is changed", async () => {
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    expect(screen.queryByTestId("config-save-bar")).toBeNull();
    const modelInput = screen.getByTestId("config-field-advisor.model_slug").querySelector("input");
    fireEvent.change(modelInput!, { target: { value: "anthropic/claude-3-5-sonnet" } });
    expect(screen.getByTestId("config-save-bar")).toBeInTheDocument();
  });

  it("hides the save bar after Discard", async () => {
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    const modelInput = screen.getByTestId("config-field-advisor.model_slug").querySelector("input");
    fireEvent.change(modelInput!, { target: { value: "anthropic/claude-3-5-sonnet" } });
    expect(screen.getByTestId("config-save-bar")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("config-discard-btn"));
    expect(screen.queryByTestId("config-save-bar")).toBeNull();
    // Field reverted to original effective value
    expect(modelInput!.value).toBe("openai/gpt-4o");
  });

  it("calls PUT /api/config with the advisor section when an advisor field is dirty", async () => {
    saveConfigMock.mockResolvedValue(makeSnapshot({ model_slug: "anthropic/claude-3-5-sonnet" }));
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    const modelInput = screen.getByTestId("config-field-advisor.model_slug").querySelector("input");
    fireEvent.change(modelInput!, { target: { value: "anthropic/claude-3-5-sonnet" } });
    fireEvent.click(screen.getByTestId("config-save-btn"));
    await waitFor(() => expect(saveConfigMock).toHaveBeenCalledTimes(1));
    const body = saveConfigMock.mock.calls[0]![0] as Record<string, unknown>;
    expect(body).toHaveProperty("advisor");
    // Safety must NOT be in the body
    expect(body).not.toHaveProperty("safety");
  });

  it("does not send safety fields in the PUT body", async () => {
    // Safety fields are read-only: navigating to Safety, changing nothing,
    // calling save must produce an empty body (or no call at all).
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    // No dirty fields → save bar not present → no PUT possible
    expect(screen.queryByTestId("config-save-bar")).toBeNull();
    expect(saveConfigMock).not.toHaveBeenCalled();
  });
});

describe("ConfigPage — field controls", () => {
  it("renders the pre-FC heat field as a number input", async () => {
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    fireEvent.click(screen.getByTestId("rail-item-Pre-FC Control"));
    await waitFor(() => screen.getByTestId("config-pane-Pre-FC Control"));
    const field = screen.getByTestId("config-field-controller.pre_fc_heat_target_percent");
    expect(field.querySelector("input[type='number']")).not.toBeNull();
  });

  it("renders the late-maillard trim enabled field as a toggle", async () => {
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    fireEvent.click(screen.getByTestId("rail-item-Late-Maillard Trim"));
    await waitFor(() => screen.getByTestId("config-pane-Late-Maillard Trim"));
    const toggle = screen.getByTestId("toggle-controller.late_maillard_trim_enabled");
    expect(toggle).toBeInTheDocument();
    expect(toggle).toHaveAttribute("aria-checked", "true");
    // Clicking the toggle changes its value
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-checked", "false");
  });

  it("renders the api_key_env field as a masked read-only display", async () => {
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    const masked = screen.getByTestId("masked-advisor.api_key_env");
    expect(masked).toBeInTheDocument();
    expect(masked).toHaveAttribute("aria-disabled", "true");
  });

  it("shows Reset to default button only when a field differs from its default, and clicking it restores the default", async () => {
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    // Initially at default (effective_value = default_value = "openai/gpt-4o") → no reset
    expect(screen.queryByTestId("reset-advisor.model_slug")).toBeNull();
    const modelInput = screen.getByTestId("config-field-advisor.model_slug").querySelector("input");
    fireEvent.change(modelInput!, { target: { value: "anthropic/claude-3-haiku" } });
    // Differs from default → reset button appears
    const resetBtn = screen.getByTestId("reset-advisor.model_slug");
    expect(resetBtn).toBeInTheDocument();
    // Clicking reset restores meta.default_value ("openai/gpt-4o")
    fireEvent.click(resetBtn);
    expect(modelInput!.value).toBe("openai/gpt-4o");
    // Save bar disappears: field is now equal to its saved (= effective) value
    expect(screen.queryByTestId("config-save-bar")).toBeNull();
    // Reset button gone again
    expect(screen.queryByTestId("reset-advisor.model_slug")).toBeNull();
  });

  it("renders safety fields as disabled with the Guarded chip", async () => {
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    fireEvent.click(screen.getByTestId("rail-item-Safety"));
    await waitFor(() => screen.getByTestId("config-pane-Safety"));
    const maxBeanField = screen.getByTestId("config-field-safety.max_bean_temp_c");
    const input = maxBeanField.querySelector("input");
    expect(input).toHaveAttribute("disabled");
    // Guarded chip
    expect(maxBeanField.textContent).toMatch(/Guarded/);
  });
});
