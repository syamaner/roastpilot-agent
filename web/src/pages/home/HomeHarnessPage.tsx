/**
 * Dev/test-only Home snapshot harness (`/__home-harness`).
 *
 * NOT a product page. It mounts the persistent `NavBar` + the `HomePage` hub over a
 * pre-seeded `/health` snapshot (no active run), so the Playwright snapshot suite
 * (D24/D26) has a deterministic target for the `home` state without an idle live
 * backend. Mirrors the `/__start-roast-harness` route-harness convention (#303,
 * #324).
 *
 * The active-run signal is seeded into the shared QueryClient so `useHealth`
 * resolves synchronously to "no active run" — the nav's Live-roast link is absent,
 * matching the idle hub. The data-assert layer (the two entry-point links + the nav
 * links) is asserted in the spec alongside the pixels.
 */

import { roastKeys } from "@/hooks/queries";
import { queryClient } from "@/lib/queryClient";
import type { HealthResponse } from "@/lib/types";

import { HomePage } from "./HomePage";
import { NavBar } from "./NavBar";

// Seed a deterministic idle health snapshot (no active run) so the nav + gate are
// stable for the snapshot. Module-level so it is set before the first render.
const IDLE_HEALTH: HealthResponse = {
  status: "ok",
  version: "harness",
  instance_id: "harness-instance",
  mcp_child: "running",
  active_run_id: null,
};
queryClient.setQueryData(roastKeys.health, IDLE_HEALTH);

export function HomeHarnessPage(): React.JSX.Element {
  return (
    <div className="min-h-screen bg-background text-foreground" data-testid="home-harness">
      <NavBar />
      <HomePage />
    </div>
  );
}
