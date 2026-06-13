"""E3-S1/E3-S2: temperature, overrun, and telemetry-validity rules
(component plan §8; orchestration plan § Safety Policy, § Milestone 1
Module Blueprint).

Command validation (E3-S3), e-stop plumbing (E3-S4), and phase/source
validity (E3-S5, D16) extend this suite.
"""

from typing import Literal

import pytest

from roastpilot_agent.config import SafetyLimits
from roastpilot_agent.models import (
    ACTIVE_ROAST_PHASES,
    OperatorAction,
    RoastCommand,
    RoastEventSource,
    RoastPhase,
)
from roastpilot_agent.safety import (
    COMMAND_PHASE_MATRIX,
    OPERATOR_ACTION_COMMAND,
    SafetyEvaluation,
    SafetyPolicy,
    SafetyVerdict,
    enabled_operator_actions,
)


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


# --- E3-S3: command validation rules ---


def test_command_within_bounds_allowed(policy: SafetyPolicy) -> None:
    evaluation = policy.evaluate_command(
        requested_heat=70, requested_fan=40, seconds_since_last_command=None
    )
    assert evaluation.verdict is SafetyVerdict.ALLOW
    assert evaluation.adjusted_heat == 70
    assert evaluation.adjusted_fan == 40


@pytest.mark.parametrize(("heat", "fan"), [(0, 0), (100, 100), (0, 100)])
def test_command_boundary_values_allowed(policy: SafetyPolicy, heat: int, fan: int) -> None:
    evaluation = policy.evaluate_command(
        requested_heat=heat, requested_fan=fan, seconds_since_last_command=None
    )
    assert evaluation.verdict is SafetyVerdict.ALLOW


@pytest.mark.parametrize(
    ("heat", "fan", "expected_heat", "expected_fan"),
    [
        (110, 40, 100, 40),
        (-5, 40, 0, 40),
        (70, 101, 70, 100),
        (70, -1, 70, 0),
        (150, -10, 100, 0),
    ],
)
def test_out_of_bounds_command_is_clamped(
    policy: SafetyPolicy, heat: int, fan: int, expected_heat: int, expected_fan: int
) -> None:
    evaluation = policy.evaluate_command(
        requested_heat=heat, requested_fan=fan, seconds_since_last_command=None
    )
    assert evaluation.verdict is SafetyVerdict.CLAMP
    assert evaluation.rule == "command_bounds"
    assert evaluation.adjusted_heat == expected_heat
    assert evaluation.adjusted_fan == expected_fan


def test_command_rate_limited(policy: SafetyPolicy) -> None:
    evaluation = policy.evaluate_command(
        requested_heat=70, requested_fan=40, seconds_since_last_command=1.0
    )
    assert evaluation.verdict is SafetyVerdict.REJECT
    assert evaluation.rule == "command_rate_limited"
    assert evaluation.adjusted_heat is None
    assert evaluation.adjusted_fan is None


def test_command_exactly_at_rate_limit_allowed(policy: SafetyPolicy) -> None:
    evaluation = policy.evaluate_command(
        requested_heat=70, requested_fan=40, seconds_since_last_command=2.0
    )
    assert evaluation.verdict is SafetyVerdict.ALLOW


def test_rate_limit_checked_before_bounds(policy: SafetyPolicy) -> None:
    """A rate-limited command is rejected outright, not clamped."""
    evaluation = policy.evaluate_command(
        requested_heat=150, requested_fan=40, seconds_since_last_command=0.5
    )
    assert evaluation.verdict is SafetyVerdict.REJECT
    assert evaluation.rule == "command_rate_limited"


def test_rate_limit_is_configurable() -> None:
    policy = SafetyPolicy(SafetyLimits(min_seconds_between_commands=5.0))
    evaluation = policy.evaluate_command(
        requested_heat=70, requested_fan=40, seconds_since_last_command=4.9
    )
    assert evaluation.verdict is SafetyVerdict.REJECT


def test_drop_recommendation_allowed_in_development(policy: SafetyPolicy) -> None:
    evaluation = policy.evaluate_drop_recommendation(phase=RoastPhase.DEVELOPMENT)
    assert evaluation.verdict is SafetyVerdict.ALLOW
    assert evaluation.rule == "drop_eligibility"


@pytest.mark.parametrize(
    "phase",
    [phase for phase in RoastPhase if phase is not RoastPhase.DEVELOPMENT],
)
def test_drop_recommendation_rejected_outside_development(
    policy: SafetyPolicy, phase: RoastPhase
) -> None:
    evaluation = policy.evaluate_drop_recommendation(phase=phase)
    assert evaluation.verdict is SafetyVerdict.REJECT
    assert evaluation.rule == "drop_eligibility"


@pytest.mark.parametrize("status", ["malformed", "unsafe"])
def test_advisor_failure_rejects_and_holds_current_targets(
    policy: SafetyPolicy, status: Literal["malformed", "unsafe"]
) -> None:
    # timeout / provider_error are the availability class (D30): they route to
    # evaluate_advisor_availability, not here — this rule is now the
    # reachable-but-misbehaving (malformed / unsafe) hold only.
    evaluation = policy.evaluate_advisor_failure(
        status=status,
        current_heat=65,
        current_fan=55,
    )
    assert evaluation.verdict is SafetyVerdict.REJECT
    assert evaluation.rule == f"advisor_{status}"
    assert evaluation.adjusted_heat == 65
    assert evaluation.adjusted_fan == 55


@pytest.mark.parametrize("failures", [1, 2])
def test_advisor_availability_below_threshold_holds_current_targets(
    policy: SafetyPolicy, failures: int
) -> None:
    """D30 (#166): availability failures below the default threshold (3) are
    tolerated — REJECT holding the current targets, exactly the prior
    hold-current behavior."""
    evaluation = policy.evaluate_advisor_availability(
        consecutive_failures=failures, current_heat=70, current_fan=30
    )
    assert evaluation.verdict is SafetyVerdict.REJECT
    assert evaluation.rule == "advisor_unavailable_tolerated"
    assert evaluation.adjusted_heat == 70
    assert evaluation.adjusted_fan == 30


def test_advisor_availability_at_threshold_fails_closed_to_recovery(policy: SafetyPolicy) -> None:
    """At the threshold the verdict is RECOVERY — heat 0 %, the configured safe
    fan — so the controller enters operator_recovery_required (not a fault)."""
    evaluation = policy.evaluate_advisor_availability(
        consecutive_failures=3, current_heat=70, current_fan=30
    )
    assert evaluation.verdict is SafetyVerdict.RECOVERY
    assert evaluation.rule == "advisor_unavailable_exhausted"
    assert evaluation.adjusted_heat == 0
    assert evaluation.adjusted_fan == SafetyLimits().overrun_safe_fan_percent


def test_advisor_availability_threshold_is_configurable() -> None:
    """``max_consecutive_advisor_failures`` arms the stop sooner/later."""
    policy = SafetyPolicy(SafetyLimits(max_consecutive_advisor_failures=2))
    assert (
        policy.evaluate_advisor_availability(
            consecutive_failures=1, current_heat=50, current_fan=50
        ).verdict
        is SafetyVerdict.REJECT
    )
    assert (
        policy.evaluate_advisor_availability(
            consecutive_failures=2, current_heat=50, current_fan=50
        ).verdict
        is SafetyVerdict.RECOVERY
    )


def test_advisor_availability_rejects_zero_failures(policy: SafetyPolicy) -> None:
    """The rule is only ever called after a failure: 0 is a programming error."""
    with pytest.raises(ValueError):
        policy.evaluate_advisor_availability(
            consecutive_failures=0, current_heat=50, current_fan=50
        )


def test_e3_s3_evaluations_are_persisted_ready(policy: SafetyPolicy) -> None:
    evaluations = [
        policy.evaluate_command(
            requested_heat=70, requested_fan=40, seconds_since_last_command=None
        ),
        policy.evaluate_command(
            requested_heat=120, requested_fan=40, seconds_since_last_command=None
        ),
        policy.evaluate_command(
            requested_heat=70, requested_fan=40, seconds_since_last_command=0.1
        ),
        policy.evaluate_drop_recommendation(phase=RoastPhase.PREHEATING),
        policy.evaluate_advisor_failure(status="malformed", current_heat=50, current_fan=50),
        policy.evaluate_advisor_availability(
            consecutive_failures=1, current_heat=50, current_fan=50
        ),
    ]
    for evaluation in evaluations:
        assert evaluation.rule
        assert evaluation.reason


# --- E3-S4: emergency stop and verdict plumbing ---


@pytest.mark.parametrize("phase", list(RoastPhase))
def test_emergency_stop_reachable_from_every_phase(policy: SafetyPolicy, phase: RoastPhase) -> None:
    evaluation = policy.evaluate_emergency_stop(phase=phase)
    assert evaluation.verdict is SafetyVerdict.EMERGENCY_STOP
    assert evaluation.rule == "emergency_stop"


def test_emergency_stop_is_structurally_ungated() -> None:
    """The e-stop rule must never grow advisor/UI/cloud gates: its
    signature accepts the phase and an operator reason — nothing else."""
    import inspect

    parameters = set(inspect.signature(SafetyPolicy.evaluate_emergency_stop).parameters)
    assert parameters == {"self", "phase", "operator_reason"}


def test_emergency_stop_records_operator_reason(policy: SafetyPolicy) -> None:
    evaluation = policy.evaluate_emergency_stop(
        phase=RoastPhase.DEVELOPMENT, operator_reason="smoke from the drum"
    )
    assert "smoke from the drum" in evaluation.reason


def test_command_evaluations_record_requested_inputs(policy: SafetyPolicy) -> None:
    """input_heat/input_fan persist exactly what was requested (plan §5),
    including out-of-range and rate-limited requests."""
    allowed = policy.evaluate_command(
        requested_heat=70, requested_fan=40, seconds_since_last_command=None
    )
    assert (allowed.input_heat, allowed.input_fan) == (70, 40)

    clamped = policy.evaluate_command(
        requested_heat=150, requested_fan=-10, seconds_since_last_command=None
    )
    assert (clamped.input_heat, clamped.input_fan) == (150, -10)
    assert (clamped.adjusted_heat, clamped.adjusted_fan) == (100, 0)

    rate_limited = policy.evaluate_command(
        requested_heat=80, requested_fan=60, seconds_since_last_command=0.5
    )
    assert (rate_limited.input_heat, rate_limited.input_fan) == (80, 60)
    assert rate_limited.adjusted_heat is None


def test_out_of_range_inputs_are_recordable() -> None:
    """input fields are deliberately unbounded — the trace must show the
    request exactly as made; only adjusted values are bounded."""
    evaluation = SafetyEvaluation(
        rule="command_bounds",
        verdict=SafetyVerdict.CLAMP,
        input_heat=1000,
        input_fan=-50,
        adjusted_heat=100,
        adjusted_fan=0,
        reason="recording an absurd request verbatim",
    )
    assert evaluation.input_heat == 1000
    assert evaluation.input_fan == -50


def test_every_rule_method_is_persisted_ready(policy: SafetyPolicy) -> None:
    """E3-S4 plumbing: every evaluation from every rule method carries a
    non-empty rule and reason, and a typed verdict."""
    evaluations = [
        policy.evaluate_telemetry(
            phase=RoastPhase.DEVELOPMENT, bean_temp_c=195.0, env_temp_c=215.0, t0_confirmed=True
        ),
        policy.evaluate_telemetry_validity(
            phase=RoastPhase.DEVELOPMENT, telemetry_age_seconds=None, max_stale_seconds=3.0
        ),
        policy.evaluate_mcp_failure(operation="read", consecutive_failures=4),
        policy.evaluate_command(
            requested_heat=70, requested_fan=40, seconds_since_last_command=None
        ),
        policy.evaluate_drop_recommendation(phase=RoastPhase.DEVELOPMENT),
        policy.evaluate_advisor_failure(status="malformed", current_heat=50, current_fan=50),
        policy.evaluate_advisor_availability(
            consecutive_failures=4, current_heat=50, current_fan=50
        ),
        policy.evaluate_emergency_stop(phase=RoastPhase.FAULTED),
    ]
    for evaluation in evaluations:
        assert evaluation.rule
        assert evaluation.reason
        assert isinstance(evaluation.verdict, SafetyVerdict)


# --- E3-S5: command×phase matrix and FC/T0 source validity (D16) ---


def test_matrix_covers_every_command() -> None:
    """Every MCP write command has a row — no command escapes the matrix."""
    assert set(COMMAND_PHASE_MATRIX) == set(RoastCommand)


@pytest.mark.parametrize("command", list(RoastCommand))
@pytest.mark.parametrize("phase", list(RoastPhase))
def test_command_phase_matrix_exhaustive(
    policy: SafetyPolicy, command: RoastCommand, phase: RoastPhase
) -> None:
    """All commands × all phases: the verdict matches the matrix cell and
    is always one of ALLOW/REJECT with rule command_phase_validity."""
    evaluation = policy.evaluate_command_phase(command=command, phase=phase)
    assert evaluation.rule == "command_phase_validity"
    expected = (
        SafetyVerdict.ALLOW if phase in COMMAND_PHASE_MATRIX[command] else SafetyVerdict.REJECT
    )
    assert evaluation.verdict is expected


def test_d16_canonical_invalid_combinations(policy: SafetyPolicy) -> None:
    """The two invalid examples named by D16."""
    heat_during_cooling = policy.evaluate_command_phase(
        command=RoastCommand.SET_HEAT, phase=RoastPhase.COOLING
    )
    assert heat_during_cooling.verdict is SafetyVerdict.REJECT

    stop_cooling_during_development = policy.evaluate_command_phase(
        command=RoastCommand.STOP_COOLING, phase=RoastPhase.DEVELOPMENT
    )
    assert stop_cooling_during_development.verdict is SafetyVerdict.REJECT


# --- E10 option (a) / D25: enabled_actions derivation over the matrix ---


@pytest.mark.parametrize("phase", list(RoastPhase))
def test_enabled_actions_match_matrix_for_mapped_actions(phase: RoastPhase) -> None:
    """The MCP-write actions in ``enabled_operator_actions`` are exactly the
    matrix rows for their backing command — a read-only projection, no second
    matrix. (The control-only three are pinned against the real controller in
    test_controller.py's biconditional test.)"""
    enabled = set(enabled_operator_actions(phase))
    for action, command in OPERATOR_ACTION_COMMAND.items():
        assert (action in enabled) is (phase in COMMAND_PHASE_MATRIX[command])


def test_enabled_actions_returns_declaration_order() -> None:
    """A stable, snapshot-friendly order (OperatorAction declaration order)."""
    enabled = enabled_operator_actions(RoastPhase.PREHEATING)
    ordered = [a for a in OperatorAction if a in set(enabled)]
    assert enabled == ordered


def test_enabled_actions_empty_of_writes_in_terminal_phases() -> None:
    """COMPLETE/FAULTED: no MCP-write action is permitted except e-stop (always),
    and acknowledge_recovery is excluded (not the recovery phase) — only the
    ungated advisory toggles + e-stop remain."""
    for phase in (RoastPhase.COMPLETE, RoastPhase.FAULTED):
        enabled = set(enabled_operator_actions(phase))
        assert enabled == {
            OperatorAction.EMERGENCY_STOP,
            OperatorAction.PAUSE_ADVISORY,
            OperatorAction.RESUME_ADVISORY,
        }


def test_every_operator_action_is_reachable_in_some_phase() -> None:
    """Exhaustiveness: every OperatorAction is enabled in at least one phase. The
    derivation fails OFF for an unwired action (the safe default), so this turns
    "someone added an action and forgot to wire it into enabled_operator_actions"
    into a red build rather than a silently-always-disabled button."""
    reachable = {a for phase in RoastPhase for a in enabled_operator_actions(phase)}
    assert reachable == set(OperatorAction)


def test_emergency_stop_matrix_row_is_every_phase() -> None:
    """The matrix must never contradict the E3-S4 e-stop invariant."""
    assert COMMAND_PHASE_MATRIX[RoastCommand.EMERGENCY_STOP] == frozenset(RoastPhase)


def test_set_heat_never_valid_without_active_control() -> None:
    """Heat is rejected in every phase outside preheating/roasting/development
    — including cooling, faulted, and recovery (no-auto-resume support)."""
    for phase in (
        RoastPhase.IDLE,
        RoastPhase.STARTING,
        RoastPhase.COOLING,
        RoastPhase.COMPLETE,
        RoastPhase.FAULTED,
        RoastPhase.OPERATOR_RECOVERY_REQUIRED,
    ):
        evaluation = policy_for_matrix().evaluate_command_phase(
            command=RoastCommand.SET_HEAT, phase=phase
        )
        assert evaluation.verdict is SafetyVerdict.REJECT


def policy_for_matrix() -> SafetyPolicy:
    return SafetyPolicy(SafetyLimits())


@pytest.mark.parametrize("transition", ["t0", "first_crack"])
@pytest.mark.parametrize("source", list(RoastEventSource))
def test_event_source_validity(
    policy: SafetyPolicy, transition: str, source: RoastEventSource
) -> None:
    """FC/T0 transitions: MCP detection and operator action only."""
    evaluation = policy.evaluate_event_source(
        transition=transition,  # pyright: ignore[reportArgumentType]
        source=source,
    )
    assert evaluation.rule == "event_source_validity"
    if source in (RoastEventSource.MCP, RoastEventSource.OPERATOR):
        assert evaluation.verdict is SafetyVerdict.ALLOW
    else:
        assert evaluation.verdict is SafetyVerdict.REJECT


def test_advisor_can_never_drive_t0_or_fc(policy: SafetyPolicy) -> None:
    """The talk's core invariant, stated directly."""
    for transition in ("t0", "first_crack"):
        evaluation = policy.evaluate_event_source(
            transition=transition,  # pyright: ignore[reportArgumentType]
            source=RoastEventSource.ADVISOR,
        )
        assert evaluation.verdict is SafetyVerdict.REJECT
        assert "operator" in evaluation.reason


def test_e3_s5_evaluations_are_persisted_ready(policy: SafetyPolicy) -> None:
    evaluations = [
        policy.evaluate_command_phase(command=RoastCommand.SET_HEAT, phase=RoastPhase.COOLING),
        policy.evaluate_command_phase(
            command=RoastCommand.DROP_BEANS, phase=RoastPhase.DEVELOPMENT
        ),
        policy.evaluate_event_source(transition="t0", source=RoastEventSource.MCP),
        policy.evaluate_event_source(transition="first_crack", source=RoastEventSource.SAFETY),
    ]
    for evaluation in evaluations:
        assert evaluation.rule
        assert evaluation.reason
