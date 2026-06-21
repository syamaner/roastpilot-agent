/**
 * Start-roast affordance (#158) with the bean-profile library (#303, D45).
 *
 * Shown on the dashboard's IDLE state (no active run): a pre-filled form so the
 * operator can start a roast from the UI on the headless Pi appliance (E11), where
 * there is no terminal to `curl POST /api/roasts`.
 *
 * #303 adds the saved bean-profile library: a dropdown of `GET /api/bean-profiles`
 * (Add / Edit modals via the CRUD mutations). Selecting a saved profile FILLS the
 * bean-identity + roast-target fields and pre-fills the charge weight from the
 * profile's `default_bean_weight_grams`. The charge weight is PER ROAST — adjustable
 * each time without editing the saved profile. Start still composes + POSTs a
 * `RoastProfile` (the selected/entered fields + the per-roast weight) to the
 * UNCHANGED start-roast endpoint — the start contract is untouched.
 *
 * INVARIANTS: the SPA renders + mutates from server state (the dropdown is the saved
 * library; on a 201 we do NOT fabricate local run state — the page's active-run
 * detection + SSE pick up the new run). Never calls MCP, never infers phase. All
 * temperatures Celsius. Starting commands the roaster to preheat (real heat) — the
 * form says so plainly. The client-side bounds are defense-in-depth + UX, not the
 * authority (the server's `SafetyLimits` + pydantic bounds are).
 */

import { useMemo, useState } from "react";

import { ApiError } from "@/lib/api";
import { cn } from "@/lib/cn";
import type { BeanProfile, BeanProfileInput, RoastProfile } from "@/lib/types";
import {
  DEFAULT_BEAN_PROFILE_DRAFT,
  draftFromBeanProfile,
  validateBeanProfile,
  type BeanProfileDraft,
  type BeanProfileErrors,
} from "./beanProfileDraft";
import { BeanProfileFields, Field } from "./beanProfileFields";
import { BeanProfileModal } from "./BeanProfileModal";
import { BeanProfilePicker } from "./BeanProfilePicker";

export interface StartRoastFormProps {
  /** Submit the assembled profile. Resolves on 201; rejects (ApiError) on
   *  4xx/5xx — e.g. 409 when a roast is already active. Wired to `api.startRoast`. */
  onStart: (profile: RoastProfile) => Promise<unknown>;
  /** The saved bean-profile library (from `useBeanProfiles`). */
  profiles?: BeanProfile[];
  /** Whether the library query is still loading. */
  profilesLoading?: boolean;
  /** Create a saved profile (wired to `useCreateBeanProfile`). */
  onCreateProfile?: (input: BeanProfileInput) => Promise<BeanProfile>;
  /** Edit a saved profile (wired to `useUpdateBeanProfile`). */
  onUpdateProfile?: (id: string, input: BeanProfileInput) => Promise<BeanProfile>;
  /** Archive a saved profile (wired to `useDeleteBeanProfile`); omit to hide it. */
  onArchiveProfile?: (id: string) => Promise<unknown>;
  className?: string;
}

/** Charge-weight bounds (UX only; the server's `bean_weight_grams > 0` is authority). */
const WEIGHT_MIN_G = 1;

type ModalState = { mode: "add" } | { mode: "edit"; profile: BeanProfile } | null;

export function StartRoastForm({
  onStart,
  profiles = [],
  profilesLoading = false,
  onCreateProfile,
  onUpdateProfile,
  onArchiveProfile,
  className,
}: StartRoastFormProps): React.JSX.Element {
  // The bean fields (filled from a selected profile, or entered manually).
  const [draft, setDraft] = useState<BeanProfileDraft>(DEFAULT_BEAN_PROFILE_DRAFT);
  const [errors, setErrors] = useState<BeanProfileErrors>({});
  // The per-roast charge weight (pre-filled from a profile's default; adjustable).
  const [weight, setWeight] = useState<string>(
    DEFAULT_BEAN_PROFILE_DRAFT.default_bean_weight_grams,
  );
  const [weightError, setWeightError] = useState<string | undefined>(undefined);
  // The selected saved-profile id ("" = manual entry).
  const [selectedId, setSelectedId] = useState<string>("");
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [modal, setModal] = useState<ModalState>(null);

  const profilesById = useMemo(() => {
    const map = new Map<string, BeanProfile>();
    for (const p of profiles) map.set(p.id, p);
    return map;
  }, [profiles]);

  // Selecting a saved profile FILLS the bean fields and pre-fills the per-roast
  // weight from its default; "" returns to manual entry without wiping the form.
  const applyProfile = (id: string) => {
    setSelectedId(id);
    setErrors({});
    setSubmitError(null);
    if (id === "") return;
    const profile = profilesById.get(id);
    if (profile === undefined) return;
    setDraft(draftFromBeanProfile(profile));
    setWeight(String(profile.default_bean_weight_grams));
    setWeightError(undefined);
  };

  const onChange = (field: keyof BeanProfileDraft, value: string) =>
    setDraft((d) => ({ ...d, [field]: value }));
  const onBlendChange = (checked: boolean) =>
    setDraft((d) => ({ ...d, is_blend: checked }));

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (submitting) return;
    setSubmitError(null);

    const result = validateBeanProfile(draft);
    const weightValue = Number(weight);
    const weightInvalid =
      weight.trim() === "" || !Number.isFinite(weightValue) || weightValue <= 0;
    if ("errors" in result || weightInvalid) {
      if ("errors" in result) setErrors(result.errors);
      setWeightError(weightInvalid ? "Must be greater than 0." : undefined);
      return;
    }
    setErrors({});
    setWeightError(undefined);
    setSubmitting(true);

    // Compose the per-roast RoastProfile: every saved bean field + this roast's
    // charge weight. `default_bean_weight_grams` is library-only (dropped here) —
    // the start contract is the unchanged RoastProfile shape.
    const { default_bean_weight_grams: _omit, ...beanFields } = result.input;
    const profile: RoastProfile = { ...beanFields, bean_weight_grams: weightValue };

    try {
      await onStart(profile);
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setSubmitError("A roast is already active. Reload to view it.");
      } else if (err instanceof ApiError) {
        setSubmitError(err.detail || `Request failed (${err.status}).`);
      } else {
        setSubmitError(err instanceof Error ? err.message : "Request failed.");
      }
    } finally {
      setSubmitting(false);
    }
  };

  // After an add/edit save: refresh the form from the saved profile + select it.
  const handleSaved = (saved: BeanProfile) => {
    setModal(null);
    setSelectedId(saved.id);
    setDraft(draftFromBeanProfile(saved));
    setWeight(String(saved.default_bean_weight_grams));
    setErrors({});
    setWeightError(undefined);
  };

  const canManageProfiles = onCreateProfile !== undefined && onUpdateProfile !== undefined;

  return (
    <>
      <form
        data-testid="start-roast-form"
        onSubmit={(e) => void handleSubmit(e)}
        noValidate
        className={cn(
          "mx-auto flex w-full max-w-2xl flex-col gap-5 rounded-lg border border-border bg-card p-6 shadow-lg",
          className,
        )}
      >
        <header className="flex flex-col gap-1">
          <h2 className="text-lg font-bold uppercase tracking-wide">Start Roast</h2>
          <p className="text-sm text-muted-foreground">
            Pick a saved bean profile (or enter the bean manually), set the charge
            weight, then start. Defaults are pre-filled.
          </p>
        </header>

        {canManageProfiles && (
          <BeanProfilePicker
            profiles={profiles}
            selectedId={selectedId}
            onSelect={applyProfile}
            onAdd={() => setModal({ mode: "add" })}
            onEdit={() => {
              const profile = profilesById.get(selectedId);
              if (profile !== undefined) setModal({ mode: "edit", profile });
            }}
            loading={profilesLoading}
          />
        )}

        <BeanProfileFields
          draft={draft}
          errors={errors}
          onChange={onChange}
          onBlendChange={onBlendChange}
          testIdPrefix="start-roast"
          showDefaultWeight={false}
        />

        {/* The per-roast charge weight (pre-filled from the profile default;
            adjustable each roast). Distinct from the saved default_bean_weight_grams. */}
        <fieldset className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <legend className="col-span-full text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            This roast
          </legend>
          <Field
            id="bean_weight_grams"
            label="Charge weight (g)"
            type="number"
            min={WEIGHT_MIN_G}
            step={1}
            value={weight}
            onChange={(e) => setWeight(e.target.value)}
            error={weightError}
            hint="This roast only — adjustable per roast"
            testIdPrefix="start-roast"
          />
        </fieldset>

        {/* The real-heat note: starting commands the roaster to preheat. */}
        <p
          data-testid="start-roast-heat-note"
          className="rounded-md border border-roast-caution/40 bg-roast-caution/10 px-4 py-3 text-sm"
        >
          Starting commands the roaster to <strong>preheat — this turns on real heat</strong>.
          Make sure the roaster is ready.
        </p>

        {submitError !== null && (
          <p
            data-testid="start-roast-error"
            role="alert"
            className="rounded-md border border-roast-fault/50 bg-roast-fault/10 px-4 py-3 text-sm text-roast-fault"
          >
            {submitError}
          </p>
        )}

        <button
          type="submit"
          data-testid="start-roast-submit"
          disabled={submitting}
          aria-disabled={submitting}
          className={cn(
            "inline-flex items-center justify-center rounded-md border border-roast-coffee/60 bg-roast-coffee/20 px-6 py-3 text-sm font-semibold uppercase tracking-wide text-roast-coffee transition-colors",
            submitting ? "cursor-not-allowed opacity-60" : "hover:bg-roast-coffee/30",
          )}
        >
          {submitting ? "Starting…" : "Start Roast"}
        </button>
      </form>

      {modal !== null && canManageProfiles && (
        <BeanProfileModal
          mode={modal.mode}
          profile={modal.mode === "edit" ? modal.profile : undefined}
          onSave={(input) =>
            modal.mode === "edit"
              ? onUpdateProfile!(modal.profile.id, input)
              : onCreateProfile!(input)
          }
          onSaved={handleSaved}
          onClose={() => setModal(null)}
          onArchive={modal.mode === "edit" ? onArchiveProfile : undefined}
        />
      )}
    </>
  );
}
