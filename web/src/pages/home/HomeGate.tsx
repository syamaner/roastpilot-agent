/**
 * Pure launcher at `/` (#423, D81).
 *
 * Always renders the `HomePage` hub — unconditionally. The loading hold and
 * active→dashboard branch that used to live here (the #403 `/` state-awareness)
 * are gone: `/live` is now the SINGLE live-roast home (three server-state-driven
 * states live there), so `/` is a stable hub with no server-state read needed.
 *
 * The NavBar still adapts the first slot on server state: idle → "Home → /";
 * active → "Live roast → /live". That gives the operator a direct `/live` link
 * mid-roast, so `/` stays reachable as the hub without any branching here.
 *
 * INVARIANTS: phase is never inferred; `/` NEVER routes to the dashboard;
 * active_run_id is NOT read at this level — it lives in NavBar + LivePage.
 */

import { HomePage } from "./HomePage";

export function HomeGate(): React.JSX.Element {
  return <HomePage />;
}
