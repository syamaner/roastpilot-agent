/**
 * Connection liveness indicator (E10 kickoff §6).
 *
 * Operator trust depends on knowing the data is fresh — staleness is a
 * safety-relevant UI state, so the header shows live / reconnecting / stale
 * explicitly rather than silently freezing. Driven by `useRoastStream`'s status.
 */

import { cn } from "@/lib/cn";
import type { ConnectionStatus } from "@/hooks/useRoastStream";

const STATUS_META: Record<
  ConnectionStatus,
  { label: string; dot: string; text: string }
> = {
  connecting: {
    label: "Connecting",
    dot: "bg-muted-foreground",
    text: "text-muted-foreground",
  },
  live: {
    label: "Live",
    dot: "bg-roast-nominal",
    text: "text-roast-nominal",
  },
  reconnecting: {
    label: "Reconnecting",
    dot: "bg-roast-caution animate-pulse",
    text: "text-roast-caution",
  },
  stale: {
    label: "Stale",
    dot: "bg-roast-fault",
    text: "text-roast-fault",
  },
};

export interface ConnectionIndicatorProps {
  status: ConnectionStatus;
  className?: string;
}

export function ConnectionIndicator({
  status,
  className,
}: ConnectionIndicatorProps): React.JSX.Element {
  const meta = STATUS_META[status];
  return (
    <span
      data-testid="connection-indicator"
      data-status={status}
      className={cn(
        "inline-flex items-center gap-1.5 text-xs font-medium",
        meta.text,
        className,
      )}
    >
      <span className={cn("size-2 rounded-full", meta.dot)} aria-hidden />
      {meta.label}
    </span>
  );
}
