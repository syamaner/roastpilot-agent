/**
 * Advisor decision timeline (#170).
 *
 * One row per persisted advisor consult — INCLUDING preheat consults and failed
 * ones. For an `ok` consult it shows the recommended heat/fan + rationale and the
 * linked safety verdict (the shared `VerdictBadge`, with a `verdictLabel` fallback
 * for the non-badge verdicts RECOVERY/FAULT/EMERGENCY_STOP). For a failed consult
 * (`timeout` / `malformed` / `provider_error`) it shows the failure explicitly, so
 * a roast where the advisor never returned a usable decision renders its failures
 * rather than a blank panel.
 *
 * Rows are selectable: clicking reports the tick so the page highlights the curve
 * (re-clicking the selected row toggles it off — the page owns the toggle). It
 * renders HISTORY read straight from the REST contract; nothing is inferred.
 */

import { useState } from "react";

import { VerdictBadge } from "@/components/shared";
import { cn } from "@/lib/cn";
import { verdictLabel } from "@/lib/verdict";
import {
  advisorStatusLabel,
  isFailureStatus,
  type AdvisorRow,
} from "./advisorModel";
import { formatClock, formatConfidence, formatPercent } from "./format";

const STATUS_TONE_CLASS: Record<"ok" | "fail", string> = {
  ok: "border-roast-nominal/40 bg-roast-nominal/15 text-roast-nominal",
  fail: "border-roast-fault/40 bg-roast-fault/15 text-roast-fault",
};

export interface AdvisorTimelineProps {
  rows: AdvisorRow[];
  /** The currently highlighted tick (selected row), or `null`. */
  selectedTick: number | null;
  /** Toggle selection for a row's tick (re-selecting the same tick clears it). */
  onSelect: (tick: number) => void;
  className?: string;
}

export function AdvisorTimeline({
  rows,
  selectedTick,
  onSelect,
  className,
}: AdvisorTimelineProps): React.JSX.Element {
  if (rows.length === 0) {
    return (
      <div
        data-testid="advisor-timeline-empty"
        className={cn(
          "rounded-lg border border-border bg-card p-4 text-sm text-muted-foreground",
          className,
        )}
      >
        The advisor was not consulted during this roast.
      </div>
    );
  }

  return (
    <div
      data-testid="advisor-timeline"
      className={cn("overflow-x-auto rounded-lg border border-border bg-card", className)}
    >
      <table className="w-full border-collapse text-sm">
        <thead>
          <tr className="border-b border-border text-left text-xs uppercase tracking-wide text-muted-foreground">
            <th scope="col" className="px-3 py-2 font-medium">Time</th>
            <th scope="col" className="px-3 py-2 font-medium">Advisor</th>
            <th scope="col" className="px-3 py-2 font-medium">Status</th>
            <th scope="col" className="px-3 py-2 font-medium">Recommended</th>
            <th scope="col" className="px-3 py-2 font-medium">Verdict</th>
            <th scope="col" className="px-3 py-2 font-medium">Rationale</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <AdvisorTimelineRow
              key={`${row.tick}-${row.recordedAtUtc}`}
              row={row}
              selected={row.tick === selectedTick}
              onSelect={onSelect}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}

interface AdvisorTimelineRowProps {
  row: AdvisorRow;
  selected: boolean;
  onSelect: (tick: number) => void;
}

function AdvisorTimelineRow({
  row,
  selected,
  onSelect,
}: AdvisorTimelineRowProps): React.JSX.Element {
  const [expanded, setExpanded] = useState(false);
  const failed = isFailureStatus(row.status);

  return (
    <tr
      data-testid="advisor-row"
      data-tick={row.tick}
      data-status={row.status}
      data-selected={selected ? "true" : "false"}
      aria-selected={selected}
      onClick={() => onSelect(row.tick)}
      className={cn(
        "cursor-pointer border-b border-border/60 transition-colors last:border-b-0",
        selected ? "bg-secondary" : "hover:bg-secondary/50",
      )}
    >
      <td className="numeric whitespace-nowrap px-3 py-2 align-top text-muted-foreground">
        {formatClock(row.elapsedSeconds)}
      </td>
      <td className="px-3 py-2 align-top">
        <div className="whitespace-nowrap text-xs">{row.model}</div>
        <div className="whitespace-nowrap text-xs text-muted-foreground">
          {row.provider}
          {row.latencyMs !== null && (
            <span className="ml-2 numeric">{row.latencyMs} ms</span>
          )}
        </div>
      </td>
      <td className="px-3 py-2 align-top">
        <span
          data-testid="advisor-status"
          data-status={row.status}
          className={cn(
            "inline-flex items-center whitespace-nowrap rounded-md border px-2 py-0.5 text-xs font-semibold uppercase tracking-wide",
            STATUS_TONE_CLASS[failed ? "fail" : "ok"],
          )}
        >
          {advisorStatusLabel(row.status)}
        </span>
      </td>
      <td className="numeric whitespace-nowrap px-3 py-2 align-top">
        {failed ? (
          <span className="text-muted-foreground">—</span>
        ) : (
          <>
            <span className="text-roast-heat">{formatPercent(row.recommendedHeat)}</span>
            {" / "}
            <span className="text-roast-fan">{formatPercent(row.recommendedFan)}</span>
            {row.confidence !== null && (
              <span className="ml-2 text-xs text-muted-foreground">
                conf {formatConfidence(row.confidence)}
              </span>
            )}
            {row.shouldDrop === true && (
              <span className="ml-2 text-xs font-medium text-roast-caution">drop</span>
            )}
          </>
        )}
      </td>
      <td className="px-3 py-2 align-top">
        <VerdictColumn verdict={row.verdict} />
      </td>
      <td className="px-3 py-2 align-top text-muted-foreground">
        <Rationale
          row={row}
          expanded={expanded}
          onToggle={(e) => {
            // Don't let the expand toggle also (de)select the row.
            e.stopPropagation();
            setExpanded((v) => !v);
          }}
        />
      </td>
    </tr>
  );
}

/**
 * The linked safety verdict. Reuses the shared `VerdictBadge` for the three
 * advisory verdicts; for the non-badge verdicts (RECOVERY/FAULT/EMERGENCY_STOP —
 * `VerdictBadge` renders nothing for those) it falls back to a plain label so the
 * row still shows what safety did. `null` (a failed consult produced no verdict)
 * renders an em dash.
 */
function VerdictColumn({
  verdict,
}: {
  verdict: AdvisorRow["verdict"];
}): React.JSX.Element {
  if (verdict === null) {
    return <span className="text-muted-foreground">—</span>;
  }
  const badge = <VerdictBadge verdict={verdict} />;
  // `VerdictBadge` returns null for recovery/fault/emergency_stop; show a label.
  if (verdict === "recovery" || verdict === "fault" || verdict === "emergency_stop") {
    return (
      <span
        data-testid="advisor-verdict-label"
        className="inline-flex items-center whitespace-nowrap rounded-md border border-roast-fault/40 bg-roast-fault/15 px-2 py-0.5 text-xs font-semibold uppercase tracking-wide text-roast-fault"
      >
        {verdictLabel(verdict)}
      </span>
    );
  }
  return badge;
}

interface RationaleProps {
  row: AdvisorRow;
  expanded: boolean;
  onToggle: (e: React.MouseEvent) => void;
}

/**
 * The advisor rationale on an `ok` consult; on a failure the safety reason if one
 * exists, else a status-derived explanation so the row is never blank.
 */
function Rationale({ row, expanded, onToggle }: RationaleProps): React.JSX.Element {
  const text =
    row.rationale ??
    row.verdictReason ??
    (isFailureStatus(row.status)
      ? `Advisor ${advisorStatusLabel(row.status).toLowerCase()} — no recommendation; controller held the last commanded levels.`
      : "—");
  const truncatable = text.length > 80;
  return (
    <div className="flex items-start gap-2">
      <span
        data-testid="advisor-rationale"
        className={cn("min-w-0", !expanded && "line-clamp-2")}
      >
        {text}
      </span>
      {truncatable && (
        <button
          type="button"
          data-testid="advisor-rationale-toggle"
          aria-expanded={expanded}
          onClick={onToggle}
          className="shrink-0 text-xs text-muted-foreground underline-offset-2 hover:underline"
        >
          {expanded ? "less" : "more"}
        </button>
      )}
    </div>
  );
}
