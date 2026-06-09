/**
 * Roast history page (E10-S4) — the `/roasts` route.
 *
 * Renders the persisted roast list from `GET /api/roasts` via the shared
 * `useHistory()` hook (read-only): a filter bar, the history table, and the two
 * empty states. Pure REST — no SSE, no MCP; the SPA renders only what the
 * contract carries (invariant). Columns: Date, Bean, Outcome, Dev %, Rating.
 * Profile name, FC time, drop temp, and the sparkline are intentionally absent —
 * not in the M1 contract (D7: no named profiles; FC time deferred to #111).
 */

import { useMemo, useState } from "react";

import { AppFrame } from "@/components/shared";
import { useHistory } from "@/hooks/queries";
import { ApiError } from "@/lib/api";

import { EMPTY_FILTERS, filterRuns, type HistoryFilters } from "./format";
import { HistoryEmpty, HistoryNoMatches } from "./HistoryEmpty";
import { HistoryFilter } from "./HistoryFilter";
import { HistoryTable } from "./HistoryTable";

export function HistoryPage(): React.JSX.Element {
  const { data, isPending, isError, error } = useHistory();
  const [filters, setFilters] = useState<HistoryFilters>(EMPTY_FILTERS);

  const runs = useMemo(() => data?.runs ?? [], [data]);
  const visible = useMemo(() => filterRuns(runs, filters), [runs, filters]);

  return (
    <AppFrame>
      <div className="mx-auto max-w-[1600px]">
        <header className="mb-6">
          <h1 className="font-mono text-3xl text-foreground">Roast History</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Review past roasts, compare results, and refine your technique.
          </p>
        </header>

        {isPending ? (
          <p className="text-sm text-muted-foreground" data-testid="history-loading">
            Loading roasts…
          </p>
        ) : isError ? (
          <p className="text-sm text-roast-fault" data-testid="history-error">
            Could not load history: {error instanceof ApiError ? error.detail : "request failed"}
          </p>
        ) : runs.length === 0 ? (
          <HistoryEmpty />
        ) : (
          <>
            <HistoryFilter filters={filters} onChange={setFilters} />
            {visible.length === 0 ? (
              <HistoryNoMatches onClear={() => setFilters(EMPTY_FILTERS)} />
            ) : (
              <HistoryTable runs={visible} />
            )}
          </>
        )}
      </div>
    </AppFrame>
  );
}
