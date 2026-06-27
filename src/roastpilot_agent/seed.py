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
#: balanced-medium starting point the operator edits to taste. The drop ceiling
#: stays at the operator's proven 195 °C known-good line (bitter > 196 °C), which
#: also keeps momentum headroom for the #336 anticipatory-trim validation. The
#: development target is 18 % DTR (up from the Koke's light 13 %): a washed,
#: dense, high-grown bean rewards MORE development for chocolate / body than a
#: delicate natural. 18 % is the convergence of our own corpus (Costa Rica
#: high-grown washed analog ~17.7 %; pooled high-grown washed ~17.1 %) and the
#: external consensus (Roast Rebels' SAME-machine Hottop KN-8828B-2K page = 19 %
#: for SHB South/Central American; Rao 20–25 % general, pulled toward the low end
#: by the small 250 g / high-burner-ratio batch), biased low to protect the
#: bright citrus. Research note 2026-06-27. All temperatures are Celsius.
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
    # 18 % DTR — washed high-grown bean wants more development than the delicate
    # Koke natural (13 %). Corpus + same-machine external research converge ~18 %
    # (see the docstring above); biased low (18 vs 19–22) to keep the citrus.
    target_development_percent=18.0,
    default_bean_weight_grams=250.0,  # 1 kg / 4 batches
    description=(
        "Huila, southern Colombia. Excelso grade, washed; grown 1,200–2,000 m on "
        "volcanic soil. Caturra / Typica / Bourbon. Sweet citrus + chocolate, "
        "bright acidity, round body, clean aroma. Balanced and forgiving — roast "
        "to a medium for body + sweetness. Targets are a conservative-medium "
        "starting point; edit to taste."
    ),
)

#: Every built-in seed profile, inserted idempotently at startup (#303).
SEED_BEAN_PROFILES: tuple[BeanProfile, ...] = (ETHIOPIA_KOKE_SEED, COLOMBIA_HUILA_SEED)
