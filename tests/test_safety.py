"""E3-S1: temperature and pre-T0 overrun rules (component plan §8;
orchestration plan § Safety Policy, § Milestone 1 Module Blueprint).

Telemetry validity (E3-S2), command validation (E3-S3), e-stop plumbing
(E3-S4), and phase/source validity (E3-S5, D16) extend this suite.
"""

import pytest

from roastpilot_agent.config import SafetyLimits
from roastpilot_agent.models import RoastPhase
from roastpilot_agent.safety import SafetyPolicy, SafetyVerdict


@pytest.fixture
def policy() -> SafetyPolicy:
    return SafetyPolicy(SafetyLimits())


def test_all_clear_within_limits(policy: SafetyPolicy) -> None:
    evaluation = policy.evaluate_telemetry(
        phase=RoastPhase.DEVELOPMENT, bean_temp_c=195.0, env_temp_c=215.0, t0_confirmed=True
    )
    assert evaluation.verdict is SafetyVerdict.ALLOW
    assert evaluation.rule == "all_clear"
    assert evaluation.adjusted_heat is None
    assert evaluation.adjusted_fan is None


def test_max_bean_temp_triggers_emergency_stop(policy: SafetyPolicy) -> None:
    evaluation = policy.evaluate_telemetry(
        phase=RoastPhase.DEVELOPMENT, bean_temp_c=230.1, env_temp_c=220.0, t0_confirmed=True
    )
    assert evaluation.verdict is SafetyVerdict.EMERGENCY_STOP
    assert evaluation.rule == "max_bean_temp"


def test_max_env_temp_triggers_emergency_stop(policy: SafetyPolicy) -> None:
    evaluation = policy.evaluate_telemetry(
        phase=RoastPhase.ROASTING_PRE_FIRST_CRACK,
        bean_temp_c=180.0,
        env_temp_c=240.1,
        t0_confirmed=True,
    )
    assert evaluation.verdict is SafetyVerdict.EMERGENCY_STOP
    assert evaluation.rule == "max_env_temp"


def test_bean_ceiling_takes_precedence_when_both_breached(policy: SafetyPolicy) -> None:
    evaluation = policy.evaluate_telemetry(
        phase=RoastPhase.DEVELOPMENT, bean_temp_c=235.0, env_temp_c=245.0, t0_confirmed=True
    )
    assert evaluation.verdict is SafetyVerdict.EMERGENCY_STOP
    assert evaluation.rule == "max_bean_temp"


@pytest.mark.parametrize(("bean", "env"), [(230.0, 220.0), (200.0, 240.0)])
def test_ceilings_are_strict_bounds(policy: SafetyPolicy, bean: float, env: float) -> None:
    """A reading exactly at a ceiling does not trip it."""
    evaluation = policy.evaluate_telemetry(
        phase=RoastPhase.DEVELOPMENT, bean_temp_c=bean, env_temp_c=env, t0_confirmed=True
    )
    assert evaluation.verdict is SafetyVerdict.ALLOW


def test_pre_t0_overrun_default_severity_is_recovery(policy: SafetyPolicy) -> None:
    evaluation = policy.evaluate_telemetry(
        phase=RoastPhase.PREHEATING, bean_temp_c=200.1, env_temp_c=210.0, t0_confirmed=False
    )
    assert evaluation.verdict is SafetyVerdict.RECOVERY
    assert evaluation.rule == "pre_t0_overrun"
    assert evaluation.adjusted_heat == 0
    assert evaluation.adjusted_fan == 100


def test_pre_t0_overrun_fault_severity() -> None:
    policy = SafetyPolicy(SafetyLimits(pre_t0_overrun_severity="fault"))
    evaluation = policy.evaluate_telemetry(
        phase=RoastPhase.PREHEATING, bean_temp_c=200.1, env_temp_c=210.0, t0_confirmed=False
    )
    assert evaluation.verdict is SafetyVerdict.FAULT
    assert evaluation.rule == "pre_t0_overrun"
    assert evaluation.adjusted_heat == 0
    assert evaluation.adjusted_fan == 100


def test_pre_t0_overrun_exact_bound_does_not_trip(policy: SafetyPolicy) -> None:
    evaluation = policy.evaluate_telemetry(
        phase=RoastPhase.PREHEATING, bean_temp_c=200.0, env_temp_c=210.0, t0_confirmed=False
    )
    assert evaluation.verdict is SafetyVerdict.ALLOW


def test_pre_t0_overrun_requires_preheating_phase(policy: SafetyPolicy) -> None:
    """201 °C is a normal bean temperature once roasting — only preheating
    without confirmed T0 makes it an overrun."""
    for phase in (RoastPhase.ROASTING_PRE_FIRST_CRACK, RoastPhase.DEVELOPMENT):
        evaluation = policy.evaluate_telemetry(
            phase=phase, bean_temp_c=201.0, env_temp_c=215.0, t0_confirmed=True
        )
        assert evaluation.verdict is SafetyVerdict.ALLOW


def test_pre_t0_overrun_not_applied_once_t0_confirmed(policy: SafetyPolicy) -> None:
    """Confirmed T0 during the preheating->roasting handover window must not
    fire the overrun rule (no confirmed T0 is a rule precondition)."""
    evaluation = policy.evaluate_telemetry(
        phase=RoastPhase.PREHEATING, bean_temp_c=201.0, env_temp_c=215.0, t0_confirmed=True
    )
    assert evaluation.verdict is SafetyVerdict.ALLOW


def test_hard_ceiling_outranks_overrun_in_preheating(policy: SafetyPolicy) -> None:
    """A pre-T0 reading above the bean ceiling is an emergency stop, not a
    recovery — severity order is ceilings first."""
    evaluation = policy.evaluate_telemetry(
        phase=RoastPhase.PREHEATING, bean_temp_c=231.0, env_temp_c=215.0, t0_confirmed=False
    )
    assert evaluation.verdict is SafetyVerdict.EMERGENCY_STOP
    assert evaluation.rule == "max_bean_temp"


def test_custom_safe_fan_value_is_used() -> None:
    policy = SafetyPolicy(SafetyLimits(overrun_safe_fan_percent=80))
    evaluation = policy.evaluate_telemetry(
        phase=RoastPhase.PREHEATING, bean_temp_c=205.0, env_temp_c=210.0, t0_confirmed=False
    )
    assert evaluation.adjusted_fan == 80


def test_evaluations_are_persisted_ready(policy: SafetyPolicy) -> None:
    """Every outcome carries rule + reason (plan §5 safety_evaluations)."""
    for bean, env, phase, t0 in [
        (195.0, 215.0, RoastPhase.DEVELOPMENT, True),
        (231.0, 215.0, RoastPhase.DEVELOPMENT, True),
        (180.0, 241.0, RoastPhase.PREHEATING, False),
        (205.0, 210.0, RoastPhase.PREHEATING, False),
    ]:
        evaluation = policy.evaluate_telemetry(
            phase=phase, bean_temp_c=bean, env_temp_c=env, t0_confirmed=t0
        )
        assert evaluation.rule
        assert evaluation.reason
