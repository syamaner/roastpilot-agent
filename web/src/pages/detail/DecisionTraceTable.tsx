/**
 * Decision-trace table (E10-S5, ui-prompts Prompt C #3 / kickoff §2 detail row).
 *
 * The heart of the detail screen: one row per safety evaluation, showing the
 * advisor recommendation it judged, the verdict, what was executed, and the
 * rationale. It renders HISTORY, not advisory state, so its verdict column shows
 * ALL SIX verdicts (ALLOW / CLAMP / REJECT / RECOVERY / FAULT / EMERGENCY STOP)
 * via `verdictLabel` — not just the three advisory badges.
 *
 * Rows are selectable by stable view identity; the row tick separately anchors the
 * matching timestamp on the curve. The page owns toggle-off. The rationale truncates
 * with an expand control.
 */

import { useState } from "react";

import { cn } from "@/lib/cn";
import type { SafetyVerdict } from "@/lib/types";
import { verdictLabel, type VerdictTone } from "@/lib/verdict";
import { formatClock, formatConfidence, formatPercent } from "./format";
import type { TraceRow } from "./traceModel";

/** Tone per verdict for the trace column — all six, unlike the advisory badge. */
const VERDICT_TONE: Record<SafetyVerdict, VerdictTone> = {
  allow: "nominal",
  clamp: "caution",
  reject: "fault",
  recovery: "caution",
  fault: "fault",
  emergency_stop: "fault",
};

const TONE_CLASS: Record<VerdictTone, string> = {
  nominal: "border-roast-nominal/40 bg-roast-nominal/15 text-roast-nominal",
  caution: "border-roast-caution/40 bg-roast-caution/15 text-roast-caution",
  fault: "border-roast-fault/40 bg-roast-fault/15 text-roast-fault",
};

export interface DecisionTraceTableProps {
  rows: TraceRow[];
  /** The currently selected view-row identity, or `null`. */
  selectedRowId: string | null;
  /** Toggle selection for a view row, retaining its tick for curve placement. */
  onSelect: (rowId: string, tick: number) => void;
  /**
   * The table container's `data-testid`. Defaults to `decision-trace-table` — the
   * #253 column-header guard scopes to exactly that id, so the modal copy of this
   * table (#271) passes a DISTINCT id to keep the guarded selector unambiguous when
   * both the inline and the modal table are mounted.
   */
  tableTestId?: string;
  className?: string;
}

export function DecisionTraceTable({
  rows,
  selectedRowId,
  onSelect,
  tableTestId = "decision-trace-table",
  className,
}: DecisionTraceTableProps): React.JSX.Element {
  if (rows.length === 0) {
    return (
      <div
        data-testid="decision-trace-empty"
        className={cn("rounded-lg border border-border bg-card p-4 text-sm text-muted-foreground", className)}
      >
        No advisory decisions recorded for this roast.
      </div>
    );
  }

  return (
    <div
      data-testid={tableTestId}
      className={cn("overflow-x-auto rounded-lg border border-border bg-card", className)}
    >
      <table className="w-full border-collapse text-sm">
        <thead>
          <tr className="border-b border-border text-left text-xs uppercase tracking-wide text-muted-foreground">
            <th scope="col" className="px-3 py-2 font-medium">Time</th>
            <th scope="col" className="px-3 py-2 font-medium">Recommended</th>
            <th scope="col" className="px-3 py-2 font-medium">Verdict</th>
            <th scope="col" className="px-3 py-2 font-medium">Executed</th>
            <th scope="col" className="px-3 py-2 font-medium">Rationale</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <TraceTableRow
              key={row.rowId}
              row={row}
              selected={row.rowId === selectedRowId}
              onSelect={onSelect}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}

interface TraceTableRowProps {
  row: TraceRow;
  selected: boolean;
  onSelect: (rowId: string, tick: number) => void;
}

function TraceTableRow({ row, selected, onSelect }: TraceTableRowProps): React.JSX.Element {
  const [expanded, setExpanded] = useState(false);
  const tone = VERDICT_TONE[row.verdict];

  return (
    <tr
      data-testid="trace-row"
      data-tick={row.tick}
      data-verdict={row.verdict}
      data-selected={selected ? "true" : "false"}
      aria-selected={selected}
      onClick={() => onSelect(row.rowId, row.tick)}
      className={cn(
        "cursor-pointer border-b border-border/60 transition-colors last:border-b-0",
        selected ? "bg-secondary" : "hover:bg-secondary/50",
      )}
    >
      <td className="numeric whitespace-nowrap px-3 py-2 align-top text-muted-foreground">
        {formatClock(row.elapsedSeconds)}
      </td>
      <td className="numeric whitespace-nowrap px-3 py-2 align-top">
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
      </td>
      <td className="px-3 py-2 align-top">
        <span
          data-testid="trace-verdict"
          data-verdict={row.verdict}
          data-tone={tone}
          className={cn(
            "inline-flex items-center whitespace-nowrap rounded-md border px-2 py-0.5 text-xs font-semibold uppercase tracking-wide",
            TONE_CLASS[tone],
          )}
        >
          {verdictLabel(row.verdict)}
        </span>
      </td>
      <td className="numeric whitespace-nowrap px-3 py-2 align-top">
        <span className="text-roast-heat">{formatPercent(row.executedHeat)}</span>
        {" / "}
        <span className="text-roast-fan">{formatPercent(row.executedFan)}</span>
      </td>
      <td className="px-3 py-2 align-top text-muted-foreground">
        <Rationale
          rationale={row.rationale}
          reason={row.reason}
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

interface RationaleProps {
  rationale: string | null;
  reason: string;
  expanded: boolean;
  onToggle: (e: React.MouseEvent) => void;
}

/** The advisor rationale (falls back to the safety reason), truncated + expandable. */
function Rationale({ rationale, reason, expanded, onToggle }: RationaleProps): React.JSX.Element {
  // Prefer the advisor's own rationale; when absent (e.g. a policy/operator
  // verdict with no advisory), show the safety reason so the row still explains
  // itself. Both come from the REST contract.
  const text = rationale ?? reason;
  const truncatable = text.length > 80;
  return (
    <div className="flex items-start gap-2">
      <span
        data-testid="trace-rationale"
        className={cn("min-w-0", !expanded && "line-clamp-2")}
      >
        {text}
      </span>
      {truncatable && (
        <button
          type="button"
          data-testid="trace-rationale-toggle"
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
