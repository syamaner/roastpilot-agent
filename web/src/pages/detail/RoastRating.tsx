/**
 * Self-rating widget (E10-S5, ui-prompts Prompt C #5).
 *
 * #566: the standalone entry UX is gone — the tasting form
 * (`RoastTastings`) is now the primary way a rating gets set (saving a
 * tasting also updates this headline from its stars, see `useSaveRating`).
 * This widget slims to a READ-ONLY headline (stars + the saved note, if any)
 * with an explicit "Edit" affordance for a direct tweak without a tasting
 * event. Posts via the same `api.rate` (`POST /api/roasts/{id}/rating`),
 * which seeds the run-detail cache directly with its response — the SPA
 * renders the server's truth, not local optimistic state, either way.
 *
 * Codex round on #568 (PRRT_kwDOSzMG_c6RdllD): `RoastTastings`' one-gesture
 * save fires a rating POST from an entirely separate `useMutation` instance
 * to this widget's own — nothing serialized the two, so an operator editing
 * "Your rating" directly while a tasting-triggered rating save was in flight
 * elsewhere on the page could last-write-wins clobber one gesture with the
 * other. `useIsMutating` reads the SHARED `ratingMutationKey` across every
 * call site on the page, so this widget can see (and block on) a save
 * in-flight from `RoastTastings` even though it never rendered here.
 *
 * Round 4 (PRRT_kwDOSzMG_c6RflF1): `ratingWriteInFlight` alone only covers
 * the moments a rating mutation is literally PENDING — it is `false` for the
 * whole gap between `RoastTastings`' partial failure and its retry, exactly
 * the window an operator could open THIS widget's Edit, save a direct
 * correction, and have it silently overwritten by `RoastTastings`' next
 * retry (which re-posts whatever it captured on ITS attempt one, unaware
 * this edit ever happened). `usePartialFailureLock` reads the shared signal
 * `RoastTastings` publishes for exactly that window, so Edit stays blocked
 * for its full duration too — not just the narrower in-flight moments.
 * (Round 5: this signal is a plain module-scoped store, not a query-cache
 * entry — see `useSaveRating.ts`'s own doc for why.)
 */

import { useEffect, useState } from "react";
import { useIsMutating } from "@tanstack/react-query";

import { StarGlyphs } from "@/components/shared";
import { cn } from "@/lib/cn";
import type { OperatorRatingRequest } from "@/lib/types";
import { ratingMutationKey, useSaveRating, usePartialFailureLock } from "./useSaveRating";

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
  const mutation = useSaveRating(runId);
  // Any in-flight rating write ANYWHERE on the page (this widget's own edit
  // form, or RoastTastings' one-gesture save) — never just this instance's
  // own `mutation.isPending`, or the two entry points could race (#568).
  const ratingWriteInFlight = useIsMutating({ mutationKey: ratingMutationKey(runId) }) > 0;
  // Round 4: also blocked for the whole span of a RoastTastings partial
  // failure awaiting retry/discard — see the module doc above.
  const tastingPartialFailureOpen = usePartialFailureLock(runId);
  const editBlocked = ratingWriteInFlight || tastingPartialFailureOpen;
  const [editing, setEditing] = useState(false);
  const [stars, setStars] = useState<Stars | null>(clampStars(rating));
  const [notesDraft, setNotesDraft] = useState(notes ?? "");

  // Re-sync the draft when the persisted values change (e.g. after a tasting
  // save updates the headline, or when navigating between runs) — and drop
  // out of edit mode, since the value just landed from the server.
  useEffect(() => {
    setStars(clampStars(rating));
    setNotesDraft(notes ?? "");
    setEditing(false);
  }, [rating, notes]);

  const openEdit = () => {
    // #568 (P3): the mutation object is reused across edit sessions, so a
    // prior attempt's isError/isSuccess would otherwise flash a stale
    // "Save failed"/"Saved." for an attempt that hasn't happened yet.
    mutation.reset();
    setEditing(true);
  };

  const onSave = () => {
    if (stars === null) return;
    mutation.mutate(
      { stars, notes: notesDraft.trim() === "" ? null : notesDraft.trim() },
      { onSuccess: () => setEditing(false) },
    );
  };

  const onCancel = () => {
    mutation.reset();
    setStars(clampStars(rating));
    setNotesDraft(notes ?? "");
    setEditing(false);
  };

  return (
    <div
      data-testid="roast-rating"
      className={cn("flex flex-col gap-3 rounded-lg border border-border bg-card p-4", className)}
    >
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Your rating</h3>
        {!editing && (
          <button
            type="button"
            data-testid="rating-edit"
            onClick={openEdit}
            disabled={editBlocked}
            className="text-xs font-medium text-primary hover:underline disabled:cursor-not-allowed disabled:opacity-50 disabled:no-underline"
          >
            Edit
          </button>
        )}
      </div>
      {!editing && editBlocked && (
        <p data-testid="rating-edit-blocked" className="text-xs text-muted-foreground">
          {tastingPartialFailureOpen
            ? "A tasting save needs a retry before this rating can be edited — resolve it in Tasting notes below (or Start over there)."
            : "A tasting save is updating this rating — try Edit again shortly."}
        </p>
      )}

      {editing ? (
        <>
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
              disabled={stars === null || editBlocked}
              className="inline-flex items-center rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:cursor-not-allowed disabled:opacity-50"
            >
              {mutation.isPending ? "Saving…" : "Save rating"}
            </button>
            <button
              type="button"
              data-testid="rating-cancel"
              onClick={onCancel}
              disabled={mutation.isPending}
              className="text-xs text-muted-foreground hover:underline disabled:cursor-not-allowed disabled:opacity-50"
            >
              Cancel
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
        </>
      ) : (
        <div data-testid="rating-headline" className="flex flex-col gap-1">
          {rating === null ? (
            <p className="text-sm text-muted-foreground">Not yet rated.</p>
          ) : (
            <>
              <StarGlyphs rating={rating} className="text-2xl leading-none text-roast-caution" />
              {notes !== null && notes !== "" && (
                <p className="text-sm text-foreground">{notes}</p>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}

/** Coerce a persisted rating int into the 1–5 star union, or `null`. */
function clampStars(value: number | null): Stars | null {
  if (value === null) return null;
  const rounded = Math.round(value);
  return rounded >= 1 && rounded <= 5 ? (rounded as Stars) : null;
}
