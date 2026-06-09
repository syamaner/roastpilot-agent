// Screenshot capture for the RoastPilot SPA — the `/capture` skill's scripted
// fallback (the skill prefers the Playwright MCP). Ported from the sketches'
// capture.mjs pattern: playwright-core + the system Google Chrome channel (no
// heavy browser download). LOCAL, NON-GATING — the CI gate is the scripted
// @playwright/test toHaveScreenshot() suite in the pinned Linux image (D24).
//
// Usage:
//   node scripts/capture.mjs <state> [--url <baseUrl>] [--out <dir>]
//
// State → route map (the foundation only ships `foundation`; S3–S6 add the
// product page states — dashboard-live, dashboard-recovery, dashboard-fault,
// roast-detail, roast-detail-selected, history, history-empty — driven by the
// replay harness):
//   foundation → /__chart-harness   (the shared-component harness, S2)

import { chromium } from "playwright-core";

const args = process.argv.slice(2);
const state = args.find((a) => !a.startsWith("--")) ?? "foundation";
const baseUrl = flag("--url") ?? process.env.ROASTPILOT_WEB_URL ?? "http://127.0.0.1:4173";
const outDir = flag("--out") ?? "test-results/captures";

function flag(name) {
  const i = args.indexOf(name);
  return i >= 0 ? args[i + 1] : undefined;
}

const ROUTES = {
  foundation: "/__chart-harness",
};

const route = ROUTES[state];
if (!route) {
  console.error(
    `unknown state: ${state}. Known: ${Object.keys(ROUTES).join(", ")}. ` +
      `(Product page states arrive with S3–S6 + the replay harness.)`,
  );
  process.exit(2);
}

const browser = await chromium.launch({ channel: "chrome" });
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
await page.goto(`${baseUrl}${route}`, { waitUntil: "networkidle" });
await page.evaluate(() => document.fonts.ready);
await page.waitForSelector("[data-chart-ready='true']", { timeout: 10_000 });

const path = `${outDir}/${state}.png`;
await page.screenshot({ path });
await browser.close();
console.log(`captured: ${path} (state=${state}, route=${route})`);
