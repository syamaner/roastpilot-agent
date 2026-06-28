/**
 * Roasted-out weight widget (#388).
 *
 * Operator-entered roasted weight + the derived weight-loss %, the objective
 * partner to the subjective star rating (D42 corpus labels). Posts via
 * `api.setRoastedWeight` (`POST /api/roasts/{id}/roasted-weight`) and invalidates
 * the run detail query so the saved weight + derived % re-read from the server —
 * the SPA renders the server's truth, not local optimistic state. Pre-fills from
 * the persisted `roasted_weight_grams`.
 *
 * "Weight loss %" is predominantly moisture but also dry-matter loss (CO₂,
 * volatiles, chaff), so it is NOT pure water loss — the label says so.
 */

import { useEffect, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { roastKeys } from "@/hooks/queries";
import type { RoastedWeightRequest } from "@/lib/types";

export interface RoastedWeightProps {
  runId: string;
  /** Green/charge weight in grams (the "in" side, `profile.bean_weight_grams`). */
  chargeWeightGrams: number;
  /** Persisted roasted-out weight in grams to pre-fill from, or `null`. */
  roastedWeightGrams: number | null;
  /** Server-derived weight-loss %, or `null` until weighed. */
  weightLossPercent: number | null;
  className?: string;
}

export function RoastedWeight({
  runId,
  chargeWeightGrams,
  roastedWeightGrams,
  weightLossPercent,
  className,
}: RoastedWeightProps): React.JSX.Element {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState(roastedWeightGrams === null ? "" : String(roastedWeightGrams));

  // Re-sync the draft when the persisted value changes (after a save refetch, or
  // when navigating between runs).
  useEffect(() => {
    setDraft(roastedWeightGrams === null ? "" : String(roastedWeightGrams));
  }, [roastedWeightGrams]);

  const mutation = useMutation({
    mutationFn: (body: RoastedWeightRequest) => api.setRoastedWeight(runId, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: roastKeys.detail(runId) });
      // The History list (roastKeys.history) renders the Loss % column (#388), so
      // invalidate it too — otherwise the saved weight_loss_percent stays stale
      // there until the 30s staleTime elapses.
      void queryClient.invalidateQueries({ queryKey: roastKeys.history });
    },
  });

  const parsed = Number.parseFloat(draft);
  // The roasted weight must be positive AND not exceed the charge weight — a
  // value above the charge is a tare/scale error (you can't gain mass roasting),
  // so the API rejects it (409, strictly >) and Save stays disabled. Equal in/out
  // (0% loss) is permitted, matching the API check + models.weight_loss_percent.
  const overCharge = Number.isFinite(parsed) && parsed > chargeWeightGrams;
  const valid = Number.isFinite(parsed) && parsed > 0 && parsed <= chargeWeightGrams;

  const onSave = () => {
    if (!valid) return;
    mutation.mutate({ roasted_weight_grams: parsed });
  };

  return (
    <div
      data-testid="roasted-weight"
      className={cn("flex flex-col gap-3 rounded-lg border border-border bg-card p-4", className)}
    >
      <h3 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
        Roasted weight
      </h3>

      <div className="flex items-center gap-2">
        <span className="text-sm text-muted-foreground">{chargeWeightGrams} g in →</span>
        <input
          type="number"
          inputMode="decimal"
          min={0}
          step="0.1"
          data-testid="roasted-weight-input"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="out"
          className="w-24 rounded-md border border-border bg-input px-2 py-1 text-sm text-foreground placeholder:text-muted-foreground/60 focus:outline-none focus:ring-1 focus:ring-ring"
        />
        <span className="text-sm text-muted-foreground">g out</span>
      </div>

      <p data-testid="weight-loss" className="text-sm">
        {weightLossPercent === null ? (
          <span className="text-muted-foreground">Weight loss %: not yet weighed</span>
        ) : (
          <>
            <span className="font-medium text-foreground">{weightLossPercent.toFixed(1)}%</span>{" "}
            <span className="text-muted-foreground">weight loss (moisture + dry-matter)</span>
          </>
        )}
      </p>

      <div className="flex items-center gap-3">
        <button
          type="button"
          data-testid="roasted-weight-save"
          onClick={onSave}
          disabled={!valid || mutation.isPending}
          className="inline-flex items-center rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:cursor-not-allowed disabled:opacity-50"
        >
          {mutation.isPending ? "Saving…" : "Save weight"}
        </button>
        {overCharge && (
          <span data-testid="roasted-weight-invalid" className="text-xs text-roast-fault">
            Must not exceed the {chargeWeightGrams} g charge.
          </span>
        )}
        {mutation.isError && (
          <span data-testid="roasted-weight-error" className="text-xs text-roast-fault">
            Save failed — try again.
          </span>
        )}
        {mutation.isSuccess && !mutation.isPending && (
          <span data-testid="roasted-weight-saved" className="text-xs text-roast-nominal">
            Saved.
          </span>
        )}
      </div>
    </div>
  );
}
