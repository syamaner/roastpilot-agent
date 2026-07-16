/**
 * Structured tasting entries (#522, D91).
 *
 * D91 (product-agents plan §5): "once we have enough data, we move towards ML
 * — so we should have the plans for that and ensure we capture valuable
 * signals." Every roast without a tasting is a lost label — this widget is
 * the entry point.
 *
 * Multiple tastings per roast (a revisit is an ADDITIONAL entry, never an
 * overwrite — the roast-13 "flat" same-evening -> "grassy" hours-later
 * refinement is exactly this signal shape) render as a list above the form.
 * Posts via `api.addTasting` (`POST /api/roasts/{id}/tastings`), which always
 * appends and returns the full updated list. Entry friction stays near zero:
 * every field beyond stars is optional, and the form resets to a fresh blank
 * entry after each save (never pre-fills from a past tasting — that would
 * invite an accidental overwrite-looking edit of history).
 *
 * #533 (Codex P3 from #527 round 4): every input is disabled while a save is
 * in flight (either mutation pending) — not just the save button. Previously
 * only the button was disabled, so an operator who kept typing during a slow
 * save had that draft silently wiped when `onSuccess` unconditionally called
 * `resetForm()`. This form has no persisted value to re-sync a draft FROM
 * (unlike `RoastRating`, which re-derives its draft from the server's
 * `rating`/`notes` props on every change and so never had this exposure) —
 * multiple tastings accumulate as a list, there is no single "the" tasting
 * to pre-fill a re-sync effect from. Disabling the whole form while pending
 * is therefore the simplest honest fix available here: the operator sees
 * the "Saving…" state and cannot lose a draft to a race they have no way
 * to observe.
 *
 * #566: this is now the PRIMARY entry point for a rating too — saving a
 * tasting fires both `useAddTasting` (the tasting corpus) and `useSaveRating`
 * (the headline rating, `RoastRating`) from one gesture, seeded with the
 * tasting's own stars. Both mutations run concurrently; a partial failure
 * (one succeeds, one doesn't) is surfaced honestly rather than reported as a
 * blanket "Saved." — a `useEffect` keyed on both mutations SETTLING resets
 * the form and shows "Saved." only once BOTH succeed (#568 Codex round 1:
 * resetting from the tasting mutation's own `onSuccess` alone would clear
 * the star rating + notes draft even when the rating write went on to fail).
 * Each failure names which record didn't save, and a retry resubmits ONLY
 * the record that actually failed — never the one that already succeeded
 * (round 2, PRRT_kwDOSzMG_c6Reetd: this must be symmetric in BOTH
 * directions, or a retry from either partial-failure state can duplicate an
 * append-only tasting or spuriously re-post an already-good rating).
 *
 * The rating-only-failed recovery path retries from THIS form's own
 * preserved draft (round 2, PRRT_kwDOSzMG_c6Reeti) — NOT via `RoastRating`'s
 * "Edit", which pre-fills from the OLD persisted rating and would lose the
 * just-attempted values entirely. `RoastRating`'s own Edit stays blocked for
 * the duration of any rating write from here too (round 2,
 * PRRT_kwDOSzMG_c6ReetO: the last-write-wins guard is symmetric — a save
 * here blocks Edit there, and a save there blocks "Add tasting" here, both
 * via the shared `ratingMutationKey`).
 *
 * Round 3 (PRRT_kwDOSzMG_c6RfBAE) pinned a RETRY to the payload captured on
 * attempt one instead of the live draft, closing the divergence where
 * editing the star rating between a partial failure and its retry made the
 * retried record disagree with the one that already landed. But that fix
 * only captured `stars`/`notes` — round 4 (PRRT_kwDOSzMG_c6RflFr /
 * PRRT_kwDOSzMG_c6RflF1) found the SAME class one layer down: (a) the
 * captured metadata (tasted-at/brew/grind/tags) was never pinned, so a
 * tasting-side retry rebuilt those fields from the (still-live, still-
 * editable) draft — a hybrid record, part attempt-one part attempt-two; and
 * (b) `RoastRating`'s standalone "Edit" was only blocked while a rating
 * WRITE was in flight, not during the whole partial-failure WINDOW, so an
 * external direct edit landing between the partial failure and the retry
 * went unobserved — the next "Add tasting" retry silently re-posted (and so
 * overwrote) the stale captured rating.
 *
 * Round 4's fix converges the whole class structurally rather than patching
 * a third edge: the partial-failure state is now a well-defined FROZEN mode.
 * `attemptRef` captures the ENTIRE first-gesture payload (every tasting
 * field, not just stars/notes) the moment a save cycle starts, and every
 * field in this form is disabled for as long as this cycle's partial
 * failure is unresolved (derived from the SETTLED mutation state —
 * `tastingOnlyFailed`/`ratingOnlyFailed`/`bothFailed` — not `isPending`,
 * which is false for the whole gap between the failure and its retry; that
 * gap is exactly the window this closes). `RoastRating`'s Edit reads the
 * SAME "a partial-failure window is open" signal via the shared
 * `usePartialFailureLock` (`useSaveRating.ts`) — set the moment a partial
 * failure is detected and cleared on full success or an explicit discard —
 * so no external direct edit can land unobserved during that window either.
 * A "Start over" affordance (round 4's explicit escape) discards the frozen
 * attempt and both mutations' state, so the operator is never trapped —
 * with a real, if rare, tradeoff owned deliberately: starting over on a
 * `tastingOnlyFailed` state does NOT retract the tasting entry that already
 * saved (it is append-only by design), it only abandons the STUCK rating
 * retry, leaving that one tasting entry's stars as the last word until a
 * fresh save or a direct `RoastRating` edit corrects it.
 *
 * Round 5 (PRRT_kwDOSzMG_c6RgNHJ): the shared lock's FIRST implementation
 * stored it as a query-cache entry under the `["roasts", runId, ...]`
 * prefix — which put it inside the blast radius of `RoastedWeight.tsx` /
 * `ChargeWeight.tsx`'s own routine, broad (non-`exact`) invalidation of
 * `roastKeys.detail` / `roastKeys.history` on this same detail page. Saving
 * a weight while a tasting/rating partial failure sat unresolved silently
 * refetched (and cleared) the lock, re-enabling `RoastRating`'s Edit and
 * reopening the exact stale-overwrite race round 4 exists to close — via a
 * completely unrelated co-action. `usePartialFailureLock` now lives in a
 * plain module-scoped store with no query key at all (see its own doc in
 * `useSaveRating.ts`), so no invalidation from ANYWHERE in the app can ever
 * touch it — its lifetime is controlled ONLY by this component's own state
 * transitions and the explicit "Start over" discard.
 */

import { useEffect, useRef, useState } from "react";
import { useIsMutating } from "@tanstack/react-query";

import { cn } from "@/lib/cn";
import { useAddTasting, useTastings } from "@/hooks/queries";
import type {
  BrewMethod,
  RoastTasting,
  TastingAttribute,
  TastingDefect,
  TastingEntryRequest,
} from "@/lib/types";
import { ratingMutationKey, useSaveRating, useSetPartialFailureLock } from "./useSaveRating";

type Stars = TastingEntryRequest["stars"];
const STAR_VALUES: Stars[] = [1, 2, 3, 4, 5];

/** The full first-gesture payload captured for a save cycle (round 4) —
 *  every tasting field plus the shared stars/notes, so a retry of EITHER
 *  side reconstructs the exact same record its sibling mutation already
 *  landed (or is about to), never a hybrid of attempt-one and a
 *  since-edited live draft. */
interface CapturedAttempt {
  stars: Stars;
  notes: string | null;
  tastedAtUtc: string | null;
  brewMethod: BrewMethod | null;
  grindNote: string | null;
  attributes: TastingAttribute[];
  defects: TastingDefect[];
}

const BREW_METHODS: BrewMethod[] = [
  "espresso",
  "pour_over",
  "french_press",
  "aeropress",
  "moka_pot",
  "drip",
  "cupping",
  "other",
];

const ATTRIBUTES: TastingAttribute[] = ["sweetness", "acidity", "body"];
const DEFECTS: TastingDefect[] = ["grassy", "baked", "bitter", "flat"];

export interface RoastTastingsProps {
  runId: string;
  className?: string;
}

export function RoastTastings({ runId, className }: RoastTastingsProps): React.JSX.Element {
  const tastings = useTastings(runId);
  const tastingMutation = useAddTasting(runId);
  const ratingMutation = useSaveRating(runId);
  // Round 2 (PRRT_kwDOSzMG_c6ReetO): the last-write-wins guard round 1 built
  // was one-directional — RoastRating's own Edit was blocked while a
  // tasting-triggered rating save was in flight, but nothing stopped a DIRECT
  // RoastRating save that was already in flight from racing a SUBSEQUENT
  // "Add tasting" click here, which fires a SECOND `api.rate` for the same
  // run concurrently with the first. The shared `ratingMutationKey` makes
  // "is a rating write in flight ANYWHERE on the page" visible from both
  // widgets equally.
  const otherRatingWriteInFlight = useIsMutating({ mutationKey: ratingMutationKey(runId) }) > 0;
  const setPartialFailureLock = useSetPartialFailureLock(runId);

  const [stars, setStars] = useState<Stars | null>(null);
  const [notes, setNotes] = useState("");
  const [tastedAt, setTastedAt] = useState("");
  const [brewMethod, setBrewMethod] = useState<BrewMethod | "">("");
  const [grindNote, setGrindNote] = useState("");
  const [attributes, setAttributes] = useState<TastingAttribute[]>([]);
  const [defects, setDefects] = useState<TastingDefect[]>([]);

  // Round 4 (PRRT_kwDOSzMG_c6RflFr): captures the ENTIRE first-gesture
  // payload — every tasting field, not just stars/notes (round 3's gap) —
  // the moment a save cycle starts. Every retry of that SAME cycle reuses
  // this captured record instead of the (frozen, but doubly-so as belt and
  // braces) live draft, so a retry can never reconstruct a hybrid of
  // attempt-one and attempt-two.
  const attemptRef = useRef<CapturedAttempt | null>(null);

  const isPending =
    tastingMutation.isPending || ratingMutation.isPending || otherRatingWriteInFlight;
  // Both mutations must succeed (this save cycle, i.e. after the most recent
  // click — see the reset below) before the form reports "Saved." and resets.
  const isSaved =
    tastingMutation.isSuccess && ratingMutation.isSuccess && !isPending;
  // A "this record saved" claim requires the OTHER mutation to have SETTLED
  // (succeeded, not just "hasn't failed yet") — #568 Codex: gating on
  // `!otherFailed` alone is also true while the other mutation is still
  // PENDING, so the UI could claim a record landed before it actually
  // settled. `tasting-only saved` / `rating-only saved` are true only once
  // the failing side has genuinely finished failing and the succeeding side
  // has genuinely finished succeeding — never mid-flight.
  const tastingFailed = tastingMutation.isError;
  const ratingFailed = ratingMutation.isError;
  const tastingOnlyFailed = tastingFailed && ratingMutation.isSuccess;
  const ratingOnlyFailed = ratingFailed && tastingMutation.isSuccess;
  const bothFailed = tastingFailed && ratingFailed;
  // Round 4: the FROZEN partial-failure window — settled into SOME failure,
  // still unresolved (no successful retry, no discard yet). Distinct from
  // `isPending`, which is `false` for the entire gap between the failure and
  // its retry; this is the state that must freeze the form and block
  // RoastRating's Edit, not just the narrower in-flight moments.
  const hasUnresolvedPartial = tastingOnlyFailed || ratingOnlyFailed || bothFailed;
  const isFrozen = isPending || hasUnresolvedPartial;

  const toggle = <T,>(list: T[], value: T, setList: (next: T[]) => void) => {
    setList(list.includes(value) ? list.filter((v) => v !== value) : [...list, value]);
  };

  const resetForm = () => {
    setStars(null);
    setNotes("");
    setTastedAt("");
    setBrewMethod("");
    setGrindNote("");
    setAttributes([]);
    setDefects([]);
  };

  // #568 Codex: the form previously reset (clearing the star rating + notes
  // draft) the instant the TASTING mutation alone succeeded — even when the
  // rating mutation went on to fail. The partial-failure message tells the
  // operator to retry via RoastRating's "Edit", but that form pre-fills from
  // the OLD persisted rating, not the just-attempted values — so the draft
  // was silently lost with no way to recover it. Defer the reset until BOTH
  // mutations have succeeded for this same attempt (the tasting record is
  // append-only and already safely saved either way, so nothing is lost by
  // waiting — only the draft-clearing is deferred, not the tasting write).
  useEffect(() => {
    if (isSaved) {
      resetForm();
      attemptRef.current = null;
    }
  }, [isSaved]);

  // Round 4/6 (PRRT_kwDOSzMG_c6Rg5YO): publish the shared "this combined save
  // gesture is not yet fully resolved" signal for the SAME `isFrozen` window
  // this form freezes its own fields for — not the narrower
  // `hasUnresolvedPartial` alone. Round 4's original version armed the lock
  // only once a partial failure had SETTLED, leaving a real timing gap: if
  // the rating mutation rejects FAST while `api.addTasting` is still
  // pending, `otherRatingWriteInFlight` (RoastRating's own in-flight guard)
  // has already gone false and `hasUnresolvedPartial` hasn't been set yet
  // (the tasting side hasn't settled either way) — RoastTastings is frozen
  // (`isFrozen` is true, since `isPending` still is) but RoastRating's Edit
  // was ENABLED for that window, and a direct edit landing there gets
  // silently overwritten the instant the tasting settles and the stale
  // captured rating retries. Driving the lock off `isFrozen` covers the
  // WHOLE combined-save lifecycle with one shared condition — armed the
  // moment the gesture starts, held through any in-flight-partial transient
  // and the settled-partial window, cleared only on full success (the
  // effect above, which fires in the same render pass `isFrozen` goes
  // false) or the explicit "Start over" below.
  useEffect(() => {
    setPartialFailureLock(isFrozen);
  }, [isFrozen, setPartialFailureLock]);

  const isRetry = hasUnresolvedPartial;

  const onSave = () => {
    if (stars === null) return;
    const trimmedNotes = notes.trim() === "" ? null : notes.trim();

    // Round 4 (PRRT_kwDOSzMG_c6RflFr): a retry reuses the ENTIRE captured
    // attempt-one payload — every tasting field, not just stars/notes
    // (round 3's fix stopped one layer short) — never the live draft, which
    // is frozen (disabled inputs) during this window anyway but pinned here
    // too as the authoritative source regardless. A fresh (non-retry) save
    // captures its own full payload as the new attempt-one.
    const payload: CapturedAttempt =
      isRetry && attemptRef.current !== null
        ? attemptRef.current
        : {
            stars,
            notes: trimmedNotes,
            tastedAtUtc: tastedAt === "" ? null : new Date(tastedAt).toISOString(),
            brewMethod: brewMethod === "" ? null : brewMethod,
            grindNote: grindNote.trim() === "" ? null : grindNote.trim(),
            attributes,
            defects,
          };
    if (!isRetry) attemptRef.current = payload;

    // Round 2 (PRRT_kwDOSzMG_c6Reetd): the retry-dedup round 1 added was also
    // one-directional — it skipped resubmitting the tasting on a
    // ratingOnlyFailed retry, but a tastingOnlyFailed retry (tasting failed,
    // rating already succeeded) still re-posted the ALREADY-SUCCEEDED rating.
    // That re-post can itself fail (reporting a spurious rating failure on a
    // retry that was really only about the tasting) or, if it succeeds,
    // silently overwrite a rating value that may have moved since (e.g. a
    // concurrent direct edit). A retry resubmits ONLY the record that
    // actually failed last time — never the one that already landed.
    if (!ratingOnlyFailed) {
      // Reset any previous attempt's error/success state before firing this
      // one, so a retry after a partial failure doesn't read as stale
      // success on the side that already landed.
      tastingMutation.reset();
      tastingMutation.mutate({
        stars: payload.stars,
        notes: payload.notes,
        tasted_at_utc: payload.tastedAtUtc,
        brew_method: payload.brewMethod,
        grind_note: payload.grindNote,
        attributes: payload.attributes,
        defects: payload.defects,
      });
    }
    // The headline rating rides the tasting's own stars/notes — one gesture,
    // both records (#566). Fired independently of the tasting mutation
    // above: either can succeed or fail on its own, and a fetch failure on
    // one must never block or silently swallow the other. Skipped on a
    // tastingOnlyFailed retry (this rating already succeeded) — same
    // no-double-write rule as the tasting side, mirrored.
    if (!tastingOnlyFailed) {
      ratingMutation.reset();
      ratingMutation.mutate({ stars: payload.stars, notes: payload.notes });
    }
  };

  // Round 4's explicit escape: discard the frozen attempt entirely, so the
  // operator is never trapped by a stuck partial-failure window. Does NOT
  // retract a tasting entry that already saved (append-only by design) — it
  // only abandons a still-failing retry and unfreezes the form for a fresh
  // save. Deliberately does not attempt to "undo" the successful side.
  const onStartOver = () => {
    tastingMutation.reset();
    ratingMutation.reset();
    attemptRef.current = null;
    setPartialFailureLock(false);
    resetForm();
  };

  return (
    <div
      data-testid="roast-tastings"
      className={cn("flex flex-col gap-3 rounded-lg border border-border bg-card p-4", className)}
    >
      <h3 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
        Tasting notes
      </h3>

      {tastings.data !== undefined && tastings.data.tastings.length > 0 && (
        <ul data-testid="tasting-entries" className="flex flex-col gap-2">
          {tastings.data.tastings.map((entry) => (
            <TastingEntry key={entry.id} entry={entry} />
          ))}
        </ul>
      )}

      <div className="flex items-center gap-1" role="radiogroup" aria-label="Tasting star rating">
        {STAR_VALUES.map((value) => {
          const filled = stars !== null && value <= stars;
          return (
            <button
              key={value}
              type="button"
              role="radio"
              aria-checked={stars === value}
              aria-label={`${value} star${value === 1 ? "" : "s"}`}
              data-testid={`tasting-star-${value}`}
              data-filled={filled ? "true" : "false"}
              onClick={() => setStars(value)}
              disabled={isFrozen}
              className={cn(
                "text-2xl leading-none transition-colors disabled:cursor-not-allowed disabled:opacity-50",
                filled ? "text-roast-caution" : "text-muted-foreground/40 hover:text-muted-foreground",
              )}
            >
              {filled ? "★" : "☆"}
            </button>
          );
        })}
      </div>

      <textarea
        data-testid="tasting-notes"
        value={notes}
        onChange={(e) => setNotes(e.target.value)}
        placeholder="Tasting notes"
        rows={2}
        disabled={isFrozen}
        className="w-full resize-y rounded-md border border-border bg-input px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground/60 focus:outline-none focus:ring-1 focus:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
      />

      <div className="flex flex-wrap items-center gap-2">
        <label className="flex items-center gap-1 text-xs text-muted-foreground">
          Tasted at
          <input
            type="datetime-local"
            data-testid="tasting-tasted-at"
            value={tastedAt}
            onChange={(e) => setTastedAt(e.target.value)}
            disabled={isFrozen}
            className="rounded-md border border-border bg-input px-2 py-1 text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
          />
        </label>
        <label className="flex items-center gap-1 text-xs text-muted-foreground">
          Brew
          <select
            data-testid="tasting-brew-method"
            value={brewMethod}
            onChange={(e) => setBrewMethod(e.target.value as BrewMethod | "")}
            disabled={isFrozen}
            className="rounded-md border border-border bg-input px-2 py-1 text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
          >
            <option value="">—</option>
            {BREW_METHODS.map((method) => (
              <option key={method} value={method}>
                {method.replace(/_/g, " ")}
              </option>
            ))}
          </select>
        </label>
        <input
          type="text"
          data-testid="tasting-grind-note"
          value={grindNote}
          onChange={(e) => setGrindNote(e.target.value)}
          placeholder="Grind note"
          disabled={isFrozen}
          className="min-w-0 flex-1 rounded-md border border-border bg-input px-2 py-1 text-sm text-foreground placeholder:text-muted-foreground/60 focus:outline-none focus:ring-1 focus:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
        />
      </div>

      <TagRow
        label="Attributes"
        testIdPrefix="tasting-attribute"
        values={ATTRIBUTES}
        selected={attributes}
        onToggle={(value) => toggle(attributes, value, setAttributes)}
        disabled={isFrozen}
      />
      <TagRow
        label="Defects"
        testIdPrefix="tasting-defect"
        values={DEFECTS}
        selected={defects}
        onToggle={(value) => toggle(defects, value, setDefects)}
        disabled={isFrozen}
      />

      <div className="flex items-center gap-3">
        <button
          type="button"
          data-testid="tasting-save"
          onClick={onSave}
          disabled={stars === null || isPending}
          className="inline-flex items-center rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:cursor-not-allowed disabled:opacity-50"
        >
          {isPending ? "Saving…" : "Add tasting"}
        </button>
        {/* #566: a partial failure (one of the two records didn't save) is
            surfaced honestly, never folded into a blanket "Saved." — each
            span names which record failed. #568 Codex: each "X saved"
            claim requires the surviving mutation to have genuinely
            SETTLED into success, not merely "hasn't failed yet" (which is
            also true mid-flight). */}
        {bothFailed && (
          <span data-testid="tasting-error" className="text-xs text-roast-fault">
            Save failed — try again.
          </span>
        )}
        {tastingOnlyFailed && (
          <span data-testid="tasting-error" className="text-xs text-roast-fault">
            Tasting save failed — try again. (Rating saved.)
          </span>
        )}
        {ratingOnlyFailed && (
          <span data-testid="rating-partial-error" className="text-xs text-roast-fault">
            Tasting saved, but the rating didn't update — try again here to
            retry just the rating.
          </span>
        )}
        {isSaved && (
          <span data-testid="tasting-saved" className="text-xs text-roast-nominal">
            Saved.
          </span>
        )}
        {/* Round 4's explicit escape from the frozen partial-failure state —
            never traps the operator behind a stuck retry. */}
        {hasUnresolvedPartial && (
          <button
            type="button"
            data-testid="tasting-start-over"
            onClick={onStartOver}
            className="text-xs text-muted-foreground hover:underline"
          >
            Start over
          </button>
        )}
      </div>
    </div>
  );
}

function TagRow<T extends string>({
  label,
  testIdPrefix,
  values,
  selected,
  onToggle,
  disabled = false,
}: {
  label: string;
  testIdPrefix: string;
  values: T[];
  selected: T[];
  onToggle: (value: T) => void;
  /** #533: disabled while a save is in flight — see the module doc's
   *  pending-save-draft-preservation note. */
  disabled?: boolean;
}): React.JSX.Element {
  return (
    <div className="flex flex-wrap items-center gap-2">
      <span className="text-xs text-muted-foreground">{label}</span>
      {values.map((value) => {
        const active = selected.includes(value);
        return (
          <button
            key={value}
            type="button"
            aria-pressed={active}
            data-testid={`${testIdPrefix}-${value}`}
            onClick={() => onToggle(value)}
            disabled={disabled}
            className={cn(
              "rounded-full border px-2 py-0.5 text-xs transition-colors disabled:cursor-not-allowed disabled:opacity-50",
              active
                ? "border-primary bg-primary/10 text-primary"
                : "border-border text-muted-foreground hover:border-muted-foreground",
            )}
          >
            {value}
          </button>
        );
      })}
    </div>
  );
}

function TastingEntry({ entry }: { entry: RoastTasting }): React.JSX.Element {
  return (
    <li
      data-testid={`tasting-entry-${entry.id}`}
      className="flex flex-col gap-1 rounded-md border border-border/60 bg-background/40 p-2 text-xs"
    >
      <div className="flex items-center justify-between gap-2">
        <span aria-label={`${entry.stars} stars`} className="text-roast-caution">
          {"★".repeat(entry.stars)}
          <span className="text-muted-foreground/40">{"★".repeat(5 - entry.stars)}</span>
        </span>
        <span className="text-muted-foreground">
          {entry.tasted_at_utc ?? entry.recorded_at_utc}
        </span>
      </div>
      {entry.notes !== null && <p className="text-foreground">{entry.notes}</p>}
      {(entry.attributes.length > 0 || entry.defects.length > 0) && (
        <p className="text-muted-foreground">
          {[...entry.attributes, ...entry.defects].join(", ")}
        </p>
      )}
      {(entry.brew_method !== null || entry.grind_note !== null) && (
        <p className="text-muted-foreground">
          {[
            entry.brew_method !== null ? entry.brew_method.replace(/_/g, " ") : null,
            entry.grind_note,
          ]
            .filter((part): part is string => part !== null)
            .join(" — ")}
        </p>
      )}
    </li>
  );
}
