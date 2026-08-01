/** Deterministic #668 operator hardware-clear warning snapshot harness. */

import { AppFrame } from "@/components/shared";
import type {
  HardwareClearAcknowledgementRequest,
  HardwareClearAcknowledgementResult,
} from "@/lib/types";
import { HardwareClearAcknowledgementCard } from "@/pages/home/StartRoastView";

const INCIDENT_ID = "a".repeat(32);

async function acknowledge(
  request: HardwareClearAcknowledgementRequest,
): Promise<HardwareClearAcknowledgementResult> {
  return {
    result: "accepted",
    hardware_clear: true,
    teardown_incident_id: request.teardown_incident_id,
    fresh_spawn_permitted: true,
  };
}

export function HardwareClearHarnessPage(): React.JSX.Element {
  return (
    <AppFrame
      headerRight={
        <span className="text-xs font-semibold uppercase tracking-wide text-roast-fault">
          Start blocked
        </span>
      }
    >
      <HardwareClearAcknowledgementCard
        incidentId={INCIDENT_ID}
        onAcknowledge={acknowledge}
      />
    </AppFrame>
  );
}
