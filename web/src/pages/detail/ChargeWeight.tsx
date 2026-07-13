/**
 * Charge-weight correction widget (#520).
 *
 * Roast 13: the operator charged 255 g but the start form still had the
 * seed-default 250 g, so the loss % displayed against the wrong charge
 * weight. This widget lets the operator correct the physical charge weight
 * AFTER completion without mutating the frozen profile — the controller and
 * advisor genuinely ran with the frozen value, so that snapshot stays
 * untouched; the correction lands in a separate column and drives
 * `weight_loss_percent` in its place.
 *
 * Both the frozen and corrected charge weights are always shown, with which
 * one is driving the displayed percentage made explicit — never a silent
 * swap of the number the operator remembers entering at start.
 *
 * Posts via `api.setChargeWeight` (`POST /api/roasts/{id}/charge-weight`) and
 * invalidates the run detail + history queries so the saved correction and
 * derived % re-read from the server, mirroring `RoastedWeight`'s
 * server-truth-not-optimistic pattern.
 */

import { useEffect, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { roastKeys } from "@/hooks/queries";
import type { ChargeWeightRequest } from "@/lib/types";

export interface ChargeWeightProps {
  runId: string;
  /** The FROZEN charge/green weight in grams (`profile.bean_weight_grams`) —
   *  what the controller/advisor actually ran with. Never mutated. */
  frozenChargeGrams: number;
  /** Persisted corrected charge weight in grams to pre-fill from, or `null`
   *  when never corrected. */
  correctedChargeGrams: number | null;
  /** Persisted roasted-out weight, or `null` when not yet weighed — shown for
   *  context and used for the client-side not-below-roasted hint. */
  roastedWeightGrams: number | null;
  /** Server-derived weight-loss %, computed from the corrected charge when
   *  present, else the frozen charge. */
  weightLossPercent: number | null;
  className?: string;
}

export function ChargeWeight({
  runId,
  frozenChargeGrams,
  correctedChargeGrams,
  roastedWeightGrams,
  weightLossPercent,
  className,
}: ChargeWeightProps): React.JSX.Element {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState(
    correctedChargeGrams === null ? "" : String(correctedChargeGrams),
  );

  // Re-sync the draft when the persisted value changes (after a save refetch, or
  // when navigating between runs).
  useEffect(() => {
    setDraft(correctedChargeGrams === null ? "" : String(correctedChargeGrams));
  }, [correctedChargeGrams]);

  const mutation = useMutation({
    mutationFn: (body: ChargeWeightRequest) => api.setChargeWeight(runId, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: roastKeys.detail(runId) });
      // The History list renders the Loss % column, so invalidate it too —
      // otherwise the corrected weight_loss_percent stays stale there.
      void queryClient.invalidateQueries({ queryKey: roastKeys.history });
    },
  });

  const parsed = Number.parseFloat(draft);
  // A corrected charge below the roasted-out weight is physically impossible
  // (the beans cannot weigh more roasted than green) — the API rejects it
  // (409); mirror the hint client-side too. No roasted weight yet means
  // nothing to bound against.
  const belowRoasted =
    roastedWeightGrams !== null && Number.isFinite(parsed) && parsed < roastedWeightGrams;
  const valid = Number.isFinite(parsed) && parsed > 0 && !belowRoasted;

  const onSave = () => {
    if (!valid) return;
    mutation.mutate({ corrected_charge_grams: parsed });
  };

  // Which charge weight is currently driving the displayed %: the corrected
  // value when present, else the frozen one — never a silent swap.
  const drivingCharge = correctedChargeGrams ?? frozenChargeGrams;

  return (
    <div
      data-testid="charge-weight"
      className={cn("flex flex-col gap-3 rounded-lg border border-border bg-card p-4", className)}
    >
      <h3 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
        Charge weight
      </h3>

      <div className="flex flex-col gap-1 text-sm">
        <p data-testid="charge-weight-frozen" className="text-muted-foreground">
          Roast ran with{" "}
          <span className="font-medium text-foreground">{frozenChargeGrams} g</span> (frozen at
          start).
        </p>
        {correctedChargeGrams !== null && (
          <p data-testid="charge-weight-corrected" className="text-muted-foreground">
            Corrected to{" "}
            <span className="font-medium text-foreground">{correctedChargeGrams} g</span>.
          </p>
        )}
        <p data-testid="charge-weight-driving" className="text-xs text-muted-foreground">
          {drivingCharge} g is driving the weight-loss % below.
        </p>
      </div>

      <div className="flex items-center gap-2">
        <input
          type="number"
          inputMode="decimal"
          min={0}
          step="0.1"
          data-testid="charge-weight-input"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="corrected g"
          className="w-28 rounded-md border border-border bg-input px-2 py-1 text-sm text-foreground placeholder:text-muted-foreground/60 focus:outline-none focus:ring-1 focus:ring-ring"
        />
        <span className="text-sm text-muted-foreground">g charged</span>
      </div>

      <p data-testid="charge-weight-loss" className="text-sm">
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
          data-testid="charge-weight-save"
          onClick={onSave}
          disabled={!valid || mutation.isPending}
          className="inline-flex items-center rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:cursor-not-allowed disabled:opacity-50"
        >
          {mutation.isPending ? "Saving…" : "Save correction"}
        </button>
        {belowRoasted && (
          <span data-testid="charge-weight-invalid" className="text-xs text-roast-fault">
            Must not be below the {roastedWeightGrams} g roasted-out weight.
          </span>
        )}
        {mutation.isError && (
          <span data-testid="charge-weight-error" className="text-xs text-roast-fault">
            Save failed — try again.
          </span>
        )}
        {mutation.isSuccess && !mutation.isPending && (
          <span data-testid="charge-weight-saved" className="text-xs text-roast-nominal">
            Saved.
          </span>
        )}
      </div>
    </div>
  );
}
