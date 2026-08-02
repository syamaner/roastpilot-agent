/**
 * Live roast dashboard — the demo centerpiece (plan §7, ui-prompts Prompt A/B,
 * kickoff §2).
 *
 * `/live` (LivePage.tsx) is the sole mount point: it only renders this page
 * once the server's `active_run_id` is non-null (#523, folding #517) — this
 * component has no idle branch of its own and never shows a start form.
 *
 * Consumes the shared foundation READ-ONLY: `useHealth` → active run id, the
 * `useRoastStream` SSE hook (phase / telemetry / enabledActions / the non-lossy
 * frame buffer — all server-derived), the shared `LiveCurve`, `ConnectionIndicator`,
 * and the verdict helper (via the page's components). The page-local
 * `useDashboardEvents` folds the remaining frames (advisory / charge guidance /
 * recovery / fault / markers) by draining the buffer (frames / frameCount), so a
 * burst never drops a frame (#122).
 *
 * INVARIANTS: phase comes from the server ONLY (never inferred here); the SPA
 * never calls MCP (only the typed REST client + SSE); temperatures Celsius;
 * verdict copy follows the enum (ALLOW, not ACCEPT); the action bar's enablement
 * mirrors the server's `enabledActions`, never a hardcoded matrix.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { AppFrame, ConnectionIndicator, LiveCurve } from "@/components/shared";
import { roastKeys, useHealth, useRoast, useTimeline } from "@/hooks/queries";
import { useFrameDrain, useRoastStream } from "@/hooks/useRoastStream";
import { api } from "@/lib/api";
import { runConfirmRetry } from "@/lib/confirmRetry";
import { smoothCurveForDisplay } from "@/lib/rorSmoothing";
import type { OperatorAction } from "@/lib/types";
import { AdvisoryPanel } from "./AdvisoryPanel";
import { ChargeBanner } from "./ChargeBanner";
import { chargeCueState } from "./chargeWindow";
import { ControlRow } from "./ControlRow";
import { FaultBanner } from "./FaultBanner";
import { resolveMicStatus } from "./micStatus";
import { OperatorActionBar, type OperatorActionResultView } from "./OperatorActionBar";
import { PostFcRecoveryStatus } from "./PostFcRecoveryStatus";
import { RecoveryModal } from "./RecoveryModal";
import { RoastHeader } from "./RoastHeader";
import { firstCrackFromTimeline } from "./events";
import { snapshotFault, useDashboardEvents } from "./useDashboardEvents";

export function DashboardPage(): React.JSX.Element {
  const health = useHealth();

  // #513: the fault-acknowledge confirm loop keeps running in this closure
  // after unmount (React does not cancel in-flight promises) — checked after
  // every await via `runConfirmRetry`'s `isMounted`, so an orphaned loop
  // never writes state or the query cache after this component is gone.
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);
  const serverRunId = health.data?.active_run_id ?? null;

  // #124: a fault finalizes the run server-side (`complete_run` stamps
  // `completed_at_utc`, so `active_run()` — and thus `active_run_id` — goes
  // null), but a transient `useHealth` refetch (a reconnect after a device
  // sleep/wifi blip mid-roast) must NOT drop the fault banner before the
  // operator has seen and acted on it. Keep showing the faulted run we were
  // already watching until the operator explicitly starts a new roast. The
  // fault itself is server-delivered (the SSE fault frame); we only refuse to
  // DISCARD it on a refetch — we never infer phase locally (invariant intact).
  const [stickyFaultedRunId, setStickyFaultedRunId] = useState<string | null>(null);
  const runId = serverRunId ?? stickyFaultedRunId;

  // Live SSE stream — phase/telemetry/enabledActions are server-derived; the
  // page-local reducer folds the NON-LOSSY frame buffer (frames/frameCount) so a
  // burst never drops a fault/recovery/advisory/marker frame (#122).
  const { status, phase, telemetry, enabledActions, frames, frameCount } = useRoastStream(runId);
  // The view-model seeds the curve from the `/telemetry` snapshot on every
  // (re)connect — keyed on `status` — so a late-joining or reconnecting device shows
  // the full roast curve, not just the frames it personally witnessed (#153).
  const view = useDashboardEvents(frames, frameCount, runId, status);

  // #592: the mount read gives an early persisted-FC fallback, then the first
  // post-subscription telemetry frame acts as the persistence barrier for the
  // snapshot→EventSource gap (the runner flushes buffered events before publishing
  // telemetry). Refresh exactly once per connection, re-armed below.
  const timeline = useTimeline(runId);
  const timelineRefreshArmedRef = useRef(true);
  useEffect(() => {
    timelineRefreshArmedRef.current = true;
  }, [runId]);
  useEffect(() => {
    if (status !== "live") timelineRefreshArmedRef.current = true;
  }, [status]);

  // P2-1 (#423): a normal roast completion emits `run_completed` on the SSE stream.
  // `useHealth` is not refetched by default on this path (health invalidation lives
  // at start + fault-ack only), so `active_run_id` stays non-null in the cache and
  // LivePage never sees the transition → the finished summary never fires. Drain the
  // non-lossy frame buffer for `run_completed` and invalidate health so the cache
  // flips to null promptly, AND the history list (#523: LivePage's persistent
  // last-completed fallback reads `useHistory()`, whose default 30s `staleTime`
  // would otherwise leave the just-finished run out of it for up to that long on
  // a fresh mount/reload — the session-sticky id covers the immediate case, but
  // invalidating here keeps a genuinely fresh reload correct too). `useFrameDrain`
  // is the established non-lossy drain pattern (#122); fire-once via an early-exit
  // in the callback (the event is one-shot per run; re-delivering it on a
  // reconnect is a no-op via invalidation).
  const queryClientForStream = useQueryClient();
  useFrameDrain(frames, frameCount, (frame) => {
    if (
      frame.event === "telemetry" &&
      timelineRefreshArmedRef.current &&
      runId !== null
    ) {
      timelineRefreshArmedRef.current = false;
      // TanStack deduplicates refetch() against an initial in-flight query when
      // that query has no cached data. Cancel it explicitly at the persistence
      // barrier, then start a guaranteed post-barrier read (#592 / Codex P2).
      void queryClientForStream
        .cancelQueries({ queryKey: roastKeys.timeline(runId) })
        .then(() => timeline.refetch());
    }
    if (frame.event === "run_completed") {
      void queryClientForStream.invalidateQueries({ queryKey: roastKeys.health });
      void queryClientForStream.invalidateQueries({ queryKey: roastKeys.history });
    }
  });

  // The run snapshot (profile name + initial enabled actions before the first
  // phase_changed). Read-only REST snapshot, hydrated by TanStack Query.
  const detail = useRoast(runId);
  // #592 reload fallback: SSE does not replay the one-shot first_crack event.
  // The timeline carries that SAME persisted server event (payload + source), so
  // a reload keeps the FC landmark without inferring it from phase/curve data.
  const persistedFirstCrack = firstCrackFromTimeline(timeline.data);
  const firstCrack = view.firstCrack ?? persistedFirstCrack;

  // Operator action POST result (the action bar surfaces its typed reason).
  const [lastResult, setLastResult] = useState<OperatorActionResultView | null>(null);

  const queryClient = useQueryClient();

  // #513: acknowledge-confirm state. The operator never loses controls while
  // this resolves (still on the faulted dashboard, heat already off), but a
  // silently-stale FaultBanner after a real acknowledge is the same class of
  // bug as the start-roast hazard this PR fixes elsewhere — this is precisely
  // the recovery path the operator exercises on the very next restart, so it
  // gets the same treatment for consistency, not just severity.
  const [acknowledgeConfirming, setAcknowledgeConfirming] = useState(false);
  const [acknowledgeConfirmFailed, setAcknowledgeConfirmFailed] = useState(false);

  // #206: the operator acknowledges a fault by POSTing the `acknowledge_fault`
  // control action. Post-#206 a fault no longer auto-finalises the run — it stays
  // operable (loop alive, heat off) so the operator can still cool the machine —
  // so the run is finalised (outcome `faulted`) only by this acknowledgement.
  // Acknowledging clears `active_run_id` on the server; we then drop the sticky-
  // faulted pin and confirm the transition directly against `api.health()`
  // (bypassing `invalidateQueries`/`refetchQueries`, which RESOLVE even when the
  // underlying fetch failed — see LiveStartView, pages/live/LivePage.tsx). Once
  // `active_run_id` clears, LivePage (the sole mount point for this page, #523)
  // swaps this component out for its own idle state (never trapping the
  // operator on the faulted view, #124). `acknowledge_fault` issues no roaster command
  // (heat is already off in faulted and stays off) — the operator keeps the
  // FaultBanner + e-stop the whole time this resolves, but the banner and the
  // acknowledge control must never go visibly silent on a failed confirm.
  const handleAcknowledgeFault = useCallback(async () => {
    const ackRunId = runId;
    // Drop the sticky-faulted pin optimistically so the page returns to idle as
    // soon as health reports no active run — the acknowledgement is the operator's
    // explicit intent and the server finalisation below makes it authoritative.
    setStickyFaultedRunId(null);
    setAcknowledgeConfirming(true);
    setAcknowledgeConfirmFailed(false);
    if (ackRunId !== null) {
      try {
        await api.operatorAction(ackRunId, { action: "acknowledge_fault" });
      } catch {
        // Best-effort: a failed acknowledge (e.g. transient) still confirms
        // health below; the operator can retry from the live view.
      }
      if (!mountedRef.current) return;
    }

    const result = await runConfirmRetry({
      attempt: () => api.health(),
      isSuccess: (health) => health.active_run_id === null,
      onResult: (health) => queryClient.setQueryData(roastKeys.health, health),
      isMounted: () => mountedRef.current,
    });
    if (result === "unmounted") return;
    // Whether confirmed or exhausted, re-enable the button (a manual retry on
    // failure) alongside the visible failure note; never leave it permanently
    // disabled.
    setAcknowledgeConfirming(false);
    if (result === "failed") setAcknowledgeConfirmFailed(true);
  }, [queryClient, runId]);

  const dispatchAction = useCallback(
    async (action: OperatorAction) => {
      if (runId === null) return;
      try {
        const result = await api.operatorAction(runId, { action });
        setLastResult({ action, result: result.result, reason: result.reason });
      } catch (err) {
        setLastResult({
          action,
          result: "failed",
          reason: err instanceof Error ? err.message : "request failed",
        });
      }
    },
    [runId],
  );

  // Development time + DTR (#220, closes the #112 gap): the live telemetry frame
  // now carries BOTH server-authoritative values — `development_elapsed_seconds`
  // (time since first crack) and `development_percent` (DTR — that time as a share
  // of the WHOLE, charge-referenced roast, consistent with the advisor's DTR,
  // #219). We render them DIRECTLY; no client-side FC-baseline derivation. Both
  // null pre-FC (the header omits the readouts).
  const developmentSeconds = telemetry?.development_elapsed_seconds ?? null;
  const developmentPercent = telemetry?.development_percent ?? null;

  // Charge-window dwell (#211): the elapsed time at which the bean first entered
  // the charge window. The banner shows how long the bean has been in the window
  // to discourage over-preheating an empty drum (the 2nd-roast failure mode).
  const [chargeEnteredElapsed, setChargeEnteredElapsed] = useState<number | null>(null);

  // Reset the per-run local state when the run changes (the view-model resets in
  // the hook): a new run must not inherit the previous run's FC baseline or a
  // stale toast dismissal. The page can stay mounted across runs.
  useEffect(() => {
    setChargeEnteredElapsed(null);
    setLastResult(null);
  }, [runId]);

  // #329: the fault that drives the FaultBanner — the LIVE evaluation (`view.fault`,
  // the real SafetyEvaluation off the one-shot `fault` SSE frame) when we witnessed
  // the fault, ELSE one synthesized from the HYDRATED SERVER SNAPSHOT (the faulted
  // `agent_phase` + persisted `fault_reason`). Without the snapshot fallback, an
  // operator who boots onto an already-faulted run or reloads while faulted folds no
  // live `fault` frame, so the banner — and the ACKNOWLEDGE affordance it hosts —
  // never render, stranding them (hit twice in roast 3). `view.fault` wins when
  // present so the real evaluation's numbers show; the snapshot stand-in only fills
  // the restore/reload gap. Phase is the server's hydrated truth — never inferred.
  const effectiveFault = view.fault ?? snapshotFault(phase, detail.data?.fault_reason);

  // #124/#329: pin the run as soon as it faults, so a later `active_run_id`→null
  // from a health refetch resolves `runId` back to this faulted run (via the `??`
  // above) instead of collapsing to idle and dropping the fault banner. Keyed on
  // `effectiveFault` so the RELOAD-while-faulted case (no live frame; the fault came
  // from the hydrated snapshot) is pinned too. Pinning the id of the run we are
  // already watching is not phase inference — the fault is server-delivered (live
  // frame or snapshot phase); cleared by `handleAcknowledgeFault`.
  useEffect(() => {
    if (effectiveFault !== null && runId !== null) {
      setStickyFaultedRunId(runId);
    }
  }, [effectiveFault, runId]);

  // Advisor targets for the control-row ghost markers (latest decision).
  const targetHeat = view.latestAdvisory?.decision?.target_heat ?? null;
  const targetFan = view.latestAdvisory?.decision?.target_fan ?? null;

  // Effective enabled actions: the live SSE mirror once a phase_changed has
  // arrived, else the snapshot's set (so the bar is correct on first paint).
  const effectiveEnabled = enabledActions ?? detail.data?.enabled_actions ?? null;

  // #117: the FaultBanner's acknowledge affordance mirrors server truth — shown
  // iff the server enables `acknowledge_fault` (only in the `faulted` phase). This
  // keeps the banner button render-from-server (no client-side command matrix,
  // D25), consistent with the OperatorActionBar.
  const canAcknowledgeFault = effectiveEnabled?.includes("acknowledge_fault") ?? false;

  // Persistent charge cue (#211): derive the cue's display state from the SERVER
  // phase (preheating), the live bean temperature, and the profile's charge band
  // from the REST snapshot. Tri-state so the cue never goes silent on an
  // over-preheat (hidden / in_window / over_window). This is a PRESENTATION
  // derivation — phase still comes only from the server; we never infer phase here.
  // Memoised on the band figures so it's referentially stable across renders (it
  // feeds both the ChargeBanner and the LiveCurve, which would otherwise see a new
  // object every render). Behaviour is identical.
  const chargeMinC = detail.data?.profile.charge_guidance_min_c ?? null;
  const chargeMaxC = detail.data?.profile.charge_guidance_max_c ?? null;
  const chargeBand = useMemo(
    () => (chargeMinC !== null && chargeMaxC !== null ? { minC: chargeMinC, maxC: chargeMaxC } : null),
    [chargeMinC, chargeMaxC],
  );
  // Charge-readiness cue is driven off the BEAN PROBE (operator decision, confirmed
  // attempt-3): roasters charge on the bean-probe reading, not the drum/env. The
  // earlier "cue didn't show" was the bean correctly still below the 170 floor, made
  // worse by the #217 fixed-axis scaling that made the high env look alarming — NOT a
  // reason to key the cue on env. (A brief env-cue experiment was reverted.)
  const beanTempC = telemetry?.bean_temp_c ?? null;
  const chargeCue = chargeCueState(phase, beanTempC, chargeBand);
  // The cue is shown (dwell tracked) once the bean reaches charge temperature —
  // both in-window and over-window count, since the dwell discourages exactly the
  // over-preheat the warning escalates on.
  const chargeCueShown = chargeCue !== "hidden";

  // Stamp the elapsed time the bean first reached the charge zone; clear it when it
  // drops back below / leaves preheating (so a re-entry restarts the dwell). Derived
  // from server telemetry + the presentation cue state — not phase inference.
  const elapsedSeconds = telemetry?.elapsed_seconds ?? null;
  useEffect(() => {
    if (chargeCueShown) {
      setChargeEnteredElapsed((prev) => (prev === null ? elapsedSeconds : prev));
    } else {
      setChargeEnteredElapsed(null);
    }
  }, [chargeCueShown, elapsedSeconds]);
  const dwellSeconds =
    chargeCueShown && chargeEnteredElapsed !== null && elapsedSeconds !== null
      ? elapsedSeconds - chargeEnteredElapsed
      : null;

  const inRecovery = phase === "operator_recovery_required";

  // #205/#344: smooth the bean + RoR series for DISPLAY ONLY (raw `bean_temp_c` /
  // `bean_ror_c_per_min` still feed the advisor/safety server-side, untouched). The
  // staircase comes from the 1 Hz quantised channels; a centered quadratic
  // Savitzky-Golay fit dissolves it without net lag (the live tail shows raw).
  const curve = useMemo(
    () => ({ points: smoothCurveForDisplay(view.points), markers: view.markers }),
    [view.points, view.markers],
  );
  // #592: SSE is authoritative while frames are flowing. On a reload/reconnect
  // before the next tick, the existing /telemetry backfill still carries the
  // server's latest bean + RoR values; read that final server point directly
  // rather than flashing empty or borrowing the chart cursor's historical value.
  const latestSnapshotPoint =
    view.points.length > 0 ? view.points[view.points.length - 1] : null;
  const latestBeanTempC =
    telemetry?.bean_temp_c ?? latestSnapshotPoint?.bean ?? null;
  const latestBeanRorCPerMin =
    telemetry === null
      ? latestSnapshotPoint?.ror ?? null
      : telemetry.bean_ror_c_per_min;

  return (
    <AppFrame headerRight={<ConnectionIndicator status={status} />}>
      <div className="flex flex-col gap-4" data-testid="dashboard">
        {/* Fault banner sits above the dashboard when faulted (Prompt B §2).
            Informational + persistent: the fault stays on screen until the
            operator acknowledges it; cooling/e-stop live in the action bar (the
            faulted run stays operable, #206). The acknowledge affordance (#117) —
            shown only when the server's enabled_actions mirror enables
            acknowledge_fault — dispatches the genuine `acknowledge_fault` action,
            finalising the operable-faulted run server-side, then clears the
            sticky-faulted pin (#124) and re-fetches health; once `active_run_id`
            clears, LivePage swaps this page out for its own idle state (#523).
            `acknowledge_fault` issues no roaster command (heat is already off). */}
        <FaultBanner
          fault={effectiveFault}
          trail={view.safetyTrail}
          // #117: the acknowledge affordance is driven by the server's
          // `enabled_actions` mirror (acknowledge_fault is enabled iff the phase
          // is `faulted`), NOT a client-side fault check — the no-client-matrix
          // invariant (D25). When the server stops enabling it, the affordance is
          // omitted. The genuine `acknowledge_fault` control action (#206)
          // finalises the operable-faulted run server-side (no roaster command —
          // heat is already off), then the page clears the sticky pin (#124) and
          // confirms against `api.health()`, handing off to LivePage's idle state
          // (#523). The label is the operator's real next step. #513: while
          // confirming the button is
          // disabled (no double-submit); if every confirm attempt fails the
          // button stays enabled for a manual retry and a visible note explains
          // why the banner hasn't cleared — it never goes silently stale.
          acknowledgeAffordance={
            canAcknowledgeFault ? (
              <div className="flex flex-col items-end gap-2">
                <button
                  type="button"
                  onClick={() => void handleAcknowledgeFault()}
                  disabled={acknowledgeConfirming}
                  data-testid="fault-acknowledge"
                  className="inline-flex items-center rounded-md border border-roast-fault/60 bg-roast-fault/15 px-4 py-2 text-sm font-semibold uppercase tracking-wide text-roast-fault transition-colors hover:bg-roast-fault/25 disabled:cursor-not-allowed disabled:opacity-60"
                >
                  {acknowledgeConfirming ? "Acknowledging…" : "Acknowledge Fault & Start New Roast"}
                </button>
                {acknowledgeConfirmFailed && (
                  <p
                    role="alert"
                    data-testid="fault-acknowledge-confirm-failed"
                    className="max-w-xs text-right text-xs text-roast-fault"
                  >
                    Acknowledged, but this page could not confirm the run cleared. Heat
                    stays off. Try again, or reload.
                  </p>
                )}
              </div>
            ) : undefined
          }
        />

        <RoastHeader
          phase={phase}
          // ROAST TIME is charge-referenced (#308): 0:00 = charge, frozen at drop.
          chargeElapsedSeconds={telemetry?.charge_elapsed_seconds ?? null}
          // Serve-referenced elapsed backs the pre-charge "Preheat" read-out only.
          elapsedSeconds={telemetry?.elapsed_seconds ?? null}
          developmentSeconds={developmentSeconds}
          developmentPercent={developmentPercent}
          beanRorCPerMin={latestBeanRorCPerMin}
          // #592: latest server telemetry — deliberately independent of the
          // chart cursor/legend's historical point.
          beanTempC={latestBeanTempC}
          profileName={detail.data?.profile.name ?? null}
          firstCrack={firstCrack}
          mcpChild={health.data?.mcp_child}
          // Capture-alive mic health (#197/#200): live frame is authoritative once
          // present (its null = idle passes through, not the stale snapshot); the
          // snapshot only paints on hydrate before the first frame. Server-derived.
          micStatus={resolveMicStatus(telemetry, detail.data?.mic_status)}
          // #464: the live "Room" conditions readout — the LATEST ambient triad,
          // read directly off the telemetry frame (no snapshot fallback: unlike
          // mic_status there is no pre-frame ambient signal on RoastDetail to
          // paint from; the readout simply shows "—" until the first frame).
          ambientTempC={telemetry?.ambient_temp_c ?? null}
          ambientHumidityPct={telemetry?.ambient_humidity_pct ?? null}
          ambientPressureHpa={telemetry?.ambient_pressure_hpa ?? null}
        />

        {/* Persistent charge-window banner (#211): replaces the easily-missed
            one-shot add-beans toast. It stays on screen the WHOLE time the
            operator should be charging — server phase `preheating` AND the live
            bean temperature inside the profile's charge band — and disappears on
            its own when the server transitions to `roasting_pre_first_crack`
            (beans added). Guidance only; it issues no roaster command. */}
        <ChargeBanner
          phase={phase}
          beanTempC={beanTempC}
          chargeBand={chargeBand}
          dwellSeconds={dwellSeconds}
        />

        <LiveCurve
          points={curve.points}
          markers={curve.markers}
          phase={phase}
          chargeBand={chargeBand ?? undefined}
          originSeconds={view.t0ElapsedSeconds}
        />

        {/* Pre-FC the controller drives heat/fan deterministically off the bean
            profile (D59), so the control row renders them as READ-OUTS, not dials
            (#318) — gated on the server `phase` (never inferred). Post-FC the bar +
            advisor-target ghost render unchanged. */}
        <ControlRow
          phase={phase}
          heatPercent={telemetry?.heat_percent ?? null}
          fanPercent={telemetry?.fan_percent ?? null}
          targetHeatPercent={targetHeat}
          targetFanPercent={targetFan}
        />

        <PostFcRecoveryStatus trace={view.postFcControl} />

        <AdvisoryPanel
          latest={view.latestAdvisory}
          history={view.advisoryHistory}
          paused={view.advisoryPaused}
          originSeconds={view.t0ElapsedSeconds}
        />
      </div>

      {/* The action bar is page chrome — always visible at the bottom. */}
      <div className="-mx-6 mt-4">
        <OperatorActionBar
          enabledActions={effectiveEnabled}
          phase={phase}
          onAction={(a) => void dispatchAction(a)}
          lastResult={lastResult}
        />
      </div>

      <RecoveryModal
        open={inRecovery}
        beanTempC={telemetry?.bean_temp_c ?? null}
        envTempC={telemetry?.env_temp_c ?? null}
        heatPercent={telemetry?.heat_percent ?? null}
        fanPercent={telemetry?.fan_percent ?? null}
        enabledActions={effectiveEnabled}
        onAction={(a) => void dispatchAction(a)}
      />
    </AppFrame>
  );
}
