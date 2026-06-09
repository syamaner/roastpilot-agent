/**
 * Self-rating widget (E10-S5, ui-prompts Prompt C #5).
 *
 * 1–5 stars + free-text notes + save. Posts via `api.rate` (`POST
 * /api/roasts/{id}/rating`) and invalidates the run detail query so the saved
 * rating re-reads from the server — the SPA renders the server's truth, not local
 * optimistic state. Pre-fills from the persisted `rating` / `notes`.
 */

import { useEffect, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { roastKeys } from "@/hooks/queries";
import type { OperatorRatingRequest } from "@/lib/types";

type Stars = OperatorRatingRequest["stars"];
const STAR_VALUES: Stars[] = [1, 2, 3, 4, 5];

export interface RoastRatingProps {
  runId: string;
  /** Persisted rating (1–5) and notes to pre-fill from. */
  rating: number | null;
  notes: string | null;
  className?: string;
}

export function RoastRating({ runId, rating, notes, className }: RoastRatingProps): React.JSX.Element {
  const queryClient = useQueryClient();
  const [stars, setStars] = useState<Stars | null>(clampStars(rating));
  const [notesDraft, setNotesDraft] = useState(notes ?? "");

  // Re-sync the draft when the persisted values change (e.g. after the query
  // refetches following a save, or when navigating between runs).
  useEffect(() => {
    setStars(clampStars(rating));
    setNotesDraft(notes ?? "");
  }, [rating, notes]);

  const mutation = useMutation({
    mutationFn: (body: OperatorRatingRequest) => api.rate(runId, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: roastKeys.detail(runId) });
    },
  });

  const onSave = () => {
    if (stars === null) return;
    mutation.mutate({ stars, notes: notesDraft.trim() === "" ? null : notesDraft.trim() });
  };

  return (
    <div
      data-testid="roast-rating"
      className={cn("flex flex-col gap-3 rounded-lg border border-border bg-card p-4", className)}
    >
      <h3 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Your rating</h3>

      <div className="flex items-center gap-1" role="radiogroup" aria-label="Star rating">
        {STAR_VALUES.map((value) => {
          const filled = stars !== null && value <= stars;
          return (
            <button
              key={value}
              type="button"
              role="radio"
              aria-checked={stars === value}
              aria-label={`${value} star${value === 1 ? "" : "s"}`}
              data-testid={`star-${value}`}
              data-filled={filled ? "true" : "false"}
              onClick={() => setStars(value)}
              className={cn(
                "text-2xl leading-none transition-colors",
                filled ? "text-roast-caution" : "text-muted-foreground/40 hover:text-muted-foreground",
              )}
            >
              {filled ? "★" : "☆"}
            </button>
          );
        })}
      </div>

      <textarea
        data-testid="rating-notes"
        value={notesDraft}
        onChange={(e) => setNotesDraft(e.target.value)}
        placeholder="Tasting notes (e.g. good body, slightly bright)"
        rows={3}
        className="w-full resize-y rounded-md border border-border bg-input px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground/60 focus:outline-none focus:ring-1 focus:ring-ring"
      />

      <div className="flex items-center gap-3">
        <button
          type="button"
          data-testid="rating-save"
          onClick={onSave}
          disabled={stars === null || mutation.isPending}
          className="inline-flex items-center rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:cursor-not-allowed disabled:opacity-50"
        >
          {mutation.isPending ? "Saving…" : "Save rating"}
        </button>
        {mutation.isError && (
          <span data-testid="rating-error" className="text-xs text-roast-fault">
            Save failed — try again.
          </span>
        )}
        {mutation.isSuccess && !mutation.isPending && (
          <span data-testid="rating-saved" className="text-xs text-roast-nominal">
            Saved.
          </span>
        )}
      </div>
    </div>
  );
}

/** Coerce a persisted rating int into the 1–5 star union, or `null`. */
function clampStars(value: number | null): Stars | null {
  if (value === null) return null;
  const rounded = Math.round(value);
  return rounded >= 1 && rounded <= 5 ? (rounded as Stars) : null;
}
