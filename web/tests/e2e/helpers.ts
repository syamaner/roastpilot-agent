/**
 * Snapshot/e2e conventions (D26 — revises D24).
 *
 * D26 un-masks the uPlot canvas: the chart is now INCLUDED in every page
 * `toHaveScreenshot()` (the product's primary visual is finally pixel-tested).
 * The mask helper is gone. The `window.__chart` data hook STAYS as the
 * authoritative correctness layer — data-assert green + pixel-diff red localizes
 * a regression to render/CSS rather than data (D26 §4).
 *
 *   - `readChartData(page)` → the `window.__chart` test hook, so the chart's
 *     DATA is asserted directly (the correctness oracle), alongside the pixels.
 *   - `waitForChartPoints(page, n)` → block until the chart has rendered ≥ n
 *     points, so the un-masked canvas shot never races async telemetry (D26 kit).
 *   - `settle(page)` → await fonts + the chart-ready marker so the whole page
 *     (chrome + canvas) is stable before shooting.
 */

import { expect, type Page } from "@playwright/test";

/** Shape of the chart test hook exposed by LiveCurve (mirror of ChartTestHook). */
export interface ChartHookSnapshot {
  columns: (number | null)[][];
  visible: Record<string, boolean>;
  markers: { kind: string; t: number; label: string }[];
  highlightTime: number | null;
  chargeBandVisible: boolean;
}

/** Read the LiveCurve data hook (the authoritative correctness layer — D26). */
export async function readChartData(page: Page): Promise<ChartHookSnapshot> {
  return page.evaluate(() => {
    const hook = (window as unknown as { __chart?: ChartHookSnapshot }).__chart;
    if (!hook) throw new Error("window.__chart not set — LiveCurve not mounted");
    return hook;
  });
}

/**
 * Block until the chart's x-series carries at least `min` points, so the
 * un-masked canvas snapshot is taken AFTER the curve has drawn (no async-data
 * race — D26 determinism kit). `window.__chart` is the same hook `readChartData`
 * asserts; here it is the snapshot gate.
 */
export async function waitForChartPoints(page: Page, min: number): Promise<void> {
  await page.waitForFunction(
    (n) => {
      const hook = (window as unknown as { __chart?: ChartHookSnapshot }).__chart;
      return (hook?.columns?.[0]?.length ?? 0) >= n;
    },
    min,
    { timeout: 15_000 },
  );
}

/** Wait for fonts + the chart-ready marker so the page (chrome + canvas) is stable. */
export async function settle(page: Page): Promise<void> {
  await page.evaluate(() => document.fonts.ready);
  await expect(page.locator("[data-chart-ready='true']")).toBeVisible();
}
