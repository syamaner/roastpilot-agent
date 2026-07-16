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
 * the form and shows "Saved." only once BOTH succeed (#568 Codex: resetting
 * from the tasting mutation's own `onSuccess` alone would clear the star
 * rating + notes draft even when the rating write went on to fail, and the
 * partial-failure message's only recovery path — RoastRating's "Edit" —
 * pre-fills from the OLD persisted rating, so the attempted values would be
 * unrecoverable). Each failure names which record didn't save. A retry after
 * a rating-only partial failure resubmits ONLY the rating — the tasting
 * already succeeded and is append-only, so replaying it on retry would create
 * a duplicate entry (a hazard this same preserved-draft fix newly exposed).
 */

import { useEffect, useState } from "react";

import { cn } from "@/lib/cn";
import { useAddTasting, useTastings } from "@/hooks/queries";
import type {
  BrewMethod,
  RoastTasting,
  TastingAttribute,
  TastingDefect,
  TastingEntryRequest,
} from "@/lib/types";
import { useSaveRating } from "./useSaveRating";

type Stars = TastingEntryRequest["stars"];
const STAR_VALUES: Stars[] = [1, 2, 3, 4, 5];

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

  const [stars, setStars] = useState<Stars | null>(null);
  const [notes, setNotes] = useState("");
  const [tastedAt, setTastedAt] = useState("");
  const [brewMethod, setBrewMethod] = useState<BrewMethod | "">("");
  const [grindNote, setGrindNote] = useState("");
  const [attributes, setAttributes] = useState<TastingAttribute[]>([]);
  const [defects, setDefects] = useState<TastingDefect[]>([]);

  const isPending = tastingMutation.isPending || ratingMutation.isPending;
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
    if (isSaved) resetForm();
  }, [isSaved]);

  const onSave = () => {
    if (stars === null) return;
    const trimmedNotes = notes.trim() === "" ? null : notes.trim();

    // A retry after a rating-only partial failure (#568: the draft is
    // preserved for exactly this retry) must NOT resubmit the tasting — it
    // already succeeded and is append-only, so clicking "Add tasting" again
    // would create a SECOND, duplicate tasting entry for the one gesture.
    // Only the rating write is retried in that case.
    if (!ratingOnlyFailed) {
      // Reset any previous attempt's error/success state before firing this
      // one, so a retry after a partial failure doesn't read as stale
      // success on the side that already landed.
      tastingMutation.reset();
      tastingMutation.mutate({
        stars,
        notes: trimmedNotes,
        tasted_at_utc: tastedAt === "" ? null : new Date(tastedAt).toISOString(),
        brew_method: brewMethod === "" ? null : brewMethod,
        grind_note: grindNote.trim() === "" ? null : grindNote.trim(),
        attributes,
        defects,
      });
    }
    // The headline rating rides the tasting's own stars/notes — one gesture,
    // both records (#566). Fired independently of the tasting mutation
    // above: either can succeed or fail on its own, and a fetch failure on
    // one must never block or silently swallow the other.
    ratingMutation.reset();
    ratingMutation.mutate({ stars, notes: trimmedNotes });
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
              disabled={isPending}
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
        disabled={isPending}
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
            disabled={isPending}
            className="rounded-md border border-border bg-input px-2 py-1 text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
          />
        </label>
        <label className="flex items-center gap-1 text-xs text-muted-foreground">
          Brew
          <select
            data-testid="tasting-brew-method"
            value={brewMethod}
            onChange={(e) => setBrewMethod(e.target.value as BrewMethod | "")}
            disabled={isPending}
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
          disabled={isPending}
          className="min-w-0 flex-1 rounded-md border border-border bg-input px-2 py-1 text-sm text-foreground placeholder:text-muted-foreground/60 focus:outline-none focus:ring-1 focus:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
        />
      </div>

      <TagRow
        label="Attributes"
        testIdPrefix="tasting-attribute"
        values={ATTRIBUTES}
        selected={attributes}
        onToggle={(value) => toggle(attributes, value, setAttributes)}
        disabled={isPending}
      />
      <TagRow
        label="Defects"
        testIdPrefix="tasting-defect"
        values={DEFECTS}
        selected={defects}
        onToggle={(value) => toggle(defects, value, setDefects)}
        disabled={isPending}
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
            Tasting saved, but the rating didn't update — use "Edit" on Your
            rating above to retry.
          </span>
        )}
        {isSaved && (
          <span data-testid="tasting-saved" className="text-xs text-roast-nominal">
            Saved.
          </span>
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
