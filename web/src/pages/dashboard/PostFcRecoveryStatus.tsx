/** Compact, read-only D96 recovery-authority status for a live roast (#699). */

import { formatPercent, formatRoR } from "./format";
import type { PostFcControlTrace } from "./useDashboardEvents";

export interface PostFcRecoveryStatusProps {
  trace: PostFcControlTrace | null | undefined;
}

const STATE_LABEL = {
  holding: "Armed · Holding",
  recovering: "Recovery entry",
  gliding: "Exit glide",
} as const;

export function PostFcRecoveryStatus({
  trace,
}: PostFcRecoveryStatusProps): React.JSX.Element {
  const label =
    trace == null
      ? "No recovery trace"
      : !trace.recoveryEnabled
        ? "Recovery off"
        : trace.heatAuthorityState === null
          ? "Recovery armed"
          : STATE_LABEL[trace.heatAuthorityState];
  const active =
    trace?.recoveryEnabled === true &&
    (trace.heatAuthorityState === "recovering" ||
      trace.heatAuthorityState === "gliding");

  return (
    <section
      data-testid="post-fc-recovery-status"
      data-state={trace?.heatAuthorityState ?? (trace?.recoveryEnabled ? "armed" : "off")}
      className="rounded-lg border border-border bg-card px-4 py-3"
      aria-label="Post-first-crack recovery authority"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Post-FC authority
          </span>
          <span
            className={`rounded-full px-2 py-0.5 text-xs font-semibold ${
              active
                ? "bg-roast-heat/15 text-roast-heat"
                : "bg-secondary text-muted-foreground"
            }`}
          >
            {label}
          </span>
        </div>
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
          <span>
            RoR <strong className="numeric text-foreground">{formatRoR(trace?.smoothedRorCPerMin)}</strong>
          </span>
          <span>
            Target <strong className="numeric text-foreground">{formatRoR(trace?.rorSetpointCPerMin)}</strong>
          </span>
          <span>
            Heat ceiling <strong className="numeric text-foreground">{formatPercent(trace?.effectiveHeatCeilingPercent)}</strong>
          </span>
        </div>
      </div>
    </section>
  );
}
