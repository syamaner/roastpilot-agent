/**
 * Dev/test-only dry-end detail snapshot harness (`/__detail-harness-dry-end`).
 *
 * NOT a product page. Mounts the real `DetailView` with `FIXTURE_TIMELINE_DRY_END`
 * — the base detail fixture PLUS a persisted `drying_end` timeline event (#351) — so
 * the Playwright suite can assert, via the `window.__chart` DATA hook (D24), that the
 * dry-end marker reaches the detail chart on the reload/persisted path. A SEPARATE
 * route from `/__detail-harness` so the base `roast-detail` snapshot stays untouched.
 *
 * Mirrors `DetailHarnessPage` (the authorized shared-route convention).
 */

import { roastKeys } from "@/hooks/queries";
import { queryClient } from "@/lib/queryClient";

import { AppFrame } from "@/components/shared";
import { DetailView } from "./DetailView";
import {
  FIXTURE_DETAIL,
  FIXTURE_TELEMETRY,
  FIXTURE_TIMELINE_DRY_END,
  fixtureTastings,
} from "./fixture";

// #522, Codex round 3: see DetailHarnessPage's identical seed comment.
queryClient.setQueryData(roastKeys.tastings(FIXTURE_DETAIL.id), fixtureTastings(FIXTURE_DETAIL.id));

export function DetailHarnessDryEndPage(): React.JSX.Element {
  return (
    <AppFrame>
      <DetailView
        detail={FIXTURE_DETAIL}
        telemetry={FIXTURE_TELEMETRY}
        timeline={FIXTURE_TIMELINE_DRY_END}
      />
    </AppFrame>
  );
}
