/**
 * Live roast dashboard — the demo centerpiece (plan §7, ui-prompts Prompt A/B,
 * kickoff §2).
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

import { useCallback, useEffect, useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { AppFrame, ConnectionIndicator, LiveCurve } from "@/components/shared";
import { roastKeys, useHealth, useRoast } from "@/hooks/queries";
import { useRoastStream } from "@/hooks/useRoastStream";
import { api } from "@/lib/api";
import type { OperatorAction } from "@/lib/types";
import { AdvisoryPanel } from "./AdvisoryPanel";
import { ChargeBanner } from "./ChargeBanner";
import { isInChargeWindow } from "./chargeWindow";
import { ControlRow } from "./ControlRow";
import { FaultBanner } from "./FaultBanner";
import { resolveMicStatus } from "./micStatus";
import { OperatorActionBar, type OperatorActionResultView } from "./OperatorActionBar";
import { RecoveryModal } from "./RecoveryModal";
import { RoastHeader } from "./RoastHeader";
import { StartRoastForm } from "./StartRoastForm";
import { useDashboardEvents } from "./useDashboardEvents";

export function DashboardPage(): React.JSX.Element {
  const health = useHealth();
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

  // Idle state (#158): health has loaded and reports NO active run (and no
  // faulted run is pinned). The dashboard then shows the Start-roast form
  // instead of the (empty) live view. We render the form only once health has
  // resolved so it does not flash before the active run is known. The transition
  // to live is server-driven: on a 201 the next `useHealth` refetch surfaces the
  // new `active_run_id`, the page re-renders with a runId, and `useRoastStream`
  // connects — the SPA renders from server state, never fabricated.
  const isIdle = health.isSuccess && runId === null;

  // Live SSE stream — phase/telemetry/enabledActions are server-derived; the
  // page-local reducer folds the NON-LOSSY frame buffer (frames/frameCount) so a
  // burst never drops a fault/recovery/advisory/marker frame (#122).
  const { status, phase, telemetry, enabledActions, frames, frameCount } = useRoastStream(runId);
  // The view-model seeds the curve from the `/telemetry` snapshot on every
  // (re)connect — keyed on `status` — so a late-joining or reconnecting device shows
  // the full roast curve, not just the frames it personally witnessed (#153).
  const view = useDashboardEvents(frames, frameCount, runId, status);

  // The run snapshot (profile name + initial enabled actions before the first
  // phase_changed). Read-only REST snapshot, hydrated by TanStack Query.
  const detail = useRoast(runId);

  // Operator action POST result (the action bar surfaces its typed reason).
  const [lastResult, setLastResult] = useState<OperatorActionResultView | null>(null);

  const queryClient = useQueryClient();

  // Start a roast from the idle form (#158). On 201 we refetch health so the new
  // `active_run_id` is discovered and the dashboard swaps to the live view — we do
  // NOT fabricate a runId here (render from server state). Errors (e.g. 409) are
  // surfaced inline by the form, which catches the thrown ApiError.
  const handleStartRoast = useCallback(
    async (profile: Parameters<typeof api.startRoast>[0]) => {
      await api.startRoast(profile);
      await queryClient.invalidateQueries({ queryKey: roastKeys.health });
    },
    [queryClient],
  );

  // #206: the operator acknowledges a fault by POSTing the `acknowledge_fault`
  // control action. Post-#206 a fault no longer auto-finalises the run — it stays
  // operable (loop alive, heat off) so the operator can still cool the machine —
  // so the run is finalised (outcome `faulted`) only by this acknowledgement.
  // Acknowledging clears `active_run_id` on the server; we then drop the sticky-
  // faulted pin and re-fetch health, returning the page to the idle Start-roast
  // form (never trapping the operator on the faulted view, #124). `acknowledge_fault`
  // issues no roaster command (heat is already off in faulted and stays off).
  const handleAcknowledgeFault = useCallback(async () => {
    const ackRunId = runId;
    // Drop the sticky-faulted pin optimistically so the page returns to idle as
    // soon as health reports no active run — the acknowledgement is the operator's
    // explicit intent and the server finalisation below makes it authoritative.
    setStickyFaultedRunId(null);
    if (ackRunId !== null) {
      try {
        await api.operatorAction(ackRunId, { action: "acknowledge_fault" });
      } catch {
        // Best-effort: a failed acknowledge (e.g. transient) still re-fetches
        // health below; the operator can retry from the live view.
      }
    }
    await queryClient.invalidateQueries({ queryKey: roastKeys.health });
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

  // Development timer (GAP A / #112): the live telemetry frame carries no
  // development_percent, so we show time SINCE first crack — derivable from the
  // FC event vs the current elapsed. Null until FC fires.
  const [fcElapsed, setFcElapsed] = useState<number | null>(null);

  // Charge-window dwell (#211): the elapsed time at which the bean first entered
  // the charge window. The banner shows how long the bean has been in the window
  // to discourage over-preheating an empty drum (the 2nd-roast failure mode).
  const [chargeEnteredElapsed, setChargeEnteredElapsed] = useState<number | null>(null);

  // Reset the per-run local state when the run changes (the view-model resets in
  // the hook): a new run must not inherit the previous run's FC baseline or a
  // stale toast dismissal. The page can stay mounted across runs.
  useEffect(() => {
    setFcElapsed(null);
    setChargeEnteredElapsed(null);
    setLastResult(null);
  }, [runId]);

  // #124: pin the run as soon as it faults, so a later `active_run_id`→null from
  // a health refetch resolves `runId` back to this faulted run (via the `??`
  // above) instead of collapsing to idle and dropping the fault banner. Pinning
  // the id of the run we are already watching is not phase inference — the fault
  // came from the server's SSE frame; cleared by `handleAcknowledgeFault`.
  useEffect(() => {
    if (view.fault !== null && runId !== null) {
      setStickyFaultedRunId(runId);
    }
  }, [view.fault, runId]);

  useEffect(() => {
    if (view.firstCrack !== null && fcElapsed === null && telemetry?.elapsed_seconds != null) {
      setFcElapsed(telemetry.elapsed_seconds);
    }
  }, [view.firstCrack, fcElapsed, telemetry?.elapsed_seconds]);
  const developmentSeconds =
    fcElapsed !== null && telemetry?.elapsed_seconds != null
      ? telemetry.elapsed_seconds - fcElapsed
      : null;

  // Advisor targets for the control-row ghost markers (latest decision).
  const targetHeat = view.latestAdvisory?.decision?.target_heat ?? null;
  const targetFan = view.latestAdvisory?.decision?.target_fan ?? null;

  // Effective enabled actions: the live SSE mirror once a phase_changed has
  // arrived, else the snapshot's set (so the bar is correct on first paint).
  const effectiveEnabled = enabledActions ?? detail.data?.enabled_actions ?? null;

  // Persistent charge cue (#211): derive "bean is in the charge window" from the
  // SERVER phase (preheating), the live bean temperature, and the profile's charge
  // band from the REST snapshot. This is a PRESENTATION derivation — phase still
  // comes only from the server; we never infer phase here.
  const chargeBand = detail.data
    ? {
        minC: detail.data.profile.charge_guidance_min_c,
        maxC: detail.data.profile.charge_guidance_max_c,
      }
    : null;
  const beanTempC = telemetry?.bean_temp_c ?? null;
  const inChargeWindow = isInChargeWindow(phase, beanTempC, chargeBand);

  // Stamp the elapsed time the bean first entered the window; clear it when it
  // leaves (so a re-entry restarts the dwell). Derived from server telemetry +
  // the presentation in-window boolean — not phase inference.
  const elapsedSeconds = telemetry?.elapsed_seconds ?? null;
  useEffect(() => {
    if (inChargeWindow) {
      setChargeEnteredElapsed((prev) => (prev === null ? elapsedSeconds : prev));
    } else {
      setChargeEnteredElapsed(null);
    }
  }, [inChargeWindow, elapsedSeconds]);
  const dwellSeconds =
    inChargeWindow && chargeEnteredElapsed !== null && elapsedSeconds !== null
      ? elapsedSeconds - chargeEnteredElapsed
      : null;

  const inRecovery = phase === "operator_recovery_required";

  const curve = useMemo(
    () => ({ points: view.points, markers: view.markers }),
    [view.points, view.markers],
  );

  // IDLE: no active run. Show the Start-roast form (#158) so the operator can start
  // a roast without `curl` (E11 headless appliance). Once a roast is active this
  // branch is not taken and the live dashboard below renders, server-driven.
  if (isIdle) {
    // No run to connect to, so the "connecting" stream indicator would be
    // misleading — show a neutral idle label instead (#160 review item 3).
    return (
      <AppFrame
        headerRight={
          <span
            data-testid="idle-indicator"
            className="text-xs font-semibold uppercase tracking-wide text-muted-foreground"
          >
            No active roast
          </span>
        }
      >
        <div className="flex flex-col gap-4" data-testid="dashboard-idle">
          <StartRoastForm onStart={handleStartRoast} />
        </div>
      </AppFrame>
    );
  }

  return (
    <AppFrame headerRight={<ConnectionIndicator status={status} />}>
      <div className="flex flex-col gap-4" data-testid="dashboard">
        {/* Fault banner sits above the dashboard when faulted (Prompt B §2).
            Informational + persistent: the fault stays on screen until the
            operator acknowledges it; cooling/e-stop live in the action bar (the
            faulted run stays operable, #206). The "Start New Roast" affordance
            dispatches the genuine `acknowledge_fault` action — finalising the
            operable-faulted run server-side — then clears the sticky-faulted pin
            (#124) and re-fetches health, returning to the idle Start-roast form.
            `acknowledge_fault` issues no roaster command (heat is already off). */}
        <FaultBanner
          fault={view.fault}
          trail={view.safetyTrail}
          startNewRoast={
            <button
              type="button"
              onClick={handleAcknowledgeFault}
              data-testid="fault-start-new-roast"
              className="inline-flex items-center rounded-md border border-roast-fault/60 bg-roast-fault/15 px-4 py-2 text-sm font-semibold uppercase tracking-wide text-roast-fault transition-colors hover:bg-roast-fault/25"
            >
              Start New Roast
            </button>
          }
        />

        <RoastHeader
          phase={phase}
          elapsedSeconds={telemetry?.elapsed_seconds ?? null}
          developmentSeconds={developmentSeconds}
          beanRorCPerMin={telemetry?.bean_ror_c_per_min ?? null}
          profileName={detail.data?.profile.name ?? null}
          firstCrack={view.firstCrack}
          mcpChild={health.data?.mcp_child}
          // Capture-alive mic health (#197/#200): live frame is authoritative once
          // present (its null = idle passes through, not the stale snapshot); the
          // snapshot only paints on hydrate before the first frame. Server-derived.
          micStatus={resolveMicStatus(telemetry, detail.data?.mic_status)}
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
        />

        <ControlRow
          heatPercent={telemetry?.heat_percent ?? null}
          fanPercent={telemetry?.fan_percent ?? null}
          targetHeatPercent={targetHeat}
          targetFanPercent={targetFan}
        />

        <AdvisoryPanel
          latest={view.latestAdvisory}
          history={view.advisoryHistory}
          paused={view.advisoryPaused}
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
