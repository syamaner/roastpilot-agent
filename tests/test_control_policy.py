"""RoastControlPolicy — single-source, phase-resolved control limits (#273).

D35 §8.2/8.3: the per-phase ``RoastControlPolicy`` is the ONE place control
limits are defined, feeding BOTH the advisor context (what the model is told) AND
the safety gate (what the harness enforces). The crux is *told == enforced*: the
heat/fan box placed in ``AdvisorContext`` is the SAME box ``evaluate_command``
clamps a command into, both sourced from one policy object. These tests prove
that equality and pin that #273 introduces no second copy and changes none of the
six safety verdicts.
"""

import pytest
from pydantic import ValidationError

from roastpilot_agent.advisor import AdvisorContext
from roastpilot_agent.config import SafetyLimits
from roastpilot_agent.control_policy import PhaseControlLimits, RoastControlPolicy
from roastpilot_agent.models import RoastPhase, RoastProfile
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


def test_limits_for_resolves_full_lever_range_today() -> None:
    """Every phase resolves the full 0–100 heat/fan box (the #273 verdict no-op).

    #222 narrows this per phase on the same object; until then the box equals the
    safety gate's historical 0–100 clamp, so wiring the gate to the policy cannot
    change any verdict.
    """
    policy = RoastControlPolicy(SafetyLimits(), _PROFILE)
    for phase in _ALL_PHASES:
        limits = policy.limits_for(phase)
        assert limits.heat_floor_percent == 0
        assert limits.heat_ceiling_percent == 100
        assert limits.fan_floor_percent == 0
        assert limits.fan_ceiling_percent == 100


def test_bitter_and_emergency_ceilings_come_from_config() -> None:
    """The told drop/bitter ceiling + emergency-drop bound are the config values."""
    limits = SafetyLimits()
    policy = RoastControlPolicy(limits)  # no profile
    box = policy.limits_for(RoastPhase.DEVELOPMENT)
    assert box.bitter_ceiling_temp_c == limits.bitter_ceiling_temp_c == 196.0
    assert box.emergency_drop_temp_c == limits.emergency_drop_temp_c == 198.0


def test_bitter_ceiling_capped_at_lower_profile_drop_target() -> None:
    """A lighter roast is never told it may push to the 196 °C hard ceiling.

    The bitter ceiling is the hard ``SafetyLimits`` value, lowered to the
    profile's ``target_drop_temp_c`` when that target is lower.
    """
    light = _PROFILE.model_copy(update={"target_drop_temp_c": 190.0})
    box = RoastControlPolicy(SafetyLimits(), light).limits_for(RoastPhase.DEVELOPMENT)
    assert box.bitter_ceiling_temp_c == 190.0
    # The emergency-drop bound is profile-independent (the last-resort bound).
    assert box.emergency_drop_temp_c == 198.0


def test_bitter_ceiling_not_raised_by_higher_profile_target() -> None:
    """A high profile drop target never *loosens* the hard bitter ceiling."""
    high = _PROFILE.model_copy(update={"target_drop_temp_c": 210.0})
    box = RoastControlPolicy(SafetyLimits(), high).limits_for(RoastPhase.DEVELOPMENT)
    assert box.bitter_ceiling_temp_c == 196.0  # capped at the hard ceiling, not 210


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


def test_safety_limits_rejects_inverted_drop_ceilings() -> None:
    """SafetyLimits pins emergency_drop_temp_c above bitter_ceiling_temp_c."""
    with pytest.raises(ValidationError):
        SafetyLimits(bitter_ceiling_temp_c=200.0, emergency_drop_temp_c=198.0)


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
