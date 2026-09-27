"""Pure interpretation of one strictly read retained cold-characterisation run.

The public entry point rebinds a retained run into a token-checked capability,
then evaluates five closed per-phase checks and derives the D191 metrics.  It
performs no file, network or process I/O, holds no actuator, transport, advisor
or controller surface, and computes no run verdict: each check reports only
what it finds, and the D191 limits are declared for rendering, never compared.

Honest limit: rebinding re-establishes the record schema, the per-run bindings,
the v1 identity parse, header agreement and container consistency.  It cannot
prove that the manifest digest is authentic, that the run is complete, or that
the retained copies are physically independent.  Deleting or truncating
records, streams or phases cannot be detected here.  Those properties hold only
when slice-6 composition supplies the strict retained-run reader's output under
an externally recorded manifest digest and verifies both retained copies.

Qualification (Q1-Q11) is frozen v1 policy over retained values; it never
converts, folds or trims them.  It is not live-freeze parity: packaged agent,
model and manifest constants, and D188 profile applicability, belong to the
later applicability gate.  Temperatures are Celsius only; Q2 fails closed.
"""

import enum
import typing

import pydantic

from roastpilot_agent.cold_characterisation.evidence_schema import ColdPhaseKind

__all__ = (
    "D191_N_LIMIT",
    "D191_X_LIMIT_MS",
    "EFFECTIVE_HOP_SECONDS",
    "PRODUCTION_FATAL_STREAK",
    "QUALIFICATION_POLICY_VERSION",
    "ColdCheck",
    "ColdCheckFailure",
    "ColdCheckOutcome",
    "ColdCheckResult",
    "ColdD191Metrics",
    "ColdIdentityFacts",
    "ColdInterpretationError",
    "ColdInterpretationFailure",
    "ColdPhaseInterpretation",
)

#: The frozen qualification policy version; it covers identity schema version 1 only.
QUALIFICATION_POLICY_VERSION: typing.Final = 1
#: Locked D191 and production limits: declared for later rendering, never compared here.
D191_N_LIMIT: typing.Final = 1
D191_X_LIMIT_MS: typing.Final = 200.0
PRODUCTION_FATAL_STREAK: typing.Final = 30
#: The D183/D188 seven-second effective hop; used only by the inference-duration check.
EFFECTIVE_HOP_SECONDS: typing.Final = 7.0

_SHA256_RUST_PATTERN: typing.Final = r"\A[0-9a-f]{64}\z"
_SOURCE_COMMIT_RUST_PATTERN: typing.Final = r"\A[0-9a-f]{40}\z"


class ColdCheck(enum.Enum):
    """The five closed per-phase checks, in result order."""

    IDENTITY_QUALIFICATION_V1 = "identity_qualification_v1"
    INFERENCE_RUNTIME = "inference_runtime"
    AUDIO_COUNTERS = "audio_counters"
    INFERENCE_DURATION = "inference_duration"
    RECORDING_ARTEFACTS = "recording_artefacts"


class ColdCheckOutcome(enum.Enum):
    """One check's outcome: ``PASS`` exactly when it recorded no failure."""

    PASS = "pass"
    FAIL = "fail"


class ColdCheckFailure(enum.Enum):
    """Closed check failures; results list them uniquely in declaration order."""

    Q_SHAPE_UNEXPECTED = "q_shape_unexpected"
    Q_MCP_VERSION = "q_mcp_version"
    Q_TEMPERATURE_UNIT = "q_temperature_unit"
    Q_RECORDING_DEVICE_NOT_SINGLE = "q_recording_device_not_single"
    Q_INFERENCE_NOT_ACTIVE = "q_inference_not_active"
    Q_CREDENTIAL_NAME = "q_credential_name"
    Q_BOOT_ID = "q_boot_id"
    Q_OPERATOR_TEXT = "q_operator_text"
    Q_DEVICE_VALUE = "q_device_value"
    Q_PROVENANCE_DIGEST = "q_provenance_digest"
    Q_SOURCE_TREE_DIRTY = "q_source_tree_dirty"
    Q_PROFILE_VALUE = "q_profile_value"
    TICK_EVIDENCE_ABSENT = "tick_evidence_absent"
    PRE_FINALISATION_EVIDENCE_ABSENT = "pre_finalisation_evidence_absent"
    FINALISATION_EVIDENCE_ABSENT = "finalisation_evidence_absent"
    FINALISATION_SESSION_AMBIGUOUS = "finalisation_session_ambiguous"
    INFERENCE_NOT_ACTIVE = "inference_not_active"
    NO_PROCESSED_WINDOW = "no_processed_window"
    FIRST_CRACK_CONFIRMED = "first_crack_confirmed"
    MICROPHONE_OR_FATAL_ERROR = "microphone_or_fatal_error"
    POST_STOP_AUDIO_RUNNING = "post_stop_audio_running"
    MEASUREMENT_SHAPE_UNEXPECTED = "measurement_shape_unexpected"
    NEGATIVE_MEASUREMENT = "negative_measurement"
    DROPPED_WINDOW = "dropped_window"
    INFERENCE_OVERRUN = "inference_overrun"
    QUEUE_GROWING = "queue_growing"
    QUEUE_NOT_DRAINED = "queue_not_drained"
    CAPTURE_RESTART = "capture_restart"
    INFERENCE_DURATION_AT_OR_ABOVE_HOP = "inference_duration_at_or_above_hop"
    RECORDING_NOT_FINALISED = "recording_not_finalised"
    RECORDING_ARTEFACT_SET_UNEXPECTED = "recording_artefact_set_unexpected"
    RECORDING_ARTEFACT_EMPTY = "recording_artefact_empty"
    RECORDING_UNEXPECTEDLY_CONFIGURED = "recording_unexpectedly_configured"


class ColdInterpretationFailure(enum.Enum):
    """Closed failures for rebinding a retained run into a capability."""

    CONTAINER_MALFORMED = "container_malformed"
    RECORD_REBIND_FAILED = "record_rebind_failed"
    HEADER_SET_MISMATCHED = "header_set_mismatched"
    NO_PHASE_PRESENT = "no_phase_present"
    CAPABILITY_INVALID = "capability_invalid"


class ColdInterpretationError(RuntimeError):
    """Closed interpretation error carrying only its failure member and a fixed message."""

    failure: ColdInterpretationFailure

    def __init__(self, failure: ColdInterpretationFailure) -> None:
        """Create a content-free interpretation failure.

        Args:
            failure: Closed refusal reason.
        """
        super().__init__("Cold interpretation failed.")
        self.failure = failure


class ColdCheckResult(pydantic.BaseModel):
    """One check's closed result; ``PASS`` exactly when ``failures`` is empty."""

    model_config = pydantic.ConfigDict(
        frozen=True, extra="forbid", strict=True, allow_inf_nan=False
    )

    check: ColdCheck
    outcome: ColdCheckOutcome
    failures: tuple[ColdCheckFailure, ...]

    @pydantic.model_validator(mode="after")
    def _require_consistent_failures(self) -> typing.Self:
        """Require unique failures in declaration order and a matching outcome."""
        ordered = tuple(member for member in ColdCheckFailure if member in self.failures)
        outcome_is_pass = self.outcome is ColdCheckOutcome.PASS
        if self.failures != ordered or outcome_is_pass == bool(self.failures):
            raise ValueError("check failures and outcome disagree")
        return self


class ColdD191Metrics(pydantic.BaseModel):
    """Derived D191 N and peak trailing X; never compared with a limit here."""

    model_config = pydantic.ConfigDict(
        frozen=True, extra="forbid", strict=True, allow_inf_nan=False
    )

    max_consecutive_overflow_count: int = pydantic.Field(ge=0)
    peak_trailing_lost_audio_ms: float = pydantic.Field(ge=0)


class ColdIdentityFacts(pydantic.BaseModel):
    """Closed facts copied from a retained identity that passed every gate Q1-Q11."""

    model_config = pydantic.ConfigDict(
        frozen=True, extra="forbid", strict=True, allow_inf_nan=False
    )

    mcp_version: typing.Literal["0.2.1"]
    temperature_unit: typing.Literal["celsius"]
    first_crack_mode: typing.Literal["audio"]
    model_precision: typing.Literal["int8"]
    recording_device_count: typing.Literal[1]
    source_tree_dirty: typing.Literal[False]
    source_revision: str = pydantic.Field(pattern=_SOURCE_COMMIT_RUST_PATTERN)
    artefact_kind: typing.Literal["wheel", "sdist", "editable_source"]
    artefact_sha256: typing.Annotated[str, pydantic.Field(pattern=_SHA256_RUST_PATTERN)] | None
    profile_source_sha256: str = pydantic.Field(pattern=_SHA256_RUST_PATTERN)
    profile_source_byte_length: int = pydantic.Field(ge=0)
    first_crack_onnx_threads: int = pydantic.Field(ge=1)
    first_crack_min_positive_windows: int = pydantic.Field(ge=1)
    first_crack_confirmation_window_seconds: float = pydantic.Field(gt=0)
    audio_sample_rate: int = pydantic.Field(gt=0)
    audio_window_seconds: float = pydantic.Field(gt=0)
    audio_overlap: float = pydantic.Field(ge=0.0, lt=1.0)
    audio_hop_seconds: typing.Annotated[float, pydantic.Field(gt=0)] | None
    session_ror_window_seconds: int = pydantic.Field(gt=0)
    session_ror_min_sample_seconds: int = pydantic.Field(gt=0)


class ColdPhaseInterpretation(pydantic.BaseModel):
    """One phase's five check results in ``ColdCheck`` order, metrics and facts."""

    model_config = pydantic.ConfigDict(
        frozen=True, extra="forbid", strict=True, allow_inf_nan=False
    )

    phase: ColdPhaseKind
    identity_sha256: str = pydantic.Field(pattern=_SHA256_RUST_PATTERN)
    results: tuple[ColdCheckResult, ...]
    d191: ColdD191Metrics | None
    identity_facts: ColdIdentityFacts | None

    @pydantic.model_validator(mode="after")
    def _require_closed_results(self) -> typing.Self:
        """Require exactly the five checks and facts exactly when qualification passed."""
        if tuple(result.check for result in self.results) != tuple(ColdCheck):
            raise ValueError("phase results are not the closed check set")
        if (self.results[0].outcome is ColdCheckOutcome.PASS) != (self.identity_facts is not None):
            raise ValueError("identity facts disagree with the qualification result")
        return self
