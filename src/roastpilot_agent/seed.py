"""Built-in seed bean profiles (#303, D45).

The bean-profile library (D45, amends D29/D7) ships with a built-in seed so the
operator has a selectable profile for the first supervised roast (#134) without
hand-entering one. The seed is a fully-formed :class:`~roastpilot_agent.models.BeanProfile`
with a *stable* id, so :meth:`~roastpilot_agent.store.RoastStore.seed_bean_profile`
can insert it idempotently (``INSERT OR IGNORE`` on the id) — a restart never
double-inserts, and an operator edit to the seeded row is never clobbered.

The Ethiopia Yirgacheffe Koke targets are a deliberately conservative-light
starting point the operator is expected to EDIT to taste, not a fixed recipe
(natural-process Yirgacheffe is delicate; the light-medium drop/DTR preserve the
fruit + florals). All temperatures are Celsius.
"""

from roastpilot_agent.models import BeanProfile

#: Fixed UTC instant stamped on every built-in seed's ``created_at`` /
#: ``updated_at``. A constant (not ``_utc_now()``) keeps the seed deterministic:
#: the idempotent insert is keyed on the stable id, so the timestamps must not
#: drift between restarts (a fresh ``now`` each boot would make the in-memory
#: seed object differ from the persisted row on every run).
_SEED_TIMESTAMP = "2026-06-21T00:00:00+00:00"

#: Stable id for the Ethiopia Yirgacheffe Koke seed (#303). Fixed so the
#: idempotent ``seed_bean_profile`` insert is a no-op after the first boot.
ETHIOPIA_KOKE_ID = "seed-ethiopia-yirgacheffe-koke-natural"

#: The Ethiopia Yirgacheffe Koke (Natural) seed profile (#303, D45) — the
#: selectable bean for the first supervised roast. Light-medium targets are a
#: conservative starting point the operator edits to taste.
ETHIOPIA_KOKE_SEED = BeanProfile(
    id=ETHIOPIA_KOKE_ID,
    created_at=_SEED_TIMESTAMP,
    updated_at=_SEED_TIMESTAMP,
    name="Ethiopia Yirgacheffe Koke (Natural)",
    bean_origin="Ethiopia",
    country="Ethiopia",
    farm="Koke Washing Station",
    bean_varietal="Dega, Kudhume, Wolisho",
    bean_species="arabica",
    is_blend=False,
    processing="natural",
    altitude_m=1885,
    source_url="https://redber.co.uk/products/ethiopia-yirgacheffe-koke",
    charge_guidance_min_c=170.0,
    charge_guidance_max_c=200.0,
    initial_heat_percent=100,
    initial_fan_percent=30,
    # Roast 2 (run c3b84625) ran past this to 196 °C and dropped slightly DARK
    # (dev only 1:09, DTR 11.6 %, under the 13 % target). 195 °C is the LATEST
    # acceptable drop for this delicate natural (operator guidance) — the advisor
    # must STRETCH development toward the DTR target before the bean reaches it.
    target_drop_temp_c=195.0,
    target_development_percent=13.0,
    default_bean_weight_grams=250.0,
    description=(
        "Yirgacheffe, Gedeo Zone. SCA 88. Coconut / white grape / lime; black tea "
        "/ blackcurrant / brown sugar. Natural process, delicate — roast "
        "light-medium to preserve fruit + florals. Targets are a "
        "conservative-light starting point; edit to taste."
    ),
)

#: Stable id for the Colombia Excelso Huila (Washed) seed (#134 roast-4 origin).
#: Fixed so the idempotent ``seed_bean_profile`` insert is a no-op after the
#: first boot and an operator edit to the seeded row is never clobbered.
COLOMBIA_HUILA_ID = "seed-colombia-excelso-huila-washed"

#: The Colombia Excelso Huila (Washed) seed profile — the second selectable bean,
#: added for the roast-4 origin (Redber GRE-COEX-BE250). A washed Colombian is
#: more forgiving than the delicate Koke natural, so the targets sit at a
#: balanced-medium starting point the operator edits to taste. The drop guide
#: stays at the operator's proven 195 °C known-good line (bitter > 196 °C). The
#: development target was STEPPED to 16 % after roast 6 landed clean (13.9 % /
#: ~190 °C), up from the first-roast de-risk of 13 % — with the default 3 pp drop
#: margin this guides the advisor to release the drop in a ~13–16 % window
#: (≈192–193 °C, still under the 195 °C ceiling). Because audio FC detection lags
#: the true crack (~30 s), the REAL development runs ~30 s longer than the number;
#: the operator's MANUAL drop (un-gated) is the backstop either way. The
#: research-backed medium for this washed high-grown bean is ~18 % DTR (corpus
#: CR-washed ~17.7 %; same-machine Roast Rebels 19 %) — keep stepping toward it as
#: the machine behaviour is dialled in. Decision 2026-06-27 (13 %), 2026-06-28
#: (→16 % for more drop flexibility). All temperatures are Celsius.
COLOMBIA_HUILA_SEED = BeanProfile(
    id=COLOMBIA_HUILA_ID,
    created_at=_SEED_TIMESTAMP,
    updated_at=_SEED_TIMESTAMP,
    name="Colombia Excelso Huila (Washed)",
    bean_origin="Colombia",
    country="Colombia",
    farm="Huila (regional Excelso lot)",
    bean_varietal="Caturra, Typica, Bourbon",
    bean_species="arabica",
    is_blend=False,
    processing="washed",
    # Huila grows 1,200–2,000 m; 1600 m is a representative central value for the
    # single ``altitude_m`` axis (the full range is in ``description``).
    altitude_m=1600,
    source_url="https://www.redber.co.uk/products/colombia-excelso-huila-green-coffee-beans",
    charge_guidance_min_c=170.0,
    charge_guidance_max_c=200.0,
    initial_heat_percent=100,
    initial_fan_percent=30,
    # 195 °C = the operator's proven known-good drop line (bitter > 196 °C); a
    # conservative ceiling for the first roast on this bean that also leaves the
    # anticipatory trim headroom against the roast-3 momentum overshoot. Edit up
    # toward a fuller medium once the trim is validated on hardware.
    target_drop_temp_c=195.0,
    # 16 % dev guide — STEPPED UP from the first-roast de-risk of 13 % after roast
    # 6 landed clean at 13.9 % / ~190 °C, toward the ~18 % research medium for this
    # washed high-grown bean (biased low for the Huila's citrus). With the default
    # 3 pp margin the advisor releases the drop in a ~13–16 % window → ~192–193 °C,
    # still under the 195 °C bitter ceiling. Audio FC lags ~30 s, so true
    # development runs ~30 s longer than the number. Drop guide stays 195 °C;
    # operator manual drop is un-gated. Decision 2026-06-28.
    target_development_percent=16.0,
    default_bean_weight_grams=250.0,  # 1 kg / 4 batches
    description=(
        "Huila, southern Colombia. Excelso grade, washed; grown 1,200–2,000 m on "
        "volcanic soil. Caturra / Typica / Bourbon. Sweet citrus + chocolate, "
        "bright acidity, round body, clean aroma. Balanced and forgiving — roast "
        "to a medium for body + sweetness. Targets are a conservative-medium "
        "starting point; edit to taste."
    ),
)

#: Stable id for the Guatemala El Durazno (White Honey) seed (Redber
#: GRE-GUED-BE250). Fixed so the idempotent ``seed_bean_profile`` insert is a
#: no-op after the first boot and an operator edit to the seeded row is never
#: clobbered.
GUATEMALA_EL_DURAZNO_ID = "seed-guatemala-el-durazno-white-honey"

#: The Guatemala El Durazno (White Honey) seed profile — a high-grown Bourbon
#: from San Pedro Pinula, Jalapa (Finca El Durazno, the Ventura family). The
#: white honey process leaves only a little mucilage, so it cups clean/bright
#: (closer to a washed than a natural). Because this is a FIRST roast on a new
#: bean, the development target sits at the conservative de-risk of 13 % — the
#: same starting point the Colombia washed used before it stepped to 16 % once
#: roast 6 landed clean. Go light, taste, then step toward the ~16–18 % medium
#: this clean high-grown bean can carry (the flavour notes — milk chocolate /
#: toasted pecan — deepen with development, but the red-apple brightness + white
#: honey clarity are lost if pushed too far). Drop guide stays at the operator's
#: proven 195 °C known-good line (bitter > 196 °C); audio FC lags ~30 s so true
#: development runs ~30 s longer than the number, and the operator's manual drop
#: is the un-gated backstop. All temperatures are Celsius.
GUATEMALA_EL_DURAZNO_SEED = BeanProfile(
    id=GUATEMALA_EL_DURAZNO_ID,
    created_at=_SEED_TIMESTAMP,
    updated_at=_SEED_TIMESTAMP,
    name="Guatemala El Durazno (White Honey)",
    bean_origin="Guatemala",
    country="Guatemala",
    farm="Finca El Durazno (Ventura family), San Pedro Pinula, Jalapa",
    bean_varietal="Bourbon",
    bean_species="arabica",
    is_blend=False,
    processing="honey",
    # Grows 1,500–2,000 m; 1750 m is a representative central value for the single
    # ``altitude_m`` axis (the full range is in ``description``).
    altitude_m=1750,
    source_url="https://www.redber.co.uk/products/guatemala-el-durazno-white-honey-process-green-coffee-beans",
    charge_guidance_min_c=170.0,
    charge_guidance_max_c=200.0,
    initial_heat_percent=100,
    initial_fan_percent=30,
    # 195 °C = the operator's proven known-good drop line (bitter > 196 °C); a
    # conservative ceiling for the first roast on this bean.
    target_drop_temp_c=195.0,
    # 13 % dev = the first-roast de-risk on a new bean (light → taste → go darker).
    # This clean high-grown white-honey can likely carry ~16–18 % eventually (like
    # the Colombia washed's trajectory), but step there only after a clean roast.
    # With the default 3 pp margin the advisor releases the drop in a ~10–13 %
    # window; the operator's manual drop is un-gated. Audio FC lags ~30 s, so true
    # development runs ~30 s longer than the number.
    target_development_percent=13.0,
    default_bean_weight_grams=250.0,
    description=(
        "San Pedro Pinula, Jalapa. Bourbon, white honey process (light mucilage — "
        "clean/bright, leaning washed); grown 1,500–2,000 m by the Ventura family "
        "(Finca El Durazno, five generations). Red apple / milk chocolate / light "
        "toasted pecan; smooth medium body, bright but mellow acidity, clean "
        "finish. Balanced Central American — roast to a medium for chocolate + nut "
        "while keeping the apple brightness. Targets are a conservative first-roast "
        "de-risk (13 % dev); step toward ~16–18 % after tasting. Edit to taste."
    ),
)

#: Every built-in seed profile, inserted idempotently at startup (#303).
SEED_BEAN_PROFILES: tuple[BeanProfile, ...] = (
    ETHIOPIA_KOKE_SEED,
    COLOMBIA_HUILA_SEED,
    GUATEMALA_EL_DURAZNO_SEED,
)
