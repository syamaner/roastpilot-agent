/**
 * Bean-profile library snapshot + data-assert suite (#303, D24/D26).
 *
 * Drives the deterministic `/__start-roast-harness` route (the real `StartRoastForm`
 * over fixed fixture data) — the idle Start page is not reachable via the replay
 * agents (they always carry an active run), so this mirrors the `/__chart-harness`
 * + `/__detail-harness` route-harness convention.
 *
 * Four baselines:
 *   - start-roast                       → the idle form with the saved-profile dropdown
 *   - start-roast-add-modal             → the add-profile modal open (full page)
 *   - start-roast-add-modal-draft-panel → JUST the draft-from-URL panel (#637), scoped
 *     to its container locator rather than the full page. The full-page shot's 1%
 *     `maxDiffPixelRatio` tolerance (tuned to absorb un-masked-canvas AA noise
 *     elsewhere in the suite) is loose enough that this panel's dark-on-dark, modest
 *     footprint can appear or vanish within budget without failing the full-page
 *     comparison — confirmed empirically: adding the panel moved only ~1% of the
 *     full-page pixels by pixelmatch's perceptual metric, right at the threshold. A
 *     locator-scoped shot of a small region has no such headroom: if the panel
 *     disappears, `toHaveScreenshot` on its locator fails outright (element not
 *     found / zero-size) before any pixel math runs, so this closes that gap
 *     without loosening or fighting the full-page tolerance.
 *   - start-roast-add-modal-catalogue-results → JUST the populated catalogue
 *     recommendation panel (#573), proving the result-card hierarchy without
 *     diluting it into the full-page tolerance.
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

  await expect(page).toHaveScreenshot("start-roast.png", { fullPage: true });
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

  await expect(page).toHaveScreenshot("start-roast-add-modal.png", { fullPage: true });
});

test("start-roast-add-modal — the draft-from-URL panel, scoped snapshot (#637)", async ({
  page,
}) => {
  // A locator-scoped shot of just the draft-from-URL panel (#637): the full-page
  // shot above budgets a 1% maxDiffPixelRatio (tuned for the un-masked canvas
  // elsewhere in the suite), which is loose enough that this panel's addition can
  // sit within tolerance and never fail the full-page comparison. Scoping to the
  // panel's own locator removes that headroom — if the panel is missing entirely,
  // `toHaveScreenshot` fails on the locator itself, not a diluted page-wide ratio.
  await page.getByTestId("bean-profile-add-button").click();
  const panel = page.getByTestId("bean-profile-draft-panel");
  await expect(panel).toBeVisible();
  await expect(page.getByTestId("bean-profile-draft-url")).toBeVisible();
  await page.evaluate(() => document.fonts.ready);

  await expect(panel).toHaveScreenshot("start-roast-add-modal-draft-panel.png");
});

test("catalogue recommendations hand the selected server URL to draft-and-review (#573)", async ({
  page,
}) => {
  const catalogueUrl = "https://vendor.example.com/collections/filter-coffee";
  const selectedProductUrl = "https://vendor.example.com/products/kiambu-aa?lot=42";

  await page.route("**/api/beans/recommend-from-catalogue", async (route) => {
    expect(route.request().method()).toBe("POST");
    expect(route.request().postDataJSON()).toEqual({ url: catalogueUrl });
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        recommendations: [
          {
            candidate_id: "candidate-01",
            product_url: selectedProductUrl,
            name: "Kenya Kiambu AA",
            country: "Kenya",
            processing: "washed",
            score: 86,
            reason_codes: ["novel_country_processing", "rated_pair_affinity"],
            reasons: [
              "Adds a country and process pairing that is new to the saved library.",
              "Similar washed coffees have previously scored well.",
            ],
          },
          {
            candidate_id: "candidate-02",
            product_url: "https://vendor.example.com/products/huila-decaf",
            name: "Colombia Huila Decaf",
            country: "Colombia",
            processing: "washed",
            score: 71,
            reason_codes: ["rated_pair_affinity"],
            reasons: ["Similar washed coffees have previously scored well."],
          },
        ],
        discovered_count: 8,
        extracted_count: 5,
      }),
    });
  });

  await page.route("**/api/beans/draft-from-url", async (route) => {
    expect(route.request().method()).toBe("POST");
    expect(route.request().postDataJSON()).toEqual({ url: selectedProductUrl });
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        draft_attempt_id: "attempt-catalogue-1",
        name: "Kenya Kiambu AA",
        bean_origin: "Kiambu",
        bean_varietal: "SL28",
        country: "Kenya",
        farm: "Mikari Estate",
        description: "Blackcurrant, grapefruit, and brown sugar",
        bean_species: "arabica",
        is_blend: false,
        processing: "washed",
        altitude_m: 1850,
        source_url: selectedProductUrl,
        charge_guidance_min_c: 175,
        charge_guidance_max_c: 185,
        initial_heat_percent: 80,
        initial_fan_percent: 20,
        target_drop_temp_c: 201,
        target_development_percent: 16,
        default_bean_weight_grams: 250,
        field_sources: {
          name: "on_page",
          bean_origin: "on_page",
          processing: "on_page",
          altitude_m: "on_page",
          target_drop_temp_c: "origin_estimated",
        },
        field_evidence: {
          name: "Kenya Kiambu AA",
          bean_origin: "Kiambu, Kenya",
          processing: "Washed process",
          altitude_m: "Grown at 1,850 metres",
        },
        scouting_note: "Conservative first-roast targets — review before saving.",
      }),
    });
  });

  await page.getByTestId("bean-profile-add-button").click();
  await page.getByTestId("bean-profile-catalogue-url").fill(catalogueUrl);
  await page.getByTestId("bean-profile-catalogue-button").click();

  const panel = page.getByTestId("bean-profile-catalogue-panel");
  await expect(page.getByTestId("bean-profile-catalogue-results")).toBeVisible();
  await expect(panel).toContainText("Kenya Kiambu AA");
  await expect(panel).toContainText("Colombia Huila Decaf");
  await expect(panel).toContainText("Why this one");
  await expect(panel).toContainText(
    "Adds a country and process pairing that is new to the saved library.",
  );
  await page.evaluate(() => document.fonts.ready);
  await expect(panel).toHaveScreenshot("start-roast-add-modal-catalogue-results.png");

  await page.getByTestId("bean-profile-catalogue-draft-candidate-01").click();
  await expect(page.getByTestId("bean-profile-draft-ready-status")).toContainText(
    /draft ready/i,
  );
  await expect(page.getByTestId("bean-profile-name")).toHaveValue("Kenya Kiambu AA");
  await expect(page.getByTestId("bean-profile-name")).toBeFocused();
  await expect(page.getByTestId("bean-profile-bean_origin")).toHaveValue("Kiambu");
  await expect(page.getByTestId("bean-profile-processing")).toHaveValue("washed");
  await expect(page.getByTestId("bean-profile-target_drop_temp_c")).toHaveValue("201");

  // Recommendation and drafting are read-only: the modal remains open and the
  // operator must still explicitly submit the existing Save Profile action.
  await expect(page.getByTestId("bean-profile-modal")).toBeVisible();
  await expect(page.getByTestId("bean-profile-save")).toHaveText("Save Profile");
  await expect(page.getByTestId("bean-profile-select")).not.toContainText("Kenya Kiambu AA");
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
