/**
 * Dev/test-only Start-Roast snapshot harness (`/__start-roast-harness`).
 *
 * NOT a product page. It mounts the real `StartRoastForm` with the bean-profile
 * library (#303) over FIXED fixture data + stubbed mutations, so the Playwright
 * snapshot suite (D24/D26) has a deterministic target for the `start-roast` and
 * `start-roast-add-modal` states — without an idle live backend (the replay agents
 * always carry an active run, so the idle Start page is not otherwise reachable).
 *
 * Mirrors the `/__detail-harness` pattern (the authorized shared-route convention).
 * The stubbed create/update echo the input back as a saved profile so the add/edit
 * flow advances; nothing here calls the network — the data-assert layer (the
 * dropdown options + the filled fields) is asserted in the spec.
 */

import { AppFrame } from "@/components/shared";
import type { BeanProfile, BeanProfileInput, RoastProfile } from "@/lib/types";
import { FIXTURE_BEAN_PROFILES } from "@/pages/dashboard/beanProfileFixture";
import { StartRoastForm } from "@/pages/dashboard/StartRoastForm";

/** Echo an input back as a saved profile (the server response shape). */
function echoSaved(id: string, input: BeanProfileInput): BeanProfile {
  return { ...input, id, created_at: "2026-06-21T00:00:00+00:00", updated_at: "2026-06-21T00:00:00+00:00" };
}

export function StartRoastHarnessPage(): React.JSX.Element {
  const onStart = (_profile: RoastProfile): Promise<void> => Promise.resolve();
  const onCreateProfile = (input: BeanProfileInput): Promise<BeanProfile> =>
    Promise.resolve(echoSaved("harness-created", input));
  const onUpdateProfile = (id: string, input: BeanProfileInput): Promise<BeanProfile> =>
    Promise.resolve(echoSaved(id, input));

  return (
    <AppFrame>
      <div className="flex flex-col gap-4" data-testid="dashboard-idle">
        <StartRoastForm
          onStart={onStart}
          profiles={FIXTURE_BEAN_PROFILES}
          onCreateProfile={onCreateProfile}
          onUpdateProfile={onUpdateProfile}
        />
      </div>
    </AppFrame>
  );
}
