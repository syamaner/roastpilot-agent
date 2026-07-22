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
  BeanProfileDraftResponse,
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
  /**
   * Marks `is_blend` as an UNRESOLVED tri-state from a draft-from-URL response
   * (#637): the vendor page never addressed blending at all (the wire value was
   * `null`), so `is_blend` above is a safe-default `false`, not an operator- or
   * vendor-confirmed "single origin". `true` while unresolved; absent (never
   * `false`) once resolved — the same "absent means unset" convention as
   * `field_sources`/`field_evidence`. `validateBeanProfile` blocks save while this
   * is `true`; `withFieldEdited` clears it the moment the operator picks blend or
   * single-origin explicitly, on the same edit that would otherwise silently
   * persist the un-chosen default.
   */
  is_blend_unresolved?: boolean;
  /**
   * Model-cited verbatim vendor-page quotes for the four typed fields
   * (`altitude_m`, `processing`, `bean_species`, `is_blend`), keyed the
   * same way as the server's `field_sources` map (#627) — mirrors
   * `models.BeanProfileDraft.field_evidence`. Untrusted vendor page text;
   * present only when a quote was captured. Read only via `fieldEvidenceFor`
   * below, which whitelists the key — never index this map directly with an
   * unvalidated key (#627b).
   */
  field_evidence?: Record<string, string>;
  /**
   * Per-field provenance, mirroring `models.BeanProfileDraft.field_sources`
   * (`BeanFieldSource`): `"on_page"` when the server read the value from the
   * vendor page text, `"origin_estimated"` when it was imputed (a
   * conservative first-roast target, or a value the page never stated). The
   * draft-review UI (#627b) surfaces this only for the four typed fields the
   * evidence quotes cover — read via `fieldSourceFor`, never indexed
   * directly with an unvalidated key.
   */
  field_sources?: Record<string, string>;
}

/** The four typed fields whose provenance + captured evidence quote the
 *  draft-review UI surfaces (#627) — every automated citation gate for these
 *  fields is permanently parked, so the operator judges from the quote
 *  instead. The ONLY keys ever read from `field_sources`/`field_evidence`:
 *  both maps are server-controlled but conceptually untrusted (vendor-page
 *  derived), so a key outside this whitelist is ignored rather than
 *  rendered. */
export const PROVENANCE_TRACKED_FIELDS = [
  "altitude_m",
  "processing",
  "bean_species",
  "is_blend",
] as const;

export type ProvenanceTrackedField = (typeof PROVENANCE_TRACKED_FIELDS)[number];

function isProvenanceTrackedField(field: string): field is ProvenanceTrackedField {
  return (PROVENANCE_TRACKED_FIELDS as readonly string[]).includes(field);
}

/** The two `field_sources` values the server ever emits (`BeanFieldSource`,
 *  models.py) — mirrors the Literal, not an enum (bean metadata, not a
 *  safety verdict). */
export type FieldSourceValue = "on_page" | "origin_estimated";

/**
 * The `field_sources` provenance for one of the four typed fields, or
 * `undefined` when the field isn't tracked, has no entry (unset/absent — the
 * same "absent means unset" convention as the server), or holds a value this
 * UI doesn't recognise. Whitelists both the KEY (only a tracked typed field)
 * and the VALUE (only the two known literals) — defense-in-depth against a
 * future/unexpected server value rendering as something it isn't.
 */
export function fieldSourceFor(
  draft: BeanProfileDraft,
  field: ProvenanceTrackedField,
): FieldSourceValue | undefined {
  if (!isProvenanceTrackedField(field)) return undefined;
  const value = draft.field_sources?.[field];
  return value === "on_page" || value === "origin_estimated" ? value : undefined;
}

/**
 * The captured vendor-page quote for one of the four typed fields, or
 * `undefined` when absent or the field isn't tracked. Whitelists the KEY —
 * the value is UNTRUSTED vendor page text; callers must render it through
 * React's default escaping only (never `dangerouslySetInnerHTML`, never
 * built into an HTML string, never linkified).
 */
export function fieldEvidenceFor(
  draft: BeanProfileDraft,
  field: ProvenanceTrackedField,
): string | undefined {
  if (!isProvenanceTrackedField(field)) return undefined;
  return draft.field_evidence?.[field];
}

/**
 * Applies an operator edit to `field` on the draft, immutably — AND drops
 * that field's `field_sources`/`field_evidence` entries, if any (#627
 * Codex round-2): provenance describes the value the server EXTRACTED from
 * the vendor page; once the operator edits the field, carrying the old
 * badge/quote forward would falsely attribute the operator's new value to
 * the vendor page. Applies to every keyed field, not just the four typed
 * ones — a `field_sources`-tracked free-text field (e.g. `bean_varietal`)
 * is subject to the same false-attribution risk.
 *
 * Only rebuilds a map when it actually carries an entry for this field, so
 * editing an untracked field (the common case — no draft-from-URL flow is
 * wired up yet) leaves `field_sources`/`field_evidence` at the SAME
 * reference as before: no spurious object churn for anything memoised on
 * them.
 *
 * Editing `is_blend` specifically also clears `is_blend_unresolved` (#637):
 * an explicit true/false choice from the operator IS the resolution, on the
 * same edit that would otherwise leave the un-chosen safe-default in place —
 * there is no separate "confirm" step for this field.
 */
export function withFieldEdited<K extends keyof BeanProfileDraft>(
  draft: BeanProfileDraft,
  field: K,
  value: BeanProfileDraft[K],
): BeanProfileDraft {
  const next: BeanProfileDraft = { ...draft, [field]: value };
  if (draft.field_sources !== undefined && field in draft.field_sources) {
    const { [field as string]: _removed, ...rest } = draft.field_sources;
    next.field_sources = rest;
  }
  if (draft.field_evidence !== undefined && field in draft.field_evidence) {
    const { [field as string]: _removed, ...rest } = draft.field_evidence;
    next.field_evidence = rest;
  }
  if (field === "is_blend") {
    next.is_blend_unresolved = undefined;
  }
  return next;
}

/**
 * Unicode bidi formatting control characters, as explicit `\u` escapes
 * (never the literal control characters themselves — embedding a real bidi
 * override in this source file would be its own Trojan-Source-style
 * footgun): ALM (U+061C), LRM/RLM (U+200E–U+200F), LRE/RLE/PDF/LRO/RLO
 * (U+202A–U+202E), and LRI/RLI/FSI/PDI (U+2066–U+2069). A fixed
 * character-class regex on exact codepoints, not user-influenced — no ReDoS
 * surface.
 */
const BIDI_CONTROL_CHARS = /[\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]/g;

/**
 * Strips Unicode bidi formatting controls from `text` before display (#627
 * retro finding on #634): `dir="ltr"` + `unicode-bidi: isolate` on the
 * quote's container only shields the SURROUNDING layout — a U+202E (or any
 * other override/embedding/isolate control) INSIDE the quote still reorders
 * the quote's own rendered characters, so a malicious vendor page could make
 * a "page says" quoted-authority display visually claim something the
 * underlying text doesn't. Bidi controls carry no legitimate content in a
 * quoted-authority display; stripping is fail-closed (retro finding on
 * #634). Applied to quote TEXT only, never to labels/hints/other UI copy —
 * the `dir`/isolate attributes stay as defence-in-depth for anything this
 * misses.
 */
export function stripBidiControls(text: string): string {
  return text.replace(BIDI_CONTROL_CHARS, "");
}

/**
 * Strips the query string from any http(s) URL-shaped substring in `text`
 * (#654 round 2 fold 4): some backend 422 detail messages embed the
 * requested URL verbatim (e.g. `drafted bean profile failed validation for
 * 'https://x.test/p?token=...'`), and a signed/token-bearing query string on
 * that URL must never render on screen. Conservative by construction: the
 * match REQUIRES an `http(s)://` prefix before it ever considers a `?`, so a
 * `?` in ordinary message text (not part of a URL) is never touched — only
 * everything from the first `?` onward within an ALREADY-matched URL span is
 * dropped. The URL/query body excludes quotes, angle brackets, and closing
 * parens in addition to whitespace — the common "URL embedded in prose"
 * delimiters (e.g. the single quotes wrapping the URL above) — so trailing
 * punctuation from the surrounding sentence is never swept into the match
 * and stripped along with the query string. No unbounded backtracking
 * (`[^...]+`/`[^...]*` are both simple negated-character-class runs) — no
 * ReDoS surface on operator-uncontrolled server text. Case-insensitive
 * (`i` flag): the scheme the backend echoes back is whatever the operator
 * typed verbatim (never normalized), so `HTTPS://…`/`Https://…` must be
 * caught the same as `https://…` — a case-sensitive match would let an
 * uppercase-scheme URL's query string escape redaction entirely.
 */
const URL_WITH_QUERY = /https?:\/\/[^\s?'"<>)]+\?[^\s'"<>)]*/gi;

export function redactUrlQueryStrings(text: string): string {
  return text.replace(URL_WITH_QUERY, (match) => match.slice(0, match.indexOf("?")));
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
 * Build a modal draft + scouting note from a `POST /api/beans/draft-from-url`
 * response (#573 phase 1, #627, #637): seeds the string-keyed form draft AND
 * carries the server's `field_sources`/`field_evidence` straight through, so
 * the existing provenance-badge/evidence-quote UI (#627) lights up with no
 * further wiring. `is_blend` is tri-state on the wire (`null` means the
 * vendor page never addressed blending at all), but this string-keyed
 * draft's checkbox is a plain boolean — `null` maps to `false` as the
 * DISPLAYED default, flagged `is_blend_unresolved: true` (#637) so the form
 * visibly marks it as an open choice rather than a confirmed single-origin,
 * and `validateBeanProfile` blocks save until the operator picks one. The
 * server never emits an `is_blend` entry in `field_sources` when the value
 * is `null` either, so the provenance badge simply doesn't render for a
 * field the page never addressed — the unresolved flag is what covers the
 * gap that badge leaves.
 *
 * Every drafted FREE-TEXT identity field (name, bean_origin, bean_varietal,
 * country, farm, description, source_url) is passed through
 * `stripBidiControls` (#654 round 2 fold 6): this is UNTRUSTED vendor-page
 * text, same as the evidence quotes — a bidi override embedded in, say, the
 * drafted `name` could make the seeded form (and, if saved unedited, the
 * saved profile and everywhere it's displayed) visually claim something
 * different from its actual character content. `bean_species`/`processing`
 * are excluded: they are constrained server-side `Literal`s, never arbitrary
 * vendor text.
 */
function strippedOrEmpty(value: string | null | undefined): string {
  return value == null ? "" : stripBidiControls(value);
}

export function draftFromBeanProfileDraft(
  response: BeanProfileDraftResponse,
): { draft: BeanProfileDraft; scoutingNote: string } {
  return {
    draft: {
      name: stripBidiControls(response.name),
      bean_origin: stripBidiControls(response.bean_origin),
      bean_varietal: strippedOrEmpty(response.bean_varietal),
      country: strippedOrEmpty(response.country),
      farm: strippedOrEmpty(response.farm),
      bean_species: response.bean_species ?? "",
      is_blend: response.is_blend ?? false,
      is_blend_unresolved: response.is_blend === null ? true : undefined,
      description: strippedOrEmpty(response.description),
      processing: response.processing ?? "",
      altitude_m: response.altitude_m == null ? "" : String(response.altitude_m),
      source_url: strippedOrEmpty(response.source_url),
      charge_guidance_min_c: String(response.charge_guidance_min_c),
      charge_guidance_max_c: String(response.charge_guidance_max_c),
      initial_heat_percent: String(response.initial_heat_percent),
      initial_fan_percent: String(response.initial_fan_percent),
      target_drop_temp_c: String(response.target_drop_temp_c),
      target_development_percent: String(response.target_development_percent),
      default_bean_weight_grams: String(response.default_bean_weight_grams),
      field_sources: response.field_sources,
      field_evidence: response.field_evidence,
    },
    scoutingNote: response.scouting_note,
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
  // #637: a draft-from-URL response whose blend flag the vendor page never
  // addressed leaves `is_blend` at a SAFE-DEFAULT `false`, flagged
  // `is_blend_unresolved` — block save until the operator explicitly picks
  // single-origin or blend, so an untouched checkbox is never mistaken for a
  // confirmed "single origin".
  if (draft.is_blend_unresolved === true) {
    errors.is_blend =
      "The vendor page didn't say — choose single-origin or blend before saving.";
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
