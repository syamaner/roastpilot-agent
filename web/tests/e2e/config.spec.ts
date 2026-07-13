/**
 * /config settings screen snapshot + data-assert suite (#475).
 *
 * `/config` had strong vitest component coverage (ConfigPage.test.tsx) but ZERO
 * browser-level coverage before this file — no replay agent carries config state
 * (config is pure REST, no SSE), so this mirrors `history.spec.ts`'s convention
 * rather than the replay-stepped dashboard specs: a Playwright route intercept of
 * the REST endpoints the real product route depends on, then `page.goto` the
 * real `/config` route (nested under `RootLayout`, so the persistent nav renders
 * too — `NavBar` reads `GET /api/health`, which is mocked idle here).
 *
 * No new harness page: `/config` is reachable directly (no active-run gating,
 * unlike `/start`'s idle-only reachability that motivated the start-roast route
 * harness), and REST-route interception is the established convention for a
 * pure-REST page (history.spec.ts) — reused here rather than adding a parallel
 * `/__config-harness` mechanism.
 *
 * Fixture: a representative `AppConfigSnapshot` + `DevicesSnapshot` covering
 * every category (Hardware incl. the #474 Ambient/environment group, Audio,
 * FC-Detection, Advisor, Pre-FC Control, Late-Maillard Trim, Safety) so the
 * default-landing Hardware pane — and the ambient fields within it — render
 * real, non-degenerate values.
 *
 * Two states:
 *   - config              → the default-landing Hardware pane (category rail +
 *                            ambient group + a read-only field's Guarded chip
 *                            is NOT visible here since Safety isn't the active
 *                            pane — asserted separately below)
 *   - config-safety        → the Safety pane selected, proving the read-only /
 *                            Guarded rendering for a representative safety field
 */

import { expect, test, type Page } from "@playwright/test";

import type { AppConfigSnapshot, DevicesSnapshot, HealthResponse } from "../../src/lib/types";

// ---------------------------------------------------------------------------
// Fixture: a representative, fully-populated AppConfigSnapshot.
// ---------------------------------------------------------------------------

function meta(overrides: Partial<AppConfigSnapshot["controller"]["tick_interval_seconds"]>) {
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

const CONFIG_SNAPSHOT: AppConfigSnapshot = {
  controller: {
    tick_interval_seconds: meta({ effective_value: 1.0, default: 1.0, read_only: true }),
    pre_fc_heat_target_percent: meta({ effective_value: 100, default: 100 }),
    pre_fc_fan_target_percent: meta({ effective_value: 30, default: 30 }),
    late_maillard_trim_enabled: meta({ effective_value: true, default: true }),
    late_maillard_trim_heat_percent: meta({ effective_value: 65, default: 65 }),
    late_maillard_trim_window_fc_eta_seconds: meta({ effective_value: 60.0, default: 60.0 }),
    late_maillard_trim_min_bean_temp_c: meta({ effective_value: 155.0, default: 155.0 }),
    late_maillard_trim_adaptive_depth_enabled: meta({ effective_value: false, default: false }),
    late_maillard_trim_base_trim: meta({ effective_value: 65, default: 65 }),
    late_maillard_trim_k_ror: meta({ effective_value: 1.5, default: 1.5 }),
    late_maillard_trim_k_eta: meta({ effective_value: 0.2, default: 0.2 }),
    late_maillard_trim_ror_ref: meta({ effective_value: 8.0, default: 8.0 }),
    late_maillard_trim_eta_ref: meta({ effective_value: 60.0, default: 60.0 }),
    late_maillard_trim_min_trim: meta({ effective_value: 45, default: 45 }),
    late_maillard_trim_max_trim: meta({ effective_value: 75, default: 75 }),
    late_maillard_trim_trim_depth_deadband_pp: meta({ effective_value: 2, default: 2 }),
    late_maillard_trim_trim_depth_slew_pp_per_tick: meta({ effective_value: 3, default: 3 }),
  },
  advisor: {
    model_slug: meta({ effective_value: "openai/gpt-4o", default: "openai/gpt-4o" }),
    prompt_version: meta({ effective_value: "c3", default: "c3" }),
    provider: meta({
      effective_value: "openai_compatible",
      default: "openai_compatible",
      read_only: true,
    }),
    provider_base_url: meta({
      effective_value: "https://openrouter.ai/api/v1",
      default: "https://openrouter.ai/api/v1",
      read_only: true,
    }),
    api_key_env: meta({
      effective_value: "OPENROUTER_API_KEY",
      default: "OPENROUTER_API_KEY",
      read_only: true,
    }),
    timeout_seconds: meta({ effective_value: 10.0, default: 10.0 }),
    temperature: meta({ effective_value: 0.0, default: 0.0 }),
  },
  safety: {
    max_bean_temp_c: meta({ effective_value: 230, default: 230, read_only: true }),
    max_env_temp_c: meta({ effective_value: 240, default: 240, read_only: true }),
    pre_t0_max_bean_temp_c: meta({ effective_value: 200, default: 200, read_only: true }),
    overrun_safe_fan_percent: meta({ effective_value: 100, default: 100, read_only: true }),
    pre_t0_overrun_severity: meta({
      effective_value: "recovery",
      default: "recovery",
      read_only: true,
    }),
    min_seconds_between_commands: meta({ effective_value: 2.0, default: 2.0, read_only: true }),
    max_consecutive_mcp_failures: meta({ effective_value: 3, default: 3, read_only: true }),
    max_consecutive_advisor_failures: meta({ effective_value: 3, default: 3, read_only: true }),
    bitter_ceiling_temp_c: meta({ effective_value: 196, default: 196, read_only: true }),
    emergency_drop_temp_c: meta({ effective_value: 198, default: 198, read_only: true }),
  },
  mcp_device: {
    serial_port: meta({ effective_value: "/dev/ttyUSB0", default: null }),
    roaster_driver: meta({ effective_value: "hottop_kn8828b_2k_plus", default: null }),
    audio_input_device: meta({ effective_value: "USB PnP", default: null }),
    recording_enabled: meta({ effective_value: true, default: null }),
    recording_autocapture: meta({ effective_value: true, default: null }),
    recording_devices: meta({ effective_value: ["USB PnP"], default: null }),
    // fc_mode / fc_confidence_threshold: the exact #482 scenario — genuinely
    // unconfigured (saved/effective both null) but the hand-authored yaml has
    // a real value the operator needs to see (never a bogus "Disabled"/"0").
    fc_mode: meta({ effective_value: null, default: null, yaml_value: "audio" }),
    fc_confidence_threshold: meta({ effective_value: null, default: null, yaml_value: 0.5 }),
    auto_t0_detection_enabled: meta({ effective_value: true, default: null }),
    auto_t0_drop_threshold_c: meta({ effective_value: 5.0, default: null }),
    // #474 ambient / environment group — a real overridden mode so the e2e
    // fixture isn't the all-null trap (#241's lesson): a populated yoctopuce
    // selection with a pinned device + a non-default poll interval.
    ambient_mode: meta({ effective_value: "yoctopuce", saved_value: "yoctopuce", default: null }),
    ambient_device: meta({
      effective_value: "METEOMK2-123456",
      saved_value: "METEOMK2-123456",
      default: null,
    }),
    ambient_poll_interval_seconds: meta({ effective_value: 45, saved_value: 45, default: null }),
  },
};

const DEVICES_SNAPSHOT: DevicesSnapshot = {
  serial: [{ value: "/dev/ttyUSB0", label: "USB Serial", note: "" }],
  serial_error: null,
  audio_input: [{ value: "USB PnP", label: "USB PnP Sound Device", note: "" }],
  audio_input_error: null,
};

const IDLE_HEALTH: HealthResponse = {
  status: "ok",
  version: "e2e-fixture",
  instance_id: "e2e-fixture-instance",
  mcp_child: "running",
  active_run_id: null,
};

async function mockConfigBackend(page: Page): Promise<void> {
  await page.route("**/api/health", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(IDLE_HEALTH),
    }),
  );
  await page.route("**/api/config/devices", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(DEVICES_SNAPSHOT),
    }),
  );
  // Registered AFTER the more specific /api/config/devices route so Playwright's
  // last-registered-first-matched order doesn't swallow the devices request.
  await page.route("**/api/config", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(CONFIG_SNAPSHOT),
    }),
  );
}

async function settle(page: Page): Promise<void> {
  await page.evaluate(() => document.fonts.ready);
}

test("config — Hardware pane on load (rail + ambient group), full-page snapshot", async ({
  page,
}) => {
  await mockConfigBackend(page);
  await page.goto("/config");

  // The category rail is present with every category, Hardware first (default).
  await expect(page.getByTestId("config-rail")).toBeVisible();
  await expect(page.getByTestId("rail-item-Hardware")).toBeVisible();
  await expect(page.getByTestId("rail-item-Audio")).toBeVisible();
  await expect(page.getByTestId("rail-item-FC-Detection")).toBeVisible();
  await expect(page.getByTestId("rail-item-Advisor")).toBeVisible();
  await expect(page.getByTestId("rail-item-Pre-FC Control")).toBeVisible();
  await expect(page.getByTestId("rail-item-Late-Maillard Trim")).toBeVisible();
  await expect(page.getByTestId("rail-item-Safety")).toBeVisible();

  // Hardware is the default-landing pane (S4 reorder) — a representative
  // editable field (roaster_driver, a text input) renders with its fixture
  // value, hard-failing on a missing/renamed field regardless of pixel tolerance.
  await expect(page.getByTestId("config-pane-Hardware")).toBeVisible();
  const driverField = page.getByTestId("config-field-mcp_device.roaster_driver");
  await expect(driverField).toBeVisible();
  await expect(driverField.locator("input[type='text']")).toHaveValue(
    "hottop_kn8828b_2k_plus",
  );

  // #474: the Ambient / environment group renders within the same Hardware
  // pane — all three ambient fields present with their fixture values, so this
  // e2e locks in the ambient config UI at the browser level (previously vitest
  // component coverage only).
  const modeField = page.getByTestId("config-field-mcp_device.ambient_mode");
  await expect(modeField).toBeVisible();
  await expect(modeField.locator("select")).toHaveValue("yoctopuce");
  const deviceField = page.getByTestId("config-field-mcp_device.ambient_device");
  await expect(deviceField).toBeVisible();
  await expect(deviceField.locator("input[type='text']")).toHaveValue("METEOMK2-123456");
  const pollField = page.getByTestId("config-field-mcp_device.ambient_poll_interval_seconds");
  await expect(pollField).toBeVisible();
  await expect(pollField.locator("input[type='number']")).toHaveValue("45");
  // Ambient fields are editable device config, not safety — no Guarded chip.
  await expect(modeField).not.toContainText("Guarded");

  await settle(page);
  await expect(page).toHaveScreenshot("config.png");
});

test("config-safety — Safety pane shows a read-only/Guarded field, full-page snapshot", async ({
  page,
}) => {
  await mockConfigBackend(page);
  await page.goto("/config");
  await expect(page.getByTestId("config-rail")).toBeVisible();

  await page.getByTestId("rail-item-Safety").click();
  await expect(page.getByTestId("config-pane-Safety")).toBeVisible();

  // A representative Safety field renders disabled with the Guarded chip and
  // the fixture's read-only effective value — the server-enforced read-only
  // state (D78 decision 2), asserted structurally before the pixel snapshot.
  const maxBeanField = page.getByTestId("config-field-safety.max_bean_temp_c");
  await expect(maxBeanField).toBeVisible();
  await expect(maxBeanField).toContainText("Guarded");
  const input = maxBeanField.locator("input");
  await expect(input).toBeDisabled();
  await expect(input).toHaveValue("230");

  await settle(page);
  await expect(page).toHaveScreenshot("config-safety.png");
});

test("config-fc-detection — fc_mode renders the real yaml value as its inherit option, never a bogus concrete option (#482)", async ({
  page,
}) => {
  // Browser-level guard for the #482 scare: fc_mode unconfigured (null) but
  // the hand-authored yaml says "audio" must never render as if FC detection
  // were "Disabled" — data-assert only, no new pixel baseline (this proves
  // the DOM/value contract; config.png / config-safety.png already cover the
  // visual regression surface for /config).
  await mockConfigBackend(page);
  await page.goto("/config");
  await expect(page.getByTestId("config-rail")).toBeVisible();

  await page.getByTestId("rail-item-FC-Detection").click();
  await expect(page.getByTestId("config-pane-FC-Detection")).toBeVisible();

  const fcModeField = page.getByTestId("config-field-mcp_device.fc_mode");
  await expect(fcModeField).toBeVisible();
  const select = fcModeField.locator("select");
  // The fixture's fc_mode is unconfigured (saved/effective both null) with
  // yaml_value="audio" — the select must show and select the inherit option
  // carrying that real value, not fall back to the first hardcoded option.
  await expect(select).toHaveValue("");
  const selectedOptionText = await select.locator("option:checked").textContent();
  expect(selectedOptionText).toBe("Inherit from yaml (audio)");
  expect(selectedOptionText).not.toBe("Disabled");

  // The baseline line reflects the yaml value, not a meaningless "Default —".
  await expect(fcModeField).toContainText("From yaml: audio");
});
