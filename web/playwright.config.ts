import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright snapshot + e2e config (D24).
 *
 * TWO TRACKS, split by job:
 *   - THE CI GATE is this scripted `toHaveScreenshot()` suite. It MUST run inside
 *     the pinned `mcr.microsoft.com/playwright:v1.55.1` Linux image
 *     (`--platform=linux/amd64`) so the committed PNG baselines match the GitHub
 *     runner — local macOS pixels drift and would flap the gate. Baselines are
 *     generated/updated INSIDE that image (see tests/e2e/README.md + the CI job).
 *   - The `/capture` skill + `ui-reviewer` (Playwright MCP) use LOCAL system
 *     Chrome for direction-match judgment only — NON-GATING (see scripts/capture.mjs).
 *
 * Determinism: fixed viewport, animations disabled, `fonts.ready` awaited in the
 * specs, a small (non-zero) pixel tolerance, and the uPlot canvas is MASKED — its
 * correctness is asserted via the chart-data hook, never pixels.
 */
export default defineConfig({
  testDir: "./tests/e2e",
  // Snapshots are committed and keyed by platform; the Linux baselines (from the
  // pinned image) are the gate. {arg} keeps multiple states in one spec distinct.
  snapshotPathTemplate: "{testDir}/__screenshots__/{testFilePath}/{arg}-{platform}{ext}",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? "github" : "list",
  expect: {
    toHaveScreenshot: {
      // Small but non-zero: absorbs sub-pixel AA noise without hiding real drift.
      maxDiffPixelRatio: 0.02,
      animations: "disabled",
    },
  },
  use: {
    baseURL: process.env.ROASTPILOT_WEB_URL ?? "http://127.0.0.1:4173",
    viewport: { width: 1600, height: 1000 },
    // Deterministic rendering: reduce motion via the emulated media feature.
    contextOptions: { reducedMotion: "reduce" },
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  // Serve the BUILT SPA for snapshots. In S2 the harness targets the deterministic
  // /__chart-harness route (fixed data) so the suite is green before any page
  // exists. S3–S6 add page snapshots backed by the replay harness (S1) — wire
  // that webServer to `roastpilot-agent --replay <fixture>` + the SPA once #93 lands.
  webServer: {
    command: "npm run preview -- --port 4173 --strictPort --host 127.0.0.1",
    // Probe the root (always 200) — `vite preview` has no SPA history fallback,
    // so a deep route like /__chart-harness 404s the readiness check.
    url: "http://127.0.0.1:4173/",
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
});
