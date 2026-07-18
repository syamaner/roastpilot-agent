/**
 * Discard / restore widget (#582).
 *
 * A reversible soft-exclude for a completed roast that produced BAD DATA
 * while the beans themselves were fine (e.g. a detector-missed first crack
 * marked late, so the derived DTR reads bogus). Posts via `api.discardRoast`
 * (`POST /api/roasts/{id}/discard`) / `api.restoreRoast`
 * (`POST /api/roasts/{id}/restore`) — a store soft flag, never a delete: the
 * run's telemetry, decision trace, and any exported audio are untouched.
 *
 * Discard requires an inline confirm step (mirrors the two-step pattern used
 * for other destructive-looking-but-reversible actions in this codebase —
 * `window.confirm` is avoided for testability); restore is a single click
 * since it only ever un-hides a run that is already visible via a direct
 * link, never enabled from the (filtered) history list.
 *
 * On success both actions invalidate the run detail AND the history query —
 * a discard/restore changes which runs the history list shows, matching
 * `RoastedWeight`/`ChargeWeight`'s existing dual-invalidate precedent.
 */

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { roastKeys } from "@/hooks/queries";

export interface RoastDiscardProps {
  runId: string;
  /** Persisted soft-exclude flag (#582). */
  excluded: boolean;
  className?: string;
}

export function RoastDiscard({
  runId,
  excluded,
  className,
}: RoastDiscardProps): React.JSX.Element {
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState(false);

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: roastKeys.detail(runId) });
    // The history list filters excluded=1 runs out entirely (#582), so a
    // discard/restore changes what it shows — invalidate it too, mirroring
    // RoastedWeight/ChargeWeight's own history-invalidate precedent.
    void queryClient.invalidateQueries({ queryKey: roastKeys.history });
  };

  const discardMutation = useMutation({
    mutationFn: () => api.discardRoast(runId),
    onSuccess: () => {
      setConfirming(false);
      invalidate();
    },
  });

  const restoreMutation = useMutation({
    mutationFn: () => api.restoreRoast(runId),
    onSuccess: invalidate,
  });

  if (excluded) {
    return (
      <div
        data-testid="roast-discard"
        className={cn(
          "flex flex-col gap-3 rounded-lg border border-roast-fault/50 bg-card p-4",
          className,
        )}
      >
        <h3 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
          Roast status
        </h3>
        <p data-testid="roast-discard-indicator" className="text-sm text-roast-fault">
          Discarded — excluded from history and the reference corpus.
        </p>
        <div className="flex items-center gap-3">
          <button
            type="button"
            data-testid="roast-restore-button"
            onClick={() => restoreMutation.mutate()}
            disabled={restoreMutation.isPending}
            className="inline-flex items-center rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:cursor-not-allowed disabled:opacity-50"
          >
            {restoreMutation.isPending ? "Restoring…" : "Restore"}
          </button>
          {restoreMutation.isError && (
            <span data-testid="roast-discard-error" className="text-xs text-roast-fault">
              Save failed — try again.
            </span>
          )}
        </div>
      </div>
    );
  }

  return (
    <div
      data-testid="roast-discard"
      className={cn("flex flex-col gap-3 rounded-lg border border-border bg-card p-4", className)}
    >
      <h3 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
        Roast status
      </h3>
      <p className="text-sm text-muted-foreground">
        Bad data (e.g. a missed first crack)? Discard this roast — it stays on record (audio +
        trace kept) but drops out of history and reference retrieval. Reversible.
      </p>
      {confirming ? (
        <div className="flex items-center gap-3">
          <span className="text-sm text-foreground">Discard this roast?</span>
          <button
            type="button"
            data-testid="roast-discard-confirm"
            onClick={() => discardMutation.mutate()}
            disabled={discardMutation.isPending}
            className="inline-flex items-center rounded-md bg-roast-fault px-3 py-1.5 text-sm font-medium text-white disabled:cursor-not-allowed disabled:opacity-50"
          >
            {discardMutation.isPending ? "Discarding…" : "Yes, discard"}
          </button>
          <button
            type="button"
            data-testid="roast-discard-cancel"
            onClick={() => setConfirming(false)}
            disabled={discardMutation.isPending}
            className="inline-flex items-center rounded-md border border-border px-3 py-1.5 text-sm font-medium text-foreground disabled:cursor-not-allowed disabled:opacity-50"
          >
            Cancel
          </button>
          {discardMutation.isError && (
            <span data-testid="roast-discard-error" className="text-xs text-roast-fault">
              Save failed — try again.
            </span>
          )}
        </div>
      ) : (
        <div className="flex items-center gap-3">
          <button
            type="button"
            data-testid="roast-discard-button"
            onClick={() => setConfirming(true)}
            className="inline-flex items-center rounded-md border border-roast-fault/50 px-3 py-1.5 text-sm font-medium text-roast-fault"
          >
            Discard
          </button>
        </div>
      )}
    </div>
  );
}
