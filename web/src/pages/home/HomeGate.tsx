/**
 * State-aware `/` (#324): the device's root branches on SERVER state.
 *
 * - active run  → the live dashboard (the active-roast view stays at `/`).
 * - idle        → the home / landing hub.
 *
 * "Is there an active run" comes from the server's `/health` snapshot
 * (`active_run_id`), the same source the dashboard already uses — we never infer
 * roast phase locally (architecture invariant). We hold the decision until health
 * has resolved so the hub does not flash before the active run is known; on a
 * faulted run the dashboard's own sticky-fault handling keeps the operator on the
 * live view (#124), and navigating to `/roasts` and back never unmounts a running
 * roast destructively — the dashboard re-hydrates from `/telemetry` + SSE.
 */

import { AppFrame } from "@/components/shared";
import { useHealth } from "@/hooks/queries";
import { DashboardPage } from "@/pages/dashboard/DashboardPage";
import { HomePage } from "./HomePage";

export function HomeGate(): React.JSX.Element {
  const health = useHealth();
  const activeRunId = health.data?.active_run_id ?? null;

  // On a health fetch error (network blip, device reboot) fall through to the hub
  // rather than leaving the operator on a blank screen indefinitely. Active run is
  // unknown in this state, so we treat it as idle — same as no active run. The hub
  // still reaches `/start` and `/roasts`; the nav's Live-roast slot stays absent.
  if (health.isError) {
    return <HomePage />;
  }

  // Hold until health resolves — don't flash the hub before the active run is
  // known. A neutral frame (no run to connect to, so no "connecting" indicator).
  if (!health.isSuccess) {
    return (
      <AppFrame>
        <div data-testid="home-gate-loading" />
      </AppFrame>
    );
  }

  return activeRunId !== null ? <DashboardPage /> : <HomePage />;
}
