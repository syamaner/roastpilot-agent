/**
 * Home / landing hub + persistent nav snapshot + data-assert suite (#324, D24/D26).
 *
 * Drives the deterministic `/__home-harness` route (the persistent `NavBar` + the
 * `HomePage` hub over a seeded idle `/health` snapshot — no active run), mirroring
 * the `/__start-roast-harness` route-harness convention. The data-assert layer
 * (per D24) is the two entry-point links + the nav links, asserted alongside the
 * pixels so a regression fails on behaviour, not only on the (CI-regenerated)
 * baseline.
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
  // Data-assert: both hub entry points route to the Start form + the history list.
  await expect(page.getByTestId("home-start-roast")).toHaveAttribute("href", "/start");
  await expect(page.getByTestId("home-view-roasts")).toHaveAttribute("href", "/roasts");

  // The persistent nav exposes Home + History; with no active run the Live-roast
  // link is absent (server-derived, never inferred).
  await expect(page.getByTestId("nav-home")).toBeVisible();
  await expect(page.getByTestId("nav-history")).toBeVisible();
  await expect(page.getByTestId("nav-live-roast")).toHaveCount(0);

  await expect(page).toHaveScreenshot("home.png");
});
