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

// The five columns the decision-trace table renders, in order
// (DecisionTraceTable.tsx). The detail snapshot asserts the live header row
// matches this EXACTLY, so a trace column added / removed / reordered fails
// structurally even when the pixel diff stays under `maxDiffPixelRatio` — the
// same blind spot #241 closed for the history table (#253). Scoped to the
// trace table's own testid so a second table on the page can never make this
// match the wrong header row.
const EXPECTED_TRACE_COLUMNS = [
  "Time",
  "Recommended",
  "Verdict",
  "Executed",
  "Rationale",
] as const;

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
  // Structural guard (#253, mirroring #241): the rendered trace-table header row
  // must match the five columns exactly. Fails fast on an added/removed/reordered
  // trace column even when the pixel diff stays under tolerance. Scoped to the
  // trace table's own `thead` so a second table on the page can't match it.
  await expect(page.locator("[data-testid='decision-trace-table'] thead th")).toHaveText([
    ...EXPECTED_TRACE_COLUMNS,
  ]);
  // #464 structural guard: the "Roast conditions" widget renders the fixture's
  // REAL captured charge-time triad (29.7 °C / 41 % / 1008 hPa), not the "—"
  // empty state — hard-fails on a missing/renamed element regardless of pixel
  // tolerance, and proves the populated baseline isn't the #241 all-null trap.
  const conditions = page.getByTestId("roast-conditions");
  await expect(conditions).toBeVisible();
  await expect(page.getByTestId("roast-conditions-temp")).toHaveText("29.7 °C");
  await expect(page.getByTestId("roast-conditions-humidity")).toHaveText("41 %");
  await expect(page.getByTestId("roast-conditions-pressure")).toHaveText("1008 hPa");
  // #566: baseline regenerated — "Your rating" now renders the read-only
  // headline (stars + saved note, with an "Edit" affordance) instead of the
  // old always-editable star-picker form.
  await expect(page).toHaveScreenshot("roast-detail.png", { fullPage: true });
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
  // layer): the selected CLAMP tick maps to t=240 on the curve.
  expect((await readChartData(page)).highlightTime).toBe(240);

  // The CLAMP highlight is drawn on the LiveCurve, which sits at the TOP of the page;
  // clicking the trace row (far below) scrolls the chart ABOVE the viewport, so the
  // old full-page-viewport baseline never contained the highlight (#126). Bring the
  // chart back into frame and capture it element-scoped, so the committed baseline
  // ACTUALLY shows the highlight line — the state this snapshot exists to prove.
  const curve = page.getByTestId("live-curve");
  await curve.scrollIntoViewIfNeeded();
  // Structural, pixel-INDEPENDENT guard (#241 lesson): assert the highlight-bearing
  // chart is genuinely in the viewport before the shot. Combined with the data-hook
  // assertion above (highlightTime === 240), a missing highlight now fails the suite
  // regardless of pixel tolerance — the chart not being framed fails HERE, the
  // highlight not being set fails the hook.
  await expect(curve).toBeInViewport();
  await settle(page);
  await expect(curve).toHaveScreenshot("roast-detail-selected.png");
});

test("the detail curve carries the full persisted series + T0/FC/drop markers", async ({
  page,
}) => {
  const hook = await readChartData(page);
  expect(hook.columns).toHaveLength(6); // x + bean/env/ror/heat/fan
  expect(hook.columns[0].length).toBeGreaterThan(0);
  expect(hook.markers.map((m) => m.kind).sort()).toEqual(["drop", "first_crack", "t0"]);
});

test.describe("dry-end marker on the persisted reload path (#351)", () => {
  // A SEPARATE harness route (FIXTURE_TIMELINE_DRY_END = the base detail data + a
  // persisted drying_end event) so this positive case runs end-to-end WITHOUT
  // disturbing the base /__detail-harness `roast-detail` snapshot. Data-only (D24):
  // no toHaveScreenshot, so no pixel baseline shifts.
  test.beforeEach(async ({ page }) => {
    await page.goto("/__detail-harness-dry-end");
    await settle(page);
    await waitForChartPoints(page, 1);
  });

  test("dry_end reaches the chart, placed at the server threshold cross (240 s)", async ({
    page,
  }) => {
    const hook = await readChartData(page);
    // The reload path hydrates dry-end FROM the persisted timeline event alongside
    // T0/FC/drop — the positive case the base detail fixture never exercises.
    expect(hook.markers.map((m) => m.kind).sort()).toEqual([
      "drop",
      "dry_end",
      "first_crack",
      "t0",
    ]);
    // Placed at the first telemetry point reaching the event's server threshold_c
    // (150 °C → fixture bean crosses at elapsed 240 s), NOT the event's monotonic.
    expect(hook.markers.find((m) => m.kind === "dry_end")?.t).toBe(240);
  });
});

test.describe("capped detail lists (#271)", () => {
  // The long-roast harness: advisor-decisions + decision-trace both have 24 rows
  // (> the inline cap of 5), so the page must show only the last 5 of each inline
  // and offer a "View all (N)" affordance into the scrollable modal — proving the
  // detail page stays a fixed-height layout instead of growing with roast length.
  test.beforeEach(async ({ page }) => {
    await page.goto("/__detail-harness-long");
    await settle(page);
    await waitForChartPoints(page, 1);
  });

  test("roast-detail-capped — inline lists cap at 5 with a 'View all' affordance", async ({
    page,
  }) => {
    // Inline cap: only the last 5 rows of each list render in the page body.
    await expect(page.getByTestId("trace-row")).toHaveCount(5);
    await expect(page.getByTestId("advisor-row")).toHaveCount(5);

    // The affordance appears iff N > cap (24 > 5 here), one per list.
    await expect(page.getByTestId("trace-view-all")).toHaveText("View all (24)");
    await expect(page.getByTestId("advisor-view-all")).toHaveText("View all (24)");

    // #253 still holds on the long page: the inline trace table keeps the guarded
    // `decision-trace-table` testid (the modal copy uses a distinct `-modal` id), so
    // the structural header guard resolves to exactly one table.
    await expect(page.locator("[data-testid='decision-trace-table'] thead th")).toHaveText([
      ...EXPECTED_TRACE_COLUMNS,
    ]);

    // #566: baseline regenerated for the same rating-headline redesign as
    // roast-detail.png above.
    await expect(page).toHaveScreenshot("roast-detail-capped.png", { fullPage: true });
  });

  test("'View all' opens the full, scrollable history and Escape closes it", async ({ page }) => {
    await page.getByTestId("trace-view-all").click();

    // The modal shows the COMPLETE set (all 24 rows), in its own table copy so the
    // inline guard above is never ambiguous.
    const dialog = page.getByTestId("trace-modal");
    await expect(dialog).toBeVisible();
    await expect(dialog.locator("[data-testid='decision-trace-table-modal'] [data-testid='trace-row']")).toHaveCount(24);

    // Escape closes and returns focus (a11y, mirroring RecoveryModal).
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
  });
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
    // #566: baseline regenerated for the same rating-headline redesign as
    // roast-detail.png above.
    await expect(page).toHaveScreenshot("roast-detail-advisor-failed.png", { fullPage: true });
  });
});
