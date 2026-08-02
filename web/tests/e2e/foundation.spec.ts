/**
 * Foundation snapshot suite (D26).
 *
 * Proves the snapshot conventions against the deterministic /__chart-harness route
 * (fixed data) — the gallery of the shared LiveCurve + badges + indicator.
 *
 * The conventions exercised here are the ones the page suites reuse:
 *   - full-page `toHaveScreenshot()` with the uPlot canvas UN-MASKED (D26)
 *   - chart correctness ALSO asserted via the DATA hook (readChartData) — the
 *     authoritative layer alongside the pixels
 *   - the point-count gate (waitForChartPoints) before shooting
 */

import { expect, test } from "@playwright/test";

import { readChartData, settle, waitForChartPoints } from "./helpers";

test.beforeEach(async ({ page }) => {
  await page.goto("/__chart-harness");
  await settle(page);
  // Gate snapshots on the rendered point-count so the un-masked canvas is stable.
  await waitForChartPoints(page, 1);
});

test("foundation chrome — full-page snapshot with the un-masked canvas", async ({ page }) => {
  // The DOM chrome (header, connection indicator, verdict badges, legend) AND the
  // rendered curve are the baseline now; correctness is also asserted via the data
  // hook in the tests below.
  await expect(page).toHaveScreenshot("foundation-chrome.png", { fullPage: true });
});

test("engaged D96 authority status exposes entry diagnostics", async ({ page }) => {
  const status = page.getByTestId("post-fc-recovery-status");
  await expect(status).toHaveAttribute("data-state", "recovering");
  await expect(status).toContainText("Recovery entry");
  await expect(status).toContainText("RoR 4.8 °C/min");
  await expect(status).toContainText("Target 6.4 °C/min");
  await expect(status).toContainText("Heat ceiling 75 %");
  await expect(status).toHaveScreenshot("foundation-d96-recovering.png");
});

test("chart data hook exposes the five series + markers (D24)", async ({ page }) => {
  const hook = await readChartData(page);
  // Six columns: x + bean/env/ror/heat/fan.
  expect(hook.columns).toHaveLength(6);
  expect(hook.columns[0].length).toBeGreaterThan(0);
  // All five series visible by default.
  expect(hook.visible).toMatchObject({
    bean: true,
    env: true,
    ror: true,
    heat: true,
    fan: true,
  });
  // T0 + first crack markers from the harness fixture.
  expect(hook.markers.map((m) => m.kind)).toContain("t0");
  expect(hook.markers.map((m) => m.kind)).toContain("first_crack");
  // Charge band visible in the preheating fixture.
  expect(hook.chargeBandVisible).toBe(true);
});

test("legend click toggles a series in the data hook (click-to-toggle)", async ({ page }) => {
  expect((await readChartData(page)).visible.heat).toBe(true);
  await page.getByTestId("legend-heat").click();
  expect((await readChartData(page)).visible.heat).toBe(false);
});

test("trace-row highlight toggles on the data hook", async ({ page }) => {
  expect((await readChartData(page)).highlightTime).toBeNull();
  await page.getByTestId("toggle-highlight").click();
  expect((await readChartData(page)).highlightTime).not.toBeNull();
  await page.getByTestId("toggle-highlight").click();
  expect((await readChartData(page)).highlightTime).toBeNull();
});

test("canvas smoke — the chart drew something (loose, not a gate baseline)", async ({ page }) => {
  // ≤1 loose canvas shot per D24: just confirm the canvas exists + has size, so a
  // blank/crashed chart is caught without a pixel baseline.
  const canvas = page.locator("[data-testid='live-curve'] canvas").first();
  await expect(canvas).toBeVisible();
  const box = await canvas.boundingBox();
  expect(box?.width ?? 0).toBeGreaterThan(0);
  expect(box?.height ?? 0).toBeGreaterThan(0);
});
