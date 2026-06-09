/**
 * Event timeline strip (E10-S5, ui-prompts Prompt C #4).
 *
 * A scannable list of the roast's milestone events from `RoastTimeline.events`:
 * T0, first crack (with its audio source + confidence when the payload carries
 * them), drop, cooling start/stop, logs exported. Each row shows the elapsed
 * clock + the event label + its source. Renders only what the contract carries.
 */

import { cn } from "@/lib/cn";
import type { RoastEventKind, RoastTimeline } from "@/lib/types";
import { formatClock, formatConfidence } from "./format";

/** Milestone events shown on the strip, in their canonical roast order, with the
 *  human label. Other event kinds (advisory/command/safety_alert/etc.) belong in
 *  the decision-trace table, not the milestone strip. */
const MILESTONE_LABELS: Partial<Record<RoastEventKind, string>> = {
  run_started: "Run started",
  charge_guidance: "Charge zone reached",
  t0_detected: "Beans added (T0)",
  first_crack: "First crack",
  recovery_required: "Operator recovery required",
  recovery_acknowledged: "Recovery acknowledged",
  fault: "Fault",
  logs_exported: "Logs exported",
  run_completed: "Run completed",
};

export interface EventTimelineProps {
  timeline: RoastTimeline | undefined;
  /** tick → elapsed seconds, so a milestone's clock matches the curve axis. */
  tickToSeconds: (tick: number) => number | null;
  className?: string;
}

export function EventTimeline({
  timeline,
  tickToSeconds,
  className,
}: EventTimelineProps): React.JSX.Element {
  const milestones = (timeline?.events ?? []).filter(
    (event) => event.kind in MILESTONE_LABELS,
  );

  if (milestones.length === 0) {
    return (
      <div
        data-testid="event-timeline-empty"
        className={cn("rounded-lg border border-border bg-card p-4 text-sm text-muted-foreground", className)}
      >
        No timeline events recorded.
      </div>
    );
  }

  return (
    <ol
      data-testid="event-timeline"
      className={cn("flex flex-col gap-2 rounded-lg border border-border bg-card p-4", className)}
    >
      {milestones.map((event, i) => {
        const tick = typeof event.payload?.tick === "number" ? event.payload.tick : null;
        const seconds = event.kind === "t0_detected" ? 0 : tick === null ? null : tickToSeconds(tick);
        const confidence =
          typeof event.payload?.confidence === "number" ? event.payload.confidence : null;
        const source = typeof event.payload?.source === "string" ? event.payload.source : null;
        return (
          <li
            key={`${event.kind}-${event.recorded_at_utc}-${i}`}
            data-testid="timeline-event"
            data-kind={event.kind}
            className="flex items-baseline gap-3 text-sm"
          >
            <span className="numeric w-12 shrink-0 text-muted-foreground">
              {formatClock(seconds)}
            </span>
            <span className="font-medium">{MILESTONE_LABELS[event.kind]}</span>
            {source && (
              <span className="text-xs text-muted-foreground">
                source: {source}
                {confidence !== null && ` · confidence ${formatConfidence(confidence)}`}
              </span>
            )}
          </li>
        );
      })}
    </ol>
  );
}
