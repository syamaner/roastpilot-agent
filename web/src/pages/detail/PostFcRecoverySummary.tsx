/** Retained D96 recovery-authority validation summary for roast detail (#699). */

import type { TelemetrySeries } from "@/lib/types";
import { formatClock, formatPercent } from "./format";
import { postFcRecoverySummary } from "./recoveryModel";

export function PostFcRecoverySummary({
  telemetry,
}: {
  telemetry: TelemetrySeries | undefined;
}): React.JSX.Element {
  const summary = postFcRecoverySummary(telemetry);
  return (
    <section
      data-testid="post-fc-recovery-summary"
      className="rounded-lg border border-border bg-card p-4"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm font-semibold tracking-tight">Post-FC recovery trace</h2>
        <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          {summary.recoveryEnabled === null
            ? "Pre-v16 / unavailable"
            : summary.recoveryEnabled
              ? "Recovery armed"
              : "Recovery off"}
        </span>
      </div>
      {!summary.observedRecovery ? (
        <p className="mt-3 text-sm text-muted-foreground">No observed recovery.</p>
      ) : (
        <dl className="mt-3 grid grid-cols-2 gap-3 text-sm sm:grid-cols-3 lg:grid-cols-6">
          <Metric label="Cycles" value={String(summary.cycleCount)} />
          <Metric label="First recovery" value={formatClock(summary.firstRecoveryChargeSeconds)} />
          <Metric label="Max ceiling" value={formatPercent(summary.maxEffectiveHeatCeilingPercent)} />
          <Metric label="Recovering" value={formatClock(summary.recoveringDurationSeconds)} />
          <Metric label="Exit glide" value={formatClock(summary.glidingDurationSeconds)} />
          <Metric label="Glide retriggers" value={String(summary.glideToRecoveryRetriggerCount)} />
        </dl>
      )}
    </section>
  );
}

function Metric({ label, value }: { label: string; value: string }): React.JSX.Element {
  return (
    <div>
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="numeric mt-0.5 font-semibold text-foreground">{value}</dd>
    </div>
  );
}
