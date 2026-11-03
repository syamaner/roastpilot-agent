/** D220 missing-safety-record confirmation, rendered only from server authority. */

import { useEffect, useRef, useState } from "react";

import type { InitializeSafetyRecordResult } from "@/lib/types";

export interface InitializeSafetyRecordCardProps {
  /** Exact server projection; false removes and resets this workflow. */
  available: boolean;
  /** Calls only D220's dedicated safety-record route. */
  onInitialize: () => Promise<InitializeSafetyRecordResult>;
  /** Refreshes the server projection after a reported result. */
  onRefreshStatus?: () => Promise<void> | void;
}

/** Render D220's bounded physical confirmation without granting Start authority. */
export function InitializeSafetyRecordCard({
  available,
  onInitialize,
  onRefreshStatus,
}: InitializeSafetyRecordCardProps): React.JSX.Element | null {
  const [confirming, setConfirming] = useState(false);
  const [stopped, setStopped] = useState(false);
  const [empty, setEmpty] = useState(false);
  const [independentStopReady, setIndependentStopReady] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reported, setReported] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const firstCheckRef = useRef<HTMLInputElement>(null);
  const submissionInFlight = useRef(false);

  useEffect(() => {
    if (confirming) firstCheckRef.current?.focus();
  }, [confirming]);

  useEffect(() => {
    setConfirming(false);
    setStopped(false);
    setEmpty(false);
    setIndependentStopReady(false);
    setError(null);
    setReported(false);
  }, [available]);

  if (!available) return null;

  const confirmed = stopped && empty && independentStopReady;

  const submit = async () => {
    if (!confirmed || submissionInFlight.current) return;
    submissionInFlight.current = true;
    setSubmitting(true);
    setError(null);
    try {
      const result = await onInitialize();
      if (result.outcome === "confirmed") {
        setReported(true);
      } else if (result.outcome === "unconfirmed") {
        setError(
          "Safety record is unconfirmed. Check refreshed status before any further attempt.",
        );
      } else {
        setError("Safety record was rejected. Check current status before another attempt.");
      }
    } catch (cause) {
      setError(
        cause instanceof Error
          ? cause.message
          : "Could not initialise the safety record. Check current status and retry.",
      );
    } finally {
      // Each POST consumes a new physical confirmation. Never retain it for a
      // retry, a stale projection, or the separate ordinary D213 clearance.
      setStopped(false);
      setEmpty(false);
      setIndependentStopReady(false);
      submissionInFlight.current = false;
      setSubmitting(false);
    }
  };

  return (
    <section
      data-testid="initialize-safety-record"
      className="mx-auto flex max-w-xl flex-col items-center gap-4 rounded-lg border border-roast-fault/50 bg-roast-fault/10 p-8 text-center"
    >
      <div role="alert" aria-atomic="true" className="flex flex-col gap-3">
        <h2 className="text-lg font-bold uppercase tracking-wide">
          Confirm stopped roaster and initialise safety record
        </h2>
        <p className="text-sm text-muted-foreground">
          The Agent can only record the missing safety record. Starting remains blocked and the
          independent emergency stop stays available separately.
        </p>
      </div>
      <p className="text-xs text-muted-foreground">
        This confirmation does not inspect the Hottop, prove its physical state, or turn heat,
        fan, or cooling on.
      </p>

      {reported ? (
        <div className="flex flex-col items-center gap-3" data-testid="initialize-safety-record-reported">
          <p role="status" className="text-sm text-roast-nominal">
            Safety record initialised. Starting remains blocked until you complete a new ordinary
            restart clearance with a fresh physical confirmation.
          </p>
          <button
            type="button"
            data-testid="initialize-safety-record-refresh-status"
            onClick={() => {
              void onRefreshStatus?.();
            }}
            className="rounded-md border border-border px-4 py-2 text-sm font-medium text-foreground"
          >
            Refresh current status
          </button>
        </div>
      ) : !confirming ? (
        <button
          type="button"
          data-testid="initialize-safety-record-open"
          onClick={() => {
            setError(null);
            setConfirming(true);
          }}
          className="rounded-md border border-roast-fault/50 px-5 py-3 text-sm font-semibold text-roast-fault transition-colors hover:bg-roast-fault/10"
        >
          Review physical safety checks
        </button>
      ) : (
        <div className="flex w-full flex-col gap-3 text-left" data-testid="initialize-safety-record-confirm">
          <label className="flex items-start gap-3 text-sm text-foreground">
            <input
              ref={firstCheckRef}
              type="checkbox"
              data-testid="initialize-safety-record-stopped"
              checked={stopped}
              onChange={(event) => setStopped(event.target.checked)}
              className="mt-0.5 h-4 w-4"
            />
            <span>I physically checked that the Hottop is stopped.</span>
          </label>
          <label className="flex items-start gap-3 text-sm text-foreground">
            <input
              type="checkbox"
              data-testid="initialize-safety-record-empty"
              checked={empty}
              onChange={(event) => setEmpty(event.target.checked)}
              className="mt-0.5 h-4 w-4"
            />
            <span>I physically checked that the Hottop is empty.</span>
          </label>
          <label className="flex items-start gap-3 text-sm text-foreground">
            <input
              type="checkbox"
              data-testid="initialize-safety-record-independent-stop"
              checked={independentStopReady}
              onChange={(event) => setIndependentStopReady(event.target.checked)}
              className="mt-0.5 h-4 w-4"
            />
            <span>The independent emergency stop is engaged or immediately ready.</span>
          </label>
          <div className="flex flex-wrap items-center justify-center gap-2 pt-1">
            <button
              type="button"
              data-testid="initialize-safety-record-submit"
              disabled={!confirmed || submitting}
              onClick={() => void submit()}
              className="rounded-md bg-roast-fault px-4 py-2 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:opacity-50"
            >
              {submitting ? "Initialising…" : "Initialise safety record"}
            </button>
            <button
              type="button"
              data-testid="initialize-safety-record-cancel"
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
            <p role="alert" data-testid="initialize-safety-record-error" className="text-xs text-roast-fault">
              {error}
            </p>
          )}
        </div>
      )}
    </section>
  );
}
