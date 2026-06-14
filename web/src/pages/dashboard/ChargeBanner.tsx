/**
 * Persistent charge-window banner (#211 — the 2nd-hardware-roast fix).
 *
 * The old `AddBeansToast` was a one-shot, dismiss-forever toast driven by the
 * `charge_guidance` SSE event. In the 2nd hardware roast it was easy to miss —
 * the operator preheated an empty drum for ~8 minutes past the charge point
 * because nothing PERSISTENT told them to add beans. This replaces it with a
 * prominent banner DERIVED from server state that stays on screen the whole time
 * the operator should be charging.
 *
 * It renders when the SERVER phase is `preheating` AND the live bean temperature
 * sits inside the profile's charge band. Once beans are added the controller
 * transitions the phase to `roasting_pre_first_crack`, so the banner disappears
 * on its own — no separate "charged" flag, and no locally-inferred phase
 * (deriving the in-window boolean from telemetry + profile band + the server
 * phase is a PRESENTATION derivation, not phase inference — invariant intact).
 *
 * It is guidance, not a control: it issues no action. T0 is auto-detected by the
 * controller, or the operator uses the existing Mark Beans Added action.
 *
 * All temperatures Celsius (reuses `formatTempC`).
 */

import { cn } from "@/lib/cn";
import type { RoastPhase } from "@/lib/types";
import { type ChargeBand, isInChargeWindow } from "./chargeWindow";
import { formatClock, formatTempC } from "./format";

export interface ChargeBannerProps {
  /** Server-authoritative phase (from `useRoastStream`); the banner shows only in
   *  the charge phase (`preheating`). Never inferred locally. */
  phase: RoastPhase | null;
  /** Live bean temperature in Celsius (from the SSE telemetry frame). */
  beanTempC: number | null;
  /** The profile's charge band from the REST snapshot; null until hydrated. */
  chargeBand: ChargeBand | null;
  /** Optional dwell timer (seconds the bean has been in the window). When provided
   *  the banner nudges against over-preheating. Omitted ⇒ no dwell line. */
  dwellSeconds?: number | null;
  className?: string;
}

export function ChargeBanner({
  phase,
  beanTempC,
  chargeBand,
  dwellSeconds,
  className,
}: ChargeBannerProps): React.JSX.Element | null {
  if (!isInChargeWindow(phase, beanTempC, chargeBand)) return null;
  // `chargeBand` / `beanTempC` are non-null here (the guard above narrows them),
  // but TypeScript doesn't carry that across the helper call, so re-assert.
  if (chargeBand === null || beanTempC == null) return null;

  const showDwell = dwellSeconds != null && Number.isFinite(dwellSeconds) && dwellSeconds >= 0;

  return (
    <div
      data-testid="charge-banner"
      role="alert"
      aria-live="assertive"
      className={cn(
        // Prominent: the nominal (green = go) token, a thick left rail, large type,
        // a soft pulse so it catches the eye from a metre away at the roaster.
        "flex flex-col gap-1 rounded-lg border-l-8 border-roast-nominal bg-roast-nominal/15 px-5 py-4 shadow-lg motion-safe:animate-pulse",
        className,
      )}
    >
      <div className="flex items-baseline justify-between gap-4">
        <span
          data-testid="charge-banner-cta"
          className="text-lg font-extrabold uppercase tracking-wide text-roast-nominal"
        >
          Charge now — add beans
        </span>
        {showDwell && (
          <span
            data-testid="charge-banner-dwell"
            className="numeric text-sm font-medium text-muted-foreground"
            title="Time in the charge window"
          >
            in window {formatClock(dwellSeconds)}
          </span>
        )}
      </div>
      <span className="numeric text-sm text-foreground/80">
        Bean {formatTempC(beanTempC)} · charge window {formatTempC(chargeBand.minC)}–
        {formatTempC(chargeBand.maxC)}
      </span>
    </div>
  );
}
