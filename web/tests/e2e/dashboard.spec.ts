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
 *   - dashboard-live          → session-2    → `preheating`  (charge band visible, bean below band)
 *   - dashboard-fault         → session-1    → `fault`       (real env-ceiling E-STOP → faulted)
 *   - dashboard-recovery      → fault-pre-t0 → `recovery`    (pre-T0 overrun → recovery_required)
 *   - dashboard-developed     → session-2    → `first_crack` (full ramping curve; own agent)
 *   - dashboard-charge-window → session-2    → preheating + bean IN the charge band → the
 *                               persistent ChargeBanner shows (#211; own agent)
 */

import { expect, test } from "@playwright/test";

import { advanceTo, AGENTS, step, stepTo } from "./global-setup";
import { readChartData, settle, settleStepped, waitForChartPoints } from "./helpers";
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
  // A small additive `step` is retry-safe HERE (8, or 16 on a retry, both stay well
  // below T0 at ~frame 99 — only the charge-window spec's step(90) needs the
  // absolute `stepTo`, #338), and additive stepping keeps the shared session-2 agent
  // independent of the other preheating specs' cursors.
  await advanceTo(AGENTS.session2, "preheating");
  const stepped = await step(AGENTS.session2, 8);
  expect(stepped.agent_phase).toBe("preheating");
  // Lossless settle (#338): the rendered phase (+ curve where charged) catches up
  // to the server's snapshot — never the lossy `__lastEventId`.
  await settleStepped(page, stepped);

  // Charge-referenced curve (#308): the x-axis re-origins to charge/T0 (0:00 =
  // charge, Artisan convention), so PRE-charge (preheating) telemetry — which the
  // server stamps with a null charge clock — carries NO curve points yet. The
  // roast curve begins at charge; in preheating only the fixed axes + the charge
  // band render. (This intentionally supersedes the #220 serve-referenced plot.)
  const hook = await readChartData(page);
  expect(hook.columns[0].length).toBe(0); // no plotted history before charge
  expect(hook.chargeBandVisible).toBe(true);
  // The charge band (170–200 °C) must be ON-SCREEN in preheating (E10-spa.md). With
  // the FIXED 0–210 °C axis (#217) the band is always in frame; assert the rendered
  // °C scale holds the pinned bounds and so spans the whole band.
  expect(hook.scales.c.min).toBe(0);
  expect(hook.scales.c.max).toBe(210);
  // The RoR axis is fixed too — pinned even in preheating before any RoR develops.
  expect(hook.scales.ror.min).toBe(-20);
  expect(hook.scales.ror.max).toBe(30);

  // The live RoR readout is surfaced as an operator-facing metric (#165) and is
  // shown from the start incl. preheat (real probe data — not hidden pre-charge).
  await expect(page.getByTestId("ror-readout")).toContainText("°C/min");
  // Pre-charge, ROAST TIME reads 00:00 and the distinct Preheat read-out carries the
  // serve-referenced lead-in (#308). The big clock is charge-referenced (0:00 =
  // charge); preheat duration is shown separately, never as roast time.
  await expect(page.getByTestId("roast-timer")).toHaveText("00:00");
  await expect(page.getByTestId("preheat-timer")).toBeVisible();

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
  // Lossless settle (#338): rendered phase + curve catch up to the server snapshot.
  await settleStepped(page, reached);

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

  // Scale-covers-data guard (#133): the c/ror axes are FIXED (#217), so assert
  // they hold the pinned bounds, and x must SPAN the loaded telemetry — never
  // collapse onto one point. A blank/collapsed plot (x null, c stuck) satisfies a
  // byte-deterministic snapshot, so the regression must fail HERE. Closes the gap
  // where this assertion lived only in dashboard-live/-developed.
  const x = hook.columns[0].filter((v): v is number => v !== null);
  expect(x.length).toBeGreaterThan(1);
  expect(hook.scales.c.min).toBe(0);
  expect(hook.scales.c.max).toBe(210);
  expect(hook.scales.ror.min).toBe(-20);
  expect(hook.scales.ror.max).toBe(30);
  // x spans the loaded telemetry, never collapses (#131). The `(max ?? 0) - (min ??
  // 0) > 0` form FAILS on a null bound (a collapsed/unranged scale) — `x.min <=
  // dataMin` passes spuriously when x.min is null (`null <= n` === true in JS).
  expect((hook.scales.x.max ?? 0) - (hook.scales.x.min ?? 0)).toBeGreaterThan(0);

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
  // Lossless settle (#338): pre-charge state — no curve yet, so the phase gate
  // alone settles (the curve is intentionally empty before charge).
  await settleStepped(page, reached);

  // Server-derived recovery phase opens the modal with the "no auto-resume" copy.
  await expect(page.getByTestId("phase-badge")).toHaveAttribute(
    "data-phase",
    "operator_recovery_required",
  );
  await expect(page.getByTestId("recovery-modal")).toBeVisible();
  await expect(page.getByTestId("recovery-no-auto-resume")).toBeVisible();

  // Charge-referenced curve (#308): the pre-T0 overrun fires BEFORE charge, so the
  // server's charge clock is still null and the curve carries NO plotted points yet
  // (the roast curve begins at charge). The fixed axes still render against an
  // unchanging frame — assert the pinned bounds (the scale-covers-data class, #133).
  const hook = await readChartData(page);
  expect(hook.columns[0].length).toBe(0); // no plotted history before charge
  expect(hook.scales.c.min).toBe(0);
  expect(hook.scales.c.max).toBe(210);
  expect(hook.scales.ror.min).toBe(-20);
  expect(hook.scales.ror.max).toBe(30);

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
  // Lossless settle (#338): rendered phase + the full charged curve catch up to the
  // server snapshot (re-hydrated from REST on (re)connect, #153), not the lossy
  // `__lastEventId` whose single dropped frame timed this spec out on #336.
  await settleStepped(page, reached);

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

  // FIXED value-axis ranges (#217): both Y-axes are pinned so the curve reads against
  // an unchanging frame and never auto-zooms to the current sensor reading. Assert the
  // rendered plot holds the operator-confirmed bounds against a REAL ramping curve —
  // temp 0–210 °C, RoR −20..+30 °C/min — so a regression to auto-fit fails HERE, not
  // only in the (regenerated-from-the-same-code) pixel baseline. The fixed °C range
  // also still covers the bean max, preserving the scale-covers-data guarantee (#131):
  // a BLANK/collapsed plot (bean ramped to 178 °C but the °C axis stuck at ~43 °C, x
  // null) would still satisfy a byte-deterministic snapshot, so it must fail HERE.
  expect(hook.scales.c.min).toBe(0);
  expect(hook.scales.c.max).toBe(210);
  expect(hook.scales.c.max ?? 0).toBeGreaterThanOrEqual(beanMax);
  expect(hook.scales.ror.min).toBe(-20);
  expect(hook.scales.ror.max).toBe(30);
  // x stays data-driven (#131): it must span the elapsed range, never collapse onto one x.
  expect((hook.scales.x.max ?? 0) - (hook.scales.x.min ?? 0)).toBeGreaterThan(300);

  // Live development time + DTR (#220): post-FC the header surfaces BOTH distinct
  // server-authoritative readouts. Assert the data renders (not just the pixels) so a
  // regression fails HERE, not only in the regenerated baseline — the timer is mm:ss,
  // the DTR a percentage. The replay reaches FC at the first development tick, so the
  // exact values are small but present (the rendered baseline captures them).
  await expect(page.getByTestId("development-timer")).toBeVisible();
  await expect(page.getByTestId("development-timer")).toHaveText(/^\d{2}:\d{2}$/);
  await expect(page.getByTestId("dtr-readout")).toBeVisible();
  // Numeric-only match (NOT `/%$/`, which the "— %" pre-FC placeholder also
  // satisfies): a server regression reverting development_percent to null post-FC
  // must fail HERE, not only in the regenerated baseline.
  await expect(page.getByTestId("dtr-readout")).toHaveText(/^\d+\.\d+ %$/);

  // ROAST TIME is CHARGE-referenced (#308): in this post-charge state the server
  // emits a non-null `charge_elapsed_seconds`, so the big clock reads since-charge,
  // NOT since-serve. This is the ONLY e2e guard on the DashboardPage wiring — a
  // revert to `elapsed_seconds` (the old serve clock) renders the SAME mm:ss shape
  // and would slip past every other assertion + the regenerated pixel baseline.
  // The charged state must hold: ROAST TIME is mm:ss and the pre-charge Preheat
  // read-out is GONE (it only shows while charge_elapsed_seconds is null).
  await expect(page.getByTestId("roast-timer")).toHaveText(/^\d{2}:\d{2}$/);
  await expect(page.getByTestId("preheat-timer")).not.toBeVisible();
  // Value check that pins the CLOCK SOURCE, not just its format. The session-2
  // fixture reaches first crack at ~535 s since charge but ~1029 s since serve (a
  // ~493 s preheat lead-in); the replay stops at that FC tick. So the charge-
  // referenced clock is ~08:55 while a serve-referenced revert would read ~17:0x.
  // Assert the rendered timer is well under the serve figure (< 15:00 = 900 s):
  // charge (~535 s) passes with wide margin, a revert to elapsed_seconds (~1029 s)
  // FAILS. Parse mm:ss → seconds rather than matching a brittle exact string.
  const roastTimerText = await page.getByTestId("roast-timer").textContent();
  expect(roastTimerText).toMatch(/^\d{2}:\d{2}$/);
  const [mm, ss] = (roastTimerText ?? "00:00").split(":").map((n) => Number(n));
  const roastTimerSeconds = mm * 60 + ss;
  expect(roastTimerSeconds).toBeGreaterThan(0); // a real post-charge clock, ticking
  expect(roastTimerSeconds).toBeLessThan(900); // charge-referenced, not the ~1029 s serve clock

  await settle(page);
  await expect(page).toHaveScreenshot("dashboard-developed.png");
});

test("dashboard-charge-window — preheating + bean in the charge band shows the persistent banner (#211)", async ({
  page,
}) => {
  // The 2nd-hardware-roast fix (#211): the old one-shot add-beans toast was easy to
  // miss, so the operator preheated an empty drum ~8 min past the charge point. This
  // state proves the PERSISTENT ChargeBanner: still in the server's `preheating`
  // phase (beans NOT yet added → no T0), but the bean has risen INTO the profile's
  // 170–200 °C charge band, so the unmissable "CHARGE NOW" banner is on screen.
  await page.goto(WEB_URLS.session2ChargeWindow);
  await expect(page.getByTestId("connection-indicator")).toHaveAttribute("data-status", "live", {
    timeout: 15_000,
  });

  // start() leaves the cursor at frame 0 (preheating, bean ~38 °C). Step forward —
  // still PRE-T0 — until the bean is in the band: in the session-2 fixture frame ~90
  // reads bean ~177 °C while phase is still preheating (T0/charge is frame 99).
  // `stepTo` (absolute cursor) lands frame 90 comfortably mid-band with margin before
  // T0 AND is retry-safe — a Playwright retry re-targets frame 90 instead of
  // over-stepping past T0 into the wrong phase (the #338 `toBe` mismatch). Assert phase
  // to fail loud if the fixture shifts (we never want a baseline of the wrong state).
  const stepped = await stepTo(AGENTS.session2ChargeWindow, 90);
  expect(stepped.agent_phase).toBe("preheating");
  // Lossless settle (#338): pre-charge state — phase gate only (no curve before T0).
  await settleStepped(page, stepped);

  // Phase reached the SPA from the server (still preheating — beans not yet added).
  await expect(page.getByTestId("phase-badge")).toHaveAttribute("data-phase", "preheating");

  // The persistent charge banner is visible with the unmissable CTA + the live bean
  // temp and the window range. This is the whole point of #211 — assert the COPY, not
  // just visibility, so a broken/empty banner fails here (not just the pixel baseline,
  // which regenerates from the same code).
  await expect(page.getByTestId("charge-banner")).toBeVisible();
  await expect(page.getByTestId("charge-banner-cta")).toContainText(/charge now/i);
  await expect(page.getByTestId("charge-banner")).toContainText(/charge window/i);
  await expect(page.getByTestId("charge-banner")).toContainText("°C");

  // Charge-referenced curve (#308): this is still PRE-charge (preheating, beans not
  // yet added → no T0), so the server's charge clock is null and the curve carries
  // NO plotted points yet — the roast curve begins at charge. The fixed axes still
  // render against an unchanging frame (the scale-covers-data class, #133/#217).
  const hook = await readChartData(page);
  expect(hook.columns[0].length).toBe(0); // no plotted history before charge
  expect(hook.scales.c.min).toBe(0);
  expect(hook.scales.c.max).toBe(210);
  expect(hook.scales.ror.min).toBe(-20);
  expect(hook.scales.ror.max).toBe(30);

  await settle(page);
  await expect(page).toHaveScreenshot("dashboard-charge-window.png");
});
