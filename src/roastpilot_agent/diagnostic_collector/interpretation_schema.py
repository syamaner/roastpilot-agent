"""Closed expectations and report vocabulary for synthetic interpretation v1."""

from enum import Enum
from math import isfinite
from typing import Annotated, Literal, Self, cast

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    StringConstraints,
    field_validator,
    model_validator,
)

from roastpilot_agent.diagnostic_collector.bundle_schema import (
    ClockName,
    CollectorAuthority,
    CollectorStream,
    Instant,
)

MAX_COORDINATE = 2**53 - 1


class InterpretationInputReason(Enum):
    """Closed reasons for rejecting interpretation arguments before admission."""

    DATA_NOT_BYTES = "data_not_bytes"
    EXPECTATIONS_INVALID = "expectations_invalid"


class InterpretationInputError(ValueError):
    """A pre-admission rejection with a fixed, source-free message."""

    def __init__(self, reason: InterpretationInputReason) -> None:
        super().__init__("synthetic collector interpretation input refused")
        self.reason = reason


class CheckName(Enum):
    """Version 1 checks in report order."""

    IDENTITY = "identity"
    TEMPERATURE_PLAUSIBILITY = "temperature_plausibility"
    TEMPERATURE_AGE = "temperature_age"
    ROOM_AGE = "room_age"
    ROOM_PLACEMENT = "room_placement"
    HUMAN_FC = "human_fc"
    SHADOW_TIMING = "shadow_timing"
    SHADOW_CONFIRMATION = "shadow_confirmation"
    OBSERVED_PROVENANCE = "observed_provenance"


class CheckStatus(Enum):
    """The result of one check over synthetic evidence."""

    SATISFIED = "satisfied"
    UNRESOLVED = "unresolved"
    INVALID = "invalid"


class CheckReason(Enum):
    """Closed reasons for unresolved or invalid checks."""

    SCOPE_MISMATCH = "scope_mismatch"
    EVIDENCE_MALFORMED = "evidence_malformed"
    EXPECTATION_UNBOUND = "expectation_unbound"
    EVIDENCE_ABSENT = "evidence_absent"
    DEPENDENCY_NOT_SATISFIED = "dependency_not_satisfied"
    CONFLICT = "conflict"
    EXPECTATION_MISMATCH = "expectation_mismatch"
    INSTANT_OUT_OF_DOMAIN = "instant_out_of_domain"
    CLOCK_ALIGNMENT_UNRESOLVED = "clock_alignment_unresolved"
    CLOCK_PATH_CONFLICT = "clock_path_conflict"
    ORDER_CONFLICT = "order_conflict"
    IMPLAUSIBLE = "implausible"
    STALE = "stale"
    PLACEMENT_ABSENT = "placement_absent"
    SYNTHETIC_EVIDENCE = "synthetic_evidence"


REASON_STATUS = {
    CheckReason.SCOPE_MISMATCH: CheckStatus.INVALID,
    CheckReason.EVIDENCE_MALFORMED: CheckStatus.INVALID,
    CheckReason.EXPECTATION_UNBOUND: CheckStatus.UNRESOLVED,
    CheckReason.EVIDENCE_ABSENT: CheckStatus.UNRESOLVED,
    CheckReason.DEPENDENCY_NOT_SATISFIED: CheckStatus.UNRESOLVED,
    CheckReason.CONFLICT: CheckStatus.INVALID,
    CheckReason.EXPECTATION_MISMATCH: CheckStatus.INVALID,
    CheckReason.INSTANT_OUT_OF_DOMAIN: CheckStatus.UNRESOLVED,
    CheckReason.CLOCK_ALIGNMENT_UNRESOLVED: CheckStatus.UNRESOLVED,
    CheckReason.CLOCK_PATH_CONFLICT: CheckStatus.UNRESOLVED,
    CheckReason.ORDER_CONFLICT: CheckStatus.INVALID,
    CheckReason.IMPLAUSIBLE: CheckStatus.INVALID,
    CheckReason.STALE: CheckStatus.INVALID,
    CheckReason.PLACEMENT_ABSENT: CheckStatus.UNRESOLVED,
    CheckReason.SYNTHETIC_EVIDENCE: CheckStatus.UNRESOLVED,
}


class ProvenanceClass(Enum):
    """The sole provenance available to the synthetic interpreter."""

    SYNTHETIC_UNATTESTED = "synthetic_unattested"


class AdapterLimit(Enum):
    """Facts reserved for producer adapters and live qualification."""

    TEMPERATURE_MEMBER_ACQUISITION_INSTANT = "temperature_member_acquisition_instant"
    ROOM_PRODUCER_SEMANTICS_199 = "room_producer_semantics_199"
    SHADOW_CHARGE_BOUNDARY = "shadow_charge_boundary"
    RECORDING_CAPTURE_CHRONOLOGY = "recording_capture_chronology"
    COMMAND_DISPATCH_INSTANT = "command_dispatch_instant"
    PERSISTENCE_POLICY_ORIGIN_AND_COMMIT_INSTANT = "persistence_policy_origin_and_commit_instant"
    OPERATOR_OBSERVATION_ATTRIBUTION = "operator_observation_attribution"
    OBSERVED_PROVENANCE = "observed_provenance"
    CLOCK_RATE_AND_DRIFT = "clock_rate_and_drift"


def _number(value: object) -> object:
    """Check a finite binary float or exact integer before Pydantic coercion."""

    if type(value) is int or (type(value) is float and isfinite(value)):
        return value
    raise ValueError("expected an integer or finite binary float")


def _text(value: object) -> object:
    """Reject text subclasses and scalar coercion at the expectation boundary."""

    if type(value) is not str:
        raise ValueError("expected exact text")
    return value


def _raw_fields(value: object) -> object:
    """Rebuild models using raw fields and retain any extras for rejection."""

    if isinstance(value, BaseModel):
        return {**vars(value), **(value.model_extra or {})}
    return value


def _expected_instant(value: object) -> Instant:
    """Rebuild the S1 instant under the interpreter's stricter coordinate rules."""

    raw = _raw_fields(value)
    if not isinstance(raw, dict) or raw.keys() != {"clock", "value"}:
        raise ValueError("expected closed instant fields")
    fields = cast(dict[str, object], raw)
    clock = fields["clock"]
    coordinate = fields["value"]
    if not isinstance(clock, ClockName):
        raise ValueError("expected a typed clock")
    _number(coordinate)
    if not isinstance(coordinate, int | float) or not 0 <= coordinate <= MAX_COORDINATE:
        raise ValueError("expected a bounded coordinate")
    if clock is ClockName.RECORDING_SAMPLES and type(coordinate) is not int:
        raise ValueError("expected an integral sample coordinate")
    return Instant(clock=clock, value=coordinate)


_Number = Annotated[int | float, BeforeValidator(_number)]
_Token = Annotated[
    str, StringConstraints(pattern=r"\A[A-Za-z0-9_-]{1,64}\z"), BeforeValidator(_text)
]
_Digest = Annotated[str, StringConstraints(pattern=r"\A[0-9a-f]{64}\z"), BeforeValidator(_text)]


class _FrozenModel(BaseModel):
    """Shared configuration for closed, immutable v1 models."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False, strict=True)


class _ExpectationModel(_FrozenModel):
    """Revalidate the raw fields of any supplied expectation model."""

    model_config = ConfigDict(revalidate_instances="always")

    @model_validator(mode="before")
    @classmethod
    def _rebuild_fields(cls, value: object) -> object:
        return _raw_fields(value)


class ComponentIdentity(_ExpectationModel):
    """Independently bound digests and stream identity."""

    agent_sha256: _Digest
    mcp_sha256: _Digest
    sdk_sha256: _Digest
    configuration_sha256: _Digest
    stream_id: _Token


class CelsiusRange(_ExpectationModel):
    """Inclusive bounds in degrees Celsius."""

    low_c: _Number
    high_c: _Number

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.low_c > self.high_c:
            raise ValueError("expected ordered Celsius bounds")
        return self


class ClockScale(_ExpectationModel):
    """Independently bound directed scale and source-coordinate validity."""

    source_clock: ClockName
    target_clock: ClockName
    target_per_source: _Number
    valid_from: _Number
    valid_to: _Number

    @model_validator(mode="after")
    def _validate_scale(self) -> Self:
        if (
            self.source_clock is self.target_clock
            or ClockName.RECORDING_SAMPLES in (self.source_clock, self.target_clock)
            or not 2**-32 <= self.target_per_source <= 2**32
            or not 0 <= self.valid_from <= self.valid_to <= MAX_COORDINATE
        ):
            raise ValueError("expected a bounded directed seconds scale")
        return self


class SyntheticExpectations(_ExpectationModel):
    """Caller-bound synthetic identity, thresholds, and clock expectations."""

    session_id: _Token
    bundle_id: _Token
    component: ComponentIdentity | None = None
    evaluation_at: Annotated[Instant, BeforeValidator(_expected_instant)] | None = None
    max_temperature_age_seconds: _Number | None = None
    bt_range_c: CelsiusRange | None = None
    et_range_c: CelsiusRange | None = None
    room_range_c: CelsiusRange | None = None
    max_room_age_seconds: _Number | None = None
    placement_id: _Token | None = None
    recording_sample_rate_hz: int | None = None
    clock_scales: tuple[ClockScale, ...] = ()

    @field_validator("max_temperature_age_seconds", "max_room_age_seconds")
    @classmethod
    def _nonnegative_age(cls, value: int | float | None) -> int | float | None:
        if value is not None and value < 0:
            raise ValueError("expected a nonnegative age bound")
        return value

    @field_validator("recording_sample_rate_hz", mode="before")
    @classmethod
    def _sample_rate(cls, value: object) -> object:
        if value is not None and (type(value) is not int or not 1 <= value <= 2**31 - 1):
            raise ValueError("expected a bounded integer sample rate")
        return value

    @field_validator("clock_scales", mode="before")
    @classmethod
    def _clock_scale_count(cls, value: object) -> object:
        if value is None:
            return ()
        if not isinstance(value, tuple):
            raise ValueError("expected a tuple of clock scales")
        scales = cast(tuple[object, ...], value)
        if len(scales) > 3:
            raise ValueError("expected at most three clock scales")
        return scales

    @field_validator("clock_scales")
    @classmethod
    def _unique_clock_scales(cls, value: tuple[ClockScale, ...]) -> tuple[ClockScale, ...]:
        if len({frozenset((scale.source_clock, scale.target_clock)) for scale in value}) != len(
            value
        ):
            raise ValueError("expected one scale per unordered clock pair")
        return value


class EvidenceRef(_FrozenModel):
    """Reference to an admitted stream record and its original instants."""

    stream: CollectorStream
    seq: int
    instants: tuple[Instant, ...]


class CheckResult(_FrozenModel):
    """One check's closed result and original evidence references."""

    check: CheckName
    status: CheckStatus
    reason: CheckReason | None
    evidence: tuple[EvidenceRef, ...]
    derived_seconds: float | None

    @model_validator(mode="after")
    def _reason_matches_status(self) -> Self:
        required = CheckStatus.SATISFIED if self.reason is None else REASON_STATUS[self.reason]
        if self.status is not required:
            raise ValueError("reason does not match check status")
        return self


class InterpretationReport(_FrozenModel):
    """Versioned synthetic check results with no authority."""

    report_schema_version: Literal[1] = 1
    bundle_sha256: _Digest
    authority: Literal[CollectorAuthority.NONE] = CollectorAuthority.NONE
    provenance: Literal[ProvenanceClass.SYNTHETIC_UNATTESTED] = ProvenanceClass.SYNTHETIC_UNATTESTED
    adapter_limits: tuple[AdapterLimit, ...] = tuple(AdapterLimit)
    checks: tuple[CheckResult, ...]

    @model_validator(mode="after")
    def _closed_report(self) -> Self:
        if self.adapter_limits != tuple(AdapterLimit):
            raise ValueError("expected the full adapter-limit inventory")
        if tuple(result.check for result in self.checks) != tuple(CheckName):
            raise ValueError("expected one result per check in schema order")
        return self
