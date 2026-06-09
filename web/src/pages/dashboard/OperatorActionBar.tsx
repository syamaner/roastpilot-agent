/**
 * Operator action bar (ui-prompts Prompt A §5, kickoff §2 / §3).
 *
 * INVARIANT — enablement mirrors SERVER state, never a hardcoded client matrix.
 * Each non-e-stop button is enabled iff its action is in `enabledActions` (the
 * D25 permission mirror the server derives read-only from the command×phase
 * matrix, carried on the snapshot + every `phase_changed`). The bar NEVER decides
 * for itself what is valid in a phase.
 *
 * `enabledActions` is a PERMISSION mirror, not a render list: `pause_advisory` /
 * `resume_advisory` are enabled in EVERY phase (the controller never gates them).
 * On a TERMINAL roast (complete/faulted) those toggles are permitted-but-
 * meaningless, so this page layer hides them — a presentation decision that does
 * not make the contract lie. Emergency stop is ALWAYS enabled and guarded by a
 * confirm-press (two-step), mirroring the prototype's guarded e-stop.
 *
 * Actions POST through the typed REST client; a rejected/failed result surfaces
 * the server's typed reason (we never pre-judge validity client-side).
 */

import { useState } from "react";

import { cn } from "@/lib/cn";
import type { OperatorAction, RoastPhase } from "@/lib/types";

/** The non-e-stop actions the bar offers, in display order, with their labels.
 *  Whether each renders/enables is decided ENTIRELY by `enabledActions` + the
 *  terminal-phase presentation rule — this list is layout, not a phase matrix. */
const ACTION_BUTTONS: { action: OperatorAction; label: string }[] = [
  { action: "drop_beans", label: "DROP BEANS" },
  { action: "mark_first_crack", label: "MARK FIRST CRACK" },
  { action: "mark_beans_added", label: "MARK BEANS ADDED" },
  { action: "pause_advisory", label: "PAUSE ADVISOR" },
  { action: "resume_advisory", label: "RESUME ADVISOR" },
  { action: "start_cooling", label: "START COOLING" },
  { action: "stop_cooling", label: "STOP COOLING" },
];

/** Phases where the advisory toggles are permitted-but-meaningless and hidden. */
const TERMINAL_PHASES: ReadonlySet<RoastPhase> = new Set<RoastPhase>([
  "complete",
  "faulted",
]);

/** Actions hidden (not just disabled) on terminal phases — the presentation call. */
const HIDE_ON_TERMINAL: ReadonlySet<OperatorAction> = new Set<OperatorAction>([
  "pause_advisory",
  "resume_advisory",
]);

export interface OperatorActionResultView {
  action: OperatorAction;
  result: "accepted" | "rejected" | "failed";
  reason: string;
}

export interface OperatorActionBarProps {
  /** Server-provided permission mirror; `null` before the first snapshot. */
  enabledActions: OperatorAction[] | null;
  /** Server-authoritative phase (drives the terminal-phase presentation rule). */
  phase: RoastPhase | null;
  /** Dispatch an action (the page wires this to `api.operatorAction`). */
  onAction: (action: OperatorAction) => void;
  /** The most recent action outcome — its reason is surfaced inline. */
  lastResult?: OperatorActionResultView | null;
}

export function OperatorActionBar({
  enabledActions,
  phase,
  onAction,
  lastResult,
}: OperatorActionBarProps): React.JSX.Element {
  // Two-step guard for emergency stop: the first press arms, the second fires.
  const [estopArmed, setEstopArmed] = useState(false);
  const enabled = new Set(enabledActions ?? []);
  const isTerminal = phase !== null && TERMINAL_PHASES.has(phase);

  const visibleButtons = ACTION_BUTTONS.filter(
    (b) => !(isTerminal && HIDE_ON_TERMINAL.has(b.action)),
  );

  const fireEstop = () => {
    if (!estopArmed) {
      setEstopArmed(true);
      return;
    }
    setEstopArmed(false);
    onAction("emergency_stop");
  };

  return (
    <div
      data-testid="operator-action-bar"
      className="flex flex-wrap items-center gap-3 border-t border-border bg-card/60 px-6 py-4"
    >
      {/* Emergency stop — always enabled, confirm-press guarded (kickoff §2). */}
      <button
        type="button"
        data-testid="action-emergency_stop"
        data-armed={estopArmed ? "true" : "false"}
        onClick={fireEstop}
        onBlur={() => setEstopArmed(false)}
        className={cn(
          "inline-flex items-center gap-2 rounded-md border px-4 py-2 text-sm font-semibold uppercase tracking-wide transition-colors",
          estopArmed
            ? "border-roast-fault bg-roast-fault text-white"
            : "border-roast-fault/60 bg-roast-fault/15 text-roast-fault hover:bg-roast-fault/25",
        )}
      >
        {estopArmed ? "CONFIRM EMERGENCY STOP" : "EMERGENCY STOP"}
      </button>

      {visibleButtons.map(({ action, label }) => {
        const isEnabled = enabled.has(action);
        return (
          <button
            key={action}
            type="button"
            data-testid={`action-${action}`}
            data-enabled={isEnabled ? "true" : "false"}
            disabled={!isEnabled}
            aria-disabled={!isEnabled}
            onClick={() => onAction(action)}
            className={cn(
              "inline-flex items-center gap-2 rounded-md border px-3 py-2 text-sm font-medium uppercase tracking-wide transition-colors",
              isEnabled
                ? "border-border bg-secondary text-secondary-foreground hover:bg-accent"
                : "cursor-not-allowed border-border/40 bg-transparent text-muted-foreground/50",
            )}
          >
            {label}
          </button>
        );
      })}

      {/* Typed result feedback — the server's reason, never a client verdict. */}
      {lastResult && lastResult.result !== "accepted" && (
        <span
          data-testid="action-result"
          data-result={lastResult.result}
          className="ml-auto text-xs font-medium text-roast-fault"
        >
          {lastResult.action.replace(/_/g, " ")} {lastResult.result}: {lastResult.reason}
        </span>
      )}
    </div>
  );
}
