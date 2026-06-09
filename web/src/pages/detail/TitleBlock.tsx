/**
 * Detail title block (E10-S5, ui-prompts Prompt C #1).
 *
 * Bean/profile name, roast date, an outcome chip, and the headline stats (total
 * time, first crack time+temp, drop time+temp, development %). Every stat is
 * derived from the REST telemetry + timeline markers — nothing inferred. A faulted
 * roast shows its `fault_reason` rather than the post-roast stats.
 */

import { cn } from "@/lib/cn";
import type { RoastDetail, RoastOutcome } from "@/lib/types";
import { formatClock, formatDate, formatPercent1, formatTemp } from "./format";
import type { HeadlineStats } from "./traceModel";

const OUTCOME_META: Record<RoastOutcome, { label: string; tone: string }> = {
  completed: { label: "COMPLETED", tone: "border-roast-nominal/40 bg-roast-nominal/15 text-roast-nominal" },
  aborted: { label: "ABORTED", tone: "border-roast-caution/40 bg-roast-caution/15 text-roast-caution" },
  faulted: { label: "FAULTED", tone: "border-roast-fault/40 bg-roast-fault/15 text-roast-fault" },
};

export interface TitleBlockProps {
  detail: RoastDetail;
  stats: HeadlineStats;
  className?: string;
}

export function TitleBlock({ detail, stats, className }: TitleBlockProps): React.JSX.Element {
  const { profile, outcome } = detail;
  const title = profile.bean_varietal
    ? `${profile.name} — ${profile.bean_varietal}`
    : profile.name;

  return (
    <div className={cn("flex flex-col gap-3", className)} data-testid="title-block">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
        {outcome && (
          <span
            data-testid="outcome-chip"
            data-outcome={outcome}
            className={cn(
              "inline-flex items-center rounded-md border px-2 py-0.5 text-xs font-semibold uppercase tracking-wide",
              OUTCOME_META[outcome].tone,
            )}
          >
            {OUTCOME_META[outcome].label}
          </span>
        )}
        <span className="text-sm text-muted-foreground" data-testid="roast-date">
          {formatDate(detail.completed_at_utc ?? detail.started_at_utc)}
        </span>
      </div>

      <p className="text-sm text-muted-foreground" data-testid="bean-origin">
        {profile.bean_origin}
      </p>

      {detail.fault_reason ? (
        <p
          data-testid="fault-reason"
          className="rounded-md border border-roast-fault/40 bg-roast-fault/10 px-3 py-2 text-sm text-roast-fault"
        >
          {detail.fault_reason}
        </p>
      ) : (
        <dl className="flex flex-wrap gap-x-8 gap-y-2" data-testid="headline-stats">
          <Stat label="Total time" value={formatClock(stats.totalSeconds)} testId="stat-total" />
          <Stat
            label="First crack"
            value={`${formatClock(stats.firstCrackSeconds)} · ${formatTemp(stats.firstCrackTempC)}`}
            testId="stat-fc"
          />
          <Stat
            label="Drop"
            value={`${formatClock(stats.dropSeconds)} · ${formatTemp(stats.dropTempC)}`}
            testId="stat-drop"
          />
          <Stat label="Development" value={formatPercent1(stats.developmentPercent)} testId="stat-dev" />
        </dl>
      )}
    </div>
  );
}

interface StatProps {
  label: string;
  value: string;
  testId: string;
}

function Stat({ label, value, testId }: StatProps): React.JSX.Element {
  return (
    <div className="flex flex-col" data-testid={testId}>
      <dt className="text-xs uppercase tracking-wide text-muted-foreground">{label}</dt>
      <dd className="numeric text-lg font-medium">{value}</dd>
    </div>
  );
}
