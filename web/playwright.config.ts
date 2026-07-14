import { defineConfig, devices } from "@playwright/test";

import { WEB_URLS } from "./tests/e2e/urls";

/**
 * Playwright snapshot + e2e config (D26 — revises D24).
 *
 * TWO TRACKS, split by job:
 *   - THE CI GATE is this scripted `toHaveScreenshot()` suite. It MUST run inside
 *     the pinned `mcr.microsoft.com/playwright:v1.55.1-noble` Linux image
 *     (`--platform=linux/amd64`) so the committed PNG baselines match the GitHub
 *     runner — local macOS pixels drift and would flap the gate. Baselines are
 *     generated/updated INSIDE that image only (see tests/e2e/README.md + the CI
 *     `web-snapshots-update` workflow_dispatch job) — NEVER on macOS.
 *   - The `/capture` skill + `ui-reviewer` (Playwright MCP) use LOCAL system
 *     Chrome for direction-match judgment only — NON-GATING (see scripts/capture.mjs).
 *
 * D26: the uPlot canvas is NO LONGER masked — the chart is included in every page
 * screenshot. Determinism kit: `deviceScaleFactor: 1` (uPlot scales its backing
 * store by DPR), the specs await the `window.__chart` point-count before shooting
 * (`waitForChartPoints`), `fonts.ready` + animations off, replay-fixed data, and a
 * small NON-ZERO `maxDiffPixelRatio`. The `window.__chart` data-assert STAYS as the
 * authoritative correctness layer alongside the pixels (D26 §4).
 *
 * MULTI-FIXTURE HARNESS: the dashboard at `/` renders the live SSE stream of
 * whatever replay fixture its agent is running, so the three dashboard states need
 * three different fixtures. We boot one agent + one `vite preview` per fixture on
 * distinct ports; each preview's `/api` proxy targets its own agent (vite reads
 * ROASTPILOT_API at preview-START, so no rebuild per target). Specs pick the
 * matching preview via its baseURL (see WEB_URLS). The route-harness pages
 * (foundation / detail / history) are fixture-independent and use the session-2
 * preview (the suite baseURL). The per-fixture preview origins live in
 * ./tests/e2e/urls.ts (shared with the specs).
 */

export default defineConfig({
  testDir: "./tests/e2e",
  // Confirms every replay agent's gated step surface is mounted (fail fast if one
  // was booted without --step) after all webServers are up.
  globalSetup: "./tests/e2e/global-setup.ts",
  // Snapshots are committed and keyed by platform; the Linux baselines (from the
  // pinned image) are the gate. {arg} keeps multiple states in one spec distinct.
  snapshotPathTemplate: "{testDir}/__screenshots__/{testFilePath}/{arg}-{platform}{ext}",
  // The replay-backed specs drive stepped runs that advance monotonically, so they
  // must run serially — parallel stepping would race on shared backend state. The
  // suite is small; a single worker is fine.
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? "github" : "list",
  expect: {
    toHaveScreenshot: {
      // Small but non-zero (D26): absorbs sub-pixel AA noise from the un-masked
      // canvas without hiding real drift. NEVER widened to paper over a flake —
      // fix determinism (point-count gate / DPR / fonts) instead.
      maxDiffPixelRatio: 0.01,
      animations: "disabled",
      // #530: NOTE — `fullPage` is NOT a valid key here (Playwright's
      // `toHaveScreenshot` project-wide default type omits it; it only exists on
      // the per-call `expect(page).toHaveScreenshot(name, { fullPage: true })`
      // signature, confirmed by `tsc -b`). Every `expect(page).toHaveScreenshot()`
      // call site sets it explicitly instead — see each `*.spec.ts` file.
    },
  },
  use: {
    baseURL: WEB_URLS.session2,
    viewport: { width: 1600, height: 1000 },
    // uPlot scales its backing canvas store by devicePixelRatio; pin it to 1 so the
    // un-masked canvas rasterizes at a fixed resolution across runs (D26 kit).
    deviceScaleFactor: 1,
    // Deterministic rendering: reduce motion via the emulated media feature.
    contextOptions: { reducedMotion: "reduce" },
  },
  projects: [
    {
      name: "chromium",
      // #530: `devices["Desktop Chrome"]` carries its own `viewport: {1280,720}`,
      // which — spread INTO this object — wins over the top-level `use.viewport`
      // (object-spread precedence is by POSITION WITHIN THIS LITERAL, not by
      // "top-level vs project-level"). Re-assert the intended 1600×1000 AFTER the
      // spread so the full-page baselines actually capture it instead of silently
      // clipping to 1280×720 (confirmed empirically: every committed detail
      // baseline was exactly 1280×720 before this fix).
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 1600, height: 1000 },
        deviceScaleFactor: 1,
      },
    },
  ],
  // Per-fixture agent + SPA pairs, all started by Playwright:
  //   agent N (REPLAY --step): the real backend (REST + SSE + the gated
  //     /api/replay/{step,advance-to} routes), one per fixture on its own port.
  //   preview N (vite preview): the built SPA, proxying /api to agent N
  //     (ROASTPILOT_API picks the target at preview-start — no rebuild per fixture).
  // session-2 is the default demo fixture (auto-T0 + the CLAMP); session-1 faults on
  // replay (real 242 °C env reading > the 240 °C ceiling); fault-pre-t0 drives the
  // real SafetyPolicy past the pre-T0 bound into operator_recovery_required.
  webServer: [
    // --- session-2 (:8000 agent / :4173 SPA) → dashboard-live + route harnesses ---
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
      env: { ROASTPILOT_API: "http://127.0.0.1:8000" },
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
    },
    // --- session-1 (:8001 agent / :4174 SPA) → dashboard-fault ---
    {
      command:
        "roastpilot-agent --replay tests/fixtures/replay/session-1 --step " +
        "--host 127.0.0.1 --port 8001",
      cwd: "..",
      url: "http://127.0.0.1:8001/api/health",
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
    },
    {
      command: "npm run preview -- --port 4174 --strictPort --host 127.0.0.1",
      url: "http://127.0.0.1:4174/",
      env: { ROASTPILOT_API: "http://127.0.0.1:8001" },
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
    },
    // --- fault-pre-t0 (:8002 agent / :4175 SPA) → dashboard-recovery ---
    {
      command:
        "roastpilot-agent --replay tests/fixtures/replay/fault-pre-t0 --step " +
        "--host 127.0.0.1 --port 8002",
      cwd: "..",
      url: "http://127.0.0.1:8002/api/health",
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
    },
    {
      command: "npm run preview -- --port 4175 --strictPort --host 127.0.0.1",
      url: "http://127.0.0.1:4175/",
      env: { ROASTPILOT_API: "http://127.0.0.1:8002" },
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
    },
    // --- session-2 developed (:8003 agent / :4176 SPA) → dashboard-developed ---
    // A SECOND session-2 agent, advanced to first_crack so the dashboard renders the
    // full ramping curve. The un-mask only guards rendering where the curve has
    // SHAPE (dashboard-live's preheating is near-flat); post-#128 the stepped
    // elapsed_seconds is sim-time, so advance-to first_crack spreads the curve across
    // ~1031 s of x instead of collapsing it. Separate from the live agent because
    // advance-to is monotonic-forward per agent.
    {
      command:
        "roastpilot-agent --replay tests/fixtures/replay/session-2 --step " +
        "--host 127.0.0.1 --port 8003",
      cwd: "..",
      url: "http://127.0.0.1:8003/api/health",
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
    },
    {
      command: "npm run preview -- --port 4176 --strictPort --host 127.0.0.1",
      url: "http://127.0.0.1:4176/",
      env: { ROASTPILOT_API: "http://127.0.0.1:8003" },
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
    },
    // --- session-2 charge-window (:8004 agent / :4177 SPA) → dashboard-charge-window ---
    // A THIRD session-2 agent (#211). dashboard-live lands at the START of preheating
    // (bean ~38 °C, below the charge band) so its baseline does NOT show the persistent
    // ChargeBanner. This state steps further INTO preheating — but still pre-T0 — until
    // the bean rises into the 170–200 °C charge band, exercising the banner's
    // unmissable "CHARGE NOW" state. Its own agent because advance-to/step is
    // monotonic-forward per agent (same reason as the developed agent).
    {
      command:
        "roastpilot-agent --replay tests/fixtures/replay/session-2 --step " +
        "--host 127.0.0.1 --port 8004",
      cwd: "..",
      url: "http://127.0.0.1:8004/api/health",
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
    },
    {
      command: "npm run preview -- --port 4177 --strictPort --host 127.0.0.1",
      url: "http://127.0.0.1:4177/",
      env: { ROASTPILOT_API: "http://127.0.0.1:8004" },
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
    },
  ],
});
