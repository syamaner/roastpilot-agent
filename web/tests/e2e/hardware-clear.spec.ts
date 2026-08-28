/** #668 explicit operator hardware-clear acknowledgement visual and interaction coverage. */

import { expect, test } from "@playwright/test";

import { expectScreenshot, SCREENSHOT_CLASSES } from "./visualBudgets";

test.beforeEach(async ({ page }) => {
  await page.goto("/__hardware-clear-harness");
  await expect(page.getByTestId("hardware-clear-required")).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
});

test("hardware-clear warning blocks start and states the physical safety boundary", async ({
  page,
}) => {
  await expect(page.getByText(/physically verified the roaster is inactive/i)).toBeVisible();
  await expect(page.getByText(/never turns heat, fan, or cooling on/i)).toBeVisible();
  await expect(page.getByTestId("start-roast-form")).toHaveCount(0);
  await expectScreenshot(page, "hardware-clear-required.png", SCREENSHOT_CLASSES.DOM_PAGE);
});

test("hardware-clear confirmation requires checkbox and reason, then latches success", async ({
  page,
}) => {
  await page.getByTestId("hardware-clear-open").click();
  const submit = page.getByTestId("hardware-clear-submit");
  await expect(submit).toBeDisabled();
  await page.getByTestId("hardware-clear-physical-check").check();
  await expect(submit).toBeDisabled();
  await page.getByTestId("hardware-clear-reason").fill("Roaster cold; child resources released");
  await expect(submit).toBeEnabled();
  await submit.click();
  await expect(page.getByTestId("hardware-clear-acknowledged")).toBeVisible();
  await expect(page.getByTestId("hardware-clear-submit")).toHaveCount(0);
});

test("route refreshes a rejected stale incident and resets confirmation for the new token", async ({
  page,
}) => {
  let incidentId = "a".repeat(32);
  const submittedIncidents: string[] = [];
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/api/health") {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          status: "ok",
          version: "e2e",
          instance_id: "e2e-instance",
          mcp_child: "stopped",
          mcp_hardware_clear_required: true,
          mcp_teardown_incident_id: incidentId,
          active_run_id: null,
        }),
      });
      return;
    }
    if (path === "/api/mcp/acknowledge-hardware-clear") {
      const body = request.postDataJSON() as { teardown_incident_id: string };
      submittedIncidents.push(body.teardown_incident_id);
      incidentId = "b".repeat(32);
      await route.fulfill({
        status: 409,
        contentType: "application/json",
        body: JSON.stringify({ detail: "incident no longer matches" }),
      });
      return;
    }
    if (path === "/api/roasts") {
      await route.fulfill({ contentType: "application/json", body: JSON.stringify({ runs: [] }) });
      return;
    }
    if (path === "/api/bean-profiles") {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({ profiles: [] }),
      });
      return;
    }
    await route.abort();
  });

  await page.goto("/start");
  await page.getByTestId("hardware-clear-open").click();
  await page.getByTestId("hardware-clear-physical-check").check();
  await page.getByTestId("hardware-clear-reason").fill("Roaster cold; resources released");
  await page.getByTestId("hardware-clear-submit").click();

  await expect(page.getByTestId("hardware-clear-open")).toBeVisible();
  await expect(page.getByTestId("hardware-clear-confirm")).toHaveCount(0);
  expect(submittedIncidents).toEqual(["a".repeat(32)]);
});
