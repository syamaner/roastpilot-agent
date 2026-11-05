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
from roastpilot_agent.models import SseEvent, SseEventType
from roastpilot_agent.replay import ReplayMarker, create_replay_app

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


def test_legacy_absence_changes_only_the_new_diagnostics_across_state_and_telemetry() -> None:
    """T5: legacy absence preserves every pre-existing state and telemetry value."""
    normal = RoastSessionState.model_validate(_session_payload(**_DIAGNOSTIC_VALUES))
    legacy_payload = _session_payload()
    legacy_first_crack = cast("dict[str, object]", legacy_payload["first_crack_status"])
    for name in _DIAGNOSTIC_VALUES:
        del legacy_first_crack[name]
    legacy = RoastSessionState.model_validate(legacy_payload)

    normal_state = normal.model_dump(mode="json")
    legacy_state = legacy.model_dump(mode="json")
    normal_diagnostics = normal_state["first_crack_status"]
    legacy_diagnostics = legacy_state["first_crack_status"]
    assert isinstance(normal_diagnostics, dict)
    assert isinstance(legacy_diagnostics, dict)
    for name in _DIAGNOSTIC_VALUES:
        assert legacy_diagnostics[name] is None
        assert normal_diagnostics[name] == _DIAGNOSTIC_VALUES[name]
        del normal_diagnostics[name]
        del legacy_diagnostics[name]
    assert legacy_state == normal_state

    normal_telemetry = project_session_state(normal, age_seconds=0.0)
    legacy_telemetry = project_session_state(legacy, age_seconds=0.0)
    assert normal_telemetry is not None
    assert legacy_telemetry is not None
    assert legacy_telemetry.bean_temp_c == normal_telemetry.bean_temp_c
    assert legacy_telemetry.env_temp_c == normal_telemetry.env_temp_c
    assert legacy_telemetry.bean_ror_c_per_min == normal_telemetry.bean_ror_c_per_min
    assert legacy_telemetry.env_ror_c_per_min == normal_telemetry.env_ror_c_per_min
    assert legacy_telemetry.first_crack_detected is normal_telemetry.first_crack_detected
    assert legacy_telemetry.mic_status is not None
    assert normal_telemetry.mic_status is not None
    for name in (
        "overflow_count_last_minute",
        "estimated_lost_audio_ms_last_minute",
        "total_overflow_count",
    ):
        assert getattr(legacy_telemetry.mic_status, name) == getattr(
            normal_telemetry.mic_status, name
        )
    normal_dump = normal_telemetry.model_dump(mode="json")
    legacy_dump = legacy_telemetry.model_dump(mode="json")
    assert isinstance(normal_dump["mic_status"], dict)
    assert isinstance(legacy_dump["mic_status"], dict)
    for name in _DIAGNOSTIC_VALUES:
        assert legacy_dump["mic_status"][name] is None
        assert normal_dump["mic_status"][name] == _DIAGNOSTIC_VALUES[name]
        del normal_dump["mic_status"][name]
        del legacy_dump["mic_status"][name]
    assert legacy_dump == normal_dump


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

    zero_duration = FirstCrackStatus.model_validate(
        _first_crack_payload(last_inference_duration_ms=0, max_inference_duration_ms=0)
    )
    assert type(zero_duration.last_inference_duration_ms) is float
    assert zero_duration.last_inference_duration_ms == 0.0
    assert type(zero_duration.max_inference_duration_ms) is float
    assert zero_duration.max_inference_duration_ms == 0.0

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


@pytest.mark.asyncio
async def test_replay_telemetry_keeps_inference_diagnostic_defaults_as_null(tmp_path: Path) -> None:
    """T14: the real replay pipeline emits all four synthesized null diagnostics."""
    fixture = Path(__file__).parent / "fixtures" / "replay" / "session-2"
    _app, service, source = await create_replay_app(
        fixture, tmp_path / "replay.sqlite3", step_mode=True, speed=60
    )
    subscriber = service.events.subscribe()
    try:
        await source.advance_to(ReplayMarker.FIRST_CRACK)
        frames: list[SseEvent] = []
        while not subscriber.empty():
            frames.append(subscriber.get_nowait())
        telemetry_frames = [frame for frame in frames if frame.event is SseEventType.TELEMETRY]
        assert telemetry_frames
        for frame in telemetry_frames:
            mic_status = frame.data["mic_status"]
            assert isinstance(mic_status, dict)
            assert {name: mic_status[name] for name in _DIAGNOSTIC_VALUES} == {
                name: None for name in _DIAGNOSTIC_VALUES
            }
    finally:
        service.events.unsubscribe(subscriber)
        await source.aclose()
