"""Behavioural coverage for MCP first-crack inference diagnostic retention (#1069)."""

import json
import math
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from roastpilot_agent.mcp_client import (
    FirstCrackStatus,
    RoastSessionState,
    parse_tool_result,
    project_mic_status,
    project_session_state,
)

_DIAGNOSTIC_VALUES: dict[str, int | float] = {
    "max_consecutive_overflow_count": 2,
    "inference_overrun_count": 3,
    "last_inference_duration_ms": 12.5,
    "max_inference_duration_ms": 640.0,
}
_COUNT_NAMES = ("max_consecutive_overflow_count", "inference_overrun_count")
_DURATION_NAMES = ("last_inference_duration_ms", "max_inference_duration_ms")
_INVALID_LIST = cast("list[object]", [])
_INVALID_DICT = cast("dict[str, object]", {})


class _IntSubclass(int):
    """A rejected integer subclass for the closed diagnostic grammar."""


class _FloatSubclass(float):
    """A rejected float subclass whose coercion hook must never be called."""

    def __float__(self) -> float:
        raise AssertionError("diagnostic admission must not coerce float subclasses")


def _first_crack_payload(**overrides: object) -> dict[str, object]:
    """Build the smallest valid MCP first-crack status payload."""
    return {
        "mode": "audio",
        "status": "pending",
        "detected_at_utc": None,
        "detected_monotonic_seconds": None,
        "allow_manual_override": True,
        **overrides,
    }


def _session_payload(**first_crack_overrides: object) -> dict[str, object]:
    """Load the committed mock-MCP state and change only first-crack diagnostics."""
    fixture = Path(__file__).parent / "fixtures" / "mcp-tool-results" / "get_roast_state.json"
    payload = cast("dict[str, object]", json.loads(fixture.read_text()))
    first_crack = cast("dict[str, object]", payload["first_crack_status"])
    payload["first_crack_status"] = {**first_crack, **first_crack_overrides}
    return payload


def test_nonzero_inference_diagnostics_validate_and_project_exactly() -> None:
    """The four MCP-produced values survive the normal mirror and projection."""
    status = FirstCrackStatus.model_validate(_first_crack_payload(**_DIAGNOSTIC_VALUES))
    mic = project_mic_status(status)

    for name in _COUNT_NAMES:
        assert type(getattr(status, name)) is int
        assert getattr(status, name) == _DIAGNOSTIC_VALUES[name]
        assert getattr(mic, name) == _DIAGNOSTIC_VALUES[name]
    for name in _DURATION_NAMES:
        assert type(getattr(status, name)) is float
        assert getattr(status, name) == _DIAGNOSTIC_VALUES[name]
        assert getattr(mic, name) == _DIAGNOSTIC_VALUES[name]


def test_explicit_null_diagnostics_remain_null_in_mirror_and_projection() -> None:
    """An explicit MCP null is unproven, not a fabricated passing zero."""
    nulls = {name: None for name in _DIAGNOSTIC_VALUES}
    status = FirstCrackStatus.model_validate(_first_crack_payload(**nulls))
    mic = project_mic_status(status)

    for name in _DIAGNOSTIC_VALUES:
        assert getattr(status, name) is None
        assert getattr(mic, name) is None


def test_legacy_absence_defaults_only_the_new_diagnostics_to_none() -> None:
    """Legacy payloads remain usable without laundering absent diagnostics."""
    legacy = FirstCrackStatus.model_validate(_first_crack_payload())
    assert all(getattr(legacy, name) is None for name in _DIAGNOSTIC_VALUES)
    assert legacy.queued_window_count == 0
    assert legacy.overflow_count_last_minute == 0
    assert legacy.estimated_lost_audio_ms_last_minute == 0.0
    assert legacy.total_overflow_count == 0


def test_inference_diagnostics_do_not_change_mic_health() -> None:
    """Diagnostics remain read-only and cannot participate in mic-health derivation."""
    baseline = project_mic_status(
        FirstCrackStatus.model_validate(_first_crack_payload(status="pending", audio_running=True))
    )
    observed = project_mic_status(
        FirstCrackStatus.model_validate(
            _first_crack_payload(status="pending", audio_running=True, **_DIAGNOSTIC_VALUES)
        )
    )
    assert observed.mic_health is baseline.mic_health


_INVALID_CASES: list[tuple[str, tuple[object, ...]]] = [
    (
        "max_consecutive_overflow_count",
        (True, False, _IntSubclass(1), -1, 1.0, "0", _INVALID_LIST, _INVALID_DICT, 2**53),
    ),
    (
        "inference_overrun_count",
        (True, False, _IntSubclass(1), -1, 1.0, "0", _INVALID_LIST, _INVALID_DICT, 2**53),
    ),
    (
        "last_inference_duration_ms",
        (
            True,
            False,
            _IntSubclass(1),
            _FloatSubclass(1.5),
            -1,
            -0.5,
            float("nan"),
            float("inf"),
            float("-inf"),
            "1.5",
            _INVALID_LIST,
            _INVALID_DICT,
            10**400,
            2**53 + 1,
        ),
    ),
    (
        "max_inference_duration_ms",
        (
            True,
            False,
            _IntSubclass(1),
            _FloatSubclass(1.5),
            -1,
            -0.5,
            float("nan"),
            float("inf"),
            float("-inf"),
            "1.5",
            _INVALID_LIST,
            _INVALID_DICT,
            10**400,
            2**53 + 1,
        ),
    ),
]


@pytest.mark.parametrize(("name", "invalid_values"), _INVALID_CASES)
def test_invalid_diagnostics_fail_closed_without_breaking_telemetry(
    name: str, invalid_values: tuple[object, ...]
) -> None:
    """Every unadmitted grammar member degrades only its diagnostic to null."""
    baseline = _session_payload()
    first_crack = dict(cast("dict[str, object]", baseline["first_crack_status"]))
    del first_crack[name]
    baseline["first_crack_status"] = first_crack
    expected = project_session_state(RoastSessionState.model_validate(baseline), age_seconds=0.0)
    assert expected is not None

    for value in invalid_values:
        state = RoastSessionState.model_validate(_session_payload(**{name: value}))
        telemetry = project_session_state(state, age_seconds=0.0)
        assert getattr(state.first_crack_status, name) is None
        assert telemetry == expected


def test_diagnostic_admission_boundaries_are_exact() -> None:
    """Closed grammar admits only its documented count and duration boundaries."""
    count = FirstCrackStatus.model_validate(
        _first_crack_payload(max_consecutive_overflow_count=2**53 - 1, inference_overrun_count=0)
    )
    assert count.max_consecutive_overflow_count == 2**53 - 1
    assert count.inference_overrun_count == 0

    duration = FirstCrackStatus.model_validate(
        _first_crack_payload(
            last_inference_duration_ms=2**53,
            max_inference_duration_ms=2.0**60,
        )
    )
    assert type(duration.last_inference_duration_ms) is float
    assert duration.last_inference_duration_ms == float(2**53)
    assert duration.max_inference_duration_ms == 2.0**60
    for value in (0.0, -0.0, 1e300):
        admitted = FirstCrackStatus.model_validate(
            _first_crack_payload(last_inference_duration_ms=value, max_inference_duration_ms=value)
        )
        assert admitted.last_inference_duration_ms == value
        assert admitted.max_inference_duration_ms == value
        assert math.isfinite(value)


def test_text_path_nonfinite_diagnostics_are_typed_nulls() -> None:
    """Text JSON's NaN and Infinity tokens cannot escape the typed mirror."""
    payload = _session_payload(
        max_consecutive_overflow_count=True,
        last_inference_duration_ms=float("nan"),
        max_inference_duration_ms=float("inf"),
    )
    result = SimpleNamespace(content=[SimpleNamespace(text=json.dumps(payload))], isError=False)
    state = RoastSessionState.model_validate(parse_tool_result(result))
    telemetry = project_session_state(state, age_seconds=0.0)

    assert telemetry is not None
    assert state.first_crack_status.max_consecutive_overflow_count is None
    assert state.first_crack_status.last_inference_duration_ms is None
    assert state.first_crack_status.max_inference_duration_ms is None
    assert telemetry.mic_status is not None
    assert telemetry.mic_status.max_consecutive_overflow_count is None
    assert telemetry.mic_status.last_inference_duration_ms is None
    assert telemetry.mic_status.max_inference_duration_ms is None
