/**
 * Dev/test-only foundation SSE smoke route (`/__stream-smoke`).
 *
 * NOT a product page. It proves the foundation's live data path end-to-end
 * against the REAL replay harness (E10-S1): resolve the active run from
 * `GET /api/health`, then `useRoastStream` hydrates from `GET /api/roasts/{id}`
 * and applies typed SSE frames. It renders only the server-derived phase + the
 * connection status + the current bean temp — enough for the Playwright smoke
 * snapshot to assert the webServer + deterministic `--step` stepping drive real
 * state into the SPA (S2 acceptance), without building any product page (S3+).
 *
 * The product page snapshots (dashboard-live/fault/recovery, detail, history)
 * land with their pages (S3–S5) / the full state matrix (S6), reusing this exact
 * hydrate→SSE→reducer path.
 */

import { AppFrame, ConnectionIndicator } from "@/components/shared";
import { useHealth } from "@/hooks/queries";
import { useRoastStream } from "@/hooks/useRoastStream";

export function StreamSmokePage(): React.JSX.Element {
  const health = useHealth();
  const runId = health.data?.active_run_id ?? null;
  const { status, phase, telemetry } = useRoastStream(runId);

  return (
    <AppFrame headerRight={<ConnectionIndicator status={status} />}>
      <div className="flex flex-col gap-3" data-testid="stream-smoke">
        <div>
          <span className="text-xs text-muted-foreground">active run</span>
          <div className="numeric text-sm" data-testid="smoke-run-id">
            {runId ?? "—"}
          </div>
        </div>
        <div>
          <span className="text-xs text-muted-foreground">server phase</span>
          {/* Phase is server-derived only — straight from the reducer. */}
          <div className="text-lg font-semibold" data-testid="smoke-phase">
            {phase ?? "—"}
          </div>
        </div>
        <div>
          <span className="text-xs text-muted-foreground">bean temp</span>
          <div className="numeric text-lg" data-testid="smoke-bean-temp">
            {telemetry?.bean_temp_c != null ? `${telemetry.bean_temp_c.toFixed(1)} °C` : "—"}
          </div>
        </div>
      </div>
    </AppFrame>
  );
}
