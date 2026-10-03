/**
 * #954 U3 read-only cold-characterisation view (E1, E2).
 *
 * Flow A snapshots the dev/test harness (synthetic, banner-labelled) in its two
 * fixed states: settle on DOM test IDs and `document.fonts.ready`; there is no
 * live-curve canvas, so both are DOM_PAGE targets.
 *
 * Flow B drives the PRODUCTION route with the real browser `EventSource`:
 * `page.route` fulfils the cold path with the committed contract fixture's real
 * frame bytes plus hostile frames, while every other `/api/` request is counted and
 * aborted (the count must stay 0). Static assets are not intercepted. No screenshot.
 */

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { expect, test } from "@playwright/test";

import { expectScreenshot, SCREENSHOT_CLASSES } from "./visualBudgets";

interface FixtureFrame {
  name: string;
  source: string;
  frame_text: string;
  id: string | null;
  event: "observation" | "heartbeat";
  data: string;
}

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURE_PATH = resolve(HERE, "../../../tests/fixtures/contract/cold_observation_frames.json");
const COLD_PATH = "/api/cold-characterisation/events";

test.describe("Flow A — harness snapshots", () => {
  test("observed harness state", async ({ page }) => {
    await page.goto("/__cold-characterisation-harness");
    await expect(page.getByTestId("cold-synthetic-banner")).toBeVisible();
    await expect(page.getByTestId("cold-shown-count")).toHaveText("2");
    await expect(page.getByTestId("cold-refused-count")).toHaveText("1");
    await expect(page.getByTestId("cold-transport")).toHaveAttribute("data-status", "connected");
    await expect(page.getByTestId("cold-roast-fan")).toHaveText("Observed, 40 %");
    await page.evaluate(() => document.fonts.ready);
    await expectScreenshot(page, "cold-characterisation-observed.png", SCREENSHOT_CLASSES.DOM_PAGE);
  });

  test("awaiting harness state", async ({ page }) => {
    await page.goto("/__cold-characterisation-harness?state=awaiting");
    await expect(page.getByTestId("cold-synthetic-banner")).toBeVisible();
    await expect(page.getByTestId("cold-transport")).toHaveAttribute("data-status", "connected");
    await expect(page.getByTestId("cold-shown-count")).toHaveText("0");
    await expect(page.getByTestId("cold-bean")).toHaveText("— °C");
    await page.evaluate(() => document.fonts.ready);
    await expectScreenshot(page, "cold-characterisation-awaiting.png", SCREENSHOT_CLASSES.DOM_PAGE);
  });
});

test.describe("Flow B — production route over real fixture frames", () => {
  test("admits real frames, refuses hostile ones, and makes no other API request", async ({
    page,
  }) => {
    const fixture = JSON.parse(readFileSync(FIXTURE_PATH, "utf8")) as { frames: FixtureFrame[] };
    const observations = fixture.frames.filter((frame) => frame.event === "observation");
    const heartbeat = fixture.frames.find((frame) => frame.event === "heartbeat");
    const first = observations[0];
    const last = observations.at(-1);
    if (first === undefined || last === undefined || heartbeat === undefined) {
      throw new Error("cold contract fixture is missing frames");
    }
    // The final frame is the fixture's public-model-only case; its values are pinned
    // here as independently known literals, not recomputed with the view's formatter.
    expect(last.name).toBe("observed_null_level");
    const canonical = JSON.parse(first.data) as Record<string, unknown>;
    const observationFrame = (id: string, data: string) =>
      `id: ${id}\nevent: observation\ndata: ${data}\n\n`;

    const hostile = {
      duplicateId: first.frame_text,
      extraKey: observationFrame(
        "0123456789abcdef-1001",
        JSON.stringify({ ...canonical, extra: "HOSTILE-EXTRA" }),
      ),
      oversize: observationFrame("0123456789abcdef-1002", `${first.data}${" ".repeat(4096)}`),
      malformedHeartbeat: 'event: heartbeat\ndata: {"HOSTILE":1}\n\n',
      bidiUtc: observationFrame(
        "0123456789abcdef-1003",
        JSON.stringify({ ...canonical, recorded_at_utc: "2026-01-01T00:00:01\u202e+00:00" }),
      ),
    };
    // An event name with no registered listener: the browser never delivers it to
    // the page, so it is neither shown nor counted.
    const unknownEvent = 'event: unknown_kind\ndata: {"UNKNOWN-EVENT-CANARY":1}\n\n';
    const body = [
      ": connected\n\n",
      first.frame_text,
      hostile.duplicateId,
      hostile.extraKey,
      unknownEvent,
      ...observations.slice(1, -1).map((frame) => frame.frame_text),
      heartbeat.frame_text,
      hostile.oversize,
      hostile.malformedHeartbeat,
      hostile.bidiUtc,
      last.frame_text,
    ].join("");

    const coldRequests: string[] = [];
    let otherApiRequests = 0;
    await page.route("**/api/**", async (route) => {
      const url = new URL(route.request().url());
      if (url.pathname !== COLD_PATH) {
        otherApiRequests += 1;
        await route.abort();
        return;
      }
      coldRequests.push(url.search);
      // The first stream delivers the frames; later ones end straight after the
      // connected comment (lazy admission lost after HTTP 200).
      await route.fulfill({
        status: 200,
        headers: { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" },
        body: coldRequests.length === 1 ? body : ": connected\n\n",
      });
    });

    await page.goto("/cold-characterisation");
    await expect(page.getByTestId("cold-view")).toBeVisible();
    await expect(page.getByTestId("cold-shown-count")).toHaveText(String(observations.length));
    await expect(page.getByTestId("cold-refused-count")).toHaveText(
      String(Object.keys(hostile).length),
    );
    await expect(page.getByTestId("cold-last-utc")).toHaveText("2026-01-01T00:00:01.250000+00:00");
    await expect(page.getByTestId("cold-phase")).toHaveText("Recording on");
    await expect(page.getByTestId("cold-bean")).toHaveText("21.5 °C");
    await expect(page.getByTestId("cold-roast-fan")).toHaveText("Observed");
    await expect(page.getByTestId("cold-synthetic-banner")).toHaveCount(0);
    await expect(page.locator("body")).not.toContainText("HOSTILE");
    await expect(page.locator("body")).not.toContainText("UNKNOWN-EVENT-CANARY");

    // The explicit reconnect carries the exact last ID string, and an empty 200
    // stream is reported honestly as reconnecting.
    await expect.poll(() => coldRequests.length, { timeout: 10_000 }).toBeGreaterThanOrEqual(2);
    expect(coldRequests[0]).toBe("");
    expect(new URLSearchParams(coldRequests[1]).get("last_event_id")).toBe(last.id);
    await expect(page.getByTestId("cold-transport")).toHaveAttribute(
      "data-status",
      "reconnecting",
      { timeout: 10_000 },
    );
    expect(await page.evaluate(() => "__lastEventId" in window)).toBe(false);
    expect(otherApiRequests).toBe(0);
  });
});
