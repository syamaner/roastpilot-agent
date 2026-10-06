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
import { smoothCurveForDisplay } from "@/lib/rorSmoothing";
import type {
  FaultAcknowledgementExecutedEventData,
  OperatorAction,
  RoastTimeline,
} from "@/lib/types";
import { AdvisoryPanel } from "./AdvisoryPanel";
import { ChargeBanner } from "./ChargeBanner";
import { chargeCueState } from "./chargeWindow";
import { ControlRow } from "./ControlRow";
import { FaultBanner } from "./FaultBanner";
import {
  FaultAcknowledgementControl,
  type FaultAcknowledgementStatus,
} from "./FaultAcknowledgementControl";
import { resolveMicStatus } from "./micStatus";
import { OperatorActionBar, type OperatorActionResultView } from "./OperatorActionBar";
import { PostFcRecoveryStatus } from "./PostFcRecoveryStatus";
import { RecoveryModal } from "./RecoveryModal";
import { RoastHeader } from "./RoastHeader";
import { firstCrackFromTimeline } from "./events";
import { snapshotFault, useDashboardEvents } from "./useDashboardEvents";

export function DashboardPage(): React.JSX.Element {
  const health = useHealth();

  // The REST queue request can resolve after this page unmounts. Keep that
  // completion from changing local feedback state after the operator leaves.
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
  // A queue response is deliberately not completion evidence. Only the current
  // stream's `fault_acknowledgement_executed` event transitions this state from
  // pending to failed/completed, so a reconnect, reload, or elapsed time cannot
  // make a stale fault look resolved.
  const [faultAcknowledgement, setFaultAcknowledgement] =
    useState<FaultAcknowledgementStatus>({ kind: "ready" });
  const faultAcknowledgementRef = useRef<FaultAcknowledgementStatus>({ kind: "ready" });
  const setFaultAcknowledgementState = useCallback((next: FaultAcknowledgementStatus) => {
    faultAcknowledgementRef.current = next;
    setFaultAcknowledgement(next);
  }, []);

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
    if (
      frame.event === "fault_acknowledgement_executed" &&
      runId !== null
    ) {
      const outcome = faultAcknowledgementOutcome(frame.data);
      if (outcome?.outcome === "confirmed") {
        // The exact current stream, not a POST response or timeout, proved
        // completion. The normal `run_completed` frame revalidates health.
        setFaultAcknowledgementState({ kind: "completed" });
        setStickyFaultedRunId(null);
      } else if (outcome?.outcome === "failed") {
        setFaultAcknowledgementState({ kind: "failed", reason: outcome.reason });
      }
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

  // A named SSE stream is scoped to `runId`, but a reconnect/reload can miss its
  // one-shot acknowledgement outcome. The timeline is the same persisted server
  // record, and is therefore the only reload fallback we accept. A timeline for a
  // different run, malformed payload, or non-operator source is ignored.
  const persistedAcknowledgement = useMemo(
    () => faultAcknowledgementFromTimeline(timeline.data, runId),
    [runId, timeline.data],
  );

  // Operator action POST result (the action bar surfaces its typed reason).
  const [lastResult, setLastResult] = useState<OperatorActionResultView | null>(null);
  const handleAcknowledgeFault = useCallback(async () => {
    const ackRunId = runId;
    if (ackRunId === null) return;
    // Mark pending before the POST. The runner can execute and publish its SSE
    // outcome before the queue-acceptance response resolves; acceptance remains
    // non-terminal and is never used as completion evidence.
    setFaultAcknowledgementState({ kind: "pending" });
    try {
      const result = await api.operatorAction(ackRunId, { action: "acknowledge_fault" });
      if (!mountedRef.current) return;
      if (result.result === "accepted" && result.queued) {
        // One persisted-server refresh closes the event/subscription race without
        // treating elapsed time or queue acceptance as an execution outcome.
        void timeline.refetch();
      } else if (faultAcknowledgementRef.current.kind === "pending") {
        setFaultAcknowledgementState({ kind: "failed", reason: "not_queued" });
      }
    } catch {
      if (mountedRef.current && faultAcknowledgementRef.current.kind === "pending") {
        setFaultAcknowledgementState({ kind: "failed", reason: "not_queued" });
      }
    }
  }, [runId, setFaultAcknowledgementState, timeline]);

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
    setFaultAcknowledgementState({ kind: "ready" });
  }, [runId, setFaultAcknowledgementState]);

  useEffect(() => {
    if (persistedAcknowledgement === null) return;
    if (persistedAcknowledgement.outcome === "confirmed") {
      setFaultAcknowledgementState({ kind: "completed" });
      setStickyFaultedRunId(null);
    } else {
      setFaultAcknowledgementState({ kind: "failed", reason: persistedAcknowledgement.reason });
    }
  }, [persistedAcknowledgement, setFaultAcknowledgementState]);

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
  // frame or snapshot phase); after a server-confirmed acknowledgement it must
  // not re-pin a run that health is about to clear.
  useEffect(() => {
    if (faultAcknowledgement.kind !== "completed" && effectiveFault !== null && runId !== null) {
      setStickyFaultedRunId(runId);
    }
  }, [effectiveFault, faultAcknowledgement.kind, runId]);

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
  // #789: `telemetry?.bean_temp_c ?? latestSnapshotPoint?.bean` would be WRONG
  // once `bean_temp_c` is nullable — `??` cannot distinguish "no live frame yet"
  // from "a live frame arrived and its own reading is null", so it would quietly
  // paint a stale snapshot temperature over a genuine current-live null. Branch
  // on `telemetry === null` explicitly (the snapshot fallback applies ONLY
  // before any live frame has been received), mirroring the RoR readout's
  // existing pattern immediately below.
  const latestBeanTempC =
    telemetry === null ? latestSnapshotPoint?.bean ?? null : telemetry.bean_temp_c;
  const latestBeanRorCPerMin =
    telemetry === null
      ? latestSnapshotPoint?.ror ?? null
      : telemetry.bean_ror_c_per_min;

  return (
    <AppFrame headerRight={<ConnectionIndicator status={status} />}>
      <div className="flex flex-col gap-4" data-testid="dashboard">
        {/* A fault stays visible while cooling/e-stop controls remain available.
            The acknowledgement is a guarded server-authorised action; its POST
            only queues work, and this page clears the sticky fault only after the
            current SSE stream confirms completion. */}
        <FaultBanner
          fault={effectiveFault}
          trail={view.safetyTrail}
          // Render permission still comes solely from the server mirror. The
          // component adds only the operator's cooling-safe confirmation and
          // server-event feedback; it never calls MCP or derives a phase.
          acknowledgeAffordance={
            canAcknowledgeFault ? (
              <FaultAcknowledgementControl
                status={faultAcknowledgement}
                onConfirm={() => void handleAcknowledgeFault()}
              />
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

        <PostFcRecoveryStatus phase={phase} trace={view.postFcControl} />

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

/**
 * Accept only the closed, bounded acknowledgement outcome grammar from the
 * server. SSE payloads are external input: malformed data leaves the fault in
 * its existing state instead of fabricating a completion or failure.
 */
function faultAcknowledgementOutcome(
  value: Record<string, unknown>,
): FaultAcknowledgementExecutedEventData | null {
  if (value.outcome === "confirmed") return { outcome: "confirmed" };
  if (
    value.outcome === "failed" &&
    typeof value.reason === "string" &&
    /^[a-z_]{1,64}$/.test(value.reason)
  ) {
    return { outcome: "failed", reason: value.reason };
  }
  return null;
}

/**
 * Read the latest bounded acknowledgement outcome recorded for this exact run.
 *
 * A timeline contains server-persisted events only. It deliberately cannot
 * represent a still-queued request, so callers leave that state as pending only
 * while the current page owns the request and never reconstruct it after reload.
 */
function faultAcknowledgementFromTimeline(
  timeline: RoastTimeline | undefined,
  runId: string | null,
): FaultAcknowledgementExecutedEventData | null {
  if (timeline === undefined || runId === null || timeline.run_id !== runId) return null;

  let latest: FaultAcknowledgementExecutedEventData | null = null;
  for (const event of timeline.events) {
    if (
      event.kind === "fault_acknowledgement_executed" &&
      event.source === "operator" &&
      event.payload !== null
    ) {
      const outcome = faultAcknowledgementOutcome(event.payload);
      if (outcome !== null) latest = outcome;
    }
  }
  return latest;
}
