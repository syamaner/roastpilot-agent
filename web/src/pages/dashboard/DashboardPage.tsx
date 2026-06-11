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
import { Link } from "react-router-dom";

import { AppFrame, ConnectionIndicator, LiveCurve } from "@/components/shared";
import { useHealth, useRoast } from "@/hooks/queries";
import { useRoastStream } from "@/hooks/useRoastStream";
import { api } from "@/lib/api";
import type { OperatorAction } from "@/lib/types";
import { AddBeansToast } from "./AddBeansToast";
import { AdvisoryPanel } from "./AdvisoryPanel";
import { ControlRow } from "./ControlRow";
import { FaultBanner } from "./FaultBanner";
import { OperatorActionBar, type OperatorActionResultView } from "./OperatorActionBar";
import { RecoveryModal } from "./RecoveryModal";
import { RoastHeader } from "./RoastHeader";
import { useDashboardEvents } from "./useDashboardEvents";

export function DashboardPage(): React.JSX.Element {
  const health = useHealth();
  const runId = health.data?.active_run_id ?? null;

  // Live SSE stream — phase/telemetry/enabledActions are server-derived; the
  // page-local reducer folds the NON-LOSSY frame buffer (frames/frameCount) so a
  // burst never drops a fault/recovery/advisory/marker frame (#122).
  const { status, phase, telemetry, enabledActions, frames, frameCount } = useRoastStream(runId);
  const view = useDashboardEvents(frames, frameCount, runId);

  // The run snapshot (profile name + initial enabled actions before the first
  // phase_changed). Read-only REST snapshot, hydrated by TanStack Query.
  const detail = useRoast(runId);

  // Operator action POST result (the action bar surfaces its typed reason).
  const [lastResult, setLastResult] = useState<OperatorActionResultView | null>(null);
  // Add-beans toast dismissal (the toast is non-blocking guidance).
  const [toastDismissed, setToastDismissed] = useState(false);

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

  // Reset the per-run local state when the run changes (the view-model resets in
  // the hook): a new run must not inherit the previous run's FC baseline or a
  // stale toast dismissal. The page can stay mounted across runs.
  useEffect(() => {
    setFcElapsed(null);
    setToastDismissed(false);
    setLastResult(null);
  }, [runId]);

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

  const showToast = !toastDismissed && view.chargeGuidance !== null;
  const inRecovery = phase === "operator_recovery_required";

  const curve = useMemo(
    () => ({ points: view.points, markers: view.markers }),
    [view.points, view.markers],
  );

  return (
    <AppFrame headerRight={<ConnectionIndicator status={status} />}>
      <div className="flex flex-col gap-4" data-testid="dashboard">
        {/* Fault banner sits above the dashboard when faulted (Prompt B §2).
            Informational + persistent: no server-dispatching button (a fault is
            terminal and must not be hidden; e-stop lives in the action bar). The
            only affordance is a forward nav — starting a new roast — which
            navigates, never dispatching a roaster command. */}
        <FaultBanner
          fault={view.fault}
          trail={view.safetyTrail}
          startNewRoast={
            <Link
              to="/roasts"
              data-testid="fault-start-new-roast"
              className="inline-flex items-center rounded-md border border-roast-fault/60 bg-roast-fault/15 px-4 py-2 text-sm font-semibold uppercase tracking-wide text-roast-fault transition-colors hover:bg-roast-fault/25"
            >
              Start New Roast
            </Link>
          }
        />

        <RoastHeader
          phase={phase}
          elapsedSeconds={telemetry?.elapsed_seconds ?? null}
          developmentSeconds={developmentSeconds}
          profileName={detail.data?.profile.name ?? null}
          firstCrack={view.firstCrack}
          mcpChild={health.data?.mcp_child}
        />

        {showToast && (
          <AddBeansToast
            guidance={view.chargeGuidance}
            visible={showToast}
            onDismiss={() => setToastDismissed(true)}
          />
        )}

        <LiveCurve
          points={curve.points}
          markers={curve.markers}
          phase={phase}
          chargeBand={
            detail.data
              ? {
                  minC: detail.data.profile.charge_guidance_min_c,
                  maxC: detail.data.profile.charge_guidance_max_c,
                }
              : undefined
          }
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
