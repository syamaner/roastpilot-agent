/**
 * Roast detail snapshot suite (E10-S5, D26).
 *
 * The two required baseline states (kickoff §5): `roast-detail` and
 * `roast-detail-selected` (a CLAMP trace row selected + its marker on the curve).
 *
 * These run against the deterministic dev/test-only `/__detail-harness` route
 * (fixed REST-shaped data via `fixture.ts`) so the baselines are reproducible
 * without the stepped-SSE replay backend. D26: the uPlot canvas is UN-MASKED — the
 * full-page shot now includes the persisted curve; chart correctness still asserted
 * via the `window.__chart` data hook as the authoritative layer alongside the pixels.
 */

import { expect, test } from "@playwright/test";

import { readChartData, settle, waitForChartPoints } from "./helpers";

test.beforeEach(async ({ page }) => {
  await page.goto("/__detail-harness");
  await settle(page);
  // Gate snapshots on the rendered point-count so the un-masked canvas is stable.
  await waitForChartPoints(page, 1);
});

test("roast-detail — full-page snapshot of the detail page (canvas un-masked)", async ({
  page,
}) => {
  // The decision-trace table, title block, timeline, rating, export row, AND the
  // persisted curve are all in the baseline now (data asserted in the test below).
  await expect(page.getByTestId("decision-trace-table")).toBeVisible();
  await expect(page).toHaveScreenshot("roast-detail.png");
});

test("roast-detail-selected — CLAMP row selected highlights the curve", async ({ page }) => {
  // No highlight until a row is selected.
  expect((await readChartData(page)).highlightTime).toBeNull();

  // Select the CLAMP trace row (the talk's key frame). It sits at fixture tick 8
  // → 240 s, so the shared LiveCurve must draw its highlight there.
  const clampRow = page.locator("[data-testid='trace-row'][data-verdict='clamp']");
  await clampRow.click();
  await expect(clampRow).toHaveAttribute("data-selected", "true");

  // Assert the cross-component highlight via the chart DATA hook (the authoritative
  // layer); the un-masked snapshot also captures the highlight line on the curve.
  expect((await readChartData(page)).highlightTime).toBe(240);

  await expect(page).toHaveScreenshot("roast-detail-selected.png");
});

test("the detail curve carries the full persisted series + T0/FC/drop markers", async ({
  page,
}) => {
  const hook = await readChartData(page);
  expect(hook.columns).toHaveLength(6); // x + bean/env/ror/heat/fan
  expect(hook.columns[0].length).toBeGreaterThan(0);
  expect(hook.markers.map((m) => m.kind).sort()).toEqual(["drop", "first_crack", "t0"]);
});

test.describe("advisor-failure detail (#170)", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/__detail-harness-failed");
    await settle(page);
    await waitForChartPoints(page, 1);
  });

  test("roast-detail-advisor-failed — the advisor timeline renders failures, not a blank panel", async ({
    page,
  }) => {
    // Every consult in this fixture is a provider_error → the advisor timeline must
    // show the failure rows (the safety-spined decision-trace table is empty here).
    const timeline = page.getByTestId("advisor-timeline");
    await expect(timeline).toBeVisible();
    await expect(page.getByTestId("advisor-row")).toHaveCount(3);
    await expect(page.getByTestId("advisor-status").first()).toHaveText("PROVIDER ERROR");
    await expect(page.getByTestId("advisor-summary-failed")).toHaveText("3 failed");
    await expect(page.getByTestId("advisor-timeline-empty")).toHaveCount(0);
    await expect(page).toHaveScreenshot("roast-detail-advisor-failed.png");
  });
});
