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

#: Every built-in seed profile, inserted idempotently at startup (#303).
SEED_BEAN_PROFILES: tuple[BeanProfile, ...] = (ETHIOPIA_KOKE_SEED,)
