/**
 * Bean-profile library snapshot + data-assert suite (#303, D24/D26).
 *
 * Drives the deterministic `/__start-roast-harness` route (the real `StartRoastForm`
 * over fixed fixture data) — the idle Start page is not reachable via the replay
 * agents (they always carry an active run), so this mirrors the `/__chart-harness`
 * + `/__detail-harness` route-harness convention.
 *
 * Two baselines:
 *   - start-roast            → the idle form with the saved-profile dropdown
 *   - start-roast-add-modal  → the add-profile modal open
 *
 * The data-assert layer (per D24) is the dropdown options + the fields filling from
 * a selected profile + the modal opening — asserted alongside the pixels so a
 * regression fails on behaviour, not only on the (regenerated) baseline.
 */

import { expect, test } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await page.goto("/__start-roast-harness");
  await expect(page.getByTestId("start-roast-form")).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
});

test("start-roast — idle form with the saved bean-profile dropdown (full-page snapshot)", async ({
  page,
}) => {
  // The dropdown renders the saved library incl. the built-in Ethiopia Koke seed
  // (data-assert: the option text, not just the control's presence).
  const select = page.getByTestId("bean-profile-select");
  await expect(select).toBeVisible();
  await expect(select).toContainText("Ethiopia Yirgacheffe Koke (Natural)");
  await expect(select).toContainText("Colombia Huila (Washed)");

  await expect(page).toHaveScreenshot("start-roast.png");
});

test("selecting a saved profile fills the form + pre-fills the per-roast weight (data-assert)", async ({
  page,
}) => {
  await page
    .getByTestId("bean-profile-select")
    .selectOption("seed-ethiopia-yirgacheffe-koke-natural");

  // The identity + target fields fill from the selected profile, and the per-roast
  // charge weight pre-fills from default_bean_weight_grams (250 g) — adjustable.
  await expect(page.getByTestId("start-roast-name")).toHaveValue(
    "Ethiopia Yirgacheffe Koke (Natural)",
  );
  await expect(page.getByTestId("start-roast-bean_origin")).toHaveValue("Ethiopia");
  await expect(page.getByTestId("start-roast-processing")).toHaveValue("natural");
  await expect(page.getByTestId("start-roast-target_drop_temp_c")).toHaveValue("190");
  await expect(page.getByTestId("start-roast-bean_weight_grams")).toHaveValue("250");
});

test("start-roast-add-modal — the add-profile modal open (full-page snapshot)", async ({
  page,
}) => {
  await page.getByTestId("bean-profile-add-button").click();
  await expect(page.getByTestId("bean-profile-modal")).toBeVisible();
  await expect(page.getByTestId("bean-profile-save")).toBeVisible();
  await page.evaluate(() => document.fonts.ready);

  await expect(page).toHaveScreenshot("start-roast-add-modal.png");
});

test("the edit pencil opens the edit modal for the selected profile (data-assert)", async ({
  page,
}) => {
  await page
    .getByTestId("bean-profile-select")
    .selectOption("seed-ethiopia-yirgacheffe-koke-natural");
  await page.getByTestId("bean-profile-edit-button").click();
  await expect(page.getByTestId("bean-profile-modal")).toBeVisible();
  // The edit modal is pre-filled from the selected profile, with the
  // future-roasts-only note.
  await expect(page.getByTestId("bean-profile-name")).toHaveValue(
    "Ethiopia Yirgacheffe Koke (Natural)",
  );
  await expect(page.getByText(/affect future roasts only/i)).toBeVisible();
});
