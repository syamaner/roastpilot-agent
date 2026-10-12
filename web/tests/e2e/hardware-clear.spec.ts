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
          // This route test isolates the incident-bound acknowledgement. The
          // D213 process gate is already cleared, and the lease is closed, so
          // neither independent gate masks the hardware-clear card.
          restart_clearance: { state: "cleared", eligible: true },
          fault_controls: { status: "closed", generation: null, enabled_actions: [] },
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

test("required restart clearance keeps a CLOSED server-advertised software e-stop visible", async ({
  page,
}) => {
  let clearanceRequired = true;
  const emergencyStopRequests: unknown[] = [];
  // `useFreshHealthGate` polls every three seconds. Freeze page time before
  // navigation so the test controls the sole post-clearance health read.
  await page.clock.install();
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
          mcp_hardware_clear_required: false,
          mcp_teardown_incident_id: null,
          active_run_id: null,
          restart_clearance: clearanceRequired
            ? { state: "required", eligible: false }
            : { state: "cleared", eligible: true },
          fault_controls: clearanceRequired
            ? { status: "closed", generation: null, enabled_actions: ["emergency_stop"] }
            : { status: "closed", generation: null, enabled_actions: [] },
        }),
      });
      return;
    }
    if (path === "/api/fault-controls/actions") {
      emergencyStopRequests.push(request.postDataJSON());
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          action: "emergency_stop",
          result: "accepted",
          reason: "request accepted",
          queued: true,
          fault_controls: { status: "closed", generation: null, enabled_actions: ["emergency_stop"] },
        }),
      });
      return;
    }
    if (path === "/api/restart-clearance") {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          outcome: "confirmed",
          cleared: true,
          restart_clearance_required: false,
          restart_clearance: { state: "cleared", eligible: true },
          fault_controls: { status: "closed", generation: null, enabled_actions: [] },
        }),
      });
      return;
    }
    if (path === "/api/roasts") {
      await route.fulfill({ contentType: "application/json", body: JSON.stringify({ runs: [] }) });
      return;
    }
    if (path === "/api/bean-profiles") {
      await route.fulfill({ contentType: "application/json", body: JSON.stringify({ profiles: [] }) });
      return;
    }
    await route.abort();
  });

  await page.goto("/start");
  await expect(page.getByTestId("restart-clearance-required")).toBeVisible();
  await expect(page.getByTestId("start-roast-form")).toHaveCount(0);
  await expect(page.getByTestId("start-restart-clearance-emergency-stop")).toBeVisible();
  await expect(page.getByText(/software request cannot confirm physical state here/i)).toBeVisible();

  const emergencyStop = page.getByTestId("start-restart-clearance-emergency-stop-submit");
  await emergencyStop.click();
  await emergencyStop.click();
  await expect.poll(() => emergencyStopRequests).toEqual([{ action: "emergency_stop" }]);

  await page.getByTestId("restart-clearance-open").click();
  await page.getByTestId("restart-clearance-stopped").check();
  await page.getByTestId("restart-clearance-empty").check();
  await page.getByTestId("restart-clearance-independent-stop").check();
  await page.getByTestId("restart-clearance-submit").click();
  await expect(page.getByTestId("restart-clearance-reported")).toBeVisible();
  await expect(page.getByTestId("restart-clearance-required")).toBeVisible();
  await expect(page.getByTestId("start-roast-form")).toHaveCount(0);

  // The POST records only the operator report. The server projection remains
  // REQUIRED until the test author advances it, so this assertion fails if the
  // client unlocks Start optimistically from the successful POST response.
  clearanceRequired = false;
  await page.getByTestId("restart-clearance-refresh-status").click();
  await expect(page.getByTestId("start-roast-form")).toBeVisible();
  await expect(page.getByTestId("restart-clearance-required")).toHaveCount(0);
});
