/**
 * Start-roast affordance (#158 — operate the appliance without curl).
 *
 * Shown on the dashboard's IDLE state (no active run): a pre-filled `RoastProfile`
 * form so the operator can start a roast from the UI on the headless Pi appliance
 * (E11), where there is no terminal to `curl POST /api/roasts`. Defaults mean the
 * operator mostly sets bean + weight, then Start.
 *
 * INVARIANTS: the SPA renders from server state. On a 201 we do NOT fabricate local
 * run state — the page's active-run detection (`useHealth.active_run_id`) + SSE pick
 * up the new run and transition the dashboard to Live. We never call MCP directly,
 * never infer phase. All temperatures Celsius. Starting commands the roaster to
 * preheat (real heat) — the form says so plainly.
 *
 * Client-side validation mirrors `models.RoastProfile`'s bounds (percents 0–100,
 * weight/drop > 0, development 0–100, guidance min < max) AND adds conservative
 * Hottop-realistic Celsius bounds on the temperature inputs (TEMP_MIN_C..TEMP_MAX_C)
 * so a fat-finger like `2050` is rejected before the POST. The server's
 * `SafetyLimits` still clamps the actual heat/fan — this is defense-in-depth + UX,
 * not the authority.
 */

import { useState } from "react";

import { cn } from "@/lib/cn";
import { ApiError } from "@/lib/api";
import type { RoastProfile } from "@/lib/types";

/** The form's string-keyed draft (inputs are strings; we parse on submit). */
interface Draft {
  name: string;
  bean_origin: string;
  bean_varietal: string;
  bean_weight_grams: string;
  charge_guidance_min_c: string;
  charge_guidance_max_c: string;
  initial_heat_percent: string;
  initial_fan_percent: string;
  target_drop_temp_c: string;
  target_development_percent: string;
}

/** Pre-filled defaults (mirror `models.RoastProfile` field defaults). The
 *  operator mainly fills name + bean_origin + weight. */
const DEFAULT_DRAFT: Draft = {
  name: "",
  bean_origin: "",
  bean_varietal: "",
  bean_weight_grams: "",
  charge_guidance_min_c: "170",
  charge_guidance_max_c: "200",
  initial_heat_percent: "70",
  initial_fan_percent: "40",
  target_drop_temp_c: "205",
  target_development_percent: "20",
};

export interface StartRoastFormProps {
  /** Submit the assembled profile. Resolves on 201; rejects (ApiError) on
   *  4xx/5xx — e.g. 409 when a roast is already active. Wired to
   *  `api.startRoast` by the page. */
  onStart: (profile: RoastProfile) => Promise<unknown>;
  className?: string;
}

/**
 * Conservative Hottop-realistic Celsius bounds for the temperature inputs. These
 * are intentionally wide enough for any sane roast (charge band, drop temp) but
 * tight enough to reject a fat-finger like `2050` instead of `205`. Defense-in-depth
 * + UX only — the server's `SafetyLimits` remains the authority for actual control.
 */
const TEMP_MIN_C = 100;
const TEMP_MAX_C = 240;

/** Field-level validation errors, keyed by draft field. */
type Errors = Partial<Record<keyof Draft, string>>;

/** Parse + bounds-check the draft. Returns the typed profile or field errors. */
function validate(draft: Draft): { profile: RoastProfile } | { errors: Errors } {
  const errors: Errors = {};

  const name = draft.name.trim();
  if (name === "") errors.name = "Required.";
  const beanOrigin = draft.bean_origin.trim();
  if (beanOrigin === "") errors.bean_origin = "Required.";
  const varietalRaw = draft.bean_varietal.trim();

  const weight = Number(draft.bean_weight_grams);
  if (draft.bean_weight_grams.trim() === "" || !Number.isFinite(weight) || weight <= 0)
    errors.bean_weight_grams = "Must be greater than 0.";

  const tempRange = `${TEMP_MIN_C}–${TEMP_MAX_C} °C.`;
  const inTempRange = (v: number) => Number.isFinite(v) && v >= TEMP_MIN_C && v <= TEMP_MAX_C;

  const minC = Number(draft.charge_guidance_min_c);
  if (!inTempRange(minC)) errors.charge_guidance_min_c = tempRange;
  const maxC = Number(draft.charge_guidance_max_c);
  if (!inTempRange(maxC)) errors.charge_guidance_max_c = tempRange;
  // Keep the min < max guidance check (rejects the >= equality path too), only once
  // both values are in range so the range error is what's shown for a bad number.
  if (
    errors.charge_guidance_min_c === undefined &&
    errors.charge_guidance_max_c === undefined &&
    minC >= maxC
  )
    errors.charge_guidance_max_c = "Max must be above min.";

  const heat = Number(draft.initial_heat_percent);
  if (!Number.isInteger(heat) || heat < 0 || heat > 100)
    errors.initial_heat_percent = "0–100.";
  const fan = Number(draft.initial_fan_percent);
  if (!Number.isInteger(fan) || fan < 0 || fan > 100) errors.initial_fan_percent = "0–100.";

  const drop = Number(draft.target_drop_temp_c);
  if (!inTempRange(drop)) errors.target_drop_temp_c = tempRange;

  const dev = Number(draft.target_development_percent);
  if (!Number.isFinite(dev) || dev <= 0 || dev >= 100)
    errors.target_development_percent = "0–100 (exclusive).";

  if (Object.keys(errors).length > 0) return { errors };

  return {
    profile: {
      name,
      bean_origin: beanOrigin,
      bean_varietal: varietalRaw === "" ? null : varietalRaw,
      bean_weight_grams: weight,
      charge_guidance_min_c: minC,
      charge_guidance_max_c: maxC,
      initial_heat_percent: heat,
      initial_fan_percent: fan,
      target_drop_temp_c: drop,
      target_development_percent: dev,
    },
  };
}

export function StartRoastForm({ onStart, className }: StartRoastFormProps): React.JSX.Element {
  const [draft, setDraft] = useState<Draft>(DEFAULT_DRAFT);
  const [errors, setErrors] = useState<Errors>({});
  const [submitting, setSubmitting] = useState(false);
  /** A submit-level error (e.g. 409 conflict / network) distinct from field errors. */
  const [submitError, setSubmitError] = useState<string | null>(null);

  const set = (field: keyof Draft) => (e: React.ChangeEvent<HTMLInputElement>) => {
    const { value } = e.target;
    setDraft((d) => ({ ...d, [field]: value }));
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (submitting) return; // never double-submit
    setSubmitError(null);

    const result = validate(draft);
    if ("errors" in result) {
      setErrors(result.errors);
      return;
    }
    setErrors({});
    setSubmitting(true);
    try {
      await onStart(result.profile);
      // On success we deliberately do NOT mutate local state to "go live": the
      // page's active-run detection + SSE pick up the new run (render from server
      // state). The form simply unmounts when the dashboard swaps to the live view.
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

  return (
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
          No active roast. Set the bean and weight, then start. Defaults are pre-filled.
        </p>
      </header>

      <fieldset className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <legend className="sr-only">Roast profile</legend>
        <Field
          id="name"
          label="Profile name"
          value={draft.name}
          onChange={set("name")}
          error={errors.name}
          placeholder="Morning batch"
        />
        <Field
          id="bean_origin"
          label="Bean origin"
          value={draft.bean_origin}
          onChange={set("bean_origin")}
          error={errors.bean_origin}
          placeholder="Ethiopia Guji"
        />
        <Field
          id="bean_varietal"
          label="Varietal (optional)"
          value={draft.bean_varietal}
          onChange={set("bean_varietal")}
          error={errors.bean_varietal}
          placeholder="Heirloom"
        />
        <Field
          id="bean_weight_grams"
          label="Bean weight (g)"
          type="number"
          min={1}
          step={1}
          value={draft.bean_weight_grams}
          onChange={set("bean_weight_grams")}
          error={errors.bean_weight_grams}
          placeholder="250"
        />
        <Field
          id="charge_guidance_min_c"
          label="Charge min (°C)"
          type="number"
          min={TEMP_MIN_C}
          max={TEMP_MAX_C}
          step={1}
          value={draft.charge_guidance_min_c}
          onChange={set("charge_guidance_min_c")}
          error={errors.charge_guidance_min_c}
          hint={`${TEMP_MIN_C}–${TEMP_MAX_C} °C`}
        />
        <Field
          id="charge_guidance_max_c"
          label="Charge max (°C)"
          type="number"
          min={TEMP_MIN_C}
          max={TEMP_MAX_C}
          step={1}
          value={draft.charge_guidance_max_c}
          onChange={set("charge_guidance_max_c")}
          error={errors.charge_guidance_max_c}
          hint={`${TEMP_MIN_C}–${TEMP_MAX_C} °C`}
        />
        <Field
          id="initial_heat_percent"
          label="Initial heat (%)"
          type="number"
          min={0}
          max={100}
          step={1}
          value={draft.initial_heat_percent}
          onChange={set("initial_heat_percent")}
          error={errors.initial_heat_percent}
          hint="0–100, whole number"
        />
        <Field
          id="initial_fan_percent"
          label="Initial fan (%)"
          type="number"
          min={0}
          max={100}
          step={1}
          value={draft.initial_fan_percent}
          onChange={set("initial_fan_percent")}
          error={errors.initial_fan_percent}
          hint="0–100, whole number"
        />
        <Field
          id="target_drop_temp_c"
          label="Target drop (°C)"
          type="number"
          min={TEMP_MIN_C}
          max={TEMP_MAX_C}
          step={1}
          value={draft.target_drop_temp_c}
          onChange={set("target_drop_temp_c")}
          error={errors.target_drop_temp_c}
          hint={`${TEMP_MIN_C}–${TEMP_MAX_C} °C`}
        />
        <Field
          id="target_development_percent"
          label="Target development (%)"
          type="number"
          min={1}
          max={99}
          step={1}
          value={draft.target_development_percent}
          onChange={set("target_development_percent")}
          error={errors.target_development_percent}
          hint="0–100 (exclusive)"
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
  );
}

interface FieldProps {
  id: keyof Draft;
  label: string;
  value: string;
  onChange: (e: React.ChangeEvent<HTMLInputElement>) => void;
  error?: string;
  type?: "text" | "number";
  placeholder?: string;
  /** Native numeric bounds + step (native UA hints; validate() is the authority). */
  min?: number;
  max?: number;
  step?: number;
  /** A small always-visible hint under the field (e.g. units/range, integer-only). */
  hint?: string;
}

function Field({
  id,
  label,
  value,
  onChange,
  error,
  type = "text",
  placeholder,
  min,
  max,
  step,
  hint,
}: FieldProps): React.JSX.Element {
  const errorId = `${id}-error`;
  const hintId = `${id}-hint`;
  // Describe by the error when present, else by the hint (so screen readers get
  // the bounds/units context even before an error).
  const describedBy = error !== undefined ? errorId : hint !== undefined ? hintId : undefined;
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        {label}
      </label>
      <input
        id={id}
        name={id}
        type={type}
        inputMode={type === "number" ? "decimal" : undefined}
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={onChange}
        placeholder={placeholder}
        aria-invalid={error !== undefined}
        aria-describedby={describedBy}
        data-testid={`start-roast-${id}`}
        className={cn(
          "rounded-md border bg-background px-3 py-2 text-sm outline-none transition-colors focus:ring-1 focus:ring-ring",
          error !== undefined ? "border-roast-fault/70" : "border-input",
        )}
      />
      {error !== undefined ? (
        <span id={errorId} data-testid={`start-roast-${id}-error`} className="text-xs text-roast-fault">
          {error}
        </span>
      ) : (
        hint !== undefined && (
          <span id={hintId} className="text-xs text-muted-foreground">
            {hint}
          </span>
        )
      )}
    </div>
  );
}
