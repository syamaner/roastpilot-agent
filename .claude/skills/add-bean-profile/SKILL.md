---
name: add-bean-profile
description: Create a bean profile from a supplier product URL plus the operator's specifics — fetch the page, extract the bean identity, map it to a BeanProfile, apply the per-origin roast-target priors, and add it so it is pickable for the next roast. Use when asked to add/create a bean profile from a supplier link.
---

Turn a supplier URL (e.g. Redber) + any operator overrides into a ready-to-pick
bean profile. All temperatures Celsius. Heat/fan are read-outs, not dials.

## 1. Fetch + extract

WebFetch the supplier URL and pull the bean identity. Map to the
`BeanProfile` / `BeanProfileInput` fields (see `src/roastpilot_agent/seed.py` for
a worked example and `models.py` `_BeanProfileFieldsBase` for the schema):

| Field | Source | Notes |
|---|---|---|
| `name` | product title | e.g. "Colombia Excelso Huila (Washed)" |
| `bean_origin`, `country` | origin | |
| `farm` | farm / region / co-op / washing station | optional |
| `bean_varietal` | variety | cultivar string, e.g. "Caturra, Typica, Bourbon" |
| `processing` | process | **Literal**: washed / natural / honey / anaerobic / wet_hulled / other |
| `bean_species` | species | **Literal**: arabica / robusta / liberica / excelsa (default arabica) |
| `altitude_m` | altitude | int 0–4000; for a range, store a representative central value, put the range in `description` |
| `is_blend` | blend? | false for single-origin even with a varietal mix |
| `description` | tasting notes + process + altitude range | free text |
| `source_url` | the URL | must be a well-formed http(s) link |

## 2. Apply the roast targets (per-origin priors + first-roast de-risk)

Defaults unless the operator overrides — confirm the weight + drop/dev direction with them:
- `charge_guidance_min_c` / `max_c`: **170 / 200** (the proven Hottop range; max must stay ≤ the pre-T0 safety bound).
- `initial_heat_percent` / `initial_fan_percent`: **100 / 30**. Leave `pre_fc_heat` / `pre_fc_fan` **null** (controller defaults + the #336 trim) unless the operator wants per-bean pre-FC levers.
- `target_drop_temp_c`: **195** — the operator's proven known-good drop line (bitter >196). Conservative ceiling.
- `target_development_percent`: apply the per-origin prior (memory `per-origin-dtr-washed-highgrown`): a washed high-grown bean's *eventual* medium is ~18 %, but **de-risk the FIRST roast on a new bean to ~13 %** (light → taste → go darker; audio FC lags ~30 s so true dev runs higher than the number). A delicate natural stays ~13 %.
- `default_bean_weight_grams`: ask (e.g. 250 g for a 1 kg / 4-batch bag).

State the chosen targets and the reasoning back to the operator before adding.

## 3. Add it

**Default — runtime library (D45), no code change / restart:** POST the
`BeanProfileInput` JSON (the fields above + `default_bean_weight_grams`, WITHOUT
id/created_at/updated_at — the store mints those) to the live server:

!`curl -sf http://127.0.0.1:8000/api/bean-profiles >/dev/null && echo "server up — POST /api/bean-profiles with the BeanProfileInput JSON" || echo "server down — start roast-live.sh, or use the seed option below"`

Then confirm it lists: `GET /api/bean-profiles`. It is now pickable in the
Start-Roast dropdown.

**Option — committed seed** (version-controlled / reproducible, like the Koke +
Colombia seeds): add a `BeanProfile` with a stable `id` to `SEED_BEAN_PROFILES`
in `src/roastpilot_agent/seed.py` (idempotent insert at startup), and add the
matching locked-values test in `tests/test_bean_profiles.py`. Use this when the
bean should ship with the build / feed the corpus reproducibly.

## 4. Confirm

Report the created profile (name + the resolved targets) and that it is
selectable. Note the de-risk posture if it is a first roast on the bean.
