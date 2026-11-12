"""Closed synthetic diagnostic-collector v1 data vocabulary.

The models in this module describe admitted evidence only.  They make no
freshness, qualification, physical-effect, or command-authority claim.
"""

import enum
import typing

import pydantic

_MODEL_CONFIG = pydantic.ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class CollectorStream(enum.Enum):
    """The closed v1 synthetic evidence stream vocabulary."""

    IDENTITY = "identity"
    TEMPERATURE_TURN = "temperature_turn"
    ROOM_READING = "room_reading"
    HUMAN_FC = "human_fc"
    SHADOW_EVENT = "shadow_event"
    SHADOW_WINDOW = "shadow_window"
    INFERENCE_CONFIG = "inference_config"
    RECORDING_SEGMENT = "recording_segment"
    COMMAND = "command"
    PERSISTENCE_ACK = "persistence_ack"
    OPERATOR_OBSERVATION = "operator_observation"
    ENDPOINT = "endpoint"


class CollectorAuthority(enum.Enum):
    """The sole authority value S1 may expose."""

    NONE = "none"


class CollectorAdmissionReason(enum.Enum):
    """Closed whole-bundle refusal reasons in their validation order."""

    BUNDLE_SIZE = "bundle_size"
    EXPECTED_DIGEST_INVALID = "expected_digest_invalid"
    DIGEST_MISMATCH = "digest_mismatch"
    FRAMING = "framing"
    RECORD_LIMIT = "record_limit"
    LINE_LIMIT = "line_limit"
    NESTING_LIMIT = "nesting_limit"
    JSON_INVALID = "json_invalid"
    STRING_LIMIT = "string_limit"
    NON_CANONICAL = "non_canonical"
    HEADER_INVALID = "header_invalid"
    ENVELOPE_INVALID = "envelope_invalid"
    INVENTORY_MISMATCH = "inventory_mismatch"


class CollectorStreamState(enum.Enum):
    """Structural admission state for one stream."""

    ABSENT = "absent"
    ADMITTED = "admitted"
    MALFORMED = "malformed"


class CollectorStreamReason(enum.Enum):
    """Closed isolated-stream malformed reasons."""

    PAYLOAD_INVALID = "payload_invalid"
    SEQUENCE_CONFLICT = "sequence_conflict"


class ClockName(enum.Enum):
    """Known coordinates retained without comparison or conversion."""

    AGENT_MONOTONIC_SECONDS = "agent_monotonic_seconds"
    MCP_SESSION_SECONDS = "mcp_session_seconds"
    RECORDING_SAMPLES = "recording_samples"
    UTC_EPOCH_SECONDS = "utc_epoch_seconds"


class CollectorAdmissionError(RuntimeError):
    """A whole-bundle refusal exposing exactly one closed reason."""

    def __init__(self, reason: CollectorAdmissionReason) -> None:
        super().__init__("synthetic collector bundle refused")
        self.reason = reason


class _CollectorModel(pydantic.BaseModel):
    """Shared immutable model configuration for the closed schema."""

    model_config = _MODEL_CONFIG


class Instant(_CollectorModel):
    """One raw coordinate retained in its source clock."""

    clock: ClockName
    value: int | float


class AlignmentAnchor(_CollectorModel):
    """One non-comparative raw alignment coordinate pair."""

    source_clock: ClockName
    source_value: int | float
    target_clock: ClockName
    target_value: int | float


class BundleHeader(_CollectorModel):
    """The synthetic v1 bundle identity and declared stream inventory."""

    schema_version: typing.Literal[1]
    evidence_class: typing.Literal["synthetic"]
    bundle_id: str
    session_id: str
    inventory: tuple["StreamInventory", ...]
    alignments: tuple[AlignmentAnchor, ...]


class IdentityPayload(_CollectorModel):
    """Digest identity for one admitted collector stream."""

    agent_sha256: str
    mcp_sha256: str
    sdk_sha256: str
    configuration_sha256: str
    stream_id: str


class StreamInventory(_CollectorModel):
    """One immutable declared stream count from the header inventory object."""

    stream: CollectorStream
    count: int


class TemperatureTurnPayload(_CollectorModel):
    """One observed temperature turn without interpretation."""

    turn_id: str
    observed_at: Instant
    bt_c: int | float | None
    et_c: int | float | None
    status: typing.Literal["accepted", "invalid"]


class RoomReadingPayload(_CollectorModel):
    """One room reading and its structural status."""

    observed_at: Instant
    temperature_c: int | float | None
    placement_id: str | None
    status: typing.Literal["valid", "invalid"]


class HumanFirstCrackPayload(_CollectorModel):
    """A human-accepted first-crack record."""

    event_id: str
    accepted_at: Instant


class ShadowEventPayload(_CollectorModel):
    """A shadow onset and decision record, separate from human first crack."""

    event_id: str
    onset_at: Instant
    decision_at: Instant
    confirming_window_seq: int


class ShadowWindowPayload(_CollectorModel):
    """One shadow inference window with its retained local classification."""

    start_at: Instant
    end_at: Instant
    decision_at: Instant | None
    eligibility: typing.Literal["eligible", "pre_charge", "post_drop"]
    status: typing.Literal["negative", "candidate", "confirmed", "error", "not_inferred"]
    probability: int | float | None


class InferenceConfigPayload(_CollectorModel):
    """Synthetic inference timing configuration."""

    effective_hop_seconds: int | float
    window_seconds: int | float


class RecordingSegmentPayload(_CollectorModel):
    """One recording segment expressed in recording samples."""

    recording_id: str
    first_sample: int
    sample_count: int
    sample_rate_hz: int


class CommandPayload(_CollectorModel):
    """A retained command-state observation, not an executable command."""

    command_id: str
    owner_id: str
    generation: int
    observed_at: Instant
    disposition: typing.Literal["not_dispatched", "returned_command_state", "unknown", "superseded"]
    commanded_heat_percent: int | float | None


class PersistenceAckPayload(_CollectorModel):
    """One retained command persistence acknowledgement."""

    command_id: str
    owner_id: str
    generation: int
    observed_at: Instant
    status: typing.Literal["acknowledged", "unavailable", "unknown"]


class OperatorObservationPayload(_CollectorModel):
    """One explicit operator observation without inferred effect."""

    observation_id: str
    observed_at: Instant
    effect: typing.Literal["heat_off", "beans_evacuated", "cooling_running", "unknown"]


class EndpointPayload(_CollectorModel):
    """One retained endpoint outcome."""

    endpoint_id: str
    accepted_at: Instant
    outcome: typing.Literal[
        "recipe_target_met", "human_protection_interrupted", "other_interruption", "unknown"
    ]


CollectorPayload: typing.TypeAlias = (
    IdentityPayload
    | TemperatureTurnPayload
    | RoomReadingPayload
    | HumanFirstCrackPayload
    | ShadowEventPayload
    | ShadowWindowPayload
    | InferenceConfigPayload
    | RecordingSegmentPayload
    | CommandPayload
    | PersistenceAckPayload
    | OperatorObservationPayload
    | EndpointPayload
)


class CollectorEnvelope(_CollectorModel):
    """One typed admitted envelope retaining its original stream and sequence."""

    schema_version: typing.Literal[1]
    stream: CollectorStream
    session_id: str
    seq: int
    payload: CollectorPayload


class CollectorStreamEntry(_CollectorModel):
    """The isolated admission result for one stream in closed stream order."""

    stream: CollectorStream
    state: CollectorStreamState
    reason: CollectorStreamReason | None
    records: tuple[CollectorEnvelope, ...]


class AdmittedBundle(_CollectorModel):
    """A digest-matched synthetic bundle with no qualification or live authority."""

    bundle_sha256: str
    header: BundleHeader
    streams: tuple[CollectorStreamEntry, ...]
    authority: typing.Literal[CollectorAuthority.NONE]
