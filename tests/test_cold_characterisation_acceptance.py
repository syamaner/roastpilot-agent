"""Behavioural coverage for fail-closed cold acceptance evaluators."""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from roastpilot_agent.cold_characterisation.acceptance import (
    EFFECTIVE_HOP_SECONDS,
    MAX_CONSECUTIVE_OVERFLOW_N,
    PEAK_TRAILING_LOST_AUDIO_MS_X,
    ColdCheckFailure,
    ColdCheckOutcome,
    ColdCheckResult,
    ColdPhaseKind,
    ColdTickAudioSample,
    ColdTickProjectionError,
    derive_d191_metrics,
    evaluate_inference_active,
    evaluate_inference_duration,
    evaluate_recording_artifacts,
    project_cold_tick_audio_sample,
)
from roastpilot_agent.cold_characterisation.mcp import (
    FinalisationFirstCrackStatus,
    FirstCrackRuntimeFinalisationEvidence,
    RecordingArtifact,
    RecordingFinalisationEvidence,
)


def _raw_tick(**changes: object) -> dict[str, object]:
    """Build a complete raw first-crack tick with explicit clean measurements."""
    result: dict[str, object] = {
        "mode": "audio",
        "status": "pending",
        "detected_at_utc": None,
        "detected_monotonic_seconds": None,
        "reason": None,
        "audio_running": True,
        "queued_window_count": 0,
        "emitted_window_count": 1,
        "dropped_window_count": 0,
        "processed_window_count": 1,
        "estimated_lost_audio_ms_last_minute": 0.0,
        "total_overflow_count": 0,
        "max_consecutive_overflow_count": 0,
        "last_inference_duration_ms": 12.0,
        "max_inference_duration_ms": 12.0,
        "inference_overrun_count": 0,
    }
    result.update(changes)
    return result


def _status(**changes: object) -> FinalisationFirstCrackStatus:
    """Build a strict finalisation snapshot from the complete raw tick shape."""
    values = _raw_tick(**changes)
    values.update(
        {
            "allow_manual_override": False,
            "mic_peak_dbfs": None,
            "mic_rms_dbfs": None,
            "overflow_count_last_minute": 0,
        }
    )
    return FinalisationFirstCrackStatus.model_validate(values)


def _runtime(
    *, status_changes: dict[str, object] | None = None
) -> FirstCrackRuntimeFinalisationEvidence:
    """Build the strict runtime envelope required by the G16 evaluator."""
    return FirstCrackRuntimeFinalisationEvidence(
        outcome="stopped",
        stop_error=None,
        capture_running_after_stop=False,
        final_status=_status(**(status_changes or {})),
    )


def _sample(**changes: object):
    """Project a complete raw tick using the sole admitted public path."""
    return project_cold_tick_audio_sample(_raw_tick(**changes))


def _recording(
    *,
    expected: bool = True,
    outcome: str = "finalised",
    reason: str | None = None,
    artifacts: tuple[RecordingArtifact, ...] | None = None,
) -> RecordingFinalisationEvidence:
    """Build strict recording evidence with the three required valid artefacts."""
    return RecordingFinalisationEvidence(
        expected=expected,
        outcome=outcome,  # type: ignore[arg-type]
        reason=reason,
        artifacts=artifacts
        if artifacts is not None
        else (
            RecordingArtifact(
                role="primary_wav",
                filename="primary.wav",
                path="/private/primary.wav",
                exists=True,
                size_bytes=1,
            ),
            RecordingArtifact(
                role="recording_sidecar",
                filename="recording.json",
                path="/private/recording.json",
                exists=True,
                size_bytes=1,
            ),
            RecordingArtifact(
                role="annotation_session_sidecar",
                filename="annotation.json",
                path="/private/annotation.json",
                exists=True,
                size_bytes=1,
            ),
        ),
    )


def test_g16_is_phase_symmetric_and_accepts_clean_active_inference() -> None:
    """G16 has no recording phase parameter and accepts clean active inference."""
    assert "phase" not in inspect.signature(evaluate_inference_active).parameters
    result = evaluate_inference_active(_runtime(), (_sample(),))
    assert result.outcome is ColdCheckOutcome.PASS
    assert result.failure is None


@pytest.mark.parametrize(
    ("changes", "failure"),
    [
        ({"mode": "disabled"}, ColdCheckFailure.INFERENCE_NOT_ACTIVE),
        ({"mode": "manual"}, ColdCheckFailure.INFERENCE_NOT_ACTIVE),
        ({"status": "disabled"}, ColdCheckFailure.INFERENCE_NOT_ACTIVE),
        ({"status": "manual"}, ColdCheckFailure.INFERENCE_NOT_ACTIVE),
        ({"status": "unavailable"}, ColdCheckFailure.INFERENCE_NOT_ACTIVE),
        ({"status": "faulted"}, ColdCheckFailure.MICROPHONE_OR_FATAL_ERROR),
    ],
)
def test_g16_refuses_inactive_or_faulted_runtime(
    changes: dict[str, object], failure: ColdCheckFailure
) -> None:
    """Disabled, manual, unavailable, and faulted runtimes never look clean."""
    assert (
        evaluate_inference_active(_runtime(status_changes=changes), (_sample(),)).failure is failure
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "detected"},
        {"detected_at_utc": "2026-09-22T00:00:00Z"},
        {"detected_monotonic_seconds": 1.0},
    ],
)
def test_g16_refuses_detected_or_confirmed_first_crack(changes: dict[str, object]) -> None:
    """Any terminal detection marker fails a cold phase."""
    assert (
        evaluate_inference_active(_runtime(status_changes=changes), (_sample(),)).failure
        is ColdCheckFailure.FIRST_CRACK_CONFIRMED
    )


def test_g16_refuses_runtime_reason() -> None:
    """An active runtime carrying a reason is a microphone or fatal error."""
    assert (
        evaluate_inference_active(
            _runtime(status_changes={"reason": "microphone fault"}), (_sample(),)
        ).failure
        is ColdCheckFailure.MICROPHONE_OR_FATAL_ERROR
    )


@pytest.mark.parametrize(
    ("status_changes", "samples", "failure"),
    [
        ({"dropped_window_count": 1}, (_sample(),), ColdCheckFailure.DROPPED_WINDOW),
        ({"inference_overrun_count": 1}, (_sample(),), ColdCheckFailure.INFERENCE_OVERRUN),
        ({"queued_window_count": 1}, (_sample(),), ColdCheckFailure.QUEUE_NOT_DRAINED),
        (
            {},
            (
                _sample(queued_window_count=0),
                _sample(queued_window_count=1),
                _sample(queued_window_count=2),
            ),
            ColdCheckFailure.QUEUE_GROWING,
        ),
        (
            {},
            (_sample(emitted_window_count=2), _sample(emitted_window_count=1)),
            ColdCheckFailure.CAPTURE_RESTART,
        ),
        (
            {},
            (_sample(processed_window_count=2), _sample(processed_window_count=1)),
            ColdCheckFailure.CAPTURE_RESTART,
        ),
        (
            {},
            (_sample(total_overflow_count=2), _sample(total_overflow_count=1)),
            ColdCheckFailure.CAPTURE_RESTART,
        ),
        (
            {},
            (_sample(max_inference_duration_ms=2.0), _sample(max_inference_duration_ms=1.0)),
            ColdCheckFailure.CAPTURE_RESTART,
        ),
        (
            {},
            (_sample(max_consecutive_overflow_count=2), _sample(max_consecutive_overflow_count=1)),
            ColdCheckFailure.CAPTURE_RESTART,
        ),
    ],
)
def test_g16_refuses_counter_and_series_failures(
    status_changes: dict[str, object], samples: tuple[Any, ...], failure: ColdCheckFailure
) -> None:
    """Every counter and restart limb fails closed."""
    assert (
        evaluate_inference_active(_runtime(status_changes=status_changes), samples).failure
        is failure
    )


def test_g16_refuses_empty_tick_series() -> None:
    """A missing series cannot synthesize a clean G16 result."""
    with pytest.raises(ColdTickProjectionError):
        evaluate_inference_active(_runtime(), ())


def test_committed_disabled_runtime_fixture_is_not_a_passing_phase() -> None:
    """The bare-capture fixture's disabled detector remains a fail-closed premise."""
    raw = json.loads(
        Path("tests/fixtures/mcp-tool-results/get_runtime_config.json").read_text(encoding="utf-8")
    )
    assert raw["first_crack_mode"] == "disabled"
    assert (
        evaluate_inference_active(
            _runtime(status_changes={"mode": raw["first_crack_mode"]}), (_sample(),)
        ).failure
        is ColdCheckFailure.INFERENCE_NOT_ACTIVE
    )


@pytest.mark.parametrize(
    "missing",
    [
        "max_consecutive_overflow_count",
        "last_inference_duration_ms",
        "max_inference_duration_ms",
        "inference_overrun_count",
    ],
)
def test_tick_projection_refuses_missing_d183_measurements(missing: str) -> None:
    """Missing D183 counters raise instead of becoming clean zero values."""
    raw = _raw_tick()
    del raw[missing]
    with pytest.raises(ColdTickProjectionError):
        project_cold_tick_audio_sample(raw)


@pytest.mark.parametrize(
    "raw",
    [
        {**_raw_tick(), "unknown": 1},
        {**_raw_tick(), "queued_window_count": "0"},
        {**_raw_tick(), "queued_window_count": 0.0},
        {**_raw_tick(), "mode": "audioish"},
        {**_raw_tick(), "status": "ready"},
    ],
)
def test_tick_projection_is_strict_complete_and_closed(raw: dict[str, object]) -> None:
    """Unknown fields, scalar coercion, and out-of-set states cannot project."""
    with pytest.raises(ColdTickProjectionError):
        project_cold_tick_audio_sample(raw)


def test_tick_evidence_cannot_be_hand_built() -> None:
    """Acceptance functions receive only samples created through raw projection."""
    with pytest.raises(ValidationError):
        ColdTickAudioSample.model_validate(_raw_tick())


def test_d191_uses_series_peak_and_finalisation_lifetime_maximum() -> None:
    """X uses every tick plus finalisation while N uses finalisation only."""
    metrics = derive_d191_metrics(
        (
            _sample(estimated_lost_audio_ms_last_minute=250.0, max_consecutive_overflow_count=99),
            _sample(estimated_lost_audio_ms_last_minute=5.0),
        ),
        _status(estimated_lost_audio_ms_last_minute=300.0, max_consecutive_overflow_count=1),
    )
    assert metrics.peak_trailing_lost_audio_ms == 300.0
    assert metrics.max_consecutive_overflow_count == 1
    assert MAX_CONSECUTIVE_OVERFLOW_N == 1
    assert PEAK_TRAILING_LOST_AUDIO_MS_X == 200.0


def test_d191_earliest_tick_peak_is_retained() -> None:
    """A final snapshot cannot hide the phase's earlier trailing-window peak."""
    metrics = derive_d191_metrics(
        (
            _sample(estimated_lost_audio_ms_last_minute=200.0),
            _sample(estimated_lost_audio_ms_last_minute=1.0),
        ),
        _status(estimated_lost_audio_ms_last_minute=2.0),
    )
    assert metrics.peak_trailing_lost_audio_ms == 200.0


def test_d191_refuses_empty_tick_evidence() -> None:
    """No empty-series path returns zero-valued metrics."""
    with pytest.raises(ColdTickProjectionError):
        derive_d191_metrics((), _status())


@pytest.mark.parametrize(
    ("duration_ms", "outcome"),
    [
        (6999.0, ColdCheckOutcome.PASS),
        (7000.0, ColdCheckOutcome.FAIL),
        (7000.1, ColdCheckOutcome.FAIL),
    ],
)
def test_g17_requires_duration_strictly_below_hop(
    duration_ms: float, outcome: ColdCheckOutcome
) -> None:
    """The effective-hop boundary is strict rather than inclusive."""
    result = evaluate_inference_duration(_status(max_inference_duration_ms=duration_ms))
    assert result.outcome is outcome
    assert EFFECTIVE_HOP_SECONDS == 7.0


@pytest.mark.parametrize(
    "evidence",
    [
        _recording(artifacts=()),
        _recording(artifacts=_recording().artifacts + (_recording().artifacts[0],)),
        _recording(
            artifacts=_recording().artifacts
            + (
                RecordingArtifact(
                    role="additional_wav",
                    filename="extra.wav",
                    path="/private/extra.wav",
                    exists=True,
                    size_bytes=1,
                ),
            )
        ),
    ],
)
def test_g18_recording_on_requires_exact_artefact_multiset(
    evidence: RecordingFinalisationEvidence,
) -> None:
    """Recording-on accepts exactly the one-primary and two-sidecar artefact roles."""
    assert (
        evaluate_recording_artifacts(ColdPhaseKind.RECORDING_ON, evidence).failure
        is ColdCheckFailure.RECORDING_ARTEFACT_SET_UNEXPECTED
    )


@pytest.mark.parametrize(
    "evidence",
    [
        _recording(outcome="not_started"),
        _recording(outcome="failed"),
        _recording(reason="failed"),
        _recording(
            artifacts=(
                RecordingArtifact(
                    role="primary_wav",
                    filename="primary.wav",
                    path="/private/primary.wav",
                    exists=False,
                    size_bytes=1,
                ),
            )
            + _recording().artifacts[1:]
        ),
        _recording(
            artifacts=(
                RecordingArtifact(
                    role="primary_wav",
                    filename="primary.wav",
                    path="/private/primary.wav",
                    exists=True,
                    size_bytes=None,
                ),
            )
            + _recording().artifacts[1:]
        ),
        _recording(
            artifacts=(
                RecordingArtifact(
                    role="primary_wav",
                    filename="primary.wav",
                    path="/private/primary.wav",
                    exists=True,
                    size_bytes=0,
                ),
            )
            + _recording().artifacts[1:]
        ),
    ],
)
def test_g18_recording_on_refuses_bad_finalisation_or_empty_artifact(
    evidence: RecordingFinalisationEvidence,
) -> None:
    """Incomplete recording output never passes the recording-on phase."""
    assert (
        evaluate_recording_artifacts(ColdPhaseKind.RECORDING_ON, evidence).outcome
        is ColdCheckOutcome.FAIL
    )


def test_g18_recording_off_is_recording_only_not_an_inference_relaxation() -> None:
    """A valid recording-off result does not make disabled inference acceptable."""
    evidence = _recording(expected=False, outcome="not_configured", artifacts=())
    assert (
        evaluate_recording_artifacts(ColdPhaseKind.RECORDING_OFF, evidence).outcome
        is ColdCheckOutcome.PASS
    )
    assert (
        evaluate_inference_active(
            _runtime(status_changes={"mode": "disabled"}), (_sample(),)
        ).failure
        is ColdCheckFailure.INFERENCE_NOT_ACTIVE
    )


def test_g18_recording_off_refuses_any_recording_artifact_or_wrong_state() -> None:
    """Recording-off requires no configured recorder and no artefacts."""
    assert (
        evaluate_recording_artifacts(ColdPhaseKind.RECORDING_OFF, _recording(expected=True)).failure
        is ColdCheckFailure.RECORDING_UNEXPECTEDLY_CONFIGURED
    )
    assert (
        evaluate_recording_artifacts(
            ColdPhaseKind.RECORDING_OFF,
            _recording(
                expected=False, outcome="not_configured", artifacts=(_recording().artifacts[0],)
            ),
        ).failure
        is ColdCheckFailure.RECORDING_UNEXPECTEDLY_CONFIGURED
    )


def test_check_result_requires_reason_exactly_for_failures() -> None:
    """Result cross-validation makes incoherent outcome/reason pairs unconstructible."""
    assert ColdCheckResult(outcome=ColdCheckOutcome.PASS, failure=None).failure is None
    with pytest.raises(ValidationError):
        ColdCheckResult(outcome=ColdCheckOutcome.FAIL, failure=None)
    with pytest.raises(ValidationError):
        ColdCheckResult(outcome=ColdCheckOutcome.PASS, failure=ColdCheckFailure.DROPPED_WINDOW)
