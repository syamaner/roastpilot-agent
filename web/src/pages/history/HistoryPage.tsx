/**
 * PLACEHOLDER — owned by E10-S4 (`history` teammate).
 *
 * S2 ships only this route stub. S4 replaces this body with the history table
 * (date, bean, profile, outcome, FC time, dev %, rating) + filter + empty state
 * per plan §7, consuming the shared foundation read-only.
 */

import { AppFrame } from "@/components/shared";

export function HistoryPage(): React.JSX.Element {
  return (
    <AppFrame>
      <p className="text-sm text-muted-foreground" data-testid="page-stub-history">
        History — built in E10-S4.
      </p>
    </AppFrame>
  );
}
