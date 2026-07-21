/**
 * Add / edit a saved bean profile (#303, D45).
 *
 * A blocking modal over the Start-Roast page that captures the full bean-profile
 * field set (the shared `BeanProfileFields`) plus the `default_bean_weight_grams`
 * that pre-fills a new roast's charge weight. On save it POSTs (add) or PUTs (edit)
 * via the typed bean-profile mutations; the parent invalidates the list query and
 * selects the saved profile. Editing a saved profile affects FUTURE roasts only —
 * the backend guarantees a past roast keeps its frozen snapshot (#303).
 *
 * INVARIANTS: the SPA renders + mutates only via the typed REST client (never MCP);
 * all temperatures are Celsius; the client-side validation is defense-in-depth + UX,
 * the server's pydantic bounds are the authority (a 422 surfaces inline).
 */

import { useState } from "react";

import { ApiError } from "@/lib/api";
import { cn } from "@/lib/cn";
import type { BeanProfile, BeanProfileInput } from "@/lib/types";
import {
  DEFAULT_BEAN_PROFILE_DRAFT,
  draftFromBeanProfile,
  validateBeanProfile,
  withFieldEdited,
  type BeanProfileDraft,
  type BeanProfileErrors,
} from "./beanProfileDraft";
import { BeanProfileFields } from "./beanProfileFields";

export interface BeanProfileModalProps {
  /** Modal mode — "add" starts blank; "edit" pre-fills from `profile`. */
  mode: "add" | "edit";
  /** The profile being edited (required in "edit" mode); ignored in "add". */
  profile?: BeanProfile;
  /** Persist the captured input — wired to the create/update mutation. Resolves to
   *  the saved `BeanProfile`; rejects (ApiError) on a 4xx/5xx (e.g. 422). */
  onSave: (input: BeanProfileInput) => Promise<BeanProfile>;
  /** Called with the saved profile so the parent can select it + close. */
  onSaved: (saved: BeanProfile) => void;
  /** Close without saving. */
  onClose: () => void;
  /** Archive (soft-delete) the edited profile — wired to the delete mutation.
   *  Only offered in "edit" mode; omit to hide the affordance. */
  onArchive?: (id: string) => Promise<unknown>;
}

export function BeanProfileModal({
  mode,
  profile,
  onSave,
  onSaved,
  onClose,
  onArchive,
}: BeanProfileModalProps): React.JSX.Element {
  const [draft, setDraft] = useState<BeanProfileDraft>(() =>
    mode === "edit" && profile !== undefined
      ? draftFromBeanProfile(profile)
      : DEFAULT_BEAN_PROFILE_DRAFT,
  );
  const [errors, setErrors] = useState<BeanProfileErrors>({});
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  // Editing a field orphans any provenance/evidence it carried (#627): those
  // describe the value the SERVER extracted, not whatever the operator just
  // typed — `withFieldEdited` drops the stale entry so it is never re-attributed
  // to the new value.
  const onChange = (field: keyof BeanProfileDraft, value: string) =>
    setDraft((d) => withFieldEdited(d, field, value));
  const onBlendChange = (checked: boolean) =>
    setDraft((d) => withFieldEdited(d, "is_blend", checked));

  const title = mode === "add" ? "Add Bean Profile" : "Edit Bean Profile";

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (submitting) return;
    setSubmitError(null);

    const result = validateBeanProfile(draft);
    if ("errors" in result) {
      setErrors(result.errors);
      return;
    }
    setErrors({});
    setSubmitting(true);
    try {
      const saved = await onSave(result.input);
      onSaved(saved);
    } catch (err) {
      if (err instanceof ApiError) {
        setSubmitError(err.detail || `Request failed (${err.status}).`);
      } else {
        setSubmitError(err instanceof Error ? err.message : "Request failed.");
      }
    } finally {
      setSubmitting(false);
    }
  };

  const handleArchive = async () => {
    if (submitting || onArchive === undefined || profile === undefined) return;
    setSubmitError(null);
    setSubmitting(true);
    try {
      await onArchive(profile.id);
      onClose();
    } catch (err) {
      if (err instanceof ApiError) {
        setSubmitError(err.detail || `Request failed (${err.status}).`);
      } else {
        setSubmitError(err instanceof Error ? err.message : "Request failed.");
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      data-testid="bean-profile-modal"
      role="dialog"
      aria-modal="true"
      aria-labelledby="bean-profile-modal-title"
      className="fixed inset-0 z-50 flex items-center justify-center bg-background/70 p-6"
    >
      <form
        onSubmit={(e) => void handleSubmit(e)}
        noValidate
        data-testid="bean-profile-form"
        className="flex max-h-full w-full max-w-2xl flex-col gap-5 overflow-auto rounded-lg border border-border bg-card p-6 shadow-2xl"
      >
        <header className="flex items-start justify-between gap-4">
          <h2
            id="bean-profile-modal-title"
            className="text-lg font-bold uppercase tracking-wide"
          >
            {title}
          </h2>
          <button
            type="button"
            onClick={onClose}
            data-testid="bean-profile-cancel"
            aria-label="Close"
            className="rounded-md border border-input px-3 py-1 text-sm text-muted-foreground transition-colors hover:bg-muted/40"
          >
            Cancel
          </button>
        </header>

        {mode === "edit" && (
          <p className="rounded-md border border-border bg-muted/30 px-4 py-2 text-xs text-muted-foreground">
            Edits affect future roasts only. Past roasts keep their saved settings.
          </p>
        )}

        <BeanProfileFields
          draft={draft}
          errors={errors}
          onChange={onChange}
          onBlendChange={onBlendChange}
          testIdPrefix="bean-profile"
          showDefaultWeight
        />

        {submitError !== null && (
          <p
            data-testid="bean-profile-error"
            role="alert"
            className="rounded-md border border-roast-fault/50 bg-roast-fault/10 px-4 py-3 text-sm text-roast-fault"
          >
            {submitError}
          </p>
        )}

        <div className="flex items-center justify-between gap-3">
          {mode === "edit" && onArchive !== undefined ? (
            <button
              type="button"
              onClick={() => void handleArchive()}
              disabled={submitting}
              data-testid="bean-profile-archive"
              className={cn(
                "inline-flex items-center justify-center rounded-md border border-roast-fault/50 px-4 py-2 text-sm font-semibold uppercase tracking-wide text-roast-fault transition-colors",
                submitting ? "cursor-not-allowed opacity-60" : "hover:bg-roast-fault/10",
              )}
            >
              Archive
            </button>
          ) : (
            <span />
          )}
          <button
            type="submit"
            disabled={submitting}
            aria-disabled={submitting}
            data-testid="bean-profile-save"
            className={cn(
              "inline-flex items-center justify-center rounded-md border border-roast-coffee/60 bg-roast-coffee/20 px-6 py-2 text-sm font-semibold uppercase tracking-wide text-roast-coffee transition-colors",
              submitting ? "cursor-not-allowed opacity-60" : "hover:bg-roast-coffee/30",
            )}
          >
            {submitting ? "Saving…" : "Save Profile"}
          </button>
        </div>
      </form>
    </div>
  );
}
