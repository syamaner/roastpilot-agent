/**
 * Control row (ui-prompts Prompt A §3, kickoff §2).
 *
 * Two large readouts — HEAT (amber) and FAN (teal). Heat/fan are NEVER set from
 * here: the advisor advises, the controller commands, and operator control is the
 * action bar's drop/cool/e-stop. The bars are presentation only.
 *
 * Pre-first-crack vs post-first-crack presentation (#318, option C + read-out UI):
 *   - PRE-FC (`preheating` / `roasting_pre_first_crack`): the controller drives
 *     heat/fan deterministically off the bean profile (D59) and re-asserts them
 *     every tick. There is nothing to set and no advisor target in play (the
 *     advisor is gated out pre-FC). So pre-FC we render PLAIN READ-OUTS: just the
 *     value, no slider-style bar and no advisor-target ghost marker — nothing that
 *     implies a settable dial that would silently snap back (the roast-2 confusion).
 *   - POST-FC (`development` onward): the advisor advises + the operator can act +
 *     the deadband gate applies, so the slider-style bar AND the advisor-target
 *     ghost marker render (unchanged).
 *
 * INVARIANT: the pre-FC-vs-post-FC presentation is gated on the SERVER-provided
 * roast phase (passed down from the dashboard's server-derived `phase`), never on
 * any client-side heuristic. The component never infers phase from telemetry.
 *
 * Current values come from the live `telemetry` frame (applied heat/fan); targets
 * from the latest advisory `decision`. All values are %.
 */

import type { RoastPhase } from "@/lib/types";

import { formatPercent, isPreFirstCrackPhase } from "./format";

export interface ControlRowProps {
  /** Server-provided roast phase (read-out vs interactive gate). Never inferred. */
  phase: RoastPhase | null;
  heatPercent: number | null;
  fanPercent: number | null;
  /** Advisor's recommended targets (the ghost markers); null when none yet. */
  targetHeatPercent: number | null;
  targetFanPercent: number | null;
}

export function ControlRow({
  phase,
  heatPercent,
  fanPercent,
  targetHeatPercent,
  targetFanPercent,
}: ControlRowProps): React.JSX.Element {
  // Pre-FC: read-outs only (no bar, no advisor target). The advisor is gated out
  // pre-FC, so even a passed-through target is suppressed here.
  const preFc = isPreFirstCrackPhase(phase);
  return (
    <div data-testid="control-row" className="grid grid-cols-1 gap-4 sm:grid-cols-2">
      <ControlIndicator
        kind="heat"
        label="Heat"
        value={heatPercent}
        target={preFc ? null : targetHeatPercent}
        readOut={preFc}
        colorVar="var(--roast-heat)"
      />
      <ControlIndicator
        kind="fan"
        label="Fan"
        value={fanPercent}
        target={preFc ? null : targetFanPercent}
        readOut={preFc}
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
  readOut,
  colorVar,
}: {
  kind: "heat" | "fan";
  label: string;
  value: number | null;
  target: number | null;
  /** Pre-FC read-out mode: no slider-style bar, no advisor-target ghost marker. */
  readOut: boolean;
  colorVar: string;
}): React.JSX.Element {
  const pct = clampPercent(value);
  const targetPct = target === null ? null : clampPercent(target);
  return (
    <section
      data-testid={`control-${kind}`}
      data-mode={readOut ? "readout" : "interactive"}
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

      {readOut ? (
        // Pre-FC read-out: the controller drives this deterministically off the
        // bean profile (D59). No bar/dial/ghost — nothing to set, nothing to
        // silently revert (the roast-2 confusion, #318).
        <p
          data-testid={`control-${kind}-readout-note`}
          className="text-xs text-muted-foreground"
        >
          Controller-driven (read-out)
        </p>
      ) : (
        <>
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
        </>
      )}
    </section>
  );
}

/** Clamp a percent into 0–100 for bar positioning (null → 0 width). */
function clampPercent(value: number | null): number {
  if (value == null || !Number.isFinite(value)) return 0;
  return Math.max(0, Math.min(100, value));
}
