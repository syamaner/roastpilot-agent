"""Fail-closed acceptance evaluators for cold characterisation evidence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import Enum
from typing import Final, Literal, cast

from pydantic import ConfigDict, ValidationError, ValidationInfo, model_validator

from roastpilot_agent.cold_characterisation.mcp import (
    FinalisationFirstCrackStatus,
    FirstCrackRuntimeFinalisationEvidence,
    RecordingFinalisationEvidence,
    StrictMCPMirror,
)
from roastpilot_agent.config import FINITE_NUMERIC_MODEL_CONFIG

EFFECTIVE_HOP_SECONDS: Final = 7.0
MAX_CONSECUTIVE_OVERFLOW_N: Final = 1
PEAK_TRAILING_LOST_AUDIO_MS_X: Final = 200.0
PRODUCTION_FATAL_STREAK: Final = 30

_COLD_FINITE_NUMERIC_MODEL_CONFIG: Final[ConfigDict] = cast(
    ConfigDict, {**FINITE_NUMERIC_MODEL_CONFIG, "frozen": True}
)
_PROJECTION_CONTEXT_KEY: Final = "cold_tick_projection"


class ColdPhaseKind(Enum):
    """The recording state of one cold-characterisation phase."""

    RECORDING_OFF = "recording_off"
    RECORDING_ON = "recording_on"


class ColdCheckOutcome(Enum):
    """Closed result outcome for one cold-characterisation check."""

    PASS = "pass"
    FAIL = "fail"


class ColdCheckFailure(Enum):
    """Closed failure grammar for cold-characterisation acceptance checks."""

    INFERENCE_NOT_ACTIVE = "inference_not_active"
    MICROPHONE_OR_FATAL_ERROR = "microphone_or_fatal_error"
    FIRST_CRACK_CONFIRMED = "first_crack_confirmed"
    DROPPED_WINDOW = "dropped_window"
    INFERENCE_OVERRUN = "inference_overrun"
    QUEUE_NOT_DRAINED = "queue_not_drained"
    QUEUE_GROWING = "queue_growing"
    CAPTURE_RESTART = "capture_restart"
    INFERENCE_DURATION_AT_OR_ABOVE_HOP = "inference_duration_at_or_above_hop"
    RECORDING_NOT_FINALISED = "recording_not_finalised"
    RECORDING_ARTEFACT_SET_UNEXPECTED = "recording_artefact_set_unexpected"
    RECORDING_ARTEFACT_EMPTY = "recording_artefact_empty"
    RECORDING_UNEXPECTEDLY_CONFIGURED = "recording_unexpectedly_configured"


class ColdCheckResult(StrictMCPMirror):
    """An immutable, internally consistent cold acceptance result."""

    outcome: ColdCheckOutcome
    failure: ColdCheckFailure | None

    @model_validator(mode="after")
    def _require_reason_exactly_for_failures(self) -> ColdCheckResult:
        """Reject reasonless failures and reasons attached to successful checks."""
        has_failure = self.failure is not None
        if (self.outcome is ColdCheckOutcome.PASS) == has_failure:
            raise ValueError("failure must be present exactly when outcome is FAIL")
        return self


class ColdTickProjectionError(ValueError):
    """Raised when an untrusted raw tick cannot become strict evidence."""


class ColdTickAudioSample(StrictMCPMirror):
    """Strict, complete projection of one raw first-crack-status tick."""

    mode: Literal["disabled", "audio", "manual"]
    status: Literal["disabled", "manual", "pending", "detected", "faulted", "unavailable"]
    detected_at_utc: str | None
    detected_monotonic_seconds: float | None
    reason: str | None
    audio_running: bool
    queued_window_count: int
    emitted_window_count: int
    dropped_window_count: int
    processed_window_count: int
    estimated_lost_audio_ms_last_minute: float
    total_overflow_count: int
    max_consecutive_overflow_count: int
    last_inference_duration_ms: float
    max_inference_duration_ms: float
    inference_overrun_count: int

    @model_validator(mode="after")
    def _require_raw_projection(self, info: ValidationInfo) -> ColdTickAudioSample:
        """Reject direct construction so raw payloads use the sole admitted path."""
        if info.context != {_PROJECTION_CONTEXT_KEY: True}:
            raise ValueError("cold tick samples must be projected from raw payloads")
        return self


class ColdD191Metrics(StrictMCPMirror):
    """The D191 quantities derived without judging them against locked limits."""

    model_config = _COLD_FINITE_NUMERIC_MODEL_CONFIG

    max_consecutive_overflow_count: int
    peak_trailing_lost_audio_ms: float


def project_cold_tick_audio_sample(raw_payload: Mapping[str, object]) -> ColdTickAudioSample:
    """Strictly project one raw MCP tick into the sole accepted evidence shape.

    Args:
        raw_payload: The unmodified ``first_crack_status`` object supplied by MCP.

    Returns:
        The complete strict tick evidence.

    Raises:
        ColdTickProjectionError: If the payload is absent, unknown, incomplete, or coerced.
    """
    try:
        return ColdTickAudioSample.model_validate(
            raw_payload, context={_PROJECTION_CONTEXT_KEY: True}
        )
    except ValidationError as error:
        raise ColdTickProjectionError("Cold tick evidence is malformed.") from error


def derive_d191_metrics(
    tick_samples: Sequence[ColdTickAudioSample],
    finalisation_status: FinalisationFirstCrackStatus,
) -> ColdD191Metrics:
    """Derive D191 quantities from their fixed, non-interchangeable sources.

    Args:
        tick_samples: Strict raw-tick projections for the complete phase.
        finalisation_status: The strict finalisation snapshot from MCP.

    Returns:
        The finalisation-sourced N and series-plus-finalisation-sourced X values.

    Raises:
        ColdTickProjectionError: If no strict per-tick evidence was supplied.
    """
    _require_tick_samples(tick_samples)
    final_sample = _strict_finalisation_projection(finalisation_status)
    return ColdD191Metrics(
        max_consecutive_overflow_count=final_sample.max_consecutive_overflow_count,
        peak_trailing_lost_audio_ms=max(
            *(sample.estimated_lost_audio_ms_last_minute for sample in tick_samples),
            final_sample.estimated_lost_audio_ms_last_minute,
        ),
    )


def evaluate_inference_active(
    runtime_evidence: FirstCrackRuntimeFinalisationEvidence,
    tick_samples: Sequence[ColdTickAudioSample],
) -> ColdCheckResult:
    """Evaluate phase-symmetric active-inference and capture-health evidence.

    Args:
        runtime_evidence: Strict finalisation evidence for first-crack inference.
        tick_samples: Strict raw-tick projections for the complete phase.

    Returns:
        A pass or one closed fail-closed reason.

    Raises:
        ColdTickProjectionError: If the series needed for counter checks is absent.
    """
    _require_tick_samples(tick_samples)
    status = _strict_finalisation_projection(runtime_evidence.final_status)
    if status.mode != "audio" or status.status in frozenset({"disabled", "manual", "unavailable"}):
        return _fail(ColdCheckFailure.INFERENCE_NOT_ACTIVE)
    if status.status == "faulted" or status.reason is not None:
        return _fail(ColdCheckFailure.MICROPHONE_OR_FATAL_ERROR)
    if (
        status.status == "detected"
        or status.detected_at_utc is not None
        or status.detected_monotonic_seconds is not None
    ):
        return _fail(ColdCheckFailure.FIRST_CRACK_CONFIRMED)
    if status.status != "pending":
        return _fail(ColdCheckFailure.INFERENCE_NOT_ACTIVE)
    if status.dropped_window_count != 0:
        return _fail(ColdCheckFailure.DROPPED_WINDOW)
    if status.inference_overrun_count != 0:
        return _fail(ColdCheckFailure.INFERENCE_OVERRUN)
    if status.queued_window_count != 0:
        return _fail(ColdCheckFailure.QUEUE_NOT_DRAINED)
    if _has_capture_restart(tick_samples):
        return _fail(ColdCheckFailure.CAPTURE_RESTART)
    if _has_sustained_queue_growth(tick_samples):
        return _fail(ColdCheckFailure.QUEUE_GROWING)
    return _pass()


def evaluate_inference_duration(
    finalisation_status: FinalisationFirstCrackStatus,
) -> ColdCheckResult:
    """Check the strict inference-duration bound from finalisation evidence.

    Args:
        finalisation_status: The strict finalisation snapshot from MCP.

    Returns:
        A pass only when the maximum duration is strictly below the effective hop.
    """
    status = _strict_finalisation_projection(finalisation_status)
    if status.max_inference_duration_ms / 1000.0 < EFFECTIVE_HOP_SECONDS:
        return _pass()
    return _fail(ColdCheckFailure.INFERENCE_DURATION_AT_OR_ABOVE_HOP)


def evaluate_recording_artifacts(
    phase: ColdPhaseKind, recording_evidence: RecordingFinalisationEvidence
) -> ColdCheckResult:
    """Evaluate the recording-only phase distinction and sealed artefact multiset.

    Args:
        phase: Whether this phase had primary WAV recording enabled.
        recording_evidence: Strict recording finalisation evidence from MCP.

    Returns:
        A pass or one closed recording failure reason.
    """
    if phase is ColdPhaseKind.RECORDING_OFF:
        if (
            recording_evidence.expected is False
            and recording_evidence.outcome == "not_configured"
            and not recording_evidence.artifacts
        ):
            return _pass()
        return _fail(ColdCheckFailure.RECORDING_UNEXPECTEDLY_CONFIGURED)

    if (
        recording_evidence.expected is not True
        or recording_evidence.outcome != "finalised"
        or recording_evidence.reason is not None
    ):
        return _fail(ColdCheckFailure.RECORDING_NOT_FINALISED)
    expected_roles = (
        "primary_wav",
        "recording_sidecar",
        "annotation_session_sidecar",
    )
    roles = tuple(artifact.role for artifact in recording_evidence.artifacts)
    if tuple(sorted(roles)) != tuple(sorted(expected_roles)):
        return _fail(ColdCheckFailure.RECORDING_ARTEFACT_SET_UNEXPECTED)
    if any(
        artifact.exists is not True or artifact.size_bytes is None or artifact.size_bytes <= 0
        for artifact in recording_evidence.artifacts
    ):
        return _fail(ColdCheckFailure.RECORDING_ARTEFACT_EMPTY)
    return _pass()


def _strict_finalisation_projection(
    finalisation_status: FinalisationFirstCrackStatus,
) -> ColdTickAudioSample:
    """Route finalisation evidence through the same strict field set as ticks."""
    raw = cast(Mapping[str, object], finalisation_status.model_dump(mode="json"))
    projected_fields = {
        field_name: raw[field_name] for field_name in ColdTickAudioSample.model_fields
    }
    return project_cold_tick_audio_sample(projected_fields)


def _require_tick_samples(tick_samples: Sequence[ColdTickAudioSample]) -> None:
    """Refuse a missing series before any evaluator can synthesize a clean value."""
    if not tick_samples:
        raise ColdTickProjectionError("Cold tick evidence is missing.")


def _has_capture_restart(tick_samples: Sequence[ColdTickAudioSample]) -> bool:
    """Return whether a monotonic capture-lifetime counter ever decreases."""
    fields = (
        "total_overflow_count",
        "emitted_window_count",
        "processed_window_count",
        "max_inference_duration_ms",
        "max_consecutive_overflow_count",
    )
    previous = tick_samples[0]
    for sample in tick_samples[1:]:
        if any(getattr(sample, field) < getattr(previous, field) for field in fields):
            return True
        previous = sample
    return False


def _has_sustained_queue_growth(tick_samples: Sequence[ColdTickAudioSample]) -> bool:
    """Return whether new queue high-water marks occur in two adjacent samples."""
    high_water = tick_samples[0].queued_window_count
    consecutive_growth = 0
    for sample in tick_samples[1:]:
        if sample.queued_window_count > high_water:
            consecutive_growth += 1
            high_water = sample.queued_window_count
            if consecutive_growth > 1:
                return True
        else:
            consecutive_growth = 0
            high_water = max(high_water, sample.queued_window_count)
    return False


def _pass() -> ColdCheckResult:
    """Build a valid successful check result."""
    return ColdCheckResult(outcome=ColdCheckOutcome.PASS, failure=None)


def _fail(failure: ColdCheckFailure) -> ColdCheckResult:
    """Build a valid failed check result with its closed reason."""
    return ColdCheckResult(outcome=ColdCheckOutcome.FAIL, failure=failure)
