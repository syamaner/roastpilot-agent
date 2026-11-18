import { useId } from "react";

import { hasHumanProtection } from "@/lib/humanProtection";
import type {
  HumanEndpointProjection,
  HumanHistoryDurability,
  HumanProtectionProjection,
} from "@/lib/types";

const ENDPOINT_LABELS: Record<HumanEndpointProjection, string> = {
  none: "No drop admitted",
  admitted: "Drop admitted",
  in_flight: "Drop command in progress",
  issued: "Drop command issued",
  unknown: "Drop outcome unknown",
  withheld: "Drop withheld",
  history_unknown: "Prior drop history unknown",
};

const HISTORY_LABELS: Record<HumanHistoryDurability, string> = {
  complete: "Run history complete",
  delayed: "Run history recorded late",
  incomplete: "Run history incomplete",
  unknown: "Run history status unknown",
  unavailable: "Run history unavailable",
};

/** Present the server's disclosure without interpreting it as actuator evidence. */
export function HumanProtectionDisclosure({
  protection,
  historyDurability = null,
}: {
  protection: HumanProtectionProjection | null | undefined;
  historyDurability?: HumanHistoryDurability | null;
}): React.JSX.Element | null {
  const headingId = useId();
  if (!hasHumanProtection(protection) && historyDurability === null) return null;

  const endpoint = protection?.human_endpoint ?? null;
  const command = protection?.human_command_disposition ?? null;
  const intervention = protection?.physical_intervention_required;
  const fallback = protection?.emergency_fallback_disclosure ?? null;

  return (
    <section
      aria-labelledby={headingId}
      aria-live="polite"
      className="flex flex-col gap-2 rounded-md border border-roast-caution/50 bg-roast-caution/10 p-4 text-sm"
    >
      <h3 id={headingId} className="font-semibold">Human-mode protection</h3>
      {intervention === true && (
        <p className="font-semibold text-roast-caution">Physical intervention required</p>
      )}
      {(endpoint !== null || command !== null || historyDurability !== null) && (
        <dl className="flex flex-wrap gap-x-6 gap-y-2">
          {endpoint !== null && (
            <div>
              <dt className="text-xs text-muted-foreground">Endpoint disposition</dt>
              <dd>{ENDPOINT_LABELS[endpoint]}</dd>
            </div>
          )}
          {command !== null && (
            <div>
              <dt className="text-xs text-muted-foreground">Command disposition</dt>
              <dd>{command === "unknown" ? "Command outcome unknown" : "Settled"}</dd>
            </div>
          )}
          {historyDurability !== null && (
            <div>
              <dt className="text-xs text-muted-foreground">History durability</dt>
              <dd>{HISTORY_LABELS[historyDurability]}</dd>
            </div>
          )}
        </dl>
      )}
      {fallback !== null && <p className="text-muted-foreground">{fallback}</p>}
    </section>
  );
}
