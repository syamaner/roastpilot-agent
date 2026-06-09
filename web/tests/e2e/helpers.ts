/**
 * Snapshot/e2e conventions (D24).
 *
 *   - `maskCanvas(page)` → locators to pass as `mask:` to `toHaveScreenshot` so
 *     the flaky uPlot <canvas> is excluded from the DOM-chrome baseline.
 *   - `readChartData(page)` → the `window.__chart` test hook, so the chart's
 *     DATA is asserted directly (deterministic) instead of pixel-diffing it.
 *   - `settle(page)` → await fonts + a paint so chrome snapshots are stable.
 */

import { expect, type Locator, type Page } from "@playwright/test";

/** The uPlot canvas region(s) to mask out of chrome screenshots. */
export function maskCanvas(page: Page): Locator[] {
  return [page.locator("[data-testid='live-curve'] canvas")];
}

/** Shape of the chart test hook exposed by LiveCurve (mirror of ChartTestHook). */
export interface ChartHookSnapshot {
  columns: (number | null)[][];
  visible: Record<string, boolean>;
  markers: { kind: string; t: number; label: string }[];
  highlightTime: number | null;
  chargeBandVisible: boolean;
}

/** Read the LiveCurve data hook (asserts data, never pixels — D24). */
export async function readChartData(page: Page): Promise<ChartHookSnapshot> {
  return page.evaluate(() => {
    const hook = (window as unknown as { __chart?: ChartHookSnapshot }).__chart;
    if (!hook) throw new Error("window.__chart not set — LiveCurve not mounted");
    return hook;
  });
}

/** Wait for fonts + the chart-ready marker so chrome snapshots are deterministic. */
export async function settle(page: Page): Promise<void> {
  await page.evaluate(() => document.fonts.ready);
  await expect(page.locator("[data-chart-ready='true']")).toBeVisible();
}
