"""Strict MCP boundary for non-actuating cold characterisation.

This module deliberately owns a smaller MCP surface than the normal roast
client.  Finalisation evidence is parsed strictly because it is a safety gate,
not forward-compatible roast telemetry.
"""

import copy
import json
from contextlib import suppress
from enum import Enum
from typing import Literal, Protocol, TypeAlias, TypeVar, cast

from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, ValidationError, model_validator

from roastpilot_agent.cold_characterisation.evidence_schema import (
    MAX_VENDOR_BLOB_BYTES,
    ColdAudioField,
    ColdEvidenceError,
    ColdEvidenceFailure,
    ColdJsonValue,
    ColdTickProjection,
    project_tick_audio,
    walk_json_value,
)
from roastpilot_agent.config import MCPDeviceConfig
from roastpilot_agent.mcp_client import (
    EventCommandResult,
    MCPConnectionError,
    MCPPhase,
    RoastSessionState,
    RuntimeConfigSnapshot,
    ServerInfo,
    StartRoastSessionResult,
    ToolCaller,
)

#: The only MCP tools reachable through this non-actuating boundary.
COLD_ALLOWED_TOOLS: frozenset[str] = frozenset(
    {
        "get_server_info",
        "get_runtime_config",
        "start_roast_session",
        "get_roast_state",
        "mark_beans_added",
        "finalise_cold_characterisation_session",
    }
)

_ResultT = TypeVar("_ResultT", bound=BaseModel)


class ColdMcpError(RuntimeError):
    """Base error for a cold-mode MCP contract violation."""


class ColdModeForbiddenToolError(ColdMcpError):
    """Raised when a tool is outside the frozen cold-mode allow-list."""


class ColdSessionPurposeError(ColdMcpError):
    """Raised when MCP does not confirm a cold-characterisation session."""


class ColdSessionIdentityError(ColdMcpError):
    """Raised when MCP returns a result for a different cold session."""


class ColdSessionPhaseError(ColdMcpError):
    """Raised when MCP does not confirm cold inference activation."""


class ColdMcpValidationError(ColdMcpError):
    """Raised when an MCP response violates the cold boundary schema."""


class ColdTickAudioProjectionError(ColdMcpValidationError):
    """Raised when a tick's first-crack evidence fails the strict audio projection.

    The error keeps only closed schema-owned diagnostics.  It never carries a
    rejected value or key name, and its message is fixed.
    """

    failure: ColdEvidenceFailure
    field_names: tuple[ColdAudioField, ...]

    def __init__(
        self, failure: ColdEvidenceFailure, field_names: tuple[ColdAudioField, ...]
    ) -> None:
        """Retain closed projection diagnostics behind a fixed public message.

        Args:
            failure: Closed failure reported by the strict projection.
            field_names: Closed audio field diagnostics reported by the projection.
        """
        super().__init__("MCP tick audio evidence failed strict projection")
        self.failure = failure
        self.field_names = field_names


class ColdRoastFanOutcome(Enum):
    """Closed commanded-roast-fan outcomes returned by a cold tick."""

    OBSERVED = "observed"
    NOT_ELIGIBLE = "not_eligible"
    UNSUPPORTED = "unsupported"
    UNREADABLE = "unreadable"
    MALFORMED = "malformed"


class ColdRoastFanProjectionFailure(Enum):
    """Closed reasons a raw roast-fan observation fails strict projection."""

    OBSERVATION_KEY_MISSING = "observation_key_missing"
    OBSERVATION_NULL = "observation_null"
    OBSERVATION_NOT_OBJECT = "observation_not_object"
    FIELD_SET_MISMATCH = "field_set_mismatch"
    OUTCOME_NOT_ADMITTED = "outcome_not_admitted"
    LEVEL_TYPE_NOT_EXACT = "level_type_not_exact"
    LEVEL_OUT_OF_RANGE = "level_out_of_range"
    LEVEL_OUTCOME_MISMATCH = "level_outcome_mismatch"


class ColdTickRoastFanProjectionError(ColdMcpValidationError):
    """Raised when a tick's roast-fan observation fails strict projection."""

    failure: ColdRoastFanProjectionFailure

    def __init__(self, failure: ColdRoastFanProjectionFailure) -> None:
        """Retain one closed failure behind a fixed public message.

        Args:
            failure: Closed reason the projection refused the observation.
        """
        super().__init__("MCP tick roast-fan observation failed strict projection")
        self.failure = failure


class ColdDeviceField(Enum):
    """Closed names of the eight raw per-tick ``device_state`` fields."""

    DRIVER = "driver"
    CONNECTED = "connected"
    BEAN_TEMP_C = "bean_temp_c"
    ENV_TEMP_C = "env_temp_c"
    HEAT_LEVEL_PERCENT = "heat_level_percent"
    FAN_LEVEL_PERCENT = "fan_level_percent"
    COOLING_ON = "cooling_on"
    RAW_VENDOR_DATA = "raw_vendor_data"


class ColdDeviceProjectionFailure(Enum):
    """Closed reasons a raw per-tick device state fails strict projection."""

    DEVICE_STATE_NOT_OBJECT = "device_state_not_object"
    FIELD_SET_MISMATCH = "field_set_mismatch"
    FIELD_TYPE_NOT_EXACT = "field_type_not_exact"
    DEVICE_VALUE_NOT_ADMITTED = "device_value_not_admitted"
    VENDOR_DATA_TOO_LARGE = "vendor_data_too_large"


class ColdTickDeviceProjectionError(ColdMcpValidationError):
    """Raised when a tick's device state fails the strict device projection.

    The error keeps only closed diagnostics.  It never carries a rejected value
    or key name, and its message is fixed.
    """

    failure: ColdDeviceProjectionFailure
    field: ColdDeviceField | None

    def __init__(self, failure: ColdDeviceProjectionFailure, field: ColdDeviceField | None) -> None:
        """Retain closed projection diagnostics behind a fixed public message.

        Args:
            failure: Closed reason the projection refused the raw device state.
            field: The one closed device field concerned, when one is known.
        """
        super().__init__("MCP tick device state failed strict projection")
        self.failure = failure
        self.field = field


class ColdSessionField(Enum):
    """Closed names of the five raw top-level per-tick session metadata fields."""

    SESSION_ID = "session_id"
    ACTIVE = "active"
    SESSION_PURPOSE = "session_purpose"
    PHASE = "phase"
    ELAPSED_MONOTONIC_SECONDS = "elapsed_monotonic_seconds"


class ColdTickSessionProjectionError(ColdMcpValidationError):
    """Raised when tick session metadata fails the strict session projection.

    The error keeps only one closed field diagnostic.  It never carries a
    rejected value or key name, and its message is fixed.
    """

    field: ColdSessionField | None

    def __init__(self, field: ColdSessionField | None) -> None:
        """Retain one closed field diagnostic behind a fixed public message.

        Args:
            field: The one closed session field concerned, when one is known.
        """
        super().__init__("MCP tick session metadata failed strict projection")
        self.field = field


class ColdMcpTransportError(ColdMcpError):
    """Raised when the injected MCP transport fails in cold mode."""


class ColdFinalisationResultError(ColdMcpError):
    """Base finalisation error retaining parsed evidence for private diagnostics."""

    result: "SessionFinalisationResult"

    def __init__(self, message: str, result: "SessionFinalisationResult") -> None:
        """Retain parsed finalisation evidence without rendering it publicly.

        Args:
            message: Fixed public error message.
            result: Validated finalisation evidence for private diagnostics.
        """
        super().__init__(message)
        self.result = result


class ColdFinalisationNotCleanError(ColdFinalisationResultError):
    """Raised when D195 finalisation is not clean."""


class ColdFinalisationSafetyError(ColdFinalisationResultError):
    """Raised when finalisation lacks safe-zero or disconnect evidence."""


class StrictMCPMirror(BaseModel):
    """Immutable MCP mirror that rejects unknown fields and most scalar coercion.

    Strict mode still converts a JSON integer into a float field, so the cold
    ingresses (tick audio and finalisation first-crack status) additionally
    enforce the exact raw JSON type of each such field.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, allow_inf_nan=False)


class RejectionReason(Enum):
    """Closed D195 finalisation rejection grammar from MCP 0.2.1."""

    UNKNOWN_SESSION = "unknown_session"
    NOT_LATEST_SESSION = "not_latest_session"
    SESSION_NOT_ACTIVE = "session_not_active"
    SESSION_PURPOSE_NOT_ELIGIBLE = "session_purpose_not_eligible"
    SESSION_FAULTED = "session_faulted"
    COMMAND_IN_PROGRESS = "command_in_progress"
    FINALISATION_IN_PROGRESS = "finalisation_in_progress"
    DRIVER_LIFECYCLE_EVIDENCE_UNSUPPORTED = "driver_lifecycle_evidence_unsupported"
    DRIVER_STATE_UNREADABLE = "driver_state_unreadable"
    DRIVER_STATE_MALFORMED = "driver_state_malformed"
    DRIVER_NOT_CONNECTED = "driver_not_connected"
    DRIVER_STATE_NOT_SAFE_ZERO = "driver_state_not_safe_zero"


FinalisationStageName: TypeAlias = Literal[
    "telemetry_sampler", "first_crack_runtime", "recording", "driver_disconnect"
]
FinalisationStageStatus: TypeAlias = Literal[
    "pending", "completed", "incomplete", "failed", "not_applicable", "skipped"
]
ConfirmationState: TypeAlias = Literal["confirmed", "not_applicable", "not_confirmed"]


class FinalisationFailure(StrictMCPMirror):
    """One append-only failure recorded by MCP finalisation."""

    stage: FinalisationStageName | Literal["revalidation"]
    code: str
    message: str
    attempt_number: int
    recorded_at_utc: str


class FinalisationStageResult(StrictMCPMirror):
    """Outcome of one ordered MCP finalisation stage."""

    stage: FinalisationStageName
    status: FinalisationStageStatus
    completed_at_utc: str | None
    completed_in_attempt: int | None
    detail: str | None


class DriverCommandStateEvidence(StrictMCPMirror):
    """Read-only six-dimension driver evidence retained by D195."""

    driver: str
    connected: StrictBool
    command_streaming_required: StrictBool
    command_loop_running: StrictBool | None
    serial_open: StrictBool | None
    heat_level_percent: StrictInt
    roast_fan_level_percent: StrictInt
    main_fan_level_percent: StrictInt
    drum_motor_on: StrictBool
    cooling_motor_on: StrictBool
    solenoid_open: StrictBool
    safe_zero: StrictBool
    non_zero_dimensions: tuple[str, ...]
    command_send_attempts: StrictInt | None
    command_write_count: StrictInt | None
    last_command_write_size: StrictInt | None
    command_loop_error_count: StrictInt | None
    status_packet_count: StrictInt | None
    status_read_error_count: StrictInt | None

    @model_validator(mode="before")
    @classmethod
    def _accept_json_dimension_array(cls, value: object) -> object:
        """Convert the JSON array representation of an immutable dimension tuple."""
        if not isinstance(value, dict):
            return value
        mapping = cast("dict[str, object]", value)
        raw_dimensions = mapping.get("non_zero_dimensions")
        if not isinstance(raw_dimensions, list):
            return mapping
        dimensions = cast("list[object]", raw_dimensions)
        normalised = dict(mapping)
        normalised["non_zero_dimensions"] = tuple(dimensions)
        return normalised


class DriverEvidenceRead(StrictMCPMirror):
    """Outcome of one non-actuating driver lifecycle evidence read."""

    captured_at_utc: str
    outcome: Literal["read", "unsupported", "unreadable", "malformed"]
    error: str | None
    evidence: DriverCommandStateEvidence | None


class SamplerFinalisationEvidence(StrictMCPMirror):
    """Bounded telemetry sampler stop evidence."""

    owned_by_session_before_stop: bool
    thread_alive_after_join: bool
    last_error: str | None


class FinalisationFirstCrackStatus(StrictMCPMirror):
    """Strict first-crack status retained in D195 evidence."""

    mode: Literal["disabled", "audio", "manual"]
    status: Literal["disabled", "manual", "pending", "detected", "faulted", "unavailable"]
    detected_at_utc: str | None
    detected_monotonic_seconds: float | None
    allow_manual_override: bool
    reason: str | None
    audio_running: bool
    queued_window_count: int
    emitted_window_count: int
    dropped_window_count: int
    processed_window_count: int
    mic_peak_dbfs: float | None
    mic_rms_dbfs: float | None
    overflow_count_last_minute: int
    estimated_lost_audio_ms_last_minute: float
    total_overflow_count: int
    max_consecutive_overflow_count: int
    last_inference_duration_ms: float
    max_inference_duration_ms: float
    inference_overrun_count: int


class FirstCrackRuntimeFinalisationEvidence(StrictMCPMirror):
    """First-crack runtime stop evidence."""

    outcome: Literal["not_active", "stopped", "capture_still_running", "stop_failed"]
    stop_error: str | None
    capture_running_after_stop: bool
    final_status: FinalisationFirstCrackStatus


class RecordingArtifact(StrictMCPMirror):
    """Stat-only identity of one recording artefact."""

    role: Literal[
        "primary_wav", "recording_sidecar", "annotation_session_sidecar", "additional_wav"
    ]
    filename: str
    path: str
    exists: bool
    size_bytes: int | None


class RecordingFinalisationEvidence(StrictMCPMirror):
    """Recording teardown and artefact evidence."""

    expected: bool
    outcome: Literal["not_configured", "finalised", "not_started", "failed"]
    reason: str | None
    artifacts: tuple[RecordingArtifact, ...]

    @model_validator(mode="before")
    @classmethod
    def _accept_json_artifact_array(cls, value: object) -> object:
        """Convert the JSON array representation of immutable artefact records."""
        if not isinstance(value, dict):
            return value
        mapping = cast("dict[str, object]", value)
        raw_artifacts = mapping.get("artifacts")
        if not isinstance(raw_artifacts, list):
            return mapping
        artifacts = cast("list[object]", raw_artifacts)
        normalised = dict(mapping)
        normalised["artifacts"] = tuple(artifacts)
        return normalised


class DisconnectEvidence(StrictMCPMirror):
    """Disconnect attempt and confirmation evidence."""

    attempt_count: StrictInt
    first_attempted_at_utc: str | None
    last_attempted_at_utc: str | None
    last_returned_without_error: StrictBool | None
    last_error: str | None
    connected_false_confirmed: StrictBool
    command_loop_stopped: ConfirmationState
    serial_closed: ConfirmationState


class SessionFinalisationResult(StrictMCPMirror):
    """Complete strict mirror of MCP 0.2.1 D195 finalisation output."""

    session_id: str
    session_purpose: Literal["roast", "cold_characterisation"] | None
    status: Literal[
        "rejected", "clean", "completed_not_clean", "partial", "disconnect_indeterminate", "aborted"
    ]
    clean: bool
    rejection_reason: RejectionReason | None
    abort_reason: Literal["emergency_stop", "session_or_reservation_changed"] | None
    retained: bool
    reservation_generation: int | None
    attempt_number: int
    recovered_after_failure: bool
    first_started_at_utc: str | None
    first_started_session_elapsed_seconds: float | None
    last_ended_at_utc: str | None
    last_ended_session_elapsed_seconds: float | None
    emergency_stop_ordering: Literal[
        "not_reached",
        "finalisation_committed_first",
        "emergency_stop_before_disconnect_commit",
        "emergency_stop_after_disconnect_attempt",
    ]
    stages: tuple[
        FinalisationStageResult,
        FinalisationStageResult,
        FinalisationStageResult,
        FinalisationStageResult,
    ]
    failures: tuple[FinalisationFailure, ...]
    admission_driver_evidence: DriverEvidenceRead | None
    pre_disconnect_driver_evidence: DriverEvidenceRead | None
    final_driver_evidence: DriverEvidenceRead | None
    sampler: SamplerFinalisationEvidence | None
    pre_finalisation_first_crack_status: FinalisationFirstCrackStatus | None
    first_crack_runtime: FirstCrackRuntimeFinalisationEvidence | None
    recording: RecordingFinalisationEvidence | None
    disconnect: DisconnectEvidence
    session_active_after: bool
    session_phase_after: (
        Literal["pre_roast", "roasting", "development", "dropped", "cooling", "complete", "fault"]
        | None
    )

    @model_validator(mode="before")
    @classmethod
    def _accept_json_finalisation_containers(cls, value: object) -> object:
        """Accept JSON container and enum representations without scalar coercion."""
        if not isinstance(value, dict):
            return value
        normalised = dict(cast("dict[str, object]", value))
        for field in ("stages", "failures"):
            field_value = normalised.get(field)
            if isinstance(field_value, list):
                normalised[field] = tuple(cast("list[object]", field_value))
        reason = normalised.get("rejection_reason")
        if isinstance(reason, str):
            with suppress(ValueError):
                normalised["rejection_reason"] = RejectionReason(reason)
        return normalised

    @model_validator(mode="after")
    def _require_ordered_stages(self) -> "SessionFinalisationResult":
        expected: tuple[FinalisationStageName, ...] = (
            "telemetry_sampler",
            "first_crack_runtime",
            "recording",
            "driver_disconnect",
        )
        if tuple(stage.stage for stage in self.stages) != expected:
            raise ValueError("finalisation stages must retain MCP's four-stage order")
        return self


class ColdMcpLifecycle(Protocol):
    """Narrow lifecycle view the cold engine may receive from MCP transport."""

    async def start(self) -> None:
        """Start the MCP child."""

    async def stop(self) -> None:
        """Stop the MCP child."""

    def set_device_config(self, device_config: MCPDeviceConfig) -> None:
        """Set the next MCP child configuration."""

    @property
    def stop_unconfirmed(self) -> bool:
        """Whether child teardown could not be confirmed."""
        ...


def finalisation_is_clean(result: SessionFinalisationResult) -> bool:
    """Whether the D195 clean-status conjunction is satisfied.

    Args:
        result: Strictly parsed MCP finalisation result.

    Returns:
        ``True`` only for a clean, fully stopped four-stage finalisation.
    """
    return (
        result.status == "clean"
        and result.clean is True
        and result.rejection_reason is None
        and result.abort_reason is None
        and not result.failures
        and result.session_active_after is False
        and result.emergency_stop_ordering in ("not_reached", "finalisation_committed_first")
        and result.session_phase_after in ("pre_roast", "roasting")
        and result.stages[0].status == "completed"
        and result.sampler is not None
        and result.pre_finalisation_first_crack_status is not None
        and result.first_crack_runtime is not None
        and result.recording is not None
        and (
            (
                result.stages[1].status == "completed"
                and result.first_crack_runtime.outcome == "stopped"
                and result.first_crack_runtime.stop_error is None
                and result.first_crack_runtime.final_status.audio_running is False
            )
            or (
                result.stages[1].status == "not_applicable"
                and result.first_crack_runtime.outcome == "not_active"
                and result.first_crack_runtime.stop_error is None
                and result.first_crack_runtime.final_status.audio_running is False
                and result.pre_finalisation_first_crack_status.audio_running is False
            )
        )
        and (
            (
                result.stages[2].status == "completed"
                and result.recording.outcome == "finalised"
                and result.recording.reason is None
            )
            or (
                result.stages[2].status == "not_applicable"
                and result.recording.outcome == "not_configured"
                and result.recording.expected is False
            )
        )
        and result.stages[3].status == "completed"
        and (result.sampler.thread_alive_after_join is False and result.sampler.last_error is None)
        and result.first_crack_runtime.capture_running_after_stop is False
        and result.first_crack_runtime.outcome in ("not_active", "stopped")
    )


def _trusted_final_driver_evidence(
    result: SessionFinalisationResult,
) -> DriverCommandStateEvidence | None:
    """Return final driver evidence only when its lifecycle read succeeded cleanly."""
    driver_read = result.final_driver_evidence
    if (
        driver_read is None
        or driver_read.outcome != "read"
        or driver_read.error is not None
        or driver_read.evidence is None
    ):
        return None
    return driver_read.evidence


def _finalisation_status_types_are_exact(tree: object, result: SessionFinalisationResult) -> bool:
    """Whether each parsed first-crack status field keeps its exact raw JSON type.

    Strict validation still converts a JSON integer into a float field, so both
    finalisation first-crack status locations are compared field by field
    against the raw JSON tree parsed from the same response text.

    Args:
        tree: The raw JSON tree the strict result was validated from.
        result: The strictly validated result of that same response text.

    Returns:
        ``True`` only when every non-null status keeps each raw field type exactly.
    """
    root = cast("dict[str, dict[str, object]]", tree)
    pairs: list[tuple[object, FinalisationFirstCrackStatus | None]] = [
        (root["pre_finalisation_first_crack_status"], result.pre_finalisation_first_crack_status)
    ]
    if result.first_crack_runtime is not None:
        pairs.append(
            (root["first_crack_runtime"]["final_status"], result.first_crack_runtime.final_status)
        )
    for raw, parsed in pairs:
        if parsed is None:
            continue
        mapping = cast("dict[str, object]", raw)
        for name in FinalisationFirstCrackStatus.model_fields:
            if type(mapping[name]) is not type(getattr(parsed, name)):
                return False
    return True


def _finalisation_has_safe_zero(result: SessionFinalisationResult) -> bool:
    """Whether D195 final evidence proves all six command dimensions are zero."""
    evidence = _trusted_final_driver_evidence(result)
    if evidence is None:
        return False
    return (
        evidence.safe_zero is True
        and not evidence.non_zero_dimensions
        and evidence.connected is False
        and evidence.heat_level_percent == 0
        and evidence.roast_fan_level_percent == 0
        and evidence.main_fan_level_percent == 0
        and evidence.drum_motor_on is False
        and evidence.cooling_motor_on is False
        and evidence.solenoid_open is False
    )


def _command_streaming_required(evidence: DriverCommandStateEvidence) -> bool:
    """Return the sole typed capability discriminator for finalisation evidence."""
    return evidence.command_streaming_required


def finalisation_command_streaming_observation(result: SessionFinalisationResult) -> bool | None:
    """Return the observed capability discriminator, or ``None`` without trusted evidence.

    This is a read-only accessor over the sole predicate; it computes no outcome.

    Args:
        result: Strictly parsed MCP finalisation result.

    Returns:
        The trusted final driver evidence's streaming requirement, else ``None``.
    """
    evidence = _trusted_final_driver_evidence(result)
    if evidence is None:
        return None
    return _command_streaming_required(evidence)


def _finalisation_has_capability_compatible_evidence(result: SessionFinalisationResult) -> bool:
    """Apply the sole AC23 streaming-capability branch to strict evidence."""
    evidence = _trusted_final_driver_evidence(result)
    if evidence is None:
        return False
    counters = (
        evidence.command_send_attempts,
        evidence.command_write_count,
        evidence.last_command_write_size,
        evidence.command_loop_error_count,
        evidence.status_packet_count,
        evidence.status_read_error_count,
    )
    confirmations = (
        result.disconnect.command_loop_stopped,
        result.disconnect.serial_closed,
    )
    if evidence.command_loop_running is True or evidence.serial_open is True:
        return False
    if _command_streaming_required(evidence):
        return all(counter is not None for counter in counters) and all(
            confirmation == "confirmed" for confirmation in confirmations
        )
    return all(
        confirmation == "confirmed" or confirmation == "not_applicable"
        for confirmation in confirmations
    )


def _finalisation_has_clean_disconnect(result: SessionFinalisationResult) -> bool:
    """Whether D195 disconnect evidence confirms a completed child disconnect."""
    disconnect = result.disconnect
    return (
        disconnect.attempt_count >= 1
        and disconnect.first_attempted_at_utc is not None
        and bool(disconnect.first_attempted_at_utc.strip())
        and disconnect.last_attempted_at_utc is not None
        and bool(disconnect.last_attempted_at_utc.strip())
        and disconnect.last_returned_without_error is True
        and disconnect.last_error is None
        and disconnect.connected_false_confirmed is True
    )


def finalisation_has_required_safety_evidence(result: SessionFinalisationResult) -> bool:
    """Whether parsed finalisation evidence satisfies every safety boundary.

    Args:
        result: Identity-bound, strictly parsed finalisation result.

    Returns:
        ``True`` only when safe-zero, capability, and disconnect evidence all hold.
    """
    return (
        _finalisation_has_safe_zero(result)
        and _finalisation_has_capability_compatible_evidence(result)
        and _finalisation_has_clean_disconnect(result)
    )


class ColdTickDeviceState(BaseModel):
    """Strict, complete projection of one raw per-tick MCP device state.

    It records exact raw values and decides nothing: it applies no zero,
    connected, range, plausibility, or startup-interval policy.
    ``ColdTickObservation.device`` is the only admissible per-tick device
    evidence; ``state.device_state`` is tolerant roast-path telemetry whose
    values may be coerced or whose unknown keys may be dropped, so it must
    never feed cold evidence.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, allow_inf_nan=False)

    driver: str
    connected: bool
    bean_temp_c: float | None
    env_temp_c: float | None
    heat_level_percent: int
    fan_level_percent: int
    cooling_on: bool
    raw_vendor_data: dict[str, ColdJsonValue]


class ColdTickRoastFanObservation(BaseModel):
    """Strict commanded roast-fan state projected from one cold MCP tick.

    This records and decides nothing. It is not physical sensing, and
    ``device.fan_level_percent`` remains the main-fan value rather than a
    roast-fan observation.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, allow_inf_nan=False)

    outcome: ColdRoastFanOutcome
    roast_fan_level_percent: int | None

    @model_validator(mode="after")
    def _validate_outcome_level_pair(self) -> "ColdTickRoastFanObservation":
        """Require a bounded level only for an observed command state.

        Returns:
            This validated observation.

        Raises:
            ValueError: If outcome and level presence or bounds disagree.
        """
        level = self.roast_fan_level_percent
        if self.outcome is ColdRoastFanOutcome.OBSERVED:
            if type(level) is not int or not 0 <= level <= 100:
                raise ValueError("observed roast fan requires a bounded integer level")
        elif level is not None:
            raise ValueError("non-observed roast fan requires a null level")
        return self


#: Exact raw JSON types admitted per device field (compared with ``is``).
_DEVICE_FIELD_TYPES: dict[ColdDeviceField, tuple[type, ...]] = {
    ColdDeviceField.DRIVER: (str,),
    ColdDeviceField.CONNECTED: (bool,),
    ColdDeviceField.BEAN_TEMP_C: (float, type(None)),
    ColdDeviceField.ENV_TEMP_C: (float, type(None)),
    ColdDeviceField.HEAT_LEVEL_PERCENT: (int,),
    ColdDeviceField.FAN_LEVEL_PERCENT: (int,),
    ColdDeviceField.COOLING_ON: (bool,),
    ColdDeviceField.RAW_VENDOR_DATA: (dict,),
}


def _canonical_vendor_json(value: object) -> str:
    """Return canonical JSON byte-identical to the evidence schema's persistence form."""
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


def _has_exact_type(value: object, allowed: tuple[type, ...]) -> bool:
    """Whether a value's exact type is one of the allowed types (no subclasses)."""
    return any(type(value) is expected for expected in allowed)


#: Exact raw JSON types admitted per session field, in declaration order
#: (compared with ``is``; a ``bool`` is never admitted as an ``int`` and a JSON
#: integer is never admitted as a float).
_SESSION_FIELD_TYPES: tuple[tuple[ColdSessionField, tuple[type, ...]], ...] = (
    (ColdSessionField.SESSION_ID, (str,)),
    (ColdSessionField.ACTIVE, (bool,)),
    (ColdSessionField.SESSION_PURPOSE, (str,)),
    (ColdSessionField.PHASE, (str,)),
    (ColdSessionField.ELAPSED_MONOTONIC_SECONDS, (float,)),
)


class ColdTickSessionMetadata(BaseModel):
    """Strict per-tick MCP session metadata projected from one raw response.

    This model records and decides nothing: it applies no active, phase,
    advance, range, or startup policy.  ``ColdTickObservation.session`` is the
    only admissible per-tick session metadata.  The tolerant ``state`` fields
    ``active``, ``elapsed_monotonic_seconds``, and their siblings are lax
    roast-path telemetry and must never feed cold decisions or evidence.

    ``elapsed_monotonic_seconds`` is the MCP software session clock.  It is not
    evidence of serial-link or driver-sample freshness, nor of any physical state.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, allow_inf_nan=False)

    session_id: str
    active: bool
    session_purpose: Literal["cold_characterisation"]
    phase: MCPPhase
    elapsed_monotonic_seconds: float


class ColdTickObservation(BaseModel):
    """One identity-bound cold tick read from a single MCP response.

    ``audio`` is the only admissible per-tick first-crack evidence: a strict,
    complete projection of the raw first-crack payload of the same response.
    ``state.first_crack_status`` is tolerant roast-path telemetry whose counters
    may be defaulted or dropped, so it must never feed cold evidence.

    ``device`` is the only admissible per-tick device evidence: a strict,
    complete projection of the raw ``device_state`` of the same response, or
    ``None`` exactly when MCP reported JSON ``null``.  ``None`` records absent
    telemetry only; it is never a pass, a default, or a zero device, and every
    consumer must handle it explicitly.  ``state.device_state`` is tolerant
    telemetry and must never feed cold evidence.

    ``roast_fan`` is the commanded, not physical, roast-fan observation from
    the same response. The tolerant main-fan telemetry is never used for it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, allow_inf_nan=False)

    state: RoastSessionState
    audio: ColdTickProjection
    device: ColdTickDeviceState | None
    roast_fan: ColdTickRoastFanObservation


class ColdCharacterisationMCPClient:
    """Typed six-tool, non-actuating client for cold characterisation."""

    def __init__(self, call_tool: ToolCaller) -> None:
        """Create the client around its sole MCP transport callable.

        Args:
            call_tool: Transport supplied by the MCP lifecycle owner.
        """
        self._call_tool = call_tool
        self._cold_session_id: str | None = None

    @staticmethod
    def _validate(model: type[_ResultT], payload: object) -> _ResultT:
        """Validate one MCP result without exposing raw schema failures.

        Args:
            model: The trusted response mirror to validate against.
            payload: The untrusted MCP response payload.

        Raises:
            ColdMcpValidationError: If the response violates the cold contract.
        """
        try:
            result = model.model_validate_json(json.dumps(payload, allow_nan=False))
        except (RecursionError, TypeError, ValidationError, ValueError):
            pass
        else:
            return result
        raise ColdMcpValidationError("MCP response failed cold contract validation") from None

    @staticmethod
    def _validate_finalisation(payload: object) -> SessionFinalisationResult:
        """Validate one finalisation result and its exact first-crack status types.

        Both parses read the same serialised text, so the raw tree is exactly
        the response the strict mirror accepted.

        Args:
            payload: The untrusted MCP finalisation response payload.

        Raises:
            ColdMcpValidationError: If the response violates the cold contract.
        """
        try:
            text = json.dumps(payload, allow_nan=False)
            result = SessionFinalisationResult.model_validate_json(text)
            tree: object = json.loads(text)
        except (RecursionError, TypeError, ValidationError, ValueError):
            pass
        else:
            if _finalisation_status_types_are_exact(tree, result):
                return result
        raise ColdMcpValidationError("MCP response failed cold contract validation") from None

    @staticmethod
    def _parse_tick(payload: object) -> tuple[RoastSessionState, object]:
        """Parse one tick response into its tolerant mirror and its raw JSON tree.

        Both parses read the same serialised text, so the raw tree is exactly
        the response the tolerant mirror accepted.

        Args:
            payload: The untrusted MCP response payload.

        Raises:
            ColdMcpValidationError: If the response violates the cold contract.
        """
        try:
            text = json.dumps(payload, allow_nan=False)
            state = RoastSessionState.model_validate_json(text)
            tree: object = json.loads(text)
        except (RecursionError, TypeError, ValidationError, ValueError):
            pass
        else:
            return state, tree
        raise ColdMcpValidationError("MCP response failed cold contract validation") from None

    @staticmethod
    def _project_device(raw: object) -> ColdTickDeviceState | None:
        """Project one raw ``device_state`` value without coercing or defaulting it.

        Args:
            raw: The raw ``device_state`` value parsed from the tick response text.

        Returns:
            The strict device projection, or ``None`` exactly when the raw value is
            JSON ``null``.  ``None`` records absence only; it is never a clean state.

        Raises:
            ColdTickDeviceProjectionError: With closed diagnostics when the raw
                value is not an exact, complete, admitted device state.
        """
        if raw is None:
            return None
        if type(raw) is not dict:
            raise ColdTickDeviceProjectionError(
                ColdDeviceProjectionFailure.DEVICE_STATE_NOT_OBJECT, None
            )
        mapping = cast("dict[str, ColdJsonValue]", raw)
        admitted = True
        try:
            walk_json_value(mapping)
        except ColdEvidenceError:
            admitted = False
        if not admitted:
            raise ColdTickDeviceProjectionError(
                ColdDeviceProjectionFailure.DEVICE_VALUE_NOT_ADMITTED, None
            )
        if set(mapping) != {field.value for field in ColdDeviceField}:
            missing = next((field for field in ColdDeviceField if field.value not in mapping), None)
            raise ColdTickDeviceProjectionError(
                ColdDeviceProjectionFailure.FIELD_SET_MISMATCH, missing
            )
        for field, allowed in _DEVICE_FIELD_TYPES.items():
            if not _has_exact_type(mapping[field.value], allowed):
                raise ColdTickDeviceProjectionError(
                    ColdDeviceProjectionFailure.FIELD_TYPE_NOT_EXACT, field
                )
        vendor = mapping[ColdDeviceField.RAW_VENDOR_DATA.value]
        if len(_canonical_vendor_json(vendor).encode("utf-8")) > MAX_VENDOR_BLOB_BYTES:
            raise ColdTickDeviceProjectionError(
                ColdDeviceProjectionFailure.VENDOR_DATA_TOO_LARGE, ColdDeviceField.RAW_VENDOR_DATA
            )
        fresh = dict(mapping)
        fresh[ColdDeviceField.RAW_VENDOR_DATA.value] = copy.deepcopy(vendor)
        device: ColdTickDeviceState | None = None
        with suppress(ValidationError):
            device = ColdTickDeviceState.model_validate(fresh, strict=True)
        if device is None:  # pragma: no cover - unreachable after the exact type checks
            raise ColdTickDeviceProjectionError(
                ColdDeviceProjectionFailure.FIELD_TYPE_NOT_EXACT, None
            )
        for field in ColdDeviceField:
            if field is ColdDeviceField.RAW_VENDOR_DATA:
                continue
            if type(getattr(device, field.value)) is not type(mapping[field.value]):
                raise ColdTickDeviceProjectionError(
                    ColdDeviceProjectionFailure.FIELD_TYPE_NOT_EXACT, field
                )
        return device

    @staticmethod
    def _project_roast_fan(tree: dict[str, object]) -> ColdTickRoastFanObservation:
        """Strictly project the commanded roast-fan observation from one raw tick.

        Args:
            tree: Raw JSON tree parsed from the same tick response as the state.

        Returns:
            The closed commanded-state observation.

        Raises:
            ColdTickRoastFanProjectionError: With one closed projection failure.
        """
        failure: ColdRoastFanProjectionFailure | None = None
        raw: object = None
        try:
            raw = tree["cold_characterisation_observation"]
        except KeyError:
            failure = ColdRoastFanProjectionFailure.OBSERVATION_KEY_MISSING
        if failure is None and raw is None:
            failure = ColdRoastFanProjectionFailure.OBSERVATION_NULL
        if failure is None and type(raw) is not dict:
            failure = ColdRoastFanProjectionFailure.OBSERVATION_NOT_OBJECT

        mapping: dict[str, object] | None = None
        if failure is None:
            mapping = cast("dict[str, object]", raw)
            if set(mapping) != {"outcome", "roast_fan_level_percent"}:
                failure = ColdRoastFanProjectionFailure.FIELD_SET_MISMATCH

        outcome: ColdRoastFanOutcome | None = None
        level: object = None
        if failure is None and mapping is not None:
            raw_outcome = mapping["outcome"]
            level = mapping["roast_fan_level_percent"]
            if type(raw_outcome) is not str:
                failure = ColdRoastFanProjectionFailure.OUTCOME_NOT_ADMITTED
            else:
                try:
                    outcome = ColdRoastFanOutcome(raw_outcome)
                except ValueError:
                    failure = ColdRoastFanProjectionFailure.OUTCOME_NOT_ADMITTED

        if failure is None and type(level) not in (int, type(None)):
            failure = ColdRoastFanProjectionFailure.LEVEL_TYPE_NOT_EXACT
        if failure is None and type(level) is int and not 0 <= level <= 100:
            failure = ColdRoastFanProjectionFailure.LEVEL_OUT_OF_RANGE
        if (
            failure is None
            and outcome is not None
            and (
                (outcome is ColdRoastFanOutcome.OBSERVED and level is None)
                or (outcome is not ColdRoastFanOutcome.OBSERVED and level is not None)
            )
        ):
            failure = ColdRoastFanProjectionFailure.LEVEL_OUTCOME_MISMATCH
        if failure is not None:
            raise ColdTickRoastFanProjectionError(failure)

        try:
            return ColdTickRoastFanObservation.model_validate(
                {"outcome": outcome, "roast_fan_level_percent": level}, strict=True
            )
        except ValidationError:  # pragma: no cover - exact guards are exhaustive
            failure = ColdRoastFanProjectionFailure.LEVEL_OUTCOME_MISMATCH
        raise ColdTickRoastFanProjectionError(failure)  # pragma: no cover - exact guards

    @staticmethod
    def _project_session(tree: dict[str, object]) -> ColdTickSessionMetadata:
        """Strictly project the top-level session metadata from one raw tick.

        The projection records and decides nothing: it applies no active,
        phase, advance, range, or startup policy.

        Args:
            tree: Raw JSON tree parsed from the same tick response as the state.

        Returns:
            The strict session metadata carrying the exact raw values.

        Raises:
            ColdTickSessionProjectionError: With the one closed field whose raw
                JSON type is not exactly the declared type, or ``None`` when the
                strict model refuses the exactly typed values.
        """
        values: dict[str, object] = {}
        for field, allowed in _SESSION_FIELD_TYPES:
            raw = tree[field.value]
            if not _has_exact_type(raw, allowed):
                raise ColdTickSessionProjectionError(field)
            values[field.value] = raw
        session: ColdTickSessionMetadata | None = None
        with suppress(ValidationError):
            session = ColdTickSessionMetadata.model_validate(values, strict=True)
        if session is None:
            raise ColdTickSessionProjectionError(None)
        return session

    @staticmethod
    def _require_lossless_audio_types(
        raw_audio: dict[str, ColdJsonValue], projection: ColdTickProjection
    ) -> None:
        """Refuse a projection whose named field type differs from its raw JSON type.

        The strict projection still converts a JSON integer into a float field,
        so each of the twenty named audio fields must keep its exact raw type.

        Args:
            raw_audio: The raw first-crack payload the projection was built from.
            projection: The strict projection of that same payload.

        Raises:
            ColdEvidenceError: With a closed failure and the one mismatched field.
        """
        for field in ColdAudioField:
            if field is ColdAudioField.UNKNOWN_FIELD:
                continue
            projected: object = getattr(projection.audio, field.value)
            if type(raw_audio[field.value]) is not type(projected):
                raise ColdEvidenceError(ColdEvidenceFailure.TICK_PAYLOAD_NOT_STRICT, (field,))

    async def _call(self, tool: str, args: dict[str, object]) -> object:
        if tool not in COLD_ALLOWED_TOOLS:
            raise ColdModeForbiddenToolError(f"tool not allowed in cold mode: {tool}")
        try:
            return await self._call_tool(tool, args)
        except MCPConnectionError:
            pass
        raise ColdMcpTransportError("MCP transport failed in cold mode") from None

    async def get_server_info(self) -> ServerInfo:
        """Return the typed MCP server inventory."""
        return self._validate(ServerInfo, await self._call("get_server_info", {}))

    async def get_runtime_config(self) -> RuntimeConfigSnapshot:
        """Return the typed MCP runtime configuration snapshot."""
        return self._validate(RuntimeConfigSnapshot, await self._call("get_runtime_config", {}))

    async def start_cold_session(self) -> StartRoastSessionResult:
        """Start and require confirmation of a cold-characterisation session."""
        if self._cold_session_id is not None:
            raise ColdSessionIdentityError("cold session is already established")
        self._cold_session_id = None
        try:
            result = self._validate(
                StartRoastSessionResult,
                await self._call("start_roast_session", {"purpose": "cold_characterisation"}),
            )
            if result.session.session_purpose != "cold_characterisation":
                raise ColdSessionPurposeError("MCP did not confirm cold_characterisation purpose")
            if not result.session.session_id.strip():
                raise ColdSessionIdentityError("MCP did not return a cold session id")
        except ColdMcpError:
            self._cold_session_id = None
            raise
        self._cold_session_id = result.session.session_id
        return result

    async def get_roast_state(self, session_id: str | None = None) -> ColdTickObservation:
        """Return one cold tick whose audio, device, and roast-fan evidence are projected.

        Args:
            session_id: Optional explicit session; it must be the established one.

        Returns:
            The tolerant session state, the strict audio projection, and the
            strict device projection (``None`` only for JSON ``null``), and
            strict commanded roast-fan projection, all parsed from one MCP
            response. Audio is projected before device and roast fan.

        Raises:
            ColdSessionIdentityError: If the session is not the established one.
            ColdSessionPurposeError: If MCP does not confirm the cold purpose.
            ColdMcpTransportError: If the MCP transport fails.
            ColdMcpValidationError: If the response violates the cold contract.
            ColdTickAudioProjectionError: If the audio evidence is not strictly complete.
            ColdTickDeviceProjectionError: If the device state is not strictly complete.
            ColdTickRoastFanProjectionError: If the roast-fan observation is malformed.
        """
        expected_session_id = self._cold_session_id
        if expected_session_id is None or (
            session_id is not None and session_id != expected_session_id
        ):
            raise ColdSessionIdentityError("requested cold session is not established")
        state, tree = self._parse_tick(
            await self._call("get_roast_state", {"session_id": expected_session_id})
        )
        if state.session_id != expected_session_id:
            raise ColdSessionIdentityError("MCP did not return the established cold session")
        if state.session_purpose != "cold_characterisation":
            raise ColdSessionPurposeError("MCP did not confirm cold_characterisation purpose")
        try:
            raw_audio = cast("dict[str, dict[str, ColdJsonValue]]", tree)["first_crack_status"]
            audio = project_tick_audio(raw_audio)
            self._require_lossless_audio_types(raw_audio, audio)
        except ColdEvidenceError as error:
            failure = error.failure
            field_names = error.field_names
        else:
            device = self._project_device(cast("dict[str, object]", tree)["device_state"])
            roast_fan = self._project_roast_fan(cast("dict[str, object]", tree))
            return ColdTickObservation(state=state, audio=audio, device=device, roast_fan=roast_fan)
        raise ColdTickAudioProjectionError(failure, field_names)

    async def mark_beans_added(self) -> EventCommandResult:
        """Request the permitted, non-actuating inference-activation event."""
        if self._cold_session_id is None:
            raise ColdSessionIdentityError("cold session identity is not established")
        result = self._validate(EventCommandResult, await self._call("mark_beans_added", {}))
        if result.session_id != self._cold_session_id:
            raise ColdSessionIdentityError("MCP did not return the established cold session")
        if result.phase != "roasting":
            raise ColdSessionPhaseError("MCP did not confirm cold inference activation")
        if result.event.kind != "beans_added":
            raise ColdSessionPhaseError("MCP did not confirm cold inference activation")
        return result

    async def finalise_session(self, session_id: str) -> SessionFinalisationResult:
        """Finalise one explicit session and reject any unclean evidence.

        Args:
            session_id: The explicitly retained MCP session identifier.

        Raises:
            ColdMcpValidationError: If MCP output is malformed or schema-drifted.
            ColdFinalisationNotCleanError: If the G5 conjunction is not met.
            ColdFinalisationSafetyError: If G7 or G14 evidence is incomplete.
        """
        if not session_id.strip() or self._cold_session_id != session_id:
            raise ColdSessionIdentityError("requested cold session is not established")
        result = self._validate_finalisation(
            await self._call("finalise_cold_characterisation_session", {"session_id": session_id})
        )
        if result.session_id != session_id:
            raise ColdSessionIdentityError("MCP did not return the requested cold session")
        self._cold_session_id = None
        if not finalisation_is_clean(result):
            raise ColdFinalisationNotCleanError("MCP finalisation was not clean", result)
        if result.session_purpose != "cold_characterisation":
            raise ColdSessionPurposeError("MCP did not confirm cold_characterisation purpose")
        if not _finalisation_has_safe_zero(result):
            raise ColdFinalisationSafetyError("MCP finalisation lacks safe-zero evidence", result)
        if not _finalisation_has_capability_compatible_evidence(result):
            raise ColdFinalisationSafetyError(
                "MCP finalisation does not satisfy streaming capability evidence", result
            )
        if not _finalisation_has_clean_disconnect(result):
            raise ColdFinalisationSafetyError(
                "MCP finalisation lacks clean disconnect evidence", result
            )
        return result
