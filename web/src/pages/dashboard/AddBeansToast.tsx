/**
 * Add-beans toast (ui-prompts Prompt A §6, kickoff §2).
 *
 * Non-blocking guidance: when the roaster reaches the charge zone, a dismissible
 * toast tells the operator they can add beans. It is guidance, not a control —
 * dismissing it changes nothing about the roast (T0 is detected by the controller
 * from MCP, or marked via the action bar). Driven by the real `charge_guidance`
 * event payload. All temperatures Celsius.
 */

import { cn } from "@/lib/cn";
import { formatTempC } from "./format";
import type { ChargeGuidanceData } from "./events";

export interface AddBeansToastProps {
  /** The charge-guidance payload; null until the event fires. */
  guidance: ChargeGuidanceData | null;
  /** Whether the toast is currently shown (the page tracks dismissal). */
  visible: boolean;
  onDismiss: () => void;
  className?: string;
}

export function AddBeansToast({
  guidance,
  visible,
  onDismiss,
  className,
}: AddBeansToastProps): React.JSX.Element | null {
  if (!visible || guidance === null) return null;
  return (
    <div
      data-testid="add-beans-toast"
      role="status"
      className={cn(
        "flex items-center justify-between gap-4 rounded-lg border border-roast-nominal/40 bg-roast-nominal/10 px-4 py-3 text-sm shadow-lg",
        className,
      )}
    >
      <span>
        Charge zone reached ({formatTempC(guidance.guidance_min_c)}–
        {formatTempC(guidance.guidance_max_c)}) — you can add beans.
      </span>
      <button
        type="button"
        data-testid="add-beans-dismiss"
        onClick={onDismiss}
        aria-label="Dismiss"
        className="shrink-0 rounded px-2 py-1 text-xs font-medium text-muted-foreground hover:bg-accent"
      >
        Dismiss
      </button>
    </div>
  );
}
