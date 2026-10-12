/**
 * Server-projected controls for a current D212/D213 terminal fault lease.
 *
 * The server resolves the lease. This component never receives or derives a
 * run, session, or device id, and it never manufactures an enabled action.
 */

import { useEffect, useRef, useState } from "react";

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
  /** A current server restart-clearance gate keeps all routine controls disabled. */
  routineControlsDisabled?: boolean;
  /** Calls the server-resolved global terminal-fault action endpoint. */
  onAction: (
    action: OperatorAction,
    confirmation?: true,
    expectedGeneration?: number,
  ) => Promise<FaultControlsActionResult>;
}

/** Render server-enumerated terminal controls with an explicit D212 confirmation. */
export function TerminalFaultControls({
  controls,
  routineControlsDisabled = false,
  onAction,
}: TerminalFaultControlsProps): React.JSX.Element {
  const leaseKey = `${controls.status}:${controls.generation ?? "none"}`;
  const [confirmationLeaseKey, setConfirmationLeaseKey] = useState<string | null>(null);
  const [emergencyStopArmed, setEmergencyStopArmed] = useState(false);
  const [pendingOrdinaryAction, setPendingOrdinaryAction] = useState<{
    action: OperatorAction;
    leaseKey: string;
    requestId: number;
  } | null>(null);
  const [pendingEmergencyRequestIds, setPendingEmergencyRequestIds] = useState<number[]>([]);
  const [result, setResult] = useState<{
    leaseKey: string;
    value: FaultControlsActionResult;
  } | null>(null);
  const latestRequestId = useRef(0);
  const currentLeaseKey = useRef(leaseKey);
  const acknowledgementButtonRef = useRef<HTMLButtonElement>(null);
  currentLeaseKey.current = leaseKey;

  // The fault-controls lease is authoritative. Confirmation and admission text
  // from a previous lease are never meaningful for its successor.
  useEffect(() => {
    setConfirmationLeaseKey(null);
    setEmergencyStopArmed(false);
    setPendingOrdinaryAction(null);
    // An emergency-stop result describes the request that the operator just
    // made, not hardware completion. Retain it across the next authoritative
    // lease so a health refresh cannot erase a failed/unconfirmed request.
    setResult((current) =>
      current?.value.action === "emergency_stop" ? current : null,
    );
  }, [leaseKey]);

  const isUnknown = controls.status === "unknown";
  // UNKNOWN is intentionally narrower than OPEN: even an unexpected wire
  // payload cannot make cooling/drop available while durable lease state is
  // unconfirmed. The one retained software path is the server-admitted e-stop.
  const enabledActions = TERMINAL_ACTIONS.filter(
    (action) =>
      controls.enabled_actions.includes(action) && (!isUnknown || action === "emergency_stop"),
  );

  const submit = async (
    action: OperatorAction,
    confirmation?: true,
    expectedGeneration?: number,
  ) => {
    const isEmergencyStop = action === "emergency_stop";
    if (
      !isEmergencyStop &&
      (pendingOrdinaryAction !== null || pendingEmergencyRequestIds.length > 0)
    ) {
      return;
    }

    const requestId = latestRequestId.current + 1;
    latestRequestId.current = requestId;
    const requestLeaseKey = leaseKey;
    if (isEmergencyStop) {
      // A D212 confirmation is specific to this lease. An e-stop attempt
      // immediately disarms it and blocks D212 until the request settles (or
      // the server publishes a new lease).
      setEmergencyStopArmed(false);
      setConfirmationLeaseKey(null);
      setPendingEmergencyRequestIds((current) => [...current, requestId]);
    } else {
      setPendingOrdinaryAction({ action, leaseKey: requestLeaseKey, requestId });
    }
    setResult(null);
    try {
      const next =
        expectedGeneration === undefined
          ? await onAction(action, confirmation)
          : await onAction(action, confirmation, expectedGeneration);
      if (
        latestRequestId.current === requestId &&
        (isEmergencyStop || currentLeaseKey.current === requestLeaseKey)
      ) {
        setResult({ leaseKey: requestLeaseKey, value: next });
      }
    } catch (cause) {
      const failedResult: FaultControlsActionResult = {
        action,
        result: "failed",
        reason:
          cause instanceof Error
            ? cause.message
            : "Could not submit the requested fault control.",
        fault_controls: controls,
      };
      if (
        latestRequestId.current === requestId &&
        (isEmergencyStop || currentLeaseKey.current === requestLeaseKey)
      ) {
        setResult({ leaseKey: requestLeaseKey, value: failedResult });
      }
    } finally {
      if (isEmergencyStop) {
        setPendingEmergencyRequestIds((current) =>
          current.filter((pendingRequestId) => pendingRequestId !== requestId),
        );
      } else {
        setPendingOrdinaryAction((current) =>
          current?.requestId === requestId ? null : current,
        );
        setConfirmationLeaseKey((current) => (current === requestLeaseKey ? null : current));
      }
    }
  };

  const confirmingAcknowledgement = confirmationLeaseKey === leaseKey;
  const currentResult =
    result === null
      ? null
      : result.value.action === "emergency_stop" || result.leaseKey === leaseKey
        ? result.value
        : null;
  const hasPendingEmergencyStop = pendingEmergencyRequestIds.length > 0;
  const acknowledgementGeneration =
    typeof controls.generation === "number" &&
    Number.isSafeInteger(controls.generation) &&
    controls.generation > 0
      ? controls.generation
      : null;

  return (
    <div
      data-testid="terminal-fault-controls"
      data-status={controls.status}
      data-lease-generation={controls.generation ?? "none"}
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
            if (action === "emergency_stop") {
              return (
                <div key={action} className="flex w-full flex-col gap-2">
                  <button
                    type="button"
                    data-testid="terminal-fault-action-emergency_stop"
                    data-armed={emergencyStopArmed ? "true" : "false"}
                    aria-pressed={emergencyStopArmed}
                    aria-describedby={
                      emergencyStopArmed
                        ? "terminal-fault-controls-emergency-stop-confirmation"
                        : hasPendingEmergencyStop
                          ? "terminal-fault-controls-emergency-stop-pending"
                          : undefined
                    }
                    onBlur={() => setEmergencyStopArmed(false)}
                    onClick={() => {
                      if (!emergencyStopArmed) {
                        setEmergencyStopArmed(true);
                        return;
                      }
                      void submit(action);
                    }}
                    className={
                      emergencyStopArmed
                        ? "w-full rounded-md border border-roast-fault bg-roast-fault px-4 py-3 text-sm font-bold uppercase tracking-wide text-white transition-colors"
                        : "w-full rounded-md border border-roast-fault/70 bg-roast-fault/20 px-4 py-3 text-sm font-bold uppercase tracking-wide text-roast-fault transition-colors hover:bg-roast-fault/30"
                    }
                  >
                    {emergencyStopArmed ? "Confirm emergency stop" : "Emergency stop"}
                  </button>
                  {emergencyStopArmed && (
                    <p
                      id="terminal-fault-controls-emergency-stop-confirmation"
                      role="status"
                      aria-live="polite"
                      data-testid="terminal-fault-controls-emergency-stop-confirmation"
                      className="text-xs font-medium text-roast-fault"
                    >
                      Press Confirm emergency stop to submit the server-resolved software request.
                      Use the independent physical emergency stop if the Hottop may be active.
                    </p>
                  )}
                  {hasPendingEmergencyStop && (
                    <p
                      id="terminal-fault-controls-emergency-stop-pending"
                      role="status"
                      aria-live="polite"
                      data-testid="terminal-fault-controls-emergency-stop-pending"
                      className="text-xs text-roast-fault"
                    >
                      A software emergency-stop request is pending. It is not physical proof; use the
                      independent physical emergency stop if the Hottop may be active.
                    </p>
                  )}
                </div>
              );
            }

            if (action === "stop_cooling_and_acknowledge") {
              return (
                <div key={action} className="flex flex-col gap-2">
                  {confirmingAcknowledgement && (
                    <p
                      id="terminal-fault-controls-confirmation"
                      data-testid="terminal-fault-controls-confirmation"
                      role="status"
                      aria-live="polite"
                      className="max-w-xs text-xs text-foreground"
                    >
                      Confirm only when it is safe to end cooling. The server must still verify heat
                      0, main fan 0, and cooling off before it acknowledges the fault.
                    </p>
                  )}
                  <button
                    type="button"
                    ref={acknowledgementButtonRef}
                    data-testid={`terminal-fault-action-${action}`}
                    aria-describedby={
                      pendingOrdinaryAction?.action === action
                          ? "terminal-fault-controls-acknowledgement-pending"
                          : confirmingAcknowledgement
                            ? "terminal-fault-controls-confirmation"
                            : undefined
                    }
                    disabled={
                      pendingOrdinaryAction !== null ||
                      hasPendingEmergencyStop ||
                      routineControlsDisabled ||
                      acknowledgementGeneration === null
                    }
                    onClick={() => {
                      if (
                        hasPendingEmergencyStop ||
                        routineControlsDisabled ||
                        acknowledgementGeneration === null
                      ) {
                        return;
                      }
                      if (!confirmingAcknowledgement) {
                        setConfirmationLeaseKey(leaseKey);
                        return;
                      }
                      void submit(action, true, acknowledgementGeneration);
                    }}
                    className="rounded-md border border-roast-fault/60 bg-roast-fault/15 px-4 py-2 text-sm font-semibold text-roast-fault transition-colors hover:bg-roast-fault/25 disabled:cursor-not-allowed disabled:opacity-60"
                  >
                    {pendingOrdinaryAction?.action === action
                      ? "Submitting…"
                      : confirmingAcknowledgement
                        ? "Confirm safe to end cooling"
                        : ACTION_LABELS[action]}
                  </button>
                  {confirmingAcknowledgement && (
                    <button
                      type="button"
                      data-testid="terminal-fault-controls-confirmation-cancel"
                      onClick={() => {
                        setConfirmationLeaseKey(null);
                        acknowledgementButtonRef.current?.focus();
                      }}
                      className="self-start text-xs font-medium text-muted-foreground underline underline-offset-2"
                    >
                      Cancel confirmation
                    </button>
                  )}
                  {pendingOrdinaryAction?.action === action && (
                    <p
                      id="terminal-fault-controls-acknowledgement-pending"
                      role="status"
                      aria-live="polite"
                      data-testid="terminal-fault-controls-acknowledgement-pending"
                      className="max-w-xs text-xs text-muted-foreground"
                    >
                      The stop-cooling acknowledgement request is pending. The server must still
                      verify heat 0, main fan 0, and cooling off.
                    </p>
                  )}
                </div>
              );
            }

            return (
              <button
                key={action}
                type="button"
                data-testid={`terminal-fault-action-${action}`}
                disabled={
                  pendingOrdinaryAction !== null ||
                  hasPendingEmergencyStop ||
                  routineControlsDisabled
                }
                onClick={() => {
                  if (hasPendingEmergencyStop || routineControlsDisabled) return;
                  void submit(action);
                }}
                className="rounded-md border border-border bg-secondary px-4 py-2 text-sm font-semibold text-secondary-foreground transition-colors hover:bg-accent disabled:cursor-not-allowed disabled:opacity-60"
              >
                {pendingOrdinaryAction?.action === action ? "Submitting…" : ACTION_LABELS[action]}
              </button>
            );
          })}
        </div>
      )}

      {routineControlsDisabled && (
        <p
          role="status"
          data-testid="terminal-fault-controls-restart-clearance-required"
          className="text-xs text-roast-fault"
        >
          Restart clearance remains required. Routine controls stay disabled until the server
          confirms clearance; emergency stop remains available.
        </p>
      )}

      {enabledActions.includes("stop_cooling_and_acknowledge") &&
        acknowledgementGeneration === null && (
          <p
            role="alert"
            data-testid="terminal-fault-controls-generation-required"
            className="text-xs text-roast-fault"
          >
            The server has not supplied a valid positive lease generation for this confirmation.
            Do not retry D212 until current server status provides one.
          </p>
        )}

      {currentResult !== null && (
        <p
          role={
            currentResult.result === "failed" || currentResult.result === "rejected"
              ? "alert"
              : "status"
          }
          data-testid="terminal-fault-controls-result"
          data-result={currentResult.result}
          className={
            currentResult.result === "failed" || currentResult.result === "rejected"
              ? "text-sm text-roast-fault"
              : "text-sm text-muted-foreground"
          }
        >
          {currentResult.action === "emergency_stop"
            ? currentResult.result === "confirmed"
              ? "Emergency-stop software readback was confirmed by the server. This typed software result is not physical proof; inspect the Hottop and use the independent physical emergency stop if it may still be active."
              : currentResult.result === "accepted"
              ? "Emergency-stop request accepted for the lease at request time. This is request-scoped historical status, not hardware completion. Check current server status and use the independent physical emergency stop if the roaster may still be active."
              : `Emergency-stop request result (historical and request-scoped): ${currentResult.reason} Software emergency stop was not confirmed; use the independent physical emergency stop if the roaster may be active, then check current server status.`
            : currentResult.result === "accepted"
              ? "Action admitted. Completion is not confirmed. This terminal view has no later execution outcome; check the current server status and use the independent physical emergency stop if the roaster may still be active."
              : `${ACTION_LABELS[currentResult.action as (typeof TERMINAL_ACTIONS)[number]]}: ${currentResult.reason}`}
        </p>
      )}
    </div>
  );
}
