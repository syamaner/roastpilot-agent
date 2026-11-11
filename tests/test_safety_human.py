"""Truth tables for D230 safety evaluations and the RD-3 compatibility seam."""

from itertools import product

import pytest

from roastpilot_agent.config import SafetyLimits
from roastpilot_agent.human_protection import (
    CommandDisposition,
    ControlAuthority,
    EndpointDisposition,
    ReadingFreshness,
)
from roastpilot_agent.models import RoastCommand, RoastPhase
from roastpilot_agent.safety import COMMAND_PHASE_MATRIX, SafetyPolicy, SafetyVerdict


@pytest.mark.parametrize("authority", ControlAuthority)
@pytest.mark.parametrize("bt", [195.99, 196.0, 196.01])
def test_protective_drop_requires_only_d230_evidence(
    authority: ControlAuthority, bt: float
) -> None:
    # Changing the legacy target ceiling cannot change the D230 decision.
    policy = SafetyPolicy(SafetyLimits(bitter_ceiling_temp_c=197.0))
    evaluation = policy.evaluate_human_protective_drop(
        authority=authority,
        charged=True,
        phase=RoastPhase.ROASTING_PRE_FIRST_CRACK,
        freshness=ReadingFreshness.FRESH,
        bean_temp_c=bt,
        endpoint=EndpointDisposition.NONE,
        command=CommandDisposition.SETTLED,
    )
    assert evaluation.verdict is (
        SafetyVerdict.ALLOW
        if authority is ControlAuthority.HUMAN and bt >= 196
        else SafetyVerdict.REJECT
    )
    assert evaluation.adjusted_heat is evaluation.adjusted_fan is None


@pytest.mark.parametrize("phase", RoastPhase)
@pytest.mark.parametrize("charged", [True, False])
@pytest.mark.parametrize("freshness", ReadingFreshness)
def test_human_reading_recovery_requires_charge_active_phase_and_non_fresh_reading(
    phase: RoastPhase, charged: bool, freshness: ReadingFreshness
) -> None:
    result = SafetyPolicy(SafetyLimits()).evaluate_human_reading(
        phase=phase, charged=charged, freshness=freshness
    )
    active_phases = {
        RoastPhase.PREHEATING,
        RoastPhase.ROASTING_PRE_FIRST_CRACK,
        RoastPhase.DEVELOPMENT,
        RoastPhase.COOLING,
    }
    if charged and phase in active_phases and freshness is not ReadingFreshness.FRESH:
        assert result.verdict is SafetyVerdict.RECOVERY
        assert result.rule == "human_reading_invalid"
    else:
        assert result.verdict is SafetyVerdict.ALLOW
    assert result.adjusted_heat is result.adjusted_fan is None


def test_recovery_drop_truth_table() -> None:
    policy = SafetyPolicy(SafetyLimits())
    allowed = set(
        product(
            (RoastPhase.OPERATOR_RECOVERY_REQUIRED, RoastPhase.FAULTED),
            (True,),
            (True,),
            (ReadingFreshness.FRESH,),
            (EndpointDisposition.NONE, EndpointDisposition.WITHHELD),
            (CommandDisposition.SETTLED,),
        )
    )
    for phase, session_matches, charged, freshness, endpoint, command in product(
        RoastPhase,
        (False, True),
        (False, True),
        ReadingFreshness,
        EndpointDisposition,
        CommandDisposition,
    ):
        result = policy.evaluate_human_recovery_drop(
            phase=phase,
            session_matches=session_matches,
            charged=charged,
            freshness_at_eval=freshness,
            endpoint=endpoint,
            command=command,
        )
        key = (phase, session_matches, charged, freshness, endpoint, command)
        expected = SafetyVerdict.ALLOW if key in allowed else SafetyVerdict.REJECT
        assert result.verdict is expected, key
        assert result.adjusted_heat is result.adjusted_fan is None, key
        if expected is SafetyVerdict.REJECT:
            assert "physical intervention" in result.reason, key


def test_continuation_truth_table() -> None:
    policy = SafetyPolicy(SafetyLimits())
    allowed = {
        (
            RoastPhase.ROASTING_PRE_FIRST_CRACK,
            True,
            True,
            False,
            ReadingFreshness.FRESH,
            EndpointDisposition.NONE,
            CommandDisposition.SETTLED,
        ),
        (
            RoastPhase.DEVELOPMENT,
            True,
            True,
            True,
            ReadingFreshness.FRESH,
            EndpointDisposition.NONE,
            CommandDisposition.SETTLED,
        ),
    }
    for target, session_matches, charged, canonical_fc, freshness, endpoint, command in product(
        RoastPhase,
        (False, True),
        (False, True),
        (False, True),
        ReadingFreshness,
        EndpointDisposition,
        CommandDisposition,
    ):
        result = policy.evaluate_human_continuation(
            target=target,
            session_matches=session_matches,
            charged=charged,
            canonical_human_fc_confirmed=canonical_fc,
            freshness=freshness,
            endpoint=endpoint,
            command=command,
        )
        key = (target, session_matches, charged, canonical_fc, freshness, endpoint, command)
        expected = SafetyVerdict.ALLOW if key in allowed else SafetyVerdict.REJECT
        assert result.verdict is expected, key
        assert result.adjusted_heat is result.adjusted_fan is None, key


def test_drop_row_excludes_recovery() -> None:
    assert COMMAND_PHASE_MATRIX[RoastCommand.DROP_BEANS] == frozenset(
        {RoastPhase.ROASTING_PRE_FIRST_CRACK, RoastPhase.DEVELOPMENT, RoastPhase.FAULTED}
    )


@pytest.mark.parametrize(
    ("age", "automatic_rule"), [(None, "missing_telemetry"), (5.1, "stale_telemetry")]
)
def test_human_charged_session_clock_stale_maps_recovery_read_failure_stays_fault(
    age: float | None, automatic_rule: str
) -> None:
    limits = SafetyLimits()
    policy = SafetyPolicy(limits)
    phase = RoastPhase.ROASTING_PRE_FIRST_CRACK
    human = policy.evaluate_telemetry_validity(
        phase=phase,
        telemetry_age_seconds=age,
        max_stale_seconds=5.0,
        authority=ControlAuthority.HUMAN,
        charged=True,
    )
    assert human.verdict is SafetyVerdict.RECOVERY
    assert human.rule == "human_reading_invalid"
    assert human.adjusted_heat is human.adjusted_fan is None

    original = policy.evaluate_telemetry_validity(
        phase=phase, telemetry_age_seconds=age, max_stale_seconds=5.0
    )
    automatic = policy.evaluate_telemetry_validity(
        phase=phase, telemetry_age_seconds=age, max_stale_seconds=5.0, charged=True
    )
    uncharged_human = policy.evaluate_telemetry_validity(
        phase=phase,
        telemetry_age_seconds=age,
        max_stale_seconds=5.0,
        authority=ControlAuthority.HUMAN,
        charged=False,
    )
    assert original == automatic == uncharged_human
    assert original.verdict is SafetyVerdict.FAULT
    assert original.rule == automatic_rule
    assert original.adjusted_heat == 0
    assert original.adjusted_fan == limits.overrun_safe_fan_percent

    read_failure = policy.evaluate_mcp_failure(
        operation="read", consecutive_failures=limits.max_consecutive_mcp_failures
    )
    assert read_failure.verdict is SafetyVerdict.FAULT
    assert read_failure.rule == "mcp_read_failures_exhausted"


def test_rd3_preserves_exact_freshness_limit() -> None:
    evaluation = SafetyPolicy(SafetyLimits()).evaluate_telemetry_validity(
        phase=RoastPhase.DEVELOPMENT,
        telemetry_age_seconds=5.0,
        max_stale_seconds=5.0,
        authority=ControlAuthority.HUMAN,
        charged=True,
    )
    assert evaluation.verdict is SafetyVerdict.ALLOW
