/**
 * Persistent device-console navigation (#324, updated #403).
 *
 * A slim header strip rendered by `RootLayout` above every routed page, so Home /
 * History / the live roast are reachable from anywhere — including mid-roast. The
 * shared `AppFrame` header (brand + connection indicator) stays below it; this bar
 * owns cross-page navigation only.
 *
 * The first nav slot adapts to server state:
 *   - Idle (no active run): "Home" → `/` (the landing hub).
 *   - Active run: "Live roast" → `/live` (the stable reload-safe route, #403).
 *
 * `/live` and `/` are distinct routes so they never both light up the active-link
 * highlight at the same time, and the operator can always see which page they are on.
 * Active-run presence is SERVER state (`useHealth().active_run_id`); we never infer
 * roast phase locally (architecture invariant).
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
  // we surface a direct "Live roast" link (→ /live, the stable route, #403);
  // phase is never inferred here.
  const hasActiveRun = (health.data?.active_run_id ?? null) !== null;

  return (
    <nav
      data-testid="app-nav"
      className="flex items-center gap-1 border-b border-border bg-card px-6 py-2"
    >
      {/* First slot: Home (→ /) when idle; Live roast (→ /live) when active (#403).
          The two distinct paths mean the active-link highlight is never ambiguous. */}
      {hasActiveRun ? (
        <NavLink to="/live" className={navLinkClass} data-testid="nav-live-roast">
          Live roast
        </NavLink>
      ) : (
        <NavLink to="/" end className={navLinkClass} data-testid="nav-home">
          Home
        </NavLink>
      )}
      <NavLink to="/roasts" className={navLinkClass} data-testid="nav-history">
        History
      </NavLink>
    </nav>
  );
}
