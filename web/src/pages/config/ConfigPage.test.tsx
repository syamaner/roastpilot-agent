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
 *  9. Dirty edits preserved when a background snapshot refresh arrives.
 * 10. PUT body nesting correct for pre_first_crack_levers and late_maillard_trim.
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
    default: null,
    env_overridden: false,
    read_only: false,
    description: "",
    yaml_value: null,
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
      tick_interval_seconds: makeFieldMeta({ effective_value: 1.0, default: 1.0, read_only: true }),
      pre_fc_heat_target_percent: makeFieldMeta({ effective_value: o.pre_fc_heat, default: 100 }),
      pre_fc_fan_target_percent: makeFieldMeta({ effective_value: 30, default: 30 }),
      late_maillard_trim_enabled: makeFieldMeta({ effective_value: o.trim_enabled, default: true }),
      late_maillard_trim_heat_percent: makeFieldMeta({ effective_value: 65, default: 65 }),
      late_maillard_trim_window_fc_eta_seconds: makeFieldMeta({ effective_value: 60.0, default: 60.0 }),
      late_maillard_trim_min_bean_temp_c: makeFieldMeta({ effective_value: 155.0, default: 155.0 }),
      late_maillard_trim_adaptive_depth_enabled: makeFieldMeta({ effective_value: false, default: false }),
      late_maillard_trim_base_trim: makeFieldMeta({ effective_value: 65, default: 65 }),
      late_maillard_trim_k_ror: makeFieldMeta({ effective_value: 1.5, default: 1.5 }),
      late_maillard_trim_k_eta: makeFieldMeta({ effective_value: 0.2, default: 0.2 }),
      late_maillard_trim_ror_ref: makeFieldMeta({ effective_value: 8.0, default: 8.0 }),
      late_maillard_trim_eta_ref: makeFieldMeta({ effective_value: 60.0, default: 60.0 }),
      late_maillard_trim_min_trim: makeFieldMeta({ effective_value: 45, default: 45 }),
      late_maillard_trim_max_trim: makeFieldMeta({ effective_value: 75, default: 75 }),
      late_maillard_trim_trim_depth_deadband_pp: makeFieldMeta({ effective_value: 2, default: 2 }),
      late_maillard_trim_trim_depth_slew_pp_per_tick: makeFieldMeta({ effective_value: 3, default: 3 }),
    },
    advisor: {
      model_slug: makeFieldMeta({ effective_value: o.model_slug, default: "openai/gpt-4o" }),
      prompt_version: makeFieldMeta({ effective_value: "c3", default: "c3" }),
      provider: makeFieldMeta({ effective_value: "openai_compatible", default: "openai_compatible" }),
      provider_base_url: makeFieldMeta({ effective_value: "https://openrouter.ai/api/v1", default: "https://openrouter.ai/api/v1" }),
      api_key_env: makeFieldMeta({ effective_value: "OPENROUTER_API_KEY", default: "OPENROUTER_API_KEY", read_only: true }),
      timeout_seconds: makeFieldMeta({ effective_value: 10.0, default: 10.0 }),
      temperature: makeFieldMeta({ effective_value: 0.0, default: 0.0 }),
    },
    safety: {
      max_bean_temp_c: makeFieldMeta({ effective_value: o.max_bean_temp, default: 230, read_only: true }),
      max_env_temp_c: makeFieldMeta({ effective_value: 240, default: 240, read_only: true }),
      pre_t0_max_bean_temp_c: makeFieldMeta({ effective_value: 200, default: 200, read_only: true }),
      overrun_safe_fan_percent: makeFieldMeta({ effective_value: 100, default: 100, read_only: true }),
      pre_t0_overrun_severity: makeFieldMeta({ effective_value: "recovery", default: "recovery", read_only: true }),
      min_seconds_between_commands: makeFieldMeta({ effective_value: 2.0, default: 2.0, read_only: true }),
      max_consecutive_mcp_failures: makeFieldMeta({ effective_value: 3, default: 3, read_only: true }),
      max_consecutive_advisor_failures: makeFieldMeta({ effective_value: 3, default: 3, read_only: true }),
      bitter_ceiling_temp_c: makeFieldMeta({ effective_value: 196, default: 196, read_only: true }),
      emergency_drop_temp_c: makeFieldMeta({ effective_value: 198, default: 198, read_only: true }),
    },
    mcp_device: {
      serial_port: makeFieldMeta({ effective_value: null, default: null }),
      roaster_driver: makeFieldMeta({ effective_value: null, default: null }),
      audio_input_device: makeFieldMeta({ effective_value: null, default: null }),
      recording_enabled: makeFieldMeta({ effective_value: null, default: null }),
      recording_autocapture: makeFieldMeta({ effective_value: null, default: null }),
      recording_devices: makeFieldMeta({ effective_value: null, default: null }),
      fc_mode: makeFieldMeta({ effective_value: null, default: null }),
      fc_confidence_threshold: makeFieldMeta({ effective_value: null, default: null }),
      auto_t0_detection_enabled: makeFieldMeta({ effective_value: null, default: null }),
      auto_t0_drop_threshold_c: makeFieldMeta({ effective_value: null, default: null }),
      ambient_mode: makeFieldMeta({ effective_value: null, default: null }),
      ambient_device: makeFieldMeta({ effective_value: null, default: null }),
      ambient_poll_interval_seconds: makeFieldMeta({ effective_value: null, default: null }),
    },
  };
}

// ---------------------------------------------------------------------------
// Mock api.config + api.saveConfig
// ---------------------------------------------------------------------------

const configMock = vi.hoisted(() => vi.fn<() => Promise<AppConfigSnapshot>>());
const saveConfigMock = vi.hoisted(() => vi.fn<(edit: unknown) => Promise<AppConfigSnapshot>>());
const devicesMock = vi.hoisted(() =>
  vi.fn<() => Promise<import("@/lib/types").DevicesSnapshot>>(),
);

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: {
      ...actual.api,
      config: configMock,
      saveConfig: saveConfigMock,
      devices: devicesMock,
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
  devicesMock.mockResolvedValue({
    serial: [{ value: "/dev/ttyUSB0", label: "USB Serial", note: "" }],
    serial_error: null,
    audio_input: [{ value: "USB PnP", label: "USB PnP Sound Device", note: "" }],
    audio_input_error: null,
  });
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
    // All seven categories (reordered in S4: Hardware first, Safety last)
    expect(screen.getByTestId("rail-item-Hardware")).toBeInTheDocument();
    expect(screen.getByTestId("rail-item-Audio")).toBeInTheDocument();
    expect(screen.getByTestId("rail-item-FC-Detection")).toBeInTheDocument();
    expect(screen.getByTestId("rail-item-Advisor")).toBeInTheDocument();
    expect(screen.getByTestId("rail-item-Pre-FC Control")).toBeInTheDocument();
    expect(screen.getByTestId("rail-item-Late-Maillard Trim")).toBeInTheDocument();
    expect(screen.getByTestId("rail-item-Safety")).toBeInTheDocument();
  });

  it("switches the content pane when a rail item is clicked", async () => {
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    // Default: Hardware pane is visible (first in the reordered rail)
    expect(screen.getByTestId("config-pane-Hardware")).toBeInTheDocument();
    // Click Pre-FC Control
    fireEvent.click(screen.getByTestId("rail-item-Pre-FC Control"));
    await waitFor(() => screen.getByTestId("config-pane-Pre-FC Control"));
    expect(screen.queryByTestId("config-pane-Hardware")).toBeNull();
  });

  it("shows a dirty dot on a category when one of its fields is changed", async () => {
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    // Navigate to the Advisor pane to access its fields
    fireEvent.click(screen.getByTestId("rail-item-Advisor"));
    await waitFor(() => screen.getByTestId("config-pane-Advisor"));
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
    // Navigate to Advisor pane (Hardware is the new default)
    fireEvent.click(screen.getByTestId("rail-item-Advisor"));
    await waitFor(() => screen.getByTestId("config-pane-Advisor"));
    expect(screen.queryByTestId("config-save-bar")).toBeNull();
    const modelInput = screen.getByTestId("config-field-advisor.model_slug").querySelector("input");
    fireEvent.change(modelInput!, { target: { value: "anthropic/claude-3-5-sonnet" } });
    expect(screen.getByTestId("config-save-bar")).toBeInTheDocument();
  });

  it("hides the save bar after Discard", async () => {
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    fireEvent.click(screen.getByTestId("rail-item-Advisor"));
    await waitFor(() => screen.getByTestId("config-pane-Advisor"));
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
    fireEvent.click(screen.getByTestId("rail-item-Advisor"));
    await waitFor(() => screen.getByTestId("config-pane-Advisor"));
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

  it("preserves dirty edits when a background snapshot refresh arrives", async () => {
    // Simulate a background refetch delivering a new snapshot reference while
    // the operator has unsaved edits — the INIT effect must be gated behind a
    // dirty check so the edits are not silently clobbered.
    const { client } = renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));

    // Navigate to Advisor and edit model slug to something non-default
    fireEvent.click(screen.getByTestId("rail-item-Advisor"));
    await waitFor(() => screen.getByTestId("config-pane-Advisor"));
    const modelInput = screen.getByTestId("config-field-advisor.model_slug").querySelector("input");
    fireEvent.change(modelInput!, { target: { value: "anthropic/claude-3-5-sonnet" } });
    expect(screen.getByTestId("config-save-bar")).toBeInTheDocument();
    expect(modelInput!.value).toBe("anthropic/claude-3-5-sonnet");

    // Simulate a background snapshot refresh by pushing a new (different-reference)
    // snapshot into the query cache — this triggers a re-render with a new prop.
    client.setQueryData(["config"], makeSnapshot({ model_slug: "openai/gpt-4o" }));

    // After the cache update, the dirty edit must still be in the input
    // (the INIT must have been skipped because the form was dirty).
    await waitFor(() => {
      expect(modelInput!.value).toBe("anthropic/claude-3-5-sonnet");
    });
    expect(screen.getByTestId("config-save-bar")).toBeInTheDocument();
  });

  it("clears the save bar and dirty dot after a successful save, and rebaselines to the response snapshot (#483)", async () => {
    // Before the fix: a successful PUT persisted server-side but the FE never
    // rebaselined, so the save bar and dirty dot stayed on indefinitely.
    saveConfigMock.mockResolvedValue(makeSnapshot({ model_slug: "anthropic/claude-3-5-sonnet" }));
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    fireEvent.click(screen.getByTestId("rail-item-Advisor"));
    await waitFor(() => screen.getByTestId("config-pane-Advisor"));
    const modelInput = screen.getByTestId("config-field-advisor.model_slug").querySelector("input");
    fireEvent.change(modelInput!, { target: { value: "anthropic/claude-3-5-sonnet" } });
    expect(screen.getByTestId("rail-dirty-Advisor")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("config-save-btn"));
    await waitFor(() => expect(saveConfigMock).toHaveBeenCalledTimes(1));

    // Banner and dirty dot clear once the save resolves.
    await waitFor(() => expect(screen.queryByTestId("config-save-bar")).toBeNull());
    expect(screen.queryByTestId("rail-dirty-Advisor")).toBeNull();
    expect(modelInput!.value).toBe("anthropic/claude-3-5-sonnet");
  });

  it("rebaselines to the saved value: editing back to the pre-save value shows dirty again", async () => {
    // Proves the baseline actually moved to the just-saved value, not just
    // that the banner happened to clear. Edit → save → edit back to the
    // ORIGINAL (pre-save) value must be dirty relative to the NEW baseline.
    saveConfigMock.mockResolvedValue(makeSnapshot({ model_slug: "anthropic/claude-3-5-sonnet" }));
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    fireEvent.click(screen.getByTestId("rail-item-Advisor"));
    await waitFor(() => screen.getByTestId("config-pane-Advisor"));
    const modelInput = screen.getByTestId("config-field-advisor.model_slug").querySelector("input");

    fireEvent.change(modelInput!, { target: { value: "anthropic/claude-3-5-sonnet" } });
    fireEvent.click(screen.getByTestId("config-save-btn"));
    await waitFor(() => expect(saveConfigMock).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.queryByTestId("config-save-bar")).toBeNull());

    // Edit back to the value that was the baseline BEFORE this save
    // ("openai/gpt-4o"). If the baseline correctly moved to
    // "anthropic/claude-3-5-sonnet", this must show dirty again.
    fireEvent.change(modelInput!, { target: { value: "openai/gpt-4o" } });
    expect(screen.getByTestId("config-save-bar")).toBeInTheDocument();
    expect(screen.getByTestId("rail-dirty-Advisor")).toBeInTheDocument();
  });

  it("disables the field control and Save/Discard while the save is pending (#483 fix round, Codex finding 1)", async () => {
    // Before the fix: fields stayed enabled during a pending save, so an edit
    // made after clicking Save but before the PUT resolved was silently
    // clobbered by the unconditional post-save INIT. Disabling the controls
    // (and the buttons) while pending makes that edit impossible rather than
    // something to reconcile.
    let resolveSave!: (snapshot: ReturnType<typeof makeSnapshot>) => void;
    saveConfigMock.mockReturnValue(
      new Promise((resolve) => {
        resolveSave = resolve;
      }),
    );
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    fireEvent.click(screen.getByTestId("rail-item-Advisor"));
    await waitFor(() => screen.getByTestId("config-pane-Advisor"));
    const modelInput = screen.getByTestId("config-field-advisor.model_slug").querySelector("input")!;
    fireEvent.change(modelInput, { target: { value: "anthropic/claude-3-5-sonnet" } });

    fireEvent.click(screen.getByTestId("config-save-btn"));
    await waitFor(() => expect(saveConfigMock).toHaveBeenCalledTimes(1));

    // While the PUT is in flight: field disabled, Save/Discard disabled. A
    // real browser refuses keyboard/pointer input on a disabled control —
    // the `disabled` attribute is the gate under test here (jsdom's
    // fireEvent.change bypasses that gate at the DOM level, so it isn't a
    // faithful way to simulate "the operator tried to type"; asserting
    // `disabled` is what actually protects against the mid-save race).
    await waitFor(() => expect(modelInput).toBeDisabled());
    expect(screen.getByTestId("config-save-btn")).toBeDisabled();
    expect(screen.getByTestId("config-discard-btn")).toBeDisabled();

    // Resolve the save — controls re-enable and rebaseline as before.
    resolveSave(makeSnapshot({ model_slug: "anthropic/claude-3-5-sonnet" }));
    await waitFor(() => expect(screen.queryByTestId("config-save-bar")).toBeNull());
    expect(modelInput).not.toBeDisabled();
  });

  it("a stale background refetch resolving after save does not overwrite the rebaselined values (#483 fix round, Codex finding 2)", async () => {
    // Simulates: a background GET /api/config was already in flight when the
    // operator clicked Save; the PUT resolves and rebaselines first, then the
    // stale GET resolves with the OLDER (pre-save) snapshot. Without
    // cancelling the in-flight query in useSaveConfig's onSuccess, that stale
    // response would land in the cache after the PUT's, silently reverting
    // the just-cleared dirty state and displayed value.
    let resolveStaleGet!: (snapshot: ReturnType<typeof makeSnapshot>) => void;
    const staleGetPromise = new Promise<ReturnType<typeof makeSnapshot>>((resolve) => {
      resolveStaleGet = resolve;
    });
    // First GET (initial load) resolves immediately with the baseline snapshot.
    configMock.mockResolvedValueOnce(makeSnapshot());
    // Second GET (the "background refetch" already in flight) hangs until we
    // resolve it by hand, after the save has completed.
    configMock.mockReturnValueOnce(staleGetPromise);
    saveConfigMock.mockResolvedValue(makeSnapshot({ model_slug: "anthropic/claude-3-5-sonnet" }));

    const { client } = renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    fireEvent.click(screen.getByTestId("rail-item-Advisor"));
    await waitFor(() => screen.getByTestId("config-pane-Advisor"));
    const modelInput = screen.getByTestId("config-field-advisor.model_slug").querySelector("input")!;

    // Kick off the "background refetch" that will hang on staleGetPromise, and
    // wait for it to genuinely register as an in-flight fetch on the query
    // (fetchStatus: "fetching") before proceeding — otherwise the save could
    // race ahead of the refetch actually starting and the test would prove
    // nothing.
    void client.refetchQueries({ queryKey: ["config"] });
    await waitFor(() =>
      expect(client.getQueryState(["config"])?.fetchStatus).toBe("fetching"),
    );

    // Operator edits and saves while that refetch is still in flight.
    fireEvent.change(modelInput, { target: { value: "anthropic/claude-3-5-sonnet" } });
    fireEvent.click(screen.getByTestId("config-save-btn"));
    await waitFor(() => expect(saveConfigMock).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.queryByTestId("config-save-bar")).toBeNull());
    expect(modelInput.value).toBe("anthropic/claude-3-5-sonnet");

    // NOW the stale background GET resolves with the OLD pre-save snapshot.
    resolveStaleGet(makeSnapshot({ model_slug: "openai/gpt-4o" }));
    // Give the resolved promise's continuation (and any resulting cache
    // write / re-render) a chance to run before asserting.
    await waitFor(() => expect(client.getQueryState(["config"])?.fetchStatus).toBe("idle"));

    // The query cache itself must still hold the just-saved value — the
    // stale response must never have been written (it was cancelled).
    const cached = client.getQueryData(["config"]) as ReturnType<typeof makeSnapshot>;
    expect(cached.advisor.model_slug.effective_value).toBe("anthropic/claude-3-5-sonnet");

    // The rebaselined value and clean state must survive on screen too.
    await waitFor(() => {
      expect(modelInput.value).toBe("anthropic/claude-3-5-sonnet");
    });
    expect(screen.queryByTestId("config-save-bar")).toBeNull();
    expect(screen.queryByTestId("rail-dirty-Advisor")).toBeNull();
  });

  it("does not clear dirty state when the PUT rejects", async () => {
    // A failed save must leave the operator's edits and the unsaved-changes
    // banner intact — only a successful PUT rebaselines.
    saveConfigMock.mockRejectedValue(new Error("Internal Server Error"));
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    fireEvent.click(screen.getByTestId("rail-item-Advisor"));
    await waitFor(() => screen.getByTestId("config-pane-Advisor"));
    const modelInput = screen.getByTestId("config-field-advisor.model_slug").querySelector("input");
    fireEvent.change(modelInput!, { target: { value: "anthropic/claude-3-5-sonnet" } });

    fireEvent.click(screen.getByTestId("config-save-btn"));
    await waitFor(() => expect(saveConfigMock).toHaveBeenCalledTimes(1));

    // Banner stays, error surfaces, dirty dot stays, edit is untouched.
    await waitFor(() => expect(screen.getByTestId("config-save-error")).toBeInTheDocument());
    expect(screen.getByTestId("config-save-bar")).toBeInTheDocument();
    expect(screen.getByTestId("rail-dirty-Advisor")).toBeInTheDocument();
    expect(modelInput!.value).toBe("anthropic/claude-3-5-sonnet");
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
    // Navigate to Advisor pane (Hardware is the new default first category)
    fireEvent.click(screen.getByTestId("rail-item-Advisor"));
    await waitFor(() => screen.getByTestId("config-pane-Advisor"));
    const masked = screen.getByTestId("masked-advisor.api_key_env");
    expect(masked).toBeInTheDocument();
    expect(masked).toHaveAttribute("aria-disabled", "true");
  });

  it("shows Reset to default button only when a field differs from its default, and clicking it restores the default", async () => {
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    // Navigate to Advisor pane
    fireEvent.click(screen.getByTestId("rail-item-Advisor"));
    await waitFor(() => screen.getByTestId("config-pane-Advisor"));
    // Initially at default (effective_value = meta.default = "openai/gpt-4o") → no reset
    expect(screen.queryByTestId("reset-advisor.model_slug")).toBeNull();
    const modelInput = screen.getByTestId("config-field-advisor.model_slug").querySelector("input");
    fireEvent.change(modelInput!, { target: { value: "anthropic/claude-3-haiku" } });
    // Differs from default → reset button appears
    const resetBtn = screen.getByTestId("reset-advisor.model_slug");
    expect(resetBtn).toBeInTheDocument();
    // Clicking reset restores meta.default ("openai/gpt-4o")
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

describe("ConfigPage — PUT body nesting", () => {
  it("sends correctly nested pre_first_crack_levers when a Pre-FC heat field is dirty", async () => {
    saveConfigMock.mockResolvedValue(makeSnapshot({ pre_fc_heat: 80 }));
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    fireEvent.click(screen.getByTestId("rail-item-Pre-FC Control"));
    await waitFor(() => screen.getByTestId("config-pane-Pre-FC Control"));
    const heatInput = screen.getByTestId("config-field-controller.pre_fc_heat_target_percent")
      .querySelector("input");
    fireEvent.change(heatInput!, { target: { value: "80" } });
    fireEvent.click(screen.getByTestId("config-save-btn"));
    await waitFor(() => expect(saveConfigMock).toHaveBeenCalledTimes(1));
    const body = saveConfigMock.mock.calls[0]![0] as Record<string, unknown>;
    // Must nest under pre_first_crack_levers, NOT flat
    expect(body).toEqual({
      controller: {
        pre_first_crack_levers: {
          heat_target_percent: 80,
        },
      },
    });
    expect(body).not.toHaveProperty("safety");
  });

  it("sends correctly nested late_maillard_trim when a trim field is dirty", async () => {
    saveConfigMock.mockResolvedValue(makeSnapshot());
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    fireEvent.click(screen.getByTestId("rail-item-Late-Maillard Trim"));
    await waitFor(() => screen.getByTestId("config-pane-Late-Maillard Trim"));
    // Change the trim_heat_percent field
    const trimHeatInput = screen.getByTestId("config-field-controller.late_maillard_trim_heat_percent")
      .querySelector("input");
    fireEvent.change(trimHeatInput!, { target: { value: "70" } });
    fireEvent.click(screen.getByTestId("config-save-btn"));
    await waitFor(() => expect(saveConfigMock).toHaveBeenCalledTimes(1));
    const body = saveConfigMock.mock.calls[0]![0] as Record<string, unknown>;
    // Must nest under pre_first_crack_levers.late_maillard_trim
    expect(body).toEqual({
      controller: {
        pre_first_crack_levers: {
          late_maillard_trim: {
            trim_heat_percent: 70,
          },
        },
      },
    });
    expect(body).not.toHaveProperty("safety");
  });
});

// ---------------------------------------------------------------------------
// Hardware / Audio / FC-Detection categories (slice 3c, #419)
// ---------------------------------------------------------------------------

describe("ConfigPage — Hardware category", () => {
  it("renders Hardware, Audio, FC-Detection rail items", async () => {
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    expect(screen.getByTestId("rail-item-Hardware")).toBeInTheDocument();
    expect(screen.getByTestId("rail-item-Audio")).toBeInTheDocument();
    expect(screen.getByTestId("rail-item-FC-Detection")).toBeInTheDocument();
  });

  it("switches to the Hardware pane when its rail item is clicked", async () => {
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    fireEvent.click(screen.getByTestId("rail-item-Hardware"));
    await waitFor(() => screen.getByTestId("config-pane-Hardware"));
    // Serial port and roaster driver fields must be rendered
    expect(screen.getByTestId("config-field-mcp_device.serial_port")).toBeInTheDocument();
    expect(screen.getByTestId("config-field-mcp_device.roaster_driver")).toBeInTheDocument();
  });

  it("renders the roaster_driver field as a text input (editable)", async () => {
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    fireEvent.click(screen.getByTestId("rail-item-Hardware"));
    await waitFor(() => screen.getByTestId("config-pane-Hardware"));
    const driverField = screen.getByTestId("config-field-mcp_device.roaster_driver");
    // Text input
    expect(driverField.querySelector("input[type='text']")).not.toBeNull();
    expect(driverField.querySelector("input[type='text']")).not.toBeDisabled();
  });

  it("sends mcp_device section in PUT body when a Hardware field is changed", async () => {
    saveConfigMock.mockResolvedValue(makeSnapshot());
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    fireEvent.click(screen.getByTestId("rail-item-Hardware"));
    await waitFor(() => screen.getByTestId("config-pane-Hardware"));
    const driverInput = screen
      .getByTestId("config-field-mcp_device.roaster_driver")
      .querySelector("input");
    fireEvent.change(driverInput!, { target: { value: "mock" } });
    fireEvent.click(screen.getByTestId("config-save-btn"));
    await waitFor(() => expect(saveConfigMock).toHaveBeenCalledTimes(1));
    const body = saveConfigMock.mock.calls[0]![0] as Record<string, unknown>;
    expect(body).toHaveProperty("mcp_device");
    expect((body.mcp_device as Record<string, unknown>).roaster_driver).toBe("mock");
    // Safety must not appear
    expect(body).not.toHaveProperty("safety");
  });
});

// ---------------------------------------------------------------------------
// Ambient / environment fields (#474) — same Hardware pane, "Ambient /
// environment" group. Reuses the #439 tri-state inherit/override mechanism;
// no new mechanism is exercised here, only that the three fields are wired
// through it correctly.
// ---------------------------------------------------------------------------

describe("ConfigPage — Ambient / environment fields (#474)", () => {
  it("renders all three ambient fields in the Hardware pane", async () => {
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    fireEvent.click(screen.getByTestId("rail-item-Hardware"));
    await waitFor(() => screen.getByTestId("config-pane-Hardware"));
    expect(screen.getByTestId("config-field-mcp_device.ambient_mode")).toBeInTheDocument();
    expect(screen.getByTestId("config-field-mcp_device.ambient_device")).toBeInTheDocument();
    expect(
      screen.getByTestId("config-field-mcp_device.ambient_poll_interval_seconds"),
    ).toBeInTheDocument();
  });

  it("renders ambient_mode as a select with an inherit option + disabled + yoctopuce (#482)", async () => {
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    fireEvent.click(screen.getByTestId("rail-item-Hardware"));
    await waitFor(() => screen.getByTestId("config-pane-Hardware"));
    const select = screen
      .getByTestId("config-field-mcp_device.ambient_mode")
      .querySelector("select");
    expect(select).not.toBeNull();
    expect(select).not.toBeDisabled();
    const optionValues = Array.from(select!.querySelectorAll("option")).map((o) => o.getAttribute("value"));
    // The fixture's ambient_mode is null (unconfigured) — a leading inherit
    // option ("") is required so the tri-state default never falls back to
    // the first real option (the #482 "Disabled" scare).
    expect(optionValues).toEqual(["", "disabled", "yoctopuce"]);
  });

  it("renders ambient_device as an editable text input", async () => {
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    fireEvent.click(screen.getByTestId("rail-item-Hardware"));
    await waitFor(() => screen.getByTestId("config-pane-Hardware"));
    const deviceField = screen.getByTestId("config-field-mcp_device.ambient_device");
    const input = deviceField.querySelector("input[type='text']");
    expect(input).not.toBeNull();
    expect(input).not.toBeDisabled();
  });

  it("renders ambient_poll_interval_seconds as an editable number input", async () => {
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    fireEvent.click(screen.getByTestId("rail-item-Hardware"));
    await waitFor(() => screen.getByTestId("config-pane-Hardware"));
    const pollField = screen.getByTestId("config-field-mcp_device.ambient_poll_interval_seconds");
    const input = pollField.querySelector("input[type='number']");
    expect(input).not.toBeNull();
    expect(input).not.toBeDisabled();
  });

  it("shows the yaml value in ambient_mode's inherit option and ambient_poll_interval_seconds' placeholder (#482)", async () => {
    // The lead's "0 poll-interval scare" scenario — ambient_mode and
    // ambient_poll_interval_seconds are the same mcp_device class as fc_mode
    // and must get identical inherit-state treatment.
    const snapshot = makeSnapshot();
    snapshot.mcp_device.ambient_mode = makeFieldMeta({
      saved_value: null,
      effective_value: null,
      default: null,
      yaml_value: "yoctopuce",
    });
    snapshot.mcp_device.ambient_poll_interval_seconds = makeFieldMeta({
      saved_value: null,
      effective_value: null,
      default: null,
      yaml_value: 30,
    });
    configMock.mockResolvedValue(snapshot);
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    fireEvent.click(screen.getByTestId("rail-item-Hardware"));
    await waitFor(() => screen.getByTestId("config-pane-Hardware"));

    const modeSelect = screen
      .getByTestId("config-field-mcp_device.ambient_mode")
      .querySelector("select") as HTMLSelectElement;
    expect(modeSelect.value).toBe("");
    const selectedOption = modeSelect.querySelector("option:checked") as HTMLOptionElement;
    expect(selectedOption.textContent).toBe("Inherit from yaml (yoctopuce)");

    const pollInput = screen
      .getByTestId("config-field-mcp_device.ambient_poll_interval_seconds")
      .querySelector("input") as HTMLInputElement;
    expect(pollInput.value).toBe("");
    expect(pollInput.placeholder).toBe("30 (from yaml)");
  });

  it("dirty-tracks ambient fields and nests them under mcp_device in the PUT body", async () => {
    saveConfigMock.mockResolvedValue(makeSnapshot());
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    fireEvent.click(screen.getByTestId("rail-item-Hardware"));
    await waitFor(() => screen.getByTestId("config-pane-Hardware"));

    // Dirty dot should not be visible before any edit.
    expect(screen.queryByTestId("rail-dirty-Hardware")).toBeNull();

    const modeSelect = screen
      .getByTestId("config-field-mcp_device.ambient_mode")
      .querySelector("select");
    fireEvent.change(modeSelect!, { target: { value: "yoctopuce" } });

    const deviceInput = screen
      .getByTestId("config-field-mcp_device.ambient_device")
      .querySelector("input");
    fireEvent.change(deviceInput!, { target: { value: "METEOMK2-123456" } });

    const pollInput = screen
      .getByTestId("config-field-mcp_device.ambient_poll_interval_seconds")
      .querySelector("input");
    fireEvent.change(pollInput!, { target: { value: "45" } });

    // Editing marks the category dirty.
    await waitFor(() =>
      expect(screen.queryByTestId("rail-dirty-Hardware")).not.toBeNull(),
    );

    fireEvent.click(screen.getByTestId("config-save-btn"));
    await waitFor(() => expect(saveConfigMock).toHaveBeenCalledTimes(1));
    const body = saveConfigMock.mock.calls[0]![0] as Record<string, unknown>;
    const mcpDevice = body.mcp_device as Record<string, unknown>;
    expect(mcpDevice.ambient_mode).toBe("yoctopuce");
    expect(mcpDevice.ambient_device).toBe("METEOMK2-123456");
    expect(mcpDevice.ambient_poll_interval_seconds).toBe(45);
    // Safety must never appear.
    expect(body).not.toHaveProperty("safety");
  });

  it("clears ambient_mode back to Inherit (null) in the PUT body (#439 tri-state)", async () => {
    // Start from an overridden value so there is something to clear.
    const snapshot = makeSnapshot();
    snapshot.mcp_device.ambient_mode = makeFieldMeta({
      effective_value: "yoctopuce",
      saved_value: "yoctopuce",
      default: null,
    });
    configMock.mockResolvedValue(snapshot);
    saveConfigMock.mockResolvedValue(snapshot);
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    fireEvent.click(screen.getByTestId("rail-item-Hardware"));
    await waitFor(() => screen.getByTestId("config-pane-Hardware"));

    const modeSelect = screen
      .getByTestId("config-field-mcp_device.ambient_mode")
      .querySelector("select") as HTMLSelectElement;
    expect(modeSelect.value).toBe("yoctopuce");

    // Reset-to-default restores the schema default, which is null (inherit)
    // for this tri-state field — the same affordance already used by the
    // other optional mcp_device fields (#439).
    fireEvent.click(screen.getByTestId("reset-mcp_device.ambient_mode"));
    fireEvent.click(screen.getByTestId("config-save-btn"));
    await waitFor(() => expect(saveConfigMock).toHaveBeenCalledTimes(1));
    const body = saveConfigMock.mock.calls[0]![0] as Record<string, unknown>;
    const mcpDevice = body.mcp_device as Record<string, unknown>;
    expect(mcpDevice).toHaveProperty("ambient_mode");
    expect(mcpDevice.ambient_mode).toBeNull();
  });

  it("does not treat ambient fields as read-only / safety-guarded (device config is editable, #474)", async () => {
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    fireEvent.click(screen.getByTestId("rail-item-Hardware"));
    await waitFor(() => screen.getByTestId("config-pane-Hardware"));

    for (const key of [
      "mcp_device.ambient_mode",
      "mcp_device.ambient_device",
      "mcp_device.ambient_poll_interval_seconds",
    ]) {
      const field = screen.getByTestId(`config-field-${key}`);
      // No "Guarded" chip (that's the safety-only affordance).
      expect(field.textContent).not.toMatch(/Guarded/);
      const control = field.querySelector("input, select");
      expect(control).not.toBeNull();
      expect(control).not.toBeDisabled();
    }
  });
});

describe("ConfigPage — Audio category", () => {
  it("switches to the Audio pane and shows the mic test button placeholder", async () => {
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    fireEvent.click(screen.getByTestId("rail-item-Audio"));
    await waitFor(() => screen.getByTestId("config-pane-Audio"));
    // Mic test button is present and disabled
    expect(screen.getByTestId("mic-test-button")).toBeInTheDocument();
    expect(screen.getByTestId("mic-test-button")).toBeDisabled();
  });

  it("renders recording_enabled as a tri-state control in Audio (#439)", async () => {
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    fireEvent.click(screen.getByTestId("rail-item-Audio"));
    await waitFor(() => screen.getByTestId("config-pane-Audio"));
    // mcp_device booleans use NullableBooleanControl (tri-state radio group),
    // not the two-state BooleanControl (role=switch).
    const toggleField = screen.getByTestId("config-field-mcp_device.recording_enabled");
    expect(toggleField.querySelector("[role='radiogroup']")).not.toBeNull();
    expect(toggleField.querySelector("[role='switch']")).toBeNull();
  });
});

describe("ConfigPage — FC-Detection category", () => {
  it("switches to the FC-Detection pane and shows fc_mode and fc_confidence_threshold", async () => {
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    fireEvent.click(screen.getByTestId("rail-item-FC-Detection"));
    await waitFor(() => screen.getByTestId("config-pane-FC-Detection"));
    expect(screen.getByTestId("config-field-mcp_device.fc_mode")).toBeInTheDocument();
    expect(screen.getByTestId("config-field-mcp_device.fc_confidence_threshold")).toBeInTheDocument();
  });

  it("sends fc_mode in the mcp_device section when changed", async () => {
    saveConfigMock.mockResolvedValue(makeSnapshot());
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    fireEvent.click(screen.getByTestId("rail-item-FC-Detection"));
    await waitFor(() => screen.getByTestId("config-pane-FC-Detection"));
    const fcModeSelect = screen
      .getByTestId("config-field-mcp_device.fc_mode")
      .querySelector("select");
    // Default value is null; change to "audio"
    fireEvent.change(fcModeSelect!, { target: { value: "audio" } });
    fireEvent.click(screen.getByTestId("config-save-btn"));
    await waitFor(() => expect(saveConfigMock).toHaveBeenCalledTimes(1));
    const body = saveConfigMock.mock.calls[0]![0] as Record<string, unknown>;
    expect((body.mcp_device as Record<string, unknown>).fc_mode).toBe("audio");
  });

  it("shows auto_t0_detection_enabled toggle and auto_t0_drop_threshold_c field", async () => {
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    fireEvent.click(screen.getByTestId("rail-item-FC-Detection"));
    await waitFor(() => screen.getByTestId("config-pane-FC-Detection"));
    expect(screen.getByTestId("config-field-mcp_device.auto_t0_detection_enabled")).toBeInTheDocument();
    // auto_t0_drop_threshold_c is hidden by default (auto_t0_detection_enabled = null → falsy)
    expect(screen.queryByTestId("config-field-mcp_device.auto_t0_drop_threshold_c")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// #482: inherit-state rendering — never a bogus concrete value for a null
// (unconfigured) mcp_device field; restore-to-inherit clears back to null.
// ---------------------------------------------------------------------------

describe("ConfigPage — #482 inherit-state rendering (fc_mode select)", () => {
  it("shows an 'Inherit from yaml (<value>)' option, never a bogus concrete option, when fc_mode is unconfigured", async () => {
    // The exact #482 scare: fc_mode is null (unconfigured) but the yaml says
    // "audio" — the select must show the real yaml value as the inherit
    // option's label and select IT, never fall back to the first hardcoded
    // option ("disabled"), which used to read as "FC detection is off".
    const snapshot = makeSnapshot();
    snapshot.mcp_device.fc_mode = makeFieldMeta({
      saved_value: null,
      effective_value: null,
      default: null,
      yaml_value: "audio",
    });
    configMock.mockResolvedValue(snapshot);
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    fireEvent.click(screen.getByTestId("rail-item-FC-Detection"));
    await waitFor(() => screen.getByTestId("config-pane-FC-Detection"));

    const select = screen
      .getByTestId("config-field-mcp_device.fc_mode")
      .querySelector("select") as HTMLSelectElement;
    // The selected option is the inherit option (value ""), not "disabled".
    expect(select.value).toBe("");
    const selectedOption = select.querySelector("option:checked") as HTMLOptionElement;
    expect(selectedOption.textContent).toBe("Inherit from yaml (audio)");
    // "Disabled" must NOT be the rendered/selected state.
    expect(selectedOption.textContent).not.toMatch(/^Disabled$/);
  });

  it("shows a plain 'Inherit from yaml' label when no yaml value is known", async () => {
    // yaml_value: null (no hand-authored yaml resolvable) — still must not
    // fabricate a concrete option; falls back to the unparameterised label.
    const snapshot = makeSnapshot();
    snapshot.mcp_device.fc_mode = makeFieldMeta({
      saved_value: null,
      effective_value: null,
      default: null,
      yaml_value: null,
    });
    configMock.mockResolvedValue(snapshot);
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    fireEvent.click(screen.getByTestId("rail-item-FC-Detection"));
    await waitFor(() => screen.getByTestId("config-pane-FC-Detection"));

    const select = screen
      .getByTestId("config-field-mcp_device.fc_mode")
      .querySelector("select") as HTMLSelectElement;
    expect(select.value).toBe("");
    const selectedOption = select.querySelector("option:checked") as HTMLOptionElement;
    expect(selectedOption.textContent).toBe("Inherit from yaml");
  });

  it("shows the 'From yaml: <value>' baseline line instead of 'Default —' when yaml_value is known", async () => {
    const snapshot = makeSnapshot();
    snapshot.mcp_device.fc_mode = makeFieldMeta({
      saved_value: null,
      effective_value: null,
      default: null,
      yaml_value: "audio",
    });
    configMock.mockResolvedValue(snapshot);
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    fireEvent.click(screen.getByTestId("rail-item-FC-Detection"));
    await waitFor(() => screen.getByTestId("config-pane-FC-Detection"));

    const field = screen.getByTestId("config-field-mcp_device.fc_mode");
    expect(field.textContent).toMatch(/From yaml: audio/);
    expect(field.textContent).not.toMatch(/Default —/);
  });

  it("selecting a concrete option overrides the inherit state and sends it in the PUT body", async () => {
    const snapshot = makeSnapshot();
    snapshot.mcp_device.fc_mode = makeFieldMeta({
      saved_value: null,
      effective_value: null,
      default: null,
      yaml_value: "audio",
    });
    configMock.mockResolvedValue(snapshot);
    saveConfigMock.mockResolvedValue(snapshot);
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    fireEvent.click(screen.getByTestId("rail-item-FC-Detection"));
    await waitFor(() => screen.getByTestId("config-pane-FC-Detection"));

    const select = screen
      .getByTestId("config-field-mcp_device.fc_mode")
      .querySelector("select") as HTMLSelectElement;
    fireEvent.change(select, { target: { value: "manual" } });
    fireEvent.click(screen.getByTestId("config-save-btn"));
    await waitFor(() => expect(saveConfigMock).toHaveBeenCalledTimes(1));
    const body = saveConfigMock.mock.calls[0]![0] as Record<string, unknown>;
    expect((body.mcp_device as Record<string, unknown>).fc_mode).toBe("manual");
  });

  it("restore-to-inherit clears an overridden fc_mode back to null in the PUT body", async () => {
    // Start from an explicit override (saved_value set) — the reset button
    // must be labelled for the yaml semantics and clear back to null.
    const snapshot = makeSnapshot();
    snapshot.mcp_device.fc_mode = makeFieldMeta({
      saved_value: "manual",
      effective_value: "manual",
      default: null,
      yaml_value: "audio",
    });
    configMock.mockResolvedValue(snapshot);
    saveConfigMock.mockResolvedValue(snapshot);
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    fireEvent.click(screen.getByTestId("rail-item-FC-Detection"));
    await waitFor(() => screen.getByTestId("config-pane-FC-Detection"));

    const restoreBtn = screen.getByTestId("reset-mcp_device.fc_mode");
    expect(restoreBtn.textContent).toMatch(/Restore to yaml/);
    fireEvent.click(restoreBtn);

    const select = screen
      .getByTestId("config-field-mcp_device.fc_mode")
      .querySelector("select") as HTMLSelectElement;
    expect(select.value).toBe("");

    fireEvent.click(screen.getByTestId("config-save-btn"));
    await waitFor(() => expect(saveConfigMock).toHaveBeenCalledTimes(1));
    const body = saveConfigMock.mock.calls[0]![0] as Record<string, unknown>;
    expect(body.mcp_device).toHaveProperty("fc_mode");
    expect((body.mcp_device as Record<string, unknown>).fc_mode).toBeNull();
  });
});

describe("ConfigPage — #482 inherit-state rendering (number fields)", () => {
  it("renders a blank input with a 'from yaml' placeholder, never a literal 0, for an inherited number field", async () => {
    const snapshot = makeSnapshot();
    snapshot.mcp_device.ambient_poll_interval_seconds = makeFieldMeta({
      saved_value: null,
      effective_value: null,
      default: null,
      yaml_value: 30,
    });
    configMock.mockResolvedValue(snapshot);
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    fireEvent.click(screen.getByTestId("rail-item-Hardware"));
    await waitFor(() => screen.getByTestId("config-pane-Hardware"));

    const input = screen
      .getByTestId("config-field-mcp_device.ambient_poll_interval_seconds")
      .querySelector("input") as HTMLInputElement;
    // Blank, not "0" — the #482 scare.
    expect(input.value).toBe("");
    expect(input.placeholder).toBe("30 (from yaml)");
  });

  it("shows the 'From yaml: <value>' baseline for an inherited number field", async () => {
    const snapshot = makeSnapshot();
    snapshot.mcp_device.fc_confidence_threshold = makeFieldMeta({
      saved_value: null,
      effective_value: null,
      default: null,
      yaml_value: 0.6,
    });
    configMock.mockResolvedValue(snapshot);
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    fireEvent.click(screen.getByTestId("rail-item-FC-Detection"));
    await waitFor(() => screen.getByTestId("config-pane-FC-Detection"));

    const field = screen.getByTestId("config-field-mcp_device.fc_confidence_threshold");
    expect(field.textContent).toMatch(/From yaml: 0\.6/);
  });

  it("typing a value overrides the inherit state and sends it as a number in the PUT body", async () => {
    const snapshot = makeSnapshot();
    snapshot.mcp_device.ambient_poll_interval_seconds = makeFieldMeta({
      saved_value: null,
      effective_value: null,
      default: null,
      yaml_value: 30,
    });
    configMock.mockResolvedValue(snapshot);
    saveConfigMock.mockResolvedValue(snapshot);
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    fireEvent.click(screen.getByTestId("rail-item-Hardware"));
    await waitFor(() => screen.getByTestId("config-pane-Hardware"));

    const input = screen
      .getByTestId("config-field-mcp_device.ambient_poll_interval_seconds")
      .querySelector("input") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "45" } });
    fireEvent.click(screen.getByTestId("config-save-btn"));
    await waitFor(() => expect(saveConfigMock).toHaveBeenCalledTimes(1));
    const body = saveConfigMock.mock.calls[0]![0] as Record<string, unknown>;
    expect((body.mcp_device as Record<string, unknown>).ambient_poll_interval_seconds).toBe(45);
  });

  it("restore-to-inherit clears an overridden number field back to null in the PUT body", async () => {
    const snapshot = makeSnapshot();
    snapshot.mcp_device.ambient_poll_interval_seconds = makeFieldMeta({
      saved_value: 45,
      effective_value: 45,
      default: null,
      yaml_value: 30,
    });
    configMock.mockResolvedValue(snapshot);
    saveConfigMock.mockResolvedValue(snapshot);
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    fireEvent.click(screen.getByTestId("rail-item-Hardware"));
    await waitFor(() => screen.getByTestId("config-pane-Hardware"));

    const restoreBtn = screen.getByTestId("reset-mcp_device.ambient_poll_interval_seconds");
    expect(restoreBtn.textContent).toMatch(/Restore to yaml/);
    fireEvent.click(restoreBtn);

    const input = screen
      .getByTestId("config-field-mcp_device.ambient_poll_interval_seconds")
      .querySelector("input") as HTMLInputElement;
    expect(input.value).toBe("");

    fireEvent.click(screen.getByTestId("config-save-btn"));
    await waitFor(() => expect(saveConfigMock).toHaveBeenCalledTimes(1));
    const body = saveConfigMock.mock.calls[0]![0] as Record<string, unknown>;
    expect(body.mcp_device).toHaveProperty("ambient_poll_interval_seconds");
    expect((body.mcp_device as Record<string, unknown>).ambient_poll_interval_seconds).toBeNull();
  });
});

describe("ConfigPage — #482 inherit-state rendering (text fields)", () => {
  it("renders a blank input with a 'from yaml' placeholder, never a bogus concrete value, for an inherited text field", async () => {
    const snapshot = makeSnapshot();
    snapshot.mcp_device.ambient_device = makeFieldMeta({
      saved_value: null,
      effective_value: null,
      default: null,
      yaml_value: "METEOMK2-999999",
    });
    configMock.mockResolvedValue(snapshot);
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    fireEvent.click(screen.getByTestId("rail-item-Hardware"));
    await waitFor(() => screen.getByTestId("config-pane-Hardware"));

    const input = screen
      .getByTestId("config-field-mcp_device.ambient_device")
      .querySelector("input") as HTMLInputElement;
    // Blank, not the bare value with no context — the placeholder carries it.
    expect(input.value).toBe("");
    expect(input.placeholder).toBe("METEOMK2-999999 (from yaml)");
  });

  it("shows the 'From yaml: <value>' baseline for an inherited text field", async () => {
    const snapshot = makeSnapshot();
    snapshot.mcp_device.ambient_device = makeFieldMeta({
      saved_value: null,
      effective_value: null,
      default: null,
      yaml_value: "METEOMK2-999999",
    });
    configMock.mockResolvedValue(snapshot);
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    fireEvent.click(screen.getByTestId("rail-item-Hardware"));
    await waitFor(() => screen.getByTestId("config-pane-Hardware"));

    const field = screen.getByTestId("config-field-mcp_device.ambient_device");
    expect(field.textContent).toMatch(/From yaml: METEOMK2-999999/);
  });

  it("restore-to-inherit clears an overridden text field back to null in the PUT body", async () => {
    const snapshot = makeSnapshot();
    snapshot.mcp_device.ambient_device = makeFieldMeta({
      saved_value: "METEOMK2-OVERRIDE",
      effective_value: "METEOMK2-OVERRIDE",
      default: null,
      yaml_value: "METEOMK2-999999",
    });
    configMock.mockResolvedValue(snapshot);
    saveConfigMock.mockResolvedValue(snapshot);
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    fireEvent.click(screen.getByTestId("rail-item-Hardware"));
    await waitFor(() => screen.getByTestId("config-pane-Hardware"));

    const restoreBtn = screen.getByTestId("reset-mcp_device.ambient_device");
    expect(restoreBtn.textContent).toMatch(/Restore to yaml/);
    fireEvent.click(restoreBtn);

    const input = screen
      .getByTestId("config-field-mcp_device.ambient_device")
      .querySelector("input") as HTMLInputElement;
    expect(input.value).toBe("");

    fireEvent.click(screen.getByTestId("config-save-btn"));
    await waitFor(() => expect(saveConfigMock).toHaveBeenCalledTimes(1));
    const body = saveConfigMock.mock.calls[0]![0] as Record<string, unknown>;
    expect(body.mcp_device).toHaveProperty("ambient_device");
    expect((body.mcp_device as Record<string, unknown>).ambient_device).toBeNull();
  });
});

describe("ConfigPage — #482 non-mcp_device fields are unaffected", () => {
  it("a controller number field still uses the plain 'Default <value>' line and label", async () => {
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    fireEvent.click(screen.getByTestId("rail-item-Pre-FC Control"));
    await waitFor(() => screen.getByTestId("config-pane-Pre-FC Control"));
    const field = screen.getByTestId("config-field-controller.pre_fc_heat_target_percent");
    expect(field.textContent).toMatch(/Default 100/);

    const input = field.querySelector("input") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "80" } });
    const resetBtn = screen.getByTestId("reset-controller.pre_fc_heat_target_percent");
    expect(resetBtn.textContent).toMatch(/Reset to default/);
    expect(resetBtn.textContent).not.toMatch(/Restore to yaml/);
  });

  it("a controller/advisor select field is unaffected by the mcp_device inherit-option logic", async () => {
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    fireEvent.click(screen.getByTestId("rail-item-Advisor"));
    await waitFor(() => screen.getByTestId("config-pane-Advisor"));
    const select = screen
      .getByTestId("config-field-advisor.prompt_version")
      .querySelector("select") as HTMLSelectElement;
    const optionValues = Array.from(select.querySelectorAll("option")).map((o) =>
      o.getAttribute("value"),
    );
    // No leading empty-value inherit option injected for a non-mcp_device select.
    expect(optionValues).not.toContain("");
  });
});

// ---------------------------------------------------------------------------
// QA must-fix: field reveal for auto_t0_drop_threshold_c (#437 qa)
// ---------------------------------------------------------------------------

describe("ConfigPage — revealWhen: auto_t0_drop_threshold_c", () => {
  it("hides auto_t0_drop_threshold_c when auto_t0_detection_enabled is false/null", async () => {
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    fireEvent.click(screen.getByTestId("rail-item-FC-Detection"));
    await waitFor(() => screen.getByTestId("config-pane-FC-Detection"));
    // Default effective_value is null → revealWhen(equals: true) → hidden
    expect(screen.queryByTestId("config-field-mcp_device.auto_t0_drop_threshold_c")).toBeNull();
  });

  it("reveals auto_t0_drop_threshold_c after enabling auto_t0_detection_enabled", async () => {
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    fireEvent.click(screen.getByTestId("rail-item-FC-Detection"));
    await waitFor(() => screen.getByTestId("config-pane-FC-Detection"));

    // auto_t0_detection_enabled is an mcp_device boolean — rendered as a tri-state
    // NullableBooleanControl (Inherit / On / Off); click the "On" segment to set true.
    const onRadio = screen.getByTestId("nullable-bool-mcp_device.auto_t0_detection_enabled-true");
    expect(onRadio).not.toBeNull();
    fireEvent.click(onRadio!);

    // Now the threshold field must appear
    await waitFor(() =>
      expect(
        screen.getByTestId("config-field-mcp_device.auto_t0_drop_threshold_c"),
      ).toBeInTheDocument(),
    );
  });

  it("hides auto_t0_drop_threshold_c again after disabling auto_t0_detection_enabled", async () => {
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    fireEvent.click(screen.getByTestId("rail-item-FC-Detection"));
    await waitFor(() => screen.getByTestId("config-pane-FC-Detection"));

    // Enable: click On segment
    const onRadio = screen.getByTestId("nullable-bool-mcp_device.auto_t0_detection_enabled-true");
    fireEvent.click(onRadio!);
    await waitFor(() =>
      expect(
        screen.queryByTestId("config-field-mcp_device.auto_t0_drop_threshold_c"),
      ).toBeInTheDocument(),
    );
    // Disable: click Inherit (clears back to null, which is falsy → hides threshold)
    const inheritRadio = screen.getByTestId("nullable-bool-mcp_device.auto_t0_detection_enabled-null");
    fireEvent.click(inheritRadio!);
    await waitFor(() =>
      expect(
        screen.queryByTestId("config-field-mcp_device.auto_t0_drop_threshold_c"),
      ).toBeNull(),
    );
  });
});

// ---------------------------------------------------------------------------
// QA must-fix: revealWhen for trim damping knobs (#443)
// ---------------------------------------------------------------------------

describe("ConfigPage — revealWhen: trim_depth_deadband_pp / trim_depth_slew_pp_per_tick", () => {
  it("hides both damping fields when adaptive_depth_enabled is false", async () => {
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    fireEvent.click(screen.getByTestId("rail-item-Late-Maillard Trim"));
    await waitFor(() => screen.getByTestId("config-pane-Late-Maillard Trim"));
    // adaptive_depth_enabled defaults to false → revealWhen(equals: true) → hidden
    expect(screen.queryByTestId("config-field-controller.late_maillard_trim_trim_depth_deadband_pp")).toBeNull();
    expect(screen.queryByTestId("config-field-controller.late_maillard_trim_trim_depth_slew_pp_per_tick")).toBeNull();
  });

  it("reveals both damping fields after enabling adaptive_depth_enabled", async () => {
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    fireEvent.click(screen.getByTestId("rail-item-Late-Maillard Trim"));
    await waitFor(() => screen.getByTestId("config-pane-Late-Maillard Trim"));

    // Click the adaptive_depth_enabled toggle (boolean field, aria-role="switch")
    const adaptiveToggle = screen.getByTestId("toggle-controller.late_maillard_trim_adaptive_depth_enabled");
    fireEvent.click(adaptiveToggle);

    // Both damping fields must now appear
    await waitFor(() =>
      expect(
        screen.getByTestId("config-field-controller.late_maillard_trim_trim_depth_deadband_pp"),
      ).toBeInTheDocument(),
    );
    await waitFor(() =>
      expect(
        screen.getByTestId("config-field-controller.late_maillard_trim_trim_depth_slew_pp_per_tick"),
      ).toBeInTheDocument(),
    );
  });

  it("hides both damping fields again after reverting adaptive_depth_enabled", async () => {
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    fireEvent.click(screen.getByTestId("rail-item-Late-Maillard Trim"));
    await waitFor(() => screen.getByTestId("config-pane-Late-Maillard Trim"));

    const adaptiveToggle = screen.getByTestId("toggle-controller.late_maillard_trim_adaptive_depth_enabled");
    // Enable
    fireEvent.click(adaptiveToggle);
    await waitFor(() =>
      expect(
        screen.getByTestId("config-field-controller.late_maillard_trim_trim_depth_deadband_pp"),
      ).toBeInTheDocument(),
    );
    // Revert (click again to set back to false/default)
    fireEvent.click(adaptiveToggle);
    await waitFor(() =>
      expect(
        screen.queryByTestId("config-field-controller.late_maillard_trim_trim_depth_deadband_pp"),
      ).toBeNull(),
    );
    expect(screen.queryByTestId("config-field-controller.late_maillard_trim_trim_depth_slew_pp_per_tick")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// QA must-fix: DeviceSelect / DeviceMultiSelect control-type assertions (#437 qa)
// ---------------------------------------------------------------------------

describe("ConfigPage — device field control types (never free-text)", () => {
  it("Hardware: serial_port renders a DeviceSelect, not a plain text input", async () => {
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    fireEvent.click(screen.getByTestId("rail-item-Hardware"));
    await waitFor(() => screen.getByTestId("config-pane-Hardware"));

    const serialField = screen.getByTestId("config-field-mcp_device.serial_port");
    // DeviceSelect trigger must be present
    expect(serialField.querySelector("[data-testid='device-select-trigger']")).not.toBeNull();
    // Must NOT have a plain text input
    expect(serialField.querySelector("input[type='text']")).toBeNull();
  });

  it("Audio: audio_input_device renders a DeviceSelect, not a plain text input", async () => {
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    fireEvent.click(screen.getByTestId("rail-item-Audio"));
    await waitFor(() => screen.getByTestId("config-pane-Audio"));

    const audioField = screen.getByTestId("config-field-mcp_device.audio_input_device");
    expect(audioField.querySelector("[data-testid='device-select-trigger']")).not.toBeNull();
    expect(audioField.querySelector("input[type='text']")).toBeNull();
  });

  it("Audio: recording_devices renders a DeviceMultiSelect trigger, not a plain text input", async () => {
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    fireEvent.click(screen.getByTestId("rail-item-Audio"));
    await waitFor(() => screen.getByTestId("config-pane-Audio"));

    const recDevField = screen.getByTestId("config-field-mcp_device.recording_devices");
    // DeviceMultiSelect renders a trigger button, not a text input
    expect(recDevField.querySelector("[data-testid='device-multi-select-trigger']")).not.toBeNull();
    expect(recDevField.querySelector("input[type='text']")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// QA must-fix: recording_devices array equality (no false-positive dirty) (#437 qa)
// ---------------------------------------------------------------------------

describe("ConfigPage — recording_devices array dirty detection", () => {
  it("does NOT show dirty dot / save bar when recording_devices content is unchanged", async () => {
    // Snapshot has recording_devices = ["USB PnP"]; toggling a device OFF then
    // ON again should leave no dirty state because valuesEqual detects same content.
    configMock.mockResolvedValue(
      (() => {
        const snap = makeSnapshot();
        snap.mcp_device.recording_devices = {
          saved_value: ["USB PnP"],
          effective_value: ["USB PnP"],
          default: null,
          env_overridden: false,
          read_only: false,
          description: "",
          yaml_value: null,
        };
        return snap;
      })(),
    );
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    fireEvent.click(screen.getByTestId("rail-item-Audio"));
    await waitFor(() => screen.getByTestId("config-pane-Audio"));

    const recDevField = screen.getByTestId("config-field-mcp_device.recording_devices");

    // Open the multi-select popover so the rows are rendered
    const trigger = recDevField.querySelector("[data-testid='device-multi-select-trigger']");
    expect(trigger).not.toBeNull();
    fireEvent.click(trigger!);

    // The USB PnP row is rendered; aria-selected=true (initially selected)
    const usbRow = await waitFor(() =>
      screen.getByTestId("device-multi-option-USB PnP"),
    );
    expect(usbRow).toHaveAttribute("aria-selected", "true");

    // Deselect then re-select — content returns to its original state
    fireEvent.click(usbRow);
    fireEvent.click(usbRow);

    // Save bar must NOT appear — same content, not dirty
    expect(screen.queryByTestId("config-save-bar")).toBeNull();
    expect(screen.queryByTestId("rail-dirty-Audio")).toBeNull();
  });

  it("DOES show dirty dot / save bar when recording_devices content differs", async () => {
    // Snapshot has recording_devices = ["USB PnP"]; deselecting → content = [] → dirty
    configMock.mockResolvedValue(
      (() => {
        const snap = makeSnapshot();
        snap.mcp_device.recording_devices = {
          saved_value: ["USB PnP"],
          effective_value: ["USB PnP"],
          default: null,
          env_overridden: false,
          read_only: false,
          description: "",
          yaml_value: null,
        };
        return snap;
      })(),
    );
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    fireEvent.click(screen.getByTestId("rail-item-Audio"));
    await waitFor(() => screen.getByTestId("config-pane-Audio"));

    const recDevField = screen.getByTestId("config-field-mcp_device.recording_devices");

    // Open the multi-select popover
    const trigger = recDevField.querySelector("[data-testid='device-multi-select-trigger']");
    expect(trigger).not.toBeNull();
    fireEvent.click(trigger!);

    const usbRow = await waitFor(() =>
      screen.getByTestId("device-multi-option-USB PnP"),
    );
    expect(usbRow).toHaveAttribute("aria-selected", "true");

    // Deselect — content now [] vs saved ["USB PnP"] → dirty
    fireEvent.click(usbRow);
    expect(screen.getByTestId("config-save-bar")).toBeInTheDocument();
    expect(screen.getByTestId("rail-dirty-Audio")).toBeInTheDocument();
  });

  it("sends correct recording_devices array in PUT body after selecting a device", async () => {
    // Start with no recording_devices (null); select "USB PnP" → PUT body has the array.
    saveConfigMock.mockResolvedValue(makeSnapshot());
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    fireEvent.click(screen.getByTestId("rail-item-Audio"));
    await waitFor(() => screen.getByTestId("config-pane-Audio"));

    const recDevField = screen.getByTestId("config-field-mcp_device.recording_devices");

    // Open the multi-select popover (initial value = null/empty)
    const trigger = recDevField.querySelector("[data-testid='device-multi-select-trigger']");
    expect(trigger).not.toBeNull();
    fireEvent.click(trigger!);

    const usbRow = await waitFor(() =>
      screen.getByTestId("device-multi-option-USB PnP"),
    );
    expect(usbRow).toHaveAttribute("aria-selected", "false");

    // Select it — adds "USB PnP" to the array
    fireEvent.click(usbRow);
    fireEvent.click(screen.getByTestId("config-save-btn"));

    await waitFor(() => expect(saveConfigMock).toHaveBeenCalledTimes(1));
    const body = saveConfigMock.mock.calls[0]![0] as Record<string, unknown>;
    const recDevices = (body.mcp_device as Record<string, unknown>)?.recording_devices;
    expect(Array.isArray(recDevices)).toBe(true);
    expect(recDevices).toContain("USB PnP");
  });
});

// ---------------------------------------------------------------------------
// S4 polish: category ordering, group subheadings, a11y, dirty-guard fix (#421)
// ---------------------------------------------------------------------------

describe("ConfigPage — S4: category ordering (Hardware first, Safety last)", () => {
  it("Hardware is the default active category (first in the new order)", async () => {
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    // Hardware is now first so it renders without any click
    expect(screen.getByTestId("config-pane-Hardware")).toBeInTheDocument();
  });

  it("Advisor appears between FC-Detection and Pre-FC Control in the rail", async () => {
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    const rail = screen.getByTestId("config-rail");
    const items = Array.from(rail.querySelectorAll("[data-testid^='rail-item-']")).map(
      (el) => el.getAttribute("data-testid"),
    );
    // Verify the new ordering: Hardware → Audio → FC-Detection → Advisor → Pre-FC → Trim → Safety
    expect(items.indexOf("rail-item-Hardware")).toBeLessThan(items.indexOf("rail-item-Audio"));
    expect(items.indexOf("rail-item-Audio")).toBeLessThan(items.indexOf("rail-item-FC-Detection"));
    expect(items.indexOf("rail-item-FC-Detection")).toBeLessThan(items.indexOf("rail-item-Advisor"));
    expect(items.indexOf("rail-item-Advisor")).toBeLessThan(items.indexOf("rail-item-Pre-FC Control"));
    expect(items.indexOf("rail-item-Pre-FC Control")).toBeLessThan(items.indexOf("rail-item-Late-Maillard Trim"));
    expect(items.indexOf("rail-item-Late-Maillard Trim")).toBeLessThan(items.indexOf("rail-item-Safety"));
  });
});

describe("ConfigPage — S4: group subheadings (h3 with hairline)", () => {
  it("Hardware pane renders the 'Roaster' group h3", async () => {
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    // Hardware is the default pane
    const pane = screen.getByTestId("config-pane-Hardware");
    const h3 = pane.querySelector("h3");
    expect(h3).not.toBeNull();
    expect(h3!.textContent?.toUpperCase()).toBe("ROASTER");
  });

  it("Audio pane renders 'First-crack input' and 'Recording' group h3s", async () => {
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    fireEvent.click(screen.getByTestId("rail-item-Audio"));
    await waitFor(() => screen.getByTestId("config-pane-Audio"));
    const pane = screen.getByTestId("config-pane-Audio");
    const h3s = Array.from(pane.querySelectorAll("h3")).map((el) =>
      el.textContent?.toUpperCase(),
    );
    expect(h3s).toContain("FIRST-CRACK INPUT");
    expect(h3s).toContain("RECORDING");
  });

  it("FC-Detection pane renders 'Detection' and 'Auto-T0' group h3s", async () => {
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    fireEvent.click(screen.getByTestId("rail-item-FC-Detection"));
    await waitFor(() => screen.getByTestId("config-pane-FC-Detection"));
    const pane = screen.getByTestId("config-pane-FC-Detection");
    const h3s = Array.from(pane.querySelectorAll("h3")).map((el) =>
      el.textContent?.toUpperCase(),
    );
    expect(h3s).toContain("DETECTION");
    // Auto-T0 group heading
    expect(h3s.some((t) => t?.includes("AUTO-T0"))).toBe(true);
  });

  it("Advisor pane renders 'Model', 'Parameters', and 'Credentials' group h3s", async () => {
    renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));
    fireEvent.click(screen.getByTestId("rail-item-Advisor"));
    await waitFor(() => screen.getByTestId("config-pane-Advisor"));
    const pane = screen.getByTestId("config-pane-Advisor");
    const h3s = Array.from(pane.querySelectorAll("h3")).map((el) =>
      el.textContent?.toUpperCase(),
    );
    expect(h3s).toContain("MODEL");
    expect(h3s).toContain("PARAMETERS");
    expect(h3s).toContain("CREDENTIALS");
  });
});

describe("ConfigPage — S4: dirty-guard fix (valuesEqual in snapshot re-init guard)", () => {
  it("re-initialises from a fresh clean snapshot even when recording_devices is the same content", async () => {
    // Before fix: recording_devices new array ref from toggle-and-revert would
    // mark form "dirty" via !== and block re-init from a background refresh.
    // After fix: valuesEqual detects same content → not dirty → re-init fires.
    const { client } = renderPage();
    await waitFor(() => screen.getByTestId("config-layout"));

    // Navigate to Advisor and confirm initial model slug
    fireEvent.click(screen.getByTestId("rail-item-Advisor"));
    await waitFor(() => screen.getByTestId("config-pane-Advisor"));
    const modelInput = screen.getByTestId("config-field-advisor.model_slug").querySelector("input");
    expect(modelInput!.value).toBe("openai/gpt-4o");

    // Push a background refresh with a NEW model (different from initial) and
    // same-content recording_devices — with the fix, this re-init fires.
    client.setQueryData(["config"], makeSnapshot({ model_slug: "anthropic/claude-3-5-sonnet" }));

    // The form should show the new value (re-init fired, no dirty to block it)
    await waitFor(() => {
      expect(modelInput!.value).toBe("anthropic/claude-3-5-sonnet");
    });
  });
});
