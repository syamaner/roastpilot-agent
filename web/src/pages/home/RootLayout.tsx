/**
 * Routing layout (#324) that mounts the persistent `NavBar` above every routed
 * page, so Home / History / the live roast are reachable from anywhere.
 *
 * Wraps the operator-facing routes only (not the dev/test snapshot harnesses,
 * whose baselines must stay nav-free + deterministic — they sit outside this
 * layout in the route table).
 */

import { Outlet } from "react-router-dom";

import { NavBar } from "./NavBar";

export function RootLayout(): React.JSX.Element {
  return (
    <div className="min-h-screen bg-background text-foreground">
      <NavBar />
      <Outlet />
    </div>
  );
}
