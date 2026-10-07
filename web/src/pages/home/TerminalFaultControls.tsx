/**
 * Server-projected controls for a current D212/D213 terminal fault lease.
 *
 * The server resolves the lease. This component never receives or derives a
 * run, session, or device id, and it never manufactures an enabled action.
 */

import { useState } from "react";

import type {
  FaultControls,
  FaultControlsActionResult,
  OperatorAction,
} from "@/lib/types";

const TERMINAL_ACTIONS = [
  "emergency_stop",
  "start_cooling",
  "stop_cooling",
  "drop_beans",
  "stop_cooling_and_acknowledge",
] as const satisfies readonly OperatorAction[];

const ACTION_LABELS: Record<(typeof TERMINAL_ACTIONS)[number], string> = {
  emergency_stop: "Emergency stop",
  start_cooling: "Start cooling",
  stop_cooling: "Stop cooling",
  drop_beans: "Drop beans",
  stop_cooling_and_acknowledge: "Stop cooling and acknowledge",
};

export interface TerminalFaultControlsProps {
  /** Current health projection; absent/malformed values must be handled by the parent. */
  controls: FaultControls;
  /** Calls the server-resolved global terminal-fault action endpoint. */
  onAction: (
    action: OperatorAction,
    confirmation?: true,
  ) => Promise<FaultControlsActionResult>;
}

/** Render server-enumerated terminal controls with an explicit D212 confirmation. */
export function TerminalFaultControls({
  controls,
  onAction,
}: TerminalFaultControlsProps): React.JSX.Element {
  const [confirmingAcknowledgement, setConfirmingAcknowledgement] = useState(false);
  const [pendingAction, setPendingAction] = useState<OperatorAction | null>(null);
  const [result, setResult] = useState<FaultControlsActionResult | null>(null);

  const isUnknown = controls.status === "unknown";
  // UNKNOWN is intentionally narrower than OPEN: even an unexpected wire
  // payload cannot make cooling/drop available while durable lease state is
  // unconfirmed. The one retained software path is the server-admitted e-stop.
  const enabledActions = TERMINAL_ACTIONS.filter(
    (action) =>
      controls.enabled_actions.includes(action) && (!isUnknown || action === "emergency_stop"),
  );

  const submit = async (action: OperatorAction, confirmation?: true) => {
    if (pendingAction !== null) return;
    setPendingAction(action);
    setResult(null);
    try {
      const next = await onAction(action, confirmation);
      setResult(next);
    } catch (cause) {
      setResult({
        action,
        result: "failed",
        reason:
          cause instanceof Error
            ? cause.message
            : "Could not submit the requested fault control.",
      });
    } finally {
      setPendingAction(null);
    }
  };

  return (
    <div
      data-testid="terminal-fault-controls"
      data-status={controls.status}
      className="mx-auto flex max-w-xl flex-col gap-4 rounded-lg border border-roast-fault/50 bg-roast-fault/10 p-6"
    >
      <div className="flex flex-col gap-2">
        <h2 className="text-lg font-bold uppercase tracking-wide text-roast-fault">
          {isUnknown ? "Fault control status unknown" : "Fault controls remain available"}
        </h2>
        <p className="text-sm text-muted-foreground">
          {isUnknown
            ? "The Agent cannot confirm the durable fault-control state. If heat or fan may be active, use the independent physical emergency stop. Only controls explicitly listed by the server can be attempted here."
            : "The historical roast record may be terminal, but the current fault-control lease remains open. Use only the controls listed by the server."}
        </p>
        <p className="text-xs text-muted-foreground">
          This page does not confirm that any physical command completed. Check the server status after an action.
        </p>
      </div>

      {enabledActions.length === 0 ? (
        <p role="alert" data-testid="terminal-fault-controls-unavailable" className="text-sm text-roast-fault">
          No software fault controls are currently confirmed. Use the independent physical emergency
          stop if the roaster may be active, then reload to check current status.
        </p>
      ) : (
        <div className="flex flex-wrap gap-3" data-testid="terminal-fault-controls-actions">
          {enabledActions.map((action) => {
            if (action === "stop_cooling_and_acknowledge") {
              return (
                <div key={action} className="flex flex-col gap-2">
                  {confirmingAcknowledgement && (
                    <p
                      data-testid="terminal-fault-controls-confirmation"
                      className="max-w-xs text-xs text-foreground"
                    >
                      Confirm only when it is safe to end cooling. The server must still verify heat
                      0, main fan 0, and cooling off before it acknowledges the fault.
                    </p>
                  )}
                  <button
                    type="button"
                    data-testid={`terminal-fault-action-${action}`}
                    disabled={pendingAction !== null}
                    onClick={() => {
                      if (!confirmingAcknowledgement) {
                        setConfirmingAcknowledgement(true);
                        return;
                      }
                      void submit(action, true);
                    }}
                    className="rounded-md border border-roast-fault/60 bg-roast-fault/15 px-4 py-2 text-sm font-semibold text-roast-fault transition-colors hover:bg-roast-fault/25 disabled:cursor-not-allowed disabled:opacity-60"
                  >
                    {pendingAction === action
                      ? "Submitting…"
                      : confirmingAcknowledgement
                        ? "Confirm safe to end cooling"
                        : ACTION_LABELS[action]}
                  </button>
                </div>
              );
            }

            return (
              <button
                key={action}
                type="button"
                data-testid={`terminal-fault-action-${action}`}
                disabled={pendingAction !== null}
                onClick={() => void submit(action)}
                className="rounded-md border border-border bg-secondary px-4 py-2 text-sm font-semibold text-secondary-foreground transition-colors hover:bg-accent disabled:cursor-not-allowed disabled:opacity-60"
              >
                {pendingAction === action ? "Submitting…" : ACTION_LABELS[action]}
              </button>
            );
          })}
        </div>
      )}

      {result !== null && (
        <p
          role={result.result === "failed" || result.result === "rejected" ? "alert" : "status"}
          data-testid="terminal-fault-controls-result"
          data-result={result.result}
          className={
            result.result === "failed" || result.result === "rejected"
              ? "text-sm text-roast-fault"
              : "text-sm text-muted-foreground"
          }
        >
          {result.result === "accepted"
            ? "Action admitted. Waiting for the server to confirm the resulting fault-control state."
            : `${ACTION_LABELS[result.action as (typeof TERMINAL_ACTIONS)[number]]}: ${result.reason}`}
        </p>
      )}
    </div>
  );
}
