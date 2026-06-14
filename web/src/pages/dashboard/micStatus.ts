/**
 * Pure mic-status selection logic (#197/#200), kept out of the component file
 * so it can be shared without tripping react-refresh's only-export-components.
 */

import type { MicStatus } from "@/lib/types";

/**
 * Choose the mic status to display from the live telemetry frame and the run
 * snapshot.
 *
 * Once a telemetry frame exists it is authoritative — its `mic_status` passes
 * through even when `null` (the documented "idle / no info" case), so the icon
 * returns to idle rather than sticking on the snapshot's stale green/red
 * (#200/Codex). Before any frame, the snapshot paints the icon on hydrate. Both
 * inputs are server-derived; this never infers state client-side.
 */
export function resolveMicStatus(
  telemetry: { mic_status: MicStatus | null } | null | undefined,
  snapshot: MicStatus | null | undefined,
): MicStatus | null {
  if (telemetry != null) return telemetry.mic_status ?? null;
  return snapshot ?? null;
}
