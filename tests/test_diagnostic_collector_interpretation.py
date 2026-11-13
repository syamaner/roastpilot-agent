"""Behavioural contract tests for D230 S2a synthetic interpretation."""

import ast
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import cast

import pydantic
import pytest

import roastpilot_agent.diagnostic_collector as collector
from roastpilot_agent.diagnostic_collector import (
    AdmittedBundle,
    CollectorAdmissionError,
    CollectorAdmissionReason,
    CollectorAuthority,
    CollectorStream,
    InterpretationInputError,
    InterpretationInputReason,
    InterpretationReport,
    SyntheticExpectations,
    bundle_reader,
    interpret_synthetic_bundle,
    interpretation_schema,
    interpreter,
)
from roastpilot_agent.diagnostic_collector.bundle_schema import ClockName, Instant
from roastpilot_agent.diagnostic_collector.interpretation_schema import (
    MAX_COORDINATE,
    REASON_STATUS,
    AdapterLimit,
    CelsiusRange,
    CheckName,
    CheckReason,
    CheckResult,
    CheckStatus,
    ClockScale,
    ComponentIdentity,
    ProvenanceClass,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "d230_collector" / "synthetic-v1.jsonl"
_A = ClockName.AGENT_MONOTONIC_SECONDS
_M = ClockName.MCP_SESSION_SECONDS
_S = ClockName.RECORDING_SAMPLES
_U = ClockName.UTC_EPOCH_SECONDS
_Lines = list[dict[str, object]]


def _instant(value: int | float, clock: ClockName = _A) -> dict[str, object]:
    return {"clock": clock.value, "value": value}


def _anchor(
    source: ClockName, source_value: int | float, target: ClockName, target_value: int | float
) -> dict[str, object]:
    return {
        "source_clock": source.value,
        "source_value": source_value,
        "target_clock": target.value,
        "target_value": target_value,
    }


def _object(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def _row(lines: _Lines, stream: str) -> dict[str, object]:
    return next(line for line in lines[1:] if line["stream"] == stream)


def _payload(lines: _Lines, stream: str) -> dict[str, object]:
    return _object(_row(lines, stream)["payload"])


def _baseline() -> _Lines:
    lines = [
        cast(dict[str, object], json.loads(line)) for line in _FIXTURE.read_bytes().splitlines()
    ]
    _payload(lines, "shadow_window").update(decision_at=_instant(5.0))
    return lines


def _bytes(lines: _Lines) -> bytes:
    counts = dict.fromkeys((stream.value for stream in CollectorStream), 0)
    for row in lines[1:]:
        counts[cast(str, row["stream"])] += 1
    header = {**lines[0], "inventory": counts}
    return b"".join(
        json.dumps(line, sort_keys=True, separators=(",", ":"), allow_nan=False).encode() + b"\n"
        for line in (header, *lines[1:])
    )


def _append(lines: _Lines, stream: str, seq: int, **payload: object) -> None:
    lines.append(
        {**_row(lines, stream), "seq": seq, "payload": {**_payload(lines, stream), **payload}}
    )


def _expect(**changes: object) -> SyntheticExpectations:
    raw: dict[str, object] = {
        "session_id": "session_1",
        "bundle_id": "bundle_1",
        "component": ComponentIdentity.model_validate(_payload(_baseline(), "identity")),
        "evaluation_at": Instant(clock=_A, value=10),
        "max_temperature_age_seconds": 9,
        "bt_range_c": CelsiusRange(low_c=100, high_c=110),
        "et_range_c": CelsiusRange(low_c=110, high_c=120),
        "room_range_c": CelsiusRange(low_c=15, high_c=25),
        "max_room_age_seconds": 8,
        "placement_id": "room_1",
        "recording_sample_rate_hz": 100,
    }
    return SyntheticExpectations.model_validate({**raw, **changes})


def _report(lines: _Lines, expected: SyntheticExpectations | None = None) -> InterpretationReport:
    data = _bytes(lines)
    report = interpret_synthetic_bundle(
        data, expected_sha256=hashlib.sha256(data).hexdigest(), expectations=expected or _expect()
    )
    observed = _check(report, CheckName.OBSERVED_PROVENANCE)
    assert observed.reason is CheckReason.SYNTHETIC_EVIDENCE
    assert observed.status is CheckStatus.UNRESOLVED
    assert observed.evidence == ()
    assert report.provenance is ProvenanceClass.SYNTHETIC_UNATTESTED
    return report


def _check(report: InterpretationReport, check: CheckName) -> CheckResult:
    return next(result for result in report.checks if result.check is check)


def test_b1_report_schema_order_exports_and_original_evidence() -> None:
    """Pin the public report fields, independent outcomes, and original instants."""

    report = _report(_baseline())
    assert report.report_schema_version == 1
    assert report.bundle_sha256 == hashlib.sha256(_bytes(_baseline())).hexdigest()
    assert report.authority is CollectorAuthority.NONE
    assert tuple(item.check for item in report.checks) == (
        CheckName.IDENTITY,
        CheckName.TEMPERATURE_PLAUSIBILITY,
        CheckName.TEMPERATURE_AGE,
        CheckName.ROOM_AGE,
        CheckName.ROOM_PLACEMENT,
        CheckName.HUMAN_FC,
        CheckName.SHADOW_TIMING,
        CheckName.SHADOW_CONFIRMATION,
        CheckName.OBSERVED_PROVENANCE,
    )
    assert report.adapter_limits == tuple(AdapterLimit)
    assert report.model_dump().keys() == {
        "report_schema_version",
        "bundle_sha256",
        "authority",
        "provenance",
        "adapter_limits",
        "checks",
    }
    assert all(item.status is CheckStatus.SATISFIED for item in report.checks[:-1])
    assert all(
        item.model_dump().keys()
        == {
            "check",
            "status",
            "reason",
            "evidence",
            "derived_seconds",
        }
        for item in report.checks
    )
    assert [item.derived_seconds for item in report.checks] == [
        None,
        None,
        9.0,
        8.0,
        None,
        None,
        1.0,
        None,
        None,
    ]
    confirmation = _check(report, CheckName.SHADOW_CONFIRMATION)
    assert [
        (ref.stream, ref.seq, tuple(point.value for point in ref.instants))
        for ref in confirmation.evidence
    ] == [
        (CollectorStream.SHADOW_EVENT, 0, (4.0, 5.0)),
        (CollectorStream.SHADOW_WINDOW, 0, (5.0, 6.0, 5.0)),
    ]
    assert confirmation.evidence[0].model_dump().keys() == {"stream", "seq", "instants"}
    assert collector.__all__ == [
        "AdmittedBundle",
        "CollectorAdmissionError",
        "CollectorAdmissionReason",
        "CollectorAuthority",
        "CollectorStream",
        "CollectorStreamReason",
        "CollectorStreamState",
        "InterpretationInputError",
        "InterpretationInputReason",
        "InterpretationReport",
        "SyntheticExpectations",
        "interpret_synthetic_bundle",
        "read_synthetic_bundle",
    ]

    for override in ({"adapter_limits": ()}, {"checks": report.checks[::-1]}):
        with pytest.raises(pydantic.ValidationError):
            InterpretationReport.model_validate({**vars(report), **override})


def test_b2_admits_once_with_original_bytes_and_unchanged_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The byte boundary delegates once and passes through the original error."""

    calls: list[tuple[bytes, str]] = []
    read = bundle_reader.read_synthetic_bundle

    def spy(data: bytes, *, expected_sha256: str) -> AdmittedBundle:
        calls.append((data, expected_sha256))
        return read(data, expected_sha256=expected_sha256)

    monkeypatch.setattr(bundle_reader, "read_synthetic_bundle", spy)
    original = _bytes(_baseline())
    for data, digest, reason in (
        (original, hashlib.sha256(original).hexdigest(), None),
        (original, "0" * 64, CollectorAdmissionReason.DIGEST_MISMATCH),
        (
            original[:-1],
            hashlib.sha256(original[:-1]).hexdigest(),
            CollectorAdmissionReason.FRAMING,
        ),
    ):
        calls.clear()
        if reason is None:
            assert interpret_synthetic_bundle(data, expected_sha256=digest, expectations=_expect())
        else:
            with pytest.raises(CollectorAdmissionError) as raised:
                interpret_synthetic_bundle(data, expected_sha256=digest, expectations=_expect())
            assert raised.value.reason is reason
        assert len(calls) == 1
        assert calls[0][0] is data
        assert calls[0][1] is digest

    sentinel = CollectorAdmissionError(CollectorAdmissionReason.FRAMING)

    def refuse(data: bytes, *, expected_sha256: str) -> AdmittedBundle:
        raise sentinel

    monkeypatch.setattr(bundle_reader, "read_synthetic_bundle", refuse)
    with pytest.raises(CollectorAdmissionError) as raised:
        interpret_synthetic_bundle(original, expected_sha256=digest, expectations=_expect())
    assert raised.value is sentinel


def test_b3_rejects_nonbytes_and_constructed_expectations_before_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neither admitted objects nor validation bypasses cross the byte boundary."""

    data = _bytes(_baseline())
    digest = hashlib.sha256(data).hexdigest()
    admitted = bundle_reader.read_synthetic_bundle(data, expected_sha256=digest)

    def forbidden(data: bytes, *, expected_sha256: str) -> AdmittedBundle:
        pytest.fail("admission called for an invalid argument")

    monkeypatch.setattr(bundle_reader, "read_synthetic_bundle", forbidden)
    for substitute in (admitted, bytearray(data), memoryview(data)):
        with pytest.raises(InterpretationInputError) as raised:
            interpret_synthetic_bundle(
                cast(bytes, substitute), expected_sha256=digest, expectations=_expect()
            )
        assert raised.value.reason is InterpretationInputReason.DATA_NOT_BYTES

    component = ComponentIdentity.model_validate(_payload(_baseline(), "identity"))
    for invalid in (
        SyntheticExpectations.model_construct(session_id="bad\n", bundle_id="bundle_1"),
        _expect().model_copy(update={"component": component.model_copy(update={"unexpected": 1})}),
        _expect().model_copy(
            update={"component": ComponentIdentity.model_construct(stream_id="x")}
        ),
        _expect().model_copy(
            update={"evaluation_at": Instant.model_construct(clock=_S, value=1.5)}
        ),
        _expect().model_copy(
            update={"evaluation_at": Instant.model_construct(clock=_A, value=True)}
        ),
        _expect().model_copy(
            update={"bt_range_c": CelsiusRange.model_construct(low_c=2, high_c=1)}
        ),
        _expect().model_copy(
            update={
                "clock_scales": (
                    ClockScale.model_construct(
                        source_clock=_A,
                        target_clock=_U,
                        target_per_source=0,
                        valid_from=0,
                        valid_to=20,
                    ),
                )
            }
        ),
    ):
        with pytest.raises(InterpretationInputError) as raised:
            interpret_synthetic_bundle(data, expected_sha256=digest, expectations=invalid)
        assert raised.value.reason is InterpretationInputReason.EXPECTATIONS_INVALID
        assert str(raised.value) == "synthetic collector interpretation input refused"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("component", {**_payload(_baseline(), "identity"), "surplus": True}),
        ("component", {**_payload(_baseline(), "identity"), "agent_sha256": "A" * 64}),
        ("placement_id", "á"),
        ("session_id", b"session_1"),
        ("max_room_age_seconds", -1),
        ("max_temperature_age_seconds", True),
        ("max_temperature_age_seconds", float("inf")),
        ("recording_sample_rate_hz", 1.0),
        ("recording_sample_rate_hz", 2**31),
        ("evaluation_at", {"clock": _A, "value": 0, "unknown": 0}),
        ("evaluation_at", {"clock": _A, "value": 2**53}),
        ("evaluation_at", {"clock": _A.value, "value": 0}),
        ("clock_scales", []),
        ("clock_scales", (None,) * 4),
        (
            "clock_scales",
            (
                ClockScale(
                    source_clock=_A, target_clock=_M, target_per_source=1, valid_from=0, valid_to=1
                ),
                ClockScale(
                    source_clock=_M, target_clock=_A, target_per_source=1, valid_from=0, valid_to=1
                ),
            ),
        ),
    ],
)
def test_expectations_validate_closed_raw_values(field: str, value: object) -> None:
    """Nested fields and all scalar bounds are checked without coercion."""

    with pytest.raises(pydantic.ValidationError):
        _expect(**{field: value})


def test_omitted_and_null_expectations_are_unbound() -> None:
    """Null and omitted optional fields have identical semantics."""

    omitted = SyntheticExpectations(session_id="session_1", bundle_id="bundle_1")
    explicit = _expect(
        **{
            field: None
            for field in SyntheticExpectations.model_fields
            if field not in {"session_id", "bundle_id"}
        }
    )
    assert omitted == explicit
    report = _report(_baseline(), explicit)
    assert [item.reason for item in report.checks[:5]] == [CheckReason.EXPECTATION_UNBOUND] * 5
    assert all(item.evidence for item in report.checks[:5])


def test_b4_exact_scope_and_component_identity() -> None:
    """Identity uses separately bound scope, digests, and matching repeat records."""

    lines = _baseline()
    _append(lines, "identity", 1)
    assert _check(_report(lines), CheckName.IDENTITY).status is CheckStatus.SATISFIED
    component = cast(ComponentIdentity, _expect().component)
    mismatch = _expect(component=component.model_copy(update={"sdk_sha256": "e" * 64}))
    result = _check(_report(lines, mismatch), CheckName.IDENTITY)
    assert result.reason is CheckReason.EXPECTATION_MISMATCH
    _append(lines, "identity", 2, sdk_sha256="e" * 64)
    assert _check(_report(lines, mismatch), CheckName.IDENTITY).reason is CheckReason.CONFLICT
    for field in ("session_id", "bundle_id"):
        results = _report(lines, _expect(**{field: "other"})).checks
        assert all(item.reason is CheckReason.SCOPE_MISMATCH for item in results[:-1])
        assert results[0].evidence[2].seq == 2


def test_b5_temperature_checks_use_last_accepted_and_exact_inclusive_ranges() -> None:
    """Later invalid rows do not refresh the selected pair or advance chronology."""

    lines = _baseline()
    for seq in range(1, 6):
        _append(
            lines,
            "temperature_turn",
            seq,
            turn_id=f"turn_{seq + 1}",
            status="invalid",
            bt_c=None,
            et_c=None,
            observed_at=_instant(seq + 4),
        )
    report = _report(lines, _expect(max_temperature_age_seconds=8))
    assert _check(report, CheckName.TEMPERATURE_PLAUSIBILITY).status is CheckStatus.SATISFIED
    age = _check(report, CheckName.TEMPERATURE_AGE)
    assert age.reason is CheckReason.STALE
    assert age.derived_seconds is None
    assert [ref.instants[0].value for ref in age.evidence] == [1, 5, 6, 7, 8, 9]
    for bt, et in ((110, 110), (100, 120)):
        _payload(lines, "temperature_turn").update(bt_c=bt, et_c=et)
        result = _check(_report(lines), CheckName.TEMPERATURE_PLAUSIBILITY)
        assert result.status is CheckStatus.SATISFIED
    for bt, et in ((10**400, 120), (100, 121)):
        _payload(lines, "temperature_turn").update(bt_c=bt, et_c=et)
        report = _report(lines)
        assert _check(report, CheckName.TEMPERATURE_PLAUSIBILITY).reason is CheckReason.IMPLAUSIBLE
        assert _check(report, CheckName.TEMPERATURE_AGE).derived_seconds == 9
    _append(lines, "temperature_turn", 6, bt_c=101)
    report = _report(lines)
    assert _check(report, CheckName.TEMPERATURE_PLAUSIBILITY).reason is CheckReason.CONFLICT
    assert _check(report, CheckName.TEMPERATURE_AGE).reason is CheckReason.CONFLICT

    lines = _baseline()
    _append(lines, "temperature_turn", 1, turn_id="earlier", observed_at=_instant(0))
    assert _check(_report(lines), CheckName.TEMPERATURE_AGE).reason is CheckReason.ORDER_CONFLICT
    for line in lines[1:]:
        if line["stream"] == "temperature_turn":
            _object(line["payload"]).update(status="invalid", bt_c=None, et_c=None)
    assert _check(_report(lines), CheckName.TEMPERATURE_AGE).reason is CheckReason.EVIDENCE_ABSENT


def test_b6_room_placement_and_age_are_independent() -> None:
    """The last valid reading supplies placement and age despite later invalid rows."""

    lines = _baseline()
    _payload(lines, "room_reading")["placement_id"] = None
    _append(
        lines,
        "room_reading",
        1,
        status="invalid",
        temperature_c=None,
        observed_at=_instant(9),
        placement_id="room_1",
    )
    report = _report(lines)
    assert _check(report, CheckName.ROOM_AGE).derived_seconds == 8
    assert _check(report, CheckName.ROOM_PLACEMENT).reason is CheckReason.PLACEMENT_ABSENT
    _payload(lines, "room_reading")["placement_id"] = "other"
    report = _report(lines, _expect(max_room_age_seconds=7))
    assert _check(report, CheckName.ROOM_PLACEMENT).reason is CheckReason.EXPECTATION_MISMATCH
    assert _check(report, CheckName.ROOM_AGE).reason is CheckReason.STALE
    _payload(lines, "room_reading")["temperature_c"] = 99
    assert _check(_report(lines), CheckName.ROOM_AGE).reason is CheckReason.IMPLAUSIBLE
    _payload(lines, "room_reading").update(status="invalid", temperature_c=None)
    report = _report(lines)
    assert _check(report, CheckName.ROOM_AGE).reason is CheckReason.EVIDENCE_ABSENT
    assert _check(report, CheckName.ROOM_PLACEMENT).reason is CheckReason.EVIDENCE_ABSENT


def test_b7_human_fc_retains_its_own_identity_and_instants() -> None:
    """Shadow problems leave canonical human evidence byte-identical."""

    baseline = _check(_report(_baseline()), CheckName.HUMAN_FC).model_dump_json()
    for variant in ("malformed", "duplicate", "sequence_conflict"):
        lines = _baseline()
        if variant == "malformed":
            _payload(lines, "shadow_event")["decision_at"] = None
        else:
            _append(lines, "shadow_event", 1 if variant == "duplicate" else 0)
        assert _check(_report(lines), CheckName.HUMAN_FC).model_dump_json() == baseline
    lines = _baseline()
    lines.remove(_row(lines, "human_fc"))
    assert _check(_report(lines), CheckName.HUMAN_FC).reason is CheckReason.EVIDENCE_ABSENT
    for change in ({"event_id": "different"}, {"accepted_at": _instant(3, _M)}):
        lines = _baseline()
        _append(lines, "human_fc", 1, accepted_at=_instant(3))
        assert _check(_report(lines), CheckName.HUMAN_FC).status is CheckStatus.SATISFIED
        _append(lines, "human_fc", 2, **change)
        result = _check(_report(lines), CheckName.HUMAN_FC)
        assert result.reason is CheckReason.CONFLICT
        assert len(result.evidence) == 3
    lines = _baseline()
    _payload(lines, "human_fc")["accepted_at"] = _instant(2**53)
    assert _check(_report(lines), CheckName.HUMAN_FC).reason is CheckReason.INSTANT_OUT_OF_DOMAIN


def test_b8_shadow_timing_and_confirmation_keep_original_evidence() -> None:
    """Shadow onset timing and its selected confirming row have separate results."""

    lines = _baseline()
    _payload(lines, "shadow_event")["decision_at"] = _instant(2)
    report = _report(lines)
    timing = _check(report, CheckName.SHADOW_TIMING)
    assert timing.reason is CheckReason.ORDER_CONFLICT
    assert [instant.value for instant in timing.evidence[0].instants] == [4, 2]
    assert (
        _check(report, CheckName.SHADOW_CONFIRMATION).reason is CheckReason.DEPENDENCY_NOT_SATISFIED
    )
    _append(lines, "shadow_event", 1, event_id="other")
    assert _check(_report(lines), CheckName.SHADOW_TIMING).reason is CheckReason.CONFLICT
    lines = _baseline()
    window = _row(lines, "shadow_window")
    window["seq"] = 1
    result = _check(_report(lines), CheckName.SHADOW_CONFIRMATION)
    assert result.reason is CheckReason.EVIDENCE_ABSENT
    assert [ref.seq for ref in result.evidence] == [0, 1]
    window["seq"] = 0
    for changes in (
        {"status": "candidate"},
        {
            "eligibility": "pre_charge",
            "status": "not_inferred",
            "decision_at": None,
            "probability": None,
        },
        {
            "eligibility": "eligible",
            "status": "confirmed",
            "decision_at": _instant(6),
            "probability": 0.9,
        },
    ):
        _payload(lines, "shadow_window").update(changes)
        result = _check(_report(lines), CheckName.SHADOW_CONFIRMATION)
        assert result.reason is CheckReason.CONFLICT
        assert result.evidence[0].instants == (
            Instant(clock=_A, value=4),
            Instant(clock=_A, value=5),
        )
        assert len(result.evidence) == 2
    _payload(lines, "shadow_window")["decision_at"] = _instant(5, _U)
    result = _check(_report(lines), CheckName.SHADOW_CONFIRMATION)
    assert result.reason is CheckReason.CLOCK_ALIGNMENT_UNRESOLVED


def _scale(source: ClockName, target: ClockName, rate: int | float = 1) -> ClockScale:
    return ClockScale(
        source_clock=source,
        target_clock=target,
        target_per_source=rate,
        valid_from=0,
        valid_to=MAX_COORDINATE,
    )


def _temperature_age(lines: _Lines, **expected: object) -> CheckResult:
    return _check(_report(lines, _expect(**expected)), CheckName.TEMPERATURE_AGE)


def test_b9_sample_rate_origin_and_missing_anchor_or_rate() -> None:
    """Rate, origin, and anchor independently affect a fixed sample observation."""

    lines = _baseline()
    _payload(lines, "temperature_turn")["observed_at"] = _instant(200, _S)
    lines[0]["alignments"] = [_anchor(_S, 100, _A, 7)]
    assert _temperature_age(lines, max_temperature_age_seconds=2).derived_seconds == 2
    assert (
        _temperature_age(
            lines, max_temperature_age_seconds=2, recording_sample_rate_hz=50
        ).derived_seconds
        == 1
    )
    _payload(lines, "temperature_turn")["observed_at"] = _instant(0, _S)
    assert _temperature_age(lines, max_temperature_age_seconds=4).derived_seconds == 4
    result = _temperature_age(lines, max_temperature_age_seconds=4, recording_sample_rate_hz=50)
    assert result.reason is CheckReason.STALE
    lines[0]["alignments"] = [_anchor(_A, 8, _S, 100)]
    assert _temperature_age(lines, max_temperature_age_seconds=3).derived_seconds == 3
    lines[0]["alignments"] = [_anchor(_A, 7, _S, 100)]
    assert _temperature_age(lines, max_temperature_age_seconds=3).reason is CheckReason.STALE
    assert (
        _temperature_age(lines, recording_sample_rate_hz=None).reason
        is CheckReason.CLOCK_ALIGNMENT_UNRESOLVED
    )
    lines[0]["alignments"] = []
    assert _temperature_age(lines).reason is CheckReason.CLOCK_ALIGNMENT_UNRESOLVED


def test_b9_comparisons_in_sample_coordinates_require_rate_even_same_clock() -> None:
    """Sample differences are divided by the bound rate in either temporal check."""

    lines = _baseline()
    _payload(lines, "temperature_turn")["observed_at"] = _instant(100, _S)
    metric = Instant(clock=_S, value=125)
    assert (
        _temperature_age(
            lines, evaluation_at=metric, max_temperature_age_seconds=0.25
        ).derived_seconds
        == 0.25
    )
    assert (
        _temperature_age(
            lines,
            evaluation_at=metric,
            recording_sample_rate_hz=None,
        ).reason
        is CheckReason.CLOCK_ALIGNMENT_UNRESOLVED
    )
    _payload(lines, "shadow_event").update(
        onset_at=_instant(100, _S), decision_at=_instant(125, _S)
    )
    result = _check(_report(lines), CheckName.SHADOW_TIMING)
    assert result.derived_seconds == 0.25
    assert result.evidence[0].instants == (Instant(clock=_S, value=100), metric)


def test_b9_seconds_scales_enforce_declared_validity_in_both_directions() -> None:
    """Reverse alignment reconstructs the source coordinate before testing validity."""

    lines = _baseline()
    lines[0]["alignments"] = [_anchor(_A, 5, _M, 10)]
    _payload(lines, "temperature_turn")["observed_at"] = _instant(12, _M)
    scale = ClockScale(
        source_clock=_A, target_clock=_M, target_per_source=2, valid_from=5, valid_to=6
    )
    assert _temperature_age(lines).reason is CheckReason.CLOCK_ALIGNMENT_UNRESOLVED
    assert _temperature_age(lines, clock_scales=(scale,)).derived_seconds == 4
    _payload(lines, "temperature_turn")["observed_at"] = _instant(13, _M)
    assert (
        _temperature_age(
            lines,
            clock_scales=(scale,),
        ).reason
        is CheckReason.CLOCK_ALIGNMENT_UNRESOLVED
    )
    lines[0]["alignments"] = [_anchor(_M, 10, _A, 5)]
    _payload(lines, "temperature_turn")["observed_at"] = _instant(5, _A)
    metric = Instant(clock=_M, value=14)
    assert _temperature_age(lines, evaluation_at=metric, clock_scales=(scale,)).derived_seconds == 4
    lines[0]["alignments"] = [_anchor(_M, 8, _A, 4)]
    assert (
        _temperature_age(
            lines,
            evaluation_at=metric,
            clock_scales=(scale,),
        ).reason
        is CheckReason.CLOCK_ALIGNMENT_UNRESOLVED
    )


def test_b9_all_usable_paths_agree_and_invalid_paths_do_not_vote() -> None:
    """Direct and indirect projections need exact agreement in the chosen clock."""

    lines = _baseline()
    _payload(lines, "temperature_turn")["observed_at"] = _instant(3, _U)
    lines[0]["alignments"] = [_anchor(_U, 0, _A, 0), _anchor(_U, 0, _M, 0), _anchor(_M, 0, _A, 0)]
    assert (
        _temperature_age(
            lines,
            clock_scales=(_scale(_U, _A), _scale(_U, _M), _scale(_M, _A)),
        ).derived_seconds
        == 7
    )
    assert (
        _temperature_age(
            lines,
            clock_scales=(_scale(_U, _A), _scale(_U, _M), _scale(_M, _A, 2)),
        ).reason
        is CheckReason.CLOCK_PATH_CONFLICT
    )
    limited = ClockScale(
        source_clock=_M, target_clock=_A, target_per_source=2, valid_from=0, valid_to=2
    )
    assert (
        _temperature_age(
            lines,
            clock_scales=(_scale(_U, _A), _scale(_U, _M), limited),
        ).derived_seconds
        == 7
    )


def test_b9_out_of_domain_raw_and_intermediate_coordinates() -> None:
    """An otherwise usable out-of-domain path takes precedence over valid mappings."""

    lines = _baseline()
    _payload(lines, "temperature_turn")["observed_at"] = _instant(2**53)
    result = _temperature_age(lines)
    assert result.reason is CheckReason.INSTANT_OUT_OF_DOMAIN
    assert result.evidence[0].instants[0].value == 2**53
    assert result.derived_seconds is None
    _payload(lines, "temperature_turn")["observed_at"] = _instant(1, _U)
    lines[0]["alignments"] = [
        _anchor(_U, 0, _A, 0),
        _anchor(_U, 0, _S, MAX_COORDINATE),
        _anchor(_S, MAX_COORDINATE, _A, 0),
    ]
    assert (
        _temperature_age(
            lines,
            clock_scales=(_scale(_U, _A),),
            recording_sample_rate_hz=1,
        ).reason
        is CheckReason.INSTANT_OUT_OF_DOMAIN
    )
    lines[0]["alignments"] = [
        _anchor(_U, 0, _A, 0),
        _anchor(_U, 2, _S, 0),
        _anchor(_S, 0, _A, 1),
    ]
    assert (
        _temperature_age(
            lines,
            clock_scales=(_scale(_U, _A),),
            recording_sample_rate_hz=1,
        ).reason
        is CheckReason.INSTANT_OUT_OF_DOMAIN
    )
    _payload(lines, "shadow_event")["onset_at"] = _instant(2**53)
    result = _check(_report(lines), CheckName.SHADOW_TIMING)
    assert result.reason is CheckReason.INSTANT_OUT_OF_DOMAIN


def test_b9_mixed_clock_chronology_and_exact_threshold_comparison() -> None:
    """Age and observation ordering use the same fixed target before rounding."""

    lines = _baseline()
    lines[0]["alignments"] = [_anchor(_M, 10, _A, 0)]
    scale = (_scale(_M, _A),)
    _append(lines, "temperature_turn", 1, turn_id="second", observed_at=_instant(10, _M))
    assert _temperature_age(lines, clock_scales=scale).reason is CheckReason.ORDER_CONFLICT
    last = _object(lines[-1]["payload"])
    last["observed_at"] = _instant(12, _M)
    result = _temperature_age(lines, clock_scales=scale, max_temperature_age_seconds=8)
    assert result.derived_seconds == 8
    assert [ref.instants[0].clock for ref in result.evidence] == [_A, _M]
    _append(lines, "room_reading", 1, observed_at=_instant(11, _M))
    room = _check(_report(lines, _expect(clock_scales=scale)), CheckName.ROOM_AGE)
    assert room.reason is CheckReason.ORDER_CONFLICT
    last["observed_at"] = _instant(21, _M)
    assert _temperature_age(lines, clock_scales=scale).reason is CheckReason.ORDER_CONFLICT

    lines = _baseline()
    _payload(lines, "temperature_turn")["observed_at"] = _instant(0.25, _M)
    lines[0]["alignments"] = [_anchor(_M, 0, _A, 0)]
    assert (
        _temperature_age(
            lines,
            evaluation_at=Instant(clock=_A, value=1),
            clock_scales=(_scale(_M, _A),),
            max_temperature_age_seconds=0.75,
        ).derived_seconds
        == 0.75
    )
    assert (
        _temperature_age(
            lines,
            evaluation_at=Instant(clock=_A, value=1),
            clock_scales=(_scale(_M, _A, 0.9999999999999999),),
            max_temperature_age_seconds=0.75,
        ).reason
        is CheckReason.STALE
    )


_DEPENDENTS = {
    CollectorStream.IDENTITY: {CheckName.IDENTITY},
    CollectorStream.TEMPERATURE_TURN: {
        CheckName.TEMPERATURE_PLAUSIBILITY,
        CheckName.TEMPERATURE_AGE,
    },
    CollectorStream.ROOM_READING: {CheckName.ROOM_AGE, CheckName.ROOM_PLACEMENT},
    CollectorStream.HUMAN_FC: {CheckName.HUMAN_FC},
    CollectorStream.SHADOW_EVENT: {CheckName.SHADOW_TIMING, CheckName.SHADOW_CONFIRMATION},
    CollectorStream.SHADOW_WINDOW: {CheckName.SHADOW_CONFIRMATION},
}


@pytest.mark.parametrize("stream", tuple(CollectorStream))
@pytest.mark.parametrize("malformed", (False, True))
def test_b10_stream_isolation_and_b11_provenance(stream: CollectorStream, malformed: bool) -> None:
    """All 24 isolated stream changes preserve every independent check in full."""

    baseline = _report(_baseline())
    lines = _baseline()
    row = _row(lines, stream.value)
    if malformed:
        row["payload"] = {}
    else:
        lines.remove(row)
    report = _report(lines)
    changed = {
        after.check
        for before, after in zip(baseline.checks, report.checks, strict=True)
        if before != after
    }
    assert changed == _DEPENDENTS.get(stream, set())
    for check in changed:
        result = _check(report, check)
        assert result.reason is (
            CheckReason.EVIDENCE_MALFORMED if malformed else CheckReason.EVIDENCE_ABSENT
        )
        assert all(ref.stream is not stream for ref in result.evidence)
        assert result.derived_seconds is None


def test_b12_reason_precedence_and_status_invariant() -> None:
    """Competing faults and illegal reason/status pairs fail deterministically."""

    lines = _baseline()
    _row(lines, "identity")["payload"] = {}
    result = _check(_report(lines, _expect(component=None)), CheckName.IDENTITY)
    assert result.reason is CheckReason.EVIDENCE_MALFORMED
    result = _check(_report(lines, _expect(component=None, session_id="other")), CheckName.IDENTITY)
    assert result.reason is CheckReason.SCOPE_MISMATCH
    _payload(lines, "temperature_turn")["observed_at"] = _instant(0, _M)
    result = _temperature_age(lines, max_temperature_age_seconds=0)
    assert result.reason is CheckReason.CLOCK_ALIGNMENT_UNRESOLVED

    assert set(REASON_STATUS) == set(CheckReason)
    for reason, status in REASON_STATUS.items():
        assert (
            CheckResult(
                check=CheckName.IDENTITY,
                status=status,
                reason=reason,
                evidence=(),
                derived_seconds=None,
            ).reason
            is reason
        )
        with pytest.raises(pydantic.ValidationError):
            CheckResult(
                check=CheckName.IDENTITY,
                status=CheckStatus.SATISFIED,
                reason=reason,
                evidence=(),
                derived_seconds=None,
            )
    with pytest.raises(pydantic.ValidationError):
        CheckResult(
            check=CheckName.IDENTITY,
            status=CheckStatus.INVALID,
            reason=None,
            evidence=(),
            derived_seconds=None,
        )


def test_b13_new_modules_have_only_allowed_imports_and_functions() -> None:
    """A syntax and fresh-process boundary proof excludes control or I/O imports."""

    for module in (interpretation_schema, interpreter):
        assert module.__file__ is not None
        tree = ast.parse(Path(module.__file__).read_text())
        allowed = {"enum", "fractions", "math", "typing", "pydantic"}
        prefix = "roastpilot_agent.diagnostic_collector."
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(
                    alias.name in allowed or alias.name.startswith(prefix) for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0
                assert node.module is not None
                assert node.module in allowed or node.module.startswith(prefix)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in {"open", "eval", "exec", "__import__"}
        public = [
            node.name
            for node in tree.body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and not node.name.startswith("_")
        ]
        assert public == (["interpret_synthetic_bundle"] if module is interpreter else [])

    program = (
        "import sys\n"
        "from roastpilot_agent.diagnostic_collector import interpret_synthetic_bundle\n"
        "blocked = {'controller', 'safety', 'mcp_client', 'live', 'mcp'}\n"
        "raise SystemExit(bool(blocked & {name.rsplit('.', 1)[-1] for name in sys.modules}))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", program],
        check=False,
        capture_output=True,
        text=True,
        env={"PATH": os.defpath},
    )
    assert result.returncode == 0, result.stderr


def test_b14_full_record_budget_preserves_bounded_ordered_evidence() -> None:
    """Both full streams and the complete byte budget are admitted and interpreted."""

    header = {**_baseline()[0], "session_id": "s"}
    instant = _instant(0, _S)
    temperature = {
        "bt_c": 1,
        "et_c": 1,
        "observed_at": instant,
        "status": "accepted",
        "turn_id": "t",
    }
    window = {
        "start_at": instant,
        "end_at": instant,
        "eligibility": "pre_charge",
        "status": "not_inferred",
        "decision_at": None,
        "probability": None,
    }
    lines: _Lines = [
        header,
        *[
            {
                "schema_version": 1,
                "seq": seq,
                "session_id": "s",
                "stream": stream,
                "payload": payload,
            }
            for stream, payload in (("temperature_turn", temperature), ("shadow_window", window))
            for seq in range(8192)
        ],
    ]
    for stream in ("temperature_turn", "shadow_window"):
        assert sum(row.get("stream") == stream for row in lines[1:]) == 8192
    assert len(lines) - 1 == 16384
    data = _bytes(lines)
    assert len(data) <= 4 * 1024 * 1024
    digest = hashlib.sha256(data).hexdigest()
    admitted = bundle_reader.read_synthetic_bundle(data, expected_sha256=digest)
    assert sum(len(entry.records) for entry in admitted.streams) == 16384
    assert [len(entry.records) for entry in admitted.streams if entry.records] == [8192, 8192]
    report = interpret_synthetic_bundle(
        data,
        expected_sha256=digest,
        expectations=_expect(
            session_id="s",
            evaluation_at=Instant(clock=_S, value=0),
            bt_range_c=CelsiusRange(low_c=1, high_c=1),
            et_range_c=CelsiusRange(low_c=1, high_c=1),
        ),
    )
    result = _check(report, CheckName.TEMPERATURE_AGE)
    assert result.derived_seconds == 0
    assert [ref.seq for ref in result.evidence] == list(range(8192))
    assert all(ref.instants == (Instant(clock=_S, value=0),) for ref in result.evidence)
    assert len(_check(report, CheckName.SHADOW_CONFIRMATION).evidence) == 8192
    assert _check(report, CheckName.OBSERVED_PROVENANCE).reason is CheckReason.SYNTHETIC_EVIDENCE
