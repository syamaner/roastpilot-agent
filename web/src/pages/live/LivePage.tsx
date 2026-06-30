/**
 * Single live-roast home at `/live` (#423, D81).
 *
 * Three server-state-driven states (all phase comes from the server):
 *
 * 1. Loading   — holds until health resolves so the start form never flashes
 *    before the active-run status is known.
 * 2. Active run — the full live dashboard (DashboardPage). A reload on this
 *    URL re-hydrates from the server snapshot + SSE — the reload-safe guarantee.
 * 3. No active run — one of two sub-states:
 *    a. Just-finalised this session (`stickyCompletedRunId` is set): the
 *       `LiveFinishedView` shows a mini curve + headline stats + links.
 *    b. Idle / no prior run this session: `LiveStartView`, ready to begin.
 *
 * `stickyCompletedRunId` is session-local (`useState`): it is set the moment
 * `active_run_id` transitions from a non-null value to null. A reload always
 * falls through to state 3b — the completed run is accessible via
 * `/roasts/:runId` and the summary was a session convenience, not permanent.
 *
 * INVARIANTS: active-run presence comes from the SERVER's `/health` snapshot
 * (`active_run_id`) — never inferred client-side (D8). Phase is not read here;
 * DashboardPage owns phase via SSE + snapshot. Operator actions and MCP access
 * live entirely in DashboardPage.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";

import { AppFrame, LiveCurve } from "@/components/shared";
import {
  roastKeys,
  useBeanProfiles,
  useCreateBeanProfile,
  useDeleteBeanProfile,
  useHealth,
  useRoast,
  useTelemetry,
  useUpdateBeanProfile,
} from "@/hooks/queries";
import { api } from "@/lib/api";
import type { BeanProfileInput, RoastProfile } from "@/lib/types";
import { DashboardPage } from "@/pages/dashboard/DashboardPage";
import { StartRoastForm } from "@/pages/dashboard/StartRoastForm";
import { headlineStats, toCurveMarkers, toCurvePoints } from "@/pages/detail/traceModel";

export function LivePage(): React.JSX.Element {
  const health = useHealth();
  const activeRunId = health.data?.active_run_id ?? null;

  // Track the most recent non-null active_run_id across renders. When the id
  // transitions non-null → null we latch it as the just-finished run so
  // LiveFinishedView can fetch and display the outcome.
  const prevRunIdRef = useRef<string | null>(null);
  const [stickyCompletedRunId, setStickyCompletedRunId] = useState<string | null>(null);

  useEffect(() => {
    if (activeRunId !== null) {
      // A run is active — remember it and clear any stale sticky from an older run.
      prevRunIdRef.current = activeRunId;
      // Don't clear a sticky while a new run is still in progress; wait for it to end.
    } else if (prevRunIdRef.current !== null) {
      // Transition: active_run_id was non-null, is now null — latch the finished run.
      setStickyCompletedRunId(prevRunIdRef.current);
      prevRunIdRef.current = null;
    }
  }, [activeRunId]);

  // Health error: active run unknown — treat as idle (fall through to no-run state).
  if (health.isError) {
    return <LiveStartView />;
  }

  // Hold until health resolves so the start form doesn't flash before the active-run
  // status is known (same pattern as the old HomeGate hold).
  if (!health.isSuccess) {
    return (
      <AppFrame>
        <div data-testid="live-page-loading" />
      </AppFrame>
    );
  }

  // Active run: the full live dashboard.
  if (activeRunId !== null) {
    return <DashboardPage />;
  }

  // No active run: show the just-finished summary if this session produced one,
  // otherwise the start form.
  if (stickyCompletedRunId !== null) {
    return (
      <LiveFinishedView
        runId={stickyCompletedRunId}
        onStartNext={() => setStickyCompletedRunId(null)}
      />
    );
  }

  // Idle / fresh session: show the start-roast form.
  return <LiveStartView />;
}

// --- LiveFinishedView: sticky post-roast summary for the just-completed run. ---

interface LiveFinishedViewProps {
  /** The run id that just completed in this session. */
  runId: string;
  /** Clear the sticky state so the operator can start the next roast. */
  onStartNext: () => void;
}

/**
 * Summary shown at `/live` immediately after a roast ends in the current session.
 *
 * Fetches the RoastDetail snapshot + the telemetry series (downsampled to 5)
 * and renders a mini curve + headline stats. 'Start next roast' clears the
 * sticky and returns to the start form. Reload always bypasses this view
 * (stickyCompletedRunId is session-only; reload → LiveStartView).
 */
function LiveFinishedView({ runId, onStartNext }: LiveFinishedViewProps): React.JSX.Element {
  const roast = useRoast(runId);
  const telemetry = useTelemetry(runId, 5);

  const stats = headlineStats(undefined, telemetry.data);
  const points = toCurvePoints(telemetry.data);
  const markers = toCurveMarkers(undefined, telemetry.data);

  const beanOrigin = roast.data?.profile.bean_origin ?? null;
  const outcome = roast.data?.outcome ?? null;

  return (
    <AppFrame
      headerRight={
        <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          Roast complete
        </span>
      }
    >
      <div className="mx-auto max-w-3xl" data-testid="live-finished-view">
        {/* Run identity */}
        <header className="mb-6">
          <h2 className="font-mono text-2xl text-foreground">
            {beanOrigin ?? "Roast complete"}
          </h2>
          {outcome !== null && (
            <p
              className="mt-1 text-sm capitalize text-muted-foreground"
              data-testid="live-finished-outcome"
            >
              {outcome}
            </p>
          )}
        </header>

        {/* Headline stats */}
        <div
          className="mb-6 grid grid-cols-2 gap-3 sm:grid-cols-4"
          data-testid="live-finished-stats"
        >
          <StatTile
            testId="stat-drop-temp"
            label="Drop temp"
            value={stats.dropTempC !== null ? `${Math.round(stats.dropTempC)} °C` : "—"}
          />
          <StatTile
            testId="stat-dev-percent"
            label="Dev %"
            value={
              stats.developmentPercent !== null
                ? `${stats.developmentPercent.toFixed(1)} %`
                : "—"
            }
          />
          <StatTile
            testId="stat-total-time"
            label="Total time"
            value={stats.totalSeconds !== null ? formatDuration(stats.totalSeconds) : "—"}
          />
          <StatTile
            testId="stat-weight-loss"
            label="Weight loss"
            value={
              roast.data?.weight_loss_percent != null
                ? `${roast.data.weight_loss_percent.toFixed(1)} %`
                : "—"
            }
          />
        </div>

        {/* Mini curve — only when we have data */}
        {points.length > 0 && (
          <div className="mb-6 rounded-lg border border-border bg-card p-4" data-testid="live-finished-curve">
            <LiveCurve points={points} markers={markers} height={180} />
          </div>
        )}

        {/* Actions */}
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
          <Link
            to={`/roasts/${runId}`}
            className="flex-1 rounded-md border border-border bg-card px-5 py-3 text-center text-sm font-medium text-foreground transition-colors hover:bg-accent/40"
            data-testid="live-finished-view-detail"
          >
            View full detail
          </Link>
          <button
            type="button"
            onClick={onStartNext}
            className="flex-1 rounded-md bg-primary px-5 py-3 text-center text-sm font-semibold text-primary-foreground transition-colors hover:bg-primary/90"
            data-testid="live-finished-start-next"
          >
            Start next roast
          </button>
        </div>
      </div>
    </AppFrame>
  );
}

/** A single headline-stat card. */
function StatTile({
  label,
  value,
  testId,
}: {
  label: string;
  value: string;
  testId: string;
}): React.JSX.Element {
  return (
    <div
      className="flex flex-col gap-1 rounded-lg border border-border bg-card px-4 py-3"
      data-testid={testId}
    >
      <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
        {label}
      </span>
      <span className="font-mono text-xl font-semibold text-foreground">{value}</span>
    </div>
  );
}

/** Format total seconds as `M:SS`. */
function formatDuration(totalSeconds: number): string {
  const m = Math.floor(totalSeconds / 60);
  const s = Math.round(totalSeconds % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

// --- LiveStartView: start-roast form shown at /live when no run is active. ---

function LiveStartView(): React.JSX.Element {
  const queryClient = useQueryClient();

  const beanProfiles = useBeanProfiles();
  const createBeanProfile = useCreateBeanProfile();
  const updateBeanProfile = useUpdateBeanProfile();
  const deleteBeanProfile = useDeleteBeanProfile();

  const handleCreateProfile = useCallback(
    (input: BeanProfileInput) => createBeanProfile.mutateAsync(input),
    [createBeanProfile],
  );
  const handleUpdateProfile = useCallback(
    (id: string, input: BeanProfileInput) => updateBeanProfile.mutateAsync({ id, input }),
    [updateBeanProfile],
  );
  const handleArchiveProfile = useCallback(
    (id: string) => deleteBeanProfile.mutateAsync(id),
    [deleteBeanProfile],
  );

  // Start a roast: POST, then AWAIT a health refetch so the cache holds the new
  // active_run_id BEFORE re-evaluating. LiveStartView only ever mounts at /live,
  // so the health refetch alone re-renders the page into DashboardPage — no
  // navigate() call needed (which would push a duplicate /live history entry).
  const handleStartRoast = useCallback(
    async (profile: RoastProfile) => {
      await api.startRoast(profile);
      await queryClient.refetchQueries({ queryKey: roastKeys.health });
    },
    [queryClient],
  );

  return (
    <AppFrame
      headerRight={
        <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          New roast
        </span>
      }
    >
      <div className="flex flex-col gap-4" data-testid="live-start-view">
        <StartRoastForm
          onStart={handleStartRoast}
          profiles={beanProfiles.data?.profiles ?? []}
          profilesLoading={beanProfiles.isLoading}
          onCreateProfile={handleCreateProfile}
          onUpdateProfile={handleUpdateProfile}
          onArchiveProfile={handleArchiveProfile}
        />
      </div>
    </AppFrame>
  );
}
