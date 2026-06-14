/**
 * Persistent charge-cue banner (#211 — the 2nd-hardware-roast fix).
 *
 * The old `AddBeansToast` was a one-shot, dismiss-forever toast driven by the
 * `charge_guidance` SSE event. In the 2nd hardware roast it was easy to miss —
 * the operator preheated an empty drum for ~8 minutes past the charge point
 * because nothing PERSISTENT told them to add beans. This replaces it with a
 * prominent banner DERIVED from server state that stays on screen the whole time
 * the operator should be charging.
 *
 * It is tri-state (see `chargeCueState`): nothing below the band, the green
 * "CHARGE NOW" cue inside the band, and an ESCALATED amber over-temperature
 * warning once the bean passes the band top while still preheating — so the cue
 * never goes silent on an over-preheat (the exact #211 failure mode). Once beans
 * are added the controller transitions the phase to `roasting_pre_first_crack`,
 * so the banner disappears on its own — no separate "charged" flag, and no
 * locally-inferred phase (deriving the state from telemetry + profile band + the
 * server phase is a PRESENTATION derivation, not phase inference — invariant
 * intact).
 *
 * It is guidance, not a control: it issues no action. T0 is auto-detected by the
 * controller, or the operator uses the existing Mark Beans Added action.
 *
 * All temperatures Celsius (reuses `formatTempC`).
 */

import { cn } from "@/lib/cn";
import type { RoastPhase } from "@/lib/types";
import { type ChargeBand, chargeCueState } from "./chargeWindow";
import { formatClock, formatTempC } from "./format";

export interface ChargeBannerProps {
  /** Server-authoritative phase (from `useRoastStream`); the banner shows only in
   *  the charge phase (`preheating`). Never inferred locally. */
  phase: RoastPhase | null;
  /** Live bean temperature in Celsius (from the SSE telemetry frame). */
  beanTempC: number | null;
  /** The profile's charge band from the REST snapshot; null until hydrated. */
  chargeBand: ChargeBand | null;
  /** Optional dwell timer (seconds the bean has been in the charge zone). When
   *  provided the banner shows it to discourage over-preheating. Omitted ⇒ no
   *  dwell line. */
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
  const state = chargeCueState(phase, beanTempC, chargeBand);
  if (state === "hidden") return null;
  // Unreachable at runtime — chargeCueState returned "hidden" for null inputs
  // above; this guard purely narrows the types for the template (TS can't carry
  // the narrowing across the helper call).
  if (chargeBand === null || beanTempC == null) return null;

  const showDwell = dwellSeconds != null && Number.isFinite(dwellSeconds) && dwellSeconds >= 0;
  const over = state === "over_window";

  return (
    <div
      data-testid="charge-banner"
      data-state={state}
      role="alert"
      // The CTA/heading + the figures announce ONCE when the cue appears (or when
      // it escalates to over-window — the copy changes). The ticking dwell timer is
      // pulled OUT of this assertive region below (#215 FIX B) so it isn't
      // re-announced every second.
      aria-live="assertive"
      className={cn(
        "flex flex-col gap-1 rounded-lg border-l-8 px-5 py-4 shadow-lg motion-safe:animate-pulse",
        // in-window: the nominal (green = go) token. over-window: escalated amber
        // caution. A thick left rail + large type catch the eye from a metre away.
        over
          ? "border-roast-caution bg-roast-caution/15"
          : "border-roast-nominal bg-roast-nominal/15",
        className,
      )}
    >
      <div className="flex items-baseline justify-between gap-4">
        <span
          data-testid="charge-banner-cta"
          className={cn(
            "text-lg font-extrabold uppercase tracking-wide",
            over ? "text-roast-caution" : "text-roast-nominal",
          )}
        >
          {over ? "Over charge temperature — add beans now or reduce heat" : "Charge now — add beans"}
        </span>
        {showDwell && (
          // Dwell ticks every second; keep it OUT of the assertive live region so a
          // screen reader doesn't re-announce the whole alert each tick (#215 FIX B).
          // `aria-hidden` removes only this node from the announcement; the visual
          // is unchanged.
          <span
            data-testid="charge-banner-dwell"
            aria-hidden="true"
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
