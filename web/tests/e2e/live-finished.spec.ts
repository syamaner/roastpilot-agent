/**
 * LiveFinishedView snapshot suite (#423 D81, D24/D26).
 *
 * Drives the deterministic `/__live-finished-harness` route (nav + the finished-
 * roast summary view over seeded fixture data). The data-assert layer (stat tile
 * values + the "View full detail" href + the curve point count) is asserted
 * BEFORE the pixel snapshot so a content regression fails on behaviour, not only
 * on the CI-generated baseline.
 *
 * D26: the uPlot canvas is UN-MASKED — the curve is included in the baseline.
 * The chart-data hook (`window.__chart`) is the authoritative correctness oracle;
 * the screenshot is the visual-smoke layer on top.
 *
 * NOTE: the `live-finished` baseline is owned by the CI Docker snapshot job
 * (D26) — it must be (re)generated there, not committed from a local macOS run.
 *
 * Constants here are kept in sync with liveFinishedFixture.ts manually — the e2e
 * tsconfig does not cover src/ imports, so the fixture types stay there for the
 * unit tests and these literals mirror the relevant display values.
 */

import { expect, test } from "@playwright/test";

import { readChartData, settle, waitForChartPoints } from "./helpers";

// Mirror of FIXTURE_FINISHED_RUN_ID from liveFinishedFixture.ts.
const FIXTURE_FINISHED_RUN_ID = "live-finished-fixture-001";
// Mirror of FIXTURE_FINISHED_STATS from liveFinishedFixture.ts.
const FIXTURE_FINISHED_STATS = {
  dropTempDisplay: "191 °C",
  devPercentDisplay: "18.7 %",
  totalTimeDisplay: "6:30",
} as const;
// Mirror of FIXTURE_FINISHED_TELEMETRY.point_count from liveFinishedFixture.ts.
const FIXTURE_FINISHED_POINT_COUNT = 5;

test.beforeEach(async ({ page }) => {
  await page.goto("/__live-finished-harness");
  await expect(page.getByTestId("live-finished-view")).toBeVisible();
  await settle(page);
  // Gate: wait for all fixture telemetry points to render (canvas stable).
  await waitForChartPoints(page, FIXTURE_FINISHED_POINT_COUNT);
});

test("live-finished — stat tiles, detail link, curve data-assert + snapshot", async ({
  page,
}) => {
  // Data-assert: stat tiles show the fixture-derived values.
  await expect(page.getByTestId("stat-drop-temp")).toContainText(
    FIXTURE_FINISHED_STATS.dropTempDisplay,
  );
  await expect(page.getByTestId("stat-dev-percent")).toContainText(
    FIXTURE_FINISHED_STATS.devPercentDisplay,
  );
  await expect(page.getByTestId("stat-total-time")).toContainText(
    FIXTURE_FINISHED_STATS.totalTimeDisplay,
  );

  // "View full detail" must link to the fixture run's detail page.
  await expect(page.getByTestId("live-finished-view-detail")).toHaveAttribute(
    "href",
    `/roasts/${FIXTURE_FINISHED_RUN_ID}`,
  );

  // Nav is idle: Home link present, Live-roast link absent.
  await expect(page.getByTestId("nav-home")).toBeVisible();
  await expect(page.getByTestId("nav-live-roast")).toHaveCount(0);

  // Chart data-assert: x-series length matches fixture point count; scale covers the data.
  const chart = await readChartData(page);
  expect(chart.columns[0].length).toBe(FIXTURE_FINISHED_POINT_COUNT);
  // The x scale must span the data (anti-collapse guard).
  expect(chart.scales.x.min).not.toBeNull();
  expect(chart.scales.x.max).not.toBeNull();
  expect(chart.scales.x.max!).toBeGreaterThan(chart.scales.x.min!);

  // Full-page pixel snapshot (canvas un-masked, D26 — CI Docker only).
  await expect(page).toHaveScreenshot("live-finished.png");
});
