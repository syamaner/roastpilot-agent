/**
 * PLACEHOLDER — owned by E10-S5 (`detail` teammate).
 *
 * S2 ships only this route stub. S5 replaces this body with the roast detail
 * page (full persisted curve via the shared LiveCurve, event timeline,
 * decision-trace table, export downloads, self-rating) per plan §7, consuming
 * the shared foundation read-only.
 */

import { useParams } from "react-router-dom";

import { AppFrame } from "@/components/shared";

export function DetailPage(): React.JSX.Element {
  const { runId } = useParams<{ runId: string }>();
  return (
    <AppFrame>
      <p className="text-sm text-muted-foreground" data-testid="page-stub-detail">
        Roast detail {runId ? `(${runId})` : ""} — built in E10-S5.
      </p>
    </AppFrame>
  );
}
