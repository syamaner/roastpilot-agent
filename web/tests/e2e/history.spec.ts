/**
 * History page snapshot suite (E10-S4 / D24).
 *
 * The history page is pure REST (`GET /api/roasts`) — no SSE, no replay
 * stepping — so both states are made deterministic with a Playwright route
 * intercept of `/api/roasts` rather than depending on whatever runs the replay
 * store happens to hold. This keeps the committed PNG baselines stable and
 * decoupled from the fixture's run count.
 *
 * Two required states (kickoff §5): `history` (a populated table) and
 * `history-empty` (the first-run empty state). DOM-chrome `toHaveScreenshot()`;
 * there is no canvas on this page, so nothing is masked.
 */

import { expect, test, type Page } from "@playwright/test";

// A fixed, hand-authored history payload: covers every outcome badge, a rated +
// an unrated run, a null dev %, and (#111) both an FC time and a run that never
// reached first crack (`first_crack_at_utc: null`). Mirrors `RoastSummary`.
const HISTORY = {
  runs: [
    {
      id: "run-a",
      started_at_utc: "2026-06-07T14:00:00Z",
      completed_at_utc: "2026-06-07T14:12:00Z",
      first_crack_at_utc: "2026-06-07T14:09:00Z",
      agent_phase: "complete",
      outcome: "completed",
      bean_origin: "Ethiopian Yirgacheffe",
      bean_varietal: "Medium",
      rating: 5,
      development_percent: 19,
    },
    {
      id: "run-b",
      started_at_utc: "2026-06-06T15:07:00Z",
      completed_at_utc: "2026-06-06T15:18:00Z",
      first_crack_at_utc: "2026-06-06T15:15:00Z",
      agent_phase: "complete",
      outcome: "aborted",
      bean_origin: "Colombian Supremo",
      bean_varietal: "Dark",
      rating: 2,
      development_percent: 21,
    },
    {
      id: "run-c",
      started_at_utc: "2026-06-05T16:14:00Z",
      completed_at_utc: "2026-06-05T16:21:00Z",
      first_crack_at_utc: null,
      agent_phase: "faulted",
      outcome: "faulted",
      bean_origin: "Kenyan AA",
      bean_varietal: null,
      rating: null,
      development_percent: null,
    },
  ],
};

async function mockHistory(page: Page, body: unknown): Promise<void> {
  await page.route("**/api/roasts", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(body),
    }),
  );
}

async function settle(page: Page): Promise<void> {
  await page.evaluate(() => document.fonts.ready);
}

test("history — populated table", async ({ page }) => {
  await mockHistory(page, HISTORY);
  await page.goto("/roasts");
  await expect(page.getByTestId("history-table")).toBeVisible();
  await expect(page.getByTestId("history-row")).toHaveCount(3);
  await settle(page);
  await expect(page).toHaveScreenshot("history.png");
});

test("history-empty — first-run empty state", async ({ page }) => {
  await mockHistory(page, { runs: [] });
  await page.goto("/roasts");
  await expect(page.getByTestId("history-empty")).toBeVisible();
  await settle(page);
  await expect(page).toHaveScreenshot("history-empty.png");
});
