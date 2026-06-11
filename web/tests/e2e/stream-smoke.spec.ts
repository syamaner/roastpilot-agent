/**
 * Real-replay smoke (D24 / D26, S2 scope).
 *
 * Proves the foundation's live data path end-to-end against the REAL replay
 * harness (S1): the agent booted in `--replay session-2 --step`, the test advances
 * it to `preheating` and steps a few ticks, and the SPA's `/__stream-smoke` route
 * hydrates from `GET /api/roasts/{id}` + applies typed SSE frames. We assert the
 * SERVER-DERIVED phase reached the SPA (not inferred locally) and take one page
 * snapshot — proving the webServer + deterministic stepping work. (`/__stream-smoke`
 * has no LiveCurve, so there is no canvas here; under D26 the product pages' canvas
 * is un-masked.) Product page-state snapshots land with S3–S6 (the full
 * fixture→marker matrix), reusing this exact path.
 */

import { expect, test } from "@playwright/test";

import { advanceTo, AGENTS, step } from "./global-setup";

// One test against the single shared stepped run (the lead-scoped S2 smoke).
// Open the page FIRST so the SSE stream connects, THEN advance the replay — the
// stepped frames flow to the live browser and the reducer publishes their ids on
// window.__lastEventId (set on applied SSE frames, not on hydration). Then wait
// until the browser caught up to the step's last_event_id, assert the
// server-derived phase reached the SPA, and snapshot the chrome.
test("real-replay smoke — server phase reaches the SPA, chrome snapshot", async ({
  page,
}) => {
  await page.goto("/__stream-smoke");
  await expect(page.getByTestId("connection-indicator")).toHaveAttribute(
    "data-status",
    "live",
    { timeout: 15_000 },
  );

  // `preheating` is the tick-0 marker (emits no new frames), so step a few ticks
  // INTO preheating: each tick emits a telemetry SSE frame the live browser
  // applies, advancing window.__lastEventId. The window is long (T0 is ~496 s in),
  // so 5 ticks stays in preheating.
  const reached = await advanceTo(AGENTS.session2, "preheating");
  expect(reached.agent_phase).toBe("preheating");
  const stepped = await step(AGENTS.session2, 5);
  expect(stepped.agent_phase).toBe("preheating");
  await page.waitForFunction(
    (id) => (window.__lastEventId ?? -1) >= id,
    stepped.last_event_id,
    { timeout: 15_000 },
  );

  // Phase comes from the server, surfaced by the reducer — assert it landed.
  await expect(page.getByTestId("smoke-phase")).toHaveText("preheating");
  await page.evaluate(() => document.fonts.ready);
  await expect(page).toHaveScreenshot("stream-smoke-preheating.png");
});
