/**
 * LLM advisory panel (ui-prompts Prompt A §4, kickoff §3).
 *
 * Renders the latest advisor recommendation — recommended heat/fan, should-drop,
 * confidence, one-line rationale — with the safety system's VERDICT BADGE
 * (ALLOW / CLAMP / REJECT, D15). The shared `VerdictBadge` follows the enum
 * (`ALLOW`, never the prototype's `ACCEPT`) and renders nothing for the three
 * non-advisory verdicts (RECOVERY / FAULT / EMERGENCY_STOP — those are the modal /
 * banner / phase change). Below the card, a compact decision-history list shows
 * the last few advisories with their badges.
 *
 * All control values are %, temperatures Celsius. The synthesized replay CLAMP
 * key frame is tagged so the panel can mark it as not-live-evaluated.
 */

import { VerdictBadge } from "@/components/shared";
import { cn } from "@/lib/cn";
import { formatConfidence, formatPercent } from "./format";
import type { AdvisoryRecord } from "./useDashboardEvents";

export interface AdvisoryPanelProps {
  latest: AdvisoryRecord | null;
  history: AdvisoryRecord[];
  /** Whether the advisor is currently paused (shown as a status note). */
  paused: boolean;
  className?: string;
}

export function AdvisoryPanel({
  latest,
  history,
  paused,
  className,
}: AdvisoryPanelProps): React.JSX.Element {
  return (
    <section
      data-testid="advisory-panel"
      className={cn("flex flex-col gap-4 rounded-lg border border-border bg-card p-5", className)}
    >
      <header className="flex items-center justify-between">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
          LLM Advisory
        </h2>
        {paused && (
          <span
            data-testid="advisory-paused"
            className="text-xs font-medium uppercase tracking-wide text-roast-caution"
          >
            Paused
          </span>
        )}
      </header>

      {latest === null ? (
        <p data-testid="advisory-empty" className="text-sm text-muted-foreground">
          Awaiting first recommendation…
        </p>
      ) : (
        <LatestRecommendation record={latest} />
      )}

      {history.length > 0 && (
        <div className="flex flex-col gap-2">
          <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Decision History
          </h3>
          <ul data-testid="advisory-history" className="flex flex-col gap-1">
            {history.map((record, i) => (
              <li
                key={i}
                data-testid="advisory-history-row"
                className="flex items-center justify-between gap-3 rounded border border-border/60 px-3 py-1.5 text-sm"
              >
                <span className="truncate text-muted-foreground">
                  {summarize(record)}
                </span>
                {record.evaluation && <VerdictBadge verdict={record.evaluation.verdict} />}
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

function LatestRecommendation({ record }: { record: AdvisoryRecord }): React.JSX.Element {
  const { decision, evaluation } = record;
  return (
    <div className="flex flex-col gap-3" data-testid="advisory-latest">
      <div className="flex items-start justify-between gap-3">
        <div className="flex flex-col gap-1">
          <span className="text-xs uppercase tracking-wide text-muted-foreground">
            Latest Recommendation
          </span>
          <p className="text-sm leading-snug">
            {decision?.rationale ?? evaluation?.reason ?? "—"}
          </p>
        </div>
        {evaluation && <VerdictBadge verdict={evaluation.verdict} />}
      </div>

      {decision && (
        <dl className="grid grid-cols-2 gap-x-6 gap-y-1 text-sm sm:grid-cols-4">
          <Stat label="Heat" value={formatPercent(decision.target_heat)} tone="heat" />
          <Stat label="Fan" value={formatPercent(decision.target_fan)} tone="fan" />
          <Stat label="Drop" value={decision.should_drop ? "yes" : "no"} />
          <Stat label="Confidence" value={formatConfidence(decision.confidence)} />
        </dl>
      )}

      {record.synthesized && (
        <p data-testid="advisory-synthesized" className="text-xs italic text-muted-foreground">
          Synthesized demo decision (safety verdict computed by the real policy).
        </p>
      )}
    </div>
  );
}

function Stat({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: "heat" | "fan";
}): React.JSX.Element {
  return (
    <div className="flex flex-col">
      <dt className="text-xs uppercase tracking-wide text-muted-foreground">{label}</dt>
      <dd
        className={cn(
          "numeric font-semibold",
          tone === "heat" && "text-roast-heat",
          tone === "fan" && "text-roast-fan",
        )}
      >
        {value}
      </dd>
    </div>
  );
}

/** A one-line summary of an advisory for the history list. */
function summarize(record: AdvisoryRecord): string {
  if (record.decision) {
    return `Heat ${Math.round(record.decision.target_heat)} % · Fan ${Math.round(
      record.decision.target_fan,
    )} %`;
  }
  return record.evaluation?.reason ?? "advisory";
}
