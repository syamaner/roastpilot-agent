/**
 * Microphone capture-alive status icon snapshots (#197, D26).
 *
 * The header mic icon is tinted by the SERVER-DERIVED `mic_status.mic_health`
 * (green ok / red error / amber idle), with the raw capture-alive fields in a
 * hover tooltip. Two states are pinned:
 *
 *   - mic-green (ok)    → the REAL session-2 replay harness, which synthesizes a
 *                         capture-alive `mic_health: "ok"` on every telemetry frame.
 *                         The faithful "render from server data only" path.
 *   - mic-error (red)   → the replay harness only ever emits `ok` (it has no
 *                         real device to fail), so a faithful red state can't be
 *                         stepped out of it. We instead route-mock the HYDRATE
 *                         snapshot (`GET /api/roasts/{id}`) to carry
 *                         `mic_health: "error"` — still pure server data the SPA
 *                         renders, deterministic, and decoupled from the fixture
 *                         (the same self-contained `page.route` technique the
 *                         history suite uses for its REST states). The replay is
 *                         left PAUSED at tick 0 so no live telemetry frame
 *                         overrides the snapshot's mic_status.
 *
 * Both shots include the canvas un-masked (D26); the mic data-assert via the DOM
 * `data-health` attribute is the authoritative correctness layer alongside the
 * pixels.
 */

import { expect, test } from "@playwright/test";

import { advanceTo, AGENTS, step } from "./global-setup";
import { settle, settleStepped } from "./helpers";
import { WEB_URLS } from "./urls";

test("mic-green — capture-alive OK mic renders the green icon (real replay, server-derived)", async ({
  page,
}) => {
  // #423: / is now a pure launcher; the live dashboard lives at /live.
  await page.goto(`${WEB_URLS.session2}/live`);
  await expect(page.getByTestId("connection-indicator")).toHaveAttribute("data-status", "live", {
    timeout: 15_000,
  });

  // Step a few ticks into preheating so telemetry frames (each carrying the
  // synthesized `mic_health: "ok"`) flow to the live browser. A small additive `step`
  // (4, or 8 on a retry — both well below T0 at ~frame 99) stays retry-safe and keeps
  // the shared session-2 agent independent of the other preheating specs. The lossless
  // `settleStepped` replaces the flaky `__lastEventId` wait (#338); the mic
  // `data-health` assertion below is the real check (it retries).
  await advanceTo(AGENTS.session2, "preheating");
  const stepped = await step(AGENTS.session2, 4);
  expect(stepped.agent_phase).toBe("preheating");
  await settleStepped(page, stepped);

  // The icon is tinted by the server-derived health — assert the DATA (the
  // authoritative layer alongside the pixels): a capture-alive mic reads OK/green.
  const mic = page.getByTestId("mic-status");
  await expect(mic).toHaveAttribute("data-health", "ok");
  await expect(mic).toContainText(/mic ok/i);

  await settle(page);
  await expect(page).toHaveScreenshot("mic-green.png", { fullPage: true });
});

test("mic-error — a faulted capture pipeline renders the red icon (snapshot-mocked, never green)", async ({
  page,
}) => {
  // Route-mock the hydrate snapshot to carry an ERROR mic. The replay harness can
  // only synthesize `ok` (no real device to fail), so this is the deterministic
  // way to pin the red state — still pure server data the SPA renders, never
  // client-inferred. The replay is left PAUSED (no `step`), so no live telemetry
  // frame arrives to override the snapshot's mic_status.
  // Match ONLY the run-detail snapshot `/api/roasts/{id}` — not its sub-resources
  // (`/telemetry`, `/events`, `/timeline`) nor the history list (`/api/roasts`).
  await page.route(/\/api\/roasts\/[^/?]+(\?.*)?$/, async (route) => {
    const response = await route.fetch();
    const detail = (await response.json()) as Record<string, unknown>;
    detail.mic_status = {
      mic_health: "error",
      audio_running: false,
      fc_status: "unavailable",
      queued_window_count: 0,
      emitted_window_count: 0,
      dropped_window_count: 0,
      processed_window_count: 0,
      reason: "audio device not available",
    };
    await route.fulfill({ response, json: detail });
  });

  // #423: / is now a pure launcher; the live dashboard lives at /live.
  await page.goto(`${WEB_URLS.session2}/live`);
  await expect(page.getByTestId("connection-indicator")).toHaveAttribute("data-status", "live", {
    timeout: 15_000,
  });

  // The header paints from the (mocked) snapshot mic_status before any telemetry
  // frame — assert the DATA: a faulted pipeline reads ERROR/red, NEVER green.
  const mic = page.getByTestId("mic-status");
  await expect(mic).toHaveAttribute("data-health", "error");
  await expect(mic).toContainText(/mic error/i);

  await settle(page);
  await expect(page).toHaveScreenshot("mic-error.png", { fullPage: true });
});
