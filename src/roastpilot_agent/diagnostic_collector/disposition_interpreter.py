"""Interpret admitted synthetic command, acknowledgement, endpoint and observation rows.

Each selection outcome is order-free over the complete exact-identity inventory.
Source order only records each retained value's first stream-local sequence.
No instant is compared, and no witness check can be satisfied.
"""

from typing import cast

from pydantic import ValidationError

import roastpilot_agent.diagnostic_collector.bundle_reader as bundle_reader
from roastpilot_agent.diagnostic_collector.bundle_schema import (
    AdmittedBundle,
    CollectorEnvelope,
    CollectorPayload,
    CollectorStream,
    CollectorStreamEntry,
    CollectorStreamState,
    CommandPayload,
    EndpointPayload,
    Instant,
    OperatorObservationPayload,
    PersistenceAckPayload,
)
from roastpilot_agent.diagnostic_collector.disposition_schema import (
    DISPOSITION_REASON_STATUS,
    AckStatusValue,
    CommandBinding,
    CommandDispositionValue,
    DispositionCheckName,
    DispositionCheckResult,
    DispositionExpectations,
    DispositionReason,
    DispositionReport,
    DispositionSelectionDetail,
    EndpointOutcomeValue,
    ObservedEffectValue,
    RetainedLabel,
)
from roastpilot_agent.diagnostic_collector.interpretation_schema import (
    CheckStatus,
    EvidenceRef,
    InterpretationInputError,
    InterpretationInputReason,
)

_Label = CommandDispositionValue | AckStatusValue | EndpointOutcomeValue | ObservedEffectValue
_Row = tuple[CollectorEnvelope, _Label]
_Outcome = tuple[DispositionReason | None, tuple[EvidenceRef, ...], DispositionSelectionDetail]
_Name = DispositionCheckName
_Reason = DispositionReason
_RETURNED = CommandDispositionValue.RETURNED_COMMAND_STATE


def _reference(record: CollectorEnvelope) -> EvidenceRef:
    fields = type(record.payload).model_fields
    values = vars(record.payload)
    instants = tuple(values[name] for name in fields if isinstance(values[name], Instant))
    return EvidenceRef(stream=record.stream, seq=record.seq, instants=instants)


def _prerequisite(
    scope_matches: bool, entry: CollectorStreamEntry, binding: object
) -> DispositionReason | None:
    if not scope_matches:
        return _Reason.SCOPE_MISMATCH
    if entry.state is CollectorStreamState.MALFORMED:
        return _Reason.EVIDENCE_MALFORMED
    if binding is None:
        return _Reason.EXPECTATION_UNBOUND
    if entry.state is CollectorStreamState.ABSENT:
        return _Reason.EVIDENCE_ABSENT
    return None


def _detail(
    rows: tuple[_Row, ...],
    foreign: int,
    unrelated: int,
    heats: tuple[int | float, ...] = (),
) -> DispositionSelectionDetail:
    seen: set[CollectorPayload] = set()
    duplicates = 0
    labels: dict[_Label, tuple[int, int]] = {}
    for record, value in rows:
        duplicates += record.payload in seen
        seen.add(record.payload)
        count, first_seq = labels.get(value, (0, record.seq))
        labels[value] = (count + 1, first_seq)
    distinct_heats: dict[int | float, int | float] = {}
    for heat in heats:
        distinct_heats.setdefault(heat, heat)  # Numeric equality: 50 and 50.0 are one claim.
    return DispositionSelectionDetail(
        selected_rows=len(rows),
        exact_duplicate_rows=duplicates,
        foreign_identity_rows=foreign,
        unrelated_rows=unrelated,
        labels=tuple(
            RetainedLabel(value=value, rows=count, first_seq=first_seq)
            for value, (count, first_seq) in labels.items()
        ),
        distinct_returned_heat_values=len(distinct_heats),
        returned_heat_percent=next(iter(distinct_heats.values()))
        if len(distinct_heats) == 1
        else None,
    )


def _owned_rows(
    records: tuple[CollectorEnvelope, ...], binding: CommandBinding
) -> tuple[tuple[CollectorEnvelope, ...], int, int]:
    selected: list[CollectorEnvelope] = []
    foreign = 0
    for record in records:
        row = cast(CommandPayload | PersistenceAckPayload, record.payload)  # S1 binds the stream.
        if row.command_id != binding.command_id:
            continue
        if row.owner_id == binding.owner_id and row.generation == binding.generation:
            selected.append(record)
        else:
            foreign += 1
    return tuple(selected), foreign, len(records) - len(selected) - foreign


def _command(records: tuple[CollectorEnvelope, ...], binding: CommandBinding) -> _Outcome:
    selected, foreign, unrelated = _owned_rows(records, binding)
    payloads = tuple(cast(CommandPayload, record.payload) for record in selected)
    values = tuple(CommandDispositionValue(row.disposition) for row in payloads)
    returned = tuple(
        row.commanded_heat_percent
        for row, value in zip(payloads, values, strict=True)
        if value is _RETURNED
    )
    heats = tuple(heat for heat in returned if heat is not None)
    detail = _detail(tuple(zip(selected, values, strict=True)), foreign, unrelated, heats)
    labels = {label.value for label in detail.labels}
    known = labels - {CommandDispositionValue.UNKNOWN}
    reason: DispositionReason | None = None
    if not selected:
        reason = _Reason.NO_MATCHING_IDENTITY
    elif len(known) > 1 or detail.distinct_returned_heat_values > 1:
        reason = _Reason.CONFLICT
    elif CommandDispositionValue.UNKNOWN in labels:
        reason = _Reason.DISPOSITION_UNKNOWN
    return reason, tuple(_reference(record) for record in selected), detail


def _acknowledgement(records: tuple[CollectorEnvelope, ...], binding: CommandBinding) -> _Outcome:
    selected, foreign, unrelated = _owned_rows(records, binding)
    rows = tuple(
        (record, AckStatusValue(cast(PersistenceAckPayload, record.payload).status))
        for record in selected
    )
    detail = _detail(rows, foreign, unrelated)
    reason: DispositionReason | None = None
    if not selected:
        reason = _Reason.NO_MATCHING_IDENTITY
    elif AckStatusValue.ACKNOWLEDGED not in {label.value for label in detail.labels}:
        reason = _Reason.ACKNOWLEDGEMENT_NOT_OBSERVED
    return reason, tuple(_reference(record) for record in selected), detail


def _endpoint(records: tuple[CollectorEnvelope, ...], endpoint_id: str) -> _Outcome:
    rows = tuple(
        (record, EndpointOutcomeValue(payload.outcome))
        for record in records
        if (payload := cast(EndpointPayload, record.payload)).endpoint_id == endpoint_id
    )
    detail = _detail(rows, 0, len(records) - len(rows))
    values = {label.value for label in detail.labels}
    reason: DispositionReason | None = None
    if not rows:
        reason = _Reason.NO_MATCHING_IDENTITY
    elif detail.selected_rows - detail.exact_duplicate_rows > 1:
        reason = _Reason.CONFLICT
    elif EndpointOutcomeValue.UNKNOWN in values:
        reason = _Reason.ENDPOINT_UNKNOWN
    elif EndpointOutcomeValue.RECIPE_TARGET_MET in values:
        reason = _Reason.RECIPE_SUCCESS_UNPROVEN
    return reason, tuple(_reference(record) for record, _ in rows), detail


def _observation(records: tuple[CollectorEnvelope, ...], observation_id: str) -> _Outcome:
    rows = tuple(
        (record, ObservedEffectValue(payload.effect))
        for record in records
        if (payload := cast(OperatorObservationPayload, record.payload)).observation_id
        == observation_id
    )
    detail = _detail(rows, 0, len(records) - len(rows))
    reason: DispositionReason | None = None
    if not rows:
        reason = _Reason.NO_MATCHING_IDENTITY
    elif detail.selected_rows - detail.exact_duplicate_rows > 1:
        reason = _Reason.CONFLICT
    elif ObservedEffectValue.UNKNOWN in {label.value for label in detail.labels}:
        reason = _Reason.EFFECT_UNKNOWN
    return reason, tuple(_reference(record) for record, _ in rows), detail


def _result(
    check: DispositionCheckName,
    reason: DispositionReason | None,
    evidence: tuple[EvidenceRef, ...] = (),
    detail: DispositionSelectionDetail | None = None,
) -> DispositionCheckResult:
    return DispositionCheckResult(
        check=check,
        status=CheckStatus.SATISFIED if reason is None else DISPOSITION_REASON_STATUS[reason],
        reason=reason,
        evidence=evidence,
        detail=detail,
    )


def _pair(
    selection: DispositionCheckName,
    witness: DispositionCheckName | None,
    prerequisite: DispositionReason | None,
    outcome: _Outcome | None,
) -> tuple[DispositionCheckResult, ...]:
    chosen = (
        _result(selection, *outcome)
        if prerequisite is None and outcome is not None
        else _result(selection, prerequisite)
    )
    if witness is None:
        return (chosen,)
    return chosen, _result(witness, prerequisite or _Reason.PRODUCER_WITNESS_ABSENT)


def _interpret(bundle: AdmittedBundle, expectations: DispositionExpectations) -> DispositionReport:
    scope = (
        bundle.header.session_id == expectations.session_id
        and bundle.header.bundle_id == expectations.bundle_id
    )
    streams = {entry.stream: entry for entry in bundle.streams}
    commands = streams[CollectorStream.COMMAND]
    acks = streams[CollectorStream.PERSISTENCE_ACK]
    endpoints = streams[CollectorStream.ENDPOINT]
    observations = streams[CollectorStream.OPERATOR_OBSERVATION]
    binding = expectations.command
    owned = binding if binding is not None and binding.bound else None
    endpoint_id, observation_id = expectations.endpoint_id, expectations.observation_id

    command_reason = _prerequisite(scope, commands, owned)
    ack_reason = _prerequisite(scope, acks, owned)
    endpoint_reason = _prerequisite(scope, endpoints, endpoint_id)
    observation_reason = _prerequisite(scope, observations, observation_id)
    return DispositionReport(
        bundle_sha256=bundle.bundle_sha256,
        checks=(
            *_pair(
                _Name.COMMAND_REPORTED_DISPOSITION,
                _Name.COMMAND_INVOCATION_WITNESS,
                command_reason,
                _command(commands.records, owned)
                if command_reason is None and owned is not None
                else None,
            ),
            *_pair(
                _Name.PERSISTENCE_ACKNOWLEDGEMENT,
                _Name.PERSISTENCE_SETTLEMENT_WITNESS,
                ack_reason,
                _acknowledgement(acks.records, owned)
                if ack_reason is None and owned is not None
                else None,
            ),
            *_pair(
                _Name.ENDPOINT_DECLARATION,
                None,
                endpoint_reason,
                _endpoint(endpoints.records, endpoint_id)
                if endpoint_reason is None and endpoint_id is not None
                else None,
            ),
            *_pair(
                _Name.OPERATOR_OBSERVATION,
                _Name.PHYSICAL_EFFECT_ASSOCIATION,
                observation_reason,
                _observation(observations.records, observation_id)
                if observation_reason is None and observation_id is not None
                else None,
            ),
            _result(_Name.OBSERVED_PROVENANCE, _Reason.SYNTHETIC_EVIDENCE),
        ),
    )


def interpret_disposition_bundle(
    data: bytes, *, expected_sha256: str, expectations: DispositionExpectations
) -> DispositionReport:
    """Evaluate in-memory synthetic disposition evidence after one S1 admission.

    Args:
        data: Unmodified canonical S1 bundle bytes.
        expected_sha256: Independently supplied digest passed through to S1.
        expectations: Independently bound exact command, endpoint and observation identities.

    Returns:
        A closed report whose satisfied checks assert only consistent declared values.

    Raises:
        InterpretationInputError: If the bytes type or expectations are invalid.
        CollectorAdmissionError: If S1 refuses the bundle.
    """
    if type(data) is not bytes:
        raise InterpretationInputError(InterpretationInputReason.DATA_NOT_BYTES)
    try:
        expected = DispositionExpectations.model_validate(expectations)
    except ValidationError:
        raise InterpretationInputError(InterpretationInputReason.EXPECTATIONS_INVALID) from None
    bundle = bundle_reader.read_synthetic_bundle(data, expected_sha256=expected_sha256)
    return _interpret(bundle, expected)
