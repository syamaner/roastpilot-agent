/**
 * Microphone / first-crack capture-alive status icon (#197).
 *
 * Renders a mic glyph tinted by the server-derived `mic_health` — green (ok),
 * red (error), amber/grey (idle) — with a hover/focus tooltip exposing the raw
 * capture-alive fields behind it (audio running, FC status, the window counts,
 * and the optional reason). Pure observability: this is a READ-OUT of server
 * state (`telemetry.mic_status` / the run snapshot), never inferred client-side
 * and never a control or safety signal.
 *
 * `mic_status === null` means no active session / no info — rendered as IDLE
 * (neutral/amber), deliberately NOT error/red (a missing field is not a fault).
 *
 * No Radix/shadcn tooltip primitive exists in this build, so the tooltip follows
 * the page's self-contained popover precedent (RoastHeader's DiagnosticsDrawer):
 * a CSS group-hover/focus panel. Accessible via `role="status"`, an `aria-label`
 * summarizing the health, and a `title` so the same summary shows on native hover.
 */

import { cn } from "@/lib/cn";
import type { MicHealth, MicStatus } from "@/lib/types";

export interface MicStatusIconProps {
  /** The server-derived mic status; `null`/`undefined` → idle (no info). */
  micStatus: MicStatus | null | undefined;
}

/** Health → token color + operator label. `null` mic_status maps to `idle`. */
const HEALTH_META: Record<MicHealth, { color: string; label: string }> = {
  ok: { color: "var(--roast-nominal)", label: "Mic OK" },
  error: { color: "var(--roast-fault)", label: "Mic error" },
  idle: { color: "var(--roast-caution)", label: "Mic idle" },
};

/** Human label for the FC pipeline status row in the tooltip. */
const FC_STATUS_LABEL: Record<MicStatus["fc_status"], string> = {
  disabled: "disabled",
  manual: "manual",
  pending: "listening",
  detected: "detected",
  faulted: "faulted",
  unavailable: "unavailable",
};

export function MicStatusIcon({ micStatus }: MicStatusIconProps): React.JSX.Element {
  // null/undefined mic_status is IDLE, not error — a missing field is "no info",
  // which must never read as red (a fault). The icon always renders so the
  // operator can see the capture pipeline's state at a glance.
  const health: MicHealth = micStatus?.mic_health ?? "idle";
  const meta = HEALTH_META[health];

  // The accessible/native-hover summary — the same one-line status for screen
  // readers and the OS tooltip; the rich panel below is the visual hover detail.
  const summary = micStatus
    ? `${meta.label}: audio ${micStatus.audio_running ? "running" : "stopped"}, first crack ${FC_STATUS_LABEL[micStatus.fc_status]}`
    : `${meta.label}: no capture session`;

  return (
    <div className="group relative inline-flex">
      <span
        data-testid="mic-status"
        data-health={health}
        role="status"
        aria-label={summary}
        title={summary}
        tabIndex={0}
        className="inline-flex items-center gap-1.5 rounded-md text-xs font-medium text-muted-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <MicGlyph color={meta.color} />
        <span className="hidden sm:inline">{meta.label}</span>
      </span>

      {/* Hover/focus tooltip — the raw capture-alive fields. Shown on the group's
          hover and on keyboard focus-within (focusable trigger above), matching the
          self-contained popover precedent (DiagnosticsDrawer). aria-hidden: the
          one-line summary already carries the status to assistive tech. */}
      <div
        data-testid="mic-status-tooltip"
        aria-hidden
        className={cn(
          "pointer-events-none absolute right-0 top-full z-20 mt-2 w-60 rounded-md border border-border",
          "bg-popover p-3 text-xs text-popover-foreground shadow-lg",
          "opacity-0 transition-opacity duration-100",
          "group-hover:opacity-100 group-focus-within:opacity-100",
        )}
      >
        <p className="mb-2 font-semibold" style={{ color: meta.color }}>
          {meta.label}
        </p>
        {micStatus ? (
          <dl className="flex flex-col gap-1">
            <TooltipRow label="Audio" value={micStatus.audio_running ? "running" : "stopped"} />
            <TooltipRow label="First crack" value={FC_STATUS_LABEL[micStatus.fc_status]} />
            <TooltipRow label="Queued" value={String(micStatus.queued_window_count)} />
            <TooltipRow label="Emitted" value={String(micStatus.emitted_window_count)} />
            <TooltipRow label="Processed" value={String(micStatus.processed_window_count)} />
            <TooltipRow label="Dropped" value={String(micStatus.dropped_window_count)} />
            {micStatus.reason != null && micStatus.reason !== "" && (
              <div className="mt-1 border-t border-border pt-1.5">
                <span className="text-muted-foreground">{micStatus.reason}</span>
              </div>
            )}
          </dl>
        ) : (
          <p className="text-muted-foreground">No active capture session.</p>
        )}
      </div>
    </div>
  );
}

function TooltipRow({ label, value }: { label: string; value: string }): React.JSX.Element {
  return (
    <div className="flex items-center justify-between gap-3">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="numeric font-medium">{value}</dd>
    </div>
  );
}

/** Inline mic glyph (no icon dependency in this build), tinted by health. */
function MicGlyph({ color }: { color: string }): React.JSX.Element {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke={color}
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
      focusable={false}
    >
      <rect x="9" y="2" width="6" height="12" rx="3" />
      <path d="M5 10v2a7 7 0 0 0 14 0v-2" />
      <line x1="12" y1="19" x2="12" y2="22" />
    </svg>
  );
}
