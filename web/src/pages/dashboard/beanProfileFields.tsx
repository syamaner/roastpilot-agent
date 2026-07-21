/**
 * Reusable bean-profile capture field components (#303, D45).
 *
 * The component half of the reusable bean-profile capture surface (the draft type,
 * defaults, and validation live in `beanProfileDraft.ts`). The field group renders
 * identically in THREE places — the inline Start-Roast form, the add-profile modal,
 * and the edit-profile modal — so the captured fields cannot drift (extracted from
 * the original `StartRoastForm`, #158/#164/#291).
 *
 * The parent owns the draft/errors state and the submit; these are presentation
 * only. A `testIdPrefix` keeps the inline form + the modals on distinct ids. All
 * temperatures are Celsius.
 */

import { useState } from "react";

import { cn } from "@/lib/cn";
import {
  ALTITUDE_MAX_M,
  ALTITUDE_MIN_M,
  fieldEvidenceFor,
  fieldSourceFor,
  PROCESSING_OPTIONS,
  SPECIES_OPTIONS,
  TEMP_MAX_C,
  TEMP_MIN_C,
  type BeanProfileDraft,
  type BeanProfileErrors,
  type FieldSourceValue,
} from "./beanProfileDraft";

/**
 * Per-field provenance badge (#627): "on page" when the server read the
 * value straight off the vendor page; "review" when it was imputed and
 * never code-confirmed — every typed-field citation gate is permanently
 * parked, so this is the operator's cue to check the value (and the quote
 * below it, when one was captured) before trusting it. Same visual language
 * as the shared `VerdictBadge` / `OutcomeBadge` (roast-nominal/caution
 * tokens), kept page-local since it renders a bean-metadata Literal, not a
 * safety verdict.
 */
function ProvenanceBadge({ source }: { source: FieldSourceValue }): React.JSX.Element {
  const onPage = source === "on_page";
  return (
    <span
      data-testid="field-provenance-badge"
      data-provenance={source}
      title={
        onPage
          ? "Confirmed on the vendor page"
          : "Not confirmed on the vendor page — review"
      }
      className={cn(
        "inline-flex items-center rounded-md border px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide",
        onPage
          ? "border-roast-nominal/40 bg-roast-nominal/15 text-roast-nominal"
          : "border-roast-caution/40 bg-roast-caution/15 text-roast-caution",
      )}
    >
      {onPage ? "On page" : "Review"}
    </span>
  );
}

/**
 * The captured vendor-page quote backing a typed field's value (#627),
 * rendered as visibly-quoted text under the field. SECURITY: `quote` is
 * UNTRUSTED vendor page text — this renders it as a plain JSX child
 * (React's default escaping) ONLY. Never `dangerouslySetInnerHTML`, never
 * interpolated into an HTML string, never linkified. Truncated (line-clamp)
 * with an expand toggle for the rare long quote (server-bounded to 500
 * chars) — mirrors the `DecisionTraceTable` rationale clamp pattern; the
 * full quote is always present in the DOM/accessibility tree, clamp is
 * visual only.
 */
function EvidenceQuote({ quote, testId }: { quote: string; testId: string }): React.JSX.Element {
  const [expanded, setExpanded] = useState(false);
  const truncatable = quote.length > 160;
  return (
    <blockquote
      data-testid={testId}
      className="flex items-start gap-2 border-l-2 border-muted-foreground/30 pl-2 text-xs text-muted-foreground"
    >
      <span className="min-w-0">
        <span className="font-semibold not-italic">Page says: </span>
        <span className={cn("italic", !expanded && "line-clamp-2")}>
          &ldquo;{quote}&rdquo;
        </span>
      </span>
      {truncatable && (
        <button
          type="button"
          data-testid={`${testId}-toggle`}
          aria-expanded={expanded}
          onClick={() => setExpanded((v) => !v)}
          className="shrink-0 text-xs text-muted-foreground underline-offset-2 hover:underline"
        >
          {expanded ? "less" : "more"}
        </button>
      )}
    </blockquote>
  );
}

interface FieldProps {
  id: string;
  label: string;
  value: string;
  onChange: (e: React.ChangeEvent<HTMLInputElement>) => void;
  error?: string;
  type?: "text" | "number" | "url";
  placeholder?: string;
  min?: number;
  max?: number;
  step?: number;
  hint?: string;
  /** data-testid prefix; e.g. "start-roast" → "start-roast-name". */
  testIdPrefix: string;
  /** Span both grid columns (for wide fields). */
  wide?: boolean;
  /** Draft-review provenance (#627) — omit for a field the server never
   *  tracks (only the four typed fields carry one). */
  provenance?: FieldSourceValue;
  /** Draft-review captured vendor quote (#627) — omit when none was
   *  captured. */
  evidenceQuote?: string;
}

/** A labelled text/number input with an inline error + always-visible hint. */
export function Field({
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
  testIdPrefix,
  wide,
  provenance,
  evidenceQuote,
}: FieldProps): React.JSX.Element {
  const errorId = `${testIdPrefix}-${id}-error`;
  const hintId = `${testIdPrefix}-${id}-hint`;
  const describedBy = error !== undefined ? errorId : hint !== undefined ? hintId : undefined;
  return (
    <div className={cn("flex flex-col gap-1", wide && "sm:col-span-2")}>
      {provenance === undefined ? (
        <label
          htmlFor={`${testIdPrefix}-${id}`}
          className="text-xs font-semibold uppercase tracking-wide text-muted-foreground"
        >
          {label}
        </label>
      ) : (
        <div className="flex items-center gap-2">
          <label
            htmlFor={`${testIdPrefix}-${id}`}
            className="text-xs font-semibold uppercase tracking-wide text-muted-foreground"
          >
            {label}
          </label>
          <ProvenanceBadge source={provenance} />
        </div>
      )}
      <input
        id={`${testIdPrefix}-${id}`}
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
        data-testid={`${testIdPrefix}-${id}`}
        className={cn(
          "rounded-md border bg-background px-3 py-2 text-sm outline-none transition-colors focus:ring-1 focus:ring-ring",
          error !== undefined ? "border-roast-fault/70" : "border-input",
        )}
      />
      {error !== undefined ? (
        <span
          id={errorId}
          data-testid={`${testIdPrefix}-${id}-error`}
          className="text-xs text-roast-fault"
        >
          {error}
        </span>
      ) : (
        hint !== undefined && (
          <span id={hintId} className="text-xs text-muted-foreground">
            {hint}
          </span>
        )
      )}
      {evidenceQuote !== undefined && (
        <EvidenceQuote quote={evidenceQuote} testId={`${testIdPrefix}-${id}-evidence`} />
      )}
    </div>
  );
}

interface SelectProps {
  id: string;
  label: string;
  value: string;
  onChange: (e: React.ChangeEvent<HTMLSelectElement>) => void;
  options: { value: string; label: string }[];
  hint: string;
  testIdPrefix: string;
  /** Draft-review provenance (#627) — omit for a field the server never
   *  tracks. */
  provenance?: FieldSourceValue;
  /** Draft-review captured vendor quote (#627) — omit when none was
   *  captured. */
  evidenceQuote?: string;
}

/** A labelled constrained-vocabulary select with an always-visible hint. */
function Select({
  id,
  label,
  value,
  onChange,
  options,
  hint,
  testIdPrefix,
  provenance,
  evidenceQuote,
}: SelectProps): React.JSX.Element {
  const hintId = `${testIdPrefix}-${id}-hint`;
  return (
    <div className="flex flex-col gap-1">
      {provenance === undefined ? (
        <label
          htmlFor={`${testIdPrefix}-${id}`}
          className="text-xs font-semibold uppercase tracking-wide text-muted-foreground"
        >
          {label}
        </label>
      ) : (
        <div className="flex items-center gap-2">
          <label
            htmlFor={`${testIdPrefix}-${id}`}
            className="text-xs font-semibold uppercase tracking-wide text-muted-foreground"
          >
            {label}
          </label>
          <ProvenanceBadge source={provenance} />
        </div>
      )}
      <select
        id={`${testIdPrefix}-${id}`}
        name={id}
        value={value}
        onChange={onChange}
        aria-describedby={hintId}
        data-testid={`${testIdPrefix}-${id}`}
        className="rounded-md border border-input bg-background px-3 py-2 text-sm outline-none transition-colors focus:ring-1 focus:ring-ring"
      >
        {options.map((opt) => (
          <option key={opt.value} value={opt.value}>
            {opt.label}
          </option>
        ))}
      </select>
      <span id={hintId} className="text-xs text-muted-foreground">
        {hint}
      </span>
      {evidenceQuote !== undefined && (
        <EvidenceQuote quote={evidenceQuote} testId={`${testIdPrefix}-${id}-evidence`} />
      )}
    </div>
  );
}

export interface BeanProfileFieldsProps {
  draft: BeanProfileDraft;
  errors: BeanProfileErrors;
  onChange: (field: keyof BeanProfileDraft, value: string) => void;
  onBlendChange: (checked: boolean) => void;
  /** data-testid prefix so the inline form + the modals keep distinct ids. */
  testIdPrefix: string;
  /** Whether to render the `default_bean_weight_grams` field. The inline Start
   *  form omits it (it owns a per-roast charge weight instead); the modals show it. */
  showDefaultWeight: boolean;
}

/**
 * The shared bean-identity + roast-target field group. Rendered identically in the
 * inline Start form and the add/edit modals so the captured fields never drift.
 * The parent owns the draft/errors state and the submit; this is presentation only.
 */
export function BeanProfileFields({
  draft,
  errors,
  onChange,
  onBlendChange,
  testIdPrefix,
  showDefaultWeight,
}: BeanProfileFieldsProps): React.JSX.Element {
  const set =
    (field: keyof BeanProfileDraft) =>
    (
      e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>,
    ) =>
      onChange(field, e.target.value);

  return (
    <>
      <fieldset className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <legend className="col-span-full text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          Bean identity
        </legend>
        <Field
          id="name"
          label="Profile name"
          value={draft.name}
          onChange={set("name")}
          error={errors.name}
          placeholder="Morning batch"
          testIdPrefix={testIdPrefix}
        />
        <Field
          id="bean_origin"
          label="Bean origin"
          value={draft.bean_origin}
          onChange={set("bean_origin")}
          error={errors.bean_origin}
          placeholder="Ethiopia Guji"
          testIdPrefix={testIdPrefix}
        />
        <Field
          id="country"
          label="Country (optional)"
          value={draft.country}
          onChange={set("country")}
          error={errors.country}
          placeholder="Ethiopia"
          hint="Producing country"
          testIdPrefix={testIdPrefix}
        />
        <Field
          id="farm"
          label="Farm / region (optional)"
          value={draft.farm}
          onChange={set("farm")}
          error={errors.farm}
          placeholder="Gedeb — Worka Sakaro"
          hint="Farm / co-op / washing station / region"
          testIdPrefix={testIdPrefix}
        />
        <Select
          id="bean_species"
          label="Species (optional)"
          value={draft.bean_species}
          onChange={set("bean_species")}
          options={SPECIES_OPTIONS}
          hint="Botanical species — distinct from cultivar"
          testIdPrefix={testIdPrefix}
          provenance={fieldSourceFor(draft, "bean_species")}
          evidenceQuote={fieldEvidenceFor(draft, "bean_species")}
        />
        <Field
          id="bean_varietal"
          label="Varietal / cultivar (optional)"
          value={draft.bean_varietal}
          onChange={set("bean_varietal")}
          error={errors.bean_varietal}
          placeholder="Heirloom"
          hint="Cultivar — distinct from species"
          testIdPrefix={testIdPrefix}
        />
        <Select
          id="processing"
          label="Processing (optional)"
          value={draft.processing}
          onChange={set("processing")}
          options={PROCESSING_OPTIONS}
          hint="Post-harvest process"
          testIdPrefix={testIdPrefix}
          provenance={fieldSourceFor(draft, "processing")}
          evidenceQuote={fieldEvidenceFor(draft, "processing")}
        />
        <Field
          id="altitude_m"
          label="Altitude (m, optional)"
          type="number"
          min={ALTITUDE_MIN_M}
          max={ALTITUDE_MAX_M}
          step={1}
          value={draft.altitude_m}
          onChange={set("altitude_m")}
          error={errors.altitude_m}
          placeholder="2100"
          hint={`Growing altitude, ${ALTITUDE_MIN_M}–${ALTITUDE_MAX_M} m`}
          testIdPrefix={testIdPrefix}
          provenance={fieldSourceFor(draft, "altitude_m")}
          evidenceQuote={fieldEvidenceFor(draft, "altitude_m")}
        />
        <BlendToggle
          checked={draft.is_blend}
          onChange={(e) => onBlendChange(e.target.checked)}
          testIdPrefix={testIdPrefix}
          provenance={fieldSourceFor(draft, "is_blend")}
          evidenceQuote={fieldEvidenceFor(draft, "is_blend")}
        />
        <DescriptionField
          value={draft.description}
          onChange={set("description")}
          isBlend={draft.is_blend}
          testIdPrefix={testIdPrefix}
        />
        <Field
          id="source_url"
          label="Product URL (optional)"
          type="url"
          value={draft.source_url}
          onChange={set("source_url")}
          error={errors.source_url}
          placeholder="https://roaster.example.com/the-bean"
          hint="Where the bean was bought — shown as a link on the roast"
          testIdPrefix={testIdPrefix}
          wide
        />
      </fieldset>

      <fieldset className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <legend className="col-span-full text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          Roast targets
        </legend>
        {showDefaultWeight && (
          <Field
            id="default_bean_weight_grams"
            label="Default weight (g)"
            type="number"
            min={1}
            step={1}
            value={draft.default_bean_weight_grams}
            onChange={set("default_bean_weight_grams")}
            error={errors.default_bean_weight_grams}
            hint="Pre-fills each roast; adjustable per roast"
            testIdPrefix={testIdPrefix}
          />
        )}
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
          testIdPrefix={testIdPrefix}
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
          testIdPrefix={testIdPrefix}
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
          testIdPrefix={testIdPrefix}
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
          testIdPrefix={testIdPrefix}
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
          testIdPrefix={testIdPrefix}
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
          testIdPrefix={testIdPrefix}
        />
      </fieldset>
    </>
  );
}

interface BlendToggleProps {
  checked: boolean;
  onChange: (e: React.ChangeEvent<HTMLInputElement>) => void;
  testIdPrefix: string;
  /** Draft-review provenance (#627) — omit for a field the server never
   *  tracks. */
  provenance?: FieldSourceValue;
  /** Draft-review captured vendor quote (#627) — omit when none was
   *  captured. */
  evidenceQuote?: string;
}

/** Single-origin vs blend toggle (#164). */
function BlendToggle({
  checked,
  onChange,
  testIdPrefix,
  provenance,
  evidenceQuote,
}: BlendToggleProps): React.JSX.Element {
  return (
    <div className="flex flex-col gap-1 sm:col-span-2">
      {provenance === undefined ? (
        <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          Blend
        </span>
      ) : (
        <div className="flex items-center gap-2">
          <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Blend
          </span>
          <ProvenanceBadge source={provenance} />
        </div>
      )}
      <label htmlFor={`${testIdPrefix}-is_blend`} className="flex items-center gap-2 text-sm">
        <input
          id={`${testIdPrefix}-is_blend`}
          name="is_blend"
          type="checkbox"
          checked={checked}
          onChange={onChange}
          data-testid={`${testIdPrefix}-is_blend`}
          className="h-4 w-4 rounded border-input bg-background accent-roast-coffee"
        />
        <span>This is a blend (not single-origin)</span>
      </label>
      <span className="text-xs text-muted-foreground">
        {checked
          ? "Put the secondary beans / components in the description below."
          : "Single origin — leave off, or turn on for a blend."}
      </span>
      {evidenceQuote !== undefined && (
        <EvidenceQuote quote={evidenceQuote} testId={`${testIdPrefix}-is_blend-evidence`} />
      )}
    </div>
  );
}

interface DescriptionFieldProps {
  value: string;
  onChange: (e: React.ChangeEvent<HTMLTextAreaElement>) => void;
  isBlend: boolean;
  testIdPrefix: string;
}

/** Free-text description (#164): process, tasting notes, lot, and — for a blend —
 *  the secondary beans. */
function DescriptionField({
  value,
  onChange,
  isBlend,
  testIdPrefix,
}: DescriptionFieldProps): React.JSX.Element {
  const hintId = `${testIdPrefix}-description-hint`;
  const hint = isBlend
    ? "Process, tasting notes, and the secondary beans / components of the blend."
    : "Process (washed/natural/honey), tasting notes, lot.";
  return (
    <div className="flex flex-col gap-1 sm:col-span-2">
      <label
        htmlFor={`${testIdPrefix}-description`}
        className="text-xs font-semibold uppercase tracking-wide text-muted-foreground"
      >
        Description (optional)
      </label>
      <textarea
        id={`${testIdPrefix}-description`}
        name="description"
        rows={2}
        value={value}
        onChange={onChange}
        aria-describedby={hintId}
        data-testid={`${testIdPrefix}-description`}
        placeholder={
          isBlend
            ? "60% Brazil Cerrado + 40% Ethiopia Guji; washed; chocolate, citrus."
            : "Washed; jasmine, bergamot, stone fruit."
        }
        className="rounded-md border border-input bg-background px-3 py-2 text-sm outline-none transition-colors focus:ring-1 focus:ring-ring"
      />
      <span id={hintId} className="text-xs text-muted-foreground">
        {hint}
      </span>
    </div>
  );
}
