/**
 * Dev/test-only foundation harness route (`/__chart-harness`).
 *
 * NOT a product page. It mounts the shared foundation components with fixed,
 * deterministic data so the Playwright snapshot suite (D24) has a stable target
 * BEFORE any page exists — proving the chart-data test hook, the canvas-mask
 * convention, the verdict badges, and the connection indicator all work, so
 * S3–S5 drop their pages into an already-green gate. Excluded from the product
 * routing once pages land if desired; kept lightweight and self-contained.
 */

import { useState } from "react";

import { AppFrame, ConnectionIndicator, LiveCurve, VerdictBadge } from "@/components/shared";
import type { ConnectionStatus } from "@/hooks/useRoastStream";
import type { SafetyVerdict } from "@/lib/types";
import type { CurveMarker, CurvePoint } from "@/components/shared/LiveCurve";

// Deterministic synthetic curve — a short roast arc. Fixed numbers so snapshots
// and chart-data assertions are reproducible.
const POINTS: CurvePoint[] = Array.from({ length: 24 }, (_, i) => {
  const t = i * 30;
  return {
    t,
    bean: 90 + i * 5.5,
    env: 110 + i * 4.5,
    ror: Math.max(2, 18 - i * 0.6),
    heat: i < 8 ? 80 : i < 16 ? 65 : 50,
    fan: i < 8 ? 40 : i < 16 ? 55 : 70,
  };
});

const MARKERS: CurveMarker[] = [
  { kind: "t0", t: 0, label: "T0" },
  { kind: "first_crack", t: 510, label: "FIRST CRACK" },
];

const ALL_VERDICTS: SafetyVerdict[] = [
  "allow",
  "clamp",
  "reject",
  "recovery",
  "fault",
  "emergency_stop",
];

export function ChartHarnessPage(): React.JSX.Element {
  // A control to exercise the trace-row → highlight toggle from a test.
  const [highlight, setHighlight] = useState<number | null>(null);
  const [status, setStatus] = useState<ConnectionStatus>("live");

  return (
    <AppFrame headerRight={<ConnectionIndicator status={status} />}>
      <div className="flex flex-col gap-4">
        <div className="flex flex-wrap items-center gap-2" data-testid="verdict-gallery">
          {ALL_VERDICTS.map((v) => (
            <VerdictBadge key={v} verdict={v} />
          ))}
          <span className="text-xs text-muted-foreground">
            (RECOVERY / FAULT / EMERGENCY_STOP render nothing — not badges)
          </span>
        </div>

        <div className="flex flex-wrap gap-2 text-xs">
          {(["connecting", "live", "reconnecting", "stale"] as ConnectionStatus[]).map((s) => (
            <button
              key={s}
              type="button"
              data-testid={`set-status-${s}`}
              onClick={() => setStatus(s)}
              className="rounded border border-border px-2 py-1"
            >
              {s}
            </button>
          ))}
          <button
            type="button"
            data-testid="toggle-highlight"
            onClick={() => setHighlight((h) => (h === null ? 510 : null))}
            className="rounded border border-border px-2 py-1"
          >
            toggle highlight
          </button>
        </div>

        <div className="rounded-lg border border-border bg-card p-3">
          <LiveCurve
            points={POINTS}
            markers={MARKERS}
            phase="preheating"
            highlightTime={highlight}
          />
        </div>
      </div>
    </AppFrame>
  );
}
