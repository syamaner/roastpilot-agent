/**
 * Persistent device-console navigation (#324).
 *
 * A slim header strip rendered by `RootLayout` above every routed page, so Home /
 * History / the live roast are reachable from anywhere — including mid-roast. The
 * shared `AppFrame` header (brand + connection indicator) stays below it; this bar
 * owns cross-page navigation only.
 *
 * The "Live roast" link is shown ONLY when the server reports an active run
 * (`useHealth().active_run_id`). Active-run presence is SERVER state — we never
 * infer roast phase locally (architecture invariant); the link merely routes to
 * `/`, where `HomeGate` shows the live dashboard while a run is active.
 */

import { NavLink } from "react-router-dom";

import { useHealth } from "@/hooks/queries";
import { cn } from "@/lib/cn";

/** Shared NavLink styling — the active route is brightened, the rest muted. */
function navLinkClass({ isActive }: { isActive: boolean }): string {
  return cn(
    "rounded-md px-3 py-1.5 text-sm font-medium uppercase tracking-wide transition-colors",
    isActive
      ? "bg-accent text-accent-foreground"
      : "text-muted-foreground hover:bg-accent/50 hover:text-foreground",
  );
}

export function NavBar(): React.JSX.Element {
  const health = useHealth();
  // Active-run presence is server-derived (the `/health` snapshot). When present
  // we surface a direct "Live roast" link; phase is never inferred here.
  const hasActiveRun = (health.data?.active_run_id ?? null) !== null;

  return (
    <nav
      data-testid="app-nav"
      className="flex items-center gap-1 border-b border-border bg-card px-6 py-2"
    >
      <NavLink to="/" end className={navLinkClass} data-testid="nav-home">
        Home
      </NavLink>
      <NavLink to="/roasts" className={navLinkClass} data-testid="nav-history">
        History
      </NavLink>
      {hasActiveRun ? (
        // `/` resolves to the live dashboard while a run is active (HomeGate), so
        // the live-roast entry routes there. `end` keeps it from matching `/roasts`.
        <NavLink to="/" end className={navLinkClass} data-testid="nav-live-roast">
          Live roast
        </NavLink>
      ) : null}
    </nav>
  );
}
