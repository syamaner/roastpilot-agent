"""E2-S1: shared enum vocabulary tests (component plan §3, §5; D15/D16).

Round-trip and invariant coverage for every shared enum, plus the typed
safety handshake's JSON round trip.
"""

from enum import Enum

import pytest

from roastpilot_agent.models import RoastEventKind, RoastEventSource, RoastPhase
from roastpilot_agent.safety import SafetyEvaluation, SafetyVerdict

ALL_SHARED_ENUMS: list[type[Enum]] = [
    RoastPhase,
    RoastEventKind,
    RoastEventSource,
    SafetyVerdict,
]


@pytest.mark.parametrize("enum_type", ALL_SHARED_ENUMS)
def test_round_trip_by_value(enum_type: type[Enum]) -> None:
    """Every member reconstructs from its value (the serialization form)."""
    for member in enum_type:
        assert enum_type(member.value) is member


@pytest.mark.parametrize("enum_type", ALL_SHARED_ENUMS)
def test_members_are_not_str(enum_type: type[Enum]) -> None:
    """D15: plain Enum, never StrEnum — string comparison must not be possible."""
    assert all(not isinstance(member, str) for member in enum_type)


@pytest.mark.parametrize("enum_type", ALL_SHARED_ENUMS)
def test_values_are_unique_snake_case(enum_type: type[Enum]) -> None:
    """Values are the persisted/SSE wire form: unique, lowercase snake_case."""
    values = [member.value for member in enum_type]
    assert len(values) == len(set(values))
    for value in values:
        assert isinstance(value, str)
        assert value
        assert value == value.lower()
        assert " " not in value


def test_event_kinds_match_plan() -> None:
    """The agent event vocabulary from plan §5 (kinds) and §6 (SSE types,
    minus transport-only telemetry/heartbeat)."""
    assert {kind.value for kind in RoastEventKind} == {
        "run_started",
        "phase_changed",
        "charge_guidance",
        "t0_detected",
        "first_crack",
        "advisory",
        "command_executed",
        "command_failed",
        "safety_alert",
        "fault",
        "recovery_required",
        "recovery_acknowledged",
        "logs_exported",
        "run_completed",
    }


def test_event_sources_match_plan() -> None:
    """roast_events.source vocabulary from plan §5."""
    assert {source.value for source in RoastEventSource} == {
        "controller",
        "mcp",
        "operator",
        "advisor",
        "safety",
    }


def test_safety_evaluation_json_round_trip() -> None:
    """SafetyEvaluation survives a JSON round trip with the verdict typed."""
    evaluation = SafetyEvaluation(
        verdict=SafetyVerdict.CLAMP,
        adjusted_heat=80,
        adjusted_fan=60,
        reason="max bean temp approached",
    )
    restored = SafetyEvaluation.model_validate_json(evaluation.model_dump_json())
    assert restored == evaluation
    assert restored.verdict is SafetyVerdict.CLAMP


def test_safety_evaluation_round_trip_without_adjusted_command() -> None:
    """Nullable adjusted values (D15) survive the round trip as None."""
    evaluation = SafetyEvaluation(verdict=SafetyVerdict.RECOVERY, reason="restart with active run")
    restored = SafetyEvaluation.model_validate_json(evaluation.model_dump_json())
    assert restored == evaluation
    assert restored.adjusted_heat is None
    assert restored.adjusted_fan is None
