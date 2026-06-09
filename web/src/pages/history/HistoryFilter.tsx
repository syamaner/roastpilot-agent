/**
 * History filter bar (E10-S4) — search + outcome + minimum-rating.
 *
 * Controlled: the page owns the `HistoryFilters` state and re-filters the
 * already-fetched REST list client-side (the list is small; no server round
 * trip). Unlike the prototype's hardcoded origin enum, the search box matches
 * the live bean strings, so no origin list can drift from the data.
 */

import { cn } from "@/lib/cn";
import type { RoastOutcome } from "@/lib/types";

import { ANY, type HistoryFilters } from "./format";

export interface HistoryFilterProps {
  filters: HistoryFilters;
  onChange: (next: HistoryFilters) => void;
}

const OUTCOMES: { value: RoastOutcome; label: string }[] = [
  { value: "completed", label: "Completed" },
  { value: "aborted", label: "Aborted" },
  { value: "faulted", label: "Faulted" },
];

const RATINGS = [5, 4, 3, 2, 1] as const;

const FIELD =
  "rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground " +
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";

export function HistoryFilter({ filters, onChange }: HistoryFilterProps): React.JSX.Element {
  return (
    <div
      className="mb-4 flex flex-wrap items-center gap-3 rounded-lg border border-border bg-card p-4"
      data-testid="history-filter"
    >
      <span className="font-mono text-xs uppercase tracking-wide text-muted-foreground">Filters</span>

      <label className="flex-1 min-w-[12rem]">
        <span className="sr-only">Search beans</span>
        <input
          type="search"
          inputMode="search"
          placeholder="Search beans…"
          value={filters.search}
          aria-label="Search beans"
          onChange={(e) => onChange({ ...filters, search: e.target.value })}
          className={cn(FIELD, "w-full")}
        />
      </label>

      <label>
        <span className="sr-only">Filter by outcome</span>
        <select
          value={filters.outcome}
          aria-label="Filter by outcome"
          onChange={(e) =>
            onChange({ ...filters, outcome: e.target.value as HistoryFilters["outcome"] })
          }
          className={FIELD}
        >
          <option value={ANY}>All outcomes</option>
          {OUTCOMES.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      </label>

      <label>
        <span className="sr-only">Filter by minimum rating</span>
        <select
          value={filters.minRating}
          aria-label="Filter by minimum rating"
          onChange={(e) => onChange({ ...filters, minRating: e.target.value })}
          className={FIELD}
        >
          <option value={ANY}>All ratings</option>
          {RATINGS.map((r) => (
            <option key={r} value={String(r)}>
              {"★".repeat(r)}
              {r < 5 ? " and up" : ""}
            </option>
          ))}
        </select>
      </label>
    </div>
  );
}
