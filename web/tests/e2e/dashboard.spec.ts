/**
 * Live dashboard snapshot — `dashboard-live` (S3, D24).
 *
 * Drives the REAL replay harness (session-2, the existing webServer) to the
 * `preheating` marker so the charge band shows, then snapshots the live
 * dashboard's DOM chrome (canvas masked) + asserts the chart DATA via the hook.
 * Phase reaches the SPA from the server (hydrate snapshot + SSE), never inferred.
 *
 * The `dashboard-fault` and `dashboard-recovery` states need DIFFERENT replay
 * fixtures (session-1 / fault-pre-t0), which need a multi-fixture harness — that
 * is built once in S6 (so the three page PRs don't each fork playwright.config).
 * Those two snapshots are deferred to S6; their components are covered by the
 * Vitest interaction tests here in S3.
 */

import { expect, test } from "@playwright/test";

import { advanceTo, step } from "./global-setup";
import { maskCanvas, readChartData, settle } from "./helpers";

test("dashboard-live — preheating with the charge band, masked chrome snapshot", async ({
  page,
}) => {
  await page.goto("/");

  // Phase + telemetry come from the server: wait until the stream is live.
  await expect(page.getByTestId("connection-indicator")).toHaveAttribute(
    "data-status",
    "live",
    { timeout: 15_000 },
  );

  // `preheating` is the tick-0 marker (emits no new frames); step a few ticks INTO
  // preheating so telemetry frames flow to the live browser and the curve builds.
  const reached = await advanceTo("preheating");
  expect(reached.agent_phase).toBe("preheating");
  const stepped = await step(8);
  expect(stepped.agent_phase).toBe("preheating");
  await page.waitForFunction(
    (id) => (window.__lastEventId ?? -1) >= id,
    stepped.last_event_id,
    { timeout: 15_000 },
  );

  // The phase badge reflects the server's preheating phase.
  await expect(page.getByTestId("phase-badge")).toHaveAttribute("data-phase", "preheating");

  // The curve built from the stepped telemetry, and the charge band shows in
  // preheating (asserted via DATA, not pixels — D24).
  const hook = await readChartData(page);
  expect(hook.columns[0].length).toBeGreaterThan(0);
  expect(hook.chargeBandVisible).toBe(true);

  await settle(page);
  await expect(page).toHaveScreenshot("dashboard-live.png", {
    mask: maskCanvas(page),
  });
});
