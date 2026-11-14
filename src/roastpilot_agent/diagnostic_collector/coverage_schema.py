"""Closed synthetic coverage expectations, results, and report vocabulary."""

from enum import Enum
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from roastpilot_agent.diagnostic_collector.bundle_schema import ClockName, CollectorAuthority
from roastpilot_agent.diagnostic_collector.interpretation_schema import (
    MAX_COORDINATE,
    CheckStatus,
    ClockScale,
    EvidenceRef,
    ProvenanceClass,
)


def _exact_int(value: object) -> object:
    if type(value) is not int:
        raise ValueError("expected an exact integer")
    return value


def _exact_number(value: object) -> object:
    if type(value) not in (int, float):
        raise ValueError("expected an exact number")
    return value


def _exact_text(value: object) -> object:
    if type(value) is not str:
        raise ValueError("expected exact text")
    return value


_Count = Annotated[int, BeforeValidator(_exact_int), Field(ge=0)]
_Coordinate = Annotated[_Count, Field(le=MAX_COORDINATE)]
_PositiveSamples = Annotated[_Coordinate, Field(gt=0)]
_SampleRate = Annotated[_Count, Field(gt=0, le=2**31 - 1)]
_Token = Annotated[
    str, BeforeValidator(_exact_text), StringConstraints(pattern=r"\A[A-Za-z0-9_-]{1,64}\z")
]
_Digest = Annotated[
    str, BeforeValidator(_exact_text), StringConstraints(pattern=r"\A[0-9a-f]{64}\z")
]


class _ClosedModel(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", allow_inf_nan=False, strict=True, revalidate_instances="always"
    )

    @model_validator(mode="before")
    @classmethod
    def _rebuild_fields(cls, value: object) -> object:
        if isinstance(value, BaseModel):
            return {**vars(value), **(value.model_extra or {})}
        return value


class DetectorSampleAnchor(_ClosedModel):
    """An independently bound projection anchor for the detector stream."""

    seconds_clock: ClockName
    seconds_value: Annotated[
        int | float, BeforeValidator(_exact_number), Field(ge=0, le=MAX_COORDINATE)
    ]
    sample_value: _Coordinate

    @model_validator(mode="after")
    def _seconds_clock_only(self) -> Self:
        if self.seconds_clock is ClockName.RECORDING_SAMPLES:
            raise ValueError("expected a seconds clock")
        return self


class DetectorLatticeBinding(_ClosedModel):
    """Detector sample coordinates and exact hop for a synthetic interval."""

    sample_rate_hz: _SampleRate
    origin_sample: _Coordinate
    charge_sample: _Coordinate
    drop_sample: _Coordinate
    hop_samples: _PositiveSamples
    length_samples: _PositiveSamples
    anchor: DetectorSampleAnchor | None = None

    @model_validator(mode="after")
    def _ordered_interval(self) -> Self:
        if self.charge_sample >= self.drop_sample:
            raise ValueError("expected charge before drop")
        return self


class RecordingBinding(_ClosedModel):
    """Required stored sample offsets for one independently bound recording."""

    recording_id: _Token
    sample_rate_hz: _SampleRate
    required_first_sample: _Coordinate
    required_end_sample: _Coordinate

    @model_validator(mode="after")
    def _ordered_interval(self) -> Self:
        if self.required_first_sample >= self.required_end_sample:
            raise ValueError("expected a nonempty half-open recording interval")
        return self


class CoverageExpectations(_ClosedModel):
    """Caller-bound identity and isolated synthetic coverage expectations."""

    session_id: _Token
    bundle_id: _Token
    clock_scales: Annotated[tuple[ClockScale, ...], Field(max_length=3)] = ()
    detector: DetectorLatticeBinding | None = None
    recording: RecordingBinding | None = None

    @model_validator(mode="after")
    def _unique_clock_pairs(self) -> Self:
        pairs = {frozenset((scale.source_clock, scale.target_clock)) for scale in self.clock_scales}
        if len(pairs) != len(self.clock_scales):
            raise ValueError("expected one scale per unordered clock pair")
        return self


class CoverageCheckName(Enum):
    """Synthetic coverage checks in report order."""

    LATTICE_SCORE_COVERAGE = "lattice_score_coverage"
    DETECTOR_CAPTURE_CONTINUITY = "detector_capture_continuity"
    RECORDING_EXTENT_COVERAGE = "recording_extent_coverage"
    RECORDING_CAPTURE_CONTINUITY = "recording_capture_continuity"
    OBSERVED_PROVENANCE = "observed_provenance"


class CoverageReason(Enum):
    """Closed reasons for unresolved or invalid synthetic coverage checks."""

    SCOPE_MISMATCH = "scope_mismatch"
    EVIDENCE_MALFORMED = "evidence_malformed"
    OFF_LATTICE = "off_lattice"
    CONFLICT = "conflict"
    ORDER_CONFLICT = "order_conflict"
    SLOTS_MISSED = "slots_missed"
    GAP = "gap"
    EXPECTATION_UNBOUND = "expectation_unbound"
    EVIDENCE_ABSENT = "evidence_absent"
    INSTANT_OUT_OF_DOMAIN = "instant_out_of_domain"
    CLOCK_ALIGNMENT_UNRESOLVED = "clock_alignment_unresolved"
    CLOCK_PATH_CONFLICT = "clock_path_conflict"
    EMPTY_DENOMINATOR = "empty_denominator"
    PRODUCER_WITNESS_ABSENT = "producer_witness_absent"
    SYNTHETIC_EVIDENCE = "synthetic_evidence"


COVERAGE_REASON_STATUS = {
    CoverageReason.SCOPE_MISMATCH: CheckStatus.INVALID,
    CoverageReason.EVIDENCE_MALFORMED: CheckStatus.INVALID,
    CoverageReason.OFF_LATTICE: CheckStatus.INVALID,
    CoverageReason.CONFLICT: CheckStatus.INVALID,
    CoverageReason.ORDER_CONFLICT: CheckStatus.INVALID,
    CoverageReason.SLOTS_MISSED: CheckStatus.INVALID,
    CoverageReason.GAP: CheckStatus.INVALID,
    CoverageReason.EXPECTATION_UNBOUND: CheckStatus.UNRESOLVED,
    CoverageReason.EVIDENCE_ABSENT: CheckStatus.UNRESOLVED,
    CoverageReason.INSTANT_OUT_OF_DOMAIN: CheckStatus.UNRESOLVED,
    CoverageReason.CLOCK_ALIGNMENT_UNRESOLVED: CheckStatus.UNRESOLVED,
    CoverageReason.CLOCK_PATH_CONFLICT: CheckStatus.UNRESOLVED,
    CoverageReason.EMPTY_DENOMINATOR: CheckStatus.UNRESOLVED,
    CoverageReason.PRODUCER_WITNESS_ABSENT: CheckStatus.UNRESOLVED,
    CoverageReason.SYNTHETIC_EVIDENCE: CheckStatus.UNRESOLVED,
}


class CoverageAdapterLimit(Enum):
    """Evidence requirements for later producers and qualification."""

    DETECTOR_SAMPLE_CLOCK_BINDING = "detector_sample_clock_binding"
    DETECTOR_CAPTURE_CONTINUITY_WITNESS = "detector_capture_continuity_witness"
    PUBLISHED_WINDOW_TIMESTAMP_CHRONOLOGY = "published_window_timestamp_chronology"
    PRODUCER_UNSCORED_ELIGIBLE_ROWS = "producer_unscored_eligible_rows"
    RECORDING_STREAM_IDENTITY = "recording_stream_identity"
    RECORDING_CAPTURE_WITNESS = "recording_capture_witness"
    OBSERVED_PROVENANCE = "observed_provenance"
    CLOCK_RATE_AND_DRIFT = "clock_rate_and_drift"


class CoverageReportName(Enum):
    """The report's discriminator."""

    COVERAGE = "coverage"


class LatticeCoverageDetail(_ClosedModel):
    """Counts of eligible synthetic slots and admitted window rows."""

    expected_slots: _Coordinate
    scored_slots: _Count
    errored_slots: _Count
    missed_slots: _Count
    duplicate_rows: _Count
    ineligible_rows: _Count

    @model_validator(mode="after")
    def _consistent_counts(self) -> Self:
        if (
            self.scored_slots + self.missed_slots != self.expected_slots
            or self.errored_slots > self.missed_slots
        ):
            raise ValueError("expected consistent lattice counts")
        return self


class RecordingExtentDetail(_ClosedModel):
    """Counts of represented stored sample offsets and segment observations."""

    required_samples: _Count
    covered_samples: _Count
    uncovered_samples: _Count
    selected_rows: _Count
    foreign_rows: _Count
    exact_duplicate_rows: _Count
    rate_mismatch_rows: _Count
    contained_spans: _Count
    overlapping_spans: _Count

    @model_validator(mode="after")
    def _consistent_counts(self) -> Self:
        if self.covered_samples + self.uncovered_samples != self.required_samples:
            raise ValueError("expected consistent recording counts")
        return self


class CoverageCheckResult(_ClosedModel):
    """A precise result with its original, dependency-local evidence."""

    check: CoverageCheckName
    status: CheckStatus
    reason: CoverageReason | None
    evidence: Annotated[tuple[EvidenceRef, ...], Field(max_length=8192)]
    detail: LatticeCoverageDetail | RecordingExtentDetail | None

    @model_validator(mode="after")
    def _closed_result(self) -> Self:
        status = (
            CheckStatus.SATISFIED if self.reason is None else COVERAGE_REASON_STATUS[self.reason]
        )
        if self.status is not status:
            raise ValueError("reason does not match check status")
        expected_detail: type[object] = type(None)
        if self.check is CoverageCheckName.LATTICE_SCORE_COVERAGE and self.reason in (
            None,
            CoverageReason.SLOTS_MISSED,
            CoverageReason.EMPTY_DENOMINATOR,
        ):
            expected_detail = LatticeCoverageDetail
        if self.check is CoverageCheckName.RECORDING_EXTENT_COVERAGE and self.reason in (
            None,
            CoverageReason.CONFLICT,
            CoverageReason.GAP,
        ):
            expected_detail = RecordingExtentDetail
        if type(self.detail) is not expected_detail:
            raise ValueError("detail does not match check and reason")
        if (
            self.reason is CoverageReason.EMPTY_DENOMINATOR
            and isinstance(self.detail, LatticeCoverageDetail)
            and self.detail.expected_slots != 0
        ):
            raise ValueError("expected an empty denominator")
        return self


class CoverageReport(_ClosedModel):
    """Versioned synthetic coverage evidence with no operational authority."""

    report_schema_version: Literal[1] = 1
    report_name: Literal[CoverageReportName.COVERAGE] = CoverageReportName.COVERAGE
    bundle_sha256: _Digest
    authority: Literal[CollectorAuthority.NONE] = CollectorAuthority.NONE
    provenance: Literal[ProvenanceClass.SYNTHETIC_UNATTESTED] = ProvenanceClass.SYNTHETIC_UNATTESTED
    adapter_limits: tuple[CoverageAdapterLimit, ...] = tuple(CoverageAdapterLimit)
    checks: tuple[CoverageCheckResult, ...]

    @model_validator(mode="after")
    def _closed_report(self) -> Self:
        if self.adapter_limits != tuple(CoverageAdapterLimit):
            raise ValueError("expected the full coverage adapter-limit inventory")
        if tuple(result.check for result in self.checks) != tuple(CoverageCheckName):
            raise ValueError("expected one coverage result per check in schema order")
        return self
