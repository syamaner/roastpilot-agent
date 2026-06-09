/**
 * Control row (ui-prompts Prompt A §3, kickoff §2).
 *
 * Two large readouts — HEAT (amber) and FAN (teal) — each a 0–100 % slider-style
 * bar with the current applied value AND a small ghost marker at the advisor's
 * recommended target (from the latest advisory decision). The ghost marker makes
 * the gap between what's commanded and what the advisor wants visible at a glance.
 *
 * Current values come from the live `telemetry` frame (applied heat/fan); targets
 * from the latest advisory `decision`. All values are %. The bars are presentation
 * only — the operator never sets heat/fan from here (the advisor advises; the
 * controller commands; operator control is the action bar's drop/cool/e-stop).
 */

import { formatPercent } from "./format";

export interface ControlRowProps {
  heatPercent: number | null;
  fanPercent: number | null;
  /** Advisor's recommended targets (the ghost markers); null when none yet. */
  targetHeatPercent: number | null;
  targetFanPercent: number | null;
}

export function ControlRow({
  heatPercent,
  fanPercent,
  targetHeatPercent,
  targetFanPercent,
}: ControlRowProps): React.JSX.Element {
  return (
    <div data-testid="control-row" className="grid grid-cols-1 gap-4 sm:grid-cols-2">
      <ControlIndicator
        kind="heat"
        label="Heat"
        value={heatPercent}
        target={targetHeatPercent}
        colorVar="var(--roast-heat)"
      />
      <ControlIndicator
        kind="fan"
        label="Fan"
        value={fanPercent}
        target={targetFanPercent}
        colorVar="var(--roast-fan)"
      />
    </div>
  );
}

function ControlIndicator({
  kind,
  label,
  value,
  target,
  colorVar,
}: {
  kind: "heat" | "fan";
  label: string;
  value: number | null;
  target: number | null;
  colorVar: string;
}): React.JSX.Element {
  const pct = clampPercent(value);
  const targetPct = target === null ? null : clampPercent(target);
  return (
    <section
      data-testid={`control-${kind}`}
      className="flex flex-col gap-3 rounded-lg border border-border bg-card p-5"
    >
      <div className="flex items-baseline justify-between">
        <span
          className="text-sm font-semibold uppercase tracking-wide"
          style={{ color: colorVar }}
        >
          {label}
        </span>
        <span
          data-testid={`control-${kind}-value`}
          className="numeric text-3xl font-bold"
          style={{ color: colorVar }}
        >
          {formatPercent(value)}
        </span>
      </div>

      <div className="relative h-3 w-full rounded-full bg-secondary">
        <div
          className="absolute inset-y-0 left-0 rounded-full"
          style={{ width: `${pct}%`, backgroundColor: colorVar }}
          aria-hidden
        />
        {targetPct !== null && (
          <span
            data-testid={`control-${kind}-ghost`}
            data-target={Math.round(target ?? 0)}
            title={`Advisor target ${formatPercent(target)}`}
            className="absolute top-1/2 size-3 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-white/80 bg-transparent"
            style={{ left: `${targetPct}%` }}
            aria-label={`Advisor target ${formatPercent(target)}`}
          />
        )}
      </div>

      <div className="flex justify-between text-xs text-muted-foreground">
        <span>0</span>
        <span>50</span>
        <span>100</span>
      </div>
    </section>
  );
}

/** Clamp a percent into 0–100 for bar positioning (null → 0 width). */
function clampPercent(value: number | null): number {
  if (value == null || !Number.isFinite(value)) return 0;
  return Math.max(0, Math.min(100, value));
}
