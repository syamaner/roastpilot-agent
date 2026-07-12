/**
 * Home / landing hub (#324, updated #423 D81, #473, #523) — the links hub,
 * always the same three entry points, never a form and never a dashboard.
 *
 * Shown at `/` always (`HomeGate` is now a pure pass-through; see D81). Three
 * entry points: Start a new roast (→ `/start`, the ONLY start-form surface
 * under the #523 IA), Live/last roast (→ `/live`, the roaster's permanent
 * state address — live dashboard while active, the last completed run's
 * summary otherwise), and Settings (→ `/config`, #473). A live-status chip
 * in the header signals an active run without turning this page into a
 * dashboard. Pure navigation — no roaster data beyond the `active_run_id`
 * presence check, no SSE, no MCP. Phase is never inferred here.
 */

import { Link } from "react-router-dom";

import { AppFrame } from "@/components/shared";
import { useHealth } from "@/hooks/queries";

/** One hub tile: a large, tap-friendly link with a title + supporting line. */
interface HubTileProps {
  to: string;
  testId: string;
  title: string;
  description: string;
}

function HubTile({ to, testId, title, description }: HubTileProps): React.JSX.Element {
  return (
    <Link
      to={to}
      data-testid={testId}
      className="group flex flex-col gap-2 rounded-lg border border-border bg-card px-6 py-8 transition-colors hover:border-roast-nominal/60 hover:bg-accent/40"
    >
      <span className="text-xl font-semibold tracking-tight text-foreground group-hover:text-roast-nominal">
        {title}
      </span>
      <span className="text-sm text-muted-foreground">{description}</span>
    </Link>
  );
}

export function HomePage(): React.JSX.Element {
  // Active-run presence is server-derived (the `/health` snapshot) — the same
  // signal NavBar uses for its own live-roast link. This page never infers
  // phase; it only reflects whether a run is active for the header chip and
  // the Live/last-roast tile's supporting copy.
  const health = useHealth();
  const hasActiveRun = (health.data?.active_run_id ?? null) !== null;

  return (
    <AppFrame
      headerRight={
        hasActiveRun ? (
          <span
            data-testid="home-live-status-chip"
            className="rounded-full bg-roast-nominal/15 px-3 py-1 text-xs font-semibold uppercase tracking-wide text-roast-nominal"
          >
            Roast in progress
          </span>
        ) : (
          <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Idle
          </span>
        )
      }
    >
      <div className="mx-auto max-w-3xl" data-testid="home-page">
        <header className="mb-8">
          <h1 className="font-mono text-3xl text-foreground">RoastPilot</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Start a roast or review past roasts and rate them.
          </p>
        </header>

        <div className="grid gap-4 sm:grid-cols-2">
          <HubTile
            to="/start"
            testId="home-start-roast"
            title="Start a new roast"
            description="Set up the bean profile and roast targets, then begin preheating."
          />
          <HubTile
            to="/live"
            testId="home-live-roast"
            title={hasActiveRun ? "View live roast" : "Last roast"}
            description={
              hasActiveRun
                ? "See status and controls for the roast in progress, including emergency stop."
                : "Review the summary of the most recently completed roast."
            }
          />
          <HubTile
            to="/roasts"
            testId="home-view-roasts"
            title="View & rate roasts"
            description="Browse roast history, open a roast for detail, and rate the result."
          />
          <HubTile
            to="/config"
            testId="home-settings"
            title="Settings"
            description="Configure the agent, hardware, and detection defaults for the next roast."
          />
        </div>
      </div>
    </AppFrame>
  );
}
