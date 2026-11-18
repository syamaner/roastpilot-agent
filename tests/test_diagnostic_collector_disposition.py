"""Synthetic command-disposition contracts for D230 S2b-2 (#1080)."""

import ast
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Callable
from itertools import combinations
from pathlib import Path
from typing import cast, get_args

import pydantic
import pytest

from roastpilot_agent.diagnostic_collector import (
    CollectorAdmissionError,
    CollectorAdmissionReason,
    CollectorAuthority,
    CollectorStream,
    InterpretationInputError,
    InterpretationInputReason,
    SyntheticExpectations,
    bundle_reader,
    disposition_schema,
    interpret_synthetic_bundle,
)
from roastpilot_agent.diagnostic_collector.bundle_schema import (
    AdmittedBundle,
    ClockName,
    CommandPayload,
    EndpointPayload,
    Instant,
    OperatorObservationPayload,
    PersistenceAckPayload,
)
from roastpilot_agent.diagnostic_collector.coverage_interpreter import interpret_coverage_bundle
from roastpilot_agent.diagnostic_collector.coverage_schema import (
    CoverageExpectations,
    RecordingBinding,
)
from roastpilot_agent.diagnostic_collector.disposition_interpreter import (
    interpret_disposition_bundle,
)
from roastpilot_agent.diagnostic_collector.disposition_schema import (
    DISPOSITION_CHECK_STREAM,
    DISPOSITION_REASON_STATUS,
    AckStatusValue,
    CommandBinding,
    CommandDispositionValue,
    DispositionAdapterLimit,
    DispositionCheckName,
    DispositionCheckResult,
    DispositionExpectations,
    DispositionReason,
    DispositionReport,
    DispositionReportName,
    DispositionSelectionDetail,
    EndpointOutcomeValue,
    ObservedEffectValue,
    RetainedLabel,
)
from roastpilot_agent.diagnostic_collector.interpretation_schema import (
    CheckStatus,
    EvidenceRef,
    ProvenanceClass,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "d230_collector" / "synthetic-v1.jsonl"
_AGENT = ClockName.AGENT_MONOTONIC_SECONDS
_N = DispositionCheckName
_R = DispositionReason
_C = CollectorStream
_CMD = CommandDispositionValue
_ACK = AckStatusValue
_END = EndpointOutcomeValue
_OBS = ObservedEffectValue
_Value = CommandDispositionValue | AckStatusValue | EndpointOutcomeValue | ObservedEffectValue
_Payload = dict[str, object]
_Streams = dict[str, list[_Payload]]
_DISPOSITION_STREAMS = ("command", "persistence_ack", "endpoint", "operator_observation")
_SELECTION = {
    "command": _N.COMMAND_REPORTED_DISPOSITION,
    "persistence_ack": _N.PERSISTENCE_ACKNOWLEDGEMENT,
    "endpoint": _N.ENDPOINT_DECLARATION,
    "operator_observation": _N.OPERATOR_OBSERVATION,
}
_WITNESS = {
    "command": _N.COMMAND_INVOCATION_WITNESS,
    "persistence_ack": _N.PERSISTENCE_SETTLEMENT_WITNESS,
    "operator_observation": _N.PHYSICAL_EFFECT_ASSOCIATION,
}
_WITNESSES = tuple(_WITNESS.values())


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _at(value: int | float) -> _Payload:
    return {"clock": _AGENT.value, "value": value}


def _returned(
    heat: int | float,
    *,
    at: int | float = 7.0,
    owner: str = "owner_1",
    generation: int = 0,
    command_id: str = "command_1",
) -> _Payload:
    return {
        "command_id": command_id,
        "owner_id": owner,
        "generation": generation,
        "observed_at": _at(at),
        "disposition": "returned_command_state",
        "commanded_heat_percent": heat,
    }


def _declared(disposition: str, *, at: int | float = 7.0, owner: str = "owner_1") -> _Payload:
    """A non-returned command row; its heat is an explicit null, never a zero."""
    return {
        "command_id": "command_1",
        "owner_id": owner,
        "generation": 0,
        "observed_at": _at(at),
        "disposition": disposition,
        "commanded_heat_percent": None,
    }


def _ack(status: str = "acknowledged", *, at: int | float = 8.0, generation: int = 0) -> _Payload:
    return {
        "command_id": "command_1",
        "owner_id": "owner_1",
        "generation": generation,
        "observed_at": _at(at),
        "status": status,
    }


def _endpoint(
    outcome: str = "human_protection_interrupted",
    *,
    at: int | float = 10.0,
    endpoint_id: str = "endpoint_1",
) -> _Payload:
    return {"endpoint_id": endpoint_id, "accepted_at": _at(at), "outcome": outcome}


def _observation(
    effect: str = "heat_off", *, at: int | float = 9.0, observation_id: str = "observation_1"
) -> _Payload:
    return {"observation_id": observation_id, "observed_at": _at(at), "effect": effect}


def _fixture_streams() -> _Streams:
    _, *rows = (json.loads(line) for line in _FIXTURE.read_bytes().splitlines())
    streams: _Streams = {}
    for row in rows:
        streams.setdefault(row["stream"], []).append(row["payload"])
    return streams


_BASELINE: _Streams = {
    "command": [_returned(10.0)],
    "persistence_ack": [_ack()],
    "endpoint": [_endpoint()],
    "operator_observation": [_observation()],
}


def _bytes(
    changes: _Streams | None = None,
    *,
    base: _Streams | None = None,
    session: str = "session_1",
) -> bytes:
    streams = (_fixture_streams() | _BASELINE if base is None else base) | (changes or {})
    header: dict[str, object] = {
        "alignments": [],
        "bundle_id": "bundle_1",
        "evidence_class": "synthetic",
        "inventory": {item.value: len(streams.get(item.value, ())) for item in CollectorStream},
        "schema_version": 1,
        "session_id": session,
    }
    rows = (
        {"schema_version": 1, "session_id": session, "stream": stream, "seq": seq, "payload": row}
        for stream, payloads in streams.items()
        for seq, row in enumerate(payloads)
    )
    return b"".join(_canonical(row) + b"\n" for row in (header, *rows))


def _expect(**changes: object) -> DispositionExpectations:
    fields: dict[str, object] = {
        "session_id": "session_1",
        "bundle_id": "bundle_1",
        "command": CommandBinding(command_id="command_1", owner_id="owner_1", generation=0),
        "endpoint_id": "endpoint_1",
        "observation_id": "observation_1",
    }
    return DispositionExpectations.model_validate(fields | changes)


def _report(data: bytes, expectations: DispositionExpectations | None = None) -> DispositionReport:
    return interpret_disposition_bundle(
        data,
        expected_sha256=hashlib.sha256(data).hexdigest(),
        expectations=expectations or _expect(),
    )


def _check(report: DispositionReport, check: DispositionCheckName) -> DispositionCheckResult:
    return next(result for result in report.checks if result.check is check)


def _detail(result: DispositionCheckResult) -> DispositionSelectionDetail:
    assert result.detail is not None
    return result.detail


def _labels(result: DispositionCheckResult) -> list[tuple[_Value, int, int]]:
    return [(label.value, label.rows, label.first_seq) for label in _detail(result).labels]


def _counts(result: DispositionCheckResult) -> tuple[int, int, int, int]:
    detail = _detail(result)
    return (
        detail.selected_rows,
        detail.exact_duplicate_rows,
        detail.foreign_identity_rows,
        detail.unrelated_rows,
    )


def _command_result(changes: _Streams, **expect: object) -> DispositionCheckResult:
    return _check(_report(_bytes(changes), _expect(**expect)), _N.COMMAND_REPORTED_DISPOSITION)


def _assert_prerequisite(result: DispositionCheckResult, reason: DispositionReason) -> None:
    assert result.reason is reason
    assert result.status is DISPOSITION_REASON_STATUS[reason]
    assert result.evidence == () and result.detail is None


# T1 -------------------------------------------------------------------------------------------


def test_t1_baseline_selections_retain_exact_labels_counts_and_refs() -> None:
    """Every selection is satisfied for its exact identity; witnesses never are."""
    data = _bytes()
    report = _report(data)
    assert report.bundle_sha256 == hashlib.sha256(data).hexdigest()
    assert report.report_schema_version == 1
    assert report.report_name is DispositionReportName.DISPOSITION
    assert report.authority is CollectorAuthority.NONE
    assert report.provenance is ProvenanceClass.SYNTHETIC_UNATTESTED
    assert report.adapter_limits == tuple(DispositionAdapterLimit)
    assert tuple(result.check for result in report.checks) == tuple(DispositionCheckName)
    expected = {
        _N.COMMAND_REPORTED_DISPOSITION: (_CMD.RETURNED_COMMAND_STATE, _C.COMMAND, 7.0),
        _N.PERSISTENCE_ACKNOWLEDGEMENT: (_ACK.ACKNOWLEDGED, _C.PERSISTENCE_ACK, 8.0),
        _N.ENDPOINT_DECLARATION: (_END.HUMAN_PROTECTION_INTERRUPTED, _C.ENDPOINT, 10.0),
        _N.OPERATOR_OBSERVATION: (_OBS.HEAT_OFF, _C.OPERATOR_OBSERVATION, 9.0),
    }
    for check, (value, stream, at) in expected.items():
        result = _check(report, check)
        assert result.status is CheckStatus.SATISFIED and result.reason is None
        assert _labels(result) == [(value, 1, 0)]
        assert _counts(result) == (1, 0, 0, 0)
        assert result.evidence == (
            EvidenceRef(stream=stream, seq=0, instants=(Instant(clock=_AGENT, value=at),)),
        )
    command = _detail(_check(report, _N.COMMAND_REPORTED_DISPOSITION))
    assert command.distinct_returned_heat_values == 1
    assert command.returned_heat_percent == 10.0
    for check in (*_WITNESSES, _N.OBSERVED_PROVENANCE):
        result = _check(report, check)
        assert result.status is CheckStatus.UNRESOLVED and result.evidence == ()
        assert result.detail is None
    assert {_check(report, check).reason for check in _WITNESSES} == {_R.PRODUCER_WITNESS_ABSENT}
    assert _check(report, _N.OBSERVED_PROVENANCE).reason is _R.SYNTHETIC_EVIDENCE
    assert _report(data).model_dump_json() == report.model_dump_json()


def test_t1_one_reference_correlates_with_its_source_row() -> None:
    """A selected ref carries its stream-local seq and original instant, not a clock order."""
    rows = [_returned(10.0, command_id="other", at=3.5), _returned(10.0, at=12.25)]
    data = _bytes({"command": rows})
    result = _check(_report(data), _N.COMMAND_REPORTED_DISPOSITION)
    source = next(
        row
        for row in map(json.loads, data.splitlines()[1:])
        if row["stream"] == "command" and row["payload"]["command_id"] == "command_1"
    )
    assert source["seq"] == 1
    assert result.evidence == (
        EvidenceRef(
            stream=_C.COMMAND,
            seq=source["seq"],
            instants=(Instant.model_validate(source["payload"]["observed_at"]),),
        ),
    )
    assert _labels(result) == [(_CMD.RETURNED_COMMAND_STATE, 1, 1)]
    assert _counts(result) == (1, 0, 0, 1)


def test_t1_raw_fixture_never_satisfies_recipe_success() -> None:
    """The unchanged S1 fixture keeps recipe target met unresolved."""
    data = _FIXTURE.read_bytes()
    report = _report(data)
    assert _check(report, _N.COMMAND_REPORTED_DISPOSITION).status is CheckStatus.SATISFIED
    assert _check(report, _N.PERSISTENCE_ACKNOWLEDGEMENT).status is CheckStatus.SATISFIED
    assert _check(report, _N.OPERATOR_OBSERVATION).status is CheckStatus.SATISFIED
    endpoint = _check(report, _N.ENDPOINT_DECLARATION)
    assert endpoint.reason is _R.RECIPE_SUCCESS_UNPROVEN
    assert endpoint.status is CheckStatus.UNRESOLVED
    assert _labels(endpoint) == [(_END.RECIPE_TARGET_MET, 1, 0)]


# T2 -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "owner, generation",
    [pytest.param("owner_2", 0, id="owner"), pytest.param("owner_1", 1, id="generation")],
)
def test_t2_foreign_owner_or_generation_never_matches(owner: str, generation: int) -> None:
    """Identical bytes flip to no matching identity when one bound component changes."""
    data = _bytes()
    assert _check(_report(data), _N.COMMAND_REPORTED_DISPOSITION).reason is None
    assert _check(_report(data), _N.PERSISTENCE_ACKNOWLEDGEMENT).reason is None
    binding = CommandBinding(command_id="command_1", owner_id=owner, generation=generation)
    report = _report(data, _expect(command=binding))
    for check in (_N.COMMAND_REPORTED_DISPOSITION, _N.PERSISTENCE_ACKNOWLEDGEMENT):
        result = _check(report, check)
        assert result.reason is _R.NO_MATCHING_IDENTITY
        assert result.status is CheckStatus.UNRESOLVED
        assert result.evidence == () and _labels(result) == []
        assert _counts(result) == (0, 0, 1, 0)
    for check in (_N.COMMAND_INVOCATION_WITNESS, _N.PERSISTENCE_SETTLEMENT_WITNESS):
        assert _check(report, check).reason is _R.PRODUCER_WITNESS_ABSENT
    command = _detail(_check(report, _N.COMMAND_REPORTED_DISPOSITION))
    assert command.distinct_returned_heat_values == 0 and command.returned_heat_percent is None


@pytest.mark.parametrize(
    "binding",
    [
        pytest.param(CommandBinding(command_id="command_1", generation=0), id="owner-none"),
        pytest.param(CommandBinding(command_id="command_1", owner_id="owner_1"), id="gen-none"),
        pytest.param(CommandBinding(command_id="command_1"), id="both-none"),
        pytest.param(None, id="no-binding"),
    ],
)
def test_t2_partial_binding_is_unbound_not_a_command_id_match(
    binding: CommandBinding | None,
) -> None:
    """A partial binding never matches on command_id alone."""
    report = _report(_bytes(), _expect(command=binding))
    for check in (
        _N.COMMAND_REPORTED_DISPOSITION,
        _N.COMMAND_INVOCATION_WITNESS,
        _N.PERSISTENCE_ACKNOWLEDGEMENT,
        _N.PERSISTENCE_SETTLEMENT_WITNESS,
    ):
        _assert_prerequisite(_check(report, check), _R.EXPECTATION_UNBOUND)
    assert _check(report, _N.ENDPOINT_DECLARATION).reason is None
    assert binding is None or not binding.bound


def test_t2_other_command_ids_are_unrelated_not_foreign() -> None:
    """Rows for another command count as unrelated; only the exact triple is selected."""
    rows = [_returned(10.0, command_id="command_2"), _returned(10.0, owner="owner_2")]
    result = _command_result({"command": rows})
    assert result.reason is _R.NO_MATCHING_IDENTITY
    assert _counts(result) == (0, 0, 1, 1)


# T3 -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "row, value, heat",
    [
        (_declared("not_dispatched"), _CMD.NOT_DISPATCHED, None),
        (_declared("superseded"), _CMD.SUPERSEDED, None),
        (_returned(35), _CMD.RETURNED_COMMAND_STATE, 35),
    ],
)
def test_t3_single_declared_value_satisfies_only_the_reported_disposition(
    row: _Payload, value: CommandDispositionValue, heat: int | None
) -> None:
    """A single declaration is satisfied while invocation and physical association are not."""
    report = _report(_bytes({"command": [row]}))
    result = _check(report, _N.COMMAND_REPORTED_DISPOSITION)
    assert result.status is CheckStatus.SATISFIED
    assert _labels(result) == [(value, 1, 0)]
    detail = _detail(result)
    assert detail.returned_heat_percent == heat
    assert detail.distinct_returned_heat_values == (0 if heat is None else 1)
    invocation = _check(report, _N.COMMAND_INVOCATION_WITNESS)
    assert invocation.reason is _R.PRODUCER_WITNESS_ABSENT
    assert invocation.status is CheckStatus.UNRESOLVED and invocation.evidence == ()
    physical = _check(report, _N.PHYSICAL_EFFECT_ASSOCIATION)
    assert physical.status is CheckStatus.UNRESOLVED
    assert physical.reason is _R.PRODUCER_WITNESS_ABSENT


# T4 -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rows, labels, heats",
    [
        pytest.param(
            [_declared("not_dispatched"), _returned(50)],
            [(_CMD.NOT_DISPATCHED, 1, 0), (_CMD.RETURNED_COMMAND_STATE, 1, 1)],
            1,
            id="equal-instant",
        ),
        pytest.param(
            [_declared("not_dispatched", at=1), _returned(50, at=2)],
            [(_CMD.NOT_DISPATCHED, 1, 0), (_CMD.RETURNED_COMMAND_STATE, 1, 1)],
            1,
            id="different-instants",
        ),
        pytest.param(
            [_returned(0, at=1), _returned(50, at=2)],
            [(_CMD.RETURNED_COMMAND_STATE, 2, 0)],
            2,
            id="heat-0-and-50",
        ),
        pytest.param(
            [_declared("superseded"), _declared("not_dispatched", at=8)],
            [(_CMD.SUPERSEDED, 1, 0), (_CMD.NOT_DISPATCHED, 1, 1)],
            0,
            id="two-declarations",
        ),
        pytest.param(
            [_declared("unknown"), _declared("not_dispatched"), _returned(5)],
            [
                (_CMD.UNKNOWN, 1, 0),
                (_CMD.NOT_DISPATCHED, 1, 1),
                (_CMD.RETURNED_COMMAND_STATE, 1, 2),
            ],
            1,
            id="unknown-does-not-mask-conflict",
        ),
    ],
)
def test_t4_known_claims_conflict(
    rows: list[_Payload], labels: list[tuple[_Value, int, int]], heats: int
) -> None:
    """Two incompatible known claims are invalid and never resolved by order."""
    forward = _command_result({"command": rows})
    reverse = _command_result({"command": rows[::-1]})
    assert _labels(forward) == labels
    for result in (forward, reverse):
        assert result.reason is _R.CONFLICT
        assert result.status is CheckStatus.INVALID
        assert _counts(result) == (len(rows), 0, 0, 0)
        assert {(value, count) for value, count, _ in _labels(result)} == {
            (value, count) for value, count, _ in labels
        }
        detail = _detail(result)
        assert detail.distinct_returned_heat_values == heats
        if heats != 1:
            assert detail.returned_heat_percent is None


@pytest.mark.parametrize("first, second", [(50, 50.0), (50.0, 50)])
def test_t4_numerically_equal_heat_is_one_claim(first: int | float, second: int | float) -> None:
    """50 and 50.0 are one returned value; the first-seen representation is retained."""
    result = _command_result({"command": [_returned(first, at=1), _returned(second, at=2)]})
    assert result.reason is None and result.status is CheckStatus.SATISFIED
    detail = _detail(result)
    assert detail.distinct_returned_heat_values == 1
    assert detail.returned_heat_percent == 50
    assert type(detail.returned_heat_percent) is type(first)
    assert _counts(result) == (2, 0, 0, 0)
    assert _labels(result) == [(_CMD.RETURNED_COMMAND_STATE, 2, 0)]


# T5 -------------------------------------------------------------------------------------------


def test_t5_unknown_with_one_known_claim_stays_unresolved_in_either_order() -> None:
    """A known value never settles UNKNOWN, and a later seq never wins."""
    rows = [_declared("unknown", at=1), _returned(20, at=2)]
    forward = _command_result({"command": rows})
    reverse = _command_result({"command": rows[::-1]})
    for result in (forward, reverse):
        assert result.reason is _R.DISPOSITION_UNKNOWN
        assert result.status is CheckStatus.UNRESOLVED
        assert _counts(result) == (2, 0, 0, 0)
        assert _detail(result).distinct_returned_heat_values == 1
        assert _detail(result).returned_heat_percent == 20
    assert _labels(forward) == [(_CMD.UNKNOWN, 1, 0), (_CMD.RETURNED_COMMAND_STATE, 1, 1)]
    assert _labels(reverse) == [(_CMD.RETURNED_COMMAND_STATE, 1, 0), (_CMD.UNKNOWN, 1, 1)]


def test_t5_repeated_unknown_rows_are_not_exact_duplicates() -> None:
    """Distinct instants keep repeated UNKNOWN rows as distinct observations."""
    result = _command_result({"command": [_declared("unknown", at=1), _declared("unknown", at=2)]})
    assert result.reason is _R.DISPOSITION_UNKNOWN
    assert _counts(result) == (2, 0, 0, 0)
    assert _labels(result) == [(_CMD.UNKNOWN, 2, 0)]
    duplicate = _command_result({"command": [_declared("unknown"), _declared("unknown")]})
    assert _counts(duplicate) == (2, 1, 0, 0)
    assert duplicate.reason is _R.DISPOSITION_UNKNOWN


# T6 -------------------------------------------------------------------------------------------


def _ack_report(rows: list[_Payload]) -> DispositionReport:
    return _report(_bytes({"persistence_ack": rows}))


def test_t6_acknowledged_presence_is_satisfied_alongside_unknown() -> None:
    """Presence of an exact acknowledged label satisfies only the presence question."""
    rows = [_ack("unknown", at=1), _ack("acknowledged", at=1)]
    forward = _ack_report(rows)
    reverse = _ack_report(rows[::-1])
    for report in (forward, reverse):
        result = _check(report, _N.PERSISTENCE_ACKNOWLEDGEMENT)
        assert result.reason is None and result.status is CheckStatus.SATISFIED
        assert _counts(result) == (2, 0, 0, 0)
        settlement = _check(report, _N.PERSISTENCE_SETTLEMENT_WITNESS)
        assert settlement.reason is _R.PRODUCER_WITNESS_ABSENT
        assert settlement.status is CheckStatus.UNRESOLVED and settlement.evidence == ()
    assert _labels(_check(forward, _N.PERSISTENCE_ACKNOWLEDGEMENT)) == [
        (_ACK.UNKNOWN, 1, 0),
        (_ACK.ACKNOWLEDGED, 1, 1),
    ]
    assert _labels(_check(reverse, _N.PERSISTENCE_ACKNOWLEDGEMENT)) == [
        (_ACK.ACKNOWLEDGED, 1, 0),
        (_ACK.UNKNOWN, 1, 1),
    ]


def test_t6_absent_acknowledgement_preserves_unavailable_and_unknown() -> None:
    """Without an acknowledged label the inventory stays visible and unresolved."""
    report = _ack_report([_ack("unavailable", at=1), _ack("unknown", at=2), _ack("unknown", at=3)])
    result = _check(report, _N.PERSISTENCE_ACKNOWLEDGEMENT)
    assert result.reason is _R.ACKNOWLEDGEMENT_NOT_OBSERVED
    assert result.status is CheckStatus.UNRESOLVED
    assert _labels(result) == [(_ACK.UNAVAILABLE, 1, 0), (_ACK.UNKNOWN, 2, 1)]
    assert [ref.seq for ref in result.evidence] == [0, 1, 2]
    witness = _check(report, _N.PERSISTENCE_SETTLEMENT_WITNESS)
    assert witness.reason is _R.PRODUCER_WITNESS_ABSENT


def test_t6_foreign_generation_matches_neither_label() -> None:
    """Acknowledged and unknown rows under another generation are foreign only."""
    report = _ack_report([_ack("acknowledged", generation=1), _ack("unknown", generation=1)])
    result = _check(report, _N.PERSISTENCE_ACKNOWLEDGEMENT)
    assert result.reason is _R.NO_MATCHING_IDENTITY
    assert _counts(result) == (0, 0, 2, 0) and _labels(result) == []
    witness = _check(report, _N.PERSISTENCE_SETTLEMENT_WITNESS)
    assert witness.reason is _R.PRODUCER_WITNESS_ABSENT


# T7 -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "outcome, reason, value",
    [
        ("recipe_target_met", _R.RECIPE_SUCCESS_UNPROVEN, _END.RECIPE_TARGET_MET),
        ("human_protection_interrupted", None, _END.HUMAN_PROTECTION_INTERRUPTED),
        ("other_interruption", None, _END.OTHER_INTERRUPTION),
        ("unknown", _R.ENDPOINT_UNKNOWN, _END.UNKNOWN),
    ],
)
def test_t7_endpoint_distinguishes_interruption_from_recipe_success(
    outcome: str, reason: DispositionReason | None, value: EndpointOutcomeValue
) -> None:
    """Only an interruption is satisfied; recipe target met stays unproven."""
    rows = [_endpoint(outcome), _endpoint(outcome, endpoint_id="endpoint_2")]
    result = _check(_report(_bytes({"endpoint": rows})), _N.ENDPOINT_DECLARATION)
    assert result.reason is reason
    assert result.status is (CheckStatus.SATISFIED if reason is None else CheckStatus.UNRESOLVED)
    assert _labels(result) == [(value, 1, 0)]
    assert _counts(result) == (1, 0, 0, 1)


@pytest.mark.parametrize(
    "rows, reason, counts",
    [
        pytest.param([_endpoint(at=1), _endpoint(at=2)], _R.CONFLICT, (2, 0, 0, 0), id="instant"),
        pytest.param(
            [_endpoint(), _endpoint("recipe_target_met")], _R.CONFLICT, (2, 0, 0, 0), id="outcome"
        ),
        pytest.param([_endpoint(), _endpoint()], None, (2, 1, 0, 0), id="exact-duplicate"),
    ],
)
def test_t7_endpoint_payload_conflicts_and_duplicates(
    rows: list[_Payload], reason: DispositionReason | None, counts: tuple[int, int, int, int]
) -> None:
    """Distinct payloads for one id are invalid; an exact duplicate is not a conflict."""
    for ordered in (rows, rows[::-1]):
        result = _check(_report(_bytes({"endpoint": ordered})), _N.ENDPOINT_DECLARATION)
        assert result.reason is reason
        assert _counts(result) == counts
        assert [ref.seq for ref in result.evidence] == [0, 1]


# T8 -------------------------------------------------------------------------------------------


def test_t8_observation_is_independent_of_command_disposition() -> None:
    """An operator observation is satisfied by its own row while the command is unknown."""
    report = _report(_bytes({"command": [_declared("unknown")]}))
    assert _check(report, _N.COMMAND_REPORTED_DISPOSITION).reason is _R.DISPOSITION_UNKNOWN
    observation = _check(report, _N.OPERATOR_OBSERVATION)
    assert observation.reason is None
    assert _labels(observation) == [(_OBS.HEAT_OFF, 1, 0)]
    assert observation.evidence == (
        EvidenceRef(
            stream=_C.OPERATOR_OBSERVATION, seq=0, instants=(Instant(clock=_AGENT, value=9.0),)
        ),
    )
    assert _check(report, _N.PHYSICAL_EFFECT_ASSOCIATION).reason is _R.PRODUCER_WITNESS_ABSENT


@pytest.mark.parametrize(
    "rows, reason",
    [
        pytest.param([_observation("unknown")], _R.EFFECT_UNKNOWN, id="unknown"),
        pytest.param([_observation(), _observation("cooling_running")], _R.CONFLICT, id="effect"),
        pytest.param([_observation(at=1), _observation(at=2)], _R.CONFLICT, id="instant"),
    ],
)
def test_t8_observation_unknown_and_conflict(
    rows: list[_Payload], reason: DispositionReason
) -> None:
    """Unknown effects stay unresolved and conflicting duplicates are invalid."""
    result = _check(_report(_bytes({"operator_observation": rows})), _N.OPERATOR_OBSERVATION)
    assert result.reason is reason
    assert result.status is DISPOSITION_REASON_STATUS[reason]


_EMPTY_ROW: _Payload = {}
_OTHER_STREAM_VARIANTS: list[_Streams] = [
    {"command": [_declared("unknown")]},
    {"command": [_declared("not_dispatched"), _returned(1)]},
    {"command": []},
    {"command": [_EMPTY_ROW]},
    {"persistence_ack": [_ack("unknown")]},
    {"persistence_ack": []},
    {"persistence_ack": [_EMPTY_ROW]},
    {"endpoint": [_endpoint("recipe_target_met")]},
    {"endpoint": []},
    {"endpoint": [_EMPTY_ROW]},
]


@pytest.mark.parametrize("changes", _OTHER_STREAM_VARIANTS)
def test_t8_observation_bytes_ignore_other_disposition_streams(changes: _Streams) -> None:
    """The observation and its witness serialize identically across other stream variations."""
    baseline = _report(_bytes())
    varied = _report(_bytes(changes))
    for check in (_N.OPERATOR_OBSERVATION, _N.PHYSICAL_EFFECT_ASSOCIATION):
        assert _check(varied, check).model_dump_json() == _check(baseline, check).model_dump_json()


# T9 -------------------------------------------------------------------------------------------


_FOREIGN: _Streams = {
    "command": [_returned(10.0, owner="owner_9")],
    "persistence_ack": [_ack(generation=9)],
    "endpoint": [_endpoint(endpoint_id="endpoint_9")],
    "operator_observation": [_observation(observation_id="observation_9")],
}


@pytest.mark.parametrize("stream", _DISPOSITION_STREAMS)
@pytest.mark.parametrize("variant", ["malformed", "absent", "foreign"])
def test_t9_each_disposition_stream_is_isolated(stream: str, variant: str) -> None:
    """Only the varied stream's selection and witness may change."""
    variants: _Streams = {"malformed": [_EMPTY_ROW], "absent": [], "foreign": _FOREIGN[stream]}
    rows = variants[variant]
    baseline = _report(_bytes())
    report = _report(_bytes({stream: rows}))
    affected = {_SELECTION[stream], _WITNESS.get(stream, _SELECTION[stream])}
    for before, after in zip(baseline.checks, report.checks, strict=True):
        if before.check not in affected:
            assert after == before
    selection = _check(report, _SELECTION[stream])
    assert selection != _check(baseline, _SELECTION[stream])
    if variant == "foreign":
        assert selection.reason is _R.NO_MATCHING_IDENTITY
        foreign = 1 if stream in ("command", "persistence_ack") else 0
        assert _counts(selection) == (0, 0, foreign, 1 - foreign)
        expected_witness = _R.PRODUCER_WITNESS_ABSENT
    else:
        expected_witness = _R.EVIDENCE_MALFORMED if variant == "malformed" else _R.EVIDENCE_ABSENT
        _assert_prerequisite(selection, expected_witness)
    if stream in _WITNESS:
        witness = _check(report, _WITNESS[stream])
        assert witness.reason is expected_witness
        assert witness.evidence == () and witness.detail is None


# T10 ------------------------------------------------------------------------------------------


_UNBOUND: dict[str, object] = {"command": None, "endpoint_id": None, "observation_id": None}
_MALFORMED: _Streams = {stream: [{}] for stream in _DISPOSITION_STREAMS}
_ABSENT: _Streams = {stream: [] for stream in _DISPOSITION_STREAMS}


@pytest.mark.parametrize(
    "streams, expect, reason",
    [
        pytest.param(_MALFORMED, _UNBOUND | {"bundle_id": "x"}, _R.SCOPE_MISMATCH, id="bundle"),
        pytest.param(_MALFORMED, _UNBOUND | {"session_id": "x"}, _R.SCOPE_MISMATCH, id="session"),
        pytest.param(_MALFORMED, _UNBOUND, _R.EVIDENCE_MALFORMED, id="malformed"),
        pytest.param(_ABSENT, _UNBOUND, _R.EXPECTATION_UNBOUND, id="unbound"),
        pytest.param(_ABSENT, {}, _R.EVIDENCE_ABSENT, id="absent"),
    ],
)
def test_t10_prerequisite_precedence(
    streams: _Streams, expect: dict[str, object], reason: DispositionReason
) -> None:
    """Scope precedes malformed, which precedes unbound, which precedes absent."""
    report = _report(_bytes(streams), _expect(**expect))
    for check in (*_SELECTION.values(), *_WITNESSES):
        _assert_prerequisite(_check(report, check), reason)
    assert _check(report, _N.OBSERVED_PROVENANCE).reason is _R.SYNTHETIC_EVIDENCE


# T11 ------------------------------------------------------------------------------------------


def _existing_reports(data: bytes) -> tuple[object, object, str, str]:
    digest = hashlib.sha256(data).hexdigest()
    synthetic = interpret_synthetic_bundle(
        data,
        expected_sha256=digest,
        expectations=SyntheticExpectations(session_id="session_1", bundle_id="bundle_1"),
    )
    coverage = interpret_coverage_bundle(
        data,
        expected_sha256=digest,
        expectations=CoverageExpectations(
            session_id="session_1",
            bundle_id="bundle_1",
            recording=RecordingBinding(
                recording_id="recording_1",
                sample_rate_hz=100,
                required_first_sample=0,
                required_end_sample=100,
            ),
        ),
    )
    assert synthetic.bundle_sha256 == digest and coverage.bundle_sha256 == digest
    return synthetic.checks, coverage.checks, synthetic.bundle_sha256, coverage.bundle_sha256


def test_t11_existing_reports_ignore_disposition_streams() -> None:
    """Existing interpreters keep their checks while each report keeps its own digest."""
    variants = [
        _bytes(),
        *(_bytes({stream: [{}]}) for stream in _DISPOSITION_STREAMS),
        _bytes(_ABSENT),
        _bytes(_FOREIGN),
    ]
    baseline = _existing_reports(variants[0])
    digests: list[str] = []
    for data in variants:
        synthetic, coverage, synthetic_digest, coverage_digest = _existing_reports(data)
        assert synthetic == baseline[0]
        assert coverage == baseline[1]
        assert synthetic_digest == coverage_digest == hashlib.sha256(data).hexdigest()
        digests.append(synthetic_digest)
    assert len(set(digests)) == len(variants)


def test_t11_disposition_checks_ignore_other_streams() -> None:
    """Varying non-disposition streams leaves every disposition check equal."""
    base = _fixture_streams() | _BASELINE
    turn = base["temperature_turn"][0] | {"bt_c": 150.0}
    variants = [
        _bytes(),
        _bytes({"temperature_turn": [{}]}),
        _bytes({"shadow_window": [], "shadow_event": []}),
        _bytes({"temperature_turn": [turn], "room_reading": []}),
        _bytes({"recording_segment": [{}], "identity": []}),
    ]
    baseline = _report(variants[0])
    digests = set[str]()
    for data in variants:
        report = _report(data)
        assert report.checks == baseline.checks
        assert report.bundle_sha256 == hashlib.sha256(data).hexdigest()
        digests.add(report.bundle_sha256)
    assert len(digests) == len(variants)


# T12 ------------------------------------------------------------------------------------------


def _assert_admissible_shape(data: bytes) -> None:
    assert len(data) <= bundle_reader.MAX_BUNDLE_BYTES
    assert max(len(line) for line in data.splitlines()) <= bundle_reader.MAX_LINE_BYTES


def test_t12a_full_legal_command_stream() -> None:
    """A full exact-identity command stream keeps every reference and one label."""
    cap = bundle_reader.MAX_STREAM_RECORDS
    data = _bytes({"command": [_returned(10.0)] * cap}, base={})
    _assert_admissible_shape(data)
    assert data.count(b"\n") - 1 == cap
    report = _report(data)
    result = _check(report, _N.COMMAND_REPORTED_DISPOSITION)
    assert result.reason is None
    assert [ref.seq for ref in result.evidence] == list(range(cap))
    assert _labels(result) == [(_CMD.RETURNED_COMMAND_STATE, cap, 0)]
    assert _counts(result) == (cap, cap - 1, 0, 0)
    assert _check(report, _N.COMMAND_INVOCATION_WITNESS).evidence == ()
    assert _check(report, _N.PERSISTENCE_ACKNOWLEDGEMENT).reason is _R.EVIDENCE_ABSENT


def test_t12b_two_full_streams_at_the_record_limit() -> None:
    """Two full streams totalling MAX_RECORDS are admitted with bounded references."""
    cap = bundle_reader.MAX_STREAM_RECORDS
    data = _bytes(
        {"operator_observation": [_observation()] * cap, "endpoint": [_endpoint()] * cap},
        base={},
    )
    _assert_admissible_shape(data)
    assert data.count(b"\n") - 1 == bundle_reader.MAX_RECORDS == 2 * cap
    report = _report(data)
    total = sum(len(result.evidence) for result in report.checks)
    assert total == bundle_reader.MAX_RECORDS
    for check in (_N.OPERATOR_OBSERVATION, _N.ENDPOINT_DECLARATION):
        result = _check(report, check)
        assert result.reason is None
        assert [ref.seq for ref in result.evidence] == list(range(cap))
        assert _counts(result) == (cap, cap - 1, 0, 0)
    assert _check(report, _N.PHYSICAL_EFFECT_ASSOCIATION).evidence == ()


@pytest.mark.parametrize(
    "streams, reason",
    [
        pytest.param(
            {"command": [_returned(10.0)] * (bundle_reader.MAX_STREAM_RECORDS + 1)},
            CollectorAdmissionReason.HEADER_INVALID,
            id="stream-cap",
        ),
        pytest.param(
            {
                "operator_observation": [_observation()] * bundle_reader.MAX_STREAM_RECORDS,
                "endpoint": [_endpoint()] * bundle_reader.MAX_STREAM_RECORDS,
                "command": [_returned(10.0)],
            },
            CollectorAdmissionReason.RECORD_LIMIT,
            id="record-limit",
        ),
    ],
)
def test_t12c_over_limit_admission_errors_propagate(
    streams: _Streams, reason: CollectorAdmissionReason
) -> None:
    """S1 refusals propagate unchanged from the new entrypoint."""
    data = _bytes(streams, base={})
    _assert_admissible_shape(data)
    with pytest.raises(CollectorAdmissionError) as caught:
        _report(data)
    assert caught.value.reason is reason


# T13 ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("value", [bytearray(b"{}"), memoryview(b"{}")])
def test_t13_nonbytes_refused_before_expectations(
    value: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-byte input is refused before expectations are revalidated or S1 is called."""

    def unexpected(*_args: object, **_kwargs: object) -> None:
        pytest.fail("admission called for invalid arguments")

    monkeypatch.setattr(bundle_reader, "read_synthetic_bundle", unexpected)
    invalid = DispositionExpectations.model_construct(session_id="")
    with pytest.raises(InterpretationInputError) as caught:
        interpret_disposition_bundle(cast(bytes, value), expected_sha256="x", expectations=invalid)
    assert caught.value.reason is InterpretationInputReason.DATA_NOT_BYTES
    assert str(caught.value) == "synthetic collector interpretation input refused"


class _Text(str):
    """A text subclass that must not pass the exact-text boundary."""


def _with_extra() -> CommandBinding:
    binding = CommandBinding(command_id="command_1", owner_id="owner_1", generation=0)
    object.__setattr__(binding, "unexpected", 1)
    return binding


@pytest.mark.parametrize(
    "binding",
    [
        pytest.param(
            lambda: CommandBinding.model_construct(command_id="bad id", owner_id="o", generation=0),
            id="invalid-token",
        ),
        pytest.param(
            lambda: CommandBinding.model_construct(command_id="c", owner_id="o", generation=-1),
            id="negative-generation",
        ),
        pytest.param(
            lambda: CommandBinding.model_construct(command_id="c", owner_id="o", generation=True),
            id="bool-generation",
        ),
        pytest.param(
            lambda: CommandBinding.model_construct(
                command_id=_Text("command_1"), owner_id="o", generation=0
            ),
            id="str-subclass",
        ),
        pytest.param(_with_extra, id="extra"),
    ],
)
def test_t13_constructed_bindings_are_revalidated(
    binding: Callable[[], CommandBinding], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Constructed nested models cannot bypass strict expectation validation."""

    def unexpected(*_args: object, **_kwargs: object) -> None:
        pytest.fail("admission called before validating expectations")

    monkeypatch.setattr(bundle_reader, "read_synthetic_bundle", unexpected)
    expect = DispositionExpectations.model_construct(
        session_id="session_1", bundle_id="bundle_1", command=binding()
    )
    with pytest.raises(InterpretationInputError) as caught:
        interpret_disposition_bundle(b"", expected_sha256="x", expectations=expect)
    assert caught.value.reason is InterpretationInputReason.EXPECTATIONS_INVALID


def test_t13_explicit_nulls_are_preserved_and_text_is_exact() -> None:
    """Explicit absent identities stay None and never become a default value."""
    fields: dict[str, object] = {"session_id": "session_1", "bundle_id": "bundle_1"}
    explicit = DispositionExpectations.model_validate(
        fields | {"command": None, "endpoint_id": None, "observation_id": None}
    )
    assert explicit == DispositionExpectations.model_validate(fields)
    assert explicit.command is None and explicit.endpoint_id is None
    partial = CommandBinding(command_id="command_1", owner_id=None, generation=None)
    assert partial.owner_id is None and partial.generation is None and not partial.bound
    assert CommandBinding(command_id="c", owner_id="o", generation=0).bound
    bads: tuple[dict[str, object], ...] = (
        {"session_id": 42},
        {"endpoint_id": _Text("e")},
        {"observation_id": ""},
    )
    for bad in bads:
        with pytest.raises(pydantic.ValidationError):
            DispositionExpectations.model_validate(fields | bad)


def test_t13_calls_admission_once_preserving_identities(monkeypatch: pytest.MonkeyPatch) -> None:
    """One S1 call forwards the exact bytes and digest; refusals propagate unchanged."""
    data = _bytes()
    digest = hashlib.sha256(data).hexdigest()
    real = bundle_reader.read_synthetic_bundle
    refused = CollectorAdmissionError(CollectorAdmissionReason.NON_CANONICAL)
    calls: list[tuple[bytes, str]] = []

    def spy(source: bytes, *, expected_sha256: str) -> AdmittedBundle:
        calls.append((source, expected_sha256))
        return real(source, expected_sha256=expected_sha256)

    monkeypatch.setattr(bundle_reader, "read_synthetic_bundle", spy)
    report = interpret_disposition_bundle(data, expected_sha256=digest, expectations=_expect())
    assert len(calls) == 1 and calls[0][0] is data and calls[0][1] is digest
    assert report.bundle_sha256 == digest

    def fail(_source: bytes, *, expected_sha256: str) -> AdmittedBundle:
        assert expected_sha256 is digest
        raise refused

    monkeypatch.setattr(bundle_reader, "read_synthetic_bundle", fail)
    with pytest.raises(CollectorAdmissionError) as caught:
        interpret_disposition_bundle(data, expected_sha256=digest, expectations=_expect())
    assert caught.value is refused


# T14 ------------------------------------------------------------------------------------------


def _ref(seq: object, stream: CollectorStream = _C.COMMAND, instants: int = 1) -> EvidenceRef:
    return EvidenceRef.model_construct(
        stream=stream, seq=seq, instants=(Instant(clock=_AGENT, value=1),) * instants
    )


def _label(value: _Value, rows: int = 1, first_seq: int = 0) -> RetainedLabel:
    return RetainedLabel(value=value, rows=rows, first_seq=first_seq)


def _detail_of(
    *labels: RetainedLabel,
    selected: int | None = None,
    duplicates: int = 0,
    foreign: int = 0,
    unrelated: int = 0,
    heats: int = 0,
    heat: int | float | None = None,
) -> DispositionSelectionDetail:
    return DispositionSelectionDetail(
        selected_rows=sum(label.rows for label in labels) if selected is None else selected,
        exact_duplicate_rows=duplicates,
        foreign_identity_rows=foreign,
        unrelated_rows=unrelated,
        labels=labels,
        distinct_returned_heat_values=heats,
        returned_heat_percent=heat,
    )


def _result(
    check: DispositionCheckName,
    reason: DispositionReason | None,
    evidence: tuple[EvidenceRef, ...] = (),
    detail: Callable[[], DispositionSelectionDetail | None] = lambda: None,
    status: CheckStatus | None = None,
) -> DispositionCheckResult:
    if status is None:
        status = CheckStatus.SATISFIED if reason is None else DISPOSITION_REASON_STATUS[reason]
    return DispositionCheckResult(
        check=check, status=status, reason=reason, evidence=evidence, detail=detail()
    )


_RET = _CMD.RETURNED_COMMAND_STATE
_CMD_CHECK = _N.COMMAND_REPORTED_DISPOSITION
_ACK_CHECK = _N.PERSISTENCE_ACKNOWLEDGEMENT
_END_CHECK = _N.ENDPOINT_DECLARATION
_OBS_CHECK = _N.OPERATOR_OBSERVATION


def _returned_detail(
    *, selected: int | None = None, duplicates: int = 0, heat: int | float | None = 10
) -> DispositionSelectionDetail:
    return _detail_of(_label(_RET), selected=selected, duplicates=duplicates, heats=1, heat=heat)


def _valid_results() -> list[DispositionCheckResult]:
    end, obs = _C.ENDPOINT, _C.OPERATOR_OBSERVATION
    return [
        _result(_CMD_CHECK, None, (_ref(0),), _returned_detail),
        _result(
            _CMD_CHECK,
            _R.CONFLICT,
            (_ref(0), _ref(1)),
            lambda: _detail_of(_label(_RET, 2), heats=2),
        ),
        _result(
            _CMD_CHECK,
            _R.DISPOSITION_UNKNOWN,
            (_ref(0), _ref(1)),
            lambda: _detail_of(_label(_CMD.UNKNOWN), _label(_RET, 1, 1), heats=1, heat=5),
        ),
        _result(_CMD_CHECK, _R.NO_MATCHING_IDENTITY, (), lambda: _detail_of(foreign=1)),
        _result(
            _ACK_CHECK,
            None,
            (_ref(0, _C.PERSISTENCE_ACK), _ref(1, _C.PERSISTENCE_ACK)),
            lambda: _detail_of(_label(_ACK.UNKNOWN), _label(_ACK.ACKNOWLEDGED, 1, 1)),
        ),
        _result(
            _ACK_CHECK,
            _R.ACKNOWLEDGEMENT_NOT_OBSERVED,
            (_ref(0, _C.PERSISTENCE_ACK),),
            lambda: _detail_of(_label(_ACK.UNAVAILABLE)),
        ),
        _result(
            _END_CHECK,
            None,
            (_ref(0, end), _ref(1, end)),
            lambda: _detail_of(_label(_END.OTHER_INTERRUPTION, 2), duplicates=1, unrelated=3),
        ),
        _result(
            _END_CHECK,
            _R.CONFLICT,
            (_ref(0, end), _ref(1, end)),
            lambda: _detail_of(_label(_END.HUMAN_PROTECTION_INTERRUPTED, 2)),
        ),
        _result(
            _END_CHECK,
            _R.RECIPE_SUCCESS_UNPROVEN,
            (_ref(0, end),),
            lambda: _detail_of(_label(_END.RECIPE_TARGET_MET)),
        ),
        _result(
            _OBS_CHECK,
            _R.EFFECT_UNKNOWN,
            (_ref(4, obs),),
            lambda: _detail_of(_label(_OBS.UNKNOWN, 1, 4)),
        ),
        _result(_N.COMMAND_INVOCATION_WITNESS, _R.PRODUCER_WITNESS_ABSENT),
        _result(_N.PHYSICAL_EFFECT_ASSOCIATION, _R.EVIDENCE_ABSENT),
        _result(_N.OBSERVED_PROVENANCE, _R.SYNTHETIC_EVIDENCE),
        _result(
            _CMD_CHECK,
            None,
            (_ref(0), _ref(bundle_reader.MAX_SEQUENCE)),
            lambda: _detail_of(_label(_RET, 2), heats=1, heat=0),
        ),
    ]


def test_t14_valid_constructions_are_accepted() -> None:
    """The contradictory cases below each differ from an accepted construction."""
    results = _valid_results()
    assert len(results) == 14
    assert _detail(results[-1]).returned_heat_percent == 0


_Invalid = Callable[[], object]
_INVALID: list[tuple[str, _Invalid]] = [
    (
        "status-mismatch",
        lambda: _result(_CMD_CHECK, None, (_ref(0),), _returned_detail, CheckStatus.UNRESOLVED),
    ),
    (
        "unreachable-reason",
        lambda: _result(_CMD_CHECK, _R.ENDPOINT_UNKNOWN, (_ref(0),), _returned_detail),
    ),
    (
        "witness-detail",
        lambda: _result(
            _N.COMMAND_INVOCATION_WITNESS,
            _R.PRODUCER_WITNESS_ABSENT,
            (),
            lambda: _detail_of(foreign=1),
        ),
    ),
    (
        "prerequisite-detail",
        lambda: _result(_CMD_CHECK, _R.EVIDENCE_ABSENT, (), lambda: _detail_of(foreign=1)),
    ),
    ("missing-detail", lambda: _result(_CMD_CHECK, None, (_ref(0),))),
    (
        "wrong-label-vocabulary",
        lambda: _result(
            _CMD_CHECK, None, (_ref(0),), lambda: _detail_of(_label(_ACK.ACKNOWLEDGED))
        ),
    ),
    (
        "mixed-label-vocabulary",
        lambda: _detail_of(_label(_CMD.UNKNOWN), _label(_ACK.UNKNOWN, 1, 1)),
    ),
    ("duplicate-labels", lambda: _detail_of(_label(_RET), _label(_RET, 1, 1), heats=1, heat=1)),
    ("unordered-first-seq", lambda: _detail_of(_label(_CMD.UNKNOWN, 1, 1), _label(_RET), heats=1)),
    (
        "selected-not-evidence",
        lambda: _result(_CMD_CHECK, None, (_ref(0), _ref(1)), _returned_detail),
    ),
    ("selected-not-label-rows", lambda: _returned_detail(selected=2)),
    (
        "first-seq-not-referenced",
        lambda: _result(
            _CMD_CHECK, None, (_ref(0),), lambda: _detail_of(_label(_RET, 1, 5), heats=1, heat=1)
        ),
    ),
    (
        "wrong-ref-stream",
        lambda: _result(_CMD_CHECK, None, (_ref(0, _C.PERSISTENCE_ACK),), _returned_detail),
    ),
    ("zero-instants", lambda: _result(_CMD_CHECK, None, (_ref(0, instants=0),), _returned_detail)),
    ("two-instants", lambda: _result(_CMD_CHECK, None, (_ref(0, instants=2),), _returned_detail)),
    (
        "repeated-seq",
        lambda: _result(
            _CMD_CHECK,
            None,
            (_ref(0), _ref(0)),
            lambda: _detail_of(_label(_RET, 2), heats=1, heat=1),
        ),
    ),
    (
        "decreasing-seq",
        lambda: _result(
            _CMD_CHECK,
            None,
            (_ref(1), _ref(0)),
            lambda: _detail_of(_label(_RET, 2), heats=1, heat=1),
        ),
    ),
    (
        "negative-seq",
        lambda: _result(
            _CMD_CHECK,
            None,
            (_ref(-1), _ref(0)),
            lambda: _detail_of(_label(_RET, 2), heats=1, heat=1),
        ),
    ),
    (
        "bool-seq",
        lambda: _result(
            _CMD_CHECK,
            None,
            (_ref(0), _ref(True)),
            lambda: _detail_of(_label(_RET, 2), heats=1, heat=1),
        ),
    ),
    (
        "seq-beyond-s1-bound",
        lambda: _result(
            _CMD_CHECK,
            None,
            (_ref(0), _ref(bundle_reader.MAX_SEQUENCE + 1)),
            lambda: _detail_of(_label(_RET, 2), heats=1, heat=1),
        ),
    ),
    (
        "endpoint-foreign",
        lambda: _result(
            _END_CHECK,
            None,
            (_ref(0, _C.ENDPOINT),),
            lambda: _detail_of(_label(_END.OTHER_INTERRUPTION), foreign=1),
        ),
    ),
    (
        "observation-foreign",
        lambda: _result(
            _OBS_CHECK,
            _R.NO_MATCHING_IDENTITY,
            (),
            lambda: _detail_of(foreign=1),
        ),
    ),
    (
        "no-match-with-selection",
        lambda: _result(_CMD_CHECK, _R.NO_MATCHING_IDENTITY, (_ref(0),), _returned_detail),
    ),
    (
        "selection-zero-without-no-match",
        lambda: _result(
            _ACK_CHECK, _R.ACKNOWLEDGEMENT_NOT_OBSERVED, (), lambda: _detail_of(foreign=1)
        ),
    ),
    (
        "satisfied-command-unknown",
        lambda: _result(_CMD_CHECK, None, (_ref(0),), lambda: _detail_of(_label(_CMD.UNKNOWN))),
    ),
    (
        "conflict-one-claim",
        lambda: _result(_CMD_CHECK, _R.CONFLICT, (_ref(0),), _returned_detail),
    ),
    (
        "unknown-without-unknown-label",
        lambda: _result(_CMD_CHECK, _R.DISPOSITION_UNKNOWN, (_ref(0),), _returned_detail),
    ),
    (
        "unknown-masking-conflict",
        lambda: _result(
            _CMD_CHECK,
            _R.DISPOSITION_UNKNOWN,
            (_ref(0), _ref(1), _ref(2)),
            lambda: _detail_of(
                _label(_CMD.UNKNOWN),
                _label(_CMD.NOT_DISPATCHED, 1, 1),
                _label(_CMD.SUPERSEDED, 1, 2),
            ),
        ),
    ),
    (
        "satisfied-ack-without-acknowledged",
        lambda: _result(
            _ACK_CHECK,
            None,
            (_ref(0, _C.PERSISTENCE_ACK),),
            lambda: _detail_of(_label(_ACK.UNKNOWN)),
        ),
    ),
    (
        "ack-not-observed-with-acknowledged",
        lambda: _result(
            _ACK_CHECK,
            _R.ACKNOWLEDGEMENT_NOT_OBSERVED,
            (_ref(0, _C.PERSISTENCE_ACK),),
            lambda: _detail_of(_label(_ACK.ACKNOWLEDGED)),
        ),
    ),
    (
        "satisfied-recipe-target-met",
        lambda: _result(
            _END_CHECK,
            None,
            (_ref(0, _C.ENDPOINT),),
            lambda: _detail_of(_label(_END.RECIPE_TARGET_MET)),
        ),
    ),
    (
        "endpoint-conflict-one-payload",
        lambda: _result(
            _END_CHECK,
            _R.CONFLICT,
            (_ref(0, _C.ENDPOINT), _ref(1, _C.ENDPOINT)),
            lambda: _detail_of(_label(_END.UNKNOWN, 2), duplicates=1),
        ),
    ),
    (
        "endpoint-satisfied-two-payloads",
        lambda: _result(
            _END_CHECK,
            None,
            (_ref(0, _C.ENDPOINT), _ref(1, _C.ENDPOINT)),
            lambda: _detail_of(_label(_END.OTHER_INTERRUPTION, 2)),
        ),
    ),
    (
        "satisfied-observation-unknown",
        lambda: _result(
            _OBS_CHECK,
            None,
            (_ref(0, _C.OPERATOR_OBSERVATION),),
            lambda: _detail_of(_label(_OBS.UNKNOWN)),
        ),
    ),
    (
        "heat-on-ack",
        lambda: _result(
            _ACK_CHECK,
            None,
            (_ref(0, _C.PERSISTENCE_ACK),),
            lambda: _detail_of(_label(_ACK.ACKNOWLEDGED), heats=1, heat=10),
        ),
    ),
    (
        "heat-without-returned",
        lambda: _result(
            _CMD_CHECK,
            None,
            (_ref(0),),
            lambda: _detail_of(_label(_CMD.NOT_DISPATCHED), heats=1, heat=10),
        ),
    ),
    (
        "returned-without-heat",
        lambda: _result(_CMD_CHECK, None, (_ref(0),), lambda: _detail_of(_label(_RET))),
    ),
    (
        "heat-count-beyond-returned-rows",
        lambda: _result(
            _CMD_CHECK, _R.CONFLICT, (_ref(0),), lambda: _detail_of(_label(_RET), heats=2)
        ),
    ),
    (
        "heat-percent-with-two-values",
        lambda: _result(
            _CMD_CHECK,
            _R.CONFLICT,
            (_ref(0), _ref(1)),
            lambda: _detail_of(_label(_RET, 2), heats=2, heat=10),
        ),
    ),
    ("heat-out-of-range", lambda: _returned_detail(heat=100.5)),
    ("heat-not-finite", lambda: _returned_detail(heat=float("nan"))),
    ("heat-bool", lambda: _returned_detail(heat=True)),
    (
        "witness-evidence",
        lambda: _result(_N.COMMAND_INVOCATION_WITNESS, _R.PRODUCER_WITNESS_ABSENT, (_ref(0),)),
    ),
    (
        "provenance-evidence",
        lambda: _result(_N.OBSERVED_PROVENANCE, _R.SYNTHETIC_EVIDENCE, (_ref(0),)),
    ),
    ("prerequisite-evidence", lambda: _result(_CMD_CHECK, _R.EVIDENCE_ABSENT, (_ref(0),))),
    ("duplicates-equal-selected", lambda: _returned_detail(duplicates=1)),
    ("empty-stream-inventory", lambda: _detail_of()),
    (
        "stream-inventory-beyond-cap",
        lambda: _detail_of(foreign=bundle_reader.MAX_STREAM_RECORDS, unrelated=1),
    ),
    ("label-zero-rows", lambda: _label(_RET, 0)),
    (
        "label-string-value",
        lambda: RetainedLabel.model_validate({"value": "unknown", "rows": 1, "first_seq": 0}),
    ),
    ("five-labels", lambda: _detail_of(*(_label(_RET, 1, seq) for seq in range(5)), heats=1)),
]


@pytest.mark.parametrize("build", [pytest.param(build, id=name) for name, build in _INVALID])
def test_t14_contradictory_results_are_rejected(build: _Invalid) -> None:
    """Every contradictory public construction is rejected by the closed validators."""
    with pytest.raises(pydantic.ValidationError):
        build()


def _report_fields(**changes: object) -> dict[str, object]:
    return dict(vars(_report(_bytes()))) | changes


def _replace(check: DispositionCheckName, result: DispositionCheckResult) -> dict[str, object]:
    checks = tuple(
        result if existing.check is check else existing for existing in _report(_bytes()).checks
    )
    return _report_fields(checks=checks)


def _full_owned(check: DispositionCheckName, stream: CollectorStream) -> DispositionCheckResult:
    cap = bundle_reader.MAX_STREAM_RECORDS
    value: _Value = _RET if check is _CMD_CHECK else _ACK.ACKNOWLEDGED
    heats = (1, 10) if check is _CMD_CHECK else (0, None)
    return DispositionCheckResult(
        check=check,
        status=CheckStatus.SATISFIED,
        reason=None,
        evidence=tuple(
            EvidenceRef(stream=stream, seq=seq, instants=(Instant(clock=_AGENT, value=0),))
            for seq in range(cap)
        ),
        detail=_detail_of(_label(value, cap), heats=heats[0], heat=heats[1]),
    )


def _bounded_checks(endpoint_refs: int) -> tuple[DispositionCheckResult, ...]:
    end = _C.ENDPOINT
    endpoint = (
        _result(
            _END_CHECK, None, (_ref(0, end),), lambda: _detail_of(_label(_END.OTHER_INTERRUPTION))
        )
        if endpoint_refs
        else _result(_END_CHECK, _R.NO_MATCHING_IDENTITY, (), lambda: _detail_of(unrelated=1))
    )
    replacements = {
        _CMD_CHECK: _full_owned(_CMD_CHECK, _C.COMMAND),
        _ACK_CHECK: _full_owned(_ACK_CHECK, _C.PERSISTENCE_ACK),
        _END_CHECK: endpoint,
        _OBS_CHECK: _result(
            _OBS_CHECK, _R.NO_MATCHING_IDENTITY, (), lambda: _detail_of(unrelated=1)
        ),
    }
    return tuple(replacements.get(result.check, result) for result in _report(_bytes()).checks)


def test_t14_report_total_references_are_bounded() -> None:
    """Total references may reach MAX_RECORDS but never exceed it."""
    accepted = DispositionReport.model_validate(_report_fields(checks=_bounded_checks(0)))
    assert sum(len(result.evidence) for result in accepted.checks) == bundle_reader.MAX_RECORDS
    with pytest.raises(pydantic.ValidationError, match="bounded total evidence"):
        DispositionReport.model_validate(_report_fields(checks=_bounded_checks(1)))


_REPORT_INVALID: list[tuple[str, Callable[[], dict[str, object]]]] = [
    (
        "witness-not-following-prerequisite",
        lambda: _replace(
            _N.COMMAND_INVOCATION_WITNESS,
            _result(_N.COMMAND_INVOCATION_WITNESS, _R.EVIDENCE_ABSENT),
        ),
    ),
    (
        "witness-absent-under-prerequisite",
        lambda: _replace(_ACK_CHECK, _result(_ACK_CHECK, _R.EVIDENCE_MALFORMED)),
    ),
    (
        "reordered-checks",
        lambda: _report_fields(checks=tuple(reversed(_report(_bytes()).checks))),
    ),
    ("missing-check", lambda: _report_fields(checks=_report(_bytes()).checks[:-1])),
    (
        "truncated-adapter-limits",
        lambda: _report_fields(adapter_limits=tuple(DispositionAdapterLimit)[:-1]),
    ),
    ("authority", lambda: _report_fields(authority="none")),
    ("provenance", lambda: _report_fields(provenance=ProvenanceClass.SYNTHETIC_UNATTESTED.value)),
    ("schema-version", lambda: _report_fields(report_schema_version=2)),
    ("digest", lambda: _report_fields(bundle_sha256="A" * 64)),
    ("extra-field", lambda: _report_fields(qualified=True)),
]


@pytest.mark.parametrize(
    "fields", [pytest.param(build, id=name) for name, build in _REPORT_INVALID]
)
def test_t14_contradictory_reports_are_rejected(fields: Callable[[], dict[str, object]]) -> None:
    """Report-level cross-checks reject inconsistent witness, order, inventory and constants."""
    assert DispositionReport.model_validate(_report_fields()) == _report(_bytes())
    with pytest.raises(pydantic.ValidationError):
        DispositionReport.model_validate(fields())


def test_t14_vocabularies_mirror_v1_literals_and_status_map() -> None:
    """Plain enums carry exactly the S1 literals; every reason maps to one status."""
    pairs: list[tuple[type[_Value], object]] = [
        (CommandDispositionValue, CommandPayload.model_fields["disposition"].annotation),
        (AckStatusValue, PersistenceAckPayload.model_fields["status"].annotation),
        (EndpointOutcomeValue, EndpointPayload.model_fields["outcome"].annotation),
        (ObservedEffectValue, OperatorObservationPayload.model_fields["effect"].annotation),
    ]
    for enum_type, literal in pairs:
        assert [member.value for member in enum_type] == list(get_args(literal))
        assert all(type(member) is enum_type for member in enum_type)
        assert not issubclass(enum_type, str)
    assert set(DISPOSITION_REASON_STATUS) == set(DispositionReason)
    assert set(DISPOSITION_CHECK_STREAM) == set(DispositionCheckName)
    assert DISPOSITION_CHECK_STREAM[_N.OBSERVED_PROVENANCE] is None
    for left, right in combinations(
        (CommandDispositionValue.UNKNOWN, AckStatusValue.UNKNOWN, EndpointOutcomeValue.UNKNOWN), 2
    ):
        assert left != right


# T15 ------------------------------------------------------------------------------------------


_PREFIX = "roastpilot_agent.diagnostic_collector."
_ALLOWED_IMPORTS = {"enum", "typing", "pydantic"} | {
    _PREFIX + name
    for name in ("bundle_schema", "bundle_reader", "interpretation_schema", "disposition_schema")
}


@pytest.mark.parametrize(
    "module, classes, functions",
    [
        (
            "disposition_schema",
            {
                "CommandBinding",
                "DispositionExpectations",
                "CommandDispositionValue",
                "AckStatusValue",
                "EndpointOutcomeValue",
                "ObservedEffectValue",
                "DispositionCheckName",
                "DispositionReason",
                "DispositionAdapterLimit",
                "DispositionReportName",
                "RetainedLabel",
                "DispositionSelectionDetail",
                "DispositionCheckResult",
                "DispositionReport",
            },
            set[str](),
        ),
        ("disposition_interpreter", set[str](), {"interpret_disposition_bundle"}),
    ],
)
def test_t15_pure_module_boundaries(module: str, classes: set[str], functions: set[str]) -> None:
    """New modules import only the closed collector surface and hard-code no S1 cap."""
    path = Path(disposition_schema.__file__).with_name(module + ".py")
    tree = ast.parse(path.read_bytes())
    forbidden_calls = {"open", "eval", "exec", "compile", "__import__", "range", "import_module"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(alias.name in _ALLOWED_IMPORTS for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module in _ALLOWED_IMPORTS
            assert not any(alias.name.startswith("_") for alias in node.names)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name | ast.Attribute):
            name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr
            assert name not in forbidden_calls
        elif isinstance(node, ast.Constant):
            assert node.value not in (8192, 16384, 4096) or type(node.value) is not int
    source = path.read_text()
    for banned in ("_clock_projection", "coverage_", "diagnostic_collector.interpreter"):
        assert banned not in source
    assert {
        node.name
        for node in tree.body
        if isinstance(node, ast.ClassDef) and not node.name.startswith("_")
    } == classes
    assert {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and not node.name.startswith("_")
    } == functions


def test_t15_fresh_process_has_no_control_imports() -> None:
    """Transitive imports from the disposition entrypoint stay offline and control-free."""
    code = """
import sys
from pathlib import Path
import roastpilot_agent.diagnostic_collector.disposition_interpreter as reviewed
assert reviewed.__file__ is not None
assert Path(reviewed.__file__).resolve() == Path(sys.argv[1]).resolve()
blocked = {'controller', 'safety', 'mcp_client', 'live', 'mcp'}
assert blocked.isdisjoint(name.rsplit('.', 1)[-1] for name in sys.modules)
assert not any(name.startswith(('mcp.', 'roastpilot_agent.live')) for name in sys.modules)
"""
    source = (
        Path(__file__).resolve().parents[1]
        / "src/roastpilot_agent/diagnostic_collector/disposition_interpreter.py"
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", code, str(source)],
        check=False,
        capture_output=True,
        text=True,
        env={"PATH": os.defpath},
    )
    assert result.returncode == 0, result.stderr
