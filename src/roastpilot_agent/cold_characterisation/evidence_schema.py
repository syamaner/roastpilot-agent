"""Closed cold-characterisation evidence schema and strict ingress boundary.

This module deliberately contains no transport, filesystem, provider, or outcome
logic.  It validates the small, typed evidence vocabulary that later slices may
persist and render.
"""

import collections.abc
import enum
import hashlib
import json
import math
import typing

import pydantic

MAX_RECORD_BYTES = 262_144
MAX_VENDOR_BLOB_BYTES = 16_384
MAX_RAW_AUDIO_EXTRA_BYTES = 8_192
MAX_ENVELOPE_BYTES = 1_048_576
MAX_TEXT_FIELD_BYTES = 2_048
MAX_INPUT_AGGREGATE_BYTES = 524_288
MAX_JSON_DEPTH = 8
MAX_JSON_NODES = 4_096
MAX_JSON_KEY_BYTES = 256
MAX_INT_DIGITS = 32
MAX_COLLECTION_LENGTH = 1_024
_RUN_ID_PATTERN = r"\A[0-9]{8}T[0-9]{6}Z-[a-z0-9-]{1,48}\Z"
_SHA256_PATTERN = r"\A[0-9a-f]{64}\Z"

_COLD_EVIDENCE_MODEL_CONFIG = pydantic.ConfigDict(
    frozen=True, extra="forbid", allow_inf_nan=False, regex_engine="python-re"
)
_COLD_EVIDENCE_STRICT_CONFIG = pydantic.ConfigDict(
    frozen=True, extra="forbid", allow_inf_nan=False, strict=True, regex_engine="python-re"
)


class ColdAudioField(enum.Enum):
    """Closed diagnostic names for the strict per-tick audio projection."""

    MODE = "mode"
    STATUS = "status"
    DETECTED_AT_UTC = "detected_at_utc"
    DETECTED_MONOTONIC_SECONDS = "detected_monotonic_seconds"
    ALLOW_MANUAL_OVERRIDE = "allow_manual_override"
    REASON = "reason"
    AUDIO_RUNNING = "audio_running"
    QUEUED_WINDOW_COUNT = "queued_window_count"
    EMITTED_WINDOW_COUNT = "emitted_window_count"
    DROPPED_WINDOW_COUNT = "dropped_window_count"
    PROCESSED_WINDOW_COUNT = "processed_window_count"
    MIC_PEAK_DBFS = "mic_peak_dbfs"
    MIC_RMS_DBFS = "mic_rms_dbfs"
    OVERFLOW_COUNT_LAST_MINUTE = "overflow_count_last_minute"
    ESTIMATED_LOST_AUDIO_MS_LAST_MINUTE = "estimated_lost_audio_ms_last_minute"
    TOTAL_OVERFLOW_COUNT = "total_overflow_count"
    MAX_CONSECUTIVE_OVERFLOW_COUNT = "max_consecutive_overflow_count"
    LAST_INFERENCE_DURATION_MS = "last_inference_duration_ms"
    MAX_INFERENCE_DURATION_MS = "max_inference_duration_ms"
    INFERENCE_OVERRUN_COUNT = "inference_overrun_count"
    UNKNOWN_FIELD = "unknown_field"


class ColdEvidenceFailure(enum.Enum):
    """Closed failures for evidence ingress and structure admission."""

    TICK_PAYLOAD_NOT_STRICT = "tick_payload_not_strict"
    RECORD_NOT_VALIDATED = "record_not_validated"
    RECORD_TOO_LARGE = "record_too_large"
    RECORD_VENDOR_BLOB_TOO_LARGE = "record_vendor_blob_too_large"
    RECORD_RAW_AUDIO_EXTRA_TOO_LARGE = "record_raw_audio_extra_too_large"
    TEXT_FIELD_TOO_LARGE = "text_field_too_large"
    JSON_VALUE_TYPE_NOT_ADMITTED = "json_value_type_not_admitted"
    JSON_DEPTH_EXCEEDED = "json_depth_exceeded"
    JSON_NODE_LIMIT_EXCEEDED = "json_node_limit_exceeded"
    JSON_KEY_INVALID = "json_key_invalid"
    JSON_VALUE_NOT_FINITE = "json_value_not_finite"
    ENVELOPE_DIGEST_MISMATCHED = "envelope_digest_mismatched"
    ENVELOPE_LENGTH_MISMATCHED = "envelope_length_mismatched"
    ENVELOPE_NOT_CANONICAL = "envelope_not_canonical"
    ENVELOPE_TOO_LARGE = "envelope_too_large"
    ENVELOPE_KIND_MISMATCHED = "envelope_kind_mismatched"
    RUN_ID_MALFORMED = "run_id_malformed"
    IDENTITY_DIGEST_MALFORMED = "identity_digest_malformed"
    ABORT_DOMAIN_REASON_MISMATCHED = "abort_domain_reason_mismatched"


class ColdMcpAbortReason(enum.Enum):
    """Closed MCP finalisation refusal grammar."""

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
    EMERGENCY_STOP = "emergency_stop"
    SESSION_OR_RESERVATION_CHANGED = "session_or_reservation_changed"


class ColdHostAbortReason(enum.Enum):
    """Closed host-bound refusal grammar."""

    THERMAL_UNREADABLE = "thermal_unreadable"
    THERMAL_MALFORMED = "thermal_malformed"
    THERMAL_EXCEEDED = "thermal_exceeded"
    THROTTLE_BINARY_MISSING = "throttle_binary_missing"
    THROTTLE_INVOCATION_FAILED = "throttle_invocation_failed"
    THROTTLE_TIMEOUT = "throttle_timeout"
    THROTTLE_OUTPUT_MALFORMED = "throttle_output_malformed"
    THROTTLE_BITS_SET = "throttle_bits_set"
    MEMINFO_UNREADABLE = "meminfo_unreadable"
    MEMINFO_MALFORMED = "meminfo_malformed"
    MEMINFO_BELOW_BOUND = "meminfo_below_bound"
    DISK_UNREADABLE = "disk_unreadable"
    DISK_BELOW_START_BOUND = "disk_below_start_bound"
    DISK_BELOW_RUN_BOUND = "disk_below_run_bound"
    PLATFORM_UNSUPPORTED = "platform_unsupported"


class ColdIdentityAbortReason(enum.Enum):
    """Closed cold-run identity refusal grammar."""

    MCP_VERSION_NOT_PINNED = "mcp_version_not_pinned"
    TEMPERATURE_UNIT_NOT_CELSIUS = "temperature_unit_not_celsius"
    RECORDING_DEVICE_NOT_SINGLE = "recording_device_not_single"
    INFERENCE_NOT_ACTIVE_IN_IDENTITY = "inference_not_active_in_identity"
    BOOT_ID_UNREADABLE = "boot_id_unreadable"
    BOOT_ID_MALFORMED = "boot_id_malformed"
    CREDENTIAL_NAME_NOT_ALLOWED = "credential_name_not_allowed"
    OPERATOR_TEXT_REJECTED = "operator_text_rejected"
    DEVICE_CONFIG_FIELD_SET_DRIFTED = "device_config_field_set_drifted"
    DEVICE_CONFIG_VALUE_REJECTED = "device_config_value_rejected"
    PROVENANCE_ARTEFACT_DIGEST_MISMATCHED = "provenance_artefact_digest_mismatched"


class ColdPhaseKind(enum.Enum):
    """Recorded cold-characterisation phase without an outcome meaning."""

    RECORDING_OFF = "recording_off"
    RECORDING_ON = "recording_on"


class ColdEvidenceStream(enum.Enum):
    """Closed evidence-record discriminator values."""

    HEADER = "header"
    TICK = "tick"
    HOST = "host"
    ADVISORY = "advisory"
    FINALISATION = "finalisation"
    ABORT = "abort"


class ColdEnvelopeKind(enum.Enum):
    """Closed kinds of retained canonical evidence bytes."""

    IDENTITY = "identity"
    FINALISATION = "finalisation"


class ColdCapabilityBranch(enum.Enum):
    """Recorded MCP capability branch without inferring it locally."""

    STREAMING = "streaming"
    NON_STREAMING = "non_streaming"


class ColdFinalisationStatus(enum.Enum):
    """Closed finalisation status grammar mirrored from MCP 0.2.1."""

    REJECTED = "rejected"
    CLEAN = "clean"
    COMPLETED_NOT_CLEAN = "completed_not_clean"
    PARTIAL = "partial"
    DISCONNECT_INDETERMINATE = "disconnect_indeterminate"
    ABORTED = "aborted"


class ColdSafetyVerdict(enum.Enum):
    """Closed safety-policy verdict grammar."""

    ALLOW = "allow"
    CLAMP = "clamp"
    REJECT = "reject"
    RECOVERY = "recovery"
    FAULT = "fault"
    EMERGENCY_STOP = "emergency_stop"


class ColdAdvisorFailureKind(enum.Enum):
    """Closed advisor-failure classification with no provider content."""

    TIMEOUT = "timeout"
    PROVIDER_ERROR = "provider_error"
    MALFORMED_OUTPUT = "malformed_output"
    UNSAFE_OUTPUT = "unsafe_output"


class ColdAbortDomain(enum.Enum):
    """Closed domains for a typed abort record."""

    HOST = "host"
    IDENTITY = "identity"
    EVIDENCE = "evidence"
    MCP = "mcp"
    ADVISOR = "advisor"
    OPERATOR = "operator"


class ColdOperatorAbortReason(enum.Enum):
    """The sole operator-originated abort classification."""

    OPERATOR_STOP = "operator_stop"


class ColdEvidenceError(RuntimeError):
    """Closed error which never retains untrusted input or parser details."""

    failure: ColdEvidenceFailure
    field_names: tuple[ColdAudioField, ...]

    def __init__(
        self,
        failure: ColdEvidenceFailure,
        field_names: tuple[ColdAudioField, ...] = (),
    ) -> None:
        """Create a content-free evidence failure.

        Args:
            failure: Closed refusal reason.
            field_names: Closed schema field names, if projection failed.
        """
        super().__init__("Cold evidence admission failed.")
        self.failure = failure
        self.field_names = field_names


# Pydantic's named recursive JSON schema is used only for model annotations;
# ``walk_json_value`` supplies this boundary's stricter exact-container admission.
ColdJsonValue: typing.TypeAlias = pydantic.JsonValue


def _closed(failure: ColdEvidenceFailure) -> ColdEvidenceError:
    """Return a fresh content-free error after an internal failure is handled."""
    return ColdEvidenceError(failure)


def _admit_text(value: str, aggregate: list[int], maximum: int | None = None) -> None:
    """Account for text without converting any arbitrary object to text."""
    if aggregate[0] + len(value) > MAX_INPUT_AGGREGATE_BYTES:
        raise _closed(ColdEvidenceFailure.RECORD_TOO_LARGE)
    encoded = value.encode("utf-8")
    if aggregate[0] + len(encoded) > MAX_INPUT_AGGREGATE_BYTES:
        raise _closed(ColdEvidenceFailure.RECORD_TOO_LARGE)
    aggregate[0] += len(encoded)
    if maximum is not None and len(encoded) > maximum:
        raise _closed(ColdEvidenceFailure.TEXT_FIELD_TOO_LARGE)


def walk_json_value(value: ColdJsonValue) -> None:
    """Iteratively validate one admitted JSON value before serialisation.

    Args:
        value: Exact JSON-shaped value to admit.

    Raises:
        ColdEvidenceError: If a type, resource, key, or finite-number invariant fails.
    """
    _walk_json_value(value, [0])


def _walk_json_value(value: ColdJsonValue, aggregate: list[int]) -> None:
    """Walk JSON using a caller-owned aggregate counter without serialising it."""
    nodes = 0
    stack: list[tuple[object, int]] = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES:
            raise _closed(ColdEvidenceFailure.JSON_NODE_LIMIT_EXCEEDED)
        if depth > MAX_JSON_DEPTH:
            raise _closed(ColdEvidenceFailure.JSON_DEPTH_EXCEEDED)
        if current is None or type(current) is bool:
            aggregate[0] += 5
        elif type(current) is int:
            if current > 10**MAX_INT_DIGITS - 1 or current < -(10**MAX_INT_DIGITS - 1):
                raise _closed(ColdEvidenceFailure.JSON_VALUE_TYPE_NOT_ADMITTED)
            aggregate[0] += MAX_INT_DIGITS + 1
        elif type(current) is float:
            if not math.isfinite(current):
                raise _closed(ColdEvidenceFailure.JSON_VALUE_NOT_FINITE)
            aggregate[0] += 32
        elif type(current) is str:
            _admit_text(current, aggregate)
        elif type(current) is list:
            items = typing.cast(list[object], current)
            if len(items) > MAX_COLLECTION_LENGTH:
                raise _closed(ColdEvidenceFailure.JSON_NODE_LIMIT_EXCEEDED)
            if nodes + len(stack) + len(items) > MAX_JSON_NODES:
                raise _closed(ColdEvidenceFailure.JSON_NODE_LIMIT_EXCEEDED)
            for child in items:
                stack.append((child, depth + 1))
        elif type(current) is dict:
            mapping = typing.cast(dict[object, object], current)
            if len(mapping) > MAX_COLLECTION_LENGTH:
                raise _closed(ColdEvidenceFailure.JSON_NODE_LIMIT_EXCEEDED)
            if nodes + len(stack) + len(mapping) > MAX_JSON_NODES:
                raise _closed(ColdEvidenceFailure.JSON_NODE_LIMIT_EXCEEDED)
            for key, child in mapping.items():
                if type(key) is not str:
                    raise _closed(ColdEvidenceFailure.JSON_KEY_INVALID)
                _admit_text(key, aggregate)
                if len(key.encode("utf-8")) > MAX_JSON_KEY_BYTES:
                    raise _closed(ColdEvidenceFailure.JSON_KEY_INVALID)
                stack.append((child, depth + 1))
        else:
            raise _closed(ColdEvidenceFailure.JSON_VALUE_TYPE_NOT_ADMITTED)
        if aggregate[0] > MAX_INPUT_AGGREGATE_BYTES:
            raise _closed(ColdEvidenceFailure.RECORD_TOO_LARGE)


def _canonical_json(value: object) -> str:
    """Return canonical JSON only after callers have admitted its value tree."""
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


class ColdTickAudioSample(pydantic.BaseModel):
    """Strict complete projection of a per-tick first-crack audio payload."""

    model_config = _COLD_EVIDENCE_STRICT_CONFIG

    mode: typing.Literal["disabled", "audio", "manual"]
    status: typing.Literal["disabled", "manual", "pending", "detected", "faulted", "unavailable"]
    detected_at_utc: str | None
    detected_monotonic_seconds: float | None
    allow_manual_override: bool
    reason: str | None = pydantic.Field(max_length=MAX_TEXT_FIELD_BYTES)
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


class ColdTickProjection(pydantic.BaseModel):
    """Frozen strict audio projection plus lossless unknown raw keys."""

    model_config = _COLD_EVIDENCE_MODEL_CONFIG

    audio: ColdTickAudioSample
    raw_audio_extra: dict[str, ColdJsonValue]


_AUDIO_FIELD_NAMES: tuple[str, ...] = tuple(
    field.value for field in ColdAudioField if field is not ColdAudioField.UNKNOWN_FIELD
)


def _projection_fields(error: pydantic.ValidationError) -> tuple[ColdAudioField, ...]:
    """Map parser locations to schema-owned diagnostics without preserving locations."""
    fields: list[ColdAudioField] = []
    for item in error.errors(include_url=False, include_input=False):
        location = item.get("loc", ())
        first = location[0] if type(location) is tuple and location else None
        member = typing.cast(
            ColdAudioField | None,
            ColdAudioField._value2member_map_.get(first),
        )
        fields.append(member if member is not None else ColdAudioField.UNKNOWN_FIELD)
    return tuple(fields)


def project_tick_audio(payload: dict[str, ColdJsonValue]) -> ColdTickProjection:
    """Strictly project one complete raw tick while preserving unknown keys.

    Args:
        payload: Exact raw JSON-shaped payload supplied by the MCP boundary.

    Returns:
        A frozen strict sample and fresh lossless forward-compatible map.

    Raises:
        ColdEvidenceError: If the payload is not a bounded strict projection.
    """
    try:
        if type(payload) is not dict:
            raise ValueError
        walk_json_value(payload)
        known: dict[str, ColdJsonValue] = {}
        extra: dict[str, ColdJsonValue] = {}
        for key, value in payload.items():
            if key in _AUDIO_FIELD_NAMES:
                known[key] = value
            else:
                extra[key] = value
        audio = ColdTickAudioSample.model_validate(known, strict=True)
        walk_json_value(extra)
        if len(_canonical_json(extra).encode("utf-8")) > MAX_RAW_AUDIO_EXTRA_BYTES:
            raise _closed(ColdEvidenceFailure.RECORD_RAW_AUDIO_EXTRA_TOO_LARGE)
        result = ColdTickProjection(
            audio=audio,
            raw_audio_extra=typing.cast(dict[str, ColdJsonValue], _copy_json_value(extra)),
        )
    except ColdEvidenceError as error:
        failure = error
    except pydantic.ValidationError as error:
        failure = ColdEvidenceError(
            ColdEvidenceFailure.TICK_PAYLOAD_NOT_STRICT,
            _projection_fields(error),
        )
    except Exception:
        failure = ColdEvidenceError(ColdEvidenceFailure.TICK_PAYLOAD_NOT_STRICT)
    else:
        return result
    raise failure


class ColdSealedEnvelope(pydantic.BaseModel):
    """Digest-bound canonical bytes retained without parsing their source model."""

    model_config = _COLD_EVIDENCE_MODEL_CONFIG

    kind: ColdEnvelopeKind
    schema_version: typing.Literal[1]
    canonical_json: str
    canonical_byte_length: int = pydantic.Field(ge=0)
    sha256: str = pydantic.Field(pattern=_SHA256_PATTERN)

    @pydantic.model_validator(mode="after")
    def _validate_canonical_bytes(self) -> typing.Self:
        """Verify the closed canonical-byte and digest invariants."""
        try:
            encoded = self.canonical_json.encode("utf-8")
            if len(encoded) > MAX_ENVELOPE_BYTES:
                raise _closed(ColdEvidenceFailure.ENVELOPE_TOO_LARGE)
            if self.canonical_byte_length != len(encoded):
                raise _closed(ColdEvidenceFailure.ENVELOPE_LENGTH_MISMATCHED)
            if self.sha256 != hashlib.sha256(encoded).hexdigest():
                raise _closed(ColdEvidenceFailure.ENVELOPE_DIGEST_MISMATCHED)
            parsed = json.loads(self.canonical_json)
            walk_json_value(parsed)
            if _canonical_json(parsed) != self.canonical_json:
                raise _closed(ColdEvidenceFailure.ENVELOPE_NOT_CANONICAL)
        except ColdEvidenceError:
            raise
        except Exception:
            failure = ColdEvidenceError(ColdEvidenceFailure.ENVELOPE_NOT_CANONICAL)
        else:
            return self
        raise failure


class ColdHostSample(pydantic.BaseModel):
    """Closed finite copy of one host-bound sample."""

    model_config = _COLD_EVIDENCE_STRICT_CONFIG

    captured_at_utc: str
    monotonic_seconds: float
    soc_temp_c: float
    throttled_word_hex: str
    mem_available_bytes: int
    free_bytes: int


class ColdSafetyEvaluation(pydantic.BaseModel):
    """Complete typed safety evaluation without provider-originated text."""

    model_config = _COLD_EVIDENCE_STRICT_CONFIG

    rule: str = pydantic.Field(min_length=1, max_length=MAX_TEXT_FIELD_BYTES)
    verdict: ColdSafetyVerdict
    input_heat: int | None
    input_fan: int | None
    adjusted_heat: int | None = pydantic.Field(ge=0, le=100)
    adjusted_fan: int | None = pydantic.Field(ge=0, le=100)
    reason: str = pydantic.Field(min_length=1, max_length=MAX_TEXT_FIELD_BYTES)


class ColdRunHeader(pydantic.BaseModel):
    """One retained identity header for a cold-characterisation run."""

    model_config = _COLD_EVIDENCE_MODEL_CONFIG

    schema_version: typing.Literal[1]
    stream: typing.Literal["header"]
    run_id: str = pydantic.Field(pattern=_RUN_ID_PATTERN)
    phase: ColdPhaseKind
    recorded_at_utc: str = pydantic.Field(max_length=MAX_TEXT_FIELD_BYTES)
    monotonic_seconds: float
    identity_sha256: str = pydantic.Field(pattern=_SHA256_PATTERN)
    identity: ColdSealedEnvelope

    @pydantic.model_validator(mode="after")
    def _require_identity_kind(self) -> typing.Self:
        """Require an identity envelope for the header stream."""
        if self.identity.kind is not ColdEnvelopeKind.IDENTITY:
            raise ColdEvidenceError(ColdEvidenceFailure.ENVELOPE_KIND_MISMATCHED)
        return self


class ColdTickRecord(pydantic.BaseModel):
    """One non-actuating device and audio observation."""

    model_config = _COLD_EVIDENCE_MODEL_CONFIG

    schema_version: typing.Literal[1]
    stream: typing.Literal["tick"]
    run_id: str = pydantic.Field(pattern=_RUN_ID_PATTERN)
    phase: ColdPhaseKind
    recorded_at_utc: str = pydantic.Field(max_length=MAX_TEXT_FIELD_BYTES)
    monotonic_seconds: float
    identity_sha256: str = pydantic.Field(pattern=_SHA256_PATTERN)
    tick: int = pydantic.Field(ge=0)
    bean_temp_c: float | None
    env_temp_c: float | None
    heat_level_percent: int = pydantic.Field(ge=0, le=100)
    fan_level_percent: int = pydantic.Field(ge=0, le=100)
    cooling_on: bool
    connected: bool
    audio: ColdTickAudioSample
    raw_audio_extra: dict[str, ColdJsonValue] = pydantic.Field(default_factory=dict)
    raw_vendor_data: dict[str, ColdJsonValue] = pydantic.Field(default_factory=dict)


class ColdHostRecord(pydantic.BaseModel):
    """One closed host-bound observation record."""

    model_config = _COLD_EVIDENCE_MODEL_CONFIG

    schema_version: typing.Literal[1]
    stream: typing.Literal["host"]
    run_id: str = pydantic.Field(pattern=_RUN_ID_PATTERN)
    phase: ColdPhaseKind
    recorded_at_utc: str = pydantic.Field(max_length=MAX_TEXT_FIELD_BYTES)
    monotonic_seconds: float
    identity_sha256: str = pydantic.Field(pattern=_SHA256_PATTERN)
    sample: ColdHostSample


class ColdAdvisoryRecord(pydantic.BaseModel):
    """One typed advisory request and safety-policy evaluation record."""

    model_config = _COLD_EVIDENCE_MODEL_CONFIG

    schema_version: typing.Literal[1]
    stream: typing.Literal["advisory"]
    run_id: str = pydantic.Field(pattern=_RUN_ID_PATTERN)
    phase: ColdPhaseKind
    recorded_at_utc: str = pydantic.Field(max_length=MAX_TEXT_FIELD_BYTES)
    monotonic_seconds: float
    identity_sha256: str = pydantic.Field(pattern=_SHA256_PATTERN)
    requested_heat: int = pydantic.Field(ge=0, le=100)
    requested_fan: int = pydantic.Field(ge=0, le=100)
    should_drop: bool
    confidence: float = pydantic.Field(ge=0, le=1)
    latency_seconds: float = pydantic.Field(ge=0)
    evaluation: ColdSafetyEvaluation
    failure: ColdAdvisorFailureKind | None


class ColdFinalisationRecord(pydantic.BaseModel):
    """One retained MCP finalisation envelope record."""

    model_config = _COLD_EVIDENCE_MODEL_CONFIG

    schema_version: typing.Literal[1]
    stream: typing.Literal["finalisation"]
    run_id: str = pydantic.Field(pattern=_RUN_ID_PATTERN)
    phase: ColdPhaseKind
    recorded_at_utc: str = pydantic.Field(max_length=MAX_TEXT_FIELD_BYTES)
    monotonic_seconds: float
    identity_sha256: str = pydantic.Field(pattern=_SHA256_PATTERN)
    session_id: str = pydantic.Field(max_length=MAX_TEXT_FIELD_BYTES)
    envelope: ColdSealedEnvelope
    status: ColdFinalisationStatus
    clean: bool
    observed_command_streaming_required: bool
    applied_branch: ColdCapabilityBranch

    @pydantic.model_validator(mode="after")
    def _require_finalisation_kind(self) -> typing.Self:
        """Require a finalisation envelope for the finalisation stream."""
        if self.envelope.kind is not ColdEnvelopeKind.FINALISATION:
            raise ColdEvidenceError(ColdEvidenceFailure.ENVELOPE_KIND_MISMATCHED)
        return self


class ColdAbortRecord(pydantic.BaseModel):
    """A closed abort classification which carries neither verdict nor outcome."""

    model_config = _COLD_EVIDENCE_MODEL_CONFIG

    schema_version: typing.Literal[1]
    stream: typing.Literal["abort"]
    run_id: str = pydantic.Field(pattern=_RUN_ID_PATTERN)
    phase: ColdPhaseKind
    recorded_at_utc: str = pydantic.Field(max_length=MAX_TEXT_FIELD_BYTES)
    monotonic_seconds: float
    identity_sha256: str = pydantic.Field(pattern=_SHA256_PATTERN)
    domain: ColdAbortDomain
    reason: (
        ColdHostAbortReason
        | ColdIdentityAbortReason
        | ColdEvidenceFailure
        | ColdMcpAbortReason
        | ColdAdvisorFailureKind
        | ColdOperatorAbortReason
    )

    @pydantic.field_validator("reason", mode="before")
    @classmethod
    def _require_typed_reason(cls, value: object) -> object:
        """Keep raw strings outside the closed abort grammar."""
        if not isinstance(value, enum.Enum):
            raise ColdEvidenceError(ColdEvidenceFailure.ABORT_DOMAIN_REASON_MISMATCHED)
        return value

    @pydantic.model_validator(mode="after")
    def _require_domain_reason_pair(self) -> typing.Self:
        """Reject every reason whose enum class does not match its domain."""
        expected: dict[ColdAbortDomain, type[enum.Enum]] = {
            ColdAbortDomain.HOST: ColdHostAbortReason,
            ColdAbortDomain.IDENTITY: ColdIdentityAbortReason,
            ColdAbortDomain.EVIDENCE: ColdEvidenceFailure,
            ColdAbortDomain.MCP: ColdMcpAbortReason,
            ColdAbortDomain.ADVISOR: ColdAdvisorFailureKind,
            ColdAbortDomain.OPERATOR: ColdOperatorAbortReason,
        }
        if type(self.reason) is not expected[self.domain]:
            raise ColdEvidenceError(ColdEvidenceFailure.ABORT_DOMAIN_REASON_MISMATCHED)
        return self


ColdEvidenceRecord: typing.TypeAlias = typing.Annotated[
    ColdRunHeader
    | ColdTickRecord
    | ColdHostRecord
    | ColdAdvisoryRecord
    | ColdFinalisationRecord
    | ColdAbortRecord,
    pydantic.Field(discriminator="stream"),
]
_RECORD_ADAPTER: pydantic.TypeAdapter[ColdEvidenceRecord] = pydantic.TypeAdapter(ColdEvidenceRecord)
_RECORD_CLASSES = (
    ColdRunHeader,
    ColdTickRecord,
    ColdHostRecord,
    ColdAdvisoryRecord,
    ColdFinalisationRecord,
    ColdAbortRecord,
)
_MODEL_FIELDS: dict[type[pydantic.BaseModel], tuple[str, ...]] = {
    ColdTickAudioSample: tuple(ColdTickAudioSample.model_fields),
    ColdHostSample: tuple(ColdHostSample.model_fields),
    ColdSafetyEvaluation: tuple(ColdSafetyEvaluation.model_fields),
    ColdSealedEnvelope: tuple(ColdSealedEnvelope.model_fields),
    ColdRunHeader: tuple(ColdRunHeader.model_fields),
    ColdTickRecord: tuple(ColdTickRecord.model_fields),
    ColdHostRecord: tuple(ColdHostRecord.model_fields),
    ColdAdvisoryRecord: tuple(ColdAdvisoryRecord.model_fields),
    ColdFinalisationRecord: tuple(ColdFinalisationRecord.model_fields),
    ColdAbortRecord: tuple(ColdAbortRecord.model_fields),
}
_NESTED_CLASSES = tuple(_MODEL_FIELDS)


_ADMITTED_ENUM_TYPES = (
    ColdAudioField,
    ColdEvidenceFailure,
    ColdMcpAbortReason,
    ColdHostAbortReason,
    ColdIdentityAbortReason,
    ColdPhaseKind,
    ColdEvidenceStream,
    ColdEnvelopeKind,
    ColdCapabilityBranch,
    ColdFinalisationStatus,
    ColdSafetyVerdict,
    ColdAdvisorFailureKind,
    ColdAbortDomain,
    ColdOperatorAbortReason,
)


def _store_extracted(
    parent: dict[str, object] | list[object] | None,
    slot: str | None,
    value: object,
) -> object:
    """Attach one copied graph node without rendering its source value."""
    if parent is None:
        return value
    if type(parent) is dict and type(slot) is str:
        parent[slot] = value
    elif type(parent) is list and slot is None:
        parent.append(value)
    else:  # pragma: no cover - private task construction fixes both shapes.
        raise _closed(ColdEvidenceFailure.RECORD_NOT_VALIDATED)
    return value


def _extract_model(value: pydantic.BaseModel, aggregate: list[int]) -> dict[str, object]:
    """Iteratively copy a bounded admitted model graph before any adapter runs."""
    root: object | None = None
    nodes = 0
    stack: list[
        tuple[object, int, dict[str, object] | list[object] | None, str | None, str | None]
    ] = [(value, 0, None, None, None)]
    while stack:
        current, depth, parent, slot, field_name = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES:
            raise _closed(ColdEvidenceFailure.JSON_NODE_LIMIT_EXCEEDED)
        if depth > MAX_JSON_DEPTH:
            raise _closed(ColdEvidenceFailure.JSON_DEPTH_EXCEEDED)
        if type(current) in _NESTED_CLASSES:
            model = typing.cast(pydantic.BaseModel, current)
            expected = _MODEL_FIELDS[type(model)]
            data = object.__getattribute__(model, "__dict__")
            extra = object.__getattribute__(model, "__pydantic_extra__")
            if type(data) is not dict or (extra is not None and type(extra) is not dict):
                raise _closed(ColdEvidenceFailure.RECORD_NOT_VALIDATED)
            raw_data = typing.cast(dict[object, object], data)
            raw_extra = typing.cast(dict[object, object] | None, extra)
            if len(raw_data) != len(expected) or raw_extra:
                raise _closed(ColdEvidenceFailure.RECORD_NOT_VALIDATED)
            for name in expected:
                if name not in raw_data:
                    raise _closed(ColdEvidenceFailure.RECORD_NOT_VALIDATED)
                _admit_text(name, aggregate)
            if depth >= MAX_JSON_DEPTH:
                raise _closed(ColdEvidenceFailure.JSON_DEPTH_EXCEEDED)
            if nodes + len(stack) + len(expected) > MAX_JSON_NODES:
                raise _closed(ColdEvidenceFailure.JSON_NODE_LIMIT_EXCEEDED)
            copied_model: dict[str, object] = {}
            root = _store_extracted(parent, slot, copied_model) if parent is None else root
            if parent is not None:
                _store_extracted(parent, slot, copied_model)
            for name, _raw in raw_data.items():
                if type(name) is not str or name not in expected:
                    raise _closed(ColdEvidenceFailure.RECORD_NOT_VALIDATED)
            for name in reversed(expected):
                stack.append((raw_data[name], depth + 1, copied_model, name, name))
        elif type(current) is dict:
            mapping = typing.cast(dict[object, object], current)
            if len(mapping) > MAX_COLLECTION_LENGTH:
                raise _closed(ColdEvidenceFailure.JSON_NODE_LIMIT_EXCEEDED)
            if depth >= MAX_JSON_DEPTH:
                raise _closed(ColdEvidenceFailure.JSON_DEPTH_EXCEEDED)
            if nodes + len(stack) + len(mapping) > MAX_JSON_NODES:
                raise _closed(ColdEvidenceFailure.JSON_NODE_LIMIT_EXCEEDED)
            copied_map: dict[str, object] = {}
            root = _store_extracted(parent, slot, copied_map) if parent is None else root
            if parent is not None:
                _store_extracted(parent, slot, copied_map)
            map_items: list[tuple[str, object]] = []
            for key, child in mapping.items():
                if type(key) is not str:
                    raise _closed(ColdEvidenceFailure.JSON_KEY_INVALID)
                _admit_text(key, aggregate)
                if len(key.encode("utf-8")) > MAX_JSON_KEY_BYTES:
                    raise _closed(ColdEvidenceFailure.JSON_KEY_INVALID)
                map_items.append((key, child))
            for key, child in reversed(map_items):
                stack.append((child, depth + 1, copied_map, key, None))
        elif type(current) is list:
            items = typing.cast(list[object], current)
            if len(items) > MAX_COLLECTION_LENGTH:
                raise _closed(ColdEvidenceFailure.JSON_NODE_LIMIT_EXCEEDED)
            if depth >= MAX_JSON_DEPTH:
                raise _closed(ColdEvidenceFailure.JSON_DEPTH_EXCEEDED)
            if nodes + len(stack) + len(items) > MAX_JSON_NODES:
                raise _closed(ColdEvidenceFailure.JSON_NODE_LIMIT_EXCEEDED)
            copied_list: list[object] = []
            root = _store_extracted(parent, slot, copied_list) if parent is None else root
            if parent is not None:
                _store_extracted(parent, slot, copied_list)
            for child in reversed(items):
                stack.append((child, depth + 1, copied_list, None, None))
        elif type(current) is str:
            maximum = (
                MAX_TEXT_FIELD_BYTES
                if field_name in {"recorded_at_utc", "session_id", "rule", "reason"}
                else None
            )
            _admit_text(current, aggregate, maximum)
            root = _store_extracted(parent, slot, current) if parent is None else root
            if parent is not None:
                _store_extracted(parent, slot, current)
        elif current is None or type(current) is bool:
            aggregate[0] += 5
            root = _store_extracted(parent, slot, current) if parent is None else root
            if parent is not None:
                _store_extracted(parent, slot, current)
        elif type(current) is int:
            if current > 10**MAX_INT_DIGITS - 1 or current < -(10**MAX_INT_DIGITS - 1):
                raise _closed(ColdEvidenceFailure.RECORD_NOT_VALIDATED)
            aggregate[0] += MAX_INT_DIGITS + 1
            root = _store_extracted(parent, slot, current) if parent is None else root
            if parent is not None:
                _store_extracted(parent, slot, current)
        elif type(current) is float:
            if not math.isfinite(current):
                raise _closed(ColdEvidenceFailure.JSON_VALUE_NOT_FINITE)
            aggregate[0] += 32
            root = _store_extracted(parent, slot, current) if parent is None else root
            if parent is not None:
                _store_extracted(parent, slot, current)
        elif type(current) in _ADMITTED_ENUM_TYPES:
            root = _store_extracted(parent, slot, current) if parent is None else root
            if parent is not None:
                _store_extracted(parent, slot, current)
        elif isinstance(current, (collections.abc.Mapping, collections.abc.Sequence)):
            raise _closed(ColdEvidenceFailure.RECORD_NOT_VALIDATED)
        else:
            raise _closed(ColdEvidenceFailure.RECORD_NOT_VALIDATED)
        if aggregate[0] > MAX_INPUT_AGGREGATE_BYTES:
            raise _closed(ColdEvidenceFailure.RECORD_TOO_LARGE)
    if type(root) is not dict:
        raise _closed(ColdEvidenceFailure.RECORD_NOT_VALIDATED)
    return typing.cast(dict[str, object], root)


def _copy_json_value(value: ColdJsonValue) -> ColdJsonValue:
    """Fresh-copy an already walked JSON value without broad container admission."""
    if type(value) is dict:
        return {key: _copy_json_value(child) for key, child in value.items()}
    if type(value) is list:
        return [_copy_json_value(child) for child in value]
    return value


def _record_bytes(value: object, failure: ColdEvidenceFailure, maximum: int) -> None:
    """Apply one canonical-byte cap after its data graph was strictly validated."""
    if len(_canonical_json(value).encode("utf-8")) > maximum:
        raise _closed(failure)


def validate_record(record: ColdEvidenceRecord) -> ColdEvidenceRecord:
    """Strictly revalidate and snapshot one record crossing into persistence.

    Args:
        record: One exact in-process evidence record instance.

    Returns:
        A newly validated record snapshot.

    Raises:
        ColdEvidenceError: If any nested value, model shape, or size is invalid.
    """
    try:
        if type(record) not in _RECORD_CLASSES:
            raise _closed(ColdEvidenceFailure.RECORD_NOT_VALIDATED)
        payload = _extract_model(typing.cast(pydantic.BaseModel, record), [0])
        validated: ColdEvidenceRecord = _RECORD_ADAPTER.validate_python(payload, strict=True)
        dumped = validated.model_dump(mode="json")
        _record_bytes(dumped, ColdEvidenceFailure.RECORD_TOO_LARGE, MAX_RECORD_BYTES)
        if type(validated) is ColdTickRecord:
            _record_bytes(
                validated.raw_vendor_data,
                ColdEvidenceFailure.RECORD_VENDOR_BLOB_TOO_LARGE,
                MAX_VENDOR_BLOB_BYTES,
            )
            _record_bytes(
                validated.raw_audio_extra,
                ColdEvidenceFailure.RECORD_RAW_AUDIO_EXTRA_TOO_LARGE,
                MAX_RAW_AUDIO_EXTRA_BYTES,
            )
    except ColdEvidenceError as error:
        failure = error
    except Exception:
        failure = ColdEvidenceError(ColdEvidenceFailure.RECORD_NOT_VALIDATED)
    else:
        return validated
    raise failure
