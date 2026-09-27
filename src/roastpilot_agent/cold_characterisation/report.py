"""Sanitised, allow-listed report over one interpreted cold-characterisation run.

The builder takes only a retained run, calls ``interpret_retained_run`` exactly
once, and projects the returned interpretation and its rebound capability into a
closed report schema.  Every string leaf is an enum member, a literal, or a
lowercase hexadecimal digest under an anchored pattern; the run and session
identifiers appear only as tagged SHA-256 digests.  Each report field is built
from one typed, allow-listed source: evidence is never dumped and then redacted.
The renderer builds that report once and emits canonical JSON bytes, re-parsed to
prove every object's key set, and a Markdown summary whose fixed templates walk
only the closed report model.

The module is pure.  It performs no file, network or process I/O and never calls
the retained-run reader, the evidence writer or the tree verifier.  It computes no
run verdict and compares no measurement with a limit: the locked D191 and
production limits are rendered, labelled not compared, and per-check results are
rendered as each check defines them.

Honest limit: the manifest digest is carried from the supplied run and is not
re-verified here, and nothing here establishes the run's completeness, provenance
or independent storage.  MCP-reported finalisation fields are recorded values, not
evaluated here.  Temperatures are Celsius only.
"""

import enum
import typing

import pydantic

from roastpilot_agent.cold_characterisation.acceptance import (
    D191_N_LIMIT,
    D191_X_LIMIT_MS,
    EFFECTIVE_HOP_SECONDS,
    PRODUCTION_FATAL_STREAK,
    ColdCheck,
    ColdCheckFailure,
    ColdCheckOutcome,
    ColdD191Metrics,
    ColdIdentityFacts,
)
from roastpilot_agent.cold_characterisation.evidence_reader import ABORT_REASON_BY_DOMAIN
from roastpilot_agent.cold_characterisation.evidence_schema import (
    ColdAbortDomain,
    ColdAdvisorFailureKind,
    ColdCapabilityBranch,
    ColdEvidenceFailure,
    ColdFinalisationStatus,
    ColdHostAbortReason,
    ColdIdentityAbortReason,
    ColdMcpAbortReason,
    ColdOperatorAbortReason,
    ColdPhaseKind,
)

__all__ = (
    "ColdReportAbort",
    "ColdReportAdvisorFailureCount",
    "ColdReportCheck",
    "ColdReportCounters",
    "ColdReportError",
    "ColdReportFailure",
    "ColdReportHostExtremes",
    "ColdReportLockedLimits",
    "ColdReportPhase",
    "ColdReportRecordingArtefact",
    "ColdSanitisedReport",
)

_SHA256_RUST_PATTERN: typing.Final = r"\A[0-9a-f]{64}\z"
#: Rendered once, labelled not compared; the pin below is their only comparison.
_LOCKED_LIMIT_VALUES: typing.Final = (
    D191_N_LIMIT,
    D191_X_LIMIT_MS,
    PRODUCTION_FATAL_STREAK,
    EFFECTIVE_HOP_SECONDS,
)

_Digest: typing.TypeAlias = typing.Annotated[str, pydantic.Field(pattern=_SHA256_RUST_PATTERN)]
_Count: typing.TypeAlias = typing.Annotated[int, pydantic.Field(ge=0)]
_Measure: typing.TypeAlias = typing.Annotated[float, pydantic.Field(ge=0)]


class ColdReportFailure(enum.Enum):
    """Closed failures for building or rendering a sanitised report."""

    REBIND_FAILED = "rebind_failed"
    VALUE_NOT_ADMITTED = "value_not_admitted"
    EGRESS_KEYSET_MISMATCH = "egress_keyset_mismatch"


class ColdReportError(RuntimeError):
    """Closed report error carrying only its failure member and a fixed message."""

    failure: ColdReportFailure

    def __init__(self, failure: ColdReportFailure) -> None:
        """Create a content-free report failure.

        Args:
            failure: Closed refusal reason.
        """
        super().__init__("Cold report failed.")
        self.failure = failure


class _ReportModel(pydantic.BaseModel):
    """Frozen, closed, strict and finite: the configuration of every report model."""

    model_config = pydantic.ConfigDict(
        frozen=True, extra="forbid", strict=True, allow_inf_nan=False
    )


class ColdReportLockedLimits(_ReportModel):
    """The locked D191 and production limits, pinned to the acceptance constants.

    They are shown beside the derived values and are never compared with them.
    """

    label: typing.Literal["not compared"]
    n: int
    x_ms: float
    fatal_streak: int
    hop_seconds: float

    @pydantic.model_validator(mode="after")
    def _require_acceptance_constants(self) -> typing.Self:
        """Pin every rendered limit to its acceptance constant."""
        if (self.n, self.x_ms, self.fatal_streak, self.hop_seconds) != _LOCKED_LIMIT_VALUES:
            raise ValueError("locked limits differ from the acceptance constants")
        return self


class ColdReportCheck(_ReportModel):
    """One per-check result, as its software check defines it; not a run verdict."""

    check: ColdCheck
    outcome: ColdCheckOutcome
    failures: tuple[ColdCheckFailure, ...]

    @pydantic.model_validator(mode="after")
    def _require_consistent_failures(self) -> typing.Self:
        """Require unique failures in declaration order and ``PASS`` exactly without one."""
        ordered = tuple(member for member in ColdCheckFailure if member in self.failures)
        outcome_is_pass = self.outcome is ColdCheckOutcome.PASS
        if self.failures != ordered or outcome_is_pass == bool(self.failures):
            raise ValueError("check failures and outcome disagree")
        return self


class ColdReportCounters(_ReportModel):
    """Final-snapshot counters plus the G17 maximum; each ``None`` when unavailable."""

    emitted: _Count | None
    processed: _Count | None
    dropped: _Count | None
    inference_overruns: _Count | None
    total_overflows: _Count | None
    max_inference_duration_ms: _Measure | None


class ColdReportAbort(_ReportModel):
    """One abort classification: a closed domain and its paired closed reason."""

    domain: ColdAbortDomain
    reason: (
        ColdHostAbortReason
        | ColdIdentityAbortReason
        | ColdEvidenceFailure
        | ColdMcpAbortReason
        | ColdAdvisorFailureKind
        | ColdOperatorAbortReason
    )

    @pydantic.model_validator(mode="after")
    def _require_paired_reason(self) -> typing.Self:
        """Require the reason's enum to be the one the retained-run reader pairs with its domain."""
        if type(self.reason) is not ABORT_REASON_BY_DOMAIN.get(self.domain):
            raise ValueError("abort reason does not belong to its domain")
        return self


class ColdReportAdvisorFailureCount(_ReportModel):
    """How many advisory records carry one advisor-failure kind."""

    kind: ColdAdvisorFailureKind
    count: _Count


class ColdReportHostExtremes(_ReportModel):
    """Host extremes across a phase's host records; rendered, never compared."""

    max_soc_temp_c: float
    min_mem_available_bytes: _Count
    min_free_bytes: _Count


class ColdReportRecordingArtefact(_ReportModel):
    """One recording artefact's role and size; its path and filename are never read."""

    role: typing.Literal[
        "primary_wav", "recording_sidecar", "annotation_session_sidecar", "additional_wav"
    ]
    size_bytes: _Count | None


class ColdReportPhase(_ReportModel):
    """One present phase: its five per-check results and allow-listed derived values."""

    phase: ColdPhaseKind
    identity_sha256: _Digest
    checks: tuple[ColdReportCheck, ...]
    d191: ColdD191Metrics | None
    tick_count: _Count
    observed_tick_span_seconds: _Measure | None
    counters: ColdReportCounters
    session_id_sha256: _Digest | None
    mcp_reported_finalisation_status: ColdFinalisationStatus | None
    mcp_reported_clean: bool | None
    observed_command_streaming_required: bool | None
    applied_branch: ColdCapabilityBranch | None
    aborts: tuple[ColdReportAbort, ...]
    advisory_record_count: _Count
    advisor_failure_counts: tuple[ColdReportAdvisorFailureCount, ...]
    host_extremes: ColdReportHostExtremes | None
    recording_artefacts: tuple[ColdReportRecordingArtefact, ...] | None
    identity_facts: ColdIdentityFacts | None

    @pydantic.model_validator(mode="after")
    def _require_closed_phase(self) -> typing.Self:
        """Require the five checks in order, facts with qualification, one count per kind."""
        if tuple(item.check for item in self.checks) != tuple(ColdCheck):
            raise ValueError("phase checks are not the closed check set")
        if (self.checks[0].outcome is ColdCheckOutcome.PASS) != (self.identity_facts is not None):
            raise ValueError("identity facts disagree with the qualification result")
        kinds = tuple(item.kind for item in self.advisor_failure_counts)
        if kinds != tuple(ColdAdvisorFailureKind):
            raise ValueError("advisor failure counts are not one per kind")
        return self


class ColdSanitisedReport(_ReportModel):
    """The closed public report schema v1 over one interpreted retained run."""

    report_schema_version: typing.Literal[1]
    run_id_sha256: _Digest
    manifest_sha256: _Digest
    locked_limits: ColdReportLockedLimits
    phases: tuple[ColdReportPhase, ...]
    phases_absent: tuple[ColdPhaseKind, ...]

    @pydantic.model_validator(mode="after")
    def _require_partitioned_phases(self) -> typing.Self:
        """Require present phases in phase order and ``phases_absent`` as their complement."""
        present = tuple(item.phase for item in self.phases)
        if not present or present != tuple(kind for kind in ColdPhaseKind if kind in present):
            raise ValueError("phases are empty, repeated or out of phase order")
        if self.phases_absent != tuple(kind for kind in ColdPhaseKind if kind not in present):
            raise ValueError("absent phases are not the complement of the present phases")
        return self
