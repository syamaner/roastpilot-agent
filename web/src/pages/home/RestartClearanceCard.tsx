/**
 * Fresh-process restart-clearance confirmation (D213).
 *
 * This component records only the operator's confirmation. It does not read
 * roaster state, assert physical proof, or issue an MCP command. The parent
 * replaces it only after a fresh server `/health` projection permits Start.
 */

import { useEffect, useRef, useState } from "react";

import type { RestartClearanceResult } from "@/lib/types";

export interface RestartClearanceCardProps {
  /** Records the three-part physical confirmation for this Agent process. */
  onAcknowledge: () => Promise<RestartClearanceResult>;
}

/** Render the mandatory, explicit restart-clearance gate. */
export function RestartClearanceCard({
  onAcknowledge,
}: RestartClearanceCardProps): React.JSX.Element {
  const [confirming, setConfirming] = useState(false);
  const [stopped, setStopped] = useState(false);
  const [empty, setEmpty] = useState(false);
  const [independentStopReady, setIndependentStopReady] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const firstCheckRef = useRef<HTMLInputElement>(null);
  const submissionInFlight = useRef(false);

  useEffect(() => {
    if (confirming) firstCheckRef.current?.focus();
  }, [confirming]);

  const confirmed = stopped && empty && independentStopReady;

  const submit = async () => {
    if (!confirmed || submissionInFlight.current) return;
    submissionInFlight.current = true;
    setSubmitting(true);
    setError(null);
    try {
      await onAcknowledge();
    } catch (cause) {
      setError(
        cause instanceof Error
          ? cause.message
          : "Could not record the restart clearance. Check current status and retry.",
      );
    } finally {
      submissionInFlight.current = false;
      setSubmitting(false);
    }
  };

  return (
    <div
      className="mx-auto flex max-w-xl flex-col items-center gap-4 rounded-lg border border-roast-fault/50 bg-roast-fault/10 p-8 text-center"
      data-testid="restart-clearance-required"
    >
      <div role="alert" aria-atomic="true" className="flex flex-col gap-3">
        <h2 className="text-lg font-bold uppercase tracking-wide">Restart clearance required</h2>
        <p className="text-sm text-muted-foreground">
          This Agent process has not received a fresh operator clearance. Starting remains
          blocked until you complete the checks below.
        </p>
      </div>
      <p className="text-xs text-muted-foreground">
        Recording this confirmation does not inspect the Hottop, prove its physical state, or
        turn heat, fan, or cooling on.
      </p>

      {!confirming ? (
        <button
          type="button"
          data-testid="restart-clearance-open"
          onClick={() => setConfirming(true)}
          className="rounded-md border border-roast-fault/50 px-5 py-3 text-sm font-semibold text-roast-fault transition-colors hover:bg-roast-fault/10"
        >
          Review physical safety checks
        </button>
      ) : (
        <div className="flex w-full flex-col gap-3 text-left" data-testid="restart-clearance-confirm">
          <label className="flex items-start gap-3 text-sm text-foreground">
            <input
              ref={firstCheckRef}
              type="checkbox"
              data-testid="restart-clearance-stopped"
              checked={stopped}
              onChange={(event) => setStopped(event.target.checked)}
              className="mt-0.5 h-4 w-4"
            />
            <span>I physically checked that the Hottop is stopped.</span>
          </label>
          <label className="flex items-start gap-3 text-sm text-foreground">
            <input
              type="checkbox"
              data-testid="restart-clearance-empty"
              checked={empty}
              onChange={(event) => setEmpty(event.target.checked)}
              className="mt-0.5 h-4 w-4"
            />
            <span>I physically checked that the Hottop is empty.</span>
          </label>
          <label className="flex items-start gap-3 text-sm text-foreground">
            <input
              type="checkbox"
              data-testid="restart-clearance-independent-stop"
              checked={independentStopReady}
              onChange={(event) => setIndependentStopReady(event.target.checked)}
              className="mt-0.5 h-4 w-4"
            />
            <span>The independent emergency stop is engaged or immediately ready.</span>
          </label>
          <div className="flex flex-wrap items-center justify-center gap-2 pt-1">
            <button
              type="button"
              data-testid="restart-clearance-submit"
              disabled={!confirmed || submitting}
              onClick={() => void submit()}
              className="rounded-md bg-roast-fault px-4 py-2 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:opacity-50"
            >
              {submitting ? "Recording…" : "Record restart clearance"}
            </button>
            <button
              type="button"
              data-testid="restart-clearance-cancel"
              disabled={submitting}
              onClick={() => {
                setConfirming(false);
                setStopped(false);
                setEmpty(false);
                setIndependentStopReady(false);
                setError(null);
              }}
              className="rounded-md border border-border px-4 py-2 text-sm font-medium text-foreground"
            >
              Cancel
            </button>
          </div>
          {error !== null && (
            <p role="alert" data-testid="restart-clearance-error" className="text-xs text-roast-fault">
              {error}
            </p>
          )}
        </div>
      )}
    </div>
  );
}
