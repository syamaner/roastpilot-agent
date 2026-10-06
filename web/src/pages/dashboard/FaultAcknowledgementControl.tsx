/**
 * Server-confirmed fault acknowledgement control.
 *
 * The control asks the operator to explicitly confirm that ending cooling is
 * appropriate before it queues `acknowledge_fault`. A queued REST response is
 * deliberately only pending: completion and failure are rendered from the
 * corresponding server event.
 */

import { useEffect, useState } from "react";

export type FaultAcknowledgementStatus =
  | { kind: "ready" }
  | { kind: "pending" }
  | { kind: "failed"; reason: string }
  | { kind: "completed" };

export interface FaultAcknowledgementControlProps {
  /** Dispatches the server-authorised acknowledge action after confirmation. */
  onConfirm: () => void;
  /** Server-event-derived acknowledgement state. */
  status: FaultAcknowledgementStatus;
}

/** Render the guarded, server-confirmed fault completion action. */
export function FaultAcknowledgementControl({
  onConfirm,
  status,
}: FaultAcknowledgementControlProps): React.JSX.Element {
  const [confirming, setConfirming] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (status.kind !== "ready") {
      setConfirming(false);
      setSubmitting(false);
    }
  }, [status.kind]);

  if (status.kind === "completed") {
    return (
      <p
        role="status"
        data-testid="fault-acknowledgement-completed"
        className="max-w-xs text-right text-xs font-medium text-foreground"
      >
        Verified cooling is off and fault acknowledged by the server.
      </p>
    );
  }

  if (status.kind === "pending") {
    return (
      <p
        role="status"
        data-testid="fault-acknowledgement-pending"
        className="max-w-xs text-right text-xs text-muted-foreground"
      >
        Acknowledgement is queued. Waiting for the server to confirm cooling is off.
      </p>
    );
  }

  return (
    <div className="flex max-w-xs flex-col items-end gap-2">
      {confirming && (
        <p data-testid="fault-acknowledgement-confirmation" className="text-right text-xs text-foreground">
          Confirm only when it is safe to end cooling.
        </p>
      )}
      <button
        type="button"
        data-testid="fault-acknowledge"
        disabled={submitting}
        onClick={() => {
          if (submitting) return;
          if (confirming) {
            setSubmitting(true);
            onConfirm();
            return;
          }
          setConfirming(true);
        }}
        className="inline-flex items-center rounded-md border border-roast-fault/60 bg-roast-fault/15 px-4 py-2 text-sm font-semibold uppercase tracking-wide text-roast-fault transition-colors hover:bg-roast-fault/25 disabled:cursor-not-allowed disabled:opacity-60"
      >
        {confirming ? "Confirm cooling is safe to end" : "Stop cooling and acknowledge"}
      </button>
      {status.kind === "failed" && (
        <p
          role="alert"
          data-testid="fault-acknowledgement-failed"
          className="max-w-xs text-right text-xs text-roast-fault"
        >
          Acknowledgement was not completed ({status.reason}). Fault controls remain available.
        </p>
      )}
    </div>
  );
}
