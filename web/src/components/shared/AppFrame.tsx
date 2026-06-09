/**
 * Shared application frame: the header (brand + connection indicator slot) and
 * the routed page outlet. Pages render inside this; the foundation owns the
 * chrome so every page shows the same header + liveness state.
 */

import type { ReactNode } from "react";

import { cn } from "@/lib/cn";

export interface AppFrameProps {
  /** Right-aligned header slot — pages mount the ConnectionIndicator / phase here. */
  headerRight?: ReactNode;
  children: ReactNode;
  className?: string;
}

export function AppFrame({ headerRight, children, className }: AppFrameProps): React.JSX.Element {
  return (
    <div className={cn("min-h-screen bg-background text-foreground", className)}>
      <header className="flex items-center justify-between border-b border-border px-6 py-3">
        <div className="flex items-center gap-2">
          <span className="text-sm font-semibold tracking-tight">RoastPilot</span>
          <span className="text-xs text-muted-foreground">device console</span>
        </div>
        <div className="flex items-center gap-3" data-testid="header-right">
          {headerRight}
        </div>
      </header>
      <main className="px-6 py-4">{children}</main>
    </div>
  );
}
