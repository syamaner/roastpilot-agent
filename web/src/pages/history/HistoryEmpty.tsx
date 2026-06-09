/**
 * History empty states (E10-S4).
 *
 * Two distinct states, deliberately separate:
 *   - `HistoryEmpty`     → no roasts exist yet (first-run); a coffee glyph +
 *     onboarding copy.
 *   - `HistoryNoMatches` → roasts exist but none match the active filters; a
 *     compact prompt to clear them. Conflating the two hides whether the store
 *     is empty or the filter is too narrow.
 *
 * Inline SVG, hand-rolled Tailwind (no icon dep) — matches the S2 shared
 * components. The first-run state offers a NAVIGATION link to the dashboard
 * (where a roast is actually started) rather than a fabricated "start roast"
 * action this page cannot fulfill — an honest route nav, not a fake control.
 */

import { Link } from "react-router-dom";

export function HistoryEmpty(): React.JSX.Element {
  return (
    <div
      className="rounded-lg border border-border bg-card p-16 text-center"
      data-testid="history-empty"
    >
      <div className="mb-6 flex justify-center text-muted-foreground" aria-hidden="true">
        <svg width="56" height="56" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
          <path d="M18 8h1a3 3 0 0 1 0 6h-1" />
          <path d="M4 8h14v6a4 4 0 0 1-4 4H8a4 4 0 0 1-4-4V8z" />
          <path d="M8 3v2M12 3v2M16 3v2" strokeLinecap="round" />
        </svg>
      </div>
      <h2 className="mb-2 font-mono text-xl text-foreground">No roasts yet</h2>
      <p className="mx-auto mb-6 max-w-md text-sm text-muted-foreground">
        Your roast history will appear here once you complete your first roast.
      </p>
      <Link
        to="/"
        data-testid="history-empty-dashboard-link"
        className="inline-flex items-center gap-1 rounded-md border border-border bg-background px-4 py-2 font-mono text-xs uppercase tracking-wide text-foreground transition-colors hover:bg-muted/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        Start a roast on the dashboard
        <span aria-hidden="true">→</span>
      </Link>
    </div>
  );
}

export interface HistoryNoMatchesProps {
  onClear: () => void;
}

export function HistoryNoMatches({ onClear }: HistoryNoMatchesProps): React.JSX.Element {
  return (
    <div
      className="rounded-lg border border-border bg-card p-12 text-center"
      data-testid="history-no-matches"
    >
      <p className="mb-4 text-sm text-muted-foreground">No roasts match your filters.</p>
      <button
        type="button"
        onClick={onClear}
        className="rounded-md border border-border bg-background px-4 py-2 font-mono text-xs uppercase tracking-wide text-foreground transition-colors hover:bg-muted/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        Clear filters
      </button>
    </div>
  );
}
