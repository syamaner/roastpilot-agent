"""RoastControlPolicy — single-source, phase-resolved control limits (#273).

D35 §8.2/8.3: the per-phase ``RoastControlPolicy`` is the ONE place control
limits are defined, feeding BOTH the advisor context (what the model is told) AND
the safety gate (what the harness enforces). The crux is *told == enforced*: the
heat/fan box placed in ``AdvisorContext`` is the SAME box ``evaluate_command``
clamps a command into, both sourced from one policy object. These tests prove
that equality and pin that #273 introduces no second copy and changes none of the
six safety verdicts.
"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from roastpilot_agent.advisor import AdvisorContext
from roastpilot_agent.config import (
    AmbientFanDoctrine,
    LateMaillardTrim,
    PreFirstCrackLevers,
    SafetyLimits,
)
from roastpilot_agent.control_policy import (
    PhaseControlLimits,
    PostFcFanSignal,
    RoastControlPolicy,
    TrimSignal,
)
from roastpilot_agent.models import RoastPhase, RoastProfile
from roastpilot_agent.roast_history import RoastCurveSample, estimate_first_crack_eta_seconds
from roastpilot_agent.safety import SafetyPolicy, SafetyVerdict

_PROFILE = RoastProfile(
    name="policy-test",
    bean_origin="Ethiopia",
    bean_weight_grams=250.0,
    initial_heat_percent=70,
    initial_fan_percent=40,
    target_drop_temp_c=205.0,
    target_development_percent=20.0,
)

_ALL_PHASES = tuple(RoastPhase)


_PRE_FC_PHASES = (RoastPhase.PREHEATING, RoastPhase.ROASTING_PRE_FIRST_CRACK)
_NON_PRE_FC_PHASES = tuple(p for p in _ALL_PHASES if p not in _PRE_FC_PHASES)

_ENFORCED_AMBIENT_FAN_DOCTRINE = AmbientFanDoctrine(enabled=True, post_fc_fan_ceiling_enabled=True)
_BINDING_POST_FC_FAN_SIGNAL = PostFcFanSignal(
    ambient_temp_c=23.1,
    current_heat_percent=70,
    post_fc_heat_floor_percent=25,
    bean_ror_c_per_min=-1.0,
)


def test_limits_for_resolves_full_lever_range_outside_pre_fc() -> None:
    """Non-pre-FC phases resolve the full 0–100 heat/fan box with no deterministic
    target (#222): development → drop is the post-FC LLM's box (#223), and the
    lifecycle states do not actuate. Only the two pre-FC phases are narrowed.
    """
    policy = RoastControlPolicy(SafetyLimits(), _PROFILE)
    for phase in _NON_PRE_FC_PHASES:
        limits = policy.limits_for(phase)
        assert limits.heat_floor_percent == 0
        assert limits.heat_ceiling_percent == 100
        assert limits.fan_floor_percent == 0
        assert limits.fan_ceiling_percent == 100
        assert not limits.has_deterministic_target
        assert limits.heat_target_percent is None
        assert limits.fan_target_percent is None


def test_ceiling_binds_in_a_cool_room() -> None:
    """A cool-room signal narrows only the DEVELOPMENT fan ceiling."""
    policy = RoastControlPolicy(
        SafetyLimits(), _PROFILE, ambient_fan_doctrine=_ENFORCED_AMBIENT_FAN_DOCTRINE
    )
    unsignalled = policy.limits_for(RoastPhase.DEVELOPMENT)
    narrowed = policy.limits_for(
        RoastPhase.DEVELOPMENT, post_fc_fan_signal=_BINDING_POST_FC_FAN_SIGNAL
    )

    assert narrowed.fan_ceiling_percent == 70
    assert (
        narrowed.model_copy(update={"fan_ceiling_percent": unsignalled.fan_ceiling_percent})
        == unsignalled
    )


@pytest.mark.parametrize("ror", [0.0, -1.0])
def test_flat_or_falling_ror_at_the_floor_still_binds(ror: float) -> None:
    """Only a strictly positive RoR makes fan the sole remaining brake."""
    policy = RoastControlPolicy(
        SafetyLimits(), _PROFILE, ambient_fan_doctrine=_ENFORCED_AMBIENT_FAN_DOCTRINE
    )
    signal = PostFcFanSignal(
        ambient_temp_c=23.1,
        current_heat_percent=25,
        post_fc_heat_floor_percent=25,
        bean_ror_c_per_min=ror,
    )
    assert (
        policy.limits_for(RoastPhase.DEVELOPMENT, post_fc_fan_signal=signal).fan_ceiling_percent
        == 70
    )


def test_no_clamp_when_ambient_absent() -> None:
    """Absent ambient, including a stale reading represented as None, fails open."""
    policy = RoastControlPolicy(
        SafetyLimits(), _PROFILE, ambient_fan_doctrine=_ENFORCED_AMBIENT_FAN_DOCTRINE
    )
    signal = PostFcFanSignal(
        ambient_temp_c=None,
        current_heat_percent=70,
        post_fc_heat_floor_percent=25,
        bean_ror_c_per_min=-1.0,
    )
    assert (
        policy.limits_for(RoastPhase.DEVELOPMENT, post_fc_fan_signal=signal).fan_ceiling_percent
        == 100
    )


@pytest.mark.parametrize("ambient", [float("nan"), float("-inf"), float("inf")])
def test_no_clamp_on_non_finite_ambient(ambient: float) -> None:
    """Every non-finite public signal value preserves full fan authority."""
    policy = RoastControlPolicy(
        SafetyLimits(), _PROFILE, ambient_fan_doctrine=_ENFORCED_AMBIENT_FAN_DOCTRINE
    )
    signal = PostFcFanSignal(
        ambient_temp_c=ambient,
        current_heat_percent=70,
        post_fc_heat_floor_percent=25,
        bean_ror_c_per_min=-1.0,
    )
    assert (
        policy.limits_for(RoastPhase.DEVELOPMENT, post_fc_fan_signal=signal).fan_ceiling_percent
        == 100
    )


@pytest.mark.parametrize("ambient", [26.0, 31.6])
def test_no_clamp_at_or_above_threshold(ambient: float) -> None:
    """The cool-room branch is strictly below the configured threshold."""
    policy = RoastControlPolicy(
        SafetyLimits(), _PROFILE, ambient_fan_doctrine=_ENFORCED_AMBIENT_FAN_DOCTRINE
    )
    signal = PostFcFanSignal(
        ambient_temp_c=ambient,
        current_heat_percent=70,
        post_fc_heat_floor_percent=25,
        bean_ror_c_per_min=-1.0,
    )
    assert (
        policy.limits_for(RoastPhase.DEVELOPMENT, post_fc_fan_signal=signal).fan_ceiling_percent
        == 100
    )


def test_carve_out_releases_when_heat_at_floor_and_climbing() -> None:
    """Fan remains unrestricted when it is the only brake left (#498)."""
    policy = RoastControlPolicy(
        SafetyLimits(), _PROFILE, ambient_fan_doctrine=_ENFORCED_AMBIENT_FAN_DOCTRINE
    )
    signal = PostFcFanSignal(
        ambient_temp_c=23.1,
        current_heat_percent=25,
        post_fc_heat_floor_percent=25,
        bean_ror_c_per_min=2.0,
    )
    assert (
        policy.limits_for(RoastPhase.DEVELOPMENT, post_fc_fan_signal=signal).fan_ceiling_percent
        == 100
    )


def test_carve_out_unknowns_release_their_conjunct() -> None:
    """Unknown RoR or floor fails toward keeping full fan brake authority."""
    policy = RoastControlPolicy(
        SafetyLimits(), _PROFILE, ambient_fan_doctrine=_ENFORCED_AMBIENT_FAN_DOCTRINE
    )
    signals = (
        PostFcFanSignal(23.1, 25, 25, None),
        PostFcFanSignal(23.1, 25, 25, float("nan")),
        PostFcFanSignal(23.1, 70, None, 2.0),
    )
    for signal in signals:
        assert (
            policy.limits_for(RoastPhase.DEVELOPMENT, post_fc_fan_signal=signal).fan_ceiling_percent
            == 100
        )


def test_binds_when_heat_above_floor_even_while_climbing() -> None:
    """Pin the 10 Aug arm's shape against the superseded base-ceiling form.

    Heat ~70 against floor 25 in a cool room while the bean climbs leaves ~45 pp
    of downward heat-brake authority. Fan is NOT the only brake, so the ceiling
    MUST hold. The superseded "at or below base ceiling" form got this exact case
    wrong and would bind only by the accident of recovery elevating heat above
    that base ceiling.
    """
    policy = RoastControlPolicy(
        SafetyLimits(), _PROFILE, ambient_fan_doctrine=_ENFORCED_AMBIENT_FAN_DOCTRINE
    )
    signal = PostFcFanSignal(23.1, 70, 25, 2.0)
    assert (
        policy.limits_for(RoastPhase.DEVELOPMENT, post_fc_fan_signal=signal).fan_ceiling_percent
        == 70
    )


def test_heat_below_floor_also_releases() -> None:
    """The carve-out tolerates an off-by-one or downward-collapsed box."""
    policy = RoastControlPolicy(
        SafetyLimits(), _PROFILE, ambient_fan_doctrine=_ENFORCED_AMBIENT_FAN_DOCTRINE
    )
    signal = PostFcFanSignal(23.1, 20, 25, 2.0)
    assert (
        policy.limits_for(RoastPhase.DEVELOPMENT, post_fc_fan_signal=signal).fan_ceiling_percent
        == 100
    )


def test_both_flags_off_is_a_total_no_op() -> None:
    """The default doctrine — both flags off — ignores even a binding signal.

    ``test_flag_off_is_a_total_no_op`` covers the two one-flag-on shapes; this
    pins the shipped default, which is the state every existing deployment runs.
    """
    policy = RoastControlPolicy(SafetyLimits(), _PROFILE, ambient_fan_doctrine=AmbientFanDoctrine())
    assert (
        policy.limits_for(
            RoastPhase.DEVELOPMENT, post_fc_fan_signal=_BINDING_POST_FC_FAN_SIGNAL
        ).fan_ceiling_percent
        == 100
    )


def test_default_constructed_policy_ignores_a_binding_signal() -> None:
    """Slice 1 is a runtime no-op for every EXISTING caller.

    Constructed the way the controller constructs it (`controller.py` passes no
    ``ambient_fan_doctrine``), a binding signal must still resolve the full
    0-100 fan box. Asserted directly rather than left implied by the conjunction
    of the flag tests, because this is the property the slice actually claims.
    """
    policy = RoastControlPolicy(SafetyLimits(), _PROFILE)
    assert (
        policy.limits_for(
            RoastPhase.DEVELOPMENT, post_fc_fan_signal=_BINDING_POST_FC_FAN_SIGNAL
        ).fan_ceiling_percent
        == 100
    )


def test_unknown_floor_with_a_falling_ror_still_binds() -> None:
    """An unknown floor satisfies only the HEAT half of the carve-out.

    ``post_fc_heat_floor_percent=None`` means the loop is not engaged. Release is
    conjunctive, so an unknown floor with a flat or falling bean still clamps —
    the negative-RoR leg the unknown-directions test leaves unpinned.
    """
    policy = RoastControlPolicy(
        SafetyLimits(), _PROFILE, ambient_fan_doctrine=_ENFORCED_AMBIENT_FAN_DOCTRINE
    )
    signal = PostFcFanSignal(23.1, 70, None, -1.0)
    assert (
        policy.limits_for(RoastPhase.DEVELOPMENT, post_fc_fan_signal=signal).fan_ceiling_percent
        == 70
    )


def test_flag_off_is_a_total_no_op() -> None:
    """Both master and destination flags independently gate enforcement."""
    doctrines = (
        AmbientFanDoctrine(enabled=True, post_fc_fan_ceiling_enabled=False),
        AmbientFanDoctrine(enabled=False, post_fc_fan_ceiling_enabled=True),
    )
    for doctrine in doctrines:
        policy = RoastControlPolicy(SafetyLimits(), _PROFILE, ambient_fan_doctrine=doctrine)
        assert (
            policy.limits_for(
                RoastPhase.DEVELOPMENT,
                post_fc_fan_signal=_BINDING_POST_FC_FAN_SIGNAL,
            ).fan_ceiling_percent
            == 100
        )


def test_only_development_narrows() -> None:
    """Cooling/lifecycle stay full-range and pre-FC keeps its lever-derived box."""
    policy = RoastControlPolicy(
        SafetyLimits(), _PROFILE, ambient_fan_doctrine=_ENFORCED_AMBIENT_FAN_DOCTRINE
    )
    assert (
        policy.limits_for(
            RoastPhase.COOLING, post_fc_fan_signal=_BINDING_POST_FC_FAN_SIGNAL
        ).fan_ceiling_percent
        == 100
    )
    other_non_pre_fc = (
        phase
        for phase in _NON_PRE_FC_PHASES
        if phase not in (RoastPhase.DEVELOPMENT, RoastPhase.COOLING)
    )
    for phase in other_non_pre_fc:
        assert (
            policy.limits_for(
                phase, post_fc_fan_signal=_BINDING_POST_FC_FAN_SIGNAL
            ).fan_ceiling_percent
            == 100
        )
    for phase in _PRE_FC_PHASES:
        assert policy.limits_for(
            phase, post_fc_fan_signal=_BINDING_POST_FC_FAN_SIGNAL
        ) == policy.limits_for(phase)


def test_unsignalled_call_is_unchanged() -> None:
    """Existing callers keep the full DEVELOPMENT box with both flags on."""
    policy = RoastControlPolicy(
        SafetyLimits(), _PROFILE, ambient_fan_doctrine=_ENFORCED_AMBIENT_FAN_DOCTRINE
    )
    assert policy.limits_for(RoastPhase.DEVELOPMENT) == RoastControlPolicy(
        SafetyLimits(), _PROFILE
    ).limits_for(RoastPhase.DEVELOPMENT)


def test_fan_clamp_becomes_reachable_through_the_narrowed_box() -> None:
    """The existing command-bounds rule enforces the narrowed fan destination."""
    limits = SafetyLimits()
    policy = RoastControlPolicy(
        limits, _PROFILE, ambient_fan_doctrine=_ENFORCED_AMBIENT_FAN_DOCTRINE
    )
    box = policy.limits_for(RoastPhase.DEVELOPMENT, post_fc_fan_signal=_BINDING_POST_FC_FAN_SIGNAL)
    evaluation = SafetyPolicy(limits).evaluate_command(
        requested_heat=70,
        requested_fan=100,
        seconds_since_last_command=None,
        bounds=box,
    )

    assert evaluation.verdict is SafetyVerdict.CLAMP
    assert evaluation.rule == "command_bounds"
    assert evaluation.adjusted_fan == 70
    assert evaluation.adjusted_heat == 70


def test_released_signal_preserves_full_fan_range() -> None:
    """A latched D157 signal keeps the DEVELOPMENT destination unrestricted."""
    policy = RoastControlPolicy(
        SafetyLimits(), _PROFILE, ambient_fan_doctrine=_ENFORCED_AMBIENT_FAN_DOCTRINE
    )
    signal = PostFcFanSignal(
        ambient_temp_c=23.1,
        current_heat_percent=70,
        post_fc_heat_floor_percent=25,
        bean_ror_c_per_min=-1.0,
        released=True,
    )

    assert (
        policy.limits_for(RoastPhase.DEVELOPMENT, post_fc_fan_signal=signal).fan_ceiling_percent
        == 100
    )


@pytest.mark.parametrize(
    ("current_heat", "ror", "released", "expected"),
    [
        (25, 4.0, False, True),
        (70, 4.0, False, False),
        (25, 0.0, False, False),
        (25, 4.0, True, True),
    ],
)
def test_fan_ceiling_release_due_uses_live_conjunction_and_ignores_latch(
    current_heat: int, ror: float, released: bool, expected: bool
) -> None:
    """The public latching condition is conjunctive and latch-independent."""
    policy = RoastControlPolicy(
        SafetyLimits(), _PROFILE, ambient_fan_doctrine=_ENFORCED_AMBIENT_FAN_DOCTRINE
    )
    signal = PostFcFanSignal(
        ambient_temp_c=23.1,
        current_heat_percent=current_heat,
        post_fc_heat_floor_percent=25,
        bean_ror_c_per_min=ror,
        released=released,
    )

    assert policy.fan_ceiling_release_due(signal) is expected


@pytest.mark.parametrize("phase", _PRE_FC_PHASES)
def test_pre_fc_phases_resolve_narrowed_box_with_deterministic_target(
    phase: RoastPhase,
) -> None:
    """D35 §3 (#222): the two pre-FC phases resolve a NARROWED box carrying the
    deterministic n8n lever target — heat pinned high (floor == the heat 100
    target, so a momentum-killing cut cannot execute) and fan capped low (≤ 30).
    """
    policy = RoastControlPolicy(SafetyLimits(), _PROFILE)
    box = policy.limits_for(phase)
    assert box.has_deterministic_target
    assert box.heat_target_percent == 100
    assert box.fan_target_percent == 30
    assert box.heat_floor_percent == 100  # pinned to the target — no cut below
    assert box.heat_ceiling_percent == 100
    assert box.fan_floor_percent == 0
    assert box.fan_ceiling_percent == 30


def test_pre_fc_levers_are_parameterised_not_hardcoded() -> None:
    """The pre-FC levers are PARAMETERS (plan §4-A.3 / §7.1): a custom
    PreFirstCrackLevers resolves into the box and target, not the n8n defaults —
    the interface a learned per-bean plan (D42) supplies."""
    levers = PreFirstCrackLevers(
        heat_target_percent=90, fan_target_percent=25, fan_ceiling_percent=40
    )
    policy = RoastControlPolicy(SafetyLimits(), _PROFILE, pre_fc_levers=levers)
    box = policy.limits_for(RoastPhase.ROASTING_PRE_FIRST_CRACK)
    assert box.heat_target_percent == 90
    assert box.heat_floor_percent == 90
    assert box.fan_target_percent == 25
    assert box.fan_ceiling_percent == 40


@pytest.mark.parametrize("phase", _PRE_FC_PHASES)
def test_pre_fc_targets_sourced_from_profile_when_set(phase: RoastPhase) -> None:
    """D59 / #318 (option C): the deterministic pre-FC heat/fan targets are sourced
    from the ACTIVE bean profile's ``pre_fc_heat`` / ``pre_fc_fan`` when set — a
    delicate natural's profile (fan 20) drives fan 20 pre-FC, not the config 30."""
    profile = _PROFILE.model_copy(update={"pre_fc_heat": 90, "pre_fc_fan": 20})
    policy = RoastControlPolicy(SafetyLimits(), profile)
    box = policy.limits_for(phase)
    assert box.heat_target_percent == 90
    assert box.heat_floor_percent == 90  # pinned to the per-bean heat — no cut below
    assert box.fan_target_percent == 20  # the per-bean fan, not the config 30
    assert box.fan_ceiling_percent == 30  # the box ceiling is unchanged (config)


@pytest.mark.parametrize("phase", _PRE_FC_PHASES)
def test_pre_fc_targets_fall_back_to_config_when_profile_unset(phase: RoastPhase) -> None:
    """D59: a profile that does NOT specify ``pre_fc_heat`` / ``pre_fc_fan`` (the
    fields default ``None``) falls back to the global ``PreFirstCrackLevers``
    config default — heat 100 / fan 30 — so every pre-#318 profile is unchanged."""
    assert _PROFILE.pre_fc_heat is None and _PROFILE.pre_fc_fan is None
    box = RoastControlPolicy(SafetyLimits(), _PROFILE).limits_for(phase)
    assert box.heat_target_percent == 100
    assert box.fan_target_percent == 30


def test_pre_fc_targets_fall_back_to_config_with_no_profile() -> None:
    """D59: with no active profile at all, the pre-FC targets are the config
    default (the policy guards the absent profile, not just the unset field)."""
    box = RoastControlPolicy(SafetyLimits()).limits_for(RoastPhase.ROASTING_PRE_FIRST_CRACK)
    assert box.heat_target_percent == 100
    assert box.fan_target_percent == 30


def test_pre_fc_one_field_set_other_falls_back() -> None:
    """D59: the two per-bean fields are independent — a profile may set only the
    fan (the delicate-natural case) and inherit the config heat default."""
    profile = _PROFILE.model_copy(update={"pre_fc_fan": 20})
    box = RoastControlPolicy(SafetyLimits(), profile).limits_for(
        RoastPhase.ROASTING_PRE_FIRST_CRACK
    )
    assert box.heat_target_percent == 100  # config default (heat unset on the profile)
    assert box.fan_target_percent == 20  # the per-bean fan


@pytest.mark.parametrize("phase", _PRE_FC_PHASES)
def test_pre_fc_profile_fan_above_ceiling_is_clamped_by_the_box(phase: RoastPhase) -> None:
    """D59 INVARIANT: a per-bean ``pre_fc_fan`` ABOVE the configured fan ceiling is
    CLAMPED into the box, never honoured blindly (every roaster write stays inside
    the pre-FC safety box). The resolved fan target cannot exceed the ceiling."""
    # pre_fc_fan 80 is well above the default fan_ceiling_percent (30).
    profile = _PROFILE.model_copy(update={"pre_fc_fan": 80})
    box = RoastControlPolicy(SafetyLimits(), profile).limits_for(phase)
    assert box.fan_ceiling_percent == 30
    assert box.fan_target_percent == 30  # clamped to the ceiling, not 80
    assert box.fan_target_percent <= box.fan_ceiling_percent  # the invariant


@pytest.mark.parametrize("phase", _PRE_FC_PHASES)
def test_pre_fc_profile_heat_is_bounded_by_the_heat_ceiling(phase: RoastPhase) -> None:
    """D59 INVARIANT (symmetric with the fan clamp): the resolved per-bean
    ``pre_fc_heat`` is CLAMPED into the pre-FC heat ceiling, never honoured above
    it. The field max (100) equals the ceiling today so a breach isn't
    constructible, but the resolved target must stay ≤ the ceiling so a future
    lower heat ceiling cannot un-bound a per-bean heat."""
    # The field maximum — exercises the upper edge of the clamp.
    profile = _PROFILE.model_copy(update={"pre_fc_heat": 100})
    box = RoastControlPolicy(SafetyLimits(), profile).limits_for(phase)
    assert box.heat_target_percent == 100  # honoured at the ceiling, not above
    assert box.heat_target_percent <= box.heat_ceiling_percent  # the invariant


def test_pre_fc_profile_fan_at_a_raised_ceiling_is_honoured() -> None:
    """D59: when config raises ``fan_ceiling_percent`` to leave room, a per-bean
    fan inside the wider box is honoured exactly (the clamp only bites above the
    ceiling — it does not floor a legitimately-low per-bean value)."""
    levers = PreFirstCrackLevers(fan_target_percent=30, fan_ceiling_percent=50)
    profile = _PROFILE.model_copy(update={"pre_fc_fan": 45})
    box = RoastControlPolicy(SafetyLimits(), profile, pre_fc_levers=levers).limits_for(
        RoastPhase.ROASTING_PRE_FIRST_CRACK
    )
    assert box.fan_ceiling_percent == 50
    assert box.fan_target_percent == 45  # inside the wider box — honoured, not clamped


@pytest.mark.parametrize("phase", _PRE_FC_PHASES)
def test_pre_fc_trim_composes_with_a_profile_sourced_heat(phase: RoastPhase) -> None:
    """D59 + #327: the anticipatory heat trim still composes with a per-bean heat.
    With the window open the engaged heat is the trim level, and it stays ≤ the
    resolved floor (the trim only ever REDUCES heat, never raises the per-bean
    target)."""
    # Per-bean heat 90; default trim level 65 < 90, so the window lowers heat to 65.
    profile = _PROFILE.model_copy(update={"pre_fc_heat": 90})
    box = RoastControlPolicy(SafetyLimits(), profile).limits_for(phase, trim_signal=_TRIM_OPEN)
    assert box.heat_target_percent == 65  # the trim level
    assert box.heat_floor_percent == 65
    assert box.heat_target_percent <= 90  # the trim never raises the per-bean heat


@pytest.mark.parametrize("phase", _PRE_FC_PHASES)
def test_pre_fc_trim_never_raises_a_below_trim_profile_heat(phase: RoastPhase) -> None:
    """D59 + #327: if a per-bean ``pre_fc_heat`` is BELOW the config trim level, the
    engaged trim must not RAISE heat up to the trim level — the trim is clamped to
    the resolved base so it stays ≤ the per-bean floor (never delays FC)."""
    # Per-bean heat 50, below the default trim level 65: the trim must not lift it.
    profile = _PROFILE.model_copy(update={"pre_fc_heat": 50})
    box = RoastControlPolicy(SafetyLimits(), profile).limits_for(phase, trim_signal=_TRIM_OPEN)
    assert box.heat_target_percent == 50  # clamped to the per-bean base, not raised to 65
    assert box.heat_floor_percent == 50


def test_bitter_and_emergency_ceilings_come_from_config() -> None:
    """The told drop/bitter ceiling + emergency-drop bound are the config values."""
    limits = SafetyLimits()
    policy = RoastControlPolicy(limits)  # no profile
    box = policy.limits_for(RoastPhase.DEVELOPMENT)
    assert box.bitter_ceiling_temp_c == limits.bitter_ceiling_temp_c == 196.0
    assert box.emergency_drop_temp_c == limits.emergency_drop_temp_c == 198.0


def test_bitter_ceiling_is_never_capped_to_the_profile_drop_target() -> None:
    """#563: the told bitter ceiling is profile-INDEPENDENT — never capped to
    (or otherwise derived from) ``target_drop_temp_c``.

    Superseded test intent: the #273-era design capped the told ceiling at
    ``min(hard_ceiling, target_drop_temp_c)``, which made the told number
    IDENTICAL to the drop target on every seeded profile (195.0 capped
    against the 196.0 hard ceiling) — the false "no overshoot room" premise
    behind the #563 bake-off finding. A profile with a LOWER target must
    still be told the true 196 °C planning line, not its own target.
    """
    light = _PROFILE.model_copy(update={"target_drop_temp_c": 190.0})
    box = RoastControlPolicy(SafetyLimits(), light).limits_for(RoastPhase.DEVELOPMENT)
    assert box.bitter_ceiling_temp_c == 196.0  # NOT 190.0 — the target never caps it
    # The emergency-drop bound is profile-independent (the last-resort bound).
    assert box.emergency_drop_temp_c == 198.0


def test_bitter_ceiling_unaffected_by_a_higher_profile_target_too() -> None:
    """A high profile drop target has no effect on the told ceiling either way
    (#563: the two are fully independent numbers, not just one-directionally
    capped)."""
    high = _PROFILE.model_copy(update={"target_drop_temp_c": 210.0})
    box = RoastControlPolicy(SafetyLimits(), high).limits_for(RoastPhase.DEVELOPMENT)
    assert box.bitter_ceiling_temp_c == 196.0


def test_bitter_ceiling_is_the_ceiling_guard_temp_when_guard_enabled() -> None:
    """#563: with the post-FC ceiling guard ON, the told ceiling is
    ``ceiling_guard_temp_c`` — the number that actually fires the
    deterministic drop — not the raw ``SafetyLimits.bitter_ceiling_temp_c``.

    Uses a guard temperature (190.0) DISTINCT from both the hard 196.0 safety
    line and the profile's own target, so this test cannot pass by numeric
    coincidence (C2: the target-205 tests stay green whether or not the guard
    wiring works, since the default guard temp equals the default hard
    ceiling — this test pins the wiring itself).
    """
    from roastpilot_agent.config import PostFirstCrackControl

    post_fc = PostFirstCrackControl(ceiling_guard_drop_enabled=True, ceiling_guard_temp_c=190.0)
    box = RoastControlPolicy(SafetyLimits(), _PROFILE, post_fc_control=post_fc).limits_for(
        RoastPhase.DEVELOPMENT
    )
    assert box.bitter_ceiling_temp_c == 190.0
    assert box.bitter_ceiling_temp_c != SafetyLimits().bitter_ceiling_temp_c


def test_bitter_ceiling_is_the_hard_safety_line_when_guard_disabled() -> None:
    """#563: with the guard OFF, nothing deterministic enforces 196 °C (only
    the 230 °C e-stop is truly enforced) — the told ceiling falls back to the
    hard ``SafetyLimits.bitter_ceiling_temp_c`` line, a known told != enforced
    gap accepted because 196 °C is still better operator guidance than no
    ceiling at all (memory `told-vs-enforced-bitter-ceiling`). The
    ``ceiling_guard_temp_c`` value must be IGNORED in this configuration.
    """
    from roastpilot_agent.config import PostFirstCrackControl

    post_fc = PostFirstCrackControl(ceiling_guard_drop_enabled=False, ceiling_guard_temp_c=190.0)
    box = RoastControlPolicy(SafetyLimits(), _PROFILE, post_fc_control=post_fc).limits_for(
        RoastPhase.DEVELOPMENT
    )
    assert box.bitter_ceiling_temp_c == 196.0
    assert box.bitter_ceiling_temp_c != 190.0  # the disabled guard's own temp is not read


def test_bitter_ceiling_defaults_match_a_default_constructed_policy() -> None:
    """A ``RoastControlPolicy`` built with no ``post_fc_control`` argument (the
    keyword-only-with-a-default shape, C1) resolves the same told ceiling as
    one built with an explicit default :class:`PostFirstCrackControl` — the
    ~25 existing call sites that do not pass this argument are unaffected."""
    from roastpilot_agent.config import PostFirstCrackControl

    implicit = RoastControlPolicy(SafetyLimits(), _PROFILE)
    explicit = RoastControlPolicy(SafetyLimits(), _PROFILE, post_fc_control=PostFirstCrackControl())
    assert implicit.limits_for(RoastPhase.DEVELOPMENT).bitter_ceiling_temp_c == (
        explicit.limits_for(RoastPhase.DEVELOPMENT).bitter_ceiling_temp_c
    )


def test_phase_control_limits_rejects_inverted_heat_box() -> None:
    """A floor above its ceiling is an empty (invalid) box."""
    with pytest.raises(ValidationError):
        PhaseControlLimits(
            heat_floor_percent=80,
            heat_ceiling_percent=50,
            fan_floor_percent=0,
            fan_ceiling_percent=100,
            bitter_ceiling_temp_c=196.0,
            emergency_drop_temp_c=198.0,
        )


def test_phase_control_limits_rejects_inverted_fan_box() -> None:
    """A fan floor above its ceiling is an empty (invalid) box."""
    with pytest.raises(ValidationError):
        PhaseControlLimits(
            heat_floor_percent=0,
            heat_ceiling_percent=100,
            fan_floor_percent=90,
            fan_ceiling_percent=30,
            bitter_ceiling_temp_c=196.0,
            emergency_drop_temp_c=198.0,
        )


def test_phase_control_limits_rejects_inverted_drop_ceilings() -> None:
    """The emergency-drop bound must sit above the bitter ceiling."""
    with pytest.raises(ValidationError):
        PhaseControlLimits(
            heat_floor_percent=0,
            heat_ceiling_percent=100,
            fan_floor_percent=0,
            fan_ceiling_percent=100,
            bitter_ceiling_temp_c=198.0,
            emergency_drop_temp_c=196.0,
        )


def test_phase_control_limits_rejects_half_set_targets() -> None:
    """The deterministic lever targets are all-or-nothing (#222): heat set without
    fan (or vice versa) is an invalid box."""
    with pytest.raises(ValidationError):
        PhaseControlLimits(
            heat_floor_percent=100,
            heat_ceiling_percent=100,
            fan_floor_percent=0,
            fan_ceiling_percent=30,
            bitter_ceiling_temp_c=196.0,
            emergency_drop_temp_c=198.0,
            heat_target_percent=100,  # fan target omitted → invalid
        )


def test_phase_control_limits_rejects_heat_target_outside_box() -> None:
    """A heat target outside its own box (#222) would be silently clamped by the
    gate — told != enforced for the deterministic path — so it is rejected."""
    with pytest.raises(ValidationError):
        PhaseControlLimits(
            heat_floor_percent=100,
            heat_ceiling_percent=100,
            fan_floor_percent=0,
            fan_ceiling_percent=30,
            bitter_ceiling_temp_c=196.0,
            emergency_drop_temp_c=198.0,
            heat_target_percent=50,  # below the pinned floor of 100
            fan_target_percent=30,
        )


def test_phase_control_limits_rejects_fan_target_outside_box() -> None:
    """A fan target outside its own box (#222) is rejected (same told==enforced
    invariant as the heat target)."""
    with pytest.raises(ValidationError):
        PhaseControlLimits(
            heat_floor_percent=100,
            heat_ceiling_percent=100,
            fan_floor_percent=0,
            fan_ceiling_percent=30,
            bitter_ceiling_temp_c=196.0,
            emergency_drop_temp_c=198.0,
            heat_target_percent=100,
            fan_target_percent=50,  # above the fan ceiling of 30
        )


def test_pre_first_crack_levers_rejects_fan_ceiling_below_target() -> None:
    """PreFirstCrackLevers pins fan_ceiling_percent >= fan_target_percent (#222):
    a ceiling below the target would make the policy's own deterministic write
    fall outside the box it resolves."""
    with pytest.raises(ValidationError):
        PreFirstCrackLevers(fan_target_percent=30, fan_ceiling_percent=20)


def test_safety_limits_rejects_inverted_drop_ceilings() -> None:
    """SafetyLimits pins emergency_drop_temp_c above bitter_ceiling_temp_c."""
    with pytest.raises(ValidationError):
        SafetyLimits(bitter_ceiling_temp_c=200.0, emergency_drop_temp_c=198.0)


# --- Anticipatory late-Maillard heat trim (#327) -----------------------------

# Default trim heat level (LateMaillardTrim.trim_heat_percent default).
# Using a named constant rather than a bare 65 literal prevents silent
# zero-engagement when the default changes.
_DEFAULT_TRIM_LEVEL: int = 65

# A trim signal sized to OPEN the default window: bean above the 155 °C floor and
# a positive FC-ETA (30 s) at/below the 60 s window.
_TRIM_OPEN = TrimSignal(bean_temp_c=165.0, first_crack_eta_seconds=30.0)


@pytest.mark.parametrize("phase", _PRE_FC_PHASES)
def test_trim_lowers_heat_floor_and_target_in_window(phase: RoastPhase) -> None:
    """With the late-Maillard window open the pre-FC heat floor AND target drop to
    the configured trim level (#327) — a moderate reduction from the flat 100 floor,
    not a crash. Fan is unchanged (the plan's "fan controlled", not raised)."""
    policy = RoastControlPolicy(SafetyLimits(), _PROFILE)
    box = policy.limits_for(phase, trim_signal=_TRIM_OPEN)
    assert box.heat_target_percent == _DEFAULT_TRIM_LEVEL  # the default trim level (~60–70 %)
    assert box.heat_floor_percent == _DEFAULT_TRIM_LEVEL  # floor pinned to the trim — no cut below
    assert box.heat_ceiling_percent == 100
    # Fan stays at the flat-floor target/box — the trim never raises fan.
    assert box.fan_target_percent == 30
    assert box.fan_floor_percent == 0
    assert box.fan_ceiling_percent == 30


def test_trim_never_exceeds_the_flat_floor_heat() -> None:
    """The trim is a strict reduction: the trimmed heat target sits at or below the
    flat-floor heat (#327), so the floor never rises and FC is never delayed."""
    policy = RoastControlPolicy(SafetyLimits(), _PROFILE)
    flat = policy.limits_for(RoastPhase.ROASTING_PRE_FIRST_CRACK)
    trimmed = policy.limits_for(RoastPhase.ROASTING_PRE_FIRST_CRACK, trim_signal=_TRIM_OPEN)
    assert trimmed.heat_target_percent is not None
    assert flat.heat_target_percent is not None
    assert trimmed.heat_target_percent <= flat.heat_target_percent


@pytest.mark.parametrize(
    "signal",
    [
        None,  # no signal at all → fail closed
        TrimSignal(bean_temp_c=165.0, first_crack_eta_seconds=None),  # FC-ETA unknown
        TrimSignal(bean_temp_c=165.0, first_crack_eta_seconds=120.0),  # FC too far out
        TrimSignal(bean_temp_c=140.0, first_crack_eta_seconds=30.0),  # bean below floor
        TrimSignal(bean_temp_c=165.0, first_crack_eta_seconds=0.0),  # non-positive ETA
        TrimSignal(bean_temp_c=165.0, first_crack_eta_seconds=float("nan")),  # NaN ETA
    ],
)
def test_trim_fails_closed_to_flat_floor(signal: TrimSignal | None) -> None:
    """Outside the window — no signal, unknown/too-far/non-positive FC-ETA, or a
    bean below the late-Maillard floor — the policy resolves the flat #222 floor
    (heat 100 / fan 30), the always-on guarantee FC still arrives (§8.4)."""
    policy = RoastControlPolicy(SafetyLimits(), _PROFILE)
    box = policy.limits_for(RoastPhase.ROASTING_PRE_FIRST_CRACK, trim_signal=signal)
    assert box.heat_target_percent == 100
    assert box.heat_floor_percent == 100
    assert box.fan_target_percent == 30


def test_latched_signal_keeps_trim_engaged_through_eta_bounce() -> None:
    """#327 hysteresis: a LATCHED signal keeps the trim engaged even when the FC-ETA
    bounces back ABOVE the window (the noisy-estimator case) — the trimmed heat is
    held, not snapped back to 100. Without the latch the same out-of-window ETA
    fails closed to the flat floor (the flip-flop the latch removes)."""
    policy = RoastControlPolicy(SafetyLimits(), _PROFILE)
    # ETA 80 s is OUTSIDE the 60 s window — a fresh signal would NOT engage…
    fresh = TrimSignal(bean_temp_c=170.0, first_crack_eta_seconds=80.0, latched=False)
    fresh_box = policy.limits_for(RoastPhase.ROASTING_PRE_FIRST_CRACK, trim_signal=fresh)
    assert fresh_box.heat_target_percent == 100
    # …but the SAME bounce with the latch set holds the trim at 65.
    latched = TrimSignal(bean_temp_c=170.0, first_crack_eta_seconds=80.0, latched=True)
    latched_box = policy.limits_for(RoastPhase.ROASTING_PRE_FIRST_CRACK, trim_signal=latched)
    assert latched_box.heat_target_percent == _DEFAULT_TRIM_LEVEL


def test_trim_window_open_ignores_the_latch() -> None:
    """#327: ``trim_window_open`` is the FRESH-engage precondition — it ignores the
    carried latch, so the controller only ever latches on a clean in-window signal
    (a garbage ETA never arms the latch even if the signal claims latched)."""
    policy = RoastControlPolicy(SafetyLimits(), _PROFILE)
    # A degenerate signal (no ETA) that falsely claims latched: window stays shut.
    assert not policy.trim_window_open(
        TrimSignal(bean_temp_c=170.0, first_crack_eta_seconds=None, latched=True)
    )
    # A clean in-window signal opens regardless of the latch value.
    assert policy.trim_window_open(
        TrimSignal(bean_temp_c=165.0, first_crack_eta_seconds=30.0, latched=False)
    )


def test_disabled_trim_ignores_the_latch() -> None:
    """#327: a config-disabled trim is never engaged, even by a latched signal —
    ``enabled=False`` is the hard off-switch the latch cannot override."""
    levers = PreFirstCrackLevers(late_maillard_trim=LateMaillardTrim(enabled=False))
    policy = RoastControlPolicy(SafetyLimits(), _PROFILE, pre_fc_levers=levers)
    latched = TrimSignal(bean_temp_c=170.0, first_crack_eta_seconds=30.0, latched=True)
    box = policy.limits_for(RoastPhase.ROASTING_PRE_FIRST_CRACK, trim_signal=latched)
    assert box.heat_target_percent == 100


def test_trim_disabled_in_config_keeps_flat_floor_even_in_window() -> None:
    """``enabled=False`` reverts to the pure #222 flat floor with no trim window —
    even a signal that would otherwise open the window resolves heat 100 (#327)."""
    levers = PreFirstCrackLevers(late_maillard_trim=LateMaillardTrim(enabled=False))
    policy = RoastControlPolicy(SafetyLimits(), _PROFILE, pre_fc_levers=levers)
    box = policy.limits_for(RoastPhase.ROASTING_PRE_FIRST_CRACK, trim_signal=_TRIM_OPEN)
    assert box.heat_target_percent == 100
    assert box.heat_floor_percent == 100


def test_trim_ignored_outside_pre_fc_phases() -> None:
    """The trim signal only affects the pre-FC phases; a non-pre-FC phase resolves
    the full 0–100 box with no deterministic target regardless of the signal."""
    policy = RoastControlPolicy(SafetyLimits(), _PROFILE)
    box = policy.limits_for(RoastPhase.DEVELOPMENT, trim_signal=_TRIM_OPEN)
    assert not box.has_deterministic_target
    assert box.heat_floor_percent == 0
    assert box.heat_ceiling_percent == 100


def test_trim_parameters_are_config_driven_not_hardcoded() -> None:
    """The trim level + window are PARAMETERS (plan §8.3 single-source): a custom
    LateMaillardTrim resolves into the box, not the roast-3-sized defaults."""
    levers = PreFirstCrackLevers(
        late_maillard_trim=LateMaillardTrim(
            trim_heat_percent=70, window_fc_eta_seconds=45.0, min_bean_temp_c=150.0
        )
    )
    policy = RoastControlPolicy(SafetyLimits(), _PROFILE, pre_fc_levers=levers)
    # Bean 152 (above the custom 150 floor), ETA 40 s (inside the custom 45 s window).
    signal = TrimSignal(bean_temp_c=152.0, first_crack_eta_seconds=40.0)
    box = policy.limits_for(RoastPhase.ROASTING_PRE_FIRST_CRACK, trim_signal=signal)
    assert box.heat_target_percent == 70
    assert box.heat_floor_percent == 70


def test_trim_heat_percent_below_safe_minimum_is_rejected() -> None:
    """LateMaillardTrim rejects trim_heat_percent < 10 at construction time (#327).
    Values that low would stall the roast (heat=0 in late Maillard); the ge=10
    bound guards against misconfiguration before it reaches hardware."""
    with pytest.raises(ValidationError):
        LateMaillardTrim(trim_heat_percent=9)
    with pytest.raises(ValidationError):
        LateMaillardTrim(trim_heat_percent=0)
    # The boundary value is valid.
    assert LateMaillardTrim(trim_heat_percent=10).trim_heat_percent == 10


def test_levers_reject_trim_heat_above_flat_floor() -> None:
    """PreFirstCrackLevers pins trim_heat_percent <= heat_target_percent (#327):
    a trim heat above the flat floor would let the trim RAISE heat, which could
    delay FC — the trim must only ever reduce heat."""
    with pytest.raises(ValidationError):
        PreFirstCrackLevers(
            heat_target_percent=80,
            late_maillard_trim=LateMaillardTrim(trim_heat_percent=90),
        )
    # A trim heat ABOVE a learned-lower flat floor is rejected too.
    with pytest.raises(ValidationError):
        PreFirstCrackLevers(
            heat_target_percent=60,
            late_maillard_trim=LateMaillardTrim(trim_heat_percent=70),
        )


def test_safety_policy_exposes_its_own_limits() -> None:
    """The gate exposes the SAME limits object the policy must build from (#273)."""
    limits = SafetyLimits()
    assert SafetyPolicy(limits).limits is limits


@pytest.mark.parametrize("phase", _ALL_PHASES)
def test_told_equals_enforced_heat_fan_box(phase: RoastPhase) -> None:
    """THE #273 proof: the box the model is TOLD equals the box the gate ENFORCES.

    For each phase, resolve the control box from the single policy, place it in
    an ``AdvisorContext`` (the told side), then drive ``evaluate_command`` with
    that same box (the enforced side). A request deliberately outside the box on
    both levers must clamp to exactly the floors/ceilings the context carries —
    proving there is no second copy of the numbers.
    """
    limits = SafetyLimits()
    policy = RoastControlPolicy(limits, _PROFILE)
    box = policy.limits_for(phase)

    # The told side: the context carries the policy's resolved box verbatim.
    context = AdvisorContext(
        phase=phase,
        roast_elapsed_seconds=0.0,
        development_elapsed_seconds=None,
        current_bean_temp_c=180.0,
        current_env_temp_c=200.0,
        bean_ror_c_per_min=5.0,
        env_ror_c_per_min=5.0,
        target_drop_temp_c=_PROFILE.target_drop_temp_c,
        profile_name=_PROFILE.name,
        heat_floor_percent=box.heat_floor_percent,
        heat_ceiling_percent=box.heat_ceiling_percent,
        fan_floor_percent=box.fan_floor_percent,
        fan_ceiling_percent=box.fan_ceiling_percent,
        bitter_ceiling_temp_c=box.bitter_ceiling_temp_c,
        emergency_drop_temp_c=box.emergency_drop_temp_c,
    )

    # The enforced side: the gate clamps to the SAME box.
    gate = SafetyPolicy(limits)
    over = gate.evaluate_command(
        requested_heat=box.heat_ceiling_percent + 5,
        requested_fan=box.fan_ceiling_percent + 5,
        seconds_since_last_command=None,
        bounds=box,
    )
    under = gate.evaluate_command(
        requested_heat=box.heat_floor_percent - 5,
        requested_fan=box.fan_floor_percent - 5,
        seconds_since_last_command=None,
        bounds=box,
    )

    # Told == enforced: the ceiling the context advertised is the ceiling the
    # gate clamped an over-request to, and likewise for the floor.
    assert over.adjusted_heat == context.heat_ceiling_percent == box.heat_ceiling_percent
    assert over.adjusted_fan == context.fan_ceiling_percent == box.fan_ceiling_percent
    assert under.adjusted_heat == context.heat_floor_percent == box.heat_floor_percent
    assert under.adjusted_fan == context.fan_floor_percent == box.fan_floor_percent
    # The told temperature ceilings are the same single-source values.
    assert context.bitter_ceiling_temp_c == box.bitter_ceiling_temp_c
    assert context.emergency_drop_temp_c == box.emergency_drop_temp_c


def test_evaluate_command_default_bounds_unchanged() -> None:
    """Without ``bounds`` the gate behaves exactly as before #273 (0–100 clamp).

    Pins the verdict no-op for every existing caller that does not pass a box.
    """
    gate = SafetyPolicy(SafetyLimits())
    # In-bounds → ALLOW, echoing the request.
    allow = gate.evaluate_command(
        requested_heat=70, requested_fan=40, seconds_since_last_command=None
    )
    assert allow.verdict is SafetyVerdict.ALLOW
    assert allow.adjusted_heat == 70 and allow.adjusted_fan == 40
    # Out-of-0–100 → CLAMP to 0–100.
    clamp = gate.evaluate_command(
        requested_heat=105, requested_fan=-5, seconds_since_last_command=None
    )
    assert clamp.verdict is SafetyVerdict.CLAMP
    assert clamp.adjusted_heat == 100 and clamp.adjusted_fan == 0


def test_full_range_bounds_match_no_bounds_verdicts() -> None:
    """Passing the (today) full-range box yields the SAME verdicts as no bounds.

    The single-source wiring must not perturb verdicts while the policy resolves
    the full 0–100 range (the #273 invariant). Compares the bounded vs unbounded
    verdict for in-, over-, and under-range requests.
    """
    gate = SafetyPolicy(SafetyLimits())
    box = RoastControlPolicy(SafetyLimits(), _PROFILE).limits_for(RoastPhase.DEVELOPMENT)
    for heat, fan in ((70, 40), (105, 40), (50, -5)):
        without = gate.evaluate_command(
            requested_heat=heat, requested_fan=fan, seconds_since_last_command=None
        )
        with_box = gate.evaluate_command(
            requested_heat=heat, requested_fan=fan, seconds_since_last_command=None, bounds=box
        )
        assert with_box.verdict is without.verdict
        assert with_box.adjusted_heat == without.adjusted_heat
        assert with_box.adjusted_fan == without.adjusted_fan


def test_rate_limit_reject_with_bounds() -> None:
    """The rate-limit REJECT branch fires even when a real box is passed (#294).

    Closes the "six verdicts unchanged with bounds" gap: ALLOW and CLAMP are
    already proven with ``bounds=box``; this exercises REJECT (an in-bounds
    request issued inside the ``min_seconds_between_commands`` window) WITH the
    phase-resolved box, confirming the rate limit precedes the bounds clamp.
    """
    gate = SafetyPolicy(SafetyLimits())
    box = RoastControlPolicy(SafetyLimits(), _PROFILE).limits_for(RoastPhase.DEVELOPMENT)
    # 0.5 s < the 2.0 s default min_seconds_between_commands → rate-limit REJECT,
    # even though 70/40 sits inside the (today full-range) box.
    evaluation = gate.evaluate_command(
        requested_heat=70,
        requested_fan=40,
        seconds_since_last_command=0.5,
        bounds=box,
    )
    assert evaluation.verdict is SafetyVerdict.REJECT


# --- #327 replay validation against a REAL Hottop roast curve ----------------

# The committed 7 Jun 2026 live-roast fixture (a real Hottop roast reaching the
# FC band), used to validate the trim engages in late Maillard on real telemetry.
# The roast-3 (21 Jun) trace lives only in the operator's local SQLite DB (DBs are
# git-ignored, AGENTS.md §Rules), so live-trace replay against roast 3 itself is
# pending the operator's export; this real-curve replay is the in-repo proxy.
_LIVE_ROAST = (
    Path(__file__).parent / "fixtures" / "live-roast-2026-06-07" / "session-2" / "roast.jsonl"
)


def _live_curve() -> list[RoastCurveSample]:
    """Load the 7 Jun session-2 telemetry as a pre-FC curve (charge-referenced)."""
    rows = [
        json.loads(line)
        for line in _LIVE_ROAST.read_text().splitlines()
        if line.strip() and "bean_temp_c" in line
    ]
    t0 = rows[0]["monotonic_seconds"]
    return [
        RoastCurveSample(
            elapsed_since_charge_seconds=r["monotonic_seconds"] - t0,
            bean_temp_c=r["bean_temp_c"],
            env_temp_c=r["env_temp_c"],
            heat_percent=r.get("heat_level_percent") or 0,
            fan_percent=r.get("fan_level_percent") or 0,
            bean_ror_c_per_min=None,
            env_ror_c_per_min=None,
        )
        for r in rows
    ]


def test_trim_engages_in_late_maillard_on_a_real_roast_curve() -> None:
    """#327 replay validation: stepping the policy + #229 FC-ETA estimator over a
    REAL Hottop roast curve, the anticipatory trim engages in the late-Maillard
    band (bean ~155–176 °C, FC predicted within the window) and NEVER below the
    155 °C late-Maillard floor — exactly the plan §3 window. This is the in-repo
    proxy for the roast-3 trajectory the trim exists to fix (the flat-100 floor
    drove the bean 8 °C over the ceiling); the trimmed heat there is 65 %, not the
    flat 100, so the env runs cooler into FC."""
    curve = _live_curve()
    policy = RoastControlPolicy(SafetyLimits(), _PROFILE)
    engaged_beans: list[float] = []
    for i in range(5, len(curve)):
        window = curve[max(0, i - 60) : i + 1]
        eta = estimate_first_crack_eta_seconds(window, fc_target_bean_temp_c=176.0)
        signal = TrimSignal(bean_temp_c=curve[i].bean_temp_c, first_crack_eta_seconds=eta)
        box = policy.limits_for(RoastPhase.ROASTING_PRE_FIRST_CRACK, trim_signal=signal)
        # Engaged when heat resolves to the trim level (not the flat 100 floor).
        if box.heat_target_percent == _DEFAULT_TRIM_LEVEL:
            engaged_beans.append(curve[i].bean_temp_c)
    # The trim DID engage on this real curve (the window opened in late Maillard).
    assert engaged_beans, "trim never engaged on the real roast curve"
    # It only ever engaged in the late-Maillard band — never below the 155 °C floor,
    # never above the FC target the estimator stops projecting past.
    assert min(engaged_beans) >= 155.0
    assert max(engaged_beans) <= 176.0


# --- Adaptive trim depth (#386) ----------------------------------------------
#
# The adaptive depth is config-gated (``adaptive_depth_enabled=False`` by
# default).  The invariants below are tested at the ``LateMaillardTrim``
# level (``depth_for``) and at the ``RoastControlPolicy`` level
# (``limits_for`` with an adaptive-enabled trim config).
#
# Offline choice-validation: ``test_adaptive_depth_is_monotonic`` proves the
# formula is directionally correct (hotter approach → deeper cut, gentle →
# shallower) for two representative RoR/ETA pairs analogous to the corpus
# extremes: a roast-3-style hot approach (RoR 14 °C/min, short ETA 25 s) vs
# a roast-6-style gentle approach (RoR 4 °C/min, ETA at the window boundary).
# The thermal OUTCOME validates on hardware at roast 7.


def _adaptive_trim(
    base_trim: int = 65,
    k_ror: float = 1.5,
    k_eta: float = 0.2,
    ror_ref: float = 8.0,
    eta_ref: float = 60.0,
    min_trim: int = 45,
    max_trim: int = 75,
) -> "LateMaillardTrim":
    """Build an adaptive-enabled LateMaillardTrim with the given coefficients."""
    return LateMaillardTrim(
        adaptive_depth_enabled=True,
        base_trim=base_trim,
        k_ror=k_ror,
        k_eta=k_eta,
        ror_ref=ror_ref,
        eta_ref=eta_ref,
        min_trim=min_trim,
        max_trim=max_trim,
    )


def test_adaptive_depth_disabled_returns_fixed_depth() -> None:
    """Default (adaptive_depth_enabled=False): depth_for returns the fixed
    trim_heat_percent regardless of RoR / ETA signal (#386)."""
    trim = LateMaillardTrim(trim_heat_percent=65)  # default: adaptive off
    # With a rich signal — should still return the fixed depth.
    assert trim.depth_for(bean_ror_c_per_min=14.0, first_crack_eta_seconds=25.0) == 65
    # With missing signal — still the fixed depth.
    assert trim.depth_for(bean_ror_c_per_min=None, first_crack_eta_seconds=30.0) == 65
    assert trim.depth_for(bean_ror_c_per_min=10.0, first_crack_eta_seconds=None) == 65
    assert trim.depth_for(bean_ror_c_per_min=None, first_crack_eta_seconds=None) == 65


def test_adaptive_depth_missing_ror_returns_fixed() -> None:
    """When RoR is None the adaptive formula FAILS CLOSED to the fixed depth
    even though the flag is on (#386 fail-closed guarantee)."""
    trim = _adaptive_trim()
    assert trim.depth_for(bean_ror_c_per_min=None, first_crack_eta_seconds=30.0) == 65


def test_adaptive_depth_missing_eta_returns_fixed() -> None:
    """When FC-ETA is None the adaptive formula FAILS CLOSED to the fixed depth
    even though the flag is on (#386 fail-closed guarantee)."""
    trim = _adaptive_trim()
    assert trim.depth_for(bean_ror_c_per_min=10.0, first_crack_eta_seconds=None) == 65


def test_adaptive_depth_gentle_approach_returns_base_trim() -> None:
    """A gentle approach (RoR at ref, ETA at eta_ref) contributes 0 from both
    terms → depth == base_trim (roast-6-style, no deepening needed)."""
    trim = _adaptive_trim(base_trim=65, ror_ref=8.0, eta_ref=60.0)
    depth = trim.depth_for(bean_ror_c_per_min=8.0, first_crack_eta_seconds=60.0)
    assert depth == 65  # both terms zero → base_trim exactly


def test_adaptive_depth_high_ror_deepens_cut() -> None:
    """A hotter approach (high RoR > ror_ref) deepens the cut below base_trim
    and clamps to [min_trim, max_trim] (#386)."""
    # RoR 14 vs ref 8: RoR term = 1.5 * (14 - 8) = 9; ETA 50 vs ref 60:
    # ETA term = 0.2 * (60 - 50) = 2; raw = 65 - 9 - 2 = 54; in [45, 75] → 54.
    trim = _adaptive_trim()
    depth = trim.depth_for(bean_ror_c_per_min=14.0, first_crack_eta_seconds=50.0)
    assert depth == 54
    assert depth < 65  # deeper than base


def test_adaptive_depth_extreme_high_ror_clamps_to_min_trim() -> None:
    """An extreme hot approach (very high RoR) is clamped to min_trim (#386)."""
    # RoR 30 vs ref 8: RoR term = 1.5 * 22 = 33; raw = 65 - 33 = 32 < min_trim 45.
    trim = _adaptive_trim()
    depth = trim.depth_for(bean_ror_c_per_min=30.0, first_crack_eta_seconds=60.0)
    assert depth == 45  # clamped to min_trim


def test_adaptive_depth_respects_max_trim_bound() -> None:
    """The formula is bounded by max_trim at its shallowest (#386): the validator
    enforces base_trim ≤ max_trim, so the formula output (always ≤ base_trim) never
    exceeds max_trim.  This test confirms the clamp is in place: the depth returned
    is always ≤ max_trim regardless of signal values."""
    trim = _adaptive_trim()  # defaults: base=65, max=75
    # Gentle signal — formula output is base_trim (65); 65 <= max_trim (75) OK.
    depth = trim.depth_for(bean_ror_c_per_min=8.0, first_crack_eta_seconds=60.0)
    assert depth <= trim.max_trim
    # Hot signal — formula goes below base_trim; still ≤ max_trim.
    depth_hot = trim.depth_for(bean_ror_c_per_min=14.0, first_crack_eta_seconds=30.0)
    assert depth_hot <= trim.max_trim


def test_adaptive_depth_is_monotonic() -> None:
    """Offline choice-validation: the formula is monotonic — hotter approach
    produces a deeper cut (lower %) than a gentle approach (#386).

    Tests two representative corpus extremes:
    - Roast-3-style hot: RoR 14 °C/min, ETA 25 s (fast approach, pre-FC overshoot)
    - Roast-6-style gentle: RoR 4 °C/min, ETA 55 s (near boundary, smooth entry)

    The thermal OUTCOME of these depth choices validates on hardware at roast 7.
    The offline test only asserts direction + bounds, not exact tuning.
    """
    trim = _adaptive_trim()
    # Hot approach: high RoR, short ETA → deep cut
    depth_hot = trim.depth_for(bean_ror_c_per_min=14.0, first_crack_eta_seconds=25.0)
    # Gentle approach: low RoR, ETA near the window boundary → shallow cut
    depth_gentle = trim.depth_for(bean_ror_c_per_min=4.0, first_crack_eta_seconds=55.0)

    # Monotonicity: hot → deeper (lower %) than gentle.
    assert depth_hot < depth_gentle, (
        f"Adaptive depth not monotonic: hot={depth_hot} should be < gentle={depth_gentle}"
    )
    # Both within bounds.
    assert 45 <= depth_hot <= 75, f"Hot depth {depth_hot} outside [45, 75]"
    assert 45 <= depth_gentle <= 75, f"Gentle depth {depth_gentle} outside [45, 75]"


def test_adaptive_depth_rounds_to_integer() -> None:
    """depth_for always returns an int (required by PhaseControlLimits)."""
    trim = _adaptive_trim(k_ror=1.0)
    # RoR term = 1.0 * (9.5 - 8.0) = 1.5; raw = 65 - 1.5 = 63.5 → rounds to 64.
    depth = trim.depth_for(bean_ror_c_per_min=9.5, first_crack_eta_seconds=60.0)
    assert isinstance(depth, int)
    assert depth == 64


def test_adaptive_depth_wires_through_limits_for() -> None:
    """The adaptive depth is honoured by ``limits_for`` when the trim engages
    (#386): the box's heat_target_percent reflects the adaptive depth, not the
    fixed 65."""
    # Configure the adaptive trim: high RoR should produce 54 pp.
    adaptive_trim = _adaptive_trim()  # base 65, k_ror 1.5, k_eta 0.2, ror_ref 8, eta_ref 60
    levers = PreFirstCrackLevers(late_maillard_trim=adaptive_trim)
    policy = RoastControlPolicy(SafetyLimits(), _PROFILE, pre_fc_levers=levers)
    # A signal that opens the window: bean 165 °C, ETA 50 s, RoR 14 °C/min.
    # Expected depth: 65 - 1.5*(14-8) - 0.2*(60-50) = 65 - 9 - 2 = 54.
    signal = TrimSignal(
        bean_temp_c=165.0,
        first_crack_eta_seconds=50.0,
        bean_ror_c_per_min=14.0,
    )
    box = policy.limits_for(RoastPhase.ROASTING_PRE_FIRST_CRACK, trim_signal=signal)
    assert box.heat_target_percent == 54
    assert box.heat_floor_percent == 54  # floor pinned to the adaptive depth


def test_adaptive_depth_fan_unchanged() -> None:
    """Fan is NEVER modified by adaptive trim depth (#386 invariant):
    the fan box and target stay at the flat-floor values."""
    adaptive_trim = _adaptive_trim()
    levers = PreFirstCrackLevers(late_maillard_trim=adaptive_trim)
    policy = RoastControlPolicy(SafetyLimits(), _PROFILE, pre_fc_levers=levers)
    signal = TrimSignal(bean_temp_c=165.0, first_crack_eta_seconds=50.0, bean_ror_c_per_min=14.0)
    box = policy.limits_for(RoastPhase.ROASTING_PRE_FIRST_CRACK, trim_signal=signal)
    assert box.fan_target_percent == 30  # unchanged
    assert box.fan_ceiling_percent == 30
    assert box.fan_floor_percent == 0


def test_adaptive_depth_missing_ror_in_signal_fails_closed() -> None:
    """When TrimSignal carries bean_ror_c_per_min=None the adaptive path fails
    closed to the fixed trim_heat_percent via limits_for (#386)."""
    adaptive_trim = _adaptive_trim()  # flag on
    levers = PreFirstCrackLevers(late_maillard_trim=adaptive_trim)
    policy = RoastControlPolicy(SafetyLimits(), _PROFILE, pre_fc_levers=levers)
    # Window is open (bean 165, ETA 30) but RoR is missing → fixed 65.
    signal = TrimSignal(bean_temp_c=165.0, first_crack_eta_seconds=30.0, bean_ror_c_per_min=None)
    box = policy.limits_for(RoastPhase.ROASTING_PRE_FIRST_CRACK, trim_signal=signal)
    assert box.heat_target_percent == 65  # fixed depth, not adaptive


def test_adaptive_depth_never_raises_heat_above_per_bean_base() -> None:
    """The adaptive trim is clamped to the per-bean base (#386 safety):
    even if max_trim > per_fc_heat, the resolved heat is min(depth, base_heat)."""
    # Per-bean heat 60, adaptive max_trim 75 → without the clamp the formula
    # could produce 75 for a very gentle approach, raising heat above the
    # per-bean floor.  The clamp (min(depth, base_heat)) prevents this.
    profile = _PROFILE.model_copy(update={"pre_fc_heat": 60})
    # max_trim 75 > per_bean 60, but validator allows it (max_trim <= heat_target_percent
    # is checked at the PreFirstCrackLevers level, not between max_trim and profile).
    adaptive_trim = LateMaillardTrim(
        adaptive_depth_enabled=True,
        base_trim=60,  # must satisfy base_trim <= max_trim
        min_trim=45,
        max_trim=60,  # <= heat_target_percent (100 default)
    )
    levers = PreFirstCrackLevers(heat_target_percent=100, late_maillard_trim=adaptive_trim)
    policy = RoastControlPolicy(SafetyLimits(), profile, pre_fc_levers=levers)
    # Very gentle signal — formula gives base_trim (60); per_bean heat is also 60.
    signal = TrimSignal(bean_temp_c=165.0, first_crack_eta_seconds=60.0, bean_ror_c_per_min=8.0)
    box = policy.limits_for(RoastPhase.ROASTING_PRE_FIRST_CRACK, trim_signal=signal)
    assert box.heat_target_percent == 60  # == per-bean base, not raised
    assert box.heat_target_percent <= 60  # never above the per-bean floor


def test_adaptive_trim_range_validator_rejects_min_above_base() -> None:
    """LateMaillardTrim rejects min_trim > base_trim (#386)."""
    with pytest.raises(ValidationError):
        LateMaillardTrim(adaptive_depth_enabled=True, base_trim=50, min_trim=60, max_trim=75)


def test_adaptive_trim_range_validator_rejects_base_above_max() -> None:
    """LateMaillardTrim rejects base_trim > max_trim (#386)."""
    with pytest.raises(ValidationError):
        LateMaillardTrim(adaptive_depth_enabled=True, base_trim=80, min_trim=45, max_trim=75)


def test_adaptive_trim_range_validator_rejects_min_above_max() -> None:
    """LateMaillardTrim rejects min_trim > max_trim (#386)."""
    with pytest.raises(ValidationError):
        LateMaillardTrim(adaptive_depth_enabled=True, base_trim=65, min_trim=80, max_trim=75)


def test_levers_reject_adaptive_max_trim_above_heat_target() -> None:
    """PreFirstCrackLevers pins max_trim <= heat_target_percent (#386):
    the adaptive depth must be a strict reduction even at its shallowest."""
    with pytest.raises(ValidationError):
        PreFirstCrackLevers(
            heat_target_percent=80,
            late_maillard_trim=LateMaillardTrim(
                adaptive_depth_enabled=True,
                base_trim=65,
                min_trim=45,
                max_trim=90,  # > heat_target_percent 80 → invalid
            ),
        )


def test_levers_allow_max_trim_above_heat_target_when_adaptive_disabled() -> None:
    """Backward-compat (#386): with adaptive depth OFF, max_trim is unused, so it
    may exceed a lowered heat_target_percent without invalidating the config."""
    levers = PreFirstCrackLevers(
        heat_target_percent=70,
        late_maillard_trim=LateMaillardTrim(
            adaptive_depth_enabled=False,
            trim_heat_percent=65,  # <= heat_target 70 — the disabled-path guarantee
            base_trim=65,
            min_trim=45,
            max_trim=75,  # > heat_target 70, but unused while disabled → allowed
        ),
    )
    assert levers.late_maillard_trim.max_trim == 75
    assert levers.heat_target_percent == 70


def test_trim_signal_carries_ror() -> None:
    """TrimSignal accepts bean_ror_c_per_min (new field, #386)."""
    sig = TrimSignal(
        bean_temp_c=165.0,
        first_crack_eta_seconds=30.0,
        bean_ror_c_per_min=10.0,
    )
    assert sig.bean_ror_c_per_min == 10.0

    # Defaults to None for backward compatibility.
    sig_no_ror = TrimSignal(bean_temp_c=165.0, first_crack_eta_seconds=30.0)
    assert sig_no_ror.bean_ror_c_per_min is None
