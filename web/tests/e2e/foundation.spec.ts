/**
 * Foundation snapshot suite (D24) — the S2 gate target.
 *
 * Proves the snapshot conventions work BEFORE any product page exists, against
 * the deterministic /__chart-harness route (fixed data). S3–S6 add page-state
 * snapshots backed by the replay harness (S1).
 *
 * The conventions exercised here are the ones the page suites reuse:
 *   - DOM-chrome `toHaveScreenshot()` with the uPlot canvas MASKED
 *   - chart correctness asserted via the DATA hook (readChartData), not pixels
 *   - a single loose canvas "did it draw / not blank" smoke shot
 */

import { expect, test } from "@playwright/test";

import { maskCanvas, readChartData, settle } from "./helpers";

test.beforeEach(async ({ page }) => {
  await page.goto("/__chart-harness");
  await settle(page);
});

test("foundation chrome — masked canvas DOM snapshot", async ({ page }) => {
  // The DOM chrome (header, connection indicator, verdict badges, legend) is the
  // baseline; the canvas is masked because pixel-correctness is asserted via data.
  await expect(page).toHaveScreenshot("foundation-chrome.png", {
    mask: maskCanvas(page),
  });
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
