/**
 * Fixed bean-profile fixtures for tests + the snapshot harness (#303).
 *
 * Mirrors the server's `BeanProfile` wire shape (models.BeanProfile) — every
 * `RoastProfile` field except the per-roast `bean_weight_grams`, plus the
 * server-owned id + timestamps and `default_bean_weight_grams`. The Koke entry
 * mirrors the built-in seed (seed.py) so the harness/tests render the same profile
 * the device ships with. All temperatures Celsius.
 */

import type { BeanProfile, BeanProfileDraftResponse } from "@/lib/types";

/** The built-in Ethiopia Yirgacheffe Koke seed (mirrors seed.ETHIOPIA_KOKE_SEED). */
export const FIXTURE_KOKE: BeanProfile = {
  id: "seed-ethiopia-yirgacheffe-koke-natural",
  created_at: "2026-06-21T00:00:00+00:00",
  updated_at: "2026-06-21T00:00:00+00:00",
  name: "Ethiopia Yirgacheffe Koke (Natural)",
  bean_origin: "Ethiopia",
  country: "Ethiopia",
  farm: "Koke Washing Station",
  bean_varietal: "Dega, Kudhume, Wolisho",
  bean_species: "arabica",
  is_blend: false,
  processing: "natural",
  altitude_m: 1885,
  source_url: "https://redber.co.uk/products/ethiopia-yirgacheffe-koke",
  charge_guidance_min_c: 170,
  charge_guidance_max_c: 200,
  initial_heat_percent: 100,
  initial_fan_percent: 30,
  target_drop_temp_c: 190,
  target_development_percent: 13,
  default_bean_weight_grams: 250,
  description:
    "Yirgacheffe, Gedeo Zone. SCA 88. Coconut / white grape / lime. Natural process.",
};

/** A second saved profile (a washed Colombian) so the dropdown shows >1 entry. */
export const FIXTURE_COLOMBIA: BeanProfile = {
  id: "profile-colombia-huila-washed",
  created_at: "2026-06-10T08:00:00+00:00",
  updated_at: "2026-06-12T09:30:00+00:00",
  name: "Colombia Huila (Washed)",
  bean_origin: "Colombia",
  country: "Colombia",
  farm: "Finca El Mirador",
  bean_varietal: "Caturra",
  bean_species: "arabica",
  is_blend: false,
  processing: "washed",
  altitude_m: 1700,
  charge_guidance_min_c: 175,
  charge_guidance_max_c: 205,
  initial_heat_percent: 80,
  initial_fan_percent: 40,
  target_drop_temp_c: 198,
  target_development_percent: 18,
  default_bean_weight_grams: 220,
  description: "Washed; caramel, red apple, milk chocolate.",
};

/** The saved library the dropdown renders from. */
export const FIXTURE_BEAN_PROFILES: BeanProfile[] = [FIXTURE_KOKE, FIXTURE_COLOMBIA];

/** A `POST /api/beans/draft-from-url` response (#573 phase 1, #627), for
 *  draft-from-URL tests (#637): one on-page typed field with a captured
 *  evidence quote, one origin-estimated typed field with none — exercises
 *  both provenance states through the real seeding path. */
export const FIXTURE_DRAFT_RESPONSE: BeanProfileDraftResponse = {
  draft_attempt_id: "0123456789abcdef0123456789abcdef",
  name: "Guji Uraga Natural",
  bean_origin: "Ethiopia",
  bean_varietal: "Heirloom",
  country: "Ethiopia",
  farm: "Uraga",
  bean_species: "arabica",
  is_blend: false,
  description: "Natural process; blueberry, jasmine.",
  processing: "natural",
  altitude_m: 2000,
  source_url: "https://roaster.example.com/guji-uraga",
  charge_guidance_min_c: 170,
  charge_guidance_max_c: 200,
  initial_heat_percent: 70,
  initial_fan_percent: 40,
  target_drop_temp_c: 195,
  target_development_percent: 15,
  default_bean_weight_grams: 250,
  field_sources: { processing: "on_page", altitude_m: "origin_estimated" },
  field_evidence: { processing: "Naturally processed on raised beds." },
  scouting_note: "Scouting run — this is the FIRST roast on this bean.",
};
