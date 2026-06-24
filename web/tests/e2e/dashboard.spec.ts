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

  // Serve-keyed curve (#326): the curve buffer keys on serve elapsed, so PRE-charge
  // (preheating) telemetry PLOTS LIVE (the #316 blank-preheat regression is fixed).
  // After stepping 8 ticks into preheat the curve carries that lead-in — assert it is
  // non-empty and ascending on the serve clock. The ROAST TIME label (0:00 = charge,
  // negative in preheat) is a display transform, not a change to the buffered x.
  const hook = await readChartData(page);
  const xs = hook.columns[0].filter((v): v is number => v !== null);
  expect(xs.length).toBeGreaterThan(0); // preheat history plots live (#326)
  for (let i = 1; i < xs.length; i++) expect(xs[i]).toBeGreaterThan(xs[i - 1]); // ascending serve-elapsed
  expect(hook.chargeBandVisible).toBe(true);
  // The charge band (170–200 °C) must be ON-SCREEN in preheating (E10-spa.md). The °C
  // axis is now controlled-dynamic (#307): assert the rendered range COVERS the band
  // and has a real, non-collapsed width (the #131 guard) rather than a pinned 0–210.
  expect(hook.scales.c.min).not.toBeNull();
  expect(hook.scales.c.max).not.toBeNull();
  expect(hook.scales.c.min ?? 0).toBeLessThanOrEqual(170);
  expect(hook.scales.c.max ?? 0).toBeGreaterThanOrEqual(200);
  expect((hook.scales.c.max ?? 0) - (hook.scales.c.min ?? 0)).toBeGreaterThan(0);
  // The RoR axis stays FIXED — pinned even in preheating before any RoR develops.
  expect(hook.scales.ror.min).toBe(-20);
  expect(hook.scales.ror.max).toBe(30);
  // The dedicated control-line axis (heat/fan, #307) is fixed 0–100 %.
  expect(hook.scales.pct.min).toBe(0);
  expect(hook.scales.pct.max).toBe(100);

  // The live RoR readout is surfaced as an operator-facing metric (#165) and is
  // shown from the start incl. preheat (real probe data — not hidden pre-charge).
  await expect(page.getByTestId("ror-readout")).toContainText("°C/min");
  // Pre-charge, ROAST TIME reads 00:00 and the distinct Preheat read-out carries the
  // serve-referenced lead-in (#308). The big clock is charge-referenced (0:00 =
  // charge); preheat duration is shown separately, never as roast time.
  await expect(page.getByTestId("roast-timer")).toHaveText("00:00");
  await expect(page.getByTestId("preheat-timer")).toBeVisible();

  // #318: pre-FC the controller drives heat/fan deterministically off the bean
  // profile (D59), so the control row renders them as READ-OUTS — gated on the
  // server phase (preheating here), never inferred. Assert the read-out mode + the
  // controller-driven note and the ABSENCE of the slider-bar/advisor-target
  // affordance that implied a settable dial (the roast-2 silent-revert confusion).
  // A revert to the interactive (post-FC) presentation pre-FC fails HERE, not only
  // in the regenerated pixel baseline.
  await expect(page.getByTestId("control-heat")).toHaveAttribute("data-mode", "readout");
  await expect(page.getByTestId("control-fan")).toHaveAttribute("data-mode", "readout");
  await expect(page.getByTestId("control-heat-readout-note")).toBeVisible();
  await expect(page.getByTestId("control-fan-readout-note")).toBeVisible();

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

  // Scale-covers-data guard (#133/#307): the °C axis is controlled-dynamic, so assert
  // it COVERS the data with a real width (never collapses), the RoR axis holds its
  // FIXED band, and x must SPAN the loaded telemetry — never collapse onto one point.
  // A blank/collapsed plot (x null, c stuck) satisfies a byte-deterministic snapshot,
  // so the regression must fail HERE. session-1 carries a 241 °C env reading — the old
  // fixed 0–210 (#217) would have CLIPPED it; the auto-range must keep it in frame.
  const x = hook.columns[0].filter((v): v is number => v !== null);
  expect(x.length).toBeGreaterThan(1);
  const env = hook.columns[2].filter((v): v is number => v !== null);
  const envMax = Math.max(...env);
  expect((hook.scales.c.max ?? 0) - (hook.scales.c.min ?? 0)).toBeGreaterThan(0);
  expect(hook.scales.c.max ?? 0).toBeGreaterThanOrEqual(envMax); // no clip (the #307 no-clip contract)
  expect(hook.scales.ror.min).toBe(-20);
  expect(hook.scales.ror.max).toBe(30);
  expect(hook.scales.pct.min).toBe(0);
  expect(hook.scales.pct.max).toBe(100);
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

  // Serve-keyed curve (#326): the pre-T0 overrun fires BEFORE charge, but the curve
  // now keys on serve elapsed, so any preheat frames seen before the overrun PLOT
  // (the curve no longer waits for charge). The count depends on the fixture's
  // pre-overrun frames; assert the curve is well-formed (ascending serve-elapsed, no
  // crash on a sparse/degenerate range — the #326 hardening) rather than a brittle
  // exact count. The fixed axes still render against an unchanging frame (the
  // scale-covers-data class, #133/#217).
  const hook = await readChartData(page);
  const xs = hook.columns[0].filter((v): v is number => v !== null);
  for (let i = 1; i < xs.length; i++) expect(xs[i]).toBeGreaterThan(xs[i - 1]); // ascending serve-elapsed
  // °C axis controlled-dynamic (#307): a real, non-collapsed width; RoR + pct fixed.
  expect((hook.scales.c.max ?? 0) - (hook.scales.c.min ?? 0)).toBeGreaterThan(0);
  expect(hook.scales.ror.min).toBe(-20);
  expect(hook.scales.ror.max).toBe(30);
  expect(hook.scales.pct.min).toBe(0);
  expect(hook.scales.pct.max).toBe(100);

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
  // The x-axis (serve-elapsed seconds, #326 — the buffer key; the ROAST TIME label
  // is a display transform) spreads across the roast — guards the #128 regression
  // where a stepped burst collapsed every point onto one x.
  expect(Math.max(...x) - Math.min(...x)).toBeGreaterThan(300);
  expect(hook.markers.map((m) => m.kind)).toContain("first_crack");

  // Axis-scaling policy (#307): the °C axis is CONTROLLED-DYNAMIC (auto-range with
  // hysteresis, no clip), the RoR axis stays FIXED, and the control lines get their own
  // FIXED 0–100 % axis. Assert against a REAL ramping curve so a regression fails HERE,
  // not only in the (regenerated-from-the-same-code) pixel baseline.
  // Scale-covers-data (#131): the °C range must COVER the bean max with a real width —
  // a BLANK/collapsed plot (bean ramped to 178 °C but the °C axis stuck/collapsed, x
  // null) would still satisfy a byte-deterministic snapshot, so it must fail HERE.
  expect((hook.scales.c.max ?? 0) - (hook.scales.c.min ?? 0)).toBeGreaterThan(0);
  expect(hook.scales.c.max ?? 0).toBeGreaterThanOrEqual(beanMax); // no clip (#307)
  expect(hook.scales.ror.min).toBe(-20);
  expect(hook.scales.ror.max).toBe(30);
  expect(hook.scales.pct.min).toBe(0);
  expect(hook.scales.pct.max).toBe(100);
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

  // #318: POST-FC (development) the advisor advises + the operator can act + the
  // deadband gate applies, so the control row stays in its INTERACTIVE presentation
  // (the slider-style bar + advisor-target ghost). Gated on the server phase — the
  // read-out treatment is pre-FC only. A regression that read-out'd heat/fan post-FC
  // fails HERE.
  await expect(page.getByTestId("control-heat")).toHaveAttribute("data-mode", "interactive");
  await expect(page.getByTestId("control-fan")).toHaveAttribute("data-mode", "interactive");

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

  // Serve-keyed curve (#326): this is still PRE-charge (preheating, beans not yet
  // added → no T0), but the curve buffer now keys on serve elapsed, so the preheat
  // lead-in PLOTS LIVE (the #316 blank-preheat regression is fixed). After stepping
  // 90 frames the curve carries the preheat history — assert it is non-empty and
  // ascending on the serve clock. The fixed axes still render against an unchanging
  // frame (the scale-covers-data class, #133/#217).
  const hook = await readChartData(page);
  const xs = hook.columns[0].filter((v): v is number => v !== null);
  expect(xs.length).toBeGreaterThan(0); // preheat history plots live (#326)
  for (let i = 1; i < xs.length; i++) expect(xs[i]).toBeGreaterThan(xs[i - 1]); // ascending serve-elapsed
  // °C axis controlled-dynamic (#307): a real, non-collapsed width; RoR + pct fixed.
  expect((hook.scales.c.max ?? 0) - (hook.scales.c.min ?? 0)).toBeGreaterThan(0);
  expect(hook.scales.ror.min).toBe(-20);
  expect(hook.scales.ror.max).toBe(30);
  expect(hook.scales.pct.min).toBe(0);
  expect(hook.scales.pct.max).toBe(100);

  await settle(page);
  await expect(page).toHaveScreenshot("dashboard-charge-window.png");
});
