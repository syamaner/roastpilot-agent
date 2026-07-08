/**
 * "Roast conditions" widget (#464, D86).
 *
 * Renders the ambient temperature/humidity/pressure triad captured ONCE at
 * charge (`RoastDetail.ambient_*`, #342/D85) — read-only corpus metadata, not a
 * control. Distinct from the live dashboard's "Room" readout (#464 too), which
 * mirrors the CURRENT/latest telemetry-frame reading; this is the single
 * charge-instant snapshot for the finished/persisted roast. `null` per field
 * when never captured (a pre-#342 run, or an ambient-disabled/unavailable MCP
 * config) — rendered as "—", matching `RoastedWeight`'s not-yet-weighed pattern
 * rather than hiding the widget outright.
 */

import { cn } from "@/lib/cn";
import { formatAmbientHumidity, formatAmbientPressure, formatAmbientTemp } from "./format";

export interface RoastConditionsProps {
  ambientTempC: number | null | undefined;
  ambientHumidityPct: number | null | undefined;
  ambientPressureHpa: number | null | undefined;
  className?: string;
}

export function RoastConditions({
  ambientTempC,
  ambientHumidityPct,
  ambientPressureHpa,
  className,
}: RoastConditionsProps): React.JSX.Element {
  const captured = ambientTempC != null || ambientHumidityPct != null || ambientPressureHpa != null;
  return (
    <div
      data-testid="roast-conditions"
      className={cn("flex flex-col gap-3 rounded-lg border border-border bg-card p-4", className)}
    >
      <h3 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
        Roast conditions
      </h3>

      <dl className="flex flex-wrap gap-x-6 gap-y-2">
        <div className="flex flex-col">
          <dt className="text-xs text-muted-foreground">Ambient temp</dt>
          <dd className="numeric text-sm font-medium" data-testid="roast-conditions-temp">
            {formatAmbientTemp(ambientTempC ?? null)}
          </dd>
        </div>
        <div className="flex flex-col">
          <dt className="text-xs text-muted-foreground">Humidity</dt>
          <dd className="numeric text-sm font-medium" data-testid="roast-conditions-humidity">
            {formatAmbientHumidity(ambientHumidityPct ?? null)}
          </dd>
        </div>
        <div className="flex flex-col">
          <dt className="text-xs text-muted-foreground">Pressure</dt>
          <dd className="numeric text-sm font-medium" data-testid="roast-conditions-pressure">
            {formatAmbientPressure(ambientPressureHpa ?? null)}
          </dd>
        </div>
      </dl>

      {!captured && (
        <p data-testid="roast-conditions-uncaptured" className="text-xs text-muted-foreground">
          Not captured for this roast (ambient probe disabled/unavailable at charge).
        </p>
      )}
    </div>
  );
}
