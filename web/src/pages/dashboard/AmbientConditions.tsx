/**
 * Live ambient "Conditions" readout (#464, D86).
 *
 * Renders the LATEST ambient triad (temperature / relative humidity / barometric
 * pressure) off the live telemetry frame — the same mirror-and-render pattern as
 * `MicStatusIcon`/`mic_status`: pure observability, server-derived every tick,
 * never inferred client-side and never a control or safety signal. Distinct from
 * the detail page's "Roast conditions" (the ONE-TIME charge-instant capture from
 * `RoastDetail`) — this reads the CURRENT reading, which can keep updating (or go
 * stale to null) through the roast.
 *
 * A subtle, room-labelled readout (not the bean probe) — deliberately unobtrusive
 * so it doesn't compete with the primary bean-temp/RoR/heat-fan surface (kickoff
 * §1 minimal-UX rule: a readout, not a control). Renders "—" per field when null
 * (uncaptured/disabled/unavailable this tick) rather than hiding the whole row,
 * so the operator always sees the same three-field shape.
 */

import { formatAmbientHumidity, formatAmbientPressure, formatAmbientTempC } from "./format";

export interface AmbientConditionsProps {
  /** Latest ambient temperature in Celsius from the live telemetry frame. */
  ambientTempC: number | null | undefined;
  /** Latest ambient relative humidity percentage. */
  ambientHumidityPct: number | null | undefined;
  /** Latest ambient barometric pressure in hectopascals. */
  ambientPressureHpa: number | null | undefined;
}

export function AmbientConditions({
  ambientTempC,
  ambientHumidityPct,
  ambientPressureHpa,
}: AmbientConditionsProps): React.JSX.Element {
  return (
    <span
      data-testid="ambient-conditions"
      className="inline-flex items-center gap-1.5 text-xs text-muted-foreground"
      title="Latest room/ambient reading — not the bean probe"
    >
      {/* Collapses under `sm:` (mirroring MicStatusIcon's `hidden sm:inline`) so
          this context chip isn't the widest item in a wrapped ~820px header —
          the triad values (the actual signal) always stay visible. */}
      <span className="hidden uppercase tracking-wide text-muted-foreground/80 sm:inline">
        Room
      </span>
      <span data-testid="ambient-temp" className="numeric font-medium">
        {formatAmbientTempC(ambientTempC)}
      </span>
      <span aria-hidden>·</span>
      <span data-testid="ambient-humidity" className="numeric font-medium">
        {formatAmbientHumidity(ambientHumidityPct)}
      </span>
      <span aria-hidden>·</span>
      <span data-testid="ambient-pressure" className="numeric font-medium">
        {formatAmbientPressure(ambientPressureHpa)}
      </span>
    </span>
  );
}
