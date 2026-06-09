/**
 * Dev/test-only detail snapshot harness (`/__detail-harness`).
 *
 * NOT a product page. It mounts the real `DetailView` with the fixed
 * REST-shaped `fixture.ts` data so the Playwright snapshot suite (D24) has a
 * deterministic target for the `roast-detail` and `roast-detail-selected` states
 * — without depending on the stepped-SSE replay backend (the full replay-backed
 * matrix is S6's scope). The fixture carries one CLAMP decision so the
 * `roast-detail-selected` shot (CLAMP row + curve marker) is reproducible.
 *
 * Mirrors S2's `/__chart-harness` pattern — the single authorized shared route.
 */

import { AppFrame } from "@/components/shared";
import { DetailView } from "./DetailView";
import { FIXTURE_DETAIL, FIXTURE_TELEMETRY, FIXTURE_TIMELINE } from "./fixture";

export function DetailHarnessPage(): React.JSX.Element {
  return (
    <AppFrame>
      <DetailView
        detail={FIXTURE_DETAIL}
        telemetry={FIXTURE_TELEMETRY}
        timeline={FIXTURE_TIMELINE}
      />
    </AppFrame>
  );
}
