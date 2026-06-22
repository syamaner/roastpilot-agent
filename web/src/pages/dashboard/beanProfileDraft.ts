/**
 * Bean-profile capture draft + validation (#303, D45).
 *
 * The non-component half of the reusable bean-profile capture surface — split out
 * so the field components (`beanProfileFields.tsx`) keep a clean component-only
 * export (react-refresh). Carries the string-keyed draft, the pre-filled defaults,
 * the select-option vocabularies, the numeric bounds, and the parse + bounds
 * validation shared by the inline Start form and the add/edit modals so they cannot
 * drift (extracted from the original `StartRoastForm`, #158/#164/#291).
 *
 * The draft is the SAVED-profile shape: it carries `default_bean_weight_grams` (the
 * per-roast charge weight pre-fill), NOT the per-roast `bean_weight_grams`, which
 * the Start form owns separately. All temperatures are Celsius — these client-side
 * bounds are defense-in-depth + UX, never the authority (the server's pydantic
 * bounds + `SafetyLimits` are).
 */

import type {
  BeanProfileInput,
  BeanSpecies,
  ProcessingMethod,
} from "@/lib/types";

/**
 * The string-keyed draft for the shared bean fields (text/number inputs are
 * strings; parsed on submit). `is_blend` is a boolean checkbox. The saved-profile
 * shape: it carries `default_bean_weight_grams`, not the per-roast
 * `bean_weight_grams` (which the Start form owns separately).
 */
export interface BeanProfileDraft {
  name: string;
  bean_origin: string;
  bean_varietal: string;
  country: string;
  farm: string;
  bean_species: string;
  is_blend: boolean;
  description: string;
  processing: string;
  altitude_m: string;
  source_url: string;
  charge_guidance_min_c: string;
  charge_guidance_max_c: string;
  initial_heat_percent: string;
  initial_fan_percent: string;
  target_drop_temp_c: string;
  target_development_percent: string;
  default_bean_weight_grams: string;
}

/** Field-level validation errors, keyed by draft field. */
export type BeanProfileErrors = Partial<Record<keyof BeanProfileDraft, string>>;

/** The botanical species select options (#164); mirrors `BeanSpecies`. The empty
 *  value is "unset" → `null` on submit. */
export const SPECIES_OPTIONS: { value: BeanSpecies | ""; label: string }[] = [
  { value: "", label: "—" },
  { value: "arabica", label: "Arabica" },
  { value: "robusta", label: "Robusta" },
  { value: "liberica", label: "Liberica" },
  { value: "excelsa", label: "Excelsa" },
];

/** The processing-method select options (#291); mirrors `ProcessingMethod`. The
 *  empty value is "unset" → `null` on submit. */
export const PROCESSING_OPTIONS: { value: ProcessingMethod | ""; label: string }[] = [
  { value: "", label: "—" },
  { value: "washed", label: "Washed" },
  { value: "natural", label: "Natural" },
  { value: "honey", label: "Honey" },
  { value: "anaerobic", label: "Anaerobic" },
  { value: "wet_hulled", label: "Wet-hulled" },
  { value: "other", label: "Other" },
];

/** Growing-altitude bounds (#291) — mirrors `models` altitude_m (0–4000 m). */
export const ALTITUDE_MIN_M = 0;
export const ALTITUDE_MAX_M = 4000;

/**
 * Conservative Hottop-realistic Celsius bounds for the temperature inputs — wide
 * enough for any sane roast, tight enough to reject a fat-finger like `2050`.
 * Defense-in-depth + UX only; the server's `SafetyLimits` remains the authority.
 */
export const TEMP_MIN_C = 100;
export const TEMP_MAX_C = 240;

/** Pre-filled defaults (mirror `models` field defaults + the empirical median). */
export const DEFAULT_BEAN_PROFILE_DRAFT: BeanProfileDraft = {
  name: "",
  bean_origin: "",
  bean_varietal: "",
  country: "",
  farm: "",
  bean_species: "",
  is_blend: false,
  description: "",
  processing: "",
  altitude_m: "",
  source_url: "",
  charge_guidance_min_c: "170",
  charge_guidance_max_c: "200",
  initial_heat_percent: "70",
  initial_fan_percent: "40",
  // Aligned with prompt v4 + the operator's empirical median (drop 195 °C / 15 % DTR).
  target_drop_temp_c: "195",
  target_development_percent: "15",
  default_bean_weight_grams: "250",
};

/** Build a draft from a saved `BeanProfileInput` (modal edit pre-fill). */
export function draftFromBeanProfile(profile: BeanProfileInput): BeanProfileDraft {
  return {
    name: profile.name,
    bean_origin: profile.bean_origin,
    bean_varietal: profile.bean_varietal ?? "",
    country: profile.country ?? "",
    farm: profile.farm ?? "",
    bean_species: profile.bean_species ?? "",
    is_blend: profile.is_blend ?? false,
    description: profile.description ?? "",
    processing: profile.processing ?? "",
    altitude_m: profile.altitude_m == null ? "" : String(profile.altitude_m),
    source_url: profile.source_url ?? "",
    charge_guidance_min_c: String(profile.charge_guidance_min_c),
    charge_guidance_max_c: String(profile.charge_guidance_max_c),
    initial_heat_percent: String(profile.initial_heat_percent),
    initial_fan_percent: String(profile.initial_fan_percent),
    target_drop_temp_c: String(profile.target_drop_temp_c),
    target_development_percent: String(profile.target_development_percent),
    default_bean_weight_grams: String(profile.default_bean_weight_grams),
  };
}

/**
 * Whether a string parses as an absolute http(s) URL with a host (#315). Mirrors
 * the server's `source_url` validator: a non-http(s) scheme (e.g. `javascript:`,
 * `ftp:`), a missing host, embedded userinfo (`user:pass@host` — a credential
 * that must never persist or render into an anchor), or an unparseable value
 * (incl. a malformed port, which the platform `URL` parser rejects) is rejected,
 * so the UI never renders a broken anchor. Uses the platform `URL` parser (no
 * extra dependency).
 */
function isWellFormedHttpUrl(value: string): boolean {
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    return false;
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return false;
  if (parsed.hostname === "") return false;
  // Reject embedded credentials (userinfo).
  if (parsed.username !== "" || parsed.password !== "") return false;
  return true;
}

/**
 * Parse + bounds-check the shared bean fields. Returns the typed
 * `BeanProfileInput` (saved-profile shape, with `default_bean_weight_grams`) or
 * the field errors. The Start form layers its per-roast weight on top.
 */
export function validateBeanProfile(
  draft: BeanProfileDraft,
): { input: BeanProfileInput } | { errors: BeanProfileErrors } {
  const errors: BeanProfileErrors = {};

  const name = draft.name.trim();
  if (name === "") errors.name = "Required.";
  const beanOrigin = draft.bean_origin.trim();
  if (beanOrigin === "") errors.bean_origin = "Required.";
  const varietalRaw = draft.bean_varietal.trim();
  const countryRaw = draft.country.trim();
  const farmRaw = draft.farm.trim();
  const descriptionRaw = draft.description.trim();
  const species = draft.bean_species === "" ? null : (draft.bean_species as BeanSpecies);

  // #315: optional product URL. Blank → null; otherwise it must parse as an
  // absolute http(s) URL with a host (mirrors the server's `source_url` validator
  // so a broken anchor never reaches the corpus or the UI). Defense-in-depth + UX
  // — the server's pydantic validator remains the authority.
  const sourceUrlRaw = draft.source_url.trim();
  let sourceUrl: string | null = null;
  if (sourceUrlRaw !== "") {
    if (isWellFormedHttpUrl(sourceUrlRaw)) {
      sourceUrl = sourceUrlRaw;
    } else {
      errors.source_url = "Must be a http(s):// URL.";
    }
  }
  const processing =
    draft.processing === "" ? null : (draft.processing as ProcessingMethod);

  const altitudeRaw = draft.altitude_m.trim();
  let altitudeM: number | null = null;
  if (altitudeRaw !== "") {
    const altitude = Number(altitudeRaw);
    if (
      !Number.isInteger(altitude) ||
      altitude < ALTITUDE_MIN_M ||
      altitude > ALTITUDE_MAX_M
    ) {
      errors.altitude_m = `${ALTITUDE_MIN_M}–${ALTITUDE_MAX_M} m, whole number.`;
    } else {
      altitudeM = altitude;
    }
  }

  const defaultWeight = Number(draft.default_bean_weight_grams);
  if (
    draft.default_bean_weight_grams.trim() === "" ||
    !Number.isFinite(defaultWeight) ||
    defaultWeight <= 0
  )
    errors.default_bean_weight_grams = "Must be greater than 0.";

  const tempRange = `${TEMP_MIN_C}–${TEMP_MAX_C} °C.`;
  const inTempRange = (v: number) => Number.isFinite(v) && v >= TEMP_MIN_C && v <= TEMP_MAX_C;

  const minC = Number(draft.charge_guidance_min_c);
  if (!inTempRange(minC)) errors.charge_guidance_min_c = tempRange;
  const maxC = Number(draft.charge_guidance_max_c);
  if (!inTempRange(maxC)) errors.charge_guidance_max_c = tempRange;
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
    input: {
      name,
      bean_origin: beanOrigin,
      bean_varietal: varietalRaw === "" ? null : varietalRaw,
      country: countryRaw === "" ? null : countryRaw,
      farm: farmRaw === "" ? null : farmRaw,
      bean_species: species,
      is_blend: draft.is_blend,
      description: descriptionRaw === "" ? null : descriptionRaw,
      processing,
      altitude_m: altitudeM,
      source_url: sourceUrl,
      charge_guidance_min_c: minC,
      charge_guidance_max_c: maxC,
      initial_heat_percent: heat,
      initial_fan_percent: fan,
      target_drop_temp_c: drop,
      target_development_percent: dev,
      default_bean_weight_grams: defaultWeight,
    },
  };
}
