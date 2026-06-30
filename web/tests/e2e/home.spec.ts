/**
 * Home / landing hub + persistent nav snapshot + data-assert suite (#324, D24/D26,
 * updated #423 D81).
 *
 * Drives the deterministic `/__home-harness` route (the persistent `NavBar` + the
 * `HomePage` hub over a seeded idle `/health` snapshot — no active run), mirroring
 * the `/__start-roast-harness` route-harness convention. The data-assert layer
 * (per D24) is the two entry-point links + the nav links, asserted alongside the
 * pixels so a regression fails on behaviour, not only on the (CI-regenerated)
 * baseline.
 *
 * D81: "Start a new roast" tile now points to `/live` (idle /live = start form;
 * active /live = live dashboard — one URL for both cases).
 *
 * NOTE: the `home` baseline is owned by the CI Docker snapshot job (D26) — it must
 * be (re)generated there, not committed from a local macOS run.
 */

import { expect, test } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await page.goto("/__home-harness");
  await expect(page.getByTestId("home-page")).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
});

test("home — landing hub with both entry points + persistent nav (full-page snapshot)", async ({
  page,
}) => {
  // Data-assert: "Start" → /live (D81 — idle /live shows the start form);
  // "View roasts" → /roasts (unchanged).
  await expect(page.getByTestId("home-start-roast")).toHaveAttribute("href", "/live");
  await expect(page.getByTestId("home-view-roasts")).toHaveAttribute("href", "/roasts");

  // The persistent nav exposes Home + History; with no active run the Live-roast
  // link is absent (server-derived, never inferred).
  await expect(page.getByTestId("nav-home")).toBeVisible();
  await expect(page.getByTestId("nav-history")).toBeVisible();
  await expect(page.getByTestId("nav-live-roast")).toHaveCount(0);

  await expect(page).toHaveScreenshot("home.png");
});
