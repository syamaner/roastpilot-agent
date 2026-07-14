/**
 * Home / landing hub + persistent nav snapshot + data-assert suite (#324, D24/D26,
 * updated #423 D81, #523).
 *
 * Drives the deterministic `/__home-harness` route (the persistent `NavBar` + the
 * `HomePage` hub over a seeded idle `/health` snapshot — no active run), mirroring
 * the `/__start-roast-harness` route-harness convention. The data-assert layer
 * (per D24) is the four entry-point links + the nav links, asserted alongside the
 * pixels so a regression fails on behaviour, not only on the (CI-regenerated)
 * baseline.
 *
 * #523: "Start a new roast" points to `/start` (the ONLY start-form surface under
 * the new IA); "Live/last roast" points to `/live` (never a form — the roaster's
 * permanent state address).
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

test("home — landing hub with all entry points + persistent nav (full-page snapshot)", async ({
  page,
}) => {
  // Data-assert: "Start" → /start (#523 — the only start-form surface);
  // "Live/last roast" → /live (never a form); "View roasts" → /roasts.
  await expect(page.getByTestId("home-start-roast")).toHaveAttribute("href", "/start");
  await expect(page.getByTestId("home-live-roast")).toHaveAttribute("href", "/live");
  await expect(page.getByTestId("home-view-roasts")).toHaveAttribute("href", "/roasts");

  // The persistent nav exposes Home + History; with no active run the Live-roast
  // link is absent (server-derived, never inferred), and no live-status chip shows.
  await expect(page.getByTestId("nav-home")).toBeVisible();
  await expect(page.getByTestId("nav-history")).toBeVisible();
  await expect(page.getByTestId("nav-live-roast")).toHaveCount(0);
  await expect(page.getByTestId("home-live-status-chip")).toHaveCount(0);

  await expect(page).toHaveScreenshot("home.png", { fullPage: true });
});
