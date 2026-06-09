/**
 * PLACEHOLDER — owned by E10-S3 (`dashboard` teammate).
 *
 * The foundation (S2) ships only this route stub so the shell routes resolve and
 * the snapshot harness has a target. S3 replaces this body with the live
 * dashboard (header, LiveCurve, control row, advisory panel, operator action
 * bar, recovery modal, fault banner) per plan §7 — consuming the shared
 * foundation read-only. Do not build dashboard logic here in S2.
 */

import { AppFrame } from "@/components/shared";

export function DashboardPage(): React.JSX.Element {
  return (
    <AppFrame>
      <p className="text-sm text-muted-foreground" data-testid="page-stub-dashboard">
        Dashboard — built in E10-S3.
      </p>
    </AppFrame>
  );
}
