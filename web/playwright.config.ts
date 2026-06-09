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
  // Drives the replay harness to the smoke state (session-2 → preheating) after
  // both webServers are up — deterministic real-replay state for the snapshot.
  globalSetup: "./tests/e2e/global-setup.ts",
  // Snapshots are committed and keyed by platform; the Linux baselines (from the
  // pinned image) are the gate. {arg} keeps multiple states in one spec distinct.
  snapshotPathTemplate: "{testDir}/__screenshots__/{testFilePath}/{arg}-{platform}{ext}",
  // The replay-backed specs drive ONE shared stepped run that advances
  // monotonically, so they must run serially — parallel stepping would race on
  // shared backend state. The suite is small; a single worker is fine.
  fullyParallel: false,
  workers: 1,
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
  // Two servers, both started by Playwright:
  //   1. The agent in REPLAY --step mode (the real backend: REST + SSE +
  //      the gated /api/replay/{step,advance-to} control routes). The SPA's
  //      preview proxy forwards /api here, so a replayed roast is byte-identical
  //      to live. session-2 is the default demo fixture (auto-T0 + the CLAMP).
  //   2. The built SPA via `vite preview` (proxying /api to server 1).
  // The deterministic step API + the replay harness are what make the snapshots
  // reproducible — global-setup.ts drives states via advance-to.
  webServer: [
    {
      command:
        "roastpilot-agent --replay tests/fixtures/replay/session-2 --step " +
        "--host 127.0.0.1 --port 8000",
      cwd: "..",
      // health is up as soon as the app mounts (paused at tick 0).
      url: "http://127.0.0.1:8000/api/health",
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
    },
    {
      command: "npm run preview -- --port 4173 --strictPort --host 127.0.0.1",
      // Probe the root (always 200) — `vite preview` has no SPA history fallback,
      // so a deep route 404s the readiness check.
      url: "http://127.0.0.1:4173/",
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
    },
  ],
});
