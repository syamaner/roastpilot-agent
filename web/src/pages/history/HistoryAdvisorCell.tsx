/**
 * Per-roast advisor summary cell for the history table (#170).
 *
 * The `GET /api/roasts` summary contract carries no advisor stats, and the backend
 * is read-only for this story, so the summary is derived client-side from the same
 * `GET /api/roasts/{id}/timeline` the detail page reads — via the shared
 * `useTimeline` hook (TanStack Query caches per-run, shared with the detail page).
 * Server data only; nothing inferred. The count of consults + how many were
 * clamped/rejected/failed comes straight off the persisted timeline rows.
 *
 * Lazy-by-row: each visible history row issues its OWN `useTimeline(runId)` query,
 * so a freshly-loaded history page fires N parallel `/timeline` requests (one per
 * visible row). This is a DELIBERATE M1 trade-off, not an oversight: there is no
 * advisor-summary field on the `GET /api/roasts` list contract and the backend is
 * read-only for this story, so per-row derivation is the only server-data-only path
 * (the alternative — a backend summary column — is a future contract change). For
 * M1's handful of local roasts the cost is negligible, and TanStack Query caches
 * per-run so a revisit or cross-navigation to the detail page is free.
 *
 * The cell degrades gracefully — em dash while loading or on error (the list still
 * renders), a "no advice" hint for a roast with zero consults.
 */

import { useTimeline } from "@/hooks/queries";

import { advisorSummary } from "@/pages/detail/advisorModel";

export interface HistoryAdvisorCellProps {
  runId: string;
}

export function HistoryAdvisorCell({ runId }: HistoryAdvisorCellProps): React.JSX.Element {
  const { data, isPending, isError } = useTimeline(runId);

  if (isPending || isError || data === undefined) {
    return (
      <span data-testid="history-advisor-pending" className="font-mono text-sm text-muted-foreground">
        —
      </span>
    );
  }

  const summary = advisorSummary(data);

  if (summary.consults === 0) {
    return (
      <span data-testid="history-advisor-none" className="font-mono text-xs text-muted-foreground">
        no advice
      </span>
    );
  }

  return (
    <div data-testid="history-advisor" className="flex flex-col gap-0.5 font-mono text-xs">
      <span className="text-sm text-foreground">
        {summary.consults} consult{summary.consults === 1 ? "" : "s"}
      </span>
      <span className="text-muted-foreground">
        {parts(summary).join(" · ")}
      </span>
    </div>
  );
}

function parts(summary: ReturnType<typeof advisorSummary>): string[] {
  const out: string[] = [];
  if (summary.clamped > 0) out.push(`${summary.clamped} clamped`);
  if (summary.rejected > 0) out.push(`${summary.rejected} rejected`);
  if (summary.failed > 0) out.push(`${summary.failed} failed`);
  if (out.length === 0) out.push("all allowed");
  return out;
}
