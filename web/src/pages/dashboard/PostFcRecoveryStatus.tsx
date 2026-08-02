/** Compact, read-only D96 recovery-authority status for a live roast (#699). */

import { formatPercent, formatRoR } from "./format";
import type { RoastPhase } from "@/lib/types";
import type { PostFcControlTrace } from "./useDashboardEvents";

export interface PostFcRecoveryStatusProps {
  phase: RoastPhase | null;
  trace: PostFcControlTrace | null | undefined;
}

const STATE_LABEL = {
  holding: "Armed · Holding",
  recovering: "Recovery entry",
  gliding: "Exit glide",
} as const;

export function PostFcRecoveryStatus({
  phase,
  trace,
}: PostFcRecoveryStatusProps): React.JSX.Element {
  // A persisted trace describes what the controller accepted historically. The
  // hydrated server phase owns whether that authority is live now: after restart
  // into recovery there may be no replayed phase-change or telemetry frame to
  // clear the old output, so never present its diagnostics outside DEVELOPMENT.
  const currentTrace =
    phase === "development" || trace == null
      ? trace
      : {
          ...trace,
          heatAuthorityState: null,
          rorSetpointCPerMin: null,
          smoothedRorCPerMin: null,
          effectiveHeatCeilingPercent: null,
        };
  const label =
    currentTrace == null
      ? "No recovery trace"
      : !currentTrace.recoveryEnabled
        ? "Recovery off"
        : currentTrace.heatAuthorityState === null
          ? "Recovery armed"
          : STATE_LABEL[currentTrace.heatAuthorityState];
  const active =
    currentTrace?.recoveryEnabled === true &&
    (currentTrace.heatAuthorityState === "recovering" ||
      currentTrace.heatAuthorityState === "gliding");

  return (
    <section
      data-testid="post-fc-recovery-status"
      data-state={
        currentTrace?.recoveryEnabled === true
          ? (currentTrace.heatAuthorityState ?? "armed")
          : "off"
      }
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
            RoR <strong className="numeric text-foreground">{formatRoR(currentTrace?.smoothedRorCPerMin)}</strong>
          </span>
          <span>
            Target <strong className="numeric text-foreground">{formatRoR(currentTrace?.rorSetpointCPerMin)}</strong>
          </span>
          <span>
            Heat ceiling <strong className="numeric text-foreground">{formatPercent(currentTrace?.effectiveHeatCeilingPercent)}</strong>
          </span>
        </div>
      </div>
    </section>
  );
}
