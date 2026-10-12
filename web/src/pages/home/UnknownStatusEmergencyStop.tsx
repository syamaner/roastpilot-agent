/**
 * Server-resolved software emergency stop for views whose health projection is
 * unknown. This component deliberately receives no cached health, lease, run,
 * or device data: the endpoint resolves its current state on the server.
 */

import { useState } from "react";

import { api } from "@/lib/api";

export interface UnknownStatusEmergencyStopProps {
  /** Unique test-id prefix for the owning unknown-status surface. */
  testIdPrefix: string;
}

/** Render a guarded software emergency-stop request with nonphysical outcome copy. */
export function UnknownStatusEmergencyStop({
  testIdPrefix,
}: UnknownStatusEmergencyStopProps): React.JSX.Element {
  const [armed, setArmed] = useState(false);
  const [pending, setPending] = useState(false);
  const [result, setResult] = useState<string | null>(null);
  const confirmationId = `${testIdPrefix}-confirmation`;
  const pendingId = `${testIdPrefix}-pending`;

  const submit = async () => {
    if (pending) return;
    setArmed(false);
    setPending(true);
    setResult(null);
    try {
      const response = await api.faultControlsAction({ action: "emergency_stop" });
      const prefix =
        response.result === "confirmed"
          ? "The server reported a software emergency-stop readback for this request."
          : response.result === "accepted"
            ? "The server accepted this emergency-stop request."
            : `The server reported this emergency-stop request as ${response.result}: ${response.reason}.`;
      setResult(
        `${prefix} This request-scoped result does not prove the Hottop stopped; inspect it and use the independent physical emergency stop if it may be active.`,
      );
    } catch (error) {
      setResult(
        `${error instanceof Error ? error.message : "The software emergency-stop request could not be submitted."} Use the independent physical emergency stop if the Hottop may be active.`,
      );
    } finally {
      setPending(false);
    }
  };

  return (
    <div
      className="flex w-full max-w-md flex-col items-center gap-2 rounded-md border border-roast-fault/50 bg-roast-fault/10 p-3"
      data-testid={testIdPrefix}
    >
      <p className="text-xs text-muted-foreground">
        If the Hottop may be active, use the independent physical emergency stop immediately.
        This software request cannot confirm physical state here.
      </p>
      <button
        type="button"
        data-testid={`${testIdPrefix}-submit`}
        data-armed={armed ? "true" : "false"}
        aria-pressed={armed}
        aria-describedby={armed ? confirmationId : pending ? pendingId : undefined}
        disabled={pending}
        onBlur={() => setArmed(false)}
        onClick={() => {
          if (!armed) {
            setArmed(true);
            return;
          }
          void submit();
        }}
        className={
          armed
            ? "w-full rounded-md border border-roast-fault bg-roast-fault px-4 py-3 text-sm font-bold uppercase tracking-wide text-white transition-colors disabled:cursor-not-allowed disabled:opacity-60"
            : "w-full rounded-md border border-roast-fault/70 bg-roast-fault/20 px-4 py-3 text-sm font-bold uppercase tracking-wide text-roast-fault transition-colors hover:bg-roast-fault/30 disabled:cursor-not-allowed disabled:opacity-60"
        }
      >
        {pending
          ? "Submitting emergency stop…"
          : armed
            ? "Confirm emergency stop"
            : "Software emergency stop"}
      </button>
      {armed && (
        <p
          id={confirmationId}
          role="status"
          aria-live="polite"
          data-testid={`${testIdPrefix}-confirmation`}
          className="text-xs font-medium text-roast-fault"
        >
          Press Confirm emergency stop to submit the server-resolved request.
        </p>
      )}
      {pending && (
        <p
          id={pendingId}
          role="status"
          aria-live="polite"
          data-testid={`${testIdPrefix}-pending`}
          className="text-xs text-roast-fault"
        >
          The software emergency-stop request is pending. Use the independent physical emergency
          stop if the Hottop may be active.
        </p>
      )}
      {result !== null && (
        <p
          role="status"
          aria-live="polite"
          data-testid={`${testIdPrefix}-result`}
          className="text-xs text-muted-foreground"
        >
          {result}
        </p>
      )}
    </div>
  );
}
