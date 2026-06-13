/**
 * Roast-history table (E10-S4).
 *
 * Columns are exactly what `GET /api/roasts` (`RoastSummary`) carries: Date,
 * Bean (origin + varietal, two-line), Outcome, Dev %, Rating. Profile name, FC
 * time, drop temp, and the sparkline curve from the prototype are intentionally
 * absent — they are not in the M1 contract (D7: no named profiles; FC time
 * deferred to #111). Rows are activated (click / Enter / Space) to open the
 * detail page; the row is the only navigation affordance.
 */

import { useNavigate } from "react-router-dom";

import { cn } from "@/lib/cn";
import type { RoastSummary } from "@/lib/types";

import { beanLabel, formatDevPercent, formatStartedAt } from "./format";
import { HistoryAdvisorCell } from "./HistoryAdvisorCell";
import { OutcomeBadge } from "./OutcomeBadge";
import { StarRating } from "./StarRating";

export interface HistoryTableProps {
  runs: readonly RoastSummary[];
}

const HEAD_CELL = "px-6 py-3 text-left font-mono text-xs uppercase tracking-wide text-muted-foreground";
const BODY_CELL = "px-6 py-4 align-top";

export function HistoryTable({ runs }: HistoryTableProps): React.JSX.Element {
  const navigate = useNavigate();
  const open = (runId: string): void => {
    navigate(`/roasts/${runId}`);
  };

  return (
    <div className="overflow-hidden rounded-lg border border-border bg-card" data-testid="history-table">
      <div className="overflow-x-auto">
        <table className="w-full border-collapse">
          <thead className="border-b border-border bg-background">
            <tr>
              <th className={HEAD_CELL}>Date</th>
              <th className={HEAD_CELL}>Bean</th>
              <th className={HEAD_CELL}>Outcome</th>
              <th className={HEAD_CELL}>Advisor</th>
              <th className={HEAD_CELL}>Dev %</th>
              <th className={HEAD_CELL}>Rating</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((run, i) => (
              <tr
                key={run.id}
                data-testid="history-row"
                data-run-id={run.id}
                tabIndex={0}
                role="link"
                aria-label={`Open roast ${beanLabel(run)} from ${formatStartedAt(run.started_at_utc)}`}
                onClick={() => open(run.id)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    open(run.id);
                  }
                }}
                className={cn(
                  "cursor-pointer border-b border-border transition-colors last:border-b-0",
                  "hover:bg-muted/40 focus-visible:bg-muted/40 focus-visible:outline-none",
                  i % 2 === 1 && "bg-background/30",
                )}
              >
                <td className={cn(BODY_CELL, "whitespace-nowrap font-mono text-sm text-foreground")}>
                  {formatStartedAt(run.started_at_utc)}
                </td>
                <td className={BODY_CELL}>
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium text-foreground">{run.bean_origin}</span>
                    {run.is_blend ? (
                      <span
                        data-testid="history-blend-badge"
                        className="inline-flex items-center rounded border border-border bg-muted/40 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground"
                      >
                        Blend
                      </span>
                    ) : null}
                  </div>
                  {run.bean_varietal ? (
                    <div className="text-xs text-muted-foreground">{run.bean_varietal}</div>
                  ) : null}
                  {run.country ? (
                    <div className="text-xs text-muted-foreground">{run.country}</div>
                  ) : null}
                </td>
                <td className={BODY_CELL}>
                  <OutcomeBadge outcome={run.outcome} />
                </td>
                <td className={BODY_CELL}>
                  <HistoryAdvisorCell runId={run.id} />
                </td>
                <td className={cn(BODY_CELL, "font-mono text-sm text-foreground")}>
                  {formatDevPercent(run.development_percent)}
                </td>
                <td className={BODY_CELL}>
                  <StarRating rating={run.rating} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="border-t border-border bg-background px-6 py-3 font-mono text-xs text-muted-foreground">
        {runs.length} roast{runs.length === 1 ? "" : "s"}
      </div>
    </div>
  );
}
