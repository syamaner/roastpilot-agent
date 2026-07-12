/**
 * Dev/test-only advisor-failure detail snapshot harness (`/__detail-harness-failed`).
 *
 * NOT a product page. Mounts the real `DetailView` with the advisor-FAILURE
 * fixture (every consult a `provider_error`, no safety evaluations) so the
 * Playwright suite has a deterministic `roast-detail-advisor-failed` baseline
 * proving the advisor timeline renders the failures rather than a blank panel
 * (#170). Mirrors `DetailHarnessPage`.
 */

import { roastKeys } from "@/hooks/queries";
import { queryClient } from "@/lib/queryClient";

import { AppFrame } from "@/components/shared";
import { DetailView } from "./DetailView";
import {
  FIXTURE_DETAIL_FAILED,
  FIXTURE_TELEMETRY_FAILED,
  FIXTURE_TIMELINE_FAILED,
  fixtureTastings,
} from "./fixture";

// #522, Codex round 3: see DetailHarnessPage's identical seed comment.
queryClient.setQueryData(
  roastKeys.tastings(FIXTURE_DETAIL_FAILED.id),
  fixtureTastings(FIXTURE_DETAIL_FAILED.id),
);

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
