"""Strict MCP boundary for non-actuating cold characterisation.

This module deliberately owns a smaller MCP surface than the normal roast
client.  Finalisation evidence is parsed strictly because it is a safety gate,
not forward-compatible roast telemetry.
"""

from enum import Enum
from typing import Literal, Protocol, TypeAlias

from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, model_validator

from roastpilot_agent.config import MCPDeviceConfig
from roastpilot_agent.mcp_client import (
    EventCommandResult,
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


class ColdMcpError(RuntimeError):
    """Base error for a cold-mode MCP contract violation."""


class ColdModeForbiddenToolError(ColdMcpError):
    """Raised when a tool is outside the frozen cold-mode allow-list."""


class ColdSessionPurposeError(ColdMcpError):
    """Raised when MCP does not confirm a cold-characterisation session."""


class ColdFinalisationNotCleanError(ColdMcpError):
    """Raised when D195 finalisation is not clean."""


class ColdFinalisationSafetyError(ColdMcpError):
    """Raised when finalisation lacks safe-zero or disconnect evidence."""


class StrictMCPMirror(BaseModel):
    """Immutable MCP mirror that rejects all unknown fields."""

    model_config = ConfigDict(frozen=True, extra="forbid")


RejectionReasonLiteral: TypeAlias = Literal[
    "unknown_session",
    "not_latest_session",
    "session_not_active",
    "session_purpose_not_eligible",
    "session_faulted",
    "command_in_progress",
    "finalisation_in_progress",
    "driver_lifecycle_evidence_unsupported",
    "driver_state_unreadable",
    "driver_state_malformed",
    "driver_not_connected",
    "driver_state_not_safe_zero",
]


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
    session_phase_after: str | None

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
        ``True`` only for the four-field clean conjunction required by G5.
    """
    return (
        result.status == "clean"
        and result.clean is True
        and result.rejection_reason is None
        and result.abort_reason is None
    )


def _finalisation_has_safe_zero(result: SessionFinalisationResult) -> bool:
    """Whether D195 final evidence proves all six command dimensions are zero."""
    driver_read = result.final_driver_evidence
    if driver_read is None or driver_read.outcome != "read" or driver_read.evidence is None:
        return False
    evidence = driver_read.evidence
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


def _finalisation_has_capability_compatible_evidence(result: SessionFinalisationResult) -> bool:
    """Apply the sole AC23 streaming-capability branch to strict evidence."""
    driver_read = result.final_driver_evidence
    if driver_read is None or driver_read.evidence is None:
        return False
    evidence = driver_read.evidence
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
        and disconnect.last_attempted_at_utc is not None
        and disconnect.last_returned_without_error is True
        and disconnect.last_error is None
        and disconnect.connected_false_confirmed is True
    )


class ColdCharacterisationMCPClient:
    """Typed six-tool, non-actuating client for cold characterisation."""

    def __init__(self, call_tool: ToolCaller) -> None:
        """Create the client around its sole MCP transport callable.

        Args:
            call_tool: Transport supplied by the MCP lifecycle owner.
        """
        self._call_tool = call_tool

    async def _call(self, tool: str, args: dict[str, object]) -> object:
        if tool not in COLD_ALLOWED_TOOLS:
            raise ColdModeForbiddenToolError(f"tool not allowed in cold mode: {tool}")
        return await self._call_tool(tool, args)

    async def get_server_info(self) -> ServerInfo:
        """Return the typed MCP server inventory."""
        return ServerInfo.model_validate(await self._call("get_server_info", {}))

    async def get_runtime_config(self) -> RuntimeConfigSnapshot:
        """Return the typed MCP runtime configuration snapshot."""
        return RuntimeConfigSnapshot.model_validate(await self._call("get_runtime_config", {}))

    async def start_cold_session(self) -> StartRoastSessionResult:
        """Start and require confirmation of a cold-characterisation session."""
        result = StartRoastSessionResult.model_validate(
            await self._call("start_roast_session", {"purpose": "cold_characterisation"})
        )
        if result.session.session_purpose != "cold_characterisation":
            raise ColdSessionPurposeError("MCP did not confirm cold_characterisation purpose")
        return result

    async def get_roast_state(self, session_id: str | None = None) -> RoastSessionState:
        """Return one cold session state, omitting an unspecified identifier."""
        args: dict[str, object] = {} if session_id is None else {"session_id": session_id}
        return RoastSessionState.model_validate(await self._call("get_roast_state", args))

    async def mark_beans_added(self) -> EventCommandResult:
        """Request the permitted, non-actuating inference-activation event."""
        return EventCommandResult.model_validate(await self._call("mark_beans_added", {}))

    async def finalise_session(self, session_id: str) -> SessionFinalisationResult:
        """Finalise one explicit session and reject any unclean evidence.

        Args:
            session_id: The explicitly retained MCP session identifier.

        Raises:
            ValidationError: If MCP output is malformed or schema-drifted.
            ColdFinalisationNotCleanError: If the G5 conjunction is not met.
            ColdFinalisationSafetyError: If G7 or G14 evidence is incomplete.
        """
        result = SessionFinalisationResult.model_validate(
            await self._call("finalise_cold_characterisation_session", {"session_id": session_id})
        )
        if not finalisation_is_clean(result):
            raise ColdFinalisationNotCleanError("MCP finalisation was not clean")
        if not _finalisation_has_safe_zero(result):
            raise ColdFinalisationSafetyError("MCP finalisation lacks safe-zero evidence")
        if not _finalisation_has_capability_compatible_evidence(result):
            raise ColdFinalisationSafetyError(
                "MCP finalisation does not satisfy streaming capability evidence"
            )
        if not _finalisation_has_clean_disconnect(result):
            raise ColdFinalisationSafetyError("MCP finalisation lacks clean disconnect evidence")
        return result
