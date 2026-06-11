/**
 * Live dashboard snapshot matrix — `dashboard-live` / `dashboard-fault` /
 * `dashboard-recovery` (S6, D26).
 *
 * D26: the uPlot canvas is UN-MASKED — every shot captures the WHOLE page,
 * chart included. The `window.__chart` data hook stays as the authoritative
 * correctness layer (asserted alongside the pixels). Phase always reaches the SPA
 * from the server (hydrate snapshot + SSE), never inferred client-side.
 *
 * The states need different replay fixtures, so each spec loads the preview
 * proxying its own agent (see urls.ts WEB_URLS / global-setup AGENTS) and drives
 * that agent:
 *   - dashboard-live      → session-2    → `preheating`  (charge band visible)
 *   - dashboard-fault     → session-1    → `fault`       (real env-ceiling E-STOP → faulted)
 *   - dashboard-recovery  → fault-pre-t0 → `recovery`    (pre-T0 overrun → recovery_required)
 *   - dashboard-developed → session-2    → `first_crack` (full ramping curve; own agent)
 */

import { expect, test } from "@playwright/test";

import { advanceTo, AGENTS, step } from "./global-setup";
import { readChartData, settle, waitForChartPoints } from "./helpers";
import { WEB_URLS } from "./urls";

test("dashboard-live — preheating with the charge band, full-page snapshot (canvas un-masked)", async ({
  page,
}) => {
  await page.goto(WEB_URLS.session2);

  // Phase + telemetry come from the server: wait until the stream is live.
  await expect(page.getByTestId("connection-indicator")).toHaveAttribute("data-status", "live", {
    timeout: 15_000,
  });

  // `preheating` is the tick-0 marker (emits no new frames); step a few ticks INTO
  // preheating so telemetry frames flow to the live browser and the curve builds.
  const reached = await advanceTo(AGENTS.session2, "preheating");
  expect(reached.agent_phase).toBe("preheating");
  const stepped = await step(AGENTS.session2, 8);
  expect(stepped.agent_phase).toBe("preheating");
  await page.waitForFunction((id) => (window.__lastEventId ?? -1) >= id, stepped.last_event_id, {
    timeout: 15_000,
  });

  // The phase badge reflects the server's preheating phase.
  await expect(page.getByTestId("phase-badge")).toHaveAttribute("data-phase", "preheating");

  // Gate the canvas shot on an INDEPENDENT minimum point-count (the stepped frames
  // mean the curve must carry ≥1 point) BEFORE reading the hook — so the barrier
  // can actually block on the async chart render, not a count it just read (D26 kit).
  await waitForChartPoints(page, 1);

  // The curve built from the stepped telemetry, and the charge band shows in
  // preheating (asserted via DATA — the authoritative layer alongside the pixels).
  const hook = await readChartData(page);
  expect(hook.columns[0].length).toBeGreaterThan(0);
  expect(hook.chargeBandVisible).toBe(true);

  await settle(page);
  await expect(page).toHaveScreenshot("dashboard-live.png");
});

test("dashboard-fault — real env-ceiling fault renders the fault banner + trail (canvas un-masked)", async ({
  page,
}) => {
  // session-1 carries a real 242 °C env reading > the agent's 240 °C ceiling, so
  // the REAL SafetyPolicy trips EMERGENCY_STOP → faulted (a faithful replay, not a
  // mock). Load the preview backed by the session-1 agent.
  await page.goto(WEB_URLS.session1);
  await expect(page.getByTestId("connection-indicator")).toHaveAttribute("data-status", "live", {
    timeout: 15_000,
  });

  // Advance to the fault marker; 404 here means the export never faulted (fail loud).
  const reached = await advanceTo(AGENTS.session1, "fault");
  expect(reached.agent_phase).toBe("faulted");
  await page.waitForFunction((id) => (window.__lastEventId ?? -1) >= id, reached.last_event_id, {
    timeout: 15_000,
  });

  // Phase reached the SPA from the server, and the fault banner is shown.
  await expect(page.getByTestId("phase-badge")).toHaveAttribute("data-phase", "faulted");
  await expect(page.getByTestId("fault-banner")).toBeVisible();
  // Assert the reason TEXT, not just visibility: this is the only place the
  // SSE-fault → real SafetyPolicy → FaultBanner copy path is exercised, and an
  // empty/truncated reason would pass both toBeVisible() and the pixel baseline
  // (the baseline regenerates from the same code). The exact figures come from the
  // real policy evaluating session-1's 241 °C env reading vs the 240 °C ceiling.
  await expect(page.getByTestId("fault-reason")).toContainText(
    /241(\.0)?\s*°C exceeds the hard ceiling 240(\.0)?\s*°C/,
  );

  // Gate the canvas shot on an INDEPENDENT minimum point-count before reading the
  // hook, so the barrier blocks on the async render (D26 kit).
  await waitForChartPoints(page, 1);

  // The curve carries the persisted telemetry up to the fault (data layer).
  const hook = await readChartData(page);
  expect(hook.columns[0].length).toBeGreaterThan(0);

  await settle(page);
  await expect(page).toHaveScreenshot("dashboard-fault.png");
});

test("dashboard-recovery — pre-T0 overrun opens the no-auto-resume recovery modal (canvas un-masked)", async ({
  page,
}) => {
  // fault-pre-t0 drives the real SafetyPolicy past the pre-T0 bound → the default
  // RECOVERY verdict → operator_recovery_required (the SPA RecoveryModal). Load the
  // preview backed by the fault-pre-t0 agent.
  await page.goto(WEB_URLS.faultPreT0);
  await expect(page.getByTestId("connection-indicator")).toHaveAttribute("data-status", "live", {
    timeout: 15_000,
  });

  const reached = await advanceTo(AGENTS.faultPreT0, "recovery");
  expect(reached.agent_phase).toBe("operator_recovery_required");
  await page.waitForFunction((id) => (window.__lastEventId ?? -1) >= id, reached.last_event_id, {
    timeout: 15_000,
  });

  // Server-derived recovery phase opens the modal with the "no auto-resume" copy.
  await expect(page.getByTestId("phase-badge")).toHaveAttribute(
    "data-phase",
    "operator_recovery_required",
  );
  await expect(page.getByTestId("recovery-modal")).toBeVisible();
  await expect(page.getByTestId("recovery-no-auto-resume")).toBeVisible();

  // Gate the canvas shot on an INDEPENDENT minimum point-count before reading the
  // hook, so the barrier blocks on the async render (D26 kit).
  await waitForChartPoints(page, 1);

  // The curve drew the short pre-T0 track (data layer).
  const hook = await readChartData(page);
  expect(hook.columns[0].length).toBeGreaterThan(0);

  await settle(page);
  await expect(page).toHaveScreenshot("dashboard-recovery.png");
});

test("dashboard-developed — full ramping curve at first crack (canvas un-masked, real shape)", async ({
  page,
}) => {
  // The un-mask only guards rendering where the curve has SHAPE — dashboard-live is
  // preheating (near-flat). This state advances session-2 to `first_crack` so the
  // dashboard renders the real ramping bean/env/RoR curve + heat/fan step lines + the
  // FC marker, on its OWN session-2 agent (advance-to is monotonic-forward per agent).
  // Post-#128 the stepped `elapsed_seconds` is sim-time, so the curve spreads across
  // the real roast duration (~1031 s) instead of collapsing onto one x.
  await page.goto(WEB_URLS.session2Developed);
  await expect(page.getByTestId("connection-indicator")).toHaveAttribute("data-status", "live", {
    timeout: 15_000,
  });

  const reached = await advanceTo(AGENTS.session2Developed, "first_crack");
  expect(reached.agent_phase).toBe("development");
  await page.waitForFunction((id) => (window.__lastEventId ?? -1) >= id, reached.last_event_id, {
    timeout: 15_000,
  });

  await expect(page.getByTestId("phase-badge")).toHaveAttribute("data-phase", "development");

  // Gate on an independent minimum (a developed curve carries many points) BEFORE
  // reading the hook, so the barrier blocks on the async render (D26 kit). The real
  // barrier pattern: an independently-known count, not one read from the hook.
  await waitForChartPoints(page, 50);

  // Curve-SHAPE assertion (not just a point count): the bean series must span a real
  // roast temperature range, the curve must spread across the roast's elapsed time
  // (post-#128 sim-clock — NOT collapsed onto one x), and the FC marker must be on the
  // curve — so this baseline guards an ACTUAL ramping curve, the whole point of the
  // un-mask. session-2 ramps bean ~38 → ~186 °C over ~1031 s by first crack (fixture);
  // assert comfortably below those so the baseline isn't brittle to fixture nudges.
  const hook = await readChartData(page);
  const x = hook.columns[0].filter((v): v is number => v !== null);
  const bean = hook.columns[1].filter((v): v is number => v !== null);
  expect(bean.length).toBeGreaterThanOrEqual(50);
  const beanMax = Math.max(...bean);
  expect(beanMax).toBeGreaterThan(120); // a developed bean temperature
  expect(beanMax - Math.min(...bean)).toBeGreaterThan(40); // a real ramp
  // The x-axis (elapsed seconds since T0) spreads across the roast — guards the #128
  // regression where a stepped burst collapsed every point onto one x.
  expect(Math.max(...x) - Math.min(...x)).toBeGreaterThan(300);
  expect(hook.markers.map((m) => m.kind)).toContain("first_crack");

  // Scale-COVERS-data guard (#131): the rendered uPlot scales must actually span the
  // data, else the curve draws off-screen / collapsed (the bug where bean ramped to
  // 178 °C but the °C axis stayed pinned at ~43 °C and x stayed null → a BLANK plot
  // that still passes a byte-deterministic snapshot). Assert the °C scale max reaches
  // the bean max and the x scale spans the elapsed range — so a blank plot fails HERE
  // even though the pixels alone would be stable.
  expect(hook.scales.c.max ?? 0).toBeGreaterThanOrEqual(beanMax);
  expect((hook.scales.x.max ?? 0) - (hook.scales.x.min ?? 0)).toBeGreaterThan(300);

  await settle(page);
  await expect(page).toHaveScreenshot("dashboard-developed.png");
});
