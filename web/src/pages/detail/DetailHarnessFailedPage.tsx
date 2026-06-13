/**
 * Dev/test-only advisor-failure detail snapshot harness (`/__detail-harness-failed`).
 *
 * NOT a product page. Mounts the real `DetailView` with the advisor-FAILURE
 * fixture (every consult a `provider_error`, no safety evaluations) so the
 * Playwright suite has a deterministic `roast-detail-advisor-failed` baseline
 * proving the advisor timeline renders the failures rather than a blank panel
 * (#170). Mirrors `DetailHarnessPage`.
 */

import { AppFrame } from "@/components/shared";
import { DetailView } from "./DetailView";
import {
  FIXTURE_DETAIL_FAILED,
  FIXTURE_TELEMETRY_FAILED,
  FIXTURE_TIMELINE_FAILED,
} from "./fixture";

export function DetailHarnessFailedPage(): React.JSX.Element {
  return (
    <AppFrame>
      <DetailView
        detail={FIXTURE_DETAIL_FAILED}
        telemetry={FIXTURE_TELEMETRY_FAILED}
        timeline={FIXTURE_TIMELINE_FAILED}
      />
    </AppFrame>
  );
}
