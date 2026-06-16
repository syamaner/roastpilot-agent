/**
 * Dev/test-only long-roast detail snapshot harness (`/__detail-harness-long`).
 *
 * NOT a product page. Mounts the real `DetailView` with the LONG-roast fixture
 * (advisor-decisions list + decision-trace table both exceed the inline cap of 5),
 * so the Playwright suite has a deterministic `roast-detail-capped` baseline
 * proving the page caps the lists inline + offers the "View all" affordance — a
 * fixed-height layout that no longer grows unbounded with roast length (#271).
 * Mirrors `DetailHarnessPage`.
 */

import { AppFrame } from "@/components/shared";
import { DetailView } from "./DetailView";
import {
  FIXTURE_DETAIL_LONG,
  FIXTURE_TELEMETRY_LONG,
  FIXTURE_TIMELINE_LONG,
} from "./fixture";

export function DetailHarnessLongPage(): React.JSX.Element {
  return (
    <AppFrame>
      <DetailView
        detail={FIXTURE_DETAIL_LONG}
        telemetry={FIXTURE_TELEMETRY_LONG}
        timeline={FIXTURE_TIMELINE_LONG}
      />
    </AppFrame>
  );
}
