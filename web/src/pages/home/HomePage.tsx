/**
 * Home / landing hub (#324, updated #423 D81, #473) — the idle navigation centre.
 *
 * Shown at `/` always (`HomeGate` is now a pure pass-through; see D81). Three
 * entry points: Start a new roast (→ `/live`, which shows the start form
 * when idle and takes the operator directly to the dashboard on a running roast),
 * View / rate roasts (→ the history list at `/roasts`), and Settings (→ `/config`,
 * #473 — previously reachable only by typing the URL). Pure navigation — no
 * roaster data, no SSE, no MCP. Phase is never inferred here.
 */

import { Link } from "react-router-dom";

import { AppFrame } from "@/components/shared";

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
  return (
    <AppFrame
      headerRight={
        <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          Idle
        </span>
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
            to="/live"
            testId="home-start-roast"
            title="Start a new roast"
            description="Set up the bean profile and roast targets, then begin preheating."
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
