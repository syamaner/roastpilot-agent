/**
 * Per-roast advisor summary cell for the history table (#170, #184).
 *
 * Server data only; nothing inferred. The consult count and how many were
 * clamped / rejected / failed are read straight off the `GET /api/roasts`
 * summary's advisor-stat fields (#184), which the backend aggregates from the
 * persisted `advisor_decisions` rows.
 *
 * Previously (#170) these counts were derived client-side per row from
 * `GET /api/roasts/{id}/timeline` via `useTimeline` — a documented M1 trade-off
 * that fired N parallel `/timeline` requests on a freshly-loaded history page
 * (one per visible row). #184 added the aggregate fields to the list contract, so
 * the cell now renders from the summary it is already handed: no per-row fetch,
 * no N+1. The displayed text is byte-for-byte the same as before.
 *
 * The cell shows a "no advice" hint for a roast with zero consults; a run that
 * predates the advisor (or simply never consulted) projects zeros and renders the
 * same hint (back-compat).
 */

import type { RoastSummary } from "@/lib/types";

export interface HistoryAdvisorCellProps {
  run: RoastSummary;
}

export function HistoryAdvisorCell({ run }: HistoryAdvisorCellProps): React.JSX.Element {
  const consults = run.advisor_consults;

  if (consults === 0) {
    return (
      <span data-testid="history-advisor-none" className="font-mono text-xs text-muted-foreground">
        no advice
      </span>
    );
  }

  return (
    <div data-testid="history-advisor" className="flex flex-col gap-0.5 font-mono text-xs">
      <span className="text-sm text-foreground">
        {consults} consult{consults === 1 ? "" : "s"}
      </span>
      <span className="text-muted-foreground">{parts(run).join(" · ")}</span>
    </div>
  );
}

function parts(run: RoastSummary): string[] {
  const out: string[] = [];
  if (run.advisor_clamped > 0) out.push(`${run.advisor_clamped} clamped`);
  if (run.advisor_rejected > 0) out.push(`${run.advisor_rejected} rejected`);
  if (run.advisor_failed > 0) out.push(`${run.advisor_failed} failed`);
  if (out.length === 0) out.push("all allowed");
  return out;
}
