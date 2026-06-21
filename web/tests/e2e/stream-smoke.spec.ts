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
// window.__lastEventId (set on applied SSE frames, not on hydration). This is the
// ONE spec whose POINT is SSE delivery, so it keeps an SSE assertion — but a
// TOLERANT one (frames flowed past the hydration baseline), not the exact
// `>= last_event_id` wedge that flaked the gate when a single frame was dropped
// with no Last-Event-ID resume (#338). The screenshot settles on the rendered
// (server-derived) phase, which is lossless (hydrate + SSE).
test("real-replay smoke — server phase reaches the SPA, chrome snapshot", async ({
  page,
}) => {
  await page.goto("/__stream-smoke");
  await expect(page.getByTestId("connection-indicator")).toHaveAttribute(
    "data-status",
    "live",
    { timeout: 15_000 },
  );
  // The browser's id baseline right after hydrate (before we step any new frames).
  const baseline = await page.evaluate(() => window.__lastEventId ?? -1);

  // `preheating` is the tick-0 marker (emits no new frames), so step a few ticks
  // INTO preheating: each tick emits a telemetry SSE frame the live browser applies,
  // advancing window.__lastEventId. The window is long (T0 is ~frame 99), so 5 ticks
  // (or 10 on a retry) stays in preheating. An ADDITIVE `step` is right here — this
  // spec's point is that frames FLOW, so it must emit fresh frames each run, and a
  // small additive step stays retry-safe + independent of the shared agent's cursor.
  await advanceTo(AGENTS.session2, "preheating");
  const stepped = await step(AGENTS.session2, 5);
  expect(stepped.agent_phase).toBe("preheating");

  // SSE-delivery coverage (the point of THIS spec): assert frames actually FLOWED to
  // the browser over SSE — __lastEventId advanced past the hydration baseline. This
  // is tolerant (frames arrived) rather than exact (every frame arrived), so a single
  // dropped frame no longer wedges the gate, but a totally dead stream still fails.
  await page.waitForFunction(
    (base) => (window.__lastEventId ?? -1) > base,
    baseline,
    { timeout: 15_000 },
  );

  // Phase comes from the server, surfaced by the reducer — assert it landed.
  await expect(page.getByTestId("smoke-phase")).toHaveText("preheating");
  await page.evaluate(() => document.fonts.ready);
  await expect(page).toHaveScreenshot("stream-smoke-preheating.png");
});
