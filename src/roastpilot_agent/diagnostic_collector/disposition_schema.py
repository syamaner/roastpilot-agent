"""Closed synthetic command-disposition expectations, results, and report vocabulary.

A satisfied selection check means only that the admitted synthetic rows for one
exact identity consistently report one declared value.  It never asserts
dispatch, invocation, timeliness, commit, settlement, physical effect, recipe
success, or currency; those facts belong to witness checks that this version
can never satisfy.
"""

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

from roastpilot_agent.diagnostic_collector.bundle_reader import (
    MAX_RECORDS,
    MAX_SEQUENCE,
    MAX_STREAM_RECORDS,
)
from roastpilot_agent.diagnostic_collector.bundle_schema import (
    CollectorAuthority,
    CollectorStream,
)
from roastpilot_agent.diagnostic_collector.interpretation_schema import (
    CheckStatus,
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
_StreamCount = Annotated[_Count, Field(le=MAX_STREAM_RECORDS)]
_Seq = Annotated[_Count, Field(le=MAX_SEQUENCE)]
_Generation = Annotated[_Count, Field(le=MAX_SEQUENCE)]
_HeatPercent = Annotated[int | float, BeforeValidator(_exact_number), Field(ge=0, le=100)]
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


class CommandBinding(_ClosedModel):
    """Caller-bound exact command identity; partial bindings never match."""

    command_id: _Token
    owner_id: _Token | None = None
    generation: _Generation | None = None

    @property
    def bound(self) -> bool:
        """Whether both the owner and the generation are bound.

        Returns:
            ``True`` only when neither identity component is ``None``.
        """
        return self.owner_id is not None and self.generation is not None


class DispositionExpectations(_ClosedModel):
    """Caller-bound identities for the synthetic disposition report, without clocks."""

    session_id: _Token
    bundle_id: _Token
    command: CommandBinding | None = None
    endpoint_id: _Token | None = None
    observation_id: _Token | None = None


class CommandDispositionValue(Enum):
    """Retained v1 command disposition labels."""

    NOT_DISPATCHED = "not_dispatched"
    RETURNED_COMMAND_STATE = "returned_command_state"
    UNKNOWN = "unknown"
    SUPERSEDED = "superseded"


class AckStatusValue(Enum):
    """Retained v1 persistence acknowledgement labels."""

    ACKNOWLEDGED = "acknowledged"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class EndpointOutcomeValue(Enum):
    """Retained v1 endpoint outcome labels."""

    RECIPE_TARGET_MET = "recipe_target_met"
    HUMAN_PROTECTION_INTERRUPTED = "human_protection_interrupted"
    OTHER_INTERRUPTION = "other_interruption"
    UNKNOWN = "unknown"


class ObservedEffectValue(Enum):
    """Retained v1 operator-observed effect labels."""

    HEAT_OFF = "heat_off"
    BEANS_EVACUATED = "beans_evacuated"
    COOLING_RUNNING = "cooling_running"
    UNKNOWN = "unknown"


class DispositionCheckName(Enum):
    """Synthetic disposition checks in report order."""

    COMMAND_REPORTED_DISPOSITION = "command_reported_disposition"
    COMMAND_INVOCATION_WITNESS = "command_invocation_witness"
    PERSISTENCE_ACKNOWLEDGEMENT = "persistence_acknowledgement"
    PERSISTENCE_SETTLEMENT_WITNESS = "persistence_settlement_witness"
    ENDPOINT_DECLARATION = "endpoint_declaration"
    OPERATOR_OBSERVATION = "operator_observation"
    PHYSICAL_EFFECT_ASSOCIATION = "physical_effect_association"
    OBSERVED_PROVENANCE = "observed_provenance"


DISPOSITION_CHECK_STREAM: dict[DispositionCheckName, CollectorStream | None] = {
    DispositionCheckName.COMMAND_REPORTED_DISPOSITION: CollectorStream.COMMAND,
    DispositionCheckName.COMMAND_INVOCATION_WITNESS: CollectorStream.COMMAND,
    DispositionCheckName.PERSISTENCE_ACKNOWLEDGEMENT: CollectorStream.PERSISTENCE_ACK,
    DispositionCheckName.PERSISTENCE_SETTLEMENT_WITNESS: CollectorStream.PERSISTENCE_ACK,
    DispositionCheckName.ENDPOINT_DECLARATION: CollectorStream.ENDPOINT,
    DispositionCheckName.OPERATOR_OBSERVATION: CollectorStream.OPERATOR_OBSERVATION,
    DispositionCheckName.PHYSICAL_EFFECT_ASSOCIATION: CollectorStream.OPERATOR_OBSERVATION,
    DispositionCheckName.OBSERVED_PROVENANCE: None,
}


class DispositionReason(Enum):
    """Closed reasons for unresolved or invalid synthetic disposition checks."""

    SCOPE_MISMATCH = "scope_mismatch"
    EVIDENCE_MALFORMED = "evidence_malformed"
    EXPECTATION_UNBOUND = "expectation_unbound"
    EVIDENCE_ABSENT = "evidence_absent"
    NO_MATCHING_IDENTITY = "no_matching_identity"
    CONFLICT = "conflict"
    DISPOSITION_UNKNOWN = "disposition_unknown"
    ACKNOWLEDGEMENT_NOT_OBSERVED = "acknowledgement_not_observed"
    ENDPOINT_UNKNOWN = "endpoint_unknown"
    RECIPE_SUCCESS_UNPROVEN = "recipe_success_unproven"
    EFFECT_UNKNOWN = "effect_unknown"
    PRODUCER_WITNESS_ABSENT = "producer_witness_absent"
    SYNTHETIC_EVIDENCE = "synthetic_evidence"


DISPOSITION_REASON_STATUS = {
    DispositionReason.SCOPE_MISMATCH: CheckStatus.INVALID,
    DispositionReason.EVIDENCE_MALFORMED: CheckStatus.INVALID,
    DispositionReason.EXPECTATION_UNBOUND: CheckStatus.UNRESOLVED,
    DispositionReason.EVIDENCE_ABSENT: CheckStatus.UNRESOLVED,
    DispositionReason.NO_MATCHING_IDENTITY: CheckStatus.UNRESOLVED,
    DispositionReason.CONFLICT: CheckStatus.INVALID,
    DispositionReason.DISPOSITION_UNKNOWN: CheckStatus.UNRESOLVED,
    DispositionReason.ACKNOWLEDGEMENT_NOT_OBSERVED: CheckStatus.UNRESOLVED,
    DispositionReason.ENDPOINT_UNKNOWN: CheckStatus.UNRESOLVED,
    DispositionReason.RECIPE_SUCCESS_UNPROVEN: CheckStatus.UNRESOLVED,
    DispositionReason.EFFECT_UNKNOWN: CheckStatus.UNRESOLVED,
    DispositionReason.PRODUCER_WITNESS_ABSENT: CheckStatus.UNRESOLVED,
    DispositionReason.SYNTHETIC_EVIDENCE: CheckStatus.UNRESOLVED,
}

_Name = DispositionCheckName
_Reason = DispositionReason
_PREREQUISITE_REASONS: frozenset[DispositionReason | None] = frozenset(
    {
        _Reason.SCOPE_MISMATCH,
        _Reason.EVIDENCE_MALFORMED,
        _Reason.EXPECTATION_UNBOUND,
        _Reason.EVIDENCE_ABSENT,
    }
)
_SELECTED = _PREREQUISITE_REASONS | {None, _Reason.NO_MATCHING_IDENTITY}
_WITNESSED = _PREREQUISITE_REASONS | {_Reason.PRODUCER_WITNESS_ABSENT}
_REASONS_BY_CHECK: dict[DispositionCheckName, frozenset[DispositionReason | None]] = {
    _Name.COMMAND_REPORTED_DISPOSITION: _SELECTED | {_Reason.CONFLICT, _Reason.DISPOSITION_UNKNOWN},
    _Name.COMMAND_INVOCATION_WITNESS: _WITNESSED,
    _Name.PERSISTENCE_ACKNOWLEDGEMENT: _SELECTED | {_Reason.ACKNOWLEDGEMENT_NOT_OBSERVED},
    _Name.PERSISTENCE_SETTLEMENT_WITNESS: _WITNESSED,
    _Name.ENDPOINT_DECLARATION: _SELECTED
    | {_Reason.CONFLICT, _Reason.ENDPOINT_UNKNOWN, _Reason.RECIPE_SUCCESS_UNPROVEN},
    _Name.OPERATOR_OBSERVATION: _SELECTED | {_Reason.CONFLICT, _Reason.EFFECT_UNKNOWN},
    _Name.PHYSICAL_EFFECT_ASSOCIATION: _WITNESSED,
    _Name.OBSERVED_PROVENANCE: frozenset({_Reason.SYNTHETIC_EVIDENCE}),
}
_LABEL_TYPES: dict[DispositionCheckName, type[Enum]] = {
    _Name.COMMAND_REPORTED_DISPOSITION: CommandDispositionValue,
    _Name.PERSISTENCE_ACKNOWLEDGEMENT: AckStatusValue,
    _Name.ENDPOINT_DECLARATION: EndpointOutcomeValue,
    _Name.OPERATOR_OBSERVATION: ObservedEffectValue,
}
_OWNED_CHECKS = frozenset({_Name.COMMAND_REPORTED_DISPOSITION, _Name.PERSISTENCE_ACKNOWLEDGEMENT})
_WITNESS_SELECTION = {
    _Name.COMMAND_INVOCATION_WITNESS: _Name.COMMAND_REPORTED_DISPOSITION,
    _Name.PERSISTENCE_SETTLEMENT_WITNESS: _Name.PERSISTENCE_ACKNOWLEDGEMENT,
    _Name.PHYSICAL_EFFECT_ASSOCIATION: _Name.OPERATOR_OBSERVATION,
}
_INTERRUPTIONS = frozenset(
    {EndpointOutcomeValue.HUMAN_PROTECTION_INTERRUPTED, EndpointOutcomeValue.OTHER_INTERRUPTION}
)


class DispositionAdapterLimit(Enum):
    """Facts v1 cannot express, each naming a future producer requirement."""

    # Session, child generation, command generation, attempt, and owner kind.
    COMMAND_REFERENCE_SPLIT = "command_reference_split"
    DISPATCH_INVOCATION_WITNESS = "dispatch_invocation_witness"
    SUPERSEDING_OWNER_REFERENCE = "superseding_owner_reference"
    # Token, run, turn, deadline, writer_entered, timely, and settlement.
    PERSISTENCE_OPERATION_RECEIPT = "persistence_operation_receipt"
    PERSISTENCE_COMMIT_INSTANT = "persistence_commit_instant"
    DRAIN_OWNER_AND_BACKLOG_OMISSION = "drain_owner_and_backlog_omission"
    OPERATOR_OBSERVATION_ATTRIBUTION = "operator_observation_attribution"
    PHYSICAL_EFFECT_CAUSAL_ASSOCIATION = "physical_effect_causal_association"
    # Accepted BT, canonical charge, human FC, operational DTR, exact controller dispatch.
    RECIPE_SUCCESS_JOINT_BINDING = "recipe_success_joint_binding"
    CROSS_STREAM_ORDER_CLOCK = "cross_stream_order_clock"
    OBSERVED_PROVENANCE = "observed_provenance"


class DispositionReportName(Enum):
    """The report's discriminator."""

    DISPOSITION = "disposition"


class RetainedLabel(_ClosedModel):
    """One distinct retained value, its row count, and its first stream-local seq."""

    value: CommandDispositionValue | AckStatusValue | EndpointOutcomeValue | ObservedEffectValue
    rows: Annotated[_StreamCount, Field(ge=1)]
    first_seq: _Seq


class DispositionSelectionDetail(_ClosedModel):
    """Counts and retained labels for one exact-identity stream selection."""

    selected_rows: _StreamCount
    exact_duplicate_rows: _StreamCount
    foreign_identity_rows: _StreamCount
    unrelated_rows: _StreamCount
    labels: Annotated[tuple[RetainedLabel, ...], Field(max_length=4)]
    distinct_returned_heat_values: _Count
    returned_heat_percent: _HeatPercent | None

    @model_validator(mode="after")
    def _consistent_inventory(self) -> Self:
        total = self.selected_rows + self.foreign_identity_rows + self.unrelated_rows
        if not 1 <= total <= MAX_STREAM_RECORDS:
            raise ValueError("expected a bounded nonempty stream inventory")
        if sum(label.rows for label in self.labels) != self.selected_rows:
            raise ValueError("expected label rows to equal selected rows")
        seqs = [label.first_seq for label in self.labels]
        if any(left >= right for left, right in zip(seqs, seqs[1:], strict=False)):
            raise ValueError("expected labels in strictly increasing first-seq order")
        values = [label.value for label in self.labels]
        if len(set(values)) != len(values) or len({type(value) for value in values}) > 1:
            raise ValueError("expected distinct labels of one vocabulary")
        if self.exact_duplicate_rows > max(self.selected_rows - 1, 0):
            raise ValueError("expected fewer duplicates than selected rows")
        return self


class DispositionCheckResult(_ClosedModel):
    """A closed disposition result with exact-identity evidence references."""

    check: DispositionCheckName
    status: CheckStatus
    reason: DispositionReason | None
    evidence: Annotated[tuple[EvidenceRef, ...], Field(max_length=MAX_STREAM_RECORDS)]
    detail: DispositionSelectionDetail | None

    @model_validator(mode="after")
    def _closed_result(self) -> Self:
        status = (
            CheckStatus.SATISFIED if self.reason is None else DISPOSITION_REASON_STATUS[self.reason]
        )
        if self.status is not status:
            raise ValueError("reason does not match check status")
        if self.reason not in _REASONS_BY_CHECK[self.check]:
            raise ValueError("reason is unreachable for this check")
        selection = self.check in _LABEL_TYPES and self.reason not in _PREREQUISITE_REASONS
        if (self.detail is not None) is not selection:
            raise ValueError("detail does not match check and reason")
        if not selection and self.evidence:
            raise ValueError("expected empty witness, provenance, or prerequisite evidence")
        self._validate_references()
        if self.detail is not None:
            self._validate_detail(self.detail)
        return self

    def _validate_references(self) -> None:
        stream = DISPOSITION_CHECK_STREAM[self.check]
        previous = -1
        for ref in self.evidence:
            seq: object = ref.seq
            if (
                ref.stream is not stream
                or type(seq) is not int
                or not previous < seq <= MAX_SEQUENCE
                or len(ref.instants) != 1
            ):
                raise ValueError("expected ordered single-instant references to this stream")
            previous = ref.seq

    def _validate_detail(self, detail: DispositionSelectionDetail) -> None:
        if len(self.evidence) != detail.selected_rows:
            raise ValueError("expected one reference per selected row")
        if not {label.first_seq for label in detail.labels} <= {ref.seq for ref in self.evidence}:
            raise ValueError("expected label first seqs among the references")
        if any(type(label.value) is not _LABEL_TYPES[self.check] for label in detail.labels):
            raise ValueError("expected labels of this check's vocabulary")
        if detail.foreign_identity_rows and self.check not in _OWNED_CHECKS:
            raise ValueError("expected no foreign-identity rows for this check")
        if (self.reason is _Reason.NO_MATCHING_IDENTITY) is not (detail.selected_rows == 0):
            raise ValueError("expected no matching identity exactly when nothing is selected")
        self._validate_heat(detail)
        if not self._outcome_supported(detail):
            raise ValueError("labels and counts do not support the reason")

    def _validate_heat(self, detail: DispositionSelectionDetail) -> None:
        heats = detail.distinct_returned_heat_values
        if self.check is not _Name.COMMAND_REPORTED_DISPOSITION:
            if heats != 0 or detail.returned_heat_percent is not None:
                raise ValueError("expected no returned heat outside the command check")
            return
        returned_rows = sum(
            label.rows
            for label in detail.labels
            if label.value is CommandDispositionValue.RETURNED_COMMAND_STATE
        )
        if (
            (heats >= 1) is not (returned_rows >= 1)
            or heats > returned_rows
            or (detail.returned_heat_percent is not None) is not (heats == 1)
        ):
            raise ValueError("expected returned heat values only for returned command state")

    def _outcome_supported(self, detail: DispositionSelectionDetail) -> bool:
        values = {label.value for label in detail.labels}
        distinct = detail.selected_rows - detail.exact_duplicate_rows
        single = distinct == 1 and len(values) == 1
        rules: dict[DispositionReason | None, bool]
        if self.check is _Name.COMMAND_REPORTED_DISPOSITION:
            unknown = CommandDispositionValue.UNKNOWN in values
            known = len(values - {CommandDispositionValue.UNKNOWN})
            conflict = known >= 2 or detail.distinct_returned_heat_values >= 2
            rules = {
                None: len(values) == 1 and not unknown and not conflict,
                _Reason.CONFLICT: conflict,
                _Reason.DISPOSITION_UNKNOWN: unknown and not conflict,
            }
        elif self.check is _Name.PERSISTENCE_ACKNOWLEDGEMENT:
            acknowledged = AckStatusValue.ACKNOWLEDGED in values
            rules = {
                None: acknowledged,
                _Reason.ACKNOWLEDGEMENT_NOT_OBSERVED: bool(values) and not acknowledged,
            }
        elif self.check is _Name.ENDPOINT_DECLARATION:
            rules = {
                None: single and values <= _INTERRUPTIONS,
                _Reason.RECIPE_SUCCESS_UNPROVEN: single
                and values == {EndpointOutcomeValue.RECIPE_TARGET_MET},
                _Reason.ENDPOINT_UNKNOWN: single and values == {EndpointOutcomeValue.UNKNOWN},
                _Reason.CONFLICT: distinct >= 2,
            }
        else:
            rules = {
                None: single and ObservedEffectValue.UNKNOWN not in values,
                _Reason.EFFECT_UNKNOWN: single and values == {ObservedEffectValue.UNKNOWN},
                _Reason.CONFLICT: distinct >= 2,
            }
        return rules.get(self.reason, True)


class DispositionReport(_ClosedModel):
    """Versioned synthetic disposition evidence with no operational authority."""

    report_schema_version: Literal[1] = 1
    report_name: Literal[DispositionReportName.DISPOSITION] = DispositionReportName.DISPOSITION
    bundle_sha256: _Digest
    authority: Literal[CollectorAuthority.NONE] = CollectorAuthority.NONE
    provenance: Literal[ProvenanceClass.SYNTHETIC_UNATTESTED] = ProvenanceClass.SYNTHETIC_UNATTESTED
    adapter_limits: tuple[DispositionAdapterLimit, ...] = tuple(DispositionAdapterLimit)
    checks: tuple[DispositionCheckResult, ...]

    @model_validator(mode="after")
    def _closed_report(self) -> Self:
        if self.adapter_limits != tuple(DispositionAdapterLimit):
            raise ValueError("expected the full disposition adapter-limit inventory")
        if tuple(result.check for result in self.checks) != tuple(DispositionCheckName):
            raise ValueError("expected one disposition result per check in schema order")
        results = {result.check: result for result in self.checks}
        for witness, selection in _WITNESS_SELECTION.items():
            prerequisite = results[selection].reason
            expected = (
                prerequisite
                if prerequisite in _PREREQUISITE_REASONS
                else _Reason.PRODUCER_WITNESS_ABSENT
            )
            if results[witness].reason is not expected:
                raise ValueError("witness reason does not follow its selection prerequisite")
        if sum(len(result.evidence) for result in self.checks) > MAX_RECORDS:
            raise ValueError("expected bounded total evidence references")
        return self
