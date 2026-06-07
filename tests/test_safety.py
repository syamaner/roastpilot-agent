"""E3-S1/E3-S2: temperature, overrun, and telemetry-validity rules
(component plan §8; orchestration plan § Safety Policy, § Milestone 1
Module Blueprint).

Command validation (E3-S3), e-stop plumbing (E3-S4), and phase/source
validity (E3-S5, D16) extend this suite.
"""

import pytest

from roastpilot_agent.config import SafetyLimits
from roastpilot_agent.models import ACTIVE_ROAST_PHASES, RoastPhase
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


# --- E3-S2: telemetry validity rules ---


INACTIVE_PHASES = [
    RoastPhase.IDLE,
    RoastPhase.STARTING,
    RoastPhase.COMPLETE,
    RoastPhase.FAULTED,
    RoastPhase.OPERATOR_RECOVERY_REQUIRED,
]

ACTIVE_PHASES = [
    RoastPhase.PREHEATING,
    RoastPhase.ROASTING_PRE_FIRST_CRACK,
    RoastPhase.DEVELOPMENT,
    RoastPhase.COOLING,
]


def test_active_roast_phases_vocabulary() -> None:
    assert frozenset(ACTIVE_PHASES) == ACTIVE_ROAST_PHASES


@pytest.mark.parametrize("phase", ACTIVE_PHASES)
def test_missing_telemetry_faults_closed_in_active_phases(
    policy: SafetyPolicy, phase: RoastPhase
) -> None:
    evaluation = policy.evaluate_telemetry_validity(
        phase=phase, telemetry_age_seconds=None, max_stale_seconds=3.0
    )
    assert evaluation.verdict is SafetyVerdict.FAULT
    assert evaluation.rule == "missing_telemetry"
    assert evaluation.adjusted_heat == 0
    assert evaluation.adjusted_fan == 100


@pytest.mark.parametrize("phase", ACTIVE_PHASES)
def test_stale_telemetry_faults_closed_in_active_phases(
    policy: SafetyPolicy, phase: RoastPhase
) -> None:
    evaluation = policy.evaluate_telemetry_validity(
        phase=phase, telemetry_age_seconds=3.1, max_stale_seconds=3.0
    )
    assert evaluation.verdict is SafetyVerdict.FAULT
    assert evaluation.rule == "stale_telemetry"
    assert evaluation.adjusted_heat == 0
    assert evaluation.adjusted_fan == 100


@pytest.mark.parametrize("phase", INACTIVE_PHASES)
def test_telemetry_validity_not_enforced_outside_active_roast(
    policy: SafetyPolicy, phase: RoastPhase
) -> None:
    """idle/starting/complete/faulted/recovery: no beans in play or no
    session telemetry yet — missing/stale telemetry must not fault."""
    for age in (None, 60.0):
        evaluation = policy.evaluate_telemetry_validity(
            phase=phase, telemetry_age_seconds=age, max_stale_seconds=3.0
        )
        assert evaluation.verdict is SafetyVerdict.ALLOW


def test_telemetry_exactly_at_staleness_bound_is_fresh(policy: SafetyPolicy) -> None:
    evaluation = policy.evaluate_telemetry_validity(
        phase=RoastPhase.DEVELOPMENT, telemetry_age_seconds=3.0, max_stale_seconds=3.0
    )
    assert evaluation.verdict is SafetyVerdict.ALLOW
    assert evaluation.rule == "all_clear"


def test_fresh_telemetry_is_all_clear(policy: SafetyPolicy) -> None:
    evaluation = policy.evaluate_telemetry_validity(
        phase=RoastPhase.PREHEATING, telemetry_age_seconds=0.4, max_stale_seconds=3.0
    )
    assert evaluation.verdict is SafetyVerdict.ALLOW


# --- E3-S2: MCP read/write failure rules ---


def test_zero_mcp_failures_all_clear(policy: SafetyPolicy) -> None:
    for evaluation in (
        policy.evaluate_mcp_failure(operation="read", consecutive_failures=0),
        policy.evaluate_mcp_failure(operation="write", consecutive_failures=0),
    ):
        assert evaluation.verdict is SafetyVerdict.ALLOW
        assert evaluation.rule == "all_clear"


def test_transient_mcp_read_failures_tolerated(policy: SafetyPolicy) -> None:
    for failures in (1, 2):
        evaluation = policy.evaluate_mcp_failure(operation="read", consecutive_failures=failures)
        assert evaluation.verdict is SafetyVerdict.ALLOW
        assert evaluation.rule == "mcp_read_failure_tolerated"


def test_exhausted_mcp_read_failures_fault_closed(policy: SafetyPolicy) -> None:
    evaluation = policy.evaluate_mcp_failure(operation="read", consecutive_failures=3)
    assert evaluation.verdict is SafetyVerdict.FAULT
    assert evaluation.rule == "mcp_read_failures_exhausted"
    assert evaluation.adjusted_heat == 0
    assert evaluation.adjusted_fan == 100


def test_exhausted_mcp_write_failures_fault_closed(policy: SafetyPolicy) -> None:
    evaluation = policy.evaluate_mcp_failure(operation="write", consecutive_failures=5)
    assert evaluation.verdict is SafetyVerdict.FAULT
    assert evaluation.rule == "mcp_write_failures_exhausted"
    assert evaluation.adjusted_heat == 0
    assert evaluation.adjusted_fan == 100


def test_mcp_failure_threshold_is_configurable() -> None:
    policy = SafetyPolicy(SafetyLimits(max_consecutive_mcp_failures=1))
    evaluation = policy.evaluate_mcp_failure(operation="write", consecutive_failures=1)
    assert evaluation.verdict is SafetyVerdict.FAULT


def test_negative_failure_count_is_a_programming_error(policy: SafetyPolicy) -> None:
    with pytest.raises(ValueError):
        policy.evaluate_mcp_failure(operation="read", consecutive_failures=-1)


def test_e3_s2_evaluations_are_persisted_ready(policy: SafetyPolicy) -> None:
    evaluations = [
        policy.evaluate_telemetry_validity(
            phase=RoastPhase.DEVELOPMENT, telemetry_age_seconds=None, max_stale_seconds=3.0
        ),
        policy.evaluate_telemetry_validity(
            phase=RoastPhase.COOLING, telemetry_age_seconds=10.0, max_stale_seconds=3.0
        ),
        policy.evaluate_mcp_failure(operation="read", consecutive_failures=4),
        policy.evaluate_mcp_failure(operation="write", consecutive_failures=1),
    ]
    for evaluation in evaluations:
        assert evaluation.rule
        assert evaluation.reason
