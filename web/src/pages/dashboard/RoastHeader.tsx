/**
 * Live dashboard header (ui-prompts Prompt A §1, kickoff §2).
 *
 * Phase badge (the operator-facing truth, tinted by the phase token), the roast
 * timer, the profile name, the first-crack pipeline status, the roaster-link
 * status, and a diagnostics drawer.
 *
 * Renders only REAL contract state (kickoff §8 — surface gaps, never invent):
 *  - Development %: the live `telemetry` frame carries NO `development_percent`
 *    (it is only on the REST series), so we show a development TIMER (time since
 *    the first-crack event) and OMIT the %. Tracked for a faithful server-field
 *    fix as #112. (Don't synthesize a % the live frame can't support.)
 *  - First-crack-audio health: the contract carries no FC-audio pipeline health
 *    signal, so we show real FC STATE — "listening" pre-FC, then "detected at
 *    HH:MM · X °C · source" from the real `first_crack` event — not a mock
 *    "audio: green" dot. The roaster-link dot reflects `mcp_child` health.
 *
 * Phase comes from the server only. All temperatures Celsius; numerics tabular.
 */

import { useState } from "react";

import { cn } from "@/lib/cn";
import type { MCPChildStatus, MicStatus, RoastPhase } from "@/lib/types";
import { formatClock, formatRoR, formatTempC, PHASE_LABEL, phaseAccentVar } from "./format";
import type { FirstCrackData } from "./events";
import { MicStatusIcon } from "./MicStatusIcon";

export interface RoastHeaderProps {
  phase: RoastPhase | null;
  /** Seconds since the run started (the roast timer); from telemetry.elapsed. */
  elapsedSeconds: number | null;
  /** Seconds since the first-crack event (the development timer); null pre-FC. */
  developmentSeconds: number | null;
  /**
   * Live bean Rate of Rise (°C/min) from the current telemetry frame; null until
   * the server has computed a rate. Shown from the start (incl. preheat) — it is
   * real probe data; the charge (T0) marker on the curve flags where the
   * meaningful post-charge RoR begins (#165, operator clarification 13 Jun).
   */
  beanRorCPerMin: number | null;
  profileName: string | null;
  /** Real FC detection from the `first_crack` event; null until it fires. */
  firstCrack: FirstCrackData | null;
  /** MCP child link health (the roaster-link dot); undefined while unknown. */
  mcpChild?: MCPChildStatus;
  /**
   * Capture-alive mic / first-crack health (#197), server-derived from the live
   * telemetry frame (or the run snapshot before the first frame). null/undefined
   * renders the icon as idle — no info, NOT a fault.
   */
  micStatus?: MicStatus | null;
}

export function RoastHeader({
  phase,
  elapsedSeconds,
  developmentSeconds,
  beanRorCPerMin,
  profileName,
  firstCrack,
  mcpChild,
  micStatus,
}: RoastHeaderProps): React.JSX.Element {
  const accent = phaseAccentVar(phase);
  return (
    <header
      data-testid="roast-header"
      className="flex flex-wrap items-center justify-between gap-4 rounded-lg border border-border bg-card px-5 py-3"
    >
      <div className="flex items-center gap-5">
        <span
          data-testid="phase-badge"
          data-phase={phase ?? ""}
          className="inline-flex items-center rounded-md border px-3 py-1 text-sm font-semibold uppercase tracking-wide"
          style={
            accent
              ? {
                  borderColor: accent,
                  color: accent,
                  backgroundColor: `color-mix(in srgb, ${accent} 15%, transparent)`,
                }
              : undefined
          }
        >
          {phase ? PHASE_LABEL[phase] : "—"}
        </span>

        <Metric label="Roast Time" value={formatClock(elapsedSeconds)} testid="roast-timer" />
        {/* Live Rate of Rise — the signal roasters steer by, and the same signal
            the advisor reasons on (operator parity, #165). Bean °C/min, Celsius. */}
        <Metric
          label="RoR (bean)"
          value={formatRoR(beanRorCPerMin)}
          testid="ror-readout"
          accent="var(--roast-nominal)"
        />
        {developmentSeconds !== null && (
          <Metric
            label="Development"
            value={formatClock(developmentSeconds)}
            testid="development-timer"
          />
        )}
      </div>

      <div className="flex items-center gap-5">
        <div className="flex flex-col items-end">
          <span className="text-xs uppercase tracking-wide text-muted-foreground">Profile</span>
          <span data-testid="profile-name" className="text-sm font-medium">
            {profileName ?? "—"}
          </span>
        </div>
        <FirstCrackStatus phase={phase} firstCrack={firstCrack} />
        <MicStatusIcon micStatus={micStatus} />
        <RoasterLink status={mcpChild} />
        <DiagnosticsDrawer
          phase={phase}
          mcpChild={mcpChild}
          firstCrack={firstCrack}
          elapsedSeconds={elapsedSeconds}
        />
      </div>
    </header>
  );
}

function Metric({
  label,
  value,
  testid,
  accent,
}: {
  label: string;
  value: string;
  testid: string;
  /** Optional CSS color for the value (e.g. the RoR readout matches its series). */
  accent?: string;
}): React.JSX.Element {
  return (
    <div className="flex flex-col">
      <span className="text-xs uppercase tracking-wide text-muted-foreground">{label}</span>
      <span
        data-testid={testid}
        className="numeric text-lg font-semibold"
        style={accent ? { color: accent } : undefined}
      >
        {value}
      </span>
    </div>
  );
}

/** Real first-crack state — "listening" before FC, the detection after. NEVER a
 *  mock audio-health dot (the contract carries no FC-audio pipeline health). */
function FirstCrackStatus({
  phase,
  firstCrack,
}: {
  phase: RoastPhase | null;
  firstCrack: FirstCrackData | null;
}): React.JSX.Element {
  // Pre-FC, FC detection is only meaningful while actively roasting toward it.
  const listening = phase === "roasting_pre_first_crack";
  let label: string;
  let dot: string;
  if (firstCrack) {
    const temp = firstCrack.bean_temp_c != null ? ` · ${formatTempC(firstCrack.bean_temp_c)}` : "";
    label = `FC detected${temp} · ${firstCrack.source}`;
    dot = "bg-roast-nominal";
  } else if (listening) {
    label = "FC: listening";
    dot = "bg-roast-caution animate-pulse";
  } else {
    label = "FC: —";
    dot = "bg-muted-foreground/50";
  }
  return (
    <span
      data-testid="fc-status"
      data-detected={firstCrack ? "true" : "false"}
      className="inline-flex items-center gap-1.5 text-xs font-medium text-muted-foreground"
    >
      <span className={cn("size-2 rounded-full", dot)} aria-hidden />
      {label}
    </span>
  );
}

/** Roaster-link health from the MCP child status (real signal). */
function RoasterLink({ status }: { status?: MCPChildStatus }): React.JSX.Element {
  const meta: Record<MCPChildStatus, { label: string; dot: string }> = {
    running: { label: "Roaster", dot: "bg-roast-nominal" },
    stopped: { label: "Roaster down", dot: "bg-roast-fault" },
    not_configured: { label: "Roaster n/a", dot: "bg-muted-foreground/50" },
  };
  const m = status ? meta[status] : { label: "Roaster …", dot: "bg-muted-foreground/50" };
  return (
    <span
      data-testid="roaster-link"
      data-status={status ?? "unknown"}
      className="inline-flex items-center gap-1.5 text-xs font-medium text-muted-foreground"
    >
      <span className={cn("size-2 rounded-full", m.dot)} aria-hidden />
      {m.label}
    </span>
  );
}

/** A small drawer over the real diagnostic signals (no invented metrics). */
function DiagnosticsDrawer({
  phase,
  mcpChild,
  firstCrack,
  elapsedSeconds,
}: {
  phase: RoastPhase | null;
  mcpChild?: MCPChildStatus;
  firstCrack: FirstCrackData | null;
  elapsedSeconds: number | null;
}): React.JSX.Element {
  const [open, setOpen] = useState(false);
  return (
    <div className="relative">
      <button
        type="button"
        data-testid="diagnostics-toggle"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className="rounded-md border border-border px-2 py-1 text-xs font-medium text-muted-foreground hover:bg-accent"
      >
        Diagnostics
      </button>
      {open && (
        <div
          data-testid="diagnostics-drawer"
          className="absolute right-0 z-10 mt-2 w-64 rounded-md border border-border bg-popover p-3 text-xs shadow-lg"
        >
          <dl className="flex flex-col gap-1.5">
            <DiagRow label="Phase" value={phase ?? "—"} />
            <DiagRow label="MCP child" value={mcpChild ?? "—"} />
            <DiagRow label="Elapsed" value={formatClock(elapsedSeconds)} />
            <DiagRow
              label="First crack"
              value={firstCrack ? `${firstCrack.source} @ ${formatTempC(firstCrack.bean_temp_c ?? null)}` : "not detected"}
            />
          </dl>
        </div>
      )}
    </div>
  );
}

function DiagRow({ label, value }: { label: string; value: string }): React.JSX.Element {
  return (
    <div className="flex items-center justify-between gap-3">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="numeric font-medium">{value}</dd>
    </div>
  );
}
