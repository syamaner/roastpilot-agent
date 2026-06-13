/**
 * Compact advisor-summary chips (#170) — consult count, ok/failed, clamped,
 * rejected. Used in the detail page's advisor-timeline header. Pure presentation
 * of an `AdvisorSummary` already computed from the REST timeline.
 */

import { cn } from "@/lib/cn";
import type { AdvisorSummary } from "./advisorModel";

export interface AdvisorSummaryChipsProps {
  summary: AdvisorSummary;
  className?: string;
}

export function AdvisorSummaryChips({
  summary,
  className,
}: AdvisorSummaryChipsProps): React.JSX.Element {
  return (
    <div
      data-testid="advisor-summary"
      className={cn("flex flex-wrap items-center gap-2 text-xs", className)}
    >
      <Chip testId="advisor-summary-consults" tone="muted">
        {summary.consults} consult{summary.consults === 1 ? "" : "s"}
      </Chip>
      {summary.failed > 0 && (
        <Chip testId="advisor-summary-failed" tone="fault">
          {summary.failed} failed
        </Chip>
      )}
      {summary.clamped > 0 && (
        <Chip testId="advisor-summary-clamped" tone="caution">
          {summary.clamped} clamped
        </Chip>
      )}
      {summary.rejected > 0 && (
        <Chip testId="advisor-summary-rejected" tone="fault">
          {summary.rejected} rejected
        </Chip>
      )}
    </div>
  );
}

const TONE_CLASS: Record<"muted" | "caution" | "fault", string> = {
  muted: "border-border bg-secondary text-muted-foreground",
  caution: "border-roast-caution/40 bg-roast-caution/15 text-roast-caution",
  fault: "border-roast-fault/40 bg-roast-fault/15 text-roast-fault",
};

function Chip({
  children,
  testId,
  tone,
}: {
  children: React.ReactNode;
  testId: string;
  tone: "muted" | "caution" | "fault";
}): React.JSX.Element {
  return (
    <span
      data-testid={testId}
      className={cn(
        "inline-flex items-center whitespace-nowrap rounded-md border px-2 py-0.5 font-medium",
        TONE_CLASS[tone],
      )}
    >
      {children}
    </span>
  );
}
