/**
 * Dev/test-only LiveFinishedView snapshot harness (`/__live-finished-harness`).
 *
 * NOT a product page. Mounts `LiveFinishedView` directly over deterministic
 * fixture data (seeded into the shared QueryClient) so the Playwright snapshot
 * suite has a stable target for the `live-finished` state without requiring a
 * live backend session that just ended. Mirrors the `/__detail-harness` pattern.
 *
 * The fixture seeds:
 *   - `useRoast(FIXTURE_FINISHED_RUN_ID)` → FIXTURE_FINISHED_DETAIL
 *   - `useTelemetry(FIXTURE_FINISHED_RUN_ID, 5)` → FIXTURE_FINISHED_TELEMETRY
 *   - `useHealth` → idle (no active run) so the nav shows "Home", not "Live roast"
 *
 * The data-assert layer in `live-finished.spec.ts` checks the stat tiles and the
 * "View full detail" href BEFORE the pixel snapshot — a content regression fails
 * on behaviour, not only on the (CI-regenerated) baseline.
 *
 * NOTE: the `live-finished` baseline is owned by the CI Docker snapshot job
 * (D26) — it must be (re)generated there, not committed from a local macOS run.
 */

import { roastKeys } from "@/hooks/queries";
import { queryClient } from "@/lib/queryClient";
import type { HealthResponse } from "@/lib/types";

import { NavBar } from "@/pages/home/NavBar";
import {
  FIXTURE_FINISHED_DETAIL,
  FIXTURE_FINISHED_RUN_ID,
  FIXTURE_FINISHED_TELEMETRY,
} from "./liveFinishedFixture";

// Seed a deterministic idle health snapshot so the nav slot shows "Home" (no
// active run) and LiveFinishedView's `useRoast` / `useTelemetry` resolve
// synchronously from the pre-seeded cache — no network, fully deterministic.
const IDLE_HEALTH: HealthResponse = {
  status: "ok",
  version: "harness",
  instance_id: "harness-instance",
  mcp_child: "running",
  mcp_hardware_clear_required: false,
  mcp_teardown_incident_id: null,
  active_run_id: null,
};
queryClient.setQueryData(roastKeys.health, IDLE_HEALTH);
queryClient.setQueryData(
  ["roasts", FIXTURE_FINISHED_RUN_ID],
  FIXTURE_FINISHED_DETAIL,
);
// Full-resolution series (downsample=1) — used by LiveFinishedView for headline
// stats (P2-2: ensures the drop/terminal row is included regardless of stride).
queryClient.setQueryData(
  roastKeys.telemetry(FIXTURE_FINISHED_RUN_ID, 1),
  FIXTURE_FINISHED_TELEMETRY,
);
// Downsampled series (downsample=5) — used by LiveFinishedView for the mini curve.
queryClient.setQueryData(
  roastKeys.telemetry(FIXTURE_FINISHED_RUN_ID, 5),
  FIXTURE_FINISHED_TELEMETRY,
);

// LiveFinishedView is not exported from LivePage (it is internal), so we re-
// implement the minimum shell here: the same structure LivePage renders when
// stickyCompletedRunId is set. This keeps the harness thin and avoids exporting
// an internal component solely for testing.
import { useRoast, useTelemetry } from "@/hooks/queries";
import { AppFrame, LiveCurve } from "@/components/shared";
import { headlineStats, toCurveMarkers, toCurvePoints } from "@/pages/detail/traceModel";
import { Link } from "react-router-dom";

function LiveFinishedHarnessView(): React.JSX.Element {
  const roast = useRoast(FIXTURE_FINISHED_RUN_ID);
  // Full-resolution for stats (P2-2: guarantees drop/terminal row is present).
  const telemetryFull = useTelemetry(FIXTURE_FINISHED_RUN_ID, 1);
  // Downsampled for the mini curve only.
  const telemetryCurve = useTelemetry(FIXTURE_FINISHED_RUN_ID, 5);

  const stats = headlineStats(undefined, telemetryFull.data);
  const points = toCurvePoints(telemetryCurve.data);
  const markers = toCurveMarkers(undefined, telemetryCurve.data);

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

        <div
          className="mb-6 grid grid-cols-2 gap-3 sm:grid-cols-4"
          data-testid="live-finished-stats"
        >
          <StatTile
            label="Drop temp"
            value={stats.dropTempC !== null ? `${Math.round(stats.dropTempC)} °C` : "—"}
            testId="stat-drop-temp"
          />
          <StatTile
            label="Dev %"
            value={
              stats.developmentPercent !== null
                ? `${stats.developmentPercent.toFixed(1)} %`
                : "—"
            }
            testId="stat-dev-percent"
          />
          <StatTile
            label="Total time"
            value={stats.totalSeconds !== null ? formatDuration(stats.totalSeconds) : "—"}
            testId="stat-total-time"
          />
          <StatTile
            label="Weight loss"
            value={
              roast.data?.weight_loss_percent != null
                ? `${roast.data.weight_loss_percent.toFixed(1)} %`
                : "—"
            }
            testId="stat-weight-loss"
          />
        </div>

        {points.length > 0 && (
          <div
            className="mb-6 rounded-lg border border-border bg-card p-4"
            data-testid="live-finished-curve"
          >
            <LiveCurve points={points} markers={markers} height={180} />
          </div>
        )}

        <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
          <Link
            to={`/roasts/${FIXTURE_FINISHED_RUN_ID}`}
            className="flex-1 rounded-md border border-border bg-card px-5 py-3 text-center text-sm font-medium text-foreground transition-colors hover:bg-accent/40"
            data-testid="live-finished-view-detail"
          >
            View full detail
          </Link>
          <Link
            to="/start"
            className="flex-1 rounded-md bg-primary px-5 py-3 text-center text-sm font-semibold text-primary-foreground transition-colors hover:bg-primary/90"
            data-testid="live-finished-start-next"
          >
            Start next roast
          </Link>
        </div>
      </div>
    </AppFrame>
  );
}

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

function formatDuration(totalSeconds: number): string {
  const m = Math.floor(totalSeconds / 60);
  const s = Math.round(totalSeconds % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

export function LiveFinishedHarnessPage(): React.JSX.Element {
  return (
    <div
      className="min-h-screen bg-background text-foreground"
      data-testid="live-finished-harness"
    >
      <NavBar />
      <LiveFinishedHarnessView />
    </div>
  );
}
