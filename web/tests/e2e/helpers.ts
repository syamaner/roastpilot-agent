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

import type { ReplayStepResult } from "./global-setup";

/** Shape of the chart test hook exposed by LiveCurve (mirror of ChartTestHook). */
export interface ChartHookSnapshot {
  columns: (number | null)[][];
  visible: Record<string, boolean>;
  markers: { kind: string; t: number; label: string }[];
  highlightTime: number | null;
  chargeBandVisible: boolean;
  /**
   * Rendered uPlot scale ranges. x is asserted to COVER the data (#131); c and ror
   * are FIXED (#217), so a test asserts they hold their pinned bounds (0–210 °C,
   * −20..+30 °C/min) regardless of the live data.
   */
  scales: {
    x: { min: number | null; max: number | null };
    c: { min: number | null; max: number | null };
    ror: { min: number | null; max: number | null };
  };
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

/**
 * Settle the browser onto a deterministic replay-step result — the LOSSLESS
 * barrier (#338) that replaces `waitForFunction(window.__lastEventId >= id)`.
 *
 * The old barrier waited for the browser's `__lastEventId` to reach the stepped
 * result's `last_event_id`. That assumes lossless SSE delivery, but the broadcaster
 * drops on `QueueFull` and offers no Last-Event-ID resume, so a single missed frame
 * (CI load, an EventSource reconnect) left `__lastEventId` permanently short → a
 * deterministic 15 s timeout. This barrier instead settles on signals the system
 * actually GUARANTEES, both store-backed and lossless:
 *
 *   1. the rendered `phase-badge` reaches the server's `agent_phase` (phase is
 *      re-hydrated from the REST snapshot on every (re)connect — never lost);
 *   2. the rendered curve (`window.__chart`) reaches the server's CHARGED point
 *      count (`persisted_point_count`), which the SPA re-hydrates from the REST
 *      `/telemetry` snapshot on (re)connect (#153) — so a dropped telemetry frame
 *      self-heals rather than wedging the barrier.
 *
 * A pre-charge state (`persisted_point_count === 0`, e.g. recovery / preheating
 * before the charge clock) carries no plotted curve, so only the phase gate
 * applies — correct, because the curve is intentionally empty there.
 */
export async function settleStepped(page: Page, reached: ReplayStepResult): Promise<void> {
  await expect(page.getByTestId("phase-badge")).toHaveAttribute(
    "data-phase",
    reached.agent_phase,
    { timeout: 15_000 },
  );
  if (reached.persisted_point_count > 0) {
    await page.waitForFunction(
      (n) => {
        const hook = (window as unknown as { __chart?: ChartHookSnapshot }).__chart;
        return (hook?.columns?.[0]?.length ?? 0) >= n;
      },
      reached.persisted_point_count,
      { timeout: 15_000 },
    );
  }
}
