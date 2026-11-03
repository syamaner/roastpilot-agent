/**
 * Operator recovery modal (ui-prompts Prompt B §1, kickoff §2).
 *
 * Shown when the service restarted during an active roast and entered
 * `operator_recovery_required`. A blocking modal over the (still-updating)
 * dashboard. The copy MUST communicate that the system will NOT touch heat or fan
 * until the operator chooses — restart never auto-resumes heat/fan (the core
 * safety invariant, AGENTS.md). Explicit actions: resume monitoring (acknowledge),
 * drop, start cooling, emergency stop.
 *
 * Each action button is gated by the server's `enabledActions` (the permission
 * mirror), never a client matrix; emergency stop is always available. The current
 * hardware readout comes from the live telemetry that keeps flowing under the
 * modal. All temperatures Celsius.
 */

import { useEffect, useRef, useState } from "react";

import { cn } from "@/lib/cn";
import type {
  ClosureOnlyClearanceResult,
  EndRecoveredRunResult,
  OperatorAction,
} from "@/lib/types";
import { formatPercent, formatTempC } from "./format";

export interface RecoveryModalProps {
  /** When false, the modal is not rendered (driven by phase === recovery). */
  open: boolean;
  /** Current hardware readout from the still-flowing telemetry. */
  beanTempC: number | null;
  envTempC: number | null;
  heatPercent: number | null;
  fanPercent: number | null;
  /** Server permission mirror — gates the action buttons. */
  enabledActions: OperatorAction[] | null;
  /** Dispatch an operator action (wired to `api.operatorAction`). */
  onAction: (action: OperatorAction) => void;
  /** A failed health refresh leaves only emergency stop available. */
  healthStale?: boolean;
  /** Server projection for the separate D216 physical-confirmation path. */
  closureOnlyAvailable?: boolean;
  /** Server projection for the dedicated D216 End path. */
  endRecoveredRunAvailable?: boolean;
  /** Submit only the server-owned closure-only confirmation. */
  onRequestClosureOnly?: () => Promise<ClosureOnlyClearanceResult>;
  /** Submit only the server-owned recovered-run End route. */
  onEndRecoveredRun?: () => Promise<EndRecoveredRunResult>;
}

/** The recovery actions, in display order, with copy + a recommendation note. */
const RECOVERY_ACTIONS: {
  action: OperatorAction;
  label: string;
  note: string;
}[] = [
  {
    action: "acknowledge_recovery",
    label: "Resume Monitoring Only",
    note: "Continue without resuming control — heat/fan stay at safe defaults.",
  },
  { action: "drop_beans", label: "Drop Beans Now", note: "Eject beans, end the roast." },
  { action: "start_cooling", label: "Start Cooling", note: "Cut heat, max fan, begin cooldown." },
  { action: "emergency_stop", label: "Emergency Stop", note: "Halt all systems immediately." },
];

export function RecoveryModal({
  open,
  beanTempC,
  envTempC,
  heatPercent,
  fanPercent,
  enabledActions,
  onAction,
  healthStale = false,
  closureOnlyAvailable = false,
  endRecoveredRunAvailable = false,
  onRequestClosureOnly,
  onEndRecoveredRun,
}: RecoveryModalProps): React.JSX.Element | null {
  const [closureConfirmationOpen, setClosureConfirmationOpen] = useState(false);
  const [stopped, setStopped] = useState(false);
  const [empty, setEmpty] = useState(false);
  const [independentStopReady, setIndependentStopReady] = useState(false);
  const [closureSubmitting, setClosureSubmitting] = useState(false);
  const [closureError, setClosureError] = useState<string | null>(null);
  const [closureReported, setClosureReported] = useState(false);
  const [endSubmitting, setEndSubmitting] = useState(false);
  const [endError, setEndError] = useState<string | null>(null);
  const firstClosureCheckRef = useRef<HTMLInputElement>(null);
  const closureSubmissionInFlight = useRef(false);

  useEffect(() => {
    if (closureConfirmationOpen) firstClosureCheckRef.current?.focus();
  }, [closureConfirmationOpen]);

  useEffect(() => {
    if (closureOnlyAvailable) return;
    setClosureConfirmationOpen(false);
    setStopped(false);
    setEmpty(false);
    setIndependentStopReady(false);
    setClosureError(null);
    setClosureReported(false);
  }, [closureOnlyAvailable]);

  useEffect(() => {
    if (open) return;
    setClosureConfirmationOpen(false);
    setStopped(false);
    setEmpty(false);
    setIndependentStopReady(false);
    setClosureError(null);
    setClosureReported(false);
    setEndError(null);
  }, [open]);

  if (!open) return null;
  // Emergency stop is always available (mirrors evaluate_emergency_stop); the
  // rest are gated by the server mirror.
  const enabled = new Set(enabledActions ?? []);
  const closureConfirmed = stopped && empty && independentStopReady;
  const showClosureOnlyConfirmation =
    !healthStale && closureOnlyAvailable && onRequestClosureOnly !== undefined;
  const showRecoveredRunEnd =
    !healthStale && endRecoveredRunAvailable && onEndRecoveredRun !== undefined;

  const submitClosureOnly = async () => {
    if (!closureConfirmed || closureSubmissionInFlight.current || onRequestClosureOnly === undefined) {
      return;
    }
    closureSubmissionInFlight.current = true;
    setClosureSubmitting(true);
    setClosureError(null);
    try {
      const result = await onRequestClosureOnly();
      if (result.outcome === "confirmed") {
        setClosureReported(true);
      } else {
        setClosureError("Closure-only clearance was not confirmed. Check current status and retry.");
      }
    } catch (cause) {
      setClosureError(
        cause instanceof Error
          ? cause.message
          : "Could not record closure-only clearance. Check current status and retry.",
      );
    } finally {
      // This is an explicit physical confirmation for one server admission.
      // Never retain checks for a retry or a later server projection.
      setStopped(false);
      setEmpty(false);
      setIndependentStopReady(false);
      closureSubmissionInFlight.current = false;
      setClosureSubmitting(false);
    }
  };

  const submitRecoveredRunEnd = async () => {
    if (onEndRecoveredRun === undefined || endSubmitting) return;
    setEndSubmitting(true);
    setEndError(null);
    try {
      await onEndRecoveredRun();
    } catch (cause) {
      setEndError(
        cause instanceof Error
          ? cause.message
          : "Could not end the recovered run. Check current status and retry.",
      );
    } finally {
      setEndSubmitting(false);
    }
  };

  return (
    <div
      data-testid="recovery-modal"
      role="dialog"
      aria-modal="true"
      aria-labelledby="recovery-modal-title"
      className="fixed inset-0 z-50 flex items-center justify-center bg-background/70 p-6"
    >
      <div className="flex max-h-full w-full max-w-xl flex-col gap-4 overflow-auto rounded-lg border border-border bg-card p-6 shadow-2xl">
        <header className="flex flex-col gap-1">
          <h2
            id="recovery-modal-title"
            className="text-lg font-bold uppercase tracking-wide text-roast-caution"
          >
            Operator Recovery Required
          </h2>
          <p className="text-sm text-muted-foreground">
            The service restarted during an active roast — manual intervention needed.
          </p>
        </header>

        {/* The core no-auto-resume guarantee (kickoff §2 required copy). */}
        <p
          data-testid="recovery-no-auto-resume"
          className="rounded-md border border-roast-caution/40 bg-roast-caution/10 px-4 py-3 text-sm"
        >
          No auto-resume. The system will not touch heat or fan until you choose an
          action below. Dashboard telemetry continues to update in real time.
        </p>

        <dl className="grid grid-cols-2 gap-x-6 gap-y-2 rounded-md border border-border p-4 text-sm">
          <Readout label="Bean Temp (now)" value={formatTempC(beanTempC)} />
          <Readout label="Environment Temp" value={formatTempC(envTempC)} />
          <Readout label="Heat (locked)" value={formatPercent(heatPercent)} />
          <Readout label="Fan (locked)" value={formatPercent(fanPercent)} />
        </dl>

        {showClosureOnlyConfirmation && (
          <section
            data-testid="closure-only-clearance"
            className="flex flex-col gap-3 rounded-md border border-roast-caution/50 bg-roast-caution/10 p-4"
          >
            <h3 className="text-sm font-semibold uppercase tracking-wide text-roast-caution">
              End recovered run only
            </h3>
            <p className="text-sm text-muted-foreground">
              This can only ask the server to close the recovered run. It cannot resume,
              start, drop, cool, or control the roaster.
            </p>
            {closureReported ? (
              <p role="status" data-testid="closure-only-reported" className="text-sm text-roast-nominal">
                Closure-only clearance was recorded. Wait for the current server status before ending
                the recovered run.
              </p>
            ) : !closureConfirmationOpen ? (
              <button
                type="button"
                data-testid="closure-only-open"
                onClick={() => setClosureConfirmationOpen(true)}
                className="self-start rounded-md border border-roast-caution/50 px-4 py-2 text-sm font-semibold text-roast-caution transition-colors hover:bg-roast-caution/10"
              >
                Review physical safety checks
              </button>
            ) : (
              <div className="flex flex-col gap-3 text-left" data-testid="closure-only-confirm">
                <label className="flex items-start gap-3 text-sm text-foreground">
                  <input
                    ref={firstClosureCheckRef}
                    type="checkbox"
                    data-testid="closure-only-stopped"
                    checked={stopped}
                    onChange={(event) => setStopped(event.target.checked)}
                    className="mt-0.5 h-4 w-4"
                  />
                  <span>I physically checked that the Hottop is stopped.</span>
                </label>
                <label className="flex items-start gap-3 text-sm text-foreground">
                  <input
                    type="checkbox"
                    data-testid="closure-only-empty"
                    checked={empty}
                    onChange={(event) => setEmpty(event.target.checked)}
                    className="mt-0.5 h-4 w-4"
                  />
                  <span>I physically checked that the Hottop is empty.</span>
                </label>
                <label className="flex items-start gap-3 text-sm text-foreground">
                  <input
                    type="checkbox"
                    data-testid="closure-only-independent-stop"
                    checked={independentStopReady}
                    onChange={(event) => setIndependentStopReady(event.target.checked)}
                    className="mt-0.5 h-4 w-4"
                  />
                  <span>The independent emergency stop is engaged or immediately ready.</span>
                </label>
                <div className="flex flex-wrap gap-2">
                  <button
                    type="button"
                    data-testid="closure-only-submit"
                    disabled={!closureConfirmed || closureSubmitting}
                    onClick={() => void submitClosureOnly()}
                    className="rounded-md bg-roast-caution px-4 py-2 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    {closureSubmitting ? "Recording…" : "Record closure-only clearance"}
                  </button>
                  <button
                    type="button"
                    data-testid="closure-only-cancel"
                    disabled={closureSubmitting}
                    onClick={() => {
                      setClosureConfirmationOpen(false);
                      setStopped(false);
                      setEmpty(false);
                      setIndependentStopReady(false);
                      setClosureError(null);
                    }}
                    className="rounded-md border border-border px-4 py-2 text-sm font-medium text-foreground"
                  >
                    Cancel
                  </button>
                </div>
                {closureError !== null && (
                  <p role="alert" data-testid="closure-only-error" className="text-xs text-roast-fault">
                    {closureError}
                  </p>
                )}
              </div>
            )}
          </section>
        )}

        {showRecoveredRunEnd && (
          <section
            data-testid="end-recovered-run"
            className="flex flex-col gap-3 rounded-md border border-roast-fault/50 bg-roast-fault/10 p-4"
          >
            <h3 className="text-sm font-semibold uppercase tracking-wide text-roast-fault">
              Server-authorised recovered-run closure
            </h3>
            <p className="text-sm text-muted-foreground">
              End closes this recovered run only. It does not prove a physical safe state or permit a
              new roast.
            </p>
            <button
              type="button"
              data-testid="end-recovered-run-submit"
              disabled={endSubmitting}
              onClick={() => void submitRecoveredRunEnd()}
              className="self-start rounded-md bg-roast-fault px-4 py-2 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:opacity-50"
            >
              {endSubmitting ? "Ending…" : "End recovered run"}
            </button>
            {endError !== null && (
              <p role="alert" data-testid="end-recovered-run-error" className="text-xs text-roast-fault">
                {endError}
              </p>
            )}
          </section>
        )}

        <div className="flex flex-col gap-2">
          <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Select Recovery Action
          </span>
          {RECOVERY_ACTIONS.filter(
            ({ action }) => action === "emergency_stop" || (!healthStale && enabled.has(action)),
          ).map(({ action, label, note }) => {
            return (
              <button
                key={action}
                type="button"
                data-testid={`recovery-${action}`}
                data-enabled="true"
                onClick={() => onAction(action)}
                className={cn(
                  "flex items-center justify-between gap-4 rounded-md border px-4 py-3 text-left transition-colors",
                  "border-border bg-secondary hover:bg-accent",
                  action === "emergency_stop" && "border-roast-fault/60 text-roast-fault",
                )}
              >
                <span className="text-sm font-semibold uppercase tracking-wide">{label}</span>
                <span className="text-xs text-muted-foreground">{note}</span>
              </button>
            );
          })}
        </div>
        {healthStale && (
          <p data-testid="recovery-health-stale" className="text-xs text-roast-fault">
            Health status is stale. Recovery actions are disabled until it recovers; emergency
            stop remains available.
          </p>
        )}
      </div>
    </div>
  );
}

function Readout({ label, value }: { label: string; value: string }): React.JSX.Element {
  return (
    <div className="flex flex-col">
      <dt className="text-xs uppercase tracking-wide text-muted-foreground">{label}</dt>
      <dd className="numeric text-base font-semibold">{value}</dd>
    </div>
  );
}
