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

import { cn } from "@/lib/cn";
import type { OperatorAction } from "@/lib/types";
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
}: RecoveryModalProps): React.JSX.Element | null {
  if (!open) return null;
  // Emergency stop is always available (mirrors evaluate_emergency_stop); the
  // rest are gated by the server mirror.
  const enabled = new Set(enabledActions ?? []);

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

        <div className="flex flex-col gap-2">
          <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Select Recovery Action
          </span>
          {RECOVERY_ACTIONS.map(({ action, label, note }) => {
            const isEnabled = action === "emergency_stop" || enabled.has(action);
            return (
              <button
                key={action}
                type="button"
                data-testid={`recovery-${action}`}
                data-enabled={isEnabled ? "true" : "false"}
                disabled={!isEnabled}
                aria-disabled={!isEnabled}
                onClick={() => onAction(action)}
                className={cn(
                  "flex items-center justify-between gap-4 rounded-md border px-4 py-3 text-left transition-colors",
                  isEnabled
                    ? "border-border bg-secondary hover:bg-accent"
                    : "cursor-not-allowed border-border/40 text-muted-foreground/50",
                  action === "emergency_stop" && isEnabled && "border-roast-fault/60 text-roast-fault",
                )}
              >
                <span className="text-sm font-semibold uppercase tracking-wide">{label}</span>
                <span className="text-xs text-muted-foreground">{note}</span>
              </button>
            );
          })}
        </div>
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
